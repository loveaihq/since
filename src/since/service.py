"""The four agent-facing tools as plain functions returning plain text: ``since``, ``get``,
``ack`` and ``status``.

Shared by the CLI and the MCP server. Every method returns ``str``; failures are *returned* as
text starting with ``error: `` (the MCP layer turns those into tool errors). ``since`` never
moves a cursor; only ``ack`` does. ``since`` and ``get`` responses (errors included) are written
to the served log exactly as returned; a call with an invalid ``agent_id`` is rejected before
anything is logged. The clock is injected (``now_fn`` returns an aware datetime). Nothing here
calls an LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from since.daemon import META_HEARTBEAT, META_MIN_SCHEDULE
from since.digest import MIN_BUDGET, render_digest
from since.handles import BatchHandle, EvtHandle, HandleError, RecHandle, parse
from since.model import (
    AGENT_ID_RE,
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SCHEMA_CHANGED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
    PRIORITIES,
    Event,
    FieldChange,
)
from since.render import (
    LAYOUT_CHANGED_TEXT,
    NOTE_LINE,
    batch_handle,
    change_text,
    event_body,
    evt_handle,
    rec_handle,
    record_label,
)
from since.sanitize import DIGEST_CAP, GET_CAP, q
from since.store import SourceState, Store
from since.timeutil import fmt_age, fmt_minute, from_iso

META_PRUNED_THROUGH = "pruned_through_seq"  # written by Store.prune

DEFAULT_AGENT = "default"
UNKNOWN_TIME = "unknown"

NowFn = Callable[[], datetime]

_HANDLE_HELP = (
    "expected since://evt/<seq>, since://rec/<source_id>/<key> "
    "or since://batch/<from>-<to>?source=<id>"
)


# --- small helpers ---------------------------------------------------------------------------


def _invalid_agent(agent_id: object) -> str | None:
    """The error text for a bad agent id, or None if it is valid."""
    if isinstance(agent_id, str) and AGENT_ID_RE.fullmatch(agent_id) is not None:
        return None
    return f"error: invalid agent_id {q(agent_id, DIGEST_CAP)}; use letters, digits, _ . -"


def _unknown_source(source: object) -> str:
    return f"error: unknown source {q(source, DIGEST_CAP)}"


def _fmt_time(iso: str | None) -> str:
    """A stored ISO timestamp as shown to agents (``2026-09-29T09:12Z``)."""
    if not iso:
        return UNKNOWN_TIME
    try:
        return fmt_minute(from_iso(iso))
    except ValueError:
        return UNKNOWN_TIME


def _max_chars(budget: int) -> int:
    """Longest text whose token estimate (``ceil(len / 3.5)``) is still within ``budget``."""
    return budget * 7 // 2


def _fit_lines(
    head: Sequence[str], body: Sequence[str], budget: int, trailer: Callable[[int], str]
) -> str:
    """``head`` lines always, then as many ``body`` lines as fit the budget (the largest such
    prefix, counting the trailer line). If body lines were cut, the text ends with
    ``trailer(cut count)``. The head alone may exceed the budget; it is never cut."""
    limit = _max_chars(budget)
    n = len(body)
    base = len("\n".join(head))
    prefix = [0]
    for line in body:
        prefix.append(prefix[-1] + 1 + len(line))
    keep = 0
    for k in range(n, -1, -1):
        length = base + prefix[k]
        if k < n:
            length += 1 + len(trailer(n - k))
        if length <= limit:
            keep = k
            break
    lines = [*head, *body[:keep]]
    if keep < n:
        lines.append(trailer(n - keep))
    return "\n".join(lines)


def _int_meta(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Heartbeat:
    """What the daemon's heartbeat says at a given moment."""

    present: bool
    age_s: float = 0.0
    min_schedule_s: int | None = None

    @property
    def stale(self) -> bool:
        """Older than twice the shortest schedule (unknown schedule: never judged stale)."""
        return (
            self.present
            and self.min_schedule_s is not None
            and self.age_s > 2 * self.min_schedule_s
        )


# --- get: event view -------------------------------------------------------------------------


def _is_long_text(change: FieldChange) -> bool:
    return change.added_chars is not None or change.removed_chars is not None


def _change_line(change: FieldChange, kind: str) -> str:
    """One field change in the ``get`` evt view (values capped at ``GET_CAP``)."""
    if kind == KIND_ADDED and not _is_long_text(change):
        return f"{change.field}: {q(change.new, GET_CAP)}"
    return change_text(change, kind, GET_CAP)


def _selectors(event: Event) -> list[object]:
    selectors = event.detail.get("selectors")
    return list(selectors) if isinstance(selectors, (list, tuple)) else []


def _event_lines(event: Event, key_label: str) -> list[str]:
    """Everything after header and note in the evt view."""
    kind = event.kind
    detail = event.detail
    if kind in (KIND_ADDED, KIND_MODIFIED, KIND_REMOVED):
        lines: list[str] = []
        if event.record_key is not None:
            suffix = " (removed)" if kind == KIND_REMOVED else ""
            record = record_label(event, key_label, GET_CAP)
            handle = rec_handle(event.source_id, event.record_key)
            lines.append(f"record: {record}{suffix}  {handle}")
        lines.extend(_change_line(change, kind) for change in event.field_changes)
        return lines
    if kind == KIND_BASELINE:
        n = int(detail.get("record_count") or 0)
        return [f"baseline: {n} record{'' if n == 1 else 's'}"]
    if kind == KIND_SOURCE_ERROR:
        return [f"error: {q(detail.get('error'), GET_CAP)}"]
    if kind == KIND_SOURCE_RECOVERED:
        since = _fmt_time(detail.get("error_since"))
        return [f"recovered; error since {since}: {q(detail.get('last_error'), GET_CAP)}"]
    if kind == KIND_SCHEMA_CHANGED:
        selectors = _selectors(event)
        if not selectors:
            return [LAYOUT_CHANGED_TEXT]
        return ["selectors matching 0 elements:", *(q(s, GET_CAP) for s in selectors)]
    return []


def _more_changes(n: int) -> str:
    return f"truncated: {n} more change{'' if n == 1 else 's'}"


def _more_fields(n: int) -> str:
    return f"truncated: {n} more field{'' if n == 1 else 's'}"


# --- the service -----------------------------------------------------------------------------


class Service:
    """``since`` / ``get`` / ``ack`` / ``status`` over one open :class:`Store`."""

    def __init__(self, store: Store, now_fn: NowFn) -> None:
        self._store = store
        self._now_fn = now_fn

    # -- heartbeat (shared by since warnings and status) --------------------------------------

    def _heartbeat(self, now: datetime) -> Heartbeat:
        raw = self._store.get_meta(META_HEARTBEAT)
        if raw is None:
            return Heartbeat(present=False)
        try:
            age = (now - from_iso(raw)).total_seconds()
        except ValueError:
            return Heartbeat(present=False)  # unreadable heartbeat counts as none
        min_schedule = _int_meta(self._store.get_meta(META_MIN_SCHEDULE))
        if min_schedule is not None and min_schedule <= 0:
            min_schedule = None
        return Heartbeat(present=True, age_s=max(0.0, age), min_schedule_s=min_schedule)

    def _warnings(self, cursor: int, now: datetime) -> list[str]:
        warnings: list[str] = []
        beat = self._heartbeat(now)
        if not beat.present:
            warnings.append("warning: daemon not running (no heartbeat); data may be stale")
        elif beat.stale:
            assert beat.min_schedule_s is not None
            warnings.append(
                f"warning: daemon heartbeat stale ({fmt_age(beat.age_s)} ago; "
                f"shortest schedule {fmt_age(beat.min_schedule_s)}); data may be stale"
            )
        pruned = _int_meta(self._store.get_meta(META_PRUNED_THROUGH))
        if pruned is not None and cursor < pruned:
            warnings.append(
                f"warning: events {cursor + 1}-{pruned} expired (retention) "
                "before this agent read them"
            )
        return warnings

    # -- since ---------------------------------------------------------------------------------

    def since(
        self,
        agent_id: str = DEFAULT_AGENT,
        budget_tokens: int = 800,
        source: str | None = None,
        via: str = "mcp",
    ) -> str:
        """The digest of events after the agent's cursor. Does not move the cursor."""
        invalid = _invalid_agent(agent_id)
        if invalid is not None:
            return invalid
        now = self._now_fn()
        text = self._digest(agent_id, max(budget_tokens, MIN_BUDGET), source, now)
        args = {"agent_id": agent_id, "budget_tokens": budget_tokens, "source": source}
        self._store.log_served(agent_id, "since", args, text, via, now)
        return text

    def _digest(self, agent_id: str, budget: int, source: str | None, now: datetime) -> str:
        store = self._store
        if source is not None and store.get_source_state(source) is None:
            return _unknown_source(source)
        cursor = store.get_cursor(agent_id)
        events = store.events_after(cursor, source)
        sources = {s.source_id: s for s in store.list_source_states()}
        return render_digest(
            agent_id,
            cursor,
            events,
            sources,
            budget,
            source,
            self._warnings(cursor, now),
            # With no events left after the cursor, a retention gap still moves the cursor
            # forward (to pruned_through_seq) so the agent can ack past the expired range.
            min_next_cursor=_int_meta(store.get_meta(META_PRUNED_THROUGH)),
        )

    # -- get -----------------------------------------------------------------------------------

    def get(
        self,
        handle: str,
        budget_tokens: int = 1500,
        agent_id: str = DEFAULT_AGENT,
        via: str = "mcp",
    ) -> str:
        """Drill into an event, record or batch handle. Every response is logged.

        Surrounding whitespace on the handle is ignored (agents paste handles with spaces or
        newlines); the served log keeps the handle exactly as received.
        """
        invalid = _invalid_agent(agent_id)
        if invalid is not None:
            return invalid
        now = self._now_fn()
        stripped = handle.strip() if isinstance(handle, str) else handle
        text = self._get_text(stripped, max(budget_tokens, MIN_BUDGET))
        args = {"handle": handle, "budget_tokens": budget_tokens, "agent_id": agent_id}
        self._store.log_served(agent_id, "get", args, text, via, now)
        return text

    def _get_text(self, handle: str, budget: int) -> str:
        try:
            parsed = parse(handle)
        except HandleError:
            return f"error: unknown handle {q(handle, DIGEST_CAP)}; {_HANDLE_HELP}"
        if isinstance(parsed, EvtHandle):
            return self._get_evt(parsed, budget)
        if isinstance(parsed, RecHandle):
            return self._get_rec(parsed, budget)
        return self._get_batch(parsed, budget)

    def _key_label(self, source_id: str) -> str:
        state = self._store.get_source_state(source_id)
        return state.key_label if state is not None else ""

    def _get_evt(self, handle: EvtHandle, budget: int) -> str:
        event = self._store.get_event(handle.seq)
        if event is None:
            return f"error: event {handle.seq} not found (expired or never existed)"
        header = (
            f"{evt_handle(event.seq)} · {event.source_id} · {event.kind} · "
            f"importance {event.importance} · {_fmt_time(event.created_at)}"
        )
        body = _event_lines(event, self._key_label(event.source_id))
        return _fit_lines([header, NOTE_LINE], body, budget, _more_changes)

    def _get_rec(self, handle: RecHandle, budget: int) -> str:
        found = self._store.get_record(handle.source_id, handle.key)
        if found is None:
            return "error: record not found"
        record, present, updated_at = found
        header = (
            f"{rec_handle(handle.source_id, handle.key)} · {handle.source_id} · "
            f"{'present' if present else 'removed'} · updated {_fmt_time(updated_at)}"
        )
        body = [f"{name}: {q(record.fields[name], GET_CAP)}" for name in sorted(record.fields)]
        return _fit_lines([header, NOTE_LINE], body, budget, _more_fields)

    def _get_batch(self, handle: BatchHandle, budget: int) -> str:
        if handle.source is not None and self._store.get_source_state(handle.source) is None:
            return _unknown_source(handle.source)
        base = batch_handle(handle.lo, handle.hi, source=handle.source)  # without ``after``
        in_range = self._store.events_in_range(handle.lo, handle.hi, source_id=handle.source)
        total = len(in_range)
        if total == 0:
            return f"{base} · 0 events"
        events = [e for e in in_range if handle.after is None or e.seq > handle.after]
        if not events:
            return f"{base} · {total} events · showing none"

        labels: dict[str, str] = {}
        lines = []
        for e in events:
            if e.source_id not in labels:
                labels[e.source_id] = self._key_label(e.source_id)
            lines.append(f"  {event_body(e, labels[e.source_id], DIGEST_CAP)}  {evt_handle(e.seq)}")

        def header(k: int) -> str:
            return f"{base} · {total} events · showing {events[0].seq}-{events[k - 1].seq}"

        def more(k: int) -> str:
            after = events[k - 1].seq
            return "more: " + batch_handle(handle.lo, handle.hi, handle.source, after)

        # The header (shown range) and the ``more:`` line depend on how many events are shown,
        # so try the largest count first; at least one event is always shown.
        limit = _max_chars(budget)
        n = len(events)
        prefix = [0]
        for line in lines:
            prefix.append(prefix[-1] + 1 + len(line))
        keep = 1
        for k in range(n, 0, -1):
            length = len(header(k)) + 1 + len(NOTE_LINE) + prefix[k]
            if k < n:
                length += 1 + len(more(k))
            if length <= limit:
                keep = k
                break
        out = [header(keep), NOTE_LINE, *lines[:keep]]
        if keep < n:
            out.append(more(keep))
        return "\n".join(out)

    # -- ack -----------------------------------------------------------------------------------

    def ack(self, agent_id: str, cursor: int) -> str:
        """Move the agent's cursor forward to ``cursor`` (equal is a no-op). Not logged."""
        invalid = _invalid_agent(agent_id)
        if invalid is not None:
            return invalid
        now = self._now_fn()
        with self._store.transaction():
            current = self._store.get_cursor(agent_id)
            latest = self._store.max_seq()
            if cursor < current:
                return (
                    f"error: cursor {cursor} is behind current cursor {current} "
                    f"for agent={agent_id}"
                )
            if cursor > latest:
                return f"error: cursor {cursor} is beyond the latest event {latest}"
            if cursor != current:
                self._store.set_cursor(agent_id, cursor, now)
        return f"ok: agent={agent_id} cursor {current} -> {cursor}"

    # -- status --------------------------------------------------------------------------------

    def status(self) -> str:
        """Per-source collection state plus the daemon heartbeat. Not logged."""
        now = self._now_fn()
        beat = self._heartbeat(now)
        if not beat.present:
            daemon = "daemon not running (no heartbeat)"
        elif beat.stale:
            assert beat.min_schedule_s is not None
            daemon = (
                f"daemon heartbeat stale ({fmt_age(beat.age_s)} ago; "
                f"shortest schedule {fmt_age(beat.min_schedule_s)})"
            )
        else:
            daemon = f"daemon heartbeat {fmt_age(beat.age_s)} ago"
        lines = [f"since status · {daemon}", NOTE_LINE]
        states = sorted(
            self._store.list_source_states(),
            key=lambda s: (_priority_rank(s.priority), s.source_id),
        )
        if not states:
            lines.append("no sources registered (run since daemon or since collect)")
        lines.extend(_status_line(s) for s in states)
        return "\n".join(lines)


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def _status_line(state: SourceState) -> str:
    parts = [f"[{state.priority}] {state.source_id} ({state.type})"]
    if state.last_success_at is None and state.last_error_at is None:
        parts.append("never collected")
    else:
        parts.append(f"records {state.record_count}")
        if state.last_success_at is None:
            parts.append("never succeeded")
        else:
            parts.append(f"last success {_fmt_time(state.last_success_at)}")
        if state.in_error:
            error = q(state.last_error, DIGEST_CAP)
            parts.append(f"error since {_fmt_time(state.error_since)}: {error}")
        else:
            parts.append("ok")
    if not state.configured:
        parts.append("not in config")
    return " · ".join(parts)
