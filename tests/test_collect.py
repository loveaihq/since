"""Collection runner tests, driven by a fake collector (no real sources involved)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import since.sources as sources
from since.collect import (
    MAX_ERROR_CHARS,
    CollectResult,
    register_sources,
    run_collection,
)
from since.config import Config, ConfigError, HighlightRule, SourceConfig
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
    FieldChange,
    Record,
)
from since.sources import CollectError, get_collector
from since.store import Store
from since.timeutil import to_iso

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def rec(key: str, **fields: Any) -> Record:
    return Record.make(key, fields)


class FakeCollector:
    """Returns canned outcomes, one per ``collect`` call: a list of records, an exception to
    raise, or a zero-argument callable whose result is returned (or which may raise)."""

    def __init__(self, type_name: str = "dir", label: str = "") -> None:
        self.type_name = type_name
        self.label = label
        self.validate_error: Exception | None = None
        self.outcomes: list[Any] = []
        self.validated: list[str] = []

    def validate(self, cfg: SourceConfig) -> None:
        self.validated.append(cfg.id)
        if self.validate_error is not None:
            raise self.validate_error

    def key_label(self, cfg: SourceConfig) -> str:
        return self.label

    def collect(self, cfg: SourceConfig) -> Any:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome


def make_cfg(
    source_id: str = "src",
    *,
    priority: str = "normal",
    track_fields: list[str] | None = None,
    highlight: list[HighlightRule] | None = None,
    type: str = "dir",
) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        type=type,
        priority=priority,
        schedule_s=900,
        track_fields=track_fields,
        highlight=highlight or [],
    )


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def fake() -> FakeCollector:
    return FakeCollector()


def run(
    store: Store,
    cfg: SourceConfig,
    fake: FakeCollector,
    outcome: Any,
    minute: int,
) -> CollectResult:
    fake.outcomes.append(outcome)
    return run_collection(store, cfg, fake, at(minute))


def kinds(store: Store, source_id: str = "src") -> list[tuple[str, str | None]]:
    return [(e.kind, e.record_key) for e in store.events_after(0, source_id)]


# -- registry ------------------------------------------------------------------------------------


def test_get_collector_unimplemented_types_raise_not_implemented() -> None:
    for type_name in ("imap", "web", "changedetection"):
        with pytest.raises(NotImplementedError, match=type_name):
            get_collector(type_name)


def test_get_collector_unknown_type_is_value_error() -> None:
    with pytest.raises(ValueError, match="nope"):
        get_collector("nope")


def test_registry_maps_the_implemented_types_lazily() -> None:
    assert sources.REGISTRY == {
        "dir": "since.sources.dir:DirCollector",
        "sql": "since.sources.sql:SqlCollector",
    }
    # Importing the package must not import the concrete collectors (they may need extras).
    code = (
        "import sys, since.sources, since.collect; "
        "assert 'since.sources.dir' not in sys.modules; "
        "assert 'since.sources.sql' not in sys.modules; "
        "assert 'sqlalchemy' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_get_collector_imports_and_instantiates_the_registered_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sources.REGISTRY, "dir", f"{__name__}:FakeCollector")
    collector = get_collector("dir")
    assert isinstance(collector, FakeCollector)


# -- register_sources ----------------------------------------------------------------------------


def test_register_upserts_sources_with_key_label_and_marks_configured(store: Store) -> None:
    store.upsert_source("stale", "dir", "low", 60)  # in the DB but no longer in the config
    config = Config(
        sources=[
            make_cfg("docs", priority="high"),
            make_cfg("po-table", type="sql"),
        ]
    )
    dir_fake = FakeCollector("dir", label="")
    sql_fake = FakeCollector("sql", label="po_no")

    reg = register_sources(store, config, {"dir": dir_fake, "sql": sql_fake})

    assert [(cfg.id, c) for cfg, c in reg.collectable] == [
        ("docs", dir_fake),
        ("po-table", sql_fake),
    ]
    assert reg.skipped == []
    assert dir_fake.validated == ["docs"]
    docs = store.get_source_state("docs")
    assert docs is not None
    assert (docs.type, docs.priority, docs.schedule_s, docs.key_label) == ("dir", "high", 900, "")
    assert docs.configured is True and docs.baselined is False
    po = store.get_source_state("po-table")
    assert po is not None and po.key_label == "po_no" and po.configured is True
    stale = store.get_source_state("stale")
    assert stale is not None and stale.configured is False


def test_register_skips_unimplemented_types_with_reason(store: Store) -> None:
    config = Config(sources=[make_cfg("docs"), make_cfg("mail", type="imap")])

    reg = register_sources(store, config, {"dir": FakeCollector()})

    assert [cfg.id for cfg, _ in reg.collectable] == ["docs"]
    assert len(reg.skipped) == 1
    skipped_id, reason = reg.skipped[0]
    assert skipped_id == "mail" and "imap" in reason and "not implemented" in reason
    assert store.get_source_state("mail") is None
    # Also usable as a plain 2-tuple.
    collectable, skipped = reg
    assert collectable == reg.collectable and skipped == reg.skipped


def test_register_uses_builtin_registry_when_no_collectors_given(store: Store) -> None:
    reg = register_sources(store, Config(sources=[make_cfg("mail", type="imap")]))
    assert reg.collectable == []
    assert [sid for sid, _ in reg.skipped] == ["mail"]


def test_register_reflects_config_changes_but_keeps_collection_state(store: Store) -> None:
    fake = FakeCollector(label="a")
    register_sources(store, Config(sources=[make_cfg("src", priority="low")]), {"dir": fake})
    run(store, make_cfg("src"), fake, [rec("k", v=1)], 0)

    fake.label = "b"
    register_sources(store, Config(sources=[make_cfg("src", priority="high")]), {"dir": fake})

    state = store.get_source_state("src")
    assert state is not None
    assert (state.priority, state.key_label) == ("high", "b")
    assert state.baselined is True and state.record_count == 1


def test_register_validation_error_propagates_and_writes_nothing(store: Store) -> None:
    good = FakeCollector("dir")
    bad = FakeCollector("sql")
    bad.validate_error = ConfigError("source 'po': key 'query': required")
    config = Config(sources=[make_cfg("docs"), make_cfg("po", type="sql")])

    with pytest.raises(ConfigError, match="query"):
        register_sources(store, config, {"dir": good, "sql": bad})

    assert store.list_source_states() == []


# -- baseline ------------------------------------------------------------------------------------


def test_first_run_is_exactly_one_baseline_and_zero_added(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg()
    result = run(store, cfg, fake, [rec("a", v=1), rec("b", v=2), rec("c", v=3)], 0)

    events = store.events_after(0)
    assert [e.kind for e in events] == [KIND_BASELINE]
    assert result == CollectResult(seqs=[events[0].seq], error=None)
    assert events[0].detail == {"record_count": 3}
    assert events[0].record_key is None and events[0].field_changes == []
    assert events[0].importance == 2  # normal(2) x baseline(1)
    assert events[0].created_at == to_iso(at(0))
    assert sorted(store.get_snapshot("src")) == ["a", "b", "c"]
    state = store.get_source_state("src")
    assert state is not None
    assert state.baselined is True and state.record_count == 3
    assert state.last_success_at == to_iso(at(0))
    assert state.in_error is False and state.last_error is None


def test_empty_first_run_is_baseline_zero(store: Store, fake: FakeCollector) -> None:
    result = run(store, make_cfg(), fake, [], 0)

    events = store.events_after(0)
    assert [(e.kind, e.detail) for e in events] == [(KIND_BASELINE, {"record_count": 0})]
    assert result.seqs == [events[0].seq]
    state = store.get_source_state("src")
    assert state is not None and state.baselined is True and state.record_count == 0
    # ... and the source is now baselined: a later record is `added`, not another baseline.
    run(store, make_cfg(), fake, [rec("a", v=1)], 5)
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_ADDED, "a")]


def test_baseline_importance_uses_source_priority(store: Store, fake: FakeCollector) -> None:
    run(store, make_cfg(priority="high"), fake, [rec("a", v=1)], 0)
    assert store.events_after(0)[0].importance == 3


def test_missing_source_row_is_created_from_cfg_and_key_label(store: Store) -> None:
    fake = FakeCollector(label="po_no")
    cfg = make_cfg("po", priority="high", type="sql")
    assert store.get_source_state("po") is None

    run(store, cfg, fake, [rec("1", status="Open")], 0)

    state = store.get_source_state("po")
    assert state is not None
    assert (state.type, state.priority, state.schedule_s, state.key_label) == (
        "sql",
        "high",
        900,
        "po_no",
    )
    assert state.configured is True and state.baselined is True


def test_collect_runs_outside_the_write_transaction(store: Store, fake: FakeCollector) -> None:
    seen: list[bool] = []

    def slow_source() -> list[Record]:
        seen.append(store._conn.in_transaction)
        return [rec("a", v=1)]

    run(store, make_cfg(), fake, slow_source, 0)

    assert seen == [False]


# -- diff after baseline -------------------------------------------------------------------------


def test_diff_events_added_modified_removed_with_importance(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg(priority="high")
    run(store, cfg, fake, [rec("a", v=1), rec("b", v=1), rec("c", v=1)], 0)

    result = run(store, cfg, fake, [rec("a", v=2), rec("c", v=1), rec("d", v=1)], 5)

    events = store.events_after(0)[1:]
    assert [(e.kind, e.record_key) for e in events] == [
        (KIND_MODIFIED, "a"),
        (KIND_REMOVED, "b"),
        (KIND_ADDED, "d"),
    ]
    assert result.seqs == [e.seq for e in events] and result.error is None
    assert [e.importance for e in events] == [12, 12, 9]  # high(3) x 4 / 4 / 3
    assert events[0].field_changes == [FieldChange("v", 1, 2)]
    assert all(e.created_at == to_iso(at(5)) for e in events)
    assert sorted(store.get_snapshot("src")) == ["a", "c", "d"]
    assert store.get_snapshot("src")["a"].fields == {"v": 2}
    removed = store.get_record("src", "b")
    assert removed is not None and removed[1] is False and removed[0].fields == {"v": 1}
    state = store.get_source_state("src")
    assert state is not None and state.record_count == 3
    assert state.last_success_at == to_iso(at(5))


def test_unchanged_run_emits_nothing_but_updates_last_success(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1)], 0)

    result = run(store, cfg, fake, [rec("a", v=1)], 5)

    assert result == CollectResult(seqs=[], error=None)
    assert kinds(store) == [(KIND_BASELINE, None)]
    state = store.get_source_state("src")
    assert state is not None and state.last_success_at == to_iso(at(5))


def test_removed_record_can_come_back_as_added(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1), rec("b", v=1)], 0)
    run(store, cfg, fake, [rec("a", v=1)], 5)
    run(store, cfg, fake, [rec("a", v=1), rec("b", v=1)], 10)

    assert kinds(store) == [
        (KIND_BASELINE, None),
        (KIND_REMOVED, "b"),
        (KIND_ADDED, "b"),
    ]
    got = store.get_record("src", "b")
    assert got is not None and got[1] is True


def test_highlight_rules_add_their_bonus(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg(
        priority="high",
        highlight=[HighlightRule("status", "changed_to", "Cancelled")],
    )
    run(store, cfg, fake, [rec("po1", status="Open"), rec("po2", status="Open")], 0)

    run(store, cfg, fake, [rec("po1", status="Cancelled"), rec("po2", status="Closed")], 5)

    by_key = {e.record_key: e.importance for e in store.events_after(0)[1:]}
    assert by_key == {"po1": 3 * 4 + 10, "po2": 3 * 4}


def test_highlight_never_applies_to_source_level_events(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg(highlight=[HighlightRule("status", "contains", "o")])
    run(store, cfg, fake, [rec("a", status="on")], 0)
    run(store, cfg, fake, RuntimeError("x"), 5)
    run(store, cfg, fake, [rec("a", status="on")], 10)

    assert [e.importance for e in store.events_after(0)] == [2, 10, 2]


def test_track_fields_limit_the_events(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg(track_fields=["status"])
    run(store, cfg, fake, [rec("a", status="Open", note="x")], 0)

    second = [rec("a", status="Closed", note="y"), rec("b", status="Open", note="z")]
    run(store, cfg, fake, second, 5)

    events = store.events_after(0)[1:]
    assert [(e.kind, e.record_key) for e in events] == [(KIND_MODIFIED, "a"), (KIND_ADDED, "b")]
    assert events[0].field_changes == [FieldChange("status", "Open", "Closed")]
    assert events[1].field_changes == [FieldChange("status", None, "Open")]


def test_hash_only_change_of_untracked_field_updates_snapshot_without_event(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg(track_fields=["status"])
    run(store, cfg, fake, [rec("a", status="Open", note="x")], 0)
    before = store.get_snapshot("src")["a"]

    result = run(store, cfg, fake, [rec("a", status="Open", note="y")], 5)

    assert result == CollectResult(seqs=[], error=None)
    assert kinds(store) == [(KIND_BASELINE, None)]
    after = store.get_snapshot("src")["a"]
    assert after.fields == {"status": "Open", "note": "y"}
    assert after.content_hash == rec("a", status="Open", note="y").content_hash
    assert after.content_hash != before.content_hash
    got = store.get_record("src", "a")
    assert got is not None and got[2] == to_iso(at(5))  # updated_at moved
    state = store.get_source_state("src")
    assert state is not None and state.last_success_at == to_iso(at(5))

    # The next real change is diffed against the refreshed snapshot.
    run(store, cfg, fake, [rec("a", status="Closed", note="y")], 10)
    last = store.events_after(0)[-1]
    assert last.kind == KIND_MODIFIED
    assert last.field_changes == [FieldChange("status", "Open", "Closed")]
    assert store.get_snapshot("src")["a"].fields == {"status": "Closed", "note": "y"}


def test_long_text_change_is_stored_as_char_counts(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg()
    old_text = "line\n" * 100
    run(store, cfg, fake, [rec("a", text=old_text)], 0)

    run(store, cfg, fake, [rec("a", text=old_text + "extra\n")], 5)

    (change,) = store.events_after(0)[-1].field_changes
    assert (change.old, change.new, change.added_chars, change.removed_chars) == (None, None, 6, 0)


# -- failure -------------------------------------------------------------------------------------


def test_failure_after_baseline_is_one_source_error_and_no_removed(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg(priority="high")
    run(store, cfg, fake, [rec("a", v=1), rec("b", v=2)], 0)
    snapshot_before = store.get_snapshot("src")

    result = run(store, cfg, fake, CollectError("connection refused"), 5)

    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    error_event = store.events_after(0)[-1]
    assert result == CollectResult(seqs=[error_event.seq], error="connection refused")
    assert error_event.detail == {"error": "connection refused"}
    assert error_event.importance == 15  # high(3) x source_error(5)
    assert error_event.created_at == to_iso(at(5))
    assert store.get_snapshot("src") == snapshot_before
    state = store.get_source_state("src")
    assert state is not None
    assert state.in_error is True and state.error_since == to_iso(at(5))
    assert (state.last_error, state.last_error_at) == ("connection refused", to_iso(at(5)))
    assert state.last_success_at == to_iso(at(0)) and state.record_count == 2


def test_second_failure_adds_no_event_but_updates_last_error(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1)], 0)
    run(store, cfg, fake, CollectError("first"), 5)

    result = run(store, cfg, fake, CollectError("second"), 10)

    assert result == CollectResult(seqs=[], error="second")
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    state = store.get_source_state("src")
    assert state is not None
    assert state.in_error is True
    assert state.error_since == to_iso(at(5))  # the streak started at the first failure
    assert (state.last_error, state.last_error_at) == ("second", to_iso(at(10)))


def test_success_after_failure_emits_recovered_then_diff_events(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg(priority="high")
    run(store, cfg, fake, [rec("a", v=1), rec("b", v=1)], 0)
    run(store, cfg, fake, CollectError("first"), 5)
    run(store, cfg, fake, CollectError("second"), 10)

    result = run(store, cfg, fake, [rec("a", v=2), rec("b", v=1), rec("c", v=1)], 15)

    assert kinds(store) == [
        (KIND_BASELINE, None),
        (KIND_SOURCE_ERROR, None),
        (KIND_SOURCE_RECOVERED, None),
        (KIND_MODIFIED, "a"),
        (KIND_ADDED, "c"),
    ]
    events = store.events_after(0)
    assert result.seqs == [e.seq for e in events[2:]] and result.error is None
    recovered = events[2]
    assert recovered.detail == {"error_since": to_iso(at(5)), "last_error": "second"}
    assert recovered.importance == 3  # high(3) x source_recovered(1)
    assert recovered.created_at == to_iso(at(15))
    state = store.get_source_state("src")
    assert state is not None
    assert state.in_error is False and state.error_since is None
    assert state.last_success_at == to_iso(at(15))
    assert state.last_error == "second"  # history kept for status()

    # A later failure starts a new streak with a new source_error.
    run(store, cfg, fake, CollectError("again"), 20)
    assert kinds(store)[-1] == (KIND_SOURCE_ERROR, None)


def test_recovery_with_no_data_changes_is_just_source_recovered(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1)], 0)
    run(store, cfg, fake, CollectError("down"), 5)

    run(store, cfg, fake, [rec("a", v=1)], 10)

    assert kinds(store) == [
        (KIND_BASELINE, None),
        (KIND_SOURCE_ERROR, None),
        (KIND_SOURCE_RECOVERED, None),
    ]


def test_first_run_failure_then_success_is_error_recovered_baseline(
    store: Store, fake: FakeCollector
) -> None:
    cfg = make_cfg()
    result = run(store, cfg, fake, CollectError("no such directory"), 0)

    assert kinds(store) == [(KIND_SOURCE_ERROR, None)]
    assert result.error == "no such directory" and len(result.seqs) == 1
    state = store.get_source_state("src")
    assert state is not None and state.baselined is False and state.in_error is True
    assert store.get_snapshot("src") == {}

    run(store, cfg, fake, [rec("a", v=1)], 5)

    assert kinds(store) == [
        (KIND_SOURCE_ERROR, None),
        (KIND_SOURCE_RECOVERED, None),
        (KIND_BASELINE, None),
    ]
    assert store.events_after(0)[-1].detail == {"record_count": 1}


def test_failure_message_format(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg()
    assert run(store, cfg, fake, CollectError("plain message"), 0).error == "plain message"
    assert run(store, cfg, fake, ValueError("boom"), 5).error == "ValueError: boom"
    assert run(store, cfg, fake, KeyError("k"), 10).error == "KeyError: 'k'"
    assert run(store, cfg, fake, CollectError(), 15).error == "CollectError"
    state = store.get_source_state("src")
    assert state is not None and state.last_error == "CollectError"


def test_failure_message_is_capped_in_event_and_state(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg()
    result = run(store, cfg, fake, CollectError("x" * 2000), 0)

    assert result.error is not None and len(result.error) == MAX_ERROR_CHARS == 500
    assert result.error.endswith("…")
    event = store.events_after(0)[0]
    assert event.detail == {"error": result.error}
    state = store.get_source_state("src")
    assert state is not None and state.last_error == result.error

    other = run(store, cfg, fake, RuntimeError("y" * 2000), 5).error
    assert other is not None and len(other) == 500 and other.startswith("RuntimeError: yyy")
    exact = run(store, cfg, fake, CollectError("z" * 500), 10).error
    assert exact == "z" * 500


def test_error_raised_lazily_by_a_generator_is_a_failure(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1)], 0)

    def broken() -> Iterator[Record]:
        yield rec("a", v=1)
        raise OSError("disk went away")

    result = run(store, cfg, fake, broken, 5)

    assert result.error == "OSError: disk went away"
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]


# -- invalid collector results -------------------------------------------------------------------


def raw(key: Any, fields: Any) -> Record:
    """A Record built without ``Record.make`` (fake hash), so invalid content can be expressed."""
    return Record(key, fields, "hash")


INVALID_RESULTS: list[tuple[str, Callable[[], Any], str]] = [
    (
        "duplicate",
        lambda: [rec("a", v=1), rec("b", v=1), rec("a", v=2)],
        'duplicate key "a" (2 records)',
    ),
    ("triplicate", lambda: [rec("a", v=1)] * 3, 'duplicate key "a" (3 records)'),
    ("empty-key", lambda: [rec("", v=1)], "invalid record key ''"),
    ("int-key", lambda: [raw(5, {"v": 1})], "invalid record key 5"),
    ("none-key", lambda: [raw(None, {})], "invalid record key None"),
    (
        "list-value",
        lambda: [raw("a", {"v": [1, 2]})],
        'record "a": field "v" has non-scalar value of type list',
    ),
    ("dict-value", lambda: [raw("a", {"v": {"x": 1}})], "non-scalar value of type dict"),
    ("bytes-value", lambda: [raw("a", {"v": b"x"})], "non-scalar value of type bytes"),
    ("datetime-value", lambda: [raw("a", {"v": T0})], "non-scalar value of type datetime"),
    ("int-field-name", lambda: [raw("a", {1: "x"})], "field name 1 must be a string"),
    ("fields-not-a-dict", lambda: [raw("a", [("v", 1)])], 'record "a": fields must be a dict'),
    ("not-a-record", lambda: [{"key": "a"}], "collector returned dict, expected Record"),
    ("not-iterable", lambda: None, "TypeError"),
]


@pytest.mark.parametrize(
    ("build", "expected"),
    [pytest.param(b, e, id=i) for i, b, e in INVALID_RESULTS],
)
def test_invalid_collector_result_is_a_failure(
    store: Store, fake: FakeCollector, build: Callable[[], Any], expected: str
) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1), rec("b", v=1)], 0)
    snapshot_before = store.get_snapshot("src")

    result = run(store, cfg, fake, build, 5)

    assert result.error is not None and expected in result.error
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    assert store.get_snapshot("src") == snapshot_before  # nothing removed, nothing replaced
    state = store.get_source_state("src")
    assert state is not None and state.in_error is True and state.last_error == result.error
    assert state.record_count == 2 and state.last_success_at == to_iso(at(0))


def test_scalar_values_of_every_allowed_type_are_accepted(
    store: Store, fake: FakeCollector
) -> None:
    result = run(
        store,
        make_cfg(),
        fake,
        [rec("a", s="x", i=1, f=1.5, b=True, n=None)],
        0,
    )
    assert result.error is None
    expected = {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None}
    assert store.get_snapshot("src")["a"].fields == expected


def test_duplicate_keys_on_first_run_fail_without_baseline(
    store: Store, fake: FakeCollector
) -> None:
    result = run(store, make_cfg(), fake, [rec("a", v=1), rec("a", v=2)], 0)

    assert result.error == 'duplicate key "a" (2 records)'
    assert kinds(store) == [(KIND_SOURCE_ERROR, None)]
    state = store.get_source_state("src")
    assert state is not None and state.baselined is False
    assert store.get_snapshot("src") == {}


# -- atomicity -----------------------------------------------------------------------------------


def _crash_on_call(
    monkeypatch: pytest.MonkeyPatch, store: Store, method: str, nth: int
) -> None:
    """Make ``store.<method>`` raise on its ``nth`` call (1-based); earlier calls run for real."""
    real = getattr(store, method)
    calls = {"n": 0}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == nth:
            raise RuntimeError("simulated crash")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, method, wrapper)


def _db_state(store: Store, keys: tuple[str, ...] = ("a", "b", "c", "d")) -> tuple[Any, ...]:
    return (
        store.events_after(0),
        store.max_seq(),
        store.get_source_state("src"),
        store.get_snapshot("src"),
        [store.get_record("src", k) for k in keys],
    )


CRASH_POINTS = [
    ("append_event", 1),
    ("append_event", 2),
    ("append_event", 3),
    ("put_records", 1),
    ("mark_removed", 1),
    ("update_source_state", 1),
]


@pytest.mark.parametrize(("method", "nth"), CRASH_POINTS)
def test_crash_mid_write_leaves_no_partial_state_after_baseline(
    store: Store,
    fake: FakeCollector,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    nth: int,
) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1), rec("b", v=1), rec("c", v=1)], 0)
    before = _db_state(store)
    # Produces modified a, removed b, added d: three events, a snapshot update and a removal.
    changed = [rec("a", v=2), rec("c", v=1), rec("d", v=1)]

    _crash_on_call(monkeypatch, store, method, nth)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run(store, cfg, fake, changed, 5)

    assert _db_state(store) == before
    assert store._conn.in_transaction is False

    # The store is still usable and the same run succeeds once the fault is gone.
    monkeypatch.undo()
    fake.outcomes.clear()
    result = run(store, cfg, fake, changed, 6)
    assert result.error is None and len(result.seqs) == 3
    assert [e.kind for e in store.events_after(0)][1:] == [KIND_MODIFIED, KIND_REMOVED, KIND_ADDED]


@pytest.mark.parametrize("method", ["put_records", "append_event", "update_source_state"])
def test_crash_mid_baseline_leaves_no_partial_state(
    store: Store, fake: FakeCollector, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    cfg = make_cfg()
    register_sources(store, Config(sources=[cfg]), {"dir": fake})
    before = _db_state(store)

    _crash_on_call(monkeypatch, store, method, 1)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run(store, cfg, fake, [rec("a", v=1)], 0)

    assert _db_state(store) == before
    state = store.get_source_state("src")
    assert state is not None and state.baselined is False
    assert store.events_after(0) == [] and store.get_snapshot("src") == {}


def test_crash_after_recovered_event_rolls_the_recovery_back(
    store: Store, fake: FakeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1)], 0)
    run(store, cfg, fake, CollectError("down"), 5)
    before = _db_state(store)

    _crash_on_call(monkeypatch, store, "append_event", 2)  # 1st = recovered, 2nd = modified
    with pytest.raises(RuntimeError, match="simulated crash"):
        run(store, cfg, fake, [rec("a", v=2)], 10)

    assert _db_state(store) == before
    state = store.get_source_state("src")
    assert state is not None and state.in_error is True and state.error_since == to_iso(at(5))


@pytest.mark.parametrize(("method", "nth"), [("append_event", 1), ("update_source_state", 1)])
def test_crash_while_recording_a_failure_leaves_no_partial_state(
    store: Store,
    fake: FakeCollector,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    nth: int,
) -> None:
    cfg = make_cfg()
    run(store, cfg, fake, [rec("a", v=1)], 0)
    before = _db_state(store)

    _crash_on_call(monkeypatch, store, method, nth)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run(store, cfg, fake, CollectError("down"), 5)

    assert _db_state(store) == before
    state = store.get_source_state("src")
    assert state is not None and state.in_error is False and state.last_error is None


def test_failed_run_leaves_every_record_present(store: Store, fake: FakeCollector) -> None:
    cfg = make_cfg()
    keys = tuple(str(i) for i in range(20))
    run(store, cfg, fake, [rec(k, v=1) for k in keys], 0)
    before = _db_state(store, keys)

    run(store, cfg, fake, ConnectionError("down"), 5)

    after = _db_state(store, keys)
    assert after[3] == before[3] and after[4] == before[4]  # snapshot + record rows untouched
    assert len(store.get_snapshot("src")) == 20
    assert [e.kind for e in store.events_after(0)] == [KIND_BASELINE, KIND_SOURCE_ERROR]
