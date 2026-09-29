"""SQLite storage (stdlib ``sqlite3``): sources, records, events, cursors, served log.

One :class:`Store` wraps one connection. The connection is in autocommit mode; multi-statement
work goes through :meth:`Store.transaction` (``BEGIN IMMEDIATE``, re-entrant). All timestamps
are stored as ISO-8601 UTC strings (``timeutil.to_iso``); every method that writes a timestamp
takes it as a parameter, nothing here reads the clock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from since.model import KINDS, Event, FieldChange, Record
from since.paths import db_path
from since.timeutil import to_iso

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
DB_FILENAME = "since.db"

_POSIX = sys.platform != "win32"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        priority TEXT NOT NULL,
        schedule_s INTEGER NOT NULL,
        key_label TEXT NOT NULL DEFAULT '',
        configured INTEGER NOT NULL DEFAULT 1,
        baselined INTEGER NOT NULL DEFAULT 0,
        in_error INTEGER NOT NULL DEFAULT 0,
        error_since TEXT,
        last_error TEXT,
        last_error_at TEXT,
        last_success_at TEXT,
        record_count INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT,
        broken_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        source_id TEXT NOT NULL,
        key TEXT NOT NULL,
        fields_json TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        present INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        record_key TEXT,
        field_changes_json TEXT NOT NULL,
        importance INTEGER NOT NULL,
        detail_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_source_seq ON events (source_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at)",
    """
    CREATE TABLE IF NOT EXISTS cursors (
        agent_id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS served_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        args_json TEXT NOT NULL,
        text TEXT NOT NULL,
        via TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
)

# Columns that schema v2 added to ``sources`` (D18): what an opened v1 database is migrated with.
_V2_SOURCE_COLUMNS = (
    ("fingerprint", "TEXT"),
    ("broken_json", "TEXT NOT NULL DEFAULT '[]'"),
)

_SOURCE_COLUMNS = (
    "source_id, type, priority, schedule_s, key_label, configured, baselined, in_error, "
    "error_since, last_error, last_error_at, last_success_at, record_count, fingerprint, "
    "broken_json"
)
# Columns update_source_state may set (everything except the primary key). ``broken`` is a list
# of str, stored as JSON in the ``broken_json`` column.
_UPDATABLE_COLUMNS = frozenset(
    {
        "type",
        "priority",
        "schedule_s",
        "key_label",
        "configured",
        "baselined",
        "in_error",
        "error_since",
        "last_error",
        "last_error_at",
        "last_success_at",
        "record_count",
        "fingerprint",
        "broken",
    }
)
_BOOL_COLUMNS = frozenset({"configured", "baselined", "in_error"})

_EVENT_COLUMNS = (
    "seq, source_id, kind, record_key, field_changes_json, importance, detail_json, created_at"
)


class StoreError(Exception):
    """The database cannot be used (e.g. written by a newer version of Since)."""


@dataclass(frozen=True)
class SourceState:
    """Persistent per-source state. Times are stored ISO strings (or None). ``fingerprint`` and
    ``broken`` are the page structure last seen by a source that tracks it (D18): the structural
    fingerprint and the sorted extractor selectors that matched nothing."""

    source_id: str
    type: str
    priority: str
    schedule_s: int
    key_label: str
    configured: bool
    baselined: bool
    in_error: bool
    error_since: str | None
    last_error: str | None
    last_error_at: str | None
    last_success_at: str | None
    record_count: int
    fingerprint: str | None = None
    broken: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ServedEntry:
    """One row of the served log: exactly what an agent was handed, and when (``at`` is ISO)."""

    id: int
    agent_id: str
    tool: str
    args: dict[str, Any]
    text: str
    via: str
    at: str


class PruneResult(NamedTuple):
    """Rows deleted by :meth:`Store.prune`."""

    events: int
    served: int
    records: int


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _state_from_row(row: sqlite3.Row) -> SourceState:
    return SourceState(
        source_id=row["source_id"],
        type=row["type"],
        priority=row["priority"],
        schedule_s=row["schedule_s"],
        key_label=row["key_label"],
        configured=bool(row["configured"]),
        baselined=bool(row["baselined"]),
        in_error=bool(row["in_error"]),
        error_since=row["error_since"],
        last_error=row["last_error"],
        last_error_at=row["last_error_at"],
        last_success_at=row["last_success_at"],
        record_count=row["record_count"],
        fingerprint=row["fingerprint"],
        broken=json.loads(row["broken_json"]),
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        seq=row["seq"],
        source_id=row["source_id"],
        kind=row["kind"],
        record_key=row["record_key"],
        field_changes=[FieldChange.from_dict(d) for d in json.loads(row["field_changes_json"])],
        importance=row["importance"],
        detail=json.loads(row["detail_json"]),
        created_at=row["created_at"],
    )


def _prepare_home(home: Path) -> None:
    existed = home.exists()
    home.mkdir(parents=True, exist_ok=True)
    if _POSIX and not existed:
        os.chmod(home, 0o700)


def _prepare_db_file(path: Path) -> None:
    """POSIX: create the db file as 0600 *before* SQLite does, so the -wal/-shm files that
    SQLite derives from its mode are private too. No-op on Windows."""
    if not _POSIX:
        return
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    os.chmod(path, 0o600)


class Store:
    """The Since database. Use :meth:`open`; close with :meth:`close` (or ``with``)."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path
        self._depth = 0

    # -- lifecycle ---------------------------------------------------------------------------

    @classmethod
    def open(cls, home: Path | str | None = None) -> Store:
        """Open (creating if needed) the database in ``home`` (default: ``since_home()``)."""
        if home is None:
            path = db_path()
        else:
            path = Path(home).expanduser() / DB_FILENAME
        _prepare_home(path.parent)
        _prepare_db_file(path)
        conn = sqlite3.connect(
            path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=True
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            store = cls(conn, path)
            store._init_schema()
        except BaseException:
            conn.close()
            raise
        return store

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _schema_is_current(self) -> bool:
        """True if the ``meta`` table exists and holds the current ``schema_version``. Plain reads
        only: an open must not take the write lock when there is nothing to create (a
        long-running writer would otherwise block every read-only command)."""
        try:
            return self.get_meta("schema_version") == str(SCHEMA_VERSION)
        except sqlite3.OperationalError:  # no such table: a new database
            return False

    def _init_schema(self) -> None:
        """Create what is missing and migrate an older database, all in one write transaction.
        Skipped entirely (no write lock) when the database is already at the current version."""
        if self._schema_is_current():
            return
        with self.transaction():
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            current = self.get_meta("schema_version")
            version = 0 if current is None else int(current)
            if version > SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path} has schema version {current}; this Since understands "
                    f"up to {SCHEMA_VERSION}. Upgrade Since."
                )
            if version < SCHEMA_VERSION:
                self._add_missing_source_columns()
                self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _add_missing_source_columns(self) -> None:
        """v1 -> v2: ``ALTER TABLE sources ADD COLUMN`` for each v2 column the table lacks. A
        table just created from the current DDL already has them; checking the actual columns
        (not just the version) also makes a repeated or concurrent migration harmless."""
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(sources)")}
        for name, ddl in _V2_SOURCE_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE sources ADD COLUMN {name} {ddl}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; rolls back if the body raises. Re-entrant: a nested
        use joins the outer transaction (an exception anywhere rolls back the whole thing)."""
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                with suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    # -- meta --------------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str | int | None) -> None:
        """Set a meta value (stored as text); ``None`` deletes the key."""
        if value is None:
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # -- sources -----------------------------------------------------------------------------

    def upsert_source(
        self,
        source_id: str,
        type: str,
        priority: str,
        schedule_s: int,
        key_label: str = "",
        configured: bool = True,
    ) -> None:
        """Insert a source or update its config-derived columns (type, priority, schedule_s,
        key_label, configured). Collection state of an existing row is never touched."""
        self._conn.execute(
            "INSERT INTO sources (source_id, type, priority, schedule_s, key_label, configured) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET type = excluded.type, "
            "priority = excluded.priority, schedule_s = excluded.schedule_s, "
            "key_label = excluded.key_label, configured = excluded.configured",
            (source_id, type, priority, schedule_s, key_label, int(configured)),
        )

    def get_source_state(self, source_id: str) -> SourceState | None:
        row = self._conn.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        return None if row is None else _state_from_row(row)

    def list_source_states(self) -> list[SourceState]:
        """All sources ordered by source_id."""
        rows = self._conn.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources ORDER BY source_id"
        ).fetchall()
        return [_state_from_row(r) for r in rows]

    def update_source_state(self, source_id: str, **cols: Any) -> None:
        """Set columns of an existing source. Bool columns accept bools; ``datetime`` values are
        converted to ISO strings; ``broken`` takes a list of str (stored as JSON). Unknown column
        -> ``ValueError``; unknown source -> ``KeyError``."""
        if not cols:
            return
        unknown = set(cols) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"unknown source state column(s): {', '.join(sorted(unknown))}")
        values: list[Any] = []
        columns: list[str] = []
        for name, value in cols.items():
            column = name
            if name in _BOOL_COLUMNS:
                value = int(bool(value))
            elif name == "broken":
                column = "broken_json"
                value = _dumps(list(value))
            elif isinstance(value, datetime):
                value = to_iso(value)
            columns.append(column)
            values.append(value)
        assignments = ", ".join(f"{column} = ?" for column in columns)  # whitelisted above
        cur = self._conn.execute(
            f"UPDATE sources SET {assignments} WHERE source_id = ?", (*values, source_id)
        )
        if cur.rowcount == 0:
            raise KeyError(source_id)

    def set_configured(self, source_ids: Iterable[str]) -> None:
        """``configured = 1`` for the given ids, 0 for every other source."""
        ids = sorted(set(source_ids))
        with self.transaction():
            self._conn.execute("UPDATE sources SET configured = 0")
            if ids:
                marks = ", ".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE sources SET configured = 1 WHERE source_id IN ({marks})", ids
                )

    # -- records -----------------------------------------------------------------------------

    def get_snapshot(self, source_id: str) -> dict[str, Record]:
        """Latest snapshot: every record of the source with ``present = 1``, by key."""
        rows = self._conn.execute(
            "SELECT key, fields_json, content_hash FROM records "
            "WHERE source_id = ? AND present = 1",
            (source_id,),
        ).fetchall()
        return {
            r["key"]: Record(r["key"], json.loads(r["fields_json"]), r["content_hash"])
            for r in rows
        }

    def get_record(self, source_id: str, key: str) -> tuple[Record, bool, str] | None:
        """``(record, present, updated_at)`` or None. Removed records keep their last fields."""
        row = self._conn.execute(
            "SELECT key, fields_json, content_hash, present, updated_at FROM records "
            "WHERE source_id = ? AND key = ?",
            (source_id, key),
        ).fetchone()
        if row is None:
            return None
        record = Record(row["key"], json.loads(row["fields_json"]), row["content_hash"])
        return record, bool(row["present"]), row["updated_at"]

    def put_records(self, source_id: str, records: Iterable[Record], now: datetime) -> None:
        """Insert or replace records (marking them present). Later duplicates win."""
        stamp = to_iso(now)
        rows = [(source_id, r.key, _dumps(r.fields), r.content_hash, stamp) for r in records]
        with self.transaction():
            self._conn.executemany(
                "INSERT INTO records (source_id, key, fields_json, content_hash, present, "
                "updated_at) VALUES (?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(source_id, key) DO UPDATE SET fields_json = excluded.fields_json, "
                "content_hash = excluded.content_hash, present = 1, "
                "updated_at = excluded.updated_at",
                rows,
            )

    def mark_removed(self, source_id: str, keys: Iterable[str], now: datetime) -> None:
        """Set ``present = 0`` (keeping last fields) on present records; unknown or already
        removed keys are ignored, so the removal time is that of the first call."""
        stamp = to_iso(now)
        rows = [(stamp, source_id, k) for k in keys]
        with self.transaction():
            self._conn.executemany(
                "UPDATE records SET present = 0, updated_at = ? "
                "WHERE source_id = ? AND key = ? AND present = 1",
                rows,
            )

    # -- events ------------------------------------------------------------------------------

    def append_event(
        self,
        source_id: str,
        kind: str,
        *,
        now: datetime,
        record_key: str | None = None,
        field_changes: Sequence[FieldChange] = (),
        importance: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> int:
        """Append an event and return its seq (global, strictly increasing, never reused)."""
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        cur = self._conn.execute(
            "INSERT INTO events (source_id, kind, record_key, field_changes_json, importance, "
            "detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                kind,
                record_key,
                _dumps([fc.to_dict() for fc in field_changes]),
                importance,
                _dumps(detail or {}),
                to_iso(now),
            ),
        )
        seq = cur.lastrowid
        assert seq is not None
        return seq

    def get_event(self, seq: int) -> Event | None:
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE seq = ?", (seq,)
        ).fetchone()
        return None if row is None else _event_from_row(row)

    def events_after(self, cursor: int, source_id: str | None = None) -> list[Event]:
        """Events with ``seq > cursor`` (optionally one source), seq ascending."""
        sql = f"SELECT {_EVENT_COLUMNS} FROM events WHERE seq > ?"
        params: list[Any] = [cursor]
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(source_id)
        rows = self._conn.execute(sql + " ORDER BY seq", params).fetchall()
        return [_event_from_row(r) for r in rows]

    def events_in_range(
        self, lo: int, hi: int, source_id: str | None = None, after: int | None = None
    ) -> list[Event]:
        """Events with ``lo <= seq <= hi`` (and ``seq > after`` if given), seq ascending."""
        sql = f"SELECT {_EVENT_COLUMNS} FROM events WHERE seq >= ? AND seq <= ?"
        params: list[Any] = [lo, hi]
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(source_id)
        if after is not None:
            sql += " AND seq > ?"
            params.append(after)
        rows = self._conn.execute(sql + " ORDER BY seq", params).fetchall()
        return [_event_from_row(r) for r in rows]

    def max_seq(self) -> int:
        """Highest seq ever assigned (0 if none). Read from ``sqlite_sequence`` so it survives
        pruning of every event."""
        row = self._conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
        return 0 if row is None else int(row["seq"])

    # -- cursors -----------------------------------------------------------------------------

    def get_cursor(self, agent_id: str) -> int:
        """Last acked seq of the agent; 0 for an agent never seen."""
        row = self._conn.execute(
            "SELECT seq FROM cursors WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return 0 if row is None else int(row["seq"])

    def set_cursor(self, agent_id: str, seq: int, now: datetime) -> None:
        """Store the cursor as given; monotonicity/bounds are the caller's (service's) job."""
        self._conn.execute(
            "INSERT INTO cursors (agent_id, seq, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET seq = excluded.seq, "
            "updated_at = excluded.updated_at",
            (agent_id, seq, to_iso(now)),
        )

    # -- served log --------------------------------------------------------------------------

    def log_served(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        text: str,
        via: str,
        at: datetime,
    ) -> int:
        """Record a ``since``/``get`` response exactly as served. Returns the row id."""
        cur = self._conn.execute(
            "INSERT INTO served_log (agent_id, tool, args_json, text, via, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, tool, _dumps(args), text, via, to_iso(at)),
        )
        row_id = cur.lastrowid
        assert row_id is not None
        return row_id

    def list_served(self, agent_id: str | None = None, limit: int = 50) -> list[ServedEntry]:
        """Newest first (id descending), optionally for one agent, at most ``limit`` rows."""
        sql = "SELECT id, agent_id, tool, args_json, text, via, at FROM served_log"
        params: list[Any] = []
        if agent_id is not None:
            sql += " WHERE agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            ServedEntry(
                id=r["id"],
                agent_id=r["agent_id"],
                tool=r["tool"],
                args=json.loads(r["args_json"]),
                text=r["text"],
                via=r["via"],
                at=r["at"],
            )
            for r in rows
        ]

    # -- retention ---------------------------------------------------------------------------

    def prune(self, before: datetime, now: datetime) -> PruneResult:
        """Delete events and served-log rows older than ``before``, then removed records
        (``present = 0``) no remaining event refers to. Records still present are never pruned.
        Sets meta ``pruned_through_seq`` (max pruned seq, never lowered) if any event was pruned
        and meta ``last_pruned_at`` to ``now``. Returns the deleted row counts."""
        cutoff = to_iso(before)
        with self.transaction():
            row = self._conn.execute(
                "SELECT MAX(seq) AS s FROM events WHERE created_at < ?", (cutoff,)
            ).fetchone()
            max_pruned = row["s"]
            events = self._conn.execute(
                "DELETE FROM events WHERE created_at < ?", (cutoff,)
            ).rowcount
            served = self._conn.execute("DELETE FROM served_log WHERE at < ?", (cutoff,)).rowcount
            records = self._conn.execute(
                "DELETE FROM records WHERE present = 0 AND (source_id, key) NOT IN "
                "(SELECT source_id, record_key FROM events WHERE record_key IS NOT NULL)"
            ).rowcount
            if max_pruned is not None:
                previous = int(self.get_meta("pruned_through_seq") or 0)
                self.set_meta("pruned_through_seq", max(previous, int(max_pruned)))
            self.set_meta("last_pruned_at", to_iso(now))
        return PruneResult(events, served, records)
