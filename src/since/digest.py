"""The `since` digest: ranked, grouped, token-budgeted text for an agent. Pure (no DB access).

See ``docs/plan-m1.md`` "Output formats -> `since` digest" for the format rules; the numbers in
comments below refer to them.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from itertools import accumulate
from typing import TYPE_CHECKING

from since.model import (
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
    KIND_WEIGHT,
    PRIORITY_WEIGHT,
    Event,
)
from since.render import (
    NOTE_LINE,
    batch_handle,
    estimate_tokens,
    event_body,
    evt_handle,
    lists_selectors,
)
from since.sanitize import DIGEST_CAP

if TYPE_CHECKING:
    from since.store import SourceState

MIN_BUDGET = 200
UNKNOWN_PRIORITY = "?"
RECOVERED_SUFFIX = " (recovered)"


class _Digest:
    """Everything needed to render the digest for any K (number of shown events)."""

    def __init__(
        self,
        agent_id: str,
        cursor: int,
        events: Sequence[Event],
        sources: Mapping[str, SourceState],
        budget: int,
        source_filter: str | None,
        warnings: Sequence[str],
    ) -> None:
        self.agent_id = agent_id
        self.budget = budget
        self.source_filter = source_filter
        self.warnings = list(warnings)
        self.sources = sources

        # Rule 1.
        selected = [
            e
            for e in events
            if e.seq > cursor and (source_filter is None or e.source_id == source_filter)
        ]
        # A source_error or selector-listing schema_changed is resolved (D28) once the same source
        # has a later source_recovered among the digest's events, shown or not: it is marked
        # " (recovered)" and ranked as a source_recovered would be. Only digest lines carry that.
        self.recovered_seq: dict[str, int] = {}
        for e in selected:
            if e.kind == KIND_SOURCE_RECOVERED:
                latest = self.recovered_seq.get(e.source_id, 0)
                self.recovered_seq[e.source_id] = max(latest, e.seq)
        # Rule 5: global rank = (effective importance desc, seq asc), a total order (seq is
        # unique). ``rank_importance[i]`` is the effective importance of ``ranked[i]``; the stored
        # ``Event.importance`` is never changed.
        importance = {e.seq: self._effective_importance(e) for e in selected}
        self.ranked = sorted(selected, key=lambda e: (-importance[e.seq], e.seq))
        self.rank_importance = [importance[e.seq] for e in self.ranked]
        self.lines = [self._event_line(e) for e in self.ranked]

        self.first = min((e.seq for e in self.ranked), default=0)
        self.last = max((e.seq for e in self.ranked), default=0)
        # Per source: indices into ``ranked``, ascending (= importance desc, seq asc).
        self.by_source: dict[str, list[int]] = {}
        for i, e in enumerate(self.ranked):
            self.by_source.setdefault(e.source_id, []).append(i)

    def _resolved(self, event: Event) -> bool:
        """A source_error, or a schema_changed that names selectors, that a later source_recovered
        of the same source has resolved (D28)."""
        if event.kind != KIND_SOURCE_ERROR and not lists_selectors(event):
            return False
        return self.recovered_seq.get(event.source_id, 0) > event.seq

    def _effective_importance(self, event: Event) -> int:
        """The importance the digest ranks by: the stored one, except for a resolved event, which
        weighs what a source_recovered of that source does (priority weight x 1). The priority is
        the source's current one; for a source without a state row it is recovered from the
        stored importance (source-level events have no highlight bonus)."""
        if not self._resolved(event):
            return event.importance
        recovered = KIND_WEIGHT[KIND_SOURCE_RECOVERED]
        state = self.sources.get(event.source_id)
        weight = PRIORITY_WEIGHT.get(state.priority) if state is not None else None
        if weight is not None:
            return weight * recovered
        return event.importance // KIND_WEIGHT[event.kind] * recovered

    def _event_line(self, event: Event) -> str:
        state = self.sources.get(event.source_id)
        key_label = state.key_label if state is not None else ""
        if self._resolved(event):
            # what a human had to do is done: no "needs a human" on a resolved error
            body = event_body(event, key_label, DIGEST_CAP, with_hint=False) + RECOVERED_SUFFIX
        else:
            body = event_body(event, key_label, DIGEST_CAP)
        return f"  {body}  {evt_handle(event.seq)}"

    # --- rendering ---------------------------------------------------------------------------

    def _header(self, shown: int) -> str:
        parts = [f"since · agent={self.agent_id}"]
        if self.source_filter is not None:
            parts.append(f"source={self.source_filter}")
        events_part = f"events {self.first}-{self.last} ({len(self.ranked)})"
        if shown < len(self.ranked):
            events_part += f", showing {shown}"
        parts.append(events_part)
        parts.append(f"budget {self.budget}")
        if self.source_filter is None:
            parts.append(f"next_cursor={self.last}")
        return " · ".join(parts)

    def _priority(self, source_id: str) -> str:
        state = self.sources.get(source_id)
        return state.priority if state is not None else UNKNOWN_PRIORITY

    def render(self, k: int) -> str:
        """Full digest text showing the top ``k`` events of the global ranking."""
        # A source's indices are in rule-4 event order; the first shown one carries its max
        # shown importance, the first omitted one its max omitted importance.
        shown: dict[str, list[int]] = {}
        omitted: dict[str, tuple[int, int]] = {}  # source -> (count, max importance)
        for sid, idxs in self.by_source.items():
            n_shown = bisect_left(idxs, k)
            if n_shown:
                shown[sid] = idxs[:n_shown]
            if n_shown < len(idxs):
                omitted[sid] = (len(idxs) - n_shown, self.rank_importance[idxs[n_shown]])

        lines = [self._header(k), *self.warnings, NOTE_LINE]

        # Rule 4: groups by max importance of shown events desc, then source_id asc.
        for sid in sorted(shown, key=lambda s: (-self.rank_importance[shown[s][0]], s)):
            total = len(self.by_source[sid])
            count = (
                f"{total}" if len(shown[sid]) == total else f"{total}, showing {len(shown[sid])}"
            )
            lines.append(f"[{self._priority(sid)}] {sid} ({count})")
            lines.extend(self.lines[i] for i in shown[sid])

        # Rule 6: one line per source with omitted events, by that source's max omitted
        # importance desc, then id.
        for sid in sorted(omitted, key=lambda s: (-omitted[s][1], s)):
            handle = batch_handle(self.first, self.last, source=sid)
            lines.append(f"omitted: {sid} {omitted[sid][0]} {handle}")

        # Rule 7.
        if self.source_filter is None:
            lines.append(f"after handling: ack(cursor={self.last})")
        else:
            lines.append("filtered view: call since() without source before ack")
        return "\n".join(lines)

    def choose_k(self) -> int:
        """Rule 5: the largest K whose full rendered text fits the budget; 0 if none does.

        The rendered size is not monotone in K (at K = N the omitted lines and ``showing`` parts
        disappear, and a source that becomes fully shown drops its omitted line), so plain
        binary search on "fits" could stop short of the largest K. Instead: binary search the
        largest K whose event lines alone fit (a monotone lower bound on the size, hence an
        upper bound on K), then step down until the full text fits.
        """
        max_chars = self.budget * 7 // 2  # ceil(len / 3.5) <= budget  <=>  len <= 3.5 * budget
        prefix = list(accumulate(len(line) + 1 for line in self.lines))
        for k in range(bisect_right(prefix, max_chars), 0, -1):
            if estimate_tokens(self.render(k)) <= self.budget:
                return k
        return 0


def render_digest(
    agent_id: str,
    cursor: int,
    events: Sequence[Event],
    sources: Mapping[str, SourceState],
    budget: int,
    source_filter: str | None = None,
    warnings: Sequence[str] = (),
    min_next_cursor: int | None = None,
) -> str:
    """Render the digest text (lines joined with ``\\n``, no trailing newline).

    ``events`` may hold any events; only those with ``seq > cursor`` (and ``source_id ==
    source_filter`` when given) are used. ``warnings`` are complete lines, already starting with
    ``warning: ``. A budget below ``MIN_BUDGET`` is raised to it and the header shows that value.
    ``min_next_cursor`` (the retention floor, ``pruned_through_seq``) only matters when there are
    no events and no source filter: if it is above ``cursor`` it becomes the next cursor (rule 8).
    """
    budget = max(budget, MIN_BUDGET)
    digest = _Digest(agent_id, cursor, events, sources, budget, source_filter, warnings)
    if not digest.ranked:
        # Rule 8: no note; a footer only when the retention floor is ahead of the cursor.
        if source_filter is not None:
            head = (
                f"since · agent={agent_id} · source={source_filter}"
                f" · no new events after cursor {cursor}"
            )
            return "\n".join([head, *warnings])
        next_cursor = cursor if min_next_cursor is None else max(cursor, min_next_cursor)
        head = f"since · agent={agent_id} · no new events after cursor {cursor}"
        head += f" · next_cursor={next_cursor}"
        lines = [head, *warnings]
        if next_cursor > cursor:
            lines.append(f"after handling: ack(cursor={next_cursor})")
        return "\n".join(lines)
    return digest.render(digest.choose_k())
