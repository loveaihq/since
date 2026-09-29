"""Time helpers. All times are UTC; nothing here reads the clock.

Stored form: ``2026-09-29T09:12:05Z``. Shown to agents: ``2026-09-29T09:12Z``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

MIN_SCHEDULE_S = 10

_SCHEDULE_RE = re.compile(r"^every\s+(\d+)\s*([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime not allowed; pass an aware datetime")
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """Aware datetime -> ``2026-09-29T09:12:05Z`` (UTC, whole seconds)."""
    return _to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s: str) -> datetime:
    """ISO-8601 string with a zone (``Z`` or offset) -> aware UTC datetime."""
    dt = datetime.fromisoformat(s.strip())
    if dt.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {s!r}")
    return dt.astimezone(UTC)


def fmt_minute(dt: datetime) -> str:
    """Aware datetime -> ``2026-09-29T09:12Z`` (the form shown to agents)."""
    return _to_utc(dt).strftime("%Y-%m-%dT%H:%MZ")


def fmt_age(seconds: float) -> str:
    """Compact age: < 60s ``Ns``, < 60m ``Nm``, < 48h ``Nh``, else ``Nd`` (floored)."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def parse_schedule(text: str) -> int:
    """``"every 15m"`` -> 900. Units s/m/h/d; minimum 10 seconds. Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError(f"schedule must be a string like 'every 15m', got {text!r}")
    m = _SCHEDULE_RE.match(text.strip().lower())
    if m is None:
        raise ValueError(f"invalid schedule {text!r}; expected e.g. 'every 15m' (units s, m, h, d)")
    seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    if seconds < MIN_SCHEDULE_S:
        raise ValueError(f"schedule {text!r} is below the minimum of {MIN_SCHEDULE_S}s")
    return seconds
