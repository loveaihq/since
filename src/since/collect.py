"""Collection runner: registers configured sources and runs one collection of one source.

``run_collection`` is the only place that turns a collector result (or failure) into stored
state: snapshot, events and source state. All writes of one run happen in ONE transaction, so a
crash never leaves a half-applied run behind. The (possibly slow) ``collector.collect()`` call
itself runs *before* that transaction opens, so a slow source never holds the database write lock.
Nothing here calls an LLM or reads the clock (``now`` is a parameter).

A run whose ``now`` is older than the last attempt already stored for the source is *stale* (a newer
run committed while this one was collecting): it is discarded, see ``CollectResult.superseded``.

Two facts a collector can report beside its records (see ``CollectOutput``): a collection
``window`` (records that aged out of it leave the snapshot without a ``removed`` event, D21) and
the page structure of a web source (``fingerprint`` / ``broken``, D18).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import NamedTuple

from since.config import Config, SourceConfig
from since.diff import diff
from since.importance import score
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_REMOVED,
    KIND_SCHEMA_CHANGED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
    Record,
    Scalar,
)
from since.sanitize import q
from since.sources import (
    CollectError,
    Collector,
    CollectOutput,
    LoginRequired,
    Window,
    get_collector,
    title_fields_for,
    track_fields_for,
)
from since.store import SourceState, Store
from since.timeutil import from_iso

MAX_ERROR_CHARS = 500
MAX_FIELD_NAME_CHARS = 64

_SCALAR_TYPES = (str, int, float, bool, type(None))
# Unicode categories a field name must not contain (D14): control, format, line/paragraph
# separators, private use, surrogates. Same set that ``since.sanitize`` scrubs from values.
_FORBIDDEN_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs"})


class Registration(NamedTuple):
    """Result of :func:`register_sources`."""

    collectable: list[tuple[SourceConfig, Collector]]
    skipped: list[tuple[str, str]]  # (source_id, reason) for types without a collector yet


@dataclass(frozen=True)
class CollectResult:
    """Outcome of one run: seqs of the events it appended (in order) and the failure message
    (``None`` on success; also set on the 2nd, 3rd... consecutive failure, which appends no
    event). ``superseded`` means a newer run had already stored its result, so this one was
    discarded and nothing was stored (D15)."""

    seqs: list[int] = field(default_factory=list)
    error: str | None = None
    superseded: bool = False


# -- registration --------------------------------------------------------------------------------


def _lookup(collectors: Mapping[str, Collector] | None, type_name: str) -> Collector:
    if collectors is None:
        return get_collector(type_name)
    try:
        return collectors[type_name]
    except KeyError:
        raise NotImplementedError(f"source type '{type_name}' is not implemented yet") from None


def register_sources(
    store: Store, config: Config, collectors: Mapping[str, Collector] | None = None
) -> Registration:
    """Validate every source with an implemented type and upsert it (incl. ``key_label``), then
    mark exactly those sources as configured (all others become ``configured = 0``).

    Sources whose type has no collector yet are returned in ``skipped`` and not stored.
    A ``ConfigError`` from ``collector.validate`` propagates and nothing is written.
    ``collectors`` (type -> collector) replaces the built-in registry; used by tests."""
    collectable: list[tuple[SourceConfig, Collector]] = []
    skipped: list[tuple[str, str]] = []
    labels: dict[str, str] = {}
    for cfg in config.sources:
        try:
            collector = _lookup(collectors, cfg.type)
        except NotImplementedError as exc:
            skipped.append((cfg.id, str(exc)))
            continue
        collector.validate(cfg)
        labels[cfg.id] = collector.key_label(cfg)
        collectable.append((cfg, collector))
    with store.transaction():
        for cfg, _collector in collectable:
            store.upsert_source(
                cfg.id, cfg.type, cfg.priority, cfg.schedule_s, key_label=labels[cfg.id]
            )
        store.set_configured(cfg.id for cfg, _collector in collectable)
    return Registration(collectable, skipped)


# -- record checking -----------------------------------------------------------------------------


def _field_name_allowed(name: str) -> bool:
    """Field names reach digests unquoted, so they get the checks a key gets (D14): non-empty, at
    most 64 characters, none of the control/format/separator/private-use/surrogate characters."""
    return 0 < len(name) <= MAX_FIELD_NAME_CHARS and not any(
        unicodedata.category(c) in _FORBIDDEN_NAME_CATEGORIES for c in name
    )


def _check_records(raw: Iterable[Record]) -> list[Record]:
    """Materialise and validate a collector result; raises ``CollectError`` on the first problem:
    not a Record, non-str/empty key, non-str or disallowed field name (D14), non-scalar value,
    duplicate key (D4)."""
    records: list[Record] = []
    counts: dict[str, int] = {}
    good_names: set[str] = set()  # names already checked (the same few names repeat per row)
    for rec in raw:
        if not isinstance(rec, Record):
            raise CollectError(f"collector returned {type(rec).__name__}, expected Record")
        key = rec.key
        if not isinstance(key, str) or not key:
            raise CollectError(f"invalid record key {key!r}: must be a non-empty string")
        if not isinstance(rec.fields, dict):
            raise CollectError(f'record "{key}": fields must be a dict')
        for name, value in rec.fields.items():
            if not isinstance(name, str):
                raise CollectError(f'record "{key}": field name {name!r} must be a string')
            if name not in good_names:
                if not _field_name_allowed(name):
                    raise CollectError(
                        f"field name {q(name, MAX_FIELD_NAME_CHARS)} is not allowed "
                        f"(control characters or longer than {MAX_FIELD_NAME_CHARS} chars)"
                    )
                good_names.add(name)
            if not isinstance(value, _SCALAR_TYPES):
                raise CollectError(
                    f'record "{key}": field "{name}" has non-scalar value of type '
                    f"{type(value).__name__}"
                )
        counts[key] = counts.get(key, 0) + 1
        records.append(rec)
    for key, n in counts.items():
        if n > 1:
            raise CollectError(f'duplicate key "{key}" ({n} records)')
    return records


def _check_unavailable(raw: Iterable[str], record_keys: set[str]) -> list[str]:
    """Validate the ``unavailable`` keys of a ``CollectOutput`` like record keys: non-empty
    strings, unique, and not also returned as a record (a collector bug)."""
    keys: list[str] = []
    seen: set[str] = set()
    for key in raw:
        if not isinstance(key, str) or not key:
            raise CollectError(f"invalid unavailable key {key!r}: must be a non-empty string")
        if key in seen:
            raise CollectError(f"duplicate unavailable key {q(key, 64)}")
        if key in record_keys:
            raise CollectError(f"key {q(key, 64)} is both a record and unavailable")
        seen.add(key)
        keys.append(key)
    return keys


class _Window(NamedTuple):
    """A validated ``Window``: the field name, the parsed start and, for a window that differs per
    scope, the scope field with the parsed start of each scope value."""

    field: str
    start: datetime
    scope_field: str | None = None
    starts: Mapping[str, datetime] = {}

    def start_for(self, fields: Mapping[str, object]) -> datetime:
        """The start that applies to a record with these ``fields`` (``Window.start_for``)."""
        if self.scope_field is not None:
            value = fields.get(self.scope_field)
            if isinstance(value, str) and value in self.starts:
                return self.starts[value]
        return self.start


@dataclass(frozen=True)
class _Checked:
    """A validated collector result."""

    records: list[Record]
    unavailable: list[str] = field(default_factory=list)
    window: _Window | None = None
    fingerprint: str | None = None
    broken: list[str] = field(default_factory=list)


def _check_window(raw: object) -> _Window | None:
    """Validate ``CollectOutput.window``: ``None`` or a ``Window`` with a non-empty str field and
    a ``start`` that ``timeutil.from_iso`` parses (so it carries a timezone)."""
    if raw is None:
        return None
    if not isinstance(raw, Window):
        raise CollectError(f"invalid window: {type(raw).__name__}, expected Window")
    if not isinstance(raw.field, str) or not raw.field:
        raise CollectError("invalid window: field must be a non-empty string")
    if not isinstance(raw.start, str):
        raise CollectError("invalid window: start must be an ISO timestamp string")
    try:
        start = from_iso(raw.start)
    except (ValueError, OverflowError):
        raise CollectError(
            f"invalid window: start {q(raw.start, 64)} is not an ISO timestamp with a timezone"
        ) from None
    scope_field = raw.scope_field
    if scope_field is not None and (not isinstance(scope_field, str) or not scope_field):
        raise CollectError("invalid window: scope_field must be None or a non-empty string")
    if not isinstance(raw.starts, Mapping):
        raise CollectError("invalid window: starts must be a mapping of scope value to timestamp")
    if raw.starts and scope_field is None:
        raise CollectError("invalid window: starts needs a scope_field")
    starts: dict[str, datetime] = {}
    for scope, text in raw.starts.items():
        if not isinstance(scope, str) or not isinstance(text, str):
            raise CollectError("invalid window: starts must map strings to ISO timestamp strings")
        try:
            starts[scope] = from_iso(text)
        except (ValueError, OverflowError):
            raise CollectError(
                f"invalid window: starts[{q(scope, 64)}] {q(text, 64)} "
                "is not an ISO timestamp with a timezone"
            ) from None
    return _Window(raw.field, start, scope_field, starts)


def _check_fingerprint(raw: object) -> str | None:
    if raw is None or (isinstance(raw, str) and raw):
        return raw
    raise CollectError("invalid fingerprint: must be None or a non-empty string")


def _check_broken(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise CollectError(f"invalid broken: {type(raw).__name__}, expected a list of strings")
    for item in raw:
        if not isinstance(item, str):
            raise CollectError(f"invalid broken entry of type {type(item).__name__}: not a string")
    return list(raw)


def _check_output(raw: Iterable[Record] | CollectOutput) -> _Checked:
    """Normalise what ``collect()`` returned (a record list or a ``CollectOutput``) into a
    checked :class:`_Checked`. Raises ``CollectError`` on the first problem."""
    if isinstance(raw, CollectOutput):
        records = _check_records(raw.records)
        return _Checked(
            records,
            _check_unavailable(raw.unavailable, {r.key for r in records}),
            _check_window(raw.window),
            _check_fingerprint(raw.fingerprint),
            _check_broken(raw.broken),
        )
    return _Checked(_check_records(raw))


def _cap(message: str) -> str:
    if len(message) <= MAX_ERROR_CHARS:
        return message
    return message[: MAX_ERROR_CHARS - 1] + "…"


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, CollectError):
        message = str(exc) or type(exc).__name__
    else:
        message = f"{type(exc).__name__}: {exc}"
    return _cap(message)


def _failure_hint(exc: Exception) -> str:
    """What a human has to do about a ``LoginRequired`` failure (its ``hint``); ``""`` for every
    other failure."""
    if not isinstance(exc, LoginRequired):
        return ""
    hint = getattr(exc, "hint", "")
    return _cap(hint) if isinstance(hint, str) else ""


# -- the run -------------------------------------------------------------------------------------


def _ensure_state(store: Store, cfg: SourceConfig, collector: Collector) -> SourceState:
    """The source row, created from ``cfg`` + ``collector.key_label`` if missing."""
    state = store.get_source_state(cfg.id)
    if state is None:
        store.upsert_source(
            cfg.id, cfg.type, cfg.priority, cfg.schedule_s, key_label=collector.key_label(cfg)
        )
        state = store.get_source_state(cfg.id)
        assert state is not None
    return state


def _source_importance(cfg: SourceConfig, kind: str) -> int:
    """Importance of a source-level event (no highlight rules apply)."""
    return score(cfg.priority, kind, [], {}, cfg.highlight)


def run_collection(
    store: Store, cfg: SourceConfig, collector: Collector, now: datetime
) -> CollectResult:
    """Collect ``cfg`` once and store the outcome.

    Any exception from ``collect()``, or an invalid result (see ``_check_records``), is a
    *failure*: one ``source_error`` event on the first failure of a streak, snapshot untouched,
    never ``removed`` events. Otherwise: ``source_recovered`` (if the source was in error), then
    either the one ``baseline`` event (first success) or the diff against the snapshot. Keys the
    collector reports as ``unavailable`` (D5) keep their last known record: unchanged, never
    removed; never-seen ones are left out. Records that aged out of the collector's ``window``
    (D21) leave the snapshot without an event. Errors raised by the store itself are not source
    failures; they propagate after the rollback.

    A collector that reports ``broken`` selectors (D18 revised) never has that result diffed, with
    or without a page ``fingerprint`` (see ``_store_success``); one that reports a ``fingerprint``
    also gets its layout changes reported as ``schema_changed``.

    A ``LoginRequired`` failure (D24 revised) is always surfaced: while the source is already in
    error, one whose message differs from the last *announced* error (``announced_error``) still
    appends a ``source_error``, carrying the exception's ``hint`` in ``detail["hint"]``.

    The diff is limited to ``track_fields_for(cfg, collector)`` (D26): the configured
    ``track_fields``, else the collector's default, else every field.

    If the stored state shows an attempt later than ``now`` (a newer run already committed), the
    result is dropped and ``CollectResult(superseded=True)`` returned (D15)."""
    try:
        title_fields = title_fields_for(cfg, collector)
        track_fields = track_fields_for(cfg, collector)
        checked = _check_output(collector.collect(cfg))
    except Exception as exc:
        return _store_failure(
            store,
            cfg,
            collector,
            now,
            _failure_message(exc),
            login=isinstance(exc, LoginRequired),
            hint=_failure_hint(exc),
        )
    return _store_success(store, cfg, collector, now, checked, title_fields, track_fields)


def _superseded(state: SourceState, now: datetime) -> bool:
    """True if a run with a later time than ``now`` has already stored its result. Stored times
    have whole-second resolution, so ``now`` is truncated the same way (equal is not later)."""
    horizon = now.replace(microsecond=0)
    for stamp in (state.last_success_at, state.last_error_at):
        if stamp is None:
            continue
        try:
            if from_iso(stamp) > horizon:
                return True
        except ValueError:
            continue  # unreadable stamp: cannot prove a newer run
    return False


def _store_failure(
    store: Store,
    cfg: SourceConfig,
    collector: Collector,
    now: datetime,
    message: str,
    *,
    login: bool = False,
    hint: str = "",
) -> CollectResult:
    with store.transaction():
        state = _ensure_state(store, cfg, collector)
        if _superseded(state, now):
            return CollectResult(superseded=True)
        return _apply_failure(store, cfg, state, now, message, login=login, hint=hint)


def _apply_failure(
    store: Store,
    cfg: SourceConfig,
    state: SourceState,
    now: datetime,
    message: str,
    *,
    login: bool = False,
    hint: str = "",
) -> CollectResult:
    """Record a failed run (inside the run's transaction, after the staleness check): one
    ``source_error`` if the source was not in error yet, and always the latest message/time.

    ``login`` (a ``LoginRequired`` failure, D24 revised) also appends a ``source_error`` when the
    source is already in error but ``message`` differs from the last *announced* error
    (``announced_error``, not ``last_error``: a plain failure in between overwrites ``last_error``
    but announces nothing, so the same login problem is never announced twice): what a human has
    to do changed, and the agent must learn it. ``hint`` (what the human has to do) is stored
    with the event. Every appended ``source_error`` sets ``announced_error``; ``error_since``
    stays as it was."""
    seqs: list[int] = []
    cols: dict[str, object] = {"last_error": message, "last_error_at": now}
    announce = not state.in_error or (login and state.announced_error != message)
    if announce:
        detail: dict[str, object] = {"error": message}
        if hint:
            detail["hint"] = hint
        seqs.append(
            store.append_event(
                cfg.id,
                KIND_SOURCE_ERROR,
                now=now,
                importance=_source_importance(cfg, KIND_SOURCE_ERROR),
                detail=detail,
            )
        )
        cols["announced_error"] = message
    if not state.in_error:
        cols["in_error"] = True
        cols["error_since"] = now
    store.update_source_state(cfg.id, **cols)
    return CollectResult(seqs, message)


def _broken_message(broken: list[str]) -> str:
    """The failure message of an extraction whose selectors matched nothing (D18)."""
    quoted = ", ".join(q(selector, 120) for selector in broken)
    return _cap(f"extractor selector(s) match 0 elements: {quoted}")


def _apply_broken(
    store: Store,
    cfg: SourceConfig,
    state: SourceState,
    now: datetime,
    checked: _Checked,
    message: str,
) -> CollectResult:
    """A baselined source whose extractor selectors matched nothing (D18). The result is not
    trusted: no diff, snapshot and record count untouched, so a layout change never produces a
    wave of ``removed``. The source goes (or stays) in error with ``message``; the first
    ``schema_changed`` event comes instead of a ``source_error``, and another one only when the
    sorted ``broken`` selectors differ from the stored ones, so a lasting broken state is announced
    once; the message it announces becomes ``announced_error`` (D24 revised). The stored
    ``fingerprint`` is NOT touched (D25): it stays the last *good* one, so a page that comes back
    unchanged recovers with just ``source_recovered``."""
    seqs: list[int] = []
    pair = sorted(checked.broken)
    cols: dict[str, object] = {
        "last_error": message,
        "last_error_at": now,
        "broken": pair,
    }
    # re-announce when something else (e.g. a login problem) was announced since (D24)
    if pair != state.broken or state.announced_error != message:
        seqs.append(
            store.append_event(
                cfg.id,
                KIND_SCHEMA_CHANGED,
                now=now,
                importance=_source_importance(cfg, KIND_SCHEMA_CHANGED),
                detail={"selectors": list(checked.broken)},
            )
        )
        cols["announced_error"] = message
    if not state.in_error:
        cols["in_error"] = True
        cols["error_since"] = now
    store.update_source_state(cfg.id, **cols)
    return CollectResult(seqs, message)


def _store_success(
    store: Store,
    cfg: SourceConfig,
    collector: Collector,
    now: datetime,
    checked: _Checked,
    title_fields: list[str],
    track_fields: list[str] | None,
) -> CollectResult:
    """Store a valid result. Page structure (D18 revised):

    - broken selectors, not baselined yet: a run failure (a source that never worked must not
      baseline an empty page);
    - broken selectors, baselined: :func:`_apply_broken`. Both hold whether or not the collector
      reports a fingerprint;
    - no broken selectors: ``broken`` is cleared; with a fingerprint it is stored, and if the
      stored one (the last good one, D25) differed, a ``schema_changed`` with no selectors
      precedes the normal diff.

    A success that ends an error streak appends ``source_recovered`` and clears
    ``announced_error``."""
    seqs: list[int] = []
    fingerprint = checked.fingerprint
    with store.transaction():
        state = _ensure_state(store, cfg, collector)
        if _superseded(state, now):
            return CollectResult(superseded=True)
        if checked.broken:
            message = _broken_message(sorted(checked.broken))
            if not state.baselined:
                return _apply_failure(store, cfg, state, now, message)
            return _apply_broken(store, cfg, state, now, checked, message)
        cols: dict[str, object] = {"last_success_at": now}
        if state.in_error:
            seqs.append(
                store.append_event(
                    cfg.id,
                    KIND_SOURCE_RECOVERED,
                    now=now,
                    importance=_source_importance(cfg, KIND_SOURCE_RECOVERED),
                    detail={"error_since": state.error_since, "last_error": state.last_error},
                )
            )
            cols["in_error"] = False
            cols["error_since"] = None
            cols["announced_error"] = None
        cols["broken"] = []  # whatever was broken is not any more (also without a fingerprint)
        if fingerprint is not None:
            cols["fingerprint"] = fingerprint
        if not state.baselined:
            store.put_records(cfg.id, checked.records, now)
            seqs.append(
                store.append_event(
                    cfg.id,
                    KIND_BASELINE,
                    now=now,
                    importance=_source_importance(cfg, KIND_BASELINE),
                    detail={"record_count": len(checked.records)},
                )
            )
            cols["baselined"] = True
            # unavailable keys are left out of a baseline; the window is ignored by it
            cols["record_count"] = len(checked.records)
        else:
            if (
                fingerprint is not None
                and state.fingerprint is not None
                and state.fingerprint != fingerprint
            ):
                seqs.append(
                    store.append_event(
                        cfg.id,
                        KIND_SCHEMA_CHANGED,
                        now=now,
                        importance=_source_importance(cfg, KIND_SCHEMA_CHANGED),
                        detail={"selectors": []},
                    )
                )
            diff_seqs, count = _store_diff(store, cfg, now, checked, title_fields, track_fields)
            seqs.extend(diff_seqs)
            cols["record_count"] = count
        store.update_source_state(cfg.id, **cols)
    return CollectResult(seqs, None)


def _title_detail(title_fields: list[str], fields: Mapping[str, Scalar]) -> dict[str, object]:
    """``{"title": [[field, value], ...]}`` for the title fields present in ``fields``, in
    title-field order (D17); ``{}`` when there is none, so untitled events look as before."""
    title = [[name, _short(fields[name])] for name in title_fields if name in fields]
    return {"title": title} if title else {}


def _short(value: Scalar) -> Scalar:
    """Title values are labels: a long text field configured as a title is stored cut to 200."""
    return value[:200] if isinstance(value, str) else value


def _aged_out(
    comparable: Mapping[str, Record], present: set[str], window: _Window | None
) -> set[str]:
    """Keys of ``comparable`` (the old records that take part in the diff) that are absent from
    the new result (``present``) and dated before the window start that applies to them (D21;
    ``Window.start_for``: per scope when the window has one). The dates are compared as parsed
    datetimes. A missing, non-str or unparseable value never ages a record out: it stays an
    ordinary removal."""
    if window is None:
        return set()
    aged: set[str] = set()
    for key, record in comparable.items():
        if key in present:
            continue
        value = record.fields.get(window.field)
        if not isinstance(value, str):
            continue
        try:
            dated = from_iso(value)
        except (ValueError, OverflowError):
            continue
        if dated < window.start_for(record.fields):
            aged.add(key)
    return aged


def _store_diff(
    store: Store,
    cfg: SourceConfig,
    now: datetime,
    checked: _Checked,
    title_fields: list[str],
    track_fields: list[str] | None,
) -> tuple[list[int], int]:
    """Diff against the stored snapshot, append the events and bring the snapshot up to date.
    Returns the event seqs and the number of records present afterwards. The diff is limited to
    ``track_fields`` (``None`` = all fields; D26).

    Added/modified/removed events carry ``detail["title"]``: the title fields of the new record
    (added/modified) or of the last known record (removed).

    Unavailable keys that are in the snapshot are carried forward: hidden from the diff (so they
    are neither modified nor removed) and their snapshot rows left untouched. Unavailable keys
    that are not in the snapshot are ignored (and are therefore never aged out either).

    Old records that are absent from the result and older than the collector's window have aged
    out (D21): dropped from the snapshot (``mark_removed``) without an event, and not counted."""
    records = checked.records
    old = store.get_snapshot(cfg.id)
    carried = {key for key in checked.unavailable if key in old}
    comparable = {key: rec for key, rec in old.items() if key not in carried} if carried else old
    aged = _aged_out(comparable, {r.key for r in records}, checked.window)
    if aged:
        comparable = {key: rec for key, rec in comparable.items() if key not in aged}
    drafts = diff(comparable, records, track_fields)
    if cfg.track_fields is None:  # collector-default track fields don't decorate `+` lines (D26)
        drafts = [replace(d, changes=[]) if d.kind == KIND_ADDED else d for d in drafts]
    seqs = [
        store.append_event(
            cfg.id,
            draft.kind,
            now=now,
            record_key=draft.key,
            field_changes=draft.changes,
            importance=score(cfg.priority, draft.kind, draft.changes, draft.fields, cfg.highlight),
            detail=_title_detail(title_fields, draft.fields),
        )
        for draft in drafts
    ]
    # Snapshot: new records and every record whose content differs, including changes to
    # untracked fields only (no event, but the next diff must compare against the new content).
    changed = [r for r in records if r.key not in old or old[r.key].content_hash != r.content_hash]
    if changed:
        store.put_records(cfg.id, changed, now)
    removed = [d.key for d in drafts if d.kind == KIND_REMOVED]
    removed.extend(sorted(aged))
    if removed:
        store.mark_removed(cfg.id, removed, now)
    return seqs, len(records) + len(carried)
