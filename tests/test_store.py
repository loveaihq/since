"""Storage tests: schema, round-trips for every API, seq/prune semantics, transactions."""

from __future__ import annotations

import sqlite3
import stat
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import since.store as store_mod
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SOURCE_ERROR,
    Event,
    FieldChange,
    Record,
)
from since.store import SourceState, Store, StoreError

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def store(since_home_dir: Path) -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


def add_source(store: Store, source_id: str = "docs", **kw: object) -> None:
    args: dict[str, object] = {
        "type": "dir",
        "priority": "normal",
        "schedule_s": 900,
        "key_label": "",
        "configured": True,
    }
    args.update(kw)
    store.upsert_source(source_id, **args)  # type: ignore[arg-type]


def raw(store: Store, sql: str, *params: object) -> list[sqlite3.Row]:
    return store._conn.execute(sql, params).fetchall()


# --- open / schema ---------------------------------------------------------------------------


def test_open_creates_home_and_wal_db(since_home_dir: Path) -> None:
    assert not since_home_dir.exists()
    s = Store.open()
    try:
        assert (since_home_dir / "since.db").is_file()
        assert s.path == since_home_dir / "since.db"
        assert s.get_meta("schema_version") == "2"
    finally:
        s.close()
    # journal_mode is persistent in the db file: a fresh connection sees wal.
    conn = sqlite3.connect(since_home_dir / "since.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_open_with_explicit_home(tmp_path: Path, since_home_dir: Path) -> None:
    home = tmp_path / "elsewhere"
    with Store.open(home) as s:
        s.set_meta("k", "v")
    assert (home / "since.db").is_file()
    assert not since_home_dir.exists()


def test_reopen_keeps_data(since_home_dir: Path) -> None:
    with Store.open() as s:
        add_source(s)
        s.put_records("docs", [Record.make("a", {"x": 1})], at())
        s.append_event("docs", KIND_BASELINE, now=at(), detail={"record_count": 1})
        s.set_cursor("agent", 1, at())
    with Store.open() as s:
        assert s.get_source_state("docs") is not None
        assert list(s.get_snapshot("docs")) == ["a"]
        assert s.max_seq() == 1
        assert s.get_cursor("agent") == 1
        assert s.get_meta("schema_version") == "2"


def test_newer_schema_version_is_refused(since_home_dir: Path) -> None:
    with Store.open() as s:
        s.set_meta("schema_version", "99")
    with pytest.raises(StoreError, match="schema version 99"):
        Store.open()


def test_open_of_an_existing_database_does_not_take_the_write_lock(
    since_home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store.open() as s:  # creates the schema
        s.set_cursor("agent", 7, at())
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 200)
    holder = sqlite3.connect(since_home_dir / "since.db", isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")  # e.g. the daemon in the middle of a collection

        started = time.monotonic()
        with Store.open() as reader:  # a read-only command (digest, status, mcp) must not wait
            assert reader.get_cursor("agent") == 7
            assert reader.get_meta("schema_version") == "2"
        assert time.monotonic() - started < 1.0

        # control: with the schema check forced onto the write path the open does block
        monkeypatch.setattr(Store, "_schema_is_current", lambda self: False)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            Store.open()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_open_of_an_empty_database_file_creates_the_schema(since_home_dir: Path) -> None:
    since_home_dir.mkdir()
    sqlite3.connect(since_home_dir / "since.db").close()  # a file without any table

    with Store.open() as s:
        assert s.get_meta("schema_version") == "2"
        assert s.get_cursor("agent") == 0
        assert s.events_after(0) == []


def test_open_completes_a_database_that_has_meta_but_no_schema_version(
    since_home_dir: Path,
) -> None:
    since_home_dir.mkdir()
    con = sqlite3.connect(since_home_dir / "since.db")
    try:
        with con:
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO meta VALUES ('daemon_pid', '5')")
    finally:
        con.close()

    with Store.open() as s:
        assert s.get_meta("schema_version") == "2"
        assert s.get_meta("daemon_pid") == "5"  # existing meta is kept
        assert s.list_source_states() == []  # the rest of the schema now exists


# --- schema v1 -> v2 -------------------------------------------------------------------------

# The v1 tables, as the v1 code created them: what a database written before page-structure
# tracking (D18) contains.
V1_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    """
    CREATE TABLE sources (
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
        record_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE records (
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
    CREATE TABLE events (
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
    "CREATE INDEX idx_events_source_seq ON events (source_id, seq)",
    "CREATE INDEX idx_events_created_at ON events (created_at)",
    "CREATE TABLE cursors (agent_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, "
    "updated_at TEXT NOT NULL)",
    """
    CREATE TABLE served_log (
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


def make_v1_database(home: Path) -> Path:
    """A real v1 database file in ``home`` with one source, record, event and cursor."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "since.db"
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        for stmt in V1_SCHEMA:
            con.execute(stmt)
        con.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        con.execute("INSERT INTO meta VALUES ('daemon_pid', '5')")
        con.execute(
            "INSERT INTO sources (source_id, type, priority, schedule_s, key_label, baselined, "
            "in_error, error_since, last_error, last_error_at, last_success_at, record_count) "
            "VALUES ('portal', 'dir', 'high', 900, 'po', 1, 1, '2026-09-29T09:00:00Z', 'boom', "
            "'2026-09-29T09:05:00Z', '2026-09-29T08:00:00Z', 1)"
        )
        con.execute(
            "INSERT INTO records VALUES ('portal', '4500123', '{\"status\": \"Open\"}', 'h1', 1, "
            "'2026-09-29T08:00:00Z')"
        )
        con.execute(
            "INSERT INTO events (source_id, kind, record_key, field_changes_json, importance, "
            "detail_json, created_at) VALUES ('portal', 'baseline', NULL, '[]', 10, "
            "'{\"record_count\": 1}', '2026-09-29T08:00:00Z')"
        )
        con.execute("INSERT INTO cursors VALUES ('agent', 1, '2026-09-29T08:30:00Z')")
    finally:
        con.close()
    return path


def source_columns(store: Store) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in raw(store, "PRAGMA table_info(sources)")}


def test_a_fresh_database_has_the_v2_source_columns(store: Store) -> None:
    columns = source_columns(store)
    assert columns["fingerprint"]["type"] == "TEXT" and columns["fingerprint"]["notnull"] == 0
    assert columns["broken_json"]["notnull"] == 1
    assert columns["broken_json"]["dflt_value"] == "'[]'"


def test_open_migrates_a_v1_database_and_keeps_its_data(since_home_dir: Path) -> None:
    make_v1_database(since_home_dir)

    with Store.open() as s:
        assert s.get_meta("schema_version") == "2"
        assert s.get_meta("daemon_pid") == "5"
        assert {"fingerprint", "broken_json"} <= set(source_columns(s))
        assert s.get_source_state("portal") == SourceState(
            source_id="portal",
            type="dir",
            priority="high",
            schedule_s=900,
            key_label="po",
            configured=True,
            baselined=True,
            in_error=True,
            error_since="2026-09-29T09:00:00Z",
            last_error="boom",
            last_error_at="2026-09-29T09:05:00Z",
            last_success_at="2026-09-29T08:00:00Z",
            record_count=1,
            fingerprint=None,
            broken=[],
        )
        assert raw(s, "SELECT broken_json FROM sources")[0][0] == "[]"  # the column default
        assert list(s.get_snapshot("portal")) == ["4500123"]
        assert [e.kind for e in s.events_after(0)] == ["baseline"]
        assert s.get_cursor("agent") == 1
        # the new columns are usable right away
        s.update_source_state("portal", fingerprint="f1", broken=["td.x"])
        s.upsert_source("newer", "web", "normal", 60)
        assert s.get_source_state("newer").broken == []  # type: ignore[union-attr]

    with Store.open() as s:  # reopening a migrated database changes nothing
        assert s.get_meta("schema_version") == "2"
        state = s.get_source_state("portal")
        assert state is not None and (state.fingerprint, state.broken) == ("f1", ["td.x"])


def test_migrating_a_v1_database_needs_the_write_lock(
    since_home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_v1_database(since_home_dir)
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 200)
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            Store.open()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    with Store.open() as s:  # the failed attempt left nothing half-migrated
        assert s.get_meta("schema_version") == "2"
        assert "broken_json" in source_columns(s)


def test_migration_of_a_database_that_already_has_the_columns_is_harmless(
    since_home_dir: Path,
) -> None:
    with Store.open() as s:  # a v2 database whose version marker says v1
        add_source(s, "docs")
        s.update_source_state("docs", fingerprint="keep", broken=["a"])
        s.set_meta("schema_version", "1")

    with Store.open() as s:
        assert s.get_meta("schema_version") == "2"
        state = s.get_source_state("docs")
        assert state is not None and (state.fingerprint, state.broken) == ("keep", ["a"])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_posix_permissions(since_home_dir: Path) -> None:
    with Store.open() as s:
        s.append_event("docs", KIND_BASELINE, now=at())
        assert stat.S_IMODE(since_home_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((since_home_dir / "since.db").stat().st_mode) == 0o600
        for suffix in ("-wal", "-shm"):
            side = since_home_dir / f"since.db{suffix}"
            if side.exists():
                assert stat.S_IMODE(side.stat().st_mode) == 0o600


# --- meta ------------------------------------------------------------------------------------


def test_meta_roundtrip_overwrite_and_delete(store: Store) -> None:
    assert store.get_meta("daemon_pid") is None
    store.set_meta("daemon_pid", "123")
    assert store.get_meta("daemon_pid") == "123"
    store.set_meta("daemon_pid", "456")
    assert store.get_meta("daemon_pid") == "456"
    store.set_meta("daemon_min_schedule_s", 900)  # ints are stored as text
    assert store.get_meta("daemon_min_schedule_s") == "900"
    store.set_meta("daemon_pid", None)
    assert store.get_meta("daemon_pid") is None
    store.set_meta("never_set", None)  # deleting a missing key is fine


# --- sources ---------------------------------------------------------------------------------


def test_upsert_source_inserts_with_fresh_state(store: Store) -> None:
    add_source(store, "po-table", type="sql", priority="high", schedule_s=900, key_label="po_no")
    assert store.get_source_state("po-table") == SourceState(
        source_id="po-table",
        type="sql",
        priority="high",
        schedule_s=900,
        key_label="po_no",
        configured=True,
        baselined=False,
        in_error=False,
        error_since=None,
        last_error=None,
        last_error_at=None,
        last_success_at=None,
        record_count=0,
        fingerprint=None,
        broken=[],
    )


def test_get_source_state_unknown_is_none(store: Store) -> None:
    assert store.get_source_state("nope") is None


def test_upsert_source_updates_config_but_not_collection_state(store: Store) -> None:
    add_source(store, "docs")
    store.update_source_state(
        "docs",
        baselined=True,
        in_error=True,
        error_since="2026-09-29T09:00:00Z",
        last_error="boom",
        last_error_at="2026-09-29T09:05:00Z",
        last_success_at="2026-09-29T08:00:00Z",
        record_count=12,
        fingerprint="f1",
        broken=["td.status"],
    )
    add_source(
        store, "docs", type="sql", priority="high", schedule_s=60, key_label="k", configured=False
    )
    state = store.get_source_state("docs")
    assert state == SourceState(
        source_id="docs",
        type="sql",
        priority="high",
        schedule_s=60,
        key_label="k",
        configured=False,
        baselined=True,
        in_error=True,
        error_since="2026-09-29T09:00:00Z",
        last_error="boom",
        last_error_at="2026-09-29T09:05:00Z",
        last_success_at="2026-09-29T08:00:00Z",
        record_count=12,
        fingerprint="f1",
        broken=["td.status"],
    )


def test_list_source_states_ordered_by_id(store: Store) -> None:
    for sid in ("zeta", "alpha", "mid"):
        add_source(store, sid)
    assert [s.source_id for s in store.list_source_states()] == ["alpha", "mid", "zeta"]


def test_update_source_state_converts_bools_and_datetimes(store: Store) -> None:
    add_source(store)
    store.update_source_state("docs", baselined=True, last_success_at=at(5), record_count=3)
    state = store.get_source_state("docs")
    assert state is not None
    assert state.baselined is True
    assert state.last_success_at == "2026-09-29T09:05:00Z"
    assert state.record_count == 3
    store.update_source_state("docs", in_error=False, error_since=None)
    assert store.get_source_state("docs").in_error is False  # type: ignore[union-attr]


def test_update_source_state_stores_fingerprint_and_broken_as_json(store: Store) -> None:
    add_source(store)
    store.update_source_state("docs", fingerprint="abc123", broken=["table#o tr", 'td[x="é"]'])
    state = store.get_source_state("docs")
    assert state is not None
    assert (state.fingerprint, state.broken) == ("abc123", ["table#o tr", 'td[x="é"]'])
    assert raw(store, "SELECT broken_json FROM sources")[0][0] == '["table#o tr", "td[x=\\"é\\"]"]'
    assert store.list_source_states()[0].broken == ["table#o tr", 'td[x="é"]']

    store.update_source_state("docs", fingerprint=None, broken=[])  # cleared again
    state = store.get_source_state("docs")
    assert state is not None and (state.fingerprint, state.broken) == (None, [])
    assert raw(store, "SELECT broken_json FROM sources")[0][0] == "[]"

    store.update_source_state("docs", broken=("a", "b"))  # any sequence of str
    assert store.get_source_state("docs").broken == ["a", "b"]  # type: ignore[union-attr]


def test_update_source_state_rejects_bad_input(store: Store) -> None:
    add_source(store)
    with pytest.raises(ValueError, match="unknown source state column"):
        store.update_source_state("docs", bogus="x")
    with pytest.raises(ValueError, match="unknown source state column"):
        store.update_source_state("docs", **{"record_count = 0; --": 1})
    with pytest.raises(ValueError, match="unknown source state column"):
        store.update_source_state("docs", broken_json="[]")  # the API name is ``broken``
    with pytest.raises(KeyError):
        store.update_source_state("missing", record_count=1)
    store.update_source_state("docs")  # no columns: no-op


def test_set_configured(store: Store) -> None:
    for sid in ("a", "b", "c"):
        add_source(store, sid)
    store.set_configured(["a", "c", "not-a-row"])
    flags = {s.source_id: s.configured for s in store.list_source_states()}
    assert flags == {"a": True, "b": False, "c": True}
    store.set_configured([])
    assert not any(s.configured for s in store.list_source_states())
    assert store.get_source_state("not-a-row") is None


# --- records ---------------------------------------------------------------------------------


def test_snapshot_and_record_roundtrip(store: Store) -> None:
    fields = {"status": "Open", "qty": 3, "price": 1.5, "ok": True, "note": None, "名前": "café ✓"}
    rec = Record.make("po/1 x", fields)
    store.put_records("po", [rec, Record.make("po/2", {"status": "Closed"})], at(1))
    snap = store.get_snapshot("po")
    assert set(snap) == {"po/1 x", "po/2"}
    assert snap["po/1 x"] == rec
    assert snap["po/1 x"].fields == fields
    got = store.get_record("po", "po/1 x")
    assert got == (rec, True, "2026-09-29T09:01:00Z")
    assert store.get_record("po", "missing") is None
    assert store.get_record("other", "po/1 x") is None
    assert store.get_snapshot("other") == {}


def test_put_records_replaces_and_later_duplicate_wins(store: Store) -> None:
    store.put_records("s", [Record.make("k", {"v": 1})], at(0))
    new = Record.make("k", {"v": 2})
    store.put_records("s", [Record.make("k", {"v": 9}), new], at(5))
    assert store.get_record("s", "k") == (new, True, "2026-09-29T09:05:00Z")
    assert len(store.get_snapshot("s")) == 1


def test_records_are_scoped_per_source(store: Store) -> None:
    store.put_records("a", [Record.make("k", {"v": "A"})], at())
    store.put_records("b", [Record.make("k", {"v": "B"})], at())
    assert store.get_snapshot("a")["k"].fields == {"v": "A"}
    assert store.get_snapshot("b")["k"].fields == {"v": "B"}


def test_mark_removed_hides_from_snapshot_but_keeps_record(store: Store) -> None:
    keep = Record.make("keep", {"v": 1})
    gone = Record.make("gone", {"v": 2})
    store.put_records("s", [keep, gone], at(0))
    store.mark_removed("s", ["gone", "never-existed"], at(10))
    assert list(store.get_snapshot("s")) == ["keep"]
    assert store.get_record("s", "gone") == (gone, False, "2026-09-29T09:10:00Z")
    assert store.get_record("s", "never-existed") is None
    # a second call keeps the first removal time
    store.mark_removed("s", ["gone"], at(20))
    assert store.get_record("s", "gone") == (gone, False, "2026-09-29T09:10:00Z")
    # the record reappears
    back = Record.make("gone", {"v": 3})
    store.put_records("s", [back], at(30))
    assert store.get_record("s", "gone") == (back, True, "2026-09-29T09:30:00Z")
    assert set(store.get_snapshot("s")) == {"keep", "gone"}


# --- events ----------------------------------------------------------------------------------


def test_event_roundtrip_all_fields(store: Store) -> None:
    changes = [
        FieldChange("status", "Open", "Cancelled"),
        FieldChange("body", None, None, added_chars=12, removed_chars=3),
        FieldChange("名前", "旧", "新"),
    ]
    seq = store.append_event(
        "po",
        KIND_MODIFIED,
        now=at(3),
        record_key="4500123",
        field_changes=changes,
        importance=22,
        detail={},
    )
    assert seq == 1
    assert store.get_event(seq) == Event(
        seq=1,
        source_id="po",
        kind=KIND_MODIFIED,
        record_key="4500123",
        field_changes=changes,
        importance=22,
        detail={},
        created_at="2026-09-29T09:03:00Z",
    )
    assert store.get_event(2) is None


def test_source_level_event_defaults_and_detail(store: Store) -> None:
    seq = store.append_event(
        "po", KIND_SOURCE_ERROR, now=at(), importance=15, detail={"error": "refused ✗"}
    )
    ev = store.get_event(seq)
    assert ev is not None
    assert ev.record_key is None
    assert ev.field_changes == []
    assert ev.detail == {"error": "refused ✗"}
    assert ev.importance == 15


def test_events_are_serialised_canonically(store: Store) -> None:
    store.append_event(
        "s",
        KIND_ADDED,
        now=at(),
        record_key="ключ",
        field_changes=[FieldChange("名前", None, "é")],
        detail={"z": "ü", "a": 1},
    )
    store.put_records("s", [Record.make("k", {"b": "ö", "a": 1})], at())
    store.log_served("agent", "since", {"source": "s", "budget_tokens": 5}, "t", "cli", at())
    ev = raw(store, "SELECT field_changes_json, detail_json FROM events")[0]
    assert ev["detail_json"] == '{"a": 1, "z": "ü"}'  # sorted keys, not ASCII-escaped
    assert ev["field_changes_json"] == ('[{"field": "名前", "new": "é", "old": null}]')
    rec = raw(store, "SELECT fields_json FROM records")[0]
    assert rec["fields_json"] == '{"a": 1, "b": "ö"}'
    served = raw(store, "SELECT args_json FROM served_log")[0]
    assert served["args_json"] == '{"budget_tokens": 5, "source": "s"}'


def test_append_event_rejects_unknown_kind(store: Store) -> None:
    with pytest.raises(ValueError, match="unknown event kind"):
        store.append_event("s", "exploded", now=at())
    assert store.max_seq() == 0


def _seed_events(store: Store) -> list[int]:
    seqs = []
    for i, (sid, kind) in enumerate(
        [
            ("a", KIND_BASELINE),
            ("b", KIND_BASELINE),
            ("a", KIND_ADDED),
            ("b", KIND_ADDED),
            ("a", KIND_REMOVED),
        ]
    ):
        seqs.append(store.append_event(sid, kind, now=at(i), record_key=f"k{i}"))
    return seqs


def test_seqs_strictly_increase_from_one(store: Store) -> None:
    assert _seed_events(store) == [1, 2, 3, 4, 5]
    assert store.max_seq() == 5


def test_events_after(store: Store) -> None:
    _seed_events(store)
    assert [e.seq for e in store.events_after(0)] == [1, 2, 3, 4, 5]
    assert [e.seq for e in store.events_after(3)] == [4, 5]
    assert store.events_after(5) == []
    assert store.events_after(99) == []
    assert [e.seq for e in store.events_after(0, source_id="a")] == [1, 3, 5]
    assert [e.seq for e in store.events_after(1, source_id="b")] == [2, 4]
    assert store.events_after(0, source_id="zzz") == []


def test_events_in_range(store: Store) -> None:
    _seed_events(store)
    assert [e.seq for e in store.events_in_range(2, 4)] == [2, 3, 4]
    assert [e.seq for e in store.events_in_range(1, 5, source_id="a")] == [1, 3, 5]
    assert [e.seq for e in store.events_in_range(1, 5, after=3)] == [4, 5]
    assert [e.seq for e in store.events_in_range(1, 5, source_id="a", after=1)] == [3, 5]
    assert [e.seq for e in store.events_in_range(3, 3)] == [3]
    assert store.events_in_range(4, 2) == []
    assert store.events_in_range(6, 9) == []
    assert store.events_in_range(1, 5, after=5) == []


def test_max_seq_empty_is_zero(store: Store) -> None:
    assert store.max_seq() == 0


# --- cursors ---------------------------------------------------------------------------------


def test_cursors(store: Store) -> None:
    assert store.get_cursor("new-agent") == 0
    store.set_cursor("a1", 7, at(1))
    store.set_cursor("a2", 3, at(2))
    assert store.get_cursor("a1") == 7
    assert store.get_cursor("a2") == 3
    store.set_cursor("a1", 9, at(3))
    assert store.get_cursor("a1") == 9
    assert raw(store, "SELECT updated_at FROM cursors WHERE agent_id = 'a1'")[0][0] == (
        "2026-09-29T09:03:00Z"
    )


# --- served log ------------------------------------------------------------------------------


def test_served_log_roundtrip_and_filters(store: Store) -> None:
    args1 = {"budget_tokens": 800, "source": None}
    id1 = store.log_served("a1", "since", args1, "digest ✓", "mcp", at(1))
    id2 = store.log_served("a2", "get", {"handle": "since://evt/1"}, "evt text", "cli", at(2))
    id3 = store.log_served("a1", "get", {"handle": "since://evt/9"}, "error: nope", "mcp", at(3))
    assert (id1, id2, id3) == (1, 2, 3)
    everything = store.list_served()
    assert [e.id for e in everything] == [3, 2, 1]  # newest first
    first = everything[-1]
    assert first.agent_id == "a1"
    assert first.tool == "since"
    assert first.args == {"budget_tokens": 800, "source": None}
    assert first.text == "digest ✓"
    assert first.via == "mcp"
    assert first.at == "2026-09-29T09:01:00Z"
    assert [e.id for e in store.list_served("a1")] == [3, 1]
    assert [e.id for e in store.list_served(limit=2)] == [3, 2]
    assert store.list_served("nobody") == []


def test_served_log_keeps_full_text(store: Store) -> None:
    text = "line one\nline two\ttabbed\n" + "x" * 50_000
    store.log_served("a", "since", {}, text, "mcp", at())
    assert store.list_served()[0].text == text


# --- prune -----------------------------------------------------------------------------------


def test_prune_deletes_old_events_and_served_only(store: Store) -> None:
    old = store.append_event("s", KIND_ADDED, now=at(0), record_key="k1")
    keep = store.append_event("s", KIND_ADDED, now=at(60), record_key="k2")
    store.log_served("a", "since", {}, "old", "mcp", at(0))
    store.log_served("a", "since", {}, "new", "mcp", at(60))
    result = store.prune(before=at(30), now=at(90))
    assert result == (1, 1, 0)
    assert result.events == 1 and result.served == 1 and result.records == 0
    assert store.get_event(old) is None
    assert store.get_event(keep) is not None
    assert [e.text for e in store.list_served()] == ["new"]
    assert store.get_meta("pruned_through_seq") == str(old)


def test_prune_boundary_is_strictly_before(store: Store) -> None:
    store.append_event("s", KIND_ADDED, now=at(30))
    assert store.prune(before=at(30), now=at(31)) == (0, 0, 0)
    assert store.prune(before=at(31), now=at(31)) == (1, 0, 0)


def test_prune_without_deletions_leaves_pruned_through_seq(store: Store) -> None:
    store.append_event("s", KIND_ADDED, now=at(60))
    assert store.prune(before=at(0), now=at(61)) == (0, 0, 0)
    assert store.get_meta("pruned_through_seq") is None
    store.set_meta("pruned_through_seq", "4")
    store.prune(before=at(0), now=at(62))
    assert store.get_meta("pruned_through_seq") == "4"


def test_prune_through_seq_never_decreases(store: Store) -> None:
    store.set_meta("pruned_through_seq", "50")
    store.append_event("s", KIND_ADDED, now=at(0))
    store.prune(before=at(10), now=at(20))
    assert store.get_meta("pruned_through_seq") == "50"


def test_prune_records_last_pruned_at(store: Store) -> None:
    store.prune(before=at(0), now=at(42))
    assert store.get_meta("last_pruned_at") == "2026-09-29T09:42:00Z"


def test_prune_removed_records_referenced_by_events(store: Store) -> None:
    store.put_records(
        "s",
        [Record.make(k, {"v": 1}) for k in ("keep-ref", "orphan", "alive", "other-src-ref")],
        at(0),
    )
    store.mark_removed("s", ["keep-ref", "orphan", "other-src-ref"], at(1))
    store.put_records("t", [Record.make("orphan", {"v": 1})], at(0))
    store.mark_removed("t", ["orphan"], at(1))
    # old event on "orphan" (will be pruned), recent event on "keep-ref" (remains);
    # an event on the same key in a different source must not protect s/other-src-ref.
    store.append_event("s", KIND_REMOVED, now=at(1), record_key="orphan")
    store.append_event("s", KIND_REMOVED, now=at(100), record_key="keep-ref")
    store.append_event("t", KIND_REMOVED, now=at(100), record_key="other-src-ref")

    result = store.prune(before=at(50), now=at(101))
    assert result.events == 1
    assert result.records == 3  # s/orphan, s/other-src-ref, t/orphan

    assert store.get_record("s", "keep-ref") is not None  # still referenced
    assert store.get_record("s", "orphan") is None
    assert store.get_record("s", "other-src-ref") is None
    assert store.get_record("t", "orphan") is None
    assert store.get_record("s", "alive") is not None  # present records are never pruned
    assert set(store.get_snapshot("s")) == {"alive"}


def test_prune_ignores_events_without_record_key(store: Store) -> None:
    store.put_records("s", [Record.make("k", {})], at(0))
    store.mark_removed("s", ["k"], at(1))
    store.append_event("s", KIND_SOURCE_ERROR, now=at(100), detail={"error": "x"})  # NULL key
    assert store.prune(before=at(50), now=at(101)).records == 1


def test_seq_never_reused_after_prune(store: Store) -> None:
    assert _seed_events(store) == [1, 2, 3, 4, 5]
    assert store.prune(before=at(1000), now=at(1001)).events == 5  # everything
    assert store.events_after(0) == []
    assert store.max_seq() == 5  # survives pruning everything
    assert store.get_meta("pruned_through_seq") == "5"
    nxt = store.append_event("a", KIND_ADDED, now=at(2000))
    assert nxt == 6
    assert store.max_seq() == 6
    # and across a reopen
    store.close()
    with Store.open() as again:
        assert again.max_seq() == 6
        assert again.append_event("a", KIND_ADDED, now=at(2001)) == 7


def test_max_seq_after_partial_prune(store: Store) -> None:
    _seed_events(store)  # created at minutes 0..4
    store.prune(before=at(3), now=at(10))  # drops seq 1-3
    assert [e.seq for e in store.events_after(0)] == [4, 5]
    assert store.max_seq() == 5
    assert store.get_meta("pruned_through_seq") == "3"


# --- transactions ----------------------------------------------------------------------------


def test_transaction_commits(store: Store, since_home_dir: Path) -> None:
    with store.transaction():
        store.append_event("s", KIND_ADDED, now=at())
        store.set_cursor("a", 1, at())
    other = sqlite3.connect(since_home_dir / "since.db")
    try:
        assert other.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        other.close()


def test_transaction_rolls_back_on_error(store: Store) -> None:
    add_source(store)
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            store.put_records("docs", [Record.make("k", {"v": 1})], at())
            store.append_event("docs", KIND_ADDED, now=at(), record_key="k")
            store.set_cursor("a", 1, at())
            store.update_source_state("docs", baselined=True, record_count=1)
            store.set_meta("daemon_pid", "1")
            store.log_served("a", "since", {}, "t", "mcp", at())
            raise RuntimeError("boom")
    assert store.get_snapshot("docs") == {}
    assert store.events_after(0) == []
    assert store.max_seq() == 0
    assert store.get_cursor("a") == 0
    state = store.get_source_state("docs")
    assert state is not None and state.baselined is False and state.record_count == 0
    assert store.get_meta("daemon_pid") is None
    assert store.list_served() == []
    # the store is usable afterwards and seqs restart cleanly
    assert store.append_event("docs", KIND_ADDED, now=at()) == 1


def test_transaction_is_reentrant_and_nested_error_rolls_back_all(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.append_event("s", KIND_ADDED, now=at())
            with store.transaction():
                store.append_event("s", KIND_ADDED, now=at())
            store.set_cursor("a", 2, at())
            with store.transaction():
                raise RuntimeError("inner")
    assert store.events_after(0) == []
    assert store.get_cursor("a") == 0


def test_nested_transaction_success_commits_once(store: Store) -> None:
    with store.transaction():
        with store.transaction():
            store.append_event("s", KIND_ADDED, now=at())
        assert store._conn.in_transaction  # inner exit did not commit
        store.append_event("s", KIND_ADDED, now=at())
    assert not store._conn.in_transaction
    assert len(store.events_after(0)) == 2


def test_compound_methods_are_atomic_inside_caller_transaction(store: Store) -> None:
    store.put_records("s", [Record.make("a", {"v": 1})], at(0))
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put_records("s", [Record.make("b", {"v": 2})], at(1))
            store.mark_removed("s", ["a"], at(1))
            raise RuntimeError
    assert set(store.get_snapshot("s")) == {"a"}


def test_failed_statement_inside_transaction_rolls_back(store: Store) -> None:
    add_source(store)
    with pytest.raises(sqlite3.Error):
        with store.transaction():
            store.append_event("docs", KIND_ADDED, now=at())
            store._conn.execute("INSERT INTO no_such_table VALUES (1)")
    assert store.events_after(0) == []
    assert not store._conn.in_transaction


def test_second_connection_sees_committed_data_only(store: Store, since_home_dir: Path) -> None:
    with Store.open() as other:
        with store.transaction():
            store.append_event("s", KIND_ADDED, now=at())
            assert other.events_after(0) == []  # WAL: readers don't see uncommitted writes
        assert len(other.events_after(0)) == 1
