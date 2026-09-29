"""Pure diff engine: compares a stored snapshot with a fresh collection.

No DB, no clock, no I/O. The collection runner (``since.collect``) turns each ``Draft`` into a
stored event; importance is computed separately by ``since.importance.score``.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from since.model import KIND_ADDED, KIND_MODIFIED, KIND_REMOVED, FieldChange, Record, Scalar

# A str value longer than this is "long text": diffs report char counts, never the text itself.
LONG_TEXT_CHARS = 200

# Line-level ``replace`` chunks up to this combined size are refined at char level.
_REFINE_MAX_CHARS = 5000


@dataclass(frozen=True)
class Draft:
    """An event that is about to be written. ``fields`` holds the new record's fields for
    added/modified and the last known (old) fields for removed."""

    kind: str
    key: str
    changes: list[FieldChange]
    fields: dict[str, Scalar]


def _lines(value: object) -> list[str]:
    return value.splitlines(keepends=True) if isinstance(value, str) else []


def text_change_stats(old: object, new: object) -> tuple[int, int]:
    """Return ``(added_chars, removed_chars)`` between two text values.

    Line-level diff first; ``delete``/``insert`` count whole lines, ``replace`` chunks of at most
    5000 combined chars are refined with a char-level diff (larger ones count whole lines).
    A missing or non-str side counts as the empty string."""
    old_lines = _lines(old)
    new_lines = _lines(new)
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_chunk = "".join(old_lines[i1:i2])
        new_chunk = "".join(new_lines[j1:j2])
        if tag == "replace" and len(old_chunk) + len(new_chunk) <= _REFINE_MAX_CHARS:
            chars = difflib.SequenceMatcher(None, old_chunk, new_chunk, autojunk=False)
            for ctag, a1, a2, b1, b2 in chars.get_opcodes():
                if ctag == "equal":
                    continue
                removed += a2 - a1
                added += b2 - b1
        else:
            removed += len(old_chunk)
            added += len(new_chunk)
    return added, removed


def _is_long(old: Scalar, new: Scalar) -> bool:
    return (isinstance(old, str) and len(old) > LONG_TEXT_CHARS) or (
        isinstance(new, str) and len(new) > LONG_TEXT_CHARS
    )


def _make_change(name: str, old: Scalar, new: Scalar) -> FieldChange:
    if _is_long(old, new):
        added, removed = text_change_stats(old, new)
        return FieldChange(name, None, None, added, removed)
    return FieldChange(name, old, new)


def _differs(old: Scalar, new: Scalar) -> bool:
    """True if two field values differ. Type-aware (``1`` vs ``True`` vs ``1.0`` differ, matching
    the content hash); NaN equals NaN."""
    if type(old) is not type(new):
        return True
    if old != new:
        return not (old != old and new != new)  # both NaN -> same
    return False


def _added(rec: Record, tracked: list[str] | None) -> Draft:
    changes: list[FieldChange] = []
    if tracked is not None:
        changes = [_make_change(n, None, rec.fields[n]) for n in tracked if n in rec.fields]
    return Draft(KIND_ADDED, rec.key, changes, dict(rec.fields))


def _modified(key: str, prev: Record, rec: Record, tracked: list[str] | None) -> Draft | None:
    if prev.content_hash == rec.content_hash:
        return None
    names = tracked if tracked is not None else sorted(prev.fields.keys() | rec.fields.keys())
    changes = []
    for n in names:
        old_v = prev.fields.get(n)
        new_v = rec.fields.get(n)
        if _differs(old_v, new_v):
            changes.append(_make_change(n, old_v, new_v))
    if not changes:
        return None
    return Draft(KIND_MODIFIED, key, changes, dict(rec.fields))


def diff(
    old: Mapping[str, Record],
    new: Iterable[Record],
    track_fields: list[str] | None,
) -> list[Draft]:
    """Compare the old snapshot (``key -> Record``) with the freshly collected records.

    Returns drafts sorted by key. ``track_fields`` (None = all fields) limits which fields are
    compared; a change to untracked fields only produces no event. Duplicate keys in ``new`` are
    the caller's problem (the runner rejects them); the last one wins here."""
    tracked = None if track_fields is None else list(dict.fromkeys(track_fields))
    new_by_key = {rec.key: rec for rec in new}
    drafts: list[Draft] = []
    for key, rec in new_by_key.items():
        prev = old.get(key)
        if prev is None:
            drafts.append(_added(rec, tracked))
        else:
            modified = _modified(key, prev, rec, tracked)
            if modified is not None:
                drafts.append(modified)
    for key, prev in old.items():
        if key not in new_by_key:
            drafts.append(Draft(KIND_REMOVED, key, [], dict(prev.fields)))
    drafts.sort(key=lambda d: d.key)
    return drafts
