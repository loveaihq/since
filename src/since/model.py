"""Core data types and constant tables shared across Since."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

# Scalar values allowed in record fields.
Scalar = str | int | float | bool | None

# --- identifiers -----------------------------------------------------------------------------

SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

SOURCE_TYPES = ("dir", "sql", "imap", "web", "changedetection")

# --- priorities ------------------------------------------------------------------------------

PRIORITY_HIGH = "high"
PRIORITY_NORMAL = "normal"
PRIORITY_LOW = "low"
PRIORITIES = (PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW)  # display order: high -> low

# --- event kinds -----------------------------------------------------------------------------

KIND_ADDED = "added"
KIND_REMOVED = "removed"
KIND_MODIFIED = "modified"
KIND_BASELINE = "baseline"
KIND_SCHEMA_CHANGED = "schema_changed"
KIND_SOURCE_ERROR = "source_error"
KIND_SOURCE_RECOVERED = "source_recovered"
KINDS = (
    KIND_ADDED,
    KIND_REMOVED,
    KIND_MODIFIED,
    KIND_BASELINE,
    KIND_SCHEMA_CHANGED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
)

# --- importance weights ----------------------------------------------------------------------
# importance = PRIORITY_WEIGHT[priority] * KIND_WEIGHT[kind] + sum(highlight bonuses)

PRIORITY_WEIGHT: dict[str, int] = {PRIORITY_HIGH: 3, PRIORITY_NORMAL: 2, PRIORITY_LOW: 1}

KIND_WEIGHT: dict[str, int] = {
    KIND_SOURCE_ERROR: 5,
    KIND_SCHEMA_CHANGED: 5,
    KIND_MODIFIED: 4,
    KIND_REMOVED: 4,
    KIND_ADDED: 3,
    KIND_BASELINE: 1,
    KIND_SOURCE_RECOVERED: 1,
}

HIGHLIGHT_BONUS_DEFAULT = 10


# --- records ---------------------------------------------------------------------------------


def content_hash(fields: dict[str, Any]) -> str:
    """sha256 hex of the canonical JSON of ``fields`` (independent of dict key order)."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class Record:
    """One item of a source at one point in time. ``key`` is stable within the source."""

    key: str
    fields: dict[str, Scalar]
    content_hash: str

    @classmethod
    def make(cls, key: str, fields: dict[str, Scalar]) -> Record:
        return cls(key=key, fields=fields, content_hash=content_hash(fields))


# --- events ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldChange:
    """A change to one field. For long text (> 200 chars) ``old``/``new`` are None and the
    char counts are set instead, so full text never lands in an event."""

    field: str
    old: Scalar
    new: Scalar
    added_chars: int | None = None
    removed_chars: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"field": self.field, "old": self.old, "new": self.new}
        if self.added_chars is not None:
            d["added_chars"] = self.added_chars
        if self.removed_chars is not None:
            d["removed_chars"] = self.removed_chars
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldChange:
        return cls(
            field=d["field"],
            old=d.get("old"),
            new=d.get("new"),
            added_chars=d.get("added_chars"),
            removed_chars=d.get("removed_chars"),
        )


@dataclass(frozen=True)
class Event:
    """A stored change event. ``created_at`` is the stored ISO-8601 UTC string
    (``2026-09-29T09:12:05Z``); use ``timeutil.from_iso`` / ``fmt_minute`` to display it.
    ``record_key`` is None for source-level kinds. ``detail`` holds source-level facts (D1)."""

    seq: int
    source_id: str
    kind: str
    record_key: str | None
    field_changes: list[FieldChange]
    importance: int
    detail: dict[str, Any]
    created_at: str
