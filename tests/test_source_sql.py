"""``sql`` source tests: SQLite file databases, driven through ``run_collection``."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from since.collect import CollectResult, register_sources, run_collection
from since.config import Config, ConfigError, HighlightRule, SourceConfig
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
)
from since.sources import CollectError, get_collector
from since.sources.sql import SqlCollector, _safe_message, _to_scalar
from since.store import Store

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)
ENV = "SINCE_TEST_DB_URL"
PO_QUERY = "select po_no, status, eta from po"


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


class Db:
    """A SQLite file database in a tmp dir."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def url(self) -> str:
        return f"sqlite:///{self.path.as_posix()}"

    def run(self, sql: str, *params: Any) -> None:
        con = sqlite3.connect(self.path)
        try:
            with con:
                con.execute(sql, params)
        finally:
            con.close()

    def scalar(self, sql: str) -> Any:
        con = sqlite3.connect(self.path)
        try:
            return con.execute(sql).fetchone()[0]
        finally:
            con.close()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Db:
    database = Db(tmp_path / "erp.db")
    database.run("create table po (po_no text primary key, status text, eta text)")
    database.run("insert into po values ('4500123', 'Open', '2026-10-01')")
    database.run("insert into po values ('4500124', 'Open', '2026-10-05')")
    monkeypatch.setenv(ENV, database.url)
    return database


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


def make_cfg(
    query: str = PO_QUERY,
    key: list[str] | None = None,
    *,
    track_fields: list[str] | None = None,
    highlight: list[HighlightRule] | None = None,
    priority: str = "high",
    **extra: Any,
) -> SourceConfig:
    options: dict[str, Any] = {
        "url_env": ENV,
        "query": query,
        "key": key if key is not None else ["po_no"],
    }
    options.update(extra)
    return SourceConfig(
        id="po-table",
        type="sql",
        priority=priority,
        schedule_s=900,
        track_fields=track_fields,
        highlight=highlight or [],
        options=options,
    )


def run(store: Store, cfg: SourceConfig, minute: int = 0) -> CollectResult:
    return run_collection(store, cfg, SqlCollector(), at(minute))


def events(store: Store) -> list[Any]:
    return store.events_after(0, "po-table")


def kinds(store: Store) -> list[tuple[str, str | None]]:
    return [(e.kind, e.record_key) for e in events(store)]


# -- module / registry ---------------------------------------------------------------------------


def test_registry_resolves_the_sql_collector() -> None:
    collector = get_collector("sql")
    assert isinstance(collector, SqlCollector)
    assert collector.type_name == "sql"


def test_importing_the_module_does_not_import_sqlalchemy() -> None:
    code = "import sys, since.sources.sql; assert 'sqlalchemy' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_sqlalchemy_is_a_collect_error(
    db: Db, monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)  # makes `import sqlalchemy` fail
    with pytest.raises(CollectError) as info:
        SqlCollector().collect(make_cfg())
    assert str(info.value) == "SQLAlchemy is not installed: install since[sql]"
    result = run(store, make_cfg())
    assert result.error == "SQLAlchemy is not installed: install since[sql]"


# -- validate / key_label ------------------------------------------------------------------------


def test_validate_accepts_the_documented_config_without_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)  # no env var, no database: validate must not care
    SqlCollector().validate(make_cfg(key=["po_no", "line"]))


@pytest.mark.parametrize(
    ("options", "fragment"),
    [
        ({"query": "select 1", "key": ["a"]}, "key 'url_env'"),
        ({"url_env": "", "query": "select 1", "key": ["a"]}, "key 'url_env'"),
        ({"url_env": 5, "query": "select 1", "key": ["a"]}, "key 'url_env'"),
        ({"url_env": ENV, "key": ["a"]}, "key 'query'"),
        ({"url_env": ENV, "query": "  ", "key": ["a"]}, "key 'query'"),
        ({"url_env": ENV, "query": "select 1"}, "key 'key'"),
        ({"url_env": ENV, "query": "select 1", "key": []}, "key 'key'"),
        ({"url_env": ENV, "query": "select 1", "key": "a"}, "key 'key'"),
        ({"url_env": ENV, "query": "select 1", "key": ["a", 3]}, "key 'key'"),
        ({"url_env": ENV, "query": "select 1", "key": ["a", ""]}, "key 'key'"),
        ({"url_env": ENV, "query": "select 1", "key": ["a"], "timeout": 5}, "key 'timeout'"),
        ({"url_env": ENV, "query": "select 1", "key": ["a"], "url": "x"}, "key 'url'"),
    ],
)
def test_validate_rejects_bad_options(options: dict[str, Any], fragment: str) -> None:
    cfg = SourceConfig(id="po-table", type="sql", options=options)
    with pytest.raises(ConfigError) as info:
        SqlCollector().validate(cfg)
    assert "source 'po-table'" in str(info.value)
    assert fragment in str(info.value)


def test_key_label_joins_key_columns_with_pipe() -> None:
    assert SqlCollector().key_label(make_cfg(key=["po_no"])) == "po_no"
    assert SqlCollector().key_label(make_cfg(key=["po_no", "line_no"])) == "po_no|line_no"


def test_key_label_tolerates_a_malformed_config() -> None:
    cfg = SourceConfig(id="po-table", type="sql", options={})
    assert SqlCollector().key_label(cfg) == ""


def test_register_sources_stores_the_key_label(store: Store) -> None:
    register_sources(store, Config(sources=[make_cfg(key=["po_no", "line_no"])]))
    state = store.get_source_state("po-table")
    assert state is not None and state.key_label == "po_no|line_no"


def test_collect_reports_invalid_options_as_a_source_error(store: Store) -> None:
    result = run(store, make_cfg(timeout=30))
    assert result.error is not None and "key 'timeout'" in result.error
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]


# -- baseline / diff through run_collection ------------------------------------------------------


def test_first_run_is_one_baseline_and_records_are_stored(db: Db, store: Store) -> None:
    result = run(store, make_cfg())

    assert result.error is None
    assert kinds(store) == [(KIND_BASELINE, None)]
    assert events(store)[0].detail == {"record_count": 2}
    snapshot = store.get_snapshot("po-table")
    assert sorted(snapshot) == ["4500123", "4500124"]
    assert snapshot["4500123"].fields == {
        "po_no": "4500123",
        "status": "Open",
        "eta": "2026-10-01",
    }
    state = store.get_source_state("po-table")
    assert state is not None and state.record_count == 2 and state.baselined


def test_unchanged_second_run_produces_no_events(db: Db, store: Store) -> None:
    run(store, make_cfg(), 0)
    result = run(store, make_cfg(), 1)
    assert result.seqs == [] and result.error is None
    assert kinds(store) == [(KIND_BASELINE, None)]


def test_added_modified_and_removed_rows(db: Db, store: Store) -> None:
    run(store, make_cfg(), 0)
    db.run("update po set status = 'Shipped' where po_no = '4500123'")
    db.run("delete from po where po_no = '4500124'")
    db.run("insert into po values ('4500125', 'Open', '2026-11-01')")

    result = run(store, make_cfg(), 1)

    assert result.error is None
    assert kinds(store) == [
        (KIND_BASELINE, None),
        (KIND_MODIFIED, "4500123"),
        (KIND_REMOVED, "4500124"),
        (KIND_ADDED, "4500125"),
    ]
    modified = events(store)[1]
    assert [(c.field, c.old, c.new) for c in modified.field_changes] == [
        ("status", "Open", "Shipped")
    ]
    record = store.get_record("po-table", "4500124")
    assert record is not None and record[1] is False  # marked removed, still retrievable


def test_track_fields_limits_the_diff(db: Db, store: Store) -> None:
    cfg = make_cfg(track_fields=["status"])
    run(store, cfg, 0)

    db.run("update po set eta = '2027-01-01' where po_no = '4500123'")  # untracked only
    assert run(store, cfg, 1).seqs == []

    db.run("update po set status = 'Late', eta = '2027-02-02' where po_no = '4500124'")
    run(store, cfg, 2)
    modified = [e for e in events(store) if e.kind == KIND_MODIFIED]
    assert len(modified) == 1
    assert [c.field for c in modified[0].field_changes] == ["status"]


def test_highlight_changed_to_raises_importance(db: Db, store: Store) -> None:
    cfg = make_cfg(highlight=[HighlightRule("status", "changed_to", "Cancelled")])
    run(store, cfg, 0)
    db.run("update po set status = 'Cancelled' where po_no = '4500123'")
    db.run("update po set status = 'Shipped' where po_no = '4500124'")

    run(store, cfg, 1)

    by_key = {e.record_key: e.importance for e in events(store) if e.kind == KIND_MODIFIED}
    assert by_key == {"4500123": 3 * 4 + 10, "4500124": 3 * 4}


def test_composite_key(db: Db, store: Store) -> None:
    db.run("create table lines (po_no text, line_no integer, qty integer)")
    db.run("insert into lines values ('4500123', 20, 5)")
    db.run("insert into lines values ('4500123', 10, 7)")
    cfg = make_cfg("select po_no, line_no, qty from lines", ["po_no", "line_no"])

    run(store, cfg, 0)
    assert sorted(store.get_snapshot("po-table")) == ["4500123|10", "4500123|20"]

    db.run("update lines set qty = 9 where line_no = 10")
    run(store, cfg, 1)
    assert kinds(store)[-1] == (KIND_MODIFIED, "4500123|10")
    assert events(store)[-1].field_changes[0].new == 9


def test_collect_returns_records_sorted_by_key(db: Db) -> None:
    records = SqlCollector().collect(make_cfg(PO_QUERY + " order by po_no desc"))
    assert [r.key for r in records] == ["4500123", "4500124"]


def test_empty_result_is_a_valid_empty_baseline(db: Db, store: Store) -> None:
    run(store, make_cfg(PO_QUERY + " where 1 = 0"))
    assert events(store)[0].detail == {"record_count": 0}


# -- failures ------------------------------------------------------------------------------------


def test_missing_env_var_names_the_variable_only(
    db: Db, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV)
    result = run(store, make_cfg())
    assert result.error is not None and ENV in result.error
    assert db.url not in result.error
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]


def test_empty_env_var_is_treated_as_unset(
    db: Db, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, "  ")
    result = run(store, make_cfg())
    assert result.error is not None and ENV in result.error


def test_bad_query_fails_without_removed_events_and_recovers(db: Db, store: Store) -> None:
    run(store, make_cfg(), 0)

    result = run(store, make_cfg("select po_no from no_such_table"), 1)
    assert result.error is not None
    assert result.error.startswith("OperationalError: (sqlite3.OperationalError) no such table")
    assert "\n" not in result.error
    assert "SQL:" not in result.error and "Background" not in result.error

    assert run(store, make_cfg("select nope from po"), 2).seqs == []  # 2nd failure: no new event
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    assert sorted(store.get_snapshot("po-table")) == ["4500123", "4500124"]

    run(store, make_cfg(), 3)  # fixed
    assert kinds(store)[-1] == (KIND_SOURCE_RECOVERED, None)
    assert KIND_REMOVED not in [k for k, _ in kinds(store)]


def test_connect_failure_is_a_source_error(
    db: Db, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, f"sqlite:///{(tmp_path / 'no' / 'such' / 'x.db').as_posix()}")
    result = run(store, make_cfg())
    assert result.error is not None
    assert "unable to open database file" in result.error
    assert "Background" not in result.error


def test_key_column_missing_from_result_names_the_column(db: Db, store: Store) -> None:
    result = run(store, make_cfg(key=["order_no"]))
    assert result.error == 'key column "order_no" is not in the query result'
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]


def test_key_column_check_applies_to_an_empty_result_too(db: Db) -> None:
    with pytest.raises(CollectError, match="order_no"):
        SqlCollector().collect(make_cfg(PO_QUERY + " where 1 = 0", ["order_no"]))


def test_null_key_value_is_an_error(db: Db, store: Store) -> None:
    db.run("create table t (id text, v text)")
    db.run("insert into t values (NULL, 'x')")
    result = run(store, make_cfg("select id, v from t", ["id"]))
    assert result.error == 'key column "id" is null'


def test_empty_key_value_is_an_error(db: Db) -> None:
    with pytest.raises(CollectError, match="key is empty"):
        SqlCollector().collect(make_cfg("select '' as id", ["id"]))


def test_duplicate_result_columns_are_an_error(db: Db) -> None:
    with pytest.raises(CollectError, match='duplicate column "a"'):
        SqlCollector().collect(make_cfg("select 1 as a, 2 as a", ["a"]))


def test_duplicate_keys_fail_the_run(db: Db, store: Store) -> None:
    result = run(store, make_cfg("select 'k' as id, 1 as n union all select 'k', 2", ["id"]))
    assert result.error == 'duplicate key "k" (2 records)'


def test_statement_without_rows_is_a_source_error(db: Db, store: Store) -> None:
    result = run(store, make_cfg("delete from po"))
    assert result.error is not None
    assert db.scalar("select count(*) from po") == 2  # nothing was committed


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="needs INSERT ... RETURNING")
def test_never_commits(db: Db, store: Store) -> None:
    result = run(store, make_cfg("insert into po values ('9', 'x', 'y') returning po_no, status"))
    assert result.error is None  # the statement ran and returned a row...
    assert db.scalar("select count(*) from po") == 2  # ...but was rolled back


def test_database_file_is_released_after_collect(db: Db) -> None:
    """The engine is disposed: on Windows an open connection would block deleting the file."""
    SqlCollector().collect(make_cfg())
    db.path.unlink()
    assert not db.path.exists()


def test_engine_is_disposed_on_success_and_on_failure(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlalchemy

    disposed: list[int] = []
    real_create_engine = sqlalchemy.create_engine

    def spy(*args: Any, **kwargs: Any) -> Any:
        engine = real_create_engine(*args, **kwargs)
        real_dispose = engine.dispose

        def dispose(*a: Any, **k: Any) -> None:
            disposed.append(1)
            real_dispose(*a, **k)

        engine.dispose = dispose
        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", spy)
    SqlCollector().collect(make_cfg())
    with pytest.raises(CollectError):
        SqlCollector().collect(make_cfg("select * from nope"))
    assert disposed == [1, 1]


# -- values --------------------------------------------------------------------------------------


def test_sqlite_value_types(db: Db) -> None:
    query = "select 'k' as id, 7 as i, 1.5 as f, 'txt' as s, NULL as n, X'00ff10' as b"
    [record] = SqlCollector().collect(make_cfg(query, ["id"]))
    fields = record.fields
    assert (fields["i"], fields["f"], fields["s"], fields["n"]) == (7, 1.5, "txt", None)
    digest = hashlib.sha256(bytes([0x00, 0xFF, 0x10])).hexdigest()[:12]
    assert fields["b"] == f"<3 bytes sha256={digest}>"


def test_to_scalar_conversions() -> None:
    assert _to_scalar(None) is None
    assert _to_scalar(True) is True
    assert _to_scalar(3) == 3 and isinstance(_to_scalar(3), int)
    assert _to_scalar(2.5) == 2.5
    assert _to_scalar("x") == "x"
    assert _to_scalar(Decimal("12.50")) == "12.50"
    assert _to_scalar(date(2026, 9, 29)) == "2026-09-29"
    assert _to_scalar(datetime(2026, 9, 29, 9, 12, 5)) == "2026-09-29T09:12:05"
    assert _to_scalar(datetime(2026, 9, 29, 9, 12, tzinfo=UTC)) == "2026-09-29T09:12:00+00:00"
    assert _to_scalar(time(9, 12, 5)) == "09:12:05"
    digest = hashlib.sha256(b"abc").hexdigest()[:12]
    assert _to_scalar(b"abc") == f"<3 bytes sha256={digest}>"
    assert _to_scalar(bytearray(b"abc")) == f"<3 bytes sha256={digest}>"
    assert _to_scalar(memoryview(b"abc")) == f"<3 bytes sha256={digest}>"
    assert _to_scalar(b"") == "<0 bytes sha256=e3b0c44298fc>"
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert _to_scalar(ident) == str(ident)
    assert _to_scalar(timedelta(hours=1)) == "1:00:00"


def test_key_is_the_string_of_the_converted_value(db: Db) -> None:
    [record] = SqlCollector().collect(make_cfg("select 42 as id, 'x' as v", ["id"]))
    assert record.key == "42" and record.fields["id"] == 42


# -- secrets -------------------------------------------------------------------------------------

PASSWORD = "s3cr3t-Pa55"


def assert_no_secret(store: Store, *secrets: str) -> None:
    state = store.get_source_state("po-table")
    assert state is not None and state.last_error
    texts = [state.last_error] + [str(e.detail) for e in events(store)]
    for text in texts:
        for secret in secrets:
            assert secret not in text


def test_password_in_an_invalid_sqlite_url_never_reaches_the_error(
    db: Db, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite://erp_user:{PASSWORD}@/nonexistent/x.db"
    monkeypatch.setenv(ENV, url)
    result = run(store, make_cfg())
    assert result.error is not None and result.error.startswith("ArgumentError")
    assert_no_secret(store, PASSWORD, url)


def test_password_with_unknown_driver_never_reaches_the_error(
    db: Db, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"postgresql+no_such_driver://erp_user:{PASSWORD}@db.example.invalid:5432/erp"
    monkeypatch.setenv(ENV, url)
    result = run(store, make_cfg())
    assert result.error is not None and "no_such_driver" in result.error
    assert_no_secret(store, PASSWORD, url)


@pytest.mark.parametrize("password", [PASSWORD, "p%40ss:w/rd"])
def test_error_that_echoes_the_url_is_scrubbed(
    db: Db, store: Store, monkeypatch: pytest.MonkeyPatch, password: str
) -> None:
    """Simulates a driver whose error text includes the connect string / the decoded password."""
    import sqlalchemy
    from sqlalchemy.engine import make_url

    url = f"postgresql://erp_user:{password.replace('/', '%2F').replace(':', '%3A')}@db.invalid/erp"
    decoded = make_url(url).password
    assert decoded is not None
    monkeypatch.setenv(ENV, url)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"cannot connect to {url} (password {decoded})\nSQL: select 1\nmore")

    monkeypatch.setattr(sqlalchemy, "create_engine", boom)
    result = run(store, make_cfg())

    assert result.error == "RuntimeError: cannot connect to *** (password ***)"
    assert_no_secret(store, password, decoded, url)


def test_unparsable_url_is_not_echoed(
    db: Db, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, f"not a url {PASSWORD}")
    result = run(store, make_cfg())
    assert result.error is not None and ENV in result.error
    assert_no_secret(store, PASSWORD, "not a url")


def test_safe_message_keeps_the_first_line_and_redacts() -> None:
    exc = ValueError("first line: SECRET and URL://x\n[SQL: select 1]\n(Background on this error)")
    assert _safe_message(exc, ["SECRET", "URL://x"]) == "ValueError: first line: *** and ***"
    assert _safe_message(ValueError(""), []) == "ValueError:"
