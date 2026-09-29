"""Deterministic, rule-based event importance. Pure: no DB, no clock, no I/O.

``importance = PRIORITY_WEIGHT[priority] * KIND_WEIGHT[kind] + sum(bonus of matching rules)``.
Highlight rules only apply to record events (added / modified / removed); see plan "Weights".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from since.config import HighlightRule
from since.model import (
    KIND_ADDED,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_WEIGHT,
    PRIORITY_WEIGHT,
    FieldChange,
    Scalar,
)

_RECORD_KINDS = frozenset({KIND_ADDED, KIND_MODIFIED, KIND_REMOVED})


def _text(value: Scalar) -> str | None:
    """Value as compared by rules: ``str(value)``; None never matches anything."""
    return None if value is None else str(value)


def _changed_to(
    rule: HighlightRule,
    kind: str,
    changes: Sequence[FieldChange],
    fields: Mapping[str, Scalar],
) -> bool:
    if kind != KIND_MODIFIED:
        return False
    for change in changes:
        if change.field != rule.field:
            continue
        # Long-text changes carry no values, so compare against the new record's value.
        new_value = fields.get(rule.field) if change.added_chars is not None else change.new
        if _text(new_value) == rule.value:
            return True
    return False


def _matches(
    rule: HighlightRule,
    kind: str,
    changes: Sequence[FieldChange],
    fields: Mapping[str, Scalar],
) -> bool:
    if rule.op == "changed_to":
        return _changed_to(rule, kind, changes, fields)
    current = _text(fields.get(rule.field))
    if current is None:
        return False
    if rule.op == "equals":
        return current == rule.value
    if rule.op == "contains":
        return rule.value.casefold() in current.casefold()
    return False


def score(
    priority: str,
    kind: str,
    changes: Sequence[FieldChange],
    fields: Mapping[str, Scalar],
    rules: Sequence[HighlightRule],
) -> int:
    """Importance of one event.

    ``fields`` are the record's current fields (for removed: the last known fields). Every
    matching rule adds its own bonus. Unknown priority or kind raises ``KeyError``."""
    total = PRIORITY_WEIGHT[priority] * KIND_WEIGHT[kind]
    if kind in _RECORD_KINDS:
        total += sum(r.bonus for r in rules if _matches(r, kind, changes, fields))
    return total
