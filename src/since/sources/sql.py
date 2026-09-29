"""``sql`` source: run a read query against a database and turn every row into a record.

Config keys (all required, no others are accepted in M1)::

    url_env: SINCE_PO_DB_URL      # name of the env var holding the SQLAlchemy URL (D9: never YAML)
    query: "select po_no, status, eta from purchase_orders"
    key: [po_no]                  # result columns that identify a row

SQLAlchemy is an optional extra (``since[sql]``) and is imported inside ``collect`` only. The
query runs in a transaction that is always rolled back, never committed. Error messages that reach
the stored ``source_error`` are scrubbed of the URL and its password.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from urllib.parse import quote, quote_plus

from since.config import ConfigError, SourceConfig
from since.model import Record, Scalar
from since.sources import CollectError

_ALLOWED_OPTIONS = ("key", "query", "url_env")
_REDACTED = "***"


class SqlCollector:
    type_name = "sql"

    def validate(self, cfg: SourceConfig) -> None:
        """Check ``url_env`` / ``query`` / ``key`` shape. No I/O: the env var is read and the
        database contacted only in ``collect``."""
        where = f"source '{cfg.id}'"
        options = cfg.options
        for name in sorted(options, key=str):
            if name not in _ALLOWED_OPTIONS:
                raise ConfigError(
                    f"{where}: key '{name}': unknown option for a sql source "
                    f"(allowed: {', '.join(_ALLOWED_OPTIONS)})"
                )
        for name in ("url_env", "query"):
            value = options.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{where}: key '{name}': required, must be a non-empty string")
        key = options.get("key")
        if not isinstance(key, list) or not key or not all(isinstance(c, str) and c for c in key):
            raise ConfigError(
                f"{where}: key 'key': required, must be a non-empty list of result column names"
            )

    def key_label(self, cfg: SourceConfig) -> str:
        """Key columns joined by ``|`` (tolerant of a malformed config: the runner may call this
        for a source whose ``validate`` would fail)."""
        key = cfg.options.get("key")
        if isinstance(key, list):
            return "|".join(str(c) for c in key)
        return ""

    def collect(self, cfg: SourceConfig) -> list[Record]:
        try:
            self.validate(cfg)
        except ConfigError as exc:
            raise CollectError(str(exc)) from None
        env_name: str = cfg.options["url_env"]
        query: str = cfg.options["query"]
        key_columns: list[str] = cfg.options["key"]

        try:
            import sqlalchemy
            from sqlalchemy.engine import make_url
        except ImportError:
            raise CollectError("SQLAlchemy is not installed: install since[sql]") from None

        url = os.environ.get(env_name, "").strip()
        if not url:
            raise CollectError(f"environment variable {env_name} is not set (or is empty)")
        try:
            parsed = make_url(url)
        except Exception:
            # Never echo the exception: parse errors of older SQLAlchemy versions quote the URL.
            raise CollectError(
                f"environment variable {env_name} does not hold a valid SQLAlchemy URL"
            ) from None
        secrets = _secrets(url, parsed)

        engine = None
        try:
            engine = sqlalchemy.create_engine(parsed)
            with engine.connect() as conn:
                result = conn.execute(sqlalchemy.text(query))
                columns = [str(c) for c in result.keys()]
                rows = [dict(row._mapping) for row in result.fetchall()]
                conn.rollback()  # read-only by contract: never commit
        except Exception as exc:
            raise CollectError(_safe_message(exc, secrets)) from None
        finally:
            if engine is not None:
                engine.dispose()
        return _build_records(columns, rows, key_columns)


def _secrets(url: str, parsed: Any) -> list[str]:
    """Strings that must never appear in a stored error: the URL and the password (raw, decoded
    and URL-encoded forms)."""
    found = [url]
    password = getattr(parsed, "password", None)
    if password:
        found += [password, quote(password, safe=""), quote_plus(password)]
    try:
        found.append(parsed.render_as_string(hide_password=False))
    except Exception:  # pragma: no cover - defensive; rendering a parsed URL does not fail
        pass
    return [s for s in dict.fromkeys(found) if s]


def _safe_message(exc: BaseException, secrets: list[str]) -> str:
    """``"<ExcType>: <first line of the message>"`` with the URL and password replaced by ``***``.

    Only the first line is kept: SQLAlchemy appends the failing SQL/params and a "Background on
    this error" link on later lines."""
    message = f"{type(exc).__name__}: {exc}"
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, _REDACTED)
    for line in message.splitlines():
        if line.strip():
            return line.strip()
    return type(exc).__name__


def _to_scalar(value: Any) -> Scalar:
    """Map a database value to a record scalar (see plan T6)."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        data = bytes(value)
        return f"<{len(data)} bytes sha256={hashlib.sha256(data).hexdigest()[:12]}>"
    return str(value)


def _build_records(
    columns: list[str], rows: list[dict[str, Any]], key_columns: list[str]
) -> list[Record]:
    """Rows -> records sorted by key. Raises ``CollectError`` for ambiguous result columns, a key
    column missing from the result, or a null/empty key value."""
    seen: set[str] = set()
    for column in columns:
        if column in seen:
            raise CollectError(f'query result has duplicate column "{column}"; alias it')
        seen.add(column)
    for column in key_columns:
        if column not in seen:
            raise CollectError(f'key column "{column}" is not in the query result')

    records: list[Record] = []
    for row in rows:
        fields = {str(name): _to_scalar(value) for name, value in row.items()}
        parts: list[str] = []
        for column in key_columns:
            value = fields[column]
            if value is None:
                raise CollectError(f'key column "{column}" is null')
            parts.append(str(value))
        key = "|".join(parts)
        if not key:
            raise CollectError(f"key is empty (key columns: {'|'.join(key_columns)})")
        records.append(Record.make(key, fields))
    records.sort(key=lambda r: r.key)
    return records
