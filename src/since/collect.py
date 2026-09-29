"""Collection runner: registers configured sources and runs one collection of one source.

``run_collection`` is the only place that turns a collector result (or failure) into stored
state: snapshot, events and source state. All writes of one run happen in ONE transaction, so a
crash never leaves a half-applied run behind. The (possibly slow) ``collector.collect()`` call
itself runs *before* that transaction opens, so a slow source never holds the database write lock.
Nothing here calls an LLM or reads the clock (``now`` is a parameter).

A run whose ``now`` is older than the last attempt already stored for the source is *stale* (a newer
run committed while this one was collecting): it is discarded, see ``CollectResult.superseded``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

from since.config import Config, SourceConfig
from since.diff import diff
from since.importance import score
from since.model import (
    KIND_BASELINE,
    KIND_REMOVED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
    Record,
    Scalar,
)
from since.sanitize import q
from since.sources import CollectError, Collector, CollectOutput, get_collector, title_fields_for
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


def _check_output(raw: Iterable[Record] | CollectOutput) -> tuple[list[Record], list[str]]:
    """Normalise what ``collect()`` returned (a record list or a ``CollectOutput``) into checked
    ``(records, unavailable_keys)``."""
    if isinstance(raw, CollectOutput):
        records = _check_records(raw.records)
        return records, _check_unavailable(raw.unavailable, {r.key for r in records})
    return _check_records(raw), []


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
    removed; never-seen ones are left out. Errors raised by the store itself are not source
    failures; they propagate after the rollback.

    If the stored state shows an attempt later than ``now`` (a newer run already committed), the
    result is dropped and ``CollectResult(superseded=True)`` returned (D15)."""
    try:
        title_fields = title_fields_for(cfg, collector)
        records, unavailable = _check_output(collector.collect(cfg))
    except Exception as exc:
        return _store_failure(store, cfg, collector, now, _failure_message(exc))
    return _store_success(store, cfg, collector, now, records, unavailable, title_fields)


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
    store: Store, cfg: SourceConfig, collector: Collector, now: datetime, message: str
) -> CollectResult:
    seqs: list[int] = []
    with store.transaction():
        state = _ensure_state(store, cfg, collector)
        if _superseded(state, now):
            return CollectResult(superseded=True)
        cols: dict[str, object] = {"last_error": message, "last_error_at": now}
        if not state.in_error:
            seqs.append(
                store.append_event(
                    cfg.id,
                    KIND_SOURCE_ERROR,
                    now=now,
                    importance=_source_importance(cfg, KIND_SOURCE_ERROR),
                    detail={"error": message},
                )
            )
            cols["in_error"] = True
            cols["error_since"] = now
        store.update_source_state(cfg.id, **cols)
    return CollectResult(seqs, message)


def _store_success(
    store: Store,
    cfg: SourceConfig,
    collector: Collector,
    now: datetime,
    records: list[Record],
    unavailable: list[str],
    title_fields: list[str],
) -> CollectResult:
    seqs: list[int] = []
    with store.transaction():
        state = _ensure_state(store, cfg, collector)
        if _superseded(state, now):
            return CollectResult(superseded=True)
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
        if not state.baselined:
            store.put_records(cfg.id, records, now)
            seqs.append(
                store.append_event(
                    cfg.id,
                    KIND_BASELINE,
                    now=now,
                    importance=_source_importance(cfg, KIND_BASELINE),
                    detail={"record_count": len(records)},
                )
            )
            cols["baselined"] = True
            cols["record_count"] = len(records)  # unavailable keys are left out of a baseline
        else:
            diff_seqs, count = _store_diff(store, cfg, now, records, unavailable, title_fields)
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


def _store_diff(
    store: Store,
    cfg: SourceConfig,
    now: datetime,
    records: list[Record],
    unavailable: list[str],
    title_fields: list[str],
) -> tuple[list[int], int]:
    """Diff against the stored snapshot, append the events and bring the snapshot up to date.
    Returns the event seqs and the number of records present afterwards.

    Added/modified/removed events carry ``detail["title"]``: the title fields of the new record
    (added/modified) or of the last known record (removed).

    Unavailable keys that are in the snapshot are carried forward: hidden from the diff (so they
    are neither modified nor removed) and their snapshot rows left untouched. Unavailable keys
    that are not in the snapshot are ignored."""
    old = store.get_snapshot(cfg.id)
    carried = {key for key in unavailable if key in old}
    comparable = {key: rec for key, rec in old.items() if key not in carried} if carried else old
    drafts = diff(comparable, records, cfg.track_fields)
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
    if removed:
        store.mark_removed(cfg.id, removed, now)
    return seqs, len(records) + len(carried)
