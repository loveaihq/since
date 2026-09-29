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
from since.model import SOURCE_TYPES, Record


class CollectError(Exception):
    """A collection failed for a reason worth showing to the human/agent.

    ``str(exc)`` is stored as the ``source_error`` message, so it must not contain secrets."""


@dataclass(frozen=True)
class CollectOutput:
    """A collection result that also reports keys it could not read this run (D5).

    ``unavailable`` lists keys that *exist* but could not be read right now (e.g. a file another
    program holds open). The runner keeps their last known record: no event, never ``removed``.
    A key that was never seen before is simply left out until it becomes readable. A key must not
    appear both in ``records`` and in ``unavailable``."""

    records: list[Record]
    unavailable: list[str] = field(default_factory=list)


class Collector(Protocol):
    """One implementation per source type.

    A collector may also define the optional method ``default_title_fields(cfg) -> list[str]``:
    the field names that make up a record's title (D17) when the source has no ``title_fields``
    option, e.g. imap ``["subject", "from"]``. A collector without it has no default title
    (``[]``). It is deliberately not declared here so that it stays optional; always go through
    :func:`title_fields_for`."""

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
        """Read the source now. Return the records, or a :class:`CollectOutput` when some keys
        exist but could not be read (``unavailable``). Raise ``CollectError`` (or anything else)
        on failure; a failed collection must never be reported as an empty list."""
        ...


# source type -> "module:Class". Modules are imported lazily by ``get_collector``.
REGISTRY: dict[str, str] = {
    "dir": "since.sources.dir:DirCollector",
    "sql": "since.sources.sql:SqlCollector",
}


def title_fields_for(cfg: SourceConfig, collector: Collector) -> list[str]:
    """The title fields of a source (D17): ``cfg.title_fields`` if configured, else the
    collector's ``default_title_fields(cfg)`` if it has that method, else ``[]``."""
    if cfg.title_fields is not None:
        return list(cfg.title_fields)
    default = getattr(collector, "default_title_fields", None)
    return list(default(cfg)) if callable(default) else []


def get_collector(type_name: str) -> Collector:
    """Return a new collector for a source type.

    ``NotImplementedError`` for types that exist but have no collector yet (imap / web /
    changedetection); ``ValueError`` for a type Since does not know at all."""
    target = REGISTRY.get(type_name)
    if target is None:
        if type_name in SOURCE_TYPES:
            raise NotImplementedError(f"source type '{type_name}' is not implemented yet")
        raise ValueError(f"unknown source type '{type_name}'")
    module_name, _, class_name = target.partition(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls()
