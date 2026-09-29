"""Quoting of untrusted values.

Everything a source hands us (field values, record keys, error messages) is data, never
instructions. Before it reaches an agent it is quoted, forced onto one line, stripped of
control/format characters and capped. Source ids, agent ids, key labels and field names come
from validated config and are not quoted.
"""

from __future__ import annotations

import unicodedata
from typing import Any

DIGEST_CAP = 120  # digests and batch listings
GET_CAP = 1000  # `get` evt/rec views

# Unicode categories replaced by a space: control, format (bidi overrides, zero-width chars),
# line/paragraph separators, private use, surrogates.
_SCRUB_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs"})

_ELLIPSIS = "…"


def _scrub(s: str) -> str:
    # str.isprintable() is False for every category we scrub (and a few we keep), so a printable
    # string can skip the per-character pass.
    if s.isprintable():
        return s
    return "".join(" " if unicodedata.category(c) in _SCRUB_CATEGORIES else c for c in s)


def q(value: Any, cap: int) -> str:
    """Render one untrusted value as a quoted, single-line, capped string.

    ``None`` -> ``null`` (unquoted). Otherwise ``str(value)``; scrubbed characters become
    spaces, whitespace runs collapse to one space, the result is stripped, cut to ``cap``
    characters (``cap - 1`` plus ``…``), and only then are ``\\`` and ``"`` escaped and the
    text wrapped in double quotes. ``cap`` counts characters before escaping.
    """
    if cap < 1:
        raise ValueError(f"cap must be >= 1, got {cap}")
    if value is None:
        return "null"
    s = " ".join(_scrub(str(value)).split())
    if len(s) > cap:
        s = s[: cap - 1] + _ELLIPSIS
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
