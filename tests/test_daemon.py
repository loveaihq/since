"""Daemon tests: fake clock, fake sleep, fake collectors (no real sources, no waiting)."""

from __future__ import annotations

import io
import signal
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import since.daemon as daemon_mod
from since.collect import run_collection
from since.config import Config, SourceConfig
from since.daemon import META_HEARTBEAT as HEARTBEAT
from since.daemon import Daemon, DaemonError
from since.model import KIND_BASELINE, KIND_SOURCE_ERROR, KIND_SOURCE_RECOVERED, Record
from since.store import Store
from since.timeutil import to_iso

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def stamp(seconds: float) -> str:
    return to_iso(at(seconds))


def src(source_id: str, schedule_s: int = 900, type: str = "dir") -> SourceConfig:
    return SourceConfig(id=source_id, type=type, schedule_s=schedule_s)


def rec(key: str, **fields: Any) -> Record:
    return Record.make(key, fields)


class Clock:
    """Fake clock: ``clock()`` is ``now_fn``; only ``advance`` / a sleep moves it."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Sleeper:
    """Fake ``sleep_fn``: records every requested duration and advances the clock. Raises
    KeyboardInterrupt on call number ``stop_after`` (so exactly that many ticks ran)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.stop_after = 50  # safety net so a broken loop cannot hang the suite
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) >= self.stop_after:
            raise KeyboardInterrupt
        self.clock.advance(seconds)


class FakeCollector:
    """One collector serving every source (looked up by source id in ``results``): a list of
    Records, an exception to raise, or a zero-argument callable. Not consumed by use, so a
    result stays in force until the test replaces it. Default: an empty source."""

    type_name = "dir"

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.results: dict[str, Any] = {}
        self.calls: list[tuple[str, datetime]] = []

    @property
    def called(self) -> list[str]:
        return [source_id for source_id, _ in self.calls]

    def validate(self, cfg: SourceConfig) -> None:
        pass

    def key_label(self, cfg: SourceConfig) -> str:
        return ""

    def collect(self, cfg: SourceConfig) -> Any:
        self.calls.append((cfg.id, self.clock()))
        outcome = self.results.get(cfg.id, [])
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sleeper(clock: Clock) -> Sleeper:
    return Sleeper(clock)


@pytest.fixture
def fake(clock: Clock) -> FakeCollector:
    return FakeCollector(clock)


@pytest.fixture
def log() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def make_daemon(
    store: Store, clock: Clock, sleeper: Sleeper, fake: FakeCollector, log: io.StringIO
) -> Callable[..., Daemon]:
    def factory(
        *sources: SourceConfig,
        pid: int = 1001,
        retention_days: int = 30,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> Daemon:
        return Daemon(
            Config(sources=list(sources), retention_days=retention_days),
            store,
            clock,
            sleeper if sleep_fn is None else sleep_fn,
            collectors={"dir": fake},
            log=log,
            pid=pid,
        )

    return factory


def kinds(store: Store, source_id: str) -> list[str]:
    return [e.kind for e in store.events_after(0, source_id)]


def log_lines(log: io.StringIO) -> list[str]:
    return log.getvalue().splitlines()


# -- start ---------------------------------------------------------------------------------------


def test_start_registers_sources_and_writes_meta(
    store: Store, make_daemon: Callable[..., Daemon], log: io.StringIO
) -> None:
    d = make_daemon(src("b", 300), src("a", 60), src("mail", 10, type="imap"))
    d.start()
    assert store.get_meta("daemon_pid") == "1001"
    assert store.get_meta("daemon_min_schedule_s") == "60"  # the skipped 10s source is ignored
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)
    assert [s.source_id for s in store.list_source_states()] == ["a", "b"]
    assert log_lines(log) == [
        "warning: source 'mail' skipped: source type 'imap' is not implemented yet"
    ]


def test_skipped_source_is_never_run(
    make_daemon: Callable[..., Daemon], fake: FakeCollector
) -> None:
    d = make_daemon(src("mail", type="imap"), src("a"))
    d.start()
    assert d.tick(at(0)) == ["a"]
    assert fake.called == ["a"]


def test_min_schedule_is_smallest_over_collectable_sources(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    make_daemon(src("a", 900), src("b", 60), src("c", 300)).start()
    assert store.get_meta("daemon_min_schedule_s") == "60"


def test_no_collectable_sources_writes_no_min_schedule_and_warns(
    store: Store, make_daemon: Callable[..., Daemon], log: io.StringIO
) -> None:
    store.set_meta("daemon_min_schedule_s", "60")  # left over from an earlier configuration
    d = make_daemon(src("mail", 10, type="imap"))
    d.start()
    assert store.get_meta("daemon_min_schedule_s") is None
    assert store.get_meta("daemon_pid") == "1001"
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)
    lines = log_lines(log)
    assert len(lines) == 2
    assert lines[0].startswith("warning: source 'mail' skipped:")
    assert lines[1].startswith("warning: no collectable sources")


@pytest.mark.parametrize(
    ("other_pid", "heartbeat_age", "refused"),
    [
        ("2000", 0, True),
        ("2000", 29, True),
        ("2000", 30, False),
        ("2000", 3600, False),
        ("2000", None, False),  # pid left behind without a heartbeat
        ("1001", 5, False),  # our own pid
        ("2000", "garbage", False),
    ],
)
def test_start_refuses_when_another_daemon_has_a_fresh_heartbeat(
    store: Store,
    make_daemon: Callable[..., Daemon],
    other_pid: str,
    heartbeat_age: Any,
    refused: bool,
) -> None:
    store.set_meta("daemon_pid", other_pid)
    if isinstance(heartbeat_age, str):
        store.set_meta("daemon_heartbeat_at", heartbeat_age)
    elif heartbeat_age is not None:
        store.set_meta("daemon_heartbeat_at", stamp(-heartbeat_age))
    d = make_daemon(src("a"))
    if refused:
        with pytest.raises(DaemonError, match="pid 2000"):
            d.start()
        # nothing was written by the refused daemon
        assert store.get_meta("daemon_pid") == "2000"
        assert store.get_meta("daemon_heartbeat_at") == stamp(-heartbeat_age)
        assert store.get_meta("daemon_min_schedule_s") is None
        assert store.list_source_states() == []
    else:
        d.start()
        assert store.get_meta("daemon_pid") == "1001"
        assert store.get_meta("daemon_heartbeat_at") == stamp(0)


def test_second_daemon_is_refused_while_the_first_runs(
    store: Store,
    make_daemon: Callable[..., Daemon],
    clock: Clock,
    sleeper: Sleeper,
) -> None:
    first = make_daemon(src("a"), pid=1001)
    second = make_daemon(src("a"), pid=1002)
    first.start()
    clock.advance(10)
    with pytest.raises(DaemonError):
        second.start()
    assert store.get_meta("daemon_pid") == "1001"
    # after the first one stopped cleanly the second may start
    first.run(once=True)
    assert store.get_meta("daemon_pid") is None
    second.start()
    assert store.get_meta("daemon_pid") == "1002"


def test_refused_run_leaves_the_other_daemons_meta_alone(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    store.set_meta("daemon_pid", "2000")
    store.set_meta("daemon_heartbeat_at", stamp(-5))
    with pytest.raises(DaemonError):
        make_daemon(src("a")).run()
    with pytest.raises(DaemonError):
        make_daemon(src("a")).run(once=True)
    assert store.get_meta("daemon_pid") == "2000"
    assert store.get_meta("daemon_heartbeat_at") == stamp(-5)


def test_tick_before_start_is_an_error(make_daemon: Callable[..., Daemon]) -> None:
    with pytest.raises(DaemonError, match="start"):
        make_daemon(src("a")).tick(at(0))


# -- tick: scheduling ----------------------------------------------------------------------------


def test_tick_runs_never_attempted_sources_in_id_order(
    store: Store, make_daemon: Callable[..., Daemon], fake: FakeCollector
) -> None:
    d = make_daemon(src("b"), src("a"), src("c"))
    d.start()
    assert d.tick(at(0)) == ["a", "b", "c"]
    assert fake.called == ["a", "b", "c"]
    for source_id in "abc":
        assert kinds(store, source_id) == [KIND_BASELINE]


def test_tick_honours_schedules(make_daemon: Callable[..., Daemon], fake: FakeCollector) -> None:
    d = make_daemon(src("a", 60), src("b", 300))
    d.start()
    expected = {
        0: ["a", "b"],
        30: [],
        59: [],
        60: ["a"],
        90: [],
        119: [],
        120: ["a"],
        300: ["a", "b"],  # a is due since 180, b since 300: due order, not just id order
        330: [],
        360: ["a"],
    }
    assert {offset: d.tick(at(offset)) for offset in expected} == expected
    assert fake.called == ["a", "b", "a", "a", "a", "b", "a"]


def test_tick_orders_due_sources_by_due_time_then_id(
    make_daemon: Callable[..., Daemon], fake: FakeCollector
) -> None:
    d = make_daemon(src("a", 600), src("b", 300), src("c", 300))
    d.start()
    assert d.tick(at(0)) == ["a", "b", "c"]
    # b and c are due at 300, a at 600: everything is due at 600, earliest due first
    assert d.tick(at(600)) == ["b", "c", "a"]


def test_failed_attempt_counts_for_the_schedule(
    store: Store, make_daemon: Callable[..., Daemon], fake: FakeCollector
) -> None:
    fake.results["a"] = RuntimeError("boom")
    d = make_daemon(src("a", 60))
    d.start()
    assert d.tick(at(0)) == ["a"]
    assert d.tick(at(59)) == []
    assert d.tick(at(60)) == ["a"]
    assert kinds(store, "a") == [KIND_SOURCE_ERROR]  # deduplicated until recovery


def test_due_time_comes_from_the_stored_state_not_from_memory(
    make_daemon: Callable[..., Daemon], fake: FakeCollector
) -> None:
    first = make_daemon(src("a", 60))
    first.start()
    assert first.tick(at(0)) == ["a"]
    restarted = make_daemon(src("a", 60))  # a new process, same database
    restarted.start()
    assert restarted.tick(at(10)) == []
    assert restarted.tick(at(60)) == ["a"]


def test_heartbeat_is_written_on_every_tick(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    d = make_daemon(src("a", 900))
    d.start()
    d.tick(at(0))
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)
    assert d.tick(at(7)) == []  # nothing due, heartbeat still moves
    assert store.get_meta("daemon_heartbeat_at") == stamp(7)


def _slow_collect(
    clock: Clock, seconds: float, seen: Callable[[], None] | None = None
) -> Callable[[], list[Record]]:
    def collect() -> list[Record]:
        if seen is not None:
            seen()
        clock.advance(seconds)  # a long collection
        return []

    return collect


def test_heartbeat_is_refreshed_with_the_current_time_before_every_source_run(
    store: Store, make_daemon: Callable[..., Daemon], fake: FakeCollector, clock: Clock
) -> None:
    seen: dict[str, str | None] = {}
    for source_id in "abc":
        fake.results[source_id] = _slow_collect(
            clock, 100, lambda sid=source_id: seen.update({sid: store.get_meta(HEARTBEAT)})
        )
    d = make_daemon(src("a"), src("b"), src("c"))
    d.start()

    assert d.tick(at(0)) == ["a", "b", "c"]

    # each source started right after a fresh clock reading, not at the tick's start time
    assert seen == {"a": stamp(0), "b": stamp(100), "c": stamp(200)}


def test_a_second_daemon_is_refused_between_long_collections(
    make_daemon: Callable[..., Daemon], fake: FakeCollector, clock: Clock
) -> None:
    second = make_daemon(src("b"), pid=1002)
    outcomes: list[str] = []

    def second_start_attempt() -> None:
        try:
            second.start()
        except DaemonError:
            outcomes.append("refused")
        else:
            outcomes.append("started")

    fake.results["a"] = _slow_collect(clock, 100)  # takes far longer than the 30s window
    fake.results["b"] = _slow_collect(clock, 0, second_start_attempt)
    d = make_daemon(src("a"), src("b"), pid=1001)
    d.start()

    d.tick(at(0))

    assert outcomes == ["refused"]  # the heartbeat written before b's run is only 0s old


def test_a_failing_heartbeat_write_is_isolated_like_any_store_error(
    store: Store,
    make_daemon: Callable[..., Daemon],
    monkeypatch: pytest.MonkeyPatch,
    log: io.StringIO,
) -> None:
    real_set_meta = store.set_meta
    calls = {"n": 0}

    def flaky(key: str, value: Any) -> None:
        if key == HEARTBEAT:
            calls["n"] += 1
            if calls["n"] == 2:  # 1st = start(); 2nd = before source "a"
                raise sqlite3.OperationalError("database is locked")
        real_set_meta(key, value)

    d = make_daemon(src("a"), src("b"))
    monkeypatch.setattr(store, "set_meta", flaky)
    d.start()

    assert d.tick(at(0)) == ["a", "b"]

    assert kinds(store, "a") == []
    assert kinds(store, "b") == [KIND_BASELINE]
    assert log_lines(log) == [
        f"{stamp(0)} a: run failed: OperationalError: database is locked",
        f"{stamp(0)} b: 1 events",
    ]


def test_a_superseded_run_is_not_logged_and_counts_as_attempted(
    store: Store,
    make_daemon: Callable[..., Daemon],
    fake: FakeCollector,
    clock: Clock,
    log: io.StringIO,
) -> None:
    cfg = src("a", 60)
    newer = FakeCollector(clock)

    def overtaken() -> list[Record]:
        # While this run reads its source, a newer collection (e.g. `since collect`) stores its
        # result; this run's older result must be dropped.
        assert run_collection(store, cfg, newer, at(500)).superseded is False
        return [rec("k", v=1)]

    fake.results["a"] = overtaken
    d = make_daemon(cfg)
    d.start()

    assert d.tick(at(0)) == ["a"]

    assert log_lines(log) == []
    assert kinds(store, "a") == [KIND_BASELINE]  # the newer run's only
    assert store.get_snapshot("a") == {}  # ...not the overtaken run's record
    assert d.tick(at(60)) == []  # the newer run's time counts for the schedule


# -- tick: pruning -------------------------------------------------------------------------------


def test_prune_runs_at_most_hourly(
    store: Store, make_daemon: Callable[..., Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[datetime, datetime]] = []
    real_prune = store.prune

    def spy(before: datetime, now: datetime) -> Any:
        calls.append((before, now))
        return real_prune(before, now)

    monkeypatch.setattr(store, "prune", spy)
    d = make_daemon(src("a", 86400), retention_days=7)
    d.start()
    d.tick(at(0))
    assert calls == [(at(0) - timedelta(days=7), at(0))]  # first tick: never pruned before
    d.tick(at(60))
    d.tick(at(3599))
    assert len(calls) == 1
    d.tick(at(3600))
    assert calls[-1] == (at(3600) - timedelta(days=7), at(3600))
    assert len(calls) == 2
    d.tick(at(3600 + 3599))
    assert len(calls) == 2
    assert store.get_meta("last_pruned_at") == stamp(3600)


def test_prune_deletes_events_older_than_retention(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    old = store.append_event("a", KIND_BASELINE, now=at(0) - timedelta(days=40))
    d = make_daemon(src("a", 86400), retention_days=30)
    d.start()
    d.tick(at(0))
    assert store.get_event(old) is None
    assert kinds(store, "a") == [KIND_BASELINE]  # the fresh baseline survived


def test_unreadable_last_pruned_at_counts_as_due(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    store.set_meta("last_pruned_at", "garbage")
    d = make_daemon(src("a"))
    d.start()
    d.tick(at(0))
    assert store.get_meta("last_pruned_at") == stamp(0)


# -- isolation -----------------------------------------------------------------------------------


def test_a_crashing_collector_does_not_stop_the_others(
    store: Store, make_daemon: Callable[..., Daemon], fake: FakeCollector, log: io.StringIO
) -> None:
    fake.results["a"] = RuntimeError("boom")
    fake.results["b"] = [rec("k")]
    d = make_daemon(src("a"), src("b"), src("c"))
    d.start()
    assert d.tick(at(0)) == ["a", "b", "c"]
    assert kinds(store, "a") == [KIND_SOURCE_ERROR]
    assert kinds(store, "b") == [KIND_BASELINE]
    assert kinds(store, "c") == [KIND_BASELINE]
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)
    assert f"{stamp(0)} a: collection failed: RuntimeError: boom" in log_lines(log)


def _fail_store_for(
    monkeypatch: pytest.MonkeyPatch, bad_id: str, message: str = "database is locked"
) -> list[datetime]:
    """Make run_collection raise a store error for ``bad_id``; returns the attempt times."""
    attempts: list[datetime] = []
    real = daemon_mod.run_collection

    def flaky(store: Store, cfg: SourceConfig, collector: Any, now: datetime) -> Any:
        if cfg.id == bad_id:
            attempts.append(now)
            raise sqlite3.OperationalError(message)
        return real(store, cfg, collector, now)

    monkeypatch.setattr(daemon_mod, "run_collection", flaky)
    return attempts


def test_a_store_error_for_one_source_is_isolated_and_logged_in_one_line(
    store: Store,
    make_daemon: Callable[..., Daemon],
    monkeypatch: pytest.MonkeyPatch,
    log: io.StringIO,
) -> None:
    attempts = _fail_store_for(monkeypatch, "a")
    d = make_daemon(src("a"), src("b"))
    d.start()
    assert d.tick(at(0)) == ["a", "b"]
    assert attempts == [at(0)]
    assert kinds(store, "b") == [KIND_BASELINE]
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)
    assert log_lines(log) == [
        f"{stamp(0)} a: run failed: OperationalError: database is locked",
        f"{stamp(0)} b: 1 events",
    ]


def test_a_source_with_a_store_error_backs_off_by_its_schedule(
    make_daemon: Callable[..., Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = _fail_store_for(monkeypatch, "a")
    d = make_daemon(src("a", 60), src("b", 900))
    d.start()
    d.tick(at(0))
    assert d.tick(at(5)) == []
    assert d.tick(at(59)) == []
    assert d.tick(at(60)) == ["a"]
    assert attempts == [at(0), at(60)]


def test_loop_does_not_spin_on_a_persistent_store_error(
    make_daemon: Callable[..., Daemon],
    monkeypatch: pytest.MonkeyPatch,
    sleeper: Sleeper,
    log: io.StringIO,
) -> None:
    attempts = _fail_store_for(monkeypatch, "a")
    sleeper.stop_after = 4
    assert make_daemon(src("a", 60)).run() == 0
    assert sleeper.calls == [5, 5, 5, 5]  # never 0: the failed source is not "due right now"
    assert attempts == [at(0)]
    assert len(log_lines(log)) == 1


# -- log lines -----------------------------------------------------------------------------------


def test_log_lines_for_events_failures_and_quiet_runs(
    store: Store, make_daemon: Callable[..., Daemon], fake: FakeCollector, log: io.StringIO
) -> None:
    d = make_daemon(src("a", 60))
    d.start()
    fake.results["a"] = [rec("k1", v=1), rec("k2", v=1)]
    d.tick(at(0))  # baseline
    assert log_lines(log) == [f"{stamp(0)} a: 1 events"]

    d.tick(at(60))  # quiet successful run: nothing logged
    assert len(log_lines(log)) == 1

    fake.results["a"] = [rec("k1", v=2), rec("k2", v=1), rec("k3", v=1)]
    d.tick(at(120))
    assert log_lines(log)[-1] == f"{stamp(120)} a: 2 events"

    fake.results["a"] = RuntimeError("boom")
    d.tick(at(180))
    d.tick(at(240))  # second failure: no new event, still reported
    assert log_lines(log)[-2:] == [
        f"{stamp(180)} a: collection failed: RuntimeError: boom",
        f"{stamp(240)} a: collection failed: RuntimeError: boom",
    ]
    assert kinds(store, "a").count(KIND_SOURCE_ERROR) == 1

    fake.results["a"] = [rec("k1", v=2), rec("k2", v=1), rec("k3", v=1)]
    d.tick(at(300))
    assert log_lines(log)[-1] == f"{stamp(300)} a: 1 events"
    assert kinds(store, "a")[-1] == KIND_SOURCE_RECOVERED


def test_failure_messages_are_flattened_to_one_line(
    make_daemon: Callable[..., Daemon], fake: FakeCollector, log: io.StringIO
) -> None:
    fake.results["a"] = RuntimeError("line one\nline two\x1b[31m red\r\n")
    d = make_daemon(src("a"))
    d.start()
    d.tick(at(0))
    assert log_lines(log) == [
        f"{stamp(0)} a: collection failed: RuntimeError: line one line two [31m red"
    ]
    assert "\x1b" not in log.getvalue()


# -- run: once mode ------------------------------------------------------------------------------


def test_once_runs_every_source_regardless_of_due_then_cleans_up(
    store: Store,
    make_daemon: Callable[..., Daemon],
    fake: FakeCollector,
    sleeper: Sleeper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta_writes: list[tuple[str, Any]] = []
    real_set_meta = store.set_meta

    def spy(key: str, value: Any) -> None:
        meta_writes.append((key, value))
        real_set_meta(key, value)

    monkeypatch.setattr(store, "set_meta", spy)
    d = make_daemon(src("b", 86400), src("a", 86400))
    assert d.run(once=True) == 0
    assert fake.called == ["a", "b"]
    assert ("daemon_heartbeat_at", stamp(0)) in meta_writes
    assert store.get_meta("last_pruned_at") == stamp(0)  # prune-if-due ran
    assert sleeper.calls == []
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)  # D16: --once keeps its heartbeat
    assert store.get_meta("daemon_pid") is None  # ...but not the claim on being the running daemon
    for source_id in "ab":
        assert kinds(store, source_id) == [KIND_BASELINE]

    # not due for another day, yet once-mode runs them again
    assert make_daemon(src("b", 86400), src("a", 86400)).run(once=True) == 0
    assert fake.called == ["a", "b", "a", "b"]


def test_once_keeps_the_last_heartbeat_and_a_later_daemon_may_start(
    store: Store, make_daemon: Callable[..., Daemon], clock: Clock
) -> None:
    clock.advance(5)
    assert make_daemon(src("a"), pid=1001).run(once=True) == 0
    assert store.get_meta("daemon_pid") is None
    assert store.get_meta("daemon_heartbeat_at") == stamp(5)

    clock.advance(1)  # well inside the 30s window: the leftover heartbeat does not block a start
    make_daemon(src("a"), pid=1002).start()
    assert store.get_meta("daemon_pid") == "1002"


def test_once_with_an_unexpected_error_still_keeps_only_the_heartbeat(
    store: Store, make_daemon: Callable[..., Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_daemon(src("a"))

    def broken(now: datetime) -> None:
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(d, "_finish_tick", broken)
    with pytest.raises(RuntimeError, match="prune exploded"):
        d.run(once=True)
    assert store.get_meta("daemon_pid") is None
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)


def test_once_does_not_clear_the_pid_of_a_daemon_that_took_over(
    store: Store, make_daemon: Callable[..., Daemon], fake: FakeCollector
) -> None:
    def takeover() -> list[Record]:
        store.set_meta("daemon_pid", "2000")  # another daemon claimed the meta meanwhile
        return []

    fake.results["a"] = takeover
    assert make_daemon(src("a")).run(once=True) == 0
    assert store.get_meta("daemon_pid") == "2000"


def test_once_survives_a_failing_source(
    store: Store, make_daemon: Callable[..., Daemon], fake: FakeCollector
) -> None:
    fake.results["a"] = RuntimeError("boom")
    assert make_daemon(src("a"), src("b")).run(once=True) == 0
    assert kinds(store, "a") == [KIND_SOURCE_ERROR]
    assert kinds(store, "b") == [KIND_BASELINE]


# -- run: loop -----------------------------------------------------------------------------------


def test_loop_ticks_and_sleeps_until_the_next_due_source_capped_at_5s(
    make_daemon: Callable[..., Daemon], fake: FakeCollector, sleeper: Sleeper
) -> None:
    sleeper.stop_after = 5
    assert make_daemon(src("a", 12)).run() == 0
    # ticks at 0, 5, 10, 12, 17; a runs at 0 and 12
    assert sleeper.calls == [5, 5, 2, 5, 5]
    assert [t for _, t in fake.calls] == [at(0), at(12)]


def test_loop_sleep_is_never_negative(
    make_daemon: Callable[..., Daemon], fake: FakeCollector, clock: Clock, sleeper: Sleeper
) -> None:
    def slow() -> list[Record]:
        clock.advance(100)  # the run takes longer than the schedule
        return []

    fake.results["a"] = slow
    sleeper.stop_after = 1
    make_daemon(src("a", 60)).run()
    assert sleeper.calls == [0]


def test_loop_without_collectable_sources_still_heartbeats_and_sleeps_5s(
    store: Store, make_daemon: Callable[..., Daemon], sleeper: Sleeper
) -> None:
    beats: list[str | None] = []
    real_sleep = sleeper.__call__

    def watching_sleep(seconds: float) -> None:
        beats.append(store.get_meta("daemon_heartbeat_at"))
        real_sleep(seconds)

    sleeper.stop_after = 3
    make_daemon(src("mail", type="imap"), sleep_fn=watching_sleep).run()
    assert sleeper.calls == [5, 5, 5]
    assert beats == [stamp(0), stamp(5), stamp(10)]


# -- run: stopping -------------------------------------------------------------------------------


def test_keyboard_interrupt_stops_cleanly(
    store: Store, make_daemon: Callable[..., Daemon], sleeper: Sleeper
) -> None:
    sleeper.stop_after = 2
    assert make_daemon(src("a", 60)).run() == 0
    assert store.get_meta("daemon_heartbeat_at") is None
    assert store.get_meta("daemon_pid") is None
    assert kinds(store, "a") == [KIND_BASELINE]


def test_unexpected_error_propagates_after_cleanup(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    def broken_sleep(seconds: float) -> None:
        raise RuntimeError("sleep exploded")

    with pytest.raises(RuntimeError, match="sleep exploded"):
        make_daemon(src("a"), sleep_fn=broken_sleep).run()
    assert store.get_meta("daemon_heartbeat_at") is None
    assert store.get_meta("daemon_pid") is None


def test_stop_does_not_remove_another_daemons_meta(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    def takeover_then_stop(seconds: float) -> None:
        store.set_meta("daemon_pid", "2000")  # a new daemon claimed the meta meanwhile
        raise KeyboardInterrupt

    assert make_daemon(src("a"), sleep_fn=takeover_then_stop).run() == 0
    assert store.get_meta("daemon_pid") == "2000"
    assert store.get_meta("daemon_heartbeat_at") == stamp(0)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handling is POSIX only")
def test_sigterm_ends_the_loop_cleanly_and_restores_the_handler(
    store: Store, make_daemon: Callable[..., Daemon]
) -> None:
    def terminate(seconds: float) -> None:
        signal.raise_signal(signal.SIGTERM)

    before = signal.getsignal(signal.SIGTERM)
    assert make_daemon(src("a"), sleep_fn=terminate).run() == 0
    assert signal.getsignal(signal.SIGTERM) == before
    assert store.get_meta("daemon_heartbeat_at") is None
    assert store.get_meta("daemon_pid") is None
    assert kinds(store, "a") == [KIND_BASELINE]
