"""Human-edited YAML config (``~/.since/since.yaml``): loading and validation.

Credentials never live in YAML: sources reference an env var (``url_env`` / ``password_env`` /
``api_key_env``) instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from since.model import (
    HIGHLIGHT_BONUS_DEFAULT,
    PRIORITIES,
    PRIORITY_NORMAL,
    SOURCE_ID_RE,
    SOURCE_TYPES,
)
from since.paths import config_path
from since.timeutil import parse_schedule

DEFAULT_SCHEDULE = "every 15m"
DEFAULT_SCHEDULE_S = 900
DEFAULT_RETENTION_DAYS = 30

HIGHLIGHT_OPS = ("equals", "contains", "changed_to")

_CREDENTIAL_KEYS = frozenset({"password", "passwd", "secret", "token", "api_key"})
_CREDENTIAL_MSG = (
    "credentials must come from an env var (url_env / password_env / api_key_env) "
    "or OS keyring, never YAML"
)

# A source option whose key ends in ``_env`` names an environment variable (D31). What a human
# pasted there instead (the secret itself) must never be echoed into an error, the DB or a digest.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_NAME_MSG = (
    "must be the NAME of an environment variable (e.g. SINCE_IMAP_PASSWORD), not the secret itself"
)

# Source keys consumed by the core; everything else lands in ``SourceConfig.options``.
_CORE_KEYS = frozenset(
    {"id", "type", "priority", "schedule", "track_fields", "title_fields", "highlight"}
)

MAX_TITLE_FIELDS = 3


class ConfigError(Exception):
    """Invalid or missing configuration. The message says what to fix."""


@dataclass(frozen=True)
class HighlightRule:
    """Adds ``bonus`` to an event's importance when it matches (see plan "Weights").

    ``op`` is one of ``equals`` / ``contains`` / ``changed_to``; ``value`` is always a string."""

    field: str
    op: str
    value: str
    bonus: int = HIGHLIGHT_BONUS_DEFAULT


@dataclass(frozen=True)
class SourceConfig:
    id: str
    type: str
    priority: str = PRIORITY_NORMAL
    schedule_s: int = DEFAULT_SCHEDULE_S
    track_fields: list[str] | None = None
    title_fields: list[str] | None = None  # record title (D17); None = the collector's default
    highlight: list[HighlightRule] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)  # remaining type-specific keys


@dataclass(frozen=True)
class Config:
    sources: list[SourceConfig] = field(default_factory=list)
    retention_days: int = DEFAULT_RETENTION_DAYS


def _err(where: str, key: str, message: str) -> ConfigError:
    return ConfigError(f"{where}: key '{key}': {message}")


def _parse_highlight(where: str, raw: Any) -> list[HighlightRule]:
    if not isinstance(raw, list):
        raise _err(where, "highlight", "must be a list of rules")
    rules: list[HighlightRule] = []
    for i, item in enumerate(raw):
        here = f"highlight[{i}]"
        if not isinstance(item, Mapping):
            raise _err(where, here, "each rule must be a mapping")
        unknown = set(item) - {"field", "bonus", *HIGHLIGHT_OPS}
        if unknown:
            raise _err(where, here, f"unknown key(s): {', '.join(sorted(map(str, unknown)))}")
        fld = item.get("field")
        if not isinstance(fld, str) or not fld:
            raise _err(where, f"{here}.field", "required, must be a non-empty string")
        ops = [op for op in HIGHLIGHT_OPS if op in item]
        if len(ops) != 1:
            raise _err(
                where,
                here,
                "must have exactly one of equals / contains / changed_to"
                + (f" (got {', '.join(ops)})" if ops else ""),
            )
        op = ops[0]
        value = item[op]
        if value is None or not isinstance(value, str | int | float | bool):
            raise _err(where, f"{here}.{op}", "must be a scalar value")
        bonus = item.get("bonus", HIGHLIGHT_BONUS_DEFAULT)
        if isinstance(bonus, bool) or not isinstance(bonus, int):
            raise _err(where, f"{here}.bonus", "must be an integer")
        rules.append(HighlightRule(field=fld, op=op, value=str(value), bonus=bonus))
    return rules


def _parse_source(index: int, raw: Any, seen_ids: set[str]) -> SourceConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"sources[{index}]: each source must be a mapping")
    sid = raw.get("id")
    if not isinstance(sid, str) or not sid:
        raise ConfigError(f"sources[{index}]: key 'id': required, must be a string")
    where = f"source '{sid}'"
    if not SOURCE_ID_RE.match(sid):
        raise _err(where, "id", "must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    if sid in seen_ids:
        raise _err(where, "id", "duplicate source id")
    seen_ids.add(sid)

    stype = raw.get("type")
    for key in raw:
        if not isinstance(key, str):
            raise _err(where, str(key), "keys must be strings")
        if key.lower() in _CREDENTIAL_KEYS or (key == "url" and stype == "sql"):
            raise _err(where, key, _CREDENTIAL_MSG)

    if stype not in SOURCE_TYPES:
        raise _err(where, "type", f"must be one of {', '.join(SOURCE_TYPES)} (got {stype!r})")

    for key, value in raw.items():
        # fullmatch, not ``$`` (which lets a trailing newline through); the message never
        # contains ``value``.
        if key.lower().endswith("_env") and not (
            isinstance(value, str) and _ENV_NAME_RE.fullmatch(value)
        ):
            raise _err(where, key, _ENV_NAME_MSG)

    priority = raw.get("priority", PRIORITY_NORMAL)
    if priority not in PRIORITIES:
        raise _err(where, "priority", f"must be one of {', '.join(PRIORITIES)} (got {priority!r})")

    try:
        schedule_s = parse_schedule(raw.get("schedule", DEFAULT_SCHEDULE))
    except ValueError as e:
        raise _err(where, "schedule", str(e)) from None

    track_fields: list[str] | None = None
    if raw.get("track_fields") is not None:
        tf = raw["track_fields"]
        if not isinstance(tf, list) or not tf or not all(isinstance(x, str) and x for x in tf):
            raise _err(where, "track_fields", "must be a non-empty list of field names")
        track_fields = list(tf)

    title_fields: list[str] | None = None
    if raw.get("title_fields") is not None:
        tt = raw["title_fields"]
        if (
            not isinstance(tt, list)
            or not 1 <= len(tt) <= MAX_TITLE_FIELDS
            or not all(isinstance(x, str) and x for x in tt)
        ):
            raise _err(
                where,
                "title_fields",
                f"must be a list of 1-{MAX_TITLE_FIELDS} field names (non-empty strings)",
            )
        title_fields = list(tt)

    highlight: list[HighlightRule] = []
    if raw.get("highlight") is not None:
        highlight = _parse_highlight(where, raw["highlight"])

    options = {k: v for k, v in raw.items() if k not in _CORE_KEYS}
    return SourceConfig(
        id=sid,
        type=stype,
        priority=priority,
        schedule_s=schedule_s,
        track_fields=track_fields,
        title_fields=title_fields,
        highlight=highlight,
        options=options,
    )


def parse_config(data: Any) -> Config:
    """Validate an already-parsed YAML document (``None`` = empty file = no sources)."""
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError("config must be a mapping with a 'sources' list")

    raw_sources = data.get("sources")
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        raise ConfigError("key 'sources': must be a list")
    seen_ids: set[str] = set()
    sources = [_parse_source(i, raw, seen_ids) for i, raw in enumerate(raw_sources)]

    retention = data.get("retention_days", DEFAULT_RETENTION_DAYS)
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
        raise ConfigError("key 'retention_days': must be an integer >= 1")
    return Config(sources=sources, retention_days=retention)


def load_config(path: Path | str | None = None) -> Config:
    """Load and validate the YAML config (default ``~/.since/since.yaml``)."""
    p = Path(path).expanduser() if path is not None else config_path()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read config file {p}: {e}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from None
    try:
        return parse_config(data)
    except ConfigError as e:
        raise ConfigError(f"{p}: {e}") from None
