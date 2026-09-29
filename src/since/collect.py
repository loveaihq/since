"""Collection runner: registers configured sources and runs one collection of one source.

``run_collection`` is the only place that turns a collector result (or failure) into stored
state: snapshot, events and source state. All writes of one run happen in ONE transaction, so a
crash never leaves a half-applied run behind. The (possibly slow) ``collector.collect()`` call
itself runs *before* that transaction opens, so a slow source never holds the database write lock.
Nothing here calls an LLM or reads the clock (``now`` is a parameter).
"""

from __future__ import annotations

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
)
from since.sources import CollectError, Collector, get_collector
from since.store import SourceState, Store

MAX_ERROR_CHARS = 500

_SCALAR_TYPES = (str, int, float, bool, type(None))


class Registration(NamedTuple):
    """Result of :func:`register_sources`."""

    collectable: list[tuple[SourceConfig, Collector]]
    skipped: list[tuple[str, str]]  # (source_id, reason) for types without a collector yet


@dataclass(frozen=True)
class CollectResult:
    """Outcome of one run: seqs of the events it appended (in order) and the failure message
    (``None`` on success; also set on the 2nd, 3rd... consecutive failure, which appends no
    event)."""

    seqs: list[int] = field(default_factory=list)
    error: str | None = None


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


def _check_records(raw: Iterable[Record]) -> list[Record]:
    """Materialise and validate a collector result; raises ``CollectError`` on the first problem:
    not a Record, non-str/empty key, non-str field name, non-scalar value, duplicate key (D4)."""
    records: list[Record] = []
    counts: dict[str, int] = {}
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
    either the one ``baseline`` event (first success) or the diff against the snapshot. Errors
    raised by the store itself are not source failures; they propagate after the rollback."""
    try:
        records = _check_records(collector.collect(cfg))
    except Exception as exc:
        return _store_failure(store, cfg, collector, now, _failure_message(exc))
    return _store_success(store, cfg, collector, now, records)


def _store_failure(
    store: Store, cfg: SourceConfig, collector: Collector, now: datetime, message: str
) -> CollectResult:
    seqs: list[int] = []
    with store.transaction():
        state = _ensure_state(store, cfg, collector)
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
    store: Store, cfg: SourceConfig, collector: Collector, now: datetime, records: list[Record]
) -> CollectResult:
    seqs: list[int] = []
    with store.transaction():
        state = _ensure_state(store, cfg, collector)
        cols: dict[str, object] = {"last_success_at": now, "record_count": len(records)}
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
        else:
            seqs.extend(_store_diff(store, cfg, now, records))
        store.update_source_state(cfg.id, **cols)
    return CollectResult(seqs, None)


def _store_diff(
    store: Store, cfg: SourceConfig, now: datetime, records: list[Record]
) -> list[int]:
    """Diff against the stored snapshot, append the events and bring the snapshot up to date."""
    old = store.get_snapshot(cfg.id)
    drafts = diff(old, records, cfg.track_fields)
    seqs = [
        store.append_event(
            cfg.id,
            draft.kind,
            now=now,
            record_key=draft.key,
            field_changes=draft.changes,
            importance=score(cfg.priority, draft.kind, draft.changes, draft.fields, cfg.highlight),
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
    return seqs
