"""Source collectors: the ``Collector`` protocol, ``CollectError`` and a lazy type registry.

Collectors never touch the database and never call an LLM; they turn a source config into a list
of :class:`~since.model.Record`. The runner (``since.collect``) does everything else. Concrete
collectors are imported on demand so that e.g. SQLAlchemy is only needed when a ``sql`` source is
actually used.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Protocol

from since.config import SourceConfig
from since.model import Record


class CollectError(Exception):
    """A collection failed for a reason worth showing to the human/agent.

    ``str(exc)`` is stored as the ``source_error`` message, so it must not contain secrets."""


class LoginRequired(CollectError):
    """A collection failed for a reason only a human can fix: the login is gone or refused (web
    ``login expired``, imap ``login failed for <user>``, changedetection ``API key rejected``).

    The runner always surfaces such a failure (D24): while the source is already in error, one
    with a message that differs from the current ``last_error`` still appends a ``source_error``,
    so an agent that saw "layout broken" learns that the cause is now "log in again"."""


@dataclass(frozen=True)
class Window:
    """How far back a source looks (D21): ``field`` names a record field holding an ISO UTC
    timestamp, ``start`` (ISO UTC string, parseable by ``timeutil.from_iso``) is the oldest
    moment still covered. A record that is absent from a result and dated before ``start`` has
    aged out of the window rather than been removed at the source."""

    field: str
    start: str


@dataclass(frozen=True)
class CollectOutput:
    """A collection result with extra facts beside the records.

    ``unavailable`` lists keys that *exist* but could not be read right now (e.g. a file another
    program holds open; D5). The runner keeps their last known record: no event, never
    ``removed``. A key that was never seen before is simply left out until it becomes readable.
    A key must not appear both in ``records`` and in ``unavailable``.

    ``window`` (D21): records of the previous snapshot that are absent from ``records`` and older
    than ``window.start`` leave the snapshot without a ``removed`` event. A baseline ignores it.

    ``fingerprint`` / ``broken`` (D18, web sources): a structural fingerprint of the page and the
    extractor selectors that matched nothing. ``None`` fingerprint = the source does not track
    page structure (``broken`` is then ignored). A non-empty ``broken`` means the extraction is
    not trustworthy: the runner never diffs such a result."""

    records: list[Record]
    unavailable: list[str] = field(default_factory=list)
    window: Window | None = None
    fingerprint: str | None = None
    broken: list[str] = field(default_factory=list)


class Collector(Protocol):
    """One implementation per source type.

    A collector may also define the optional method ``default_title_fields(cfg) -> list[str]``:
    the field names that make up a record's title (D17) when the source has no ``title_fields``
    option, e.g. imap ``["subject", "from"]``. A collector without it has no default title
    (``[]``). It is deliberately not declared here so that it stays optional; always go through
    :func:`title_fields_for`.

    Likewise the optional ``default_track_fields(cfg) -> list[str]`` (D26): the fields whose changes
    make an event when the source has no ``track_fields`` option, e.g. imap
    ``["folder", "flagged", "answered"]`` (a mail merely being read is not news). A collector
    without it tracks every field. Always go through :func:`track_fields_for`."""

    type_name: str

    def validate(self, cfg: SourceConfig) -> None:
        """Check the type-specific options in ``cfg.options``; raise ``ConfigError`` if invalid.
        Must not do I/O (no connections, no filesystem reads)."""
        ...

    def key_label(self, cfg: SourceConfig) -> str:
        """Human label for the record key in digests (``po_no`` -> ``po_no "4500123"``);
        ``""`` when the quoted key alone is clear enough."""
        ...

    def collect(self, cfg: SourceConfig) -> list[Record] | CollectOutput:
        """Read the source now. Return the records, or a :class:`CollectOutput` when there is
        more to report: keys that exist but could not be read (``unavailable``), a collection
        ``window``, a page ``fingerprint`` with the ``broken`` selectors. Raise ``CollectError``
        (or anything else) on failure; a failed collection must never be reported as an empty
        list."""
        ...


# source type -> "module:Class". Modules are imported lazily by ``get_collector``.
REGISTRY: dict[str, str] = {
    "dir": "since.sources.dir:DirCollector",
    "sql": "since.sources.sql:SqlCollector",
    "imap": "since.sources.imap:ImapCollector",
    "web": "since.sources.web:WebCollector",
    "changedetection": "since.sources.changedetection:ChangedetectionCollector",
}


def title_fields_for(cfg: SourceConfig, collector: Collector) -> list[str]:
    """The title fields of a source (D17): ``cfg.title_fields`` if configured, else the
    collector's ``default_title_fields(cfg)`` if it has that method, else ``[]``."""
    if cfg.title_fields is not None:
        return list(cfg.title_fields)
    default = getattr(collector, "default_title_fields", None)
    return list(default(cfg)) if callable(default) else []


def track_fields_for(cfg: SourceConfig, collector: Collector) -> list[str] | None:
    """The tracked fields of a source (D26): ``cfg.track_fields`` if configured, else the
    collector's ``default_track_fields(cfg)`` if it has that method, else ``None`` (all fields)."""
    if cfg.track_fields is not None:
        return list(cfg.track_fields)
    default = getattr(collector, "default_track_fields", None)
    if not callable(default):
        return None
    fields = default(cfg)
    return None if fields is None else list(fields)


def get_collector(type_name: str) -> Collector:
    """Return a new collector for a source type; ``ValueError`` for a type Since does not know.

    The concrete module is imported here, not with this package, so that a source type's optional
    dependencies (Playwright, SQLAlchemy) are only needed when such a source is actually used."""
    target = REGISTRY.get(type_name)
    if target is None:
        raise ValueError(f"unknown source type '{type_name}'")
    module_name, _, class_name = target.partition(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls()
