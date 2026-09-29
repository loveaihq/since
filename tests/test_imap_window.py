"""The imap collection window (D20/D21 revised): per-folder starts on ``received``, the cap that
shortens the SINCE date instead of cutting the result, and the runner side of a per-scope
``Window`` (``Window.start_for``). Driven against the in-process fake IMAP server, whose
``SEARCH SINCE`` compares INTERNALDATE's date in the server's own time zone like a real one."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest
from imap_fake import FakeImapServer

import since.sources.imap as imapmod
from since.collect import CollectResult, run_collection
from since.config import SourceConfig
from since.model import KIND_ADDED, KIND_BASELINE, KIND_REMOVED, KIND_SOURCE_ERROR, Record
from since.service import Service
from since.sources import CollectOutput, Window
from since.sources.imap import ImapCollector
from since.store import Store

NOW = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)  # since_days 14: SINCE 15-Sep, start 16-Sep
ENV = "SINCE_TEST_IMAP_PW"
ALLOWED_COMMANDS = {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}


def at(day: int, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    """A moment on ``day`` of September 2026 (UTC); days past 30 roll into October."""
    return datetime(2026, 9, 1, hour, minute, second, tzinfo=UTC) + timedelta(days=day - 1)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    """Injectable ``now_fn``; the same clock feeds the collector and ``run_collection``."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeImapServer]:
    with FakeImapServer() as fake:
        monkeypatch.setenv(ENV, fake.password)
        yield fake


def make_cfg(server: FakeImapServer, **overrides: Any) -> SourceConfig:
    options: dict[str, Any] = {
        "host": server.host,
        "port": server.port,
        "security": "none",
        "username": server.username,
        "password_env": ENV,
    }
    options.update(overrides)
    return SourceConfig(id="inbox", type="imap", priority="normal", schedule_s=900, options=options)


def run(store: Store, cfg: SourceConfig, clock: Clock) -> CollectResult:
    result = run_collection(store, cfg, ImapCollector(now_fn=clock), clock.now)
    assert result.error is None, result.error
    return result


def collect(cfg: SourceConfig, clock: Clock) -> CollectOutput:
    return ImapCollector(now_fn=clock).collect(cfg)


def kinds(store: Store) -> list[tuple[str, str | None]]:
    return [(e.kind, e.record_key) for e in store.events_after(0, "inbox")]


def key(uid: int) -> str:
    """The Message-ID the fake gives a mail without an explicit one."""
    return f"<m{uid}@example.test>"


def uid_lists(server: FakeImapServer) -> list[list[int]]:
    """The UIDs of every UID FETCH the server received."""
    sets = [c.split()[2] for c in server.commands_named("UID FETCH")]
    return [[int(u) for u in s.split(",")] for s in sets]


# -- Window.start_for ----------------------------------------------------------------------------

SCOPED = Window(
    "received",
    "2026-09-16T00:00:00Z",
    scope_field="folder",
    starts={"Busy": "2026-09-27T00:00:00Z", "Quiet": "2026-09-17T00:00:00Z"},
)


def test_a_window_without_a_scope_has_one_start() -> None:
    window = Window("date", "2026-09-20T00:00:00Z")
    assert (window.scope_field, dict(window.starts)) == (None, {})
    assert window.start_for({"date": "2026-09-01T00:00:00Z", "folder": "Busy"}) == window.start
    assert window.start_for({}) == window.start


def test_start_for_uses_the_start_of_the_records_scope_value() -> None:
    assert SCOPED.start_for({"folder": "Busy"}) == "2026-09-27T00:00:00Z"
    assert SCOPED.start_for({"folder": "Quiet", "received": "2026-09-01T00:00:00Z"}) == (
        "2026-09-17T00:00:00Z"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"folder": "Gone"},  # a scope value without an own start
        {"folder": "busy"},  # scope values are compared exactly
        {"received": "2026-09-01T00:00:00Z"},  # no scope field on the record
        {"folder": None},
        {"folder": 5},
        {"folder": True},
        {},
    ],
)
def test_start_for_falls_back_to_the_general_start(fields: dict[str, Any]) -> None:
    assert SCOPED.start_for(fields) == "2026-09-16T00:00:00Z"


def test_starts_without_a_scope_field_are_ignored_by_start_for() -> None:
    window = Window("date", "2026-09-20T00:00:00Z", starts={"Busy": "2026-09-27T00:00:00Z"})
    assert window.start_for({"folder": "Busy"}) == "2026-09-20T00:00:00Z"


def test_window_stays_a_frozen_value_with_unshared_defaults() -> None:
    with pytest.raises(AttributeError):
        SCOPED.start = "x"  # type: ignore[misc]
    assert Window("date", "2026-09-20T00:00:00Z") == Window("date", "2026-09-20T00:00:00Z")
    assert SCOPED != Window("received", "2026-09-16T00:00:00Z", scope_field="folder")
    same = Window("date", "2026-09-20T00:00:00Z")
    assert hash(same) == hash(Window("date", "2026-09-20T00:00:00Z"))
    assert Window("a", "x").starts is not Window("a", "x").starts


# -- the runner with a per-scope window ----------------------------------------------------------


class WindowCollector:
    """Returns the queued outputs, one per ``collect`` call."""

    type_name = "dir"

    def __init__(self) -> None:
        self.outputs: list[CollectOutput] = []

    def validate(self, cfg: SourceConfig) -> None:
        pass

    def key_label(self, cfg: SourceConfig) -> str:
        return ""

    def collect(self, cfg: SourceConfig) -> CollectOutput:
        return self.outputs.pop(0)


def scoped_record(name: str, folder: str, received: str) -> Record:
    return Record.make(name, {"folder": folder, "received": received})


def run_window(
    store: Store, collector: WindowCollector, output: CollectOutput, minute: int
) -> CollectResult:
    collector.outputs.append(output)
    cfg = SourceConfig(id="src", type="dir", priority="normal", schedule_s=900)
    return run_collection(store, cfg, collector, NOW + timedelta(minutes=minute))


def src_kinds(store: Store) -> list[tuple[str, str | None]]:
    return [(e.kind, e.record_key) for e in store.events_after(0, "src")]


def test_the_runner_ages_records_out_by_the_start_of_their_scope(store: Store) -> None:
    collector = WindowCollector()
    old = "2026-09-20T12:00:00Z"
    run_window(
        store,
        collector,
        CollectOutput(
            [
                scoped_record("busy-old", "Busy", old),
                scoped_record("quiet-old", "Quiet", old),
                scoped_record("free-old", "Elsewhere", old),
                scoped_record("keep", "Busy", "2026-09-28T12:00:00Z"),
            ]
        ),
        0,
    )
    # the three mails dated 20-Sep are absent now. Busy's start (27-Sep) and the general start
    # (21-Sep, which "Elsewhere" falls back to) are after them: aged out, no event. Quiet's own
    # start (17-Sep) is before it: a real removal.
    window = Window(
        "received",
        "2026-09-21T00:00:00Z",
        scope_field="folder",
        starts={"Busy": "2026-09-27T00:00:00Z", "Quiet": "2026-09-17T00:00:00Z"},
    )
    keep = [scoped_record("keep", "Busy", "2026-09-28T12:00:00Z")]
    result = run_window(store, collector, CollectOutput(keep, window=window), 5)
    assert result.error is None
    assert src_kinds(store) == [(KIND_BASELINE, None), (KIND_REMOVED, "quiet-old")]
    assert sorted(store.get_snapshot("src")) == ["keep"]


def test_a_scope_start_can_be_earlier_than_the_general_start(store: Store) -> None:
    collector = WindowCollector()
    old = scoped_record("old", "Quiet", "2026-09-20T12:00:00Z")
    run_window(store, collector, CollectOutput([old]), 0)
    window = Window(
        "received",
        "2026-09-25T00:00:00Z",
        scope_field="folder",
        starts={"Quiet": "2026-09-17T00:00:00Z"},
    )
    run_window(store, collector, CollectOutput([], window=window), 5)
    assert src_kinds(store) == [(KIND_BASELINE, None), (KIND_REMOVED, "old")]


INVALID_SCOPED_WINDOWS: list[tuple[str, Any, str]] = [
    (
        "empty-scope-field",
        Window("received", "2026-09-20T00:00:00Z", scope_field=""),
        "invalid window: scope_field must be None or a non-empty string",
    ),
    (
        "int-scope-field",
        Window("received", "2026-09-20T00:00:00Z", scope_field=5),  # type: ignore[arg-type]
        "invalid window: scope_field must be None or a non-empty string",
    ),
    (
        "list-starts",
        Window("received", "2026-09-20T00:00:00Z", scope_field="folder", starts=[]),  # type: ignore[arg-type]
        "invalid window: starts must be a mapping of scope value to timestamp",
    ),
    (
        "starts-without-scope-field",
        Window("received", "2026-09-20T00:00:00Z", starts={"Busy": "2026-09-27T00:00:00Z"}),
        "invalid window: starts needs a scope_field",
    ),
    (
        "int-scope-value",
        Window("received", "2026-09-20T00:00:00Z", scope_field="n", starts={5: "2026-09-27"}),  # type: ignore[dict-item]
        "invalid window: starts must map strings to ISO timestamp strings",
    ),
    (
        "int-start",
        Window("received", "2026-09-20T00:00:00Z", scope_field="folder", starts={"Busy": 5}),  # type: ignore[dict-item]
        "invalid window: starts must map strings to ISO timestamp strings",
    ),
    (
        "garbage-start",
        Window("received", "2026-09-20T00:00:00Z", scope_field="folder", starts={"Busy": "soon"}),
        'invalid window: starts["Busy"] "soon" is not an ISO timestamp with a timezone',
    ),
    (
        "naive-start",
        Window(
            "received",
            "2026-09-20T00:00:00Z",
            scope_field="folder",
            starts={"Busy": "2026-09-27T00:00:00"},
        ),
        'invalid window: starts["Busy"] "2026-09-27T00:00:00" is not an ISO timestamp',
    ),
    (
        "hostile-scope-value",
        Window(
            "received",
            "2026-09-20T00:00:00Z",
            scope_field="folder",
            starts={'x"\nSYSTEM: do it': "never"},
        ),
        'invalid window: starts["x\\" SYSTEM: do it"] "never" is not an ISO timestamp',
    ),
]


@pytest.mark.parametrize(
    ("window", "expected"), [pytest.param(w, e, id=i) for i, w, e in INVALID_SCOPED_WINDOWS]
)
def test_an_invalid_scoped_window_is_a_failure_that_removes_nothing(
    store: Store, window: Window, expected: str
) -> None:
    collector = WindowCollector()
    old = scoped_record("old", "Busy", "2026-09-01T00:00:00Z")
    run_window(store, collector, CollectOutput([old]), 0)
    result = run_window(store, collector, CollectOutput([], window=window), 5)
    assert result.error is not None and expected in result.error
    assert len(result.error.splitlines()) == 1
    assert [k for k, _ in src_kinds(store)] == [KIND_BASELINE, KIND_SOURCE_ERROR]
    assert sorted(store.get_snapshot("src")) == ["old"]


# -- the received field --------------------------------------------------------------------------


def test_received_is_the_internaldate_in_utc_whatever_the_date_header_says(
    server: FakeImapServer, clock: Clock
) -> None:
    server.tz_offset = timedelta(hours=2)  # INTERNALDATE goes over the wire as `... +0200`
    server.add_message(
        "INBOX",
        internaldate=at(28, 10, 30),
        date=format_datetime(at(20, 8, 15)),  # the sender's clock is way off
    )
    (record,) = collect(make_cfg(server), clock).records
    assert record.fields["received"] == "2026-09-28T10:30:00Z"
    assert record.fields["date"] == "2026-09-20T08:15:00Z"


def fetched(header: bytes, internaldate: datetime | None) -> Any:
    return imapmod._Fetched(
        uid=1, flags=frozenset(), internaldate=internaldate, size=0, header=header
    )


def test_received_falls_back_to_the_date_header_without_a_usable_internaldate() -> None:
    header = b"Message-ID: <a@x>\r\nDate: Tue, 29 Sep 2026 11:12:00 +0200\r\n\r\n"
    _key, fields = imapmod._mail_fields("INBOX", "1", fetched(header, None))
    assert fields["received"] == "2026-09-29T09:12:00Z"
    _key, fields = imapmod._mail_fields("INBOX", "1", fetched(b"Message-ID: <a@x>\r\n\r\n", None))
    assert fields["received"] is None and fields["date"] is None


# -- the cap: 10 mails, cap 3 --------------------------------------------------------------------


def test_the_cap_bisects_the_number_of_days_instead_of_cutting_the_result(
    server: FakeImapServer, clock: Clock
) -> None:
    # 10 mails, one per day at noon: 19-Sep (Mail number 1) .. 28-Sep (Mail number 10)
    server.add_messages("INBOX", [at(18 + n) for n in range(1, 11)])
    out = collect(make_cfg(server, max_messages=3), clock)
    # the days tried: 14, then 7 (7 mails: too many), 3 (3: fits), 5 (too many), 4 (too many)
    assert server.commands_named("UID SEARCH") == [
        f"UID SEARCH SINCE {d}-Sep-2026" for d in (15, 22, 26, 24, 25)
    ]
    assert [r.key for r in out.records] == sorted(key(u) for u in (8, 9, 10))
    assert uid_lists(server) == [[8, 9, 10]]
    # the folder claims what it covers: SINCE 26-Sep, plus a day of margin
    assert out.window == Window(
        "received",
        "2026-09-16T00:00:00Z",
        scope_field="folder",
        starts={"INBOX": "2026-09-27T00:00:00Z"},
    )
    assert set(server.command_names()) <= ALLOWED_COMMANDS and server.bad_commands == []


def test_a_folder_within_the_cap_takes_a_single_search(
    server: FakeImapServer, clock: Clock
) -> None:
    server.add_messages("INBOX", [at(18 + n) for n in range(1, 11)])
    out = collect(make_cfg(server, max_messages=10), clock)
    assert server.commands_named("UID SEARCH") == ["UID SEARCH SINCE 15-Sep-2026"]
    assert len(out.records) == 10
    assert out.window.starts == {"INBOX": "2026-09-16T00:00:00Z"}


def test_the_number_of_days_is_found_for_any_cap(server: FakeImapServer, clock: Clock) -> None:
    server.add_messages("INBOX", [at(18 + n) for n in range(1, 11)])  # one per day, 19..28-Sep
    for cap in range(1, 10):
        out = collect(make_cfg(server, max_messages=cap), clock)
        # the largest day count that fits: SINCE 28-Sep has 1 mail, SINCE 27-Sep has 2, ...
        assert len(out.records) == cap, cap
        assert out.window.starts == {"INBOX": iso(at(30 - cap, 0))}  # SINCE (29 - cap) + a day


def test_a_cap_that_even_one_day_exceeds_keeps_the_newest_uids(
    server: FakeImapServer, clock: Clock
) -> None:
    # six mails on 28-Sep, ten minutes apart, cap 3
    server.add_messages("INBOX", [at(28, 10, 10 * i) for i in range(6)])
    out = collect(make_cfg(server, max_messages=3), clock)
    assert [r.key for r in out.records] == sorted(key(u) for u in (4, 5, 6))
    assert uid_lists(server) == [[4, 5, 6]]
    # bisected down to one day (14, 7, 3, 1), which is still too many
    assert server.commands_named("UID SEARCH")[-1] == "UID SEARCH SINCE 28-Sep-2026"
    assert len(server.commands_named("UID SEARCH")) == 4
    # the start is the oldest kept mail plus a day
    assert out.window.starts == {"INBOX": "2026-09-29T10:30:00Z"}


def test_since_days_one_over_the_cap_takes_a_single_search(
    server: FakeImapServer, clock: Clock
) -> None:
    server.add_messages("INBOX", [at(28, 10, 10 * i) for i in range(6)])
    out = collect(make_cfg(server, since_days=1, max_messages=2), clock)
    assert server.commands_named("UID SEARCH") == ["UID SEARCH SINCE 28-Sep-2026"]
    assert len(out.records) == 2
    assert out.window.starts == {"INBOX": "2026-09-29T10:40:00Z"}


def test_a_new_mail_pushes_the_oldest_out_without_removed_even_when_one_day_is_over_the_cap(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_messages("INBOX", [at(28, 10, 10 * i) for i in range(6)])
    cfg = make_cfg(server, max_messages=3)
    run(store, cfg, clock)
    clock.advance(minutes=30)
    server.add_message("INBOX", subject="Newest", internaldate=at(29, 8))
    run(store, cfg, clock)
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_ADDED, key(7))]
    assert sorted(store.get_snapshot("inbox")) == sorted(key(u) for u in (5, 6, 7))


def test_new_mail_over_the_cap_never_removes_the_mail_it_pushes_out(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    # QA issue 1: with cap 3 every new mail produced `- "Mail number 1" ... removed`
    server.add_messages("INBOX", [at(18 + n) for n in range(1, 11)])
    cfg = make_cfg(server, max_messages=3)
    run(store, cfg, clock)
    assert sorted(store.get_snapshot("inbox")) == sorted(key(u) for u in (8, 9, 10))
    for n in range(11, 16):  # a mail a day
        clock.advance(days=1)
        server.add_message(
            "INBOX", subject=f"Mail number {n}", internaldate=clock.now - timedelta(hours=1)
        )
        run(store, cfg, clock)
        state = store.get_source_state("inbox")
        assert state is not None and state.record_count == 3
    assert kinds(store) == [(KIND_BASELINE, None)] + [(KIND_ADDED, key(n)) for n in range(11, 16)]
    assert sorted(store.get_snapshot("inbox")) == sorted(key(u) for u in (13, 14, 15))
    digest = Service(store, clock).since()
    assert not [line for line in digest.splitlines() if line.startswith("  -")], digest


# -- the server's time zone (QA issue 5) ---------------------------------------------------------


def test_an_evening_mail_on_a_server_west_of_utc_ages_out_without_removed(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.tz_offset = timedelta(hours=-7)
    # 16-Sep 03:00 UTC is 15-Sep 20:00 on the server: its SINCE 15-Sep finds it, its SINCE 16-Sep
    # no longer does, although 00:00 UTC of 16-Sep is before the mail
    server.add_message("INBOX", subject="Evening", internaldate=at(16, 3, 0))
    server.add_message("INBOX", subject="Recent", internaldate=at(28, 12))
    cfg = make_cfg(server)
    run(store, cfg, clock)
    assert store.get_snapshot("inbox")[key(1)].fields["received"] == "2026-09-16T03:00:00Z"
    clock.advance(days=1)
    run(store, cfg, clock)
    assert server.commands_named("UID SEARCH")[-1] == "UID SEARCH SINCE 16-Sep-2026"
    assert kinds(store) == [(KIND_BASELINE, None)]
    assert sorted(store.get_snapshot("inbox")) == [key(2)]


@pytest.mark.parametrize("hour", [0, 23])  # collecting just after / just before midnight UTC
@pytest.mark.parametrize("offset_minutes", [-720, -420, 0, 330, 840])
def test_no_server_time_zone_turns_ageing_into_removed(
    server: FakeImapServer, store: Store, offset_minutes: int, hour: int
) -> None:
    server.tz_offset = timedelta(minutes=offset_minutes)
    first = datetime(2026, 9, 20, hour, 10, tzinfo=UTC)
    # a mail every five hours for ten days, ending at the first collection
    server.add_messages("INBOX", [first - timedelta(hours=5 * i) for i in range(48)])
    cfg = make_cfg(server, since_days=3)
    clock = Clock(first)
    run(store, cfg, clock)
    first_count = len(store.get_snapshot("inbox"))
    for _ in range(9):  # nothing is deleted at the source: the mails only age out
        clock.advance(days=1)
        run(store, cfg, clock)
    assert [k for k, _ in kinds(store)] == [KIND_BASELINE]
    assert 0 < first_count < 48
    assert store.get_snapshot("inbox") == {}


# -- Date header versus received -----------------------------------------------------------------


def test_a_date_header_ahead_of_received_does_not_turn_ageing_into_removed(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message(
        "INBOX", subject="Skewed", internaldate=at(16, 12), date=format_datetime(at(25, 12))
    )
    server.add_message("INBOX", subject="Recent", internaldate=at(28, 12))
    cfg = make_cfg(server)
    run(store, cfg, clock)
    assert store.get_snapshot("inbox")[key(1)].fields["date"] == "2026-09-25T12:00:00Z"
    clock.advance(days=2)  # SINCE 17-Sep: the server drops it; its start is 18-Sep
    run(store, cfg, clock)
    assert kinds(store) == [(KIND_BASELINE, None)]
    assert sorted(store.get_snapshot("inbox")) == [key(2)]


def test_a_date_header_behind_received_does_not_hide_a_real_deletion(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message(
        "INBOX", subject="Skewed", internaldate=at(25, 12), date=format_datetime(at(10, 12))
    )
    cfg = make_cfg(server)
    run(store, cfg, clock)
    assert key(1) in store.get_snapshot("inbox")  # the server filters on INTERNALDATE: collected
    server.delete_message("INBOX", 1)
    clock.advance(minutes=15)
    run(store, cfg, clock)
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_REMOVED, key(1))]


# -- real deletions ------------------------------------------------------------------------------


def test_a_deleted_mail_inside_the_window_is_still_removed(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Keep", internaldate=at(28, 12))
    server.add_message("INBOX", subject="Gone", internaldate=at(20, 12))
    cfg = make_cfg(server)
    run(store, cfg, clock)
    server.delete_message("INBOX", 2)
    clock.advance(minutes=15)
    run(store, cfg, clock)
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_REMOVED, key(2))]
    removed = store.events_after(0, "inbox")[-1]
    assert removed.detail["title"][0] == ["subject", "Gone"]


@pytest.mark.parametrize(
    ("received", "removed"),
    [
        (at(15, 12), False),  # on the SINCE day: inside the margin, ages out silently
        (at(15, 23, 59, 59), False),
        (at(16, 0, 0, 0), True),  # from the window start on a deletion is reported
        (at(16, 0, 0, 1), True),
    ],
)
def test_the_window_starts_a_day_after_the_since_day(
    server: FakeImapServer, store: Store, clock: Clock, received: datetime, removed: bool
) -> None:
    server.add_message("INBOX", subject="Keep", internaldate=at(28, 12))
    server.add_message("INBOX", subject="Edge", internaldate=received)
    cfg = make_cfg(server)
    run(store, cfg, clock)
    assert sorted(store.get_snapshot("inbox")) == [key(1), key(2)]
    server.delete_message("INBOX", 2)
    clock.advance(minutes=15)
    run(store, cfg, clock)
    expected = [(KIND_BASELINE, None)] + ([(KIND_REMOVED, key(2))] if removed else [])
    assert kinds(store) == expected


# -- one start per folder ------------------------------------------------------------------------


def test_each_folder_has_the_start_its_own_volume_allows(
    server: FakeImapServer, clock: Clock
) -> None:
    server.add_messages("Busy", [at(18 + n) for n in range(1, 11)])  # one a day, 19..28-Sep
    server.add_message("Quiet", message_id="<q1@example.test>", internaldate=at(20, 12))
    server.add_message("Quiet", message_id="<q2@example.test>", internaldate=at(27, 12))
    out = collect(make_cfg(server, folders=["Busy", "Quiet"], max_messages=3), clock)
    assert out.window == Window(
        "received",
        "2026-09-16T00:00:00Z",  # the start of since_days: for records of no configured folder
        scope_field="folder",
        starts={"Busy": "2026-09-27T00:00:00Z", "Quiet": "2026-09-16T00:00:00Z"},
    )
    by_folder: dict[str, int] = {}
    for record in out.records:
        folder = str(record.fields["folder"])
        by_folder[folder] = by_folder.get(folder, 0) + 1
    assert by_folder == {"Busy": 3, "Quiet": 2}


def test_a_quiet_folder_still_reports_a_deletion_the_busy_folder_would_not(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_messages("Busy", [at(18 + n) for n in range(1, 11)])
    server.add_message("Quiet", message_id="<q1@example.test>", internaldate=at(20, 12))
    server.add_message("Quiet", message_id="<q2@example.test>", internaldate=at(27, 12))
    cfg = make_cfg(server, folders=["Busy", "Quiet"], max_messages=3)
    run(store, cfg, clock)
    assert len(store.get_snapshot("inbox")) == 5  # Busy's newest three, both of Quiet's
    clock.advance(days=1)
    server.add_message("Busy", subject="Fresh", internaldate=clock.now - timedelta(hours=1))
    # its mail q1 was received on 20-Sep: before Busy's new start, after Quiet's
    server.delete_message("Quiet", 1)
    run(store, cfg, clock)
    # Busy's mail number 8 (26-Sep) is beyond Busy's shortened range and left without an event;
    # the deleted Quiet mail is inside Quiet's range and is a real removal
    assert kinds(store)[0] == (KIND_BASELINE, None)
    assert sorted(kinds(store)[1:]) == sorted(
        [(KIND_ADDED, key(11)), (KIND_REMOVED, "<q1@example.test>")]
    )
    assert sorted(store.get_snapshot("inbox")) == sorted(
        [key(9), key(10), key(11), "<q2@example.test>"]
    )


def test_a_folder_that_is_no_longer_configured_uses_the_general_start(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Stays", internaldate=at(28, 12))
    server.add_message("Old", message_id="<o1@example.test>", internaldate=at(20, 12))
    run(store, make_cfg(server, folders=["INBOX", "Old"]), clock)
    clock.advance(minutes=15)
    run(store, make_cfg(server), clock)  # the folder left the config: its mail is gone
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_REMOVED, "<o1@example.test>")]
