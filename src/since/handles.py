"""Parsing of ``since://`` handles (the strict inverse of the formatters in ``since.render``).

Three kinds exist::

    since://evt/<seq>
    since://rec/<source_id>/<key>                    key = urllib.parse.quote(key, safe="/|")
    since://batch/<from>-<to>[?source=<id>][&after=<seq>]

Handles come from agents, so parsing is strict: exact scheme, canonical positive integers (no
signs, no leading zeros, no non-ASCII digits, at most SQLite's INTEGER range), ``from <= to``,
source ids matching ``SOURCE_ID_RE``, only the ``source`` and ``after`` query parameters and each
at most once. Anything else raises :class:`HandleError`. Surrounding whitespace is not stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from since.model import SOURCE_ID_RE
from since.render import batch_handle, evt_handle, rec_handle

MAX_SEQ = 2**63 - 1  # the largest integer SQLite can bind

_EVT_RE = re.compile(r"since://evt/([0-9]+)")
_BATCH_RE = re.compile(r"since://batch/([0-9]+)-([0-9]+)(?:\?(.*))?")
_REC_PREFIX = "since://rec/"
_POSITIVE_RE = re.compile(r"[1-9][0-9]*")  # ASCII only, no leading zeros
_MAX_DIGITS = len(str(MAX_SEQ))


class HandleError(ValueError):
    """The text is not a well-formed handle."""


@dataclass(frozen=True)
class EvtHandle:
    seq: int


@dataclass(frozen=True)
class RecHandle:
    source_id: str
    key: str


@dataclass(frozen=True)
class BatchHandle:
    lo: int
    hi: int
    source: str | None = None
    after: int | None = None


Handle = EvtHandle | RecHandle | BatchHandle


def _positive(digits: str, what: str) -> int:
    if len(digits) > _MAX_DIGITS or _POSITIVE_RE.fullmatch(digits) is None:
        raise HandleError(f"{what} must be a positive integer")
    value = int(digits)
    if value > MAX_SEQ:
        raise HandleError(f"{what} is out of range")
    return value


def _source_id(text: str) -> str:
    if SOURCE_ID_RE.fullmatch(text) is None:
        raise HandleError("invalid source id")
    return text


def parse(text: str) -> Handle:
    """Parse a handle; raises :class:`HandleError` for anything that is not exactly one."""
    if not isinstance(text, str):
        raise HandleError("handle must be a string")
    if text.startswith(_REC_PREFIX):
        return _parse_rec(text[len(_REC_PREFIX) :])
    m = _EVT_RE.fullmatch(text)
    if m is not None:
        return EvtHandle(_positive(m.group(1), "seq"))
    m = _BATCH_RE.fullmatch(text)
    if m is not None:
        return _parse_batch(m.group(1), m.group(2), m.group(3))
    raise HandleError("not a since:// handle")


def _parse_rec(rest: str) -> RecHandle:
    source_id, slash, encoded_key = rest.partition("/")
    if not slash:
        raise HandleError("record handle has no key")
    _source_id(source_id)
    try:
        key = unquote(encoded_key, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HandleError("record key is not valid percent-encoded UTF-8") from None
    if not key:
        raise HandleError("record key is empty")
    return RecHandle(source_id, key)


def _parse_batch(lo_text: str, hi_text: str, query: str | None) -> BatchHandle:
    lo = _positive(lo_text, "range start")
    hi = _positive(hi_text, "range end")
    if lo > hi:
        raise HandleError("range start is after range end")
    source: str | None = None
    after: int | None = None
    if query is not None:
        seen: set[str] = set()
        for part in query.split("&"):
            name, equals, value = part.partition("=")
            if not equals or not value:
                raise HandleError("malformed query parameter")
            if name not in ("source", "after"):
                raise HandleError(f"unknown query parameter {name!r}")
            if name in seen:
                raise HandleError(f"duplicate query parameter {name!r}")
            seen.add(name)
            if name == "source":
                source = _source_id(value)
            else:
                after = _positive(value, "after")
    return BatchHandle(lo, hi, source, after)


def format_handle(handle: Handle) -> str:
    """The canonical text of a parsed handle (``parse(format_handle(h)) == h``)."""
    if isinstance(handle, EvtHandle):
        return evt_handle(handle.seq)
    if isinstance(handle, RecHandle):
        return rec_handle(handle.source_id, handle.key)
    return batch_handle(handle.lo, handle.hi, source=handle.source, after=handle.after)
