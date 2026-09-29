"""``imap`` source tests, driven through ``run_collection`` against the in-process fake IMAP server
(``tests/imap_fake.py``, security ``none`` on 127.0.0.1). The ssl / starttls paths are unit-tested
by replacing the ``imaplib`` classes."""

from __future__ import annotations

import imaplib
import json
import ssl
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from imap_fake import FakeImapServer, decode_mutf7

import since.sources.imap as imapmod
from since.collect import CollectResult, register_sources, run_collection
from since.config import Config, ConfigError, SourceConfig
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
)
from since.service import Service
from since.sources import CollectError, Window, title_fields_for
from since.sources.imap import ImapCollector, mutf7_encode
from since.store import Store

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)  # SINCE day with 14 days: 2026-09-15
ENV = "SINCE_TEST_IMAP_PW"
SEEN = "\\Seen"
FLAGGED = "\\Flagged"
ANSWERED = "\\Answered"
# The exception classes as imaplib defines them, captured before tests replace ``imaplib.IMAP4``.
IMAP_ERROR = imaplib.IMAP4.error
IMAP_ABORT = imaplib.IMAP4.abort
ALLOWED_COMMANDS = {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}


class Clock:
    """Injectable ``now_fn``; the same clock feeds the collector and ``run_collection``."""

    def __init__(self, now: datetime = T0) -> None:
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
    return run_collection(store, cfg, ImapCollector(now_fn=clock), clock.now)


def collect(cfg: SourceConfig, clock: Clock | None = None) -> Any:
    return ImapCollector(now_fn=clock or Clock()).collect(cfg)


def events(store: Store) -> list[Any]:
    return store.events_after(0, "inbox")


def kinds(store: Store) -> list[tuple[str, str | None]]:
    return [(e.kind, e.record_key) for e in events(store)]


def dump_state(store: Store, clock: Clock) -> str:
    """Everything Since stored about the source, as text (for "the password is nowhere")."""
    parts = [
        repr(events(store)),
        repr(store.list_source_states()),
        repr(store.get_snapshot("inbox")),
    ]
    parts.append(Service(store, clock).since())
    return "\n".join(parts)


# -- modified UTF-7 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("INBOX", "INBOX"),
        ("台北", "&U,BTFw-"),  # RFC 3501 section 5.1.3
        ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),  # RFC 3501 section 5.1.3
        ("Sent & Archive", "Sent &- Archive"),
        ("a台b", "a&U,A-b"),
        ("😀", "&2D3eAA-"),  # outside the BMP: a surrogate pair
        ("Entwürfe", "Entw&APw-rfe"),
        ("[Gmail]/已发邮件", "[Gmail]/&XfJT0ZCuTvY-"),
        ("", ""),
    ],
)
def test_mutf7_known_vectors(name: str, encoded: str) -> None:
    assert mutf7_encode(name) == encoded


@pytest.mark.parametrize("name", ["台北", "Ünïcödé/子/&", "a\nb", "\U0001f600 x", "&-&", "x" * 300])
def test_mutf7_round_trips_through_an_independent_decoder(name: str) -> None:
    encoded = mutf7_encode(name)
    assert all(0x20 <= ord(c) <= 0x7E for c in encoded)  # only printable ASCII is ever sent
    assert decode_mutf7(encoded) == name


# -- validate / key_label / titles ---------------------------------------------------------------


def opts(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "host": "imap.example.com",
        "username": "me@example.com",
        "password_env": ENV,
    }
    options.update(overrides)
    return options


def cfg_with(**overrides: Any) -> SourceConfig:
    return SourceConfig(id="inbox", type="imap", options=opts(**overrides))


def test_validate_accepts_minimal_and_full_config_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV, raising=False)  # no env var, no server: validate must not care
    ImapCollector().validate(cfg_with())
    ImapCollector().validate(
        cfg_with(
            port=1143,
            security="starttls",
            folders=["INBOX", "[Gmail]/已发邮件"],
            since_days=365,
            max_messages=5000,
        )
    )


def test_defaults_port_folders_window_and_cap() -> None:
    assert imapmod._settings(cfg_with()) == imapmod._Settings(
        host="imap.example.com",
        port=993,
        security="ssl",
        username="me@example.com",
        password_env=ENV,
        folders=["INBOX"],
        since_days=14,
        max_messages=500,
    )
    assert imapmod._settings(cfg_with(security="starttls")).port == 143
    assert imapmod._settings(cfg_with(host="localhost", security="none")).port == 143
    assert imapmod._settings(cfg_with(security="ssl", port=1993)).port == 1993


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"host": None}, "key 'host'"),
        ({"host": ""}, "key 'host'"),
        ({"host": 5}, "key 'host'"),
        ({"host": "imap example.com"}, "key 'host'"),
        ({"port": 0}, "key 'port'"),
        ({"port": 70000}, "key 'port'"),
        ({"port": "993"}, "key 'port'"),
        ({"port": True}, "key 'port'"),
        ({"security": "tls"}, "key 'security'"),
        ({"security": None}, "key 'security'"),
        ({"security": "none"}, "key 'security'"),  # not local
        ({"security": "none", "host": "127.0.0.2"}, "key 'security'"),
        ({"security": "none", "host": "localhost.example.com"}, "key 'security'"),
        ({"username": ""}, "key 'username'"),
        ({"username": 7}, "key 'username'"),
        ({"username": "jörg@example.com"}, "key 'username'"),
        ({"username": "a\r\nb"}, "key 'username'"),
        ({"password_env": ""}, "key 'password_env'"),
        ({"password_env": None}, "key 'password_env'"),
        ({"folders": []}, "key 'folders'"),
        ({"folders": "INBOX"}, "key 'folders'"),
        ({"folders": ["INBOX", ""]}, "key 'folders'"),
        ({"folders": ["INBOX", 3]}, "key 'folders'"),
        ({"folders": ["bad\ud800name"]}, "key 'folders'"),
        ({"since_days": 0}, "key 'since_days'"),
        ({"since_days": 366}, "key 'since_days'"),
        ({"since_days": 1.5}, "key 'since_days'"),
        ({"since_days": "14"}, "key 'since_days'"),
        ({"max_messages": 0}, "key 'max_messages'"),
        ({"max_messages": 5001}, "key 'max_messages'"),
        ({"max_messages": False}, "key 'max_messages'"),
        ({"timeout": 5}, "key 'timeout'"),
        ({"password": "x"}, "key 'password'"),
    ],
)
def test_validate_rejects_bad_options(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ConfigError) as info:
        ImapCollector().validate(cfg_with(**overrides))
    assert "source 'inbox'" in str(info.value)
    assert fragment in str(info.value)


def test_validate_reports_a_missing_required_key() -> None:
    options = opts()
    del options["password_env"]
    with pytest.raises(ConfigError, match="key 'password_env'"):
        ImapCollector().validate(SourceConfig(id="inbox", type="imap", options=options))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "LocalHost"])
def test_security_none_is_allowed_for_local_hosts_only(host: str) -> None:
    ImapCollector().validate(cfg_with(host=host, security="none"))


def test_key_label_and_default_title_fields() -> None:
    collector = ImapCollector()
    assert collector.type_name == "imap"
    assert collector.key_label(cfg_with()) == ""
    assert collector.default_title_fields(cfg_with()) == ["subject", "from"]
    assert title_fields_for(cfg_with(), collector) == ["subject", "from"]
    configured = SourceConfig(id="inbox", type="imap", title_fields=["subject"], options=opts())
    assert title_fields_for(configured, collector) == ["subject"]


def test_register_sources_stores_the_empty_key_label(store: Store) -> None:
    register_sources(store, Config(sources=[cfg_with()]), {"imap": ImapCollector()})
    state = store.get_source_state("inbox")
    assert state is not None and state.key_label == ""


def test_collect_reports_invalid_options_as_a_source_error(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    result = run(store, make_cfg(server, since_days=0), clock)
    assert result.error is not None and "key 'since_days'" in result.error
    assert server.connections == 0


# -- SINCE date and window -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("month", "name"),
    list(
        enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
            start=1,
        )
    ),
)
def test_imap_date_uses_english_month_names(month: int, name: str) -> None:
    assert imapmod._imap_date(datetime(2026, month, 5, tzinfo=UTC)) == f"05-{name}-2026"


def test_since_day_and_window_start_are_utc_midnights() -> None:
    start = imapmod._since_start(datetime(2026, 9, 29, 23, 59, tzinfo=UTC), 14)
    assert start == datetime(2026, 9, 15, tzinfo=UTC)
    # a non-UTC clock is converted first: 2026-09-30 01:00+02:00 is still 2026-09-29 in UTC
    plus2 = datetime(2026, 9, 30, 1, 0, tzinfo=timezone_plus(2))
    assert imapmod._since_start(plus2, 1) == datetime(2026, 9, 28, tzinfo=UTC)


def timezone_plus(hours: int) -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=hours))


def test_collect_returns_the_window_and_searches_since_that_day(
    server: FakeImapServer, clock: Clock
) -> None:
    server.add_message("INBOX")
    out = collect(make_cfg(server), clock)
    assert out.window == Window("date", "2026-09-15T00:00:00Z")
    assert server.commands_named("UID SEARCH") == ["UID SEARCH SINCE 15-Sep-2026"]
    out = collect(make_cfg(server, since_days=1), clock)
    assert out.window == Window("date", "2026-09-28T00:00:00Z")
    assert server.commands_named("UID SEARCH")[-1] == "UID SEARCH SINCE 28-Sep-2026"


def test_default_clock_is_the_real_utc_clock(server: FakeImapServer) -> None:
    server.add_message("INBOX", internaldate=datetime.now(UTC))
    out = ImapCollector().collect(make_cfg(server, since_days=1))
    assert len(out.records) == 1
    start = datetime.fromisoformat(out.window.start.replace("Z", "+00:00"))
    assert timedelta(days=1) <= datetime.now(UTC) - start < timedelta(days=2, seconds=1)


# -- baseline and diffs through run_collection ---------------------------------------------------


def test_first_run_is_a_baseline(server: FakeImapServer, store: Store, clock: Clock) -> None:
    server.add_message("INBOX", subject="One")
    server.add_message("INBOX", subject="Two", flags=(SEEN, ANSWERED))
    result = run(store, make_cfg(server), clock)
    assert result.error is None
    assert [e.kind for e in events(store)] == [KIND_BASELINE]
    assert events(store)[0].detail == {"record_count": 2}
    snapshot = store.get_snapshot("inbox")
    assert sorted(snapshot) == ["<m1@example.test>", "<m2@example.test>"]
    assert snapshot["<m2@example.test>"].fields == {
        "subject": "Two",
        "from": "Alice <alice@example.test>",
        "to": "bob@example.test",
        "date": "2026-09-28T12:00:00Z",
        "folder": "INBOX",
        "seen": True,
        "flagged": False,
        "answered": True,
        "size": server.find("INBOX", 2).rfc822_size,
    }
    # a second identical run: no events, nothing removed
    clock.advance(minutes=15)
    assert run(store, make_cfg(server), clock).seqs == []
    assert len(events(store)) == 1


def test_new_mail_is_a_titled_added_event_and_digest_line(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Old")
    cfg = make_cfg(server)
    run(store, cfg, clock)
    clock.advance(minutes=15)
    server.add_message(
        "INBOX",
        message_id="<asn@supplier.example>",
        subject="Re: DJ ASN rejection",
        from_="EDI Desk <edi@supplier.example>",
    )
    result = run(store, cfg, clock)
    assert result.error is None
    added = events(store)[-1]
    assert (added.kind, added.record_key) == (KIND_ADDED, "<asn@supplier.example>")
    assert added.detail["title"] == [
        ["subject", "Re: DJ ASN rejection"],
        ["from", "EDI Desk <edi@supplier.example>"],
    ]
    digest = Service(store, clock).since()
    lines = [line.strip() for line in digest.splitlines()]
    expected = '+ "Re: DJ ASN rejection" from "EDI Desk <edi@supplier.example>"'
    assert any(line.startswith(expected) for line in lines), digest


def test_flag_change_is_a_modified_seen_event(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Please read")
    cfg = make_cfg(server)
    run(store, cfg, clock)
    server.set_flags("INBOX", 1, SEEN, FLAGGED)
    clock.advance(minutes=15)
    run(store, cfg, clock)
    modified = events(store)[-1]
    assert (modified.kind, modified.record_key) == (KIND_MODIFIED, "<m1@example.test>")
    assert [(c.field, c.old, c.new) for c in modified.field_changes] == [
        ("flagged", False, True),
        ("seen", False, True),
    ]
    assert modified.detail["title"][0] == ["subject", "Please read"]


def test_deleted_mail_is_removed_with_its_last_known_title(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Keep")
    server.add_message("INBOX", subject="Bye", from_="Bob <bob@example.test>")
    cfg = make_cfg(server)
    run(store, cfg, clock)
    server.delete_message("INBOX", 2)
    clock.advance(minutes=15)
    run(store, cfg, clock)
    removed = events(store)[-1]
    assert (removed.kind, removed.record_key) == (KIND_REMOVED, "<m2@example.test>")
    assert removed.detail["title"] == [["subject", "Bye"], ["from", "Bob <bob@example.test>"]]
    state = store.get_source_state("inbox")
    assert state is not None and state.record_count == 1


def test_a_mail_that_ages_out_of_the_window_leaves_without_an_event(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message(
        "INBOX", subject="Ancient", internaldate=datetime(2026, 9, 16, 10, tzinfo=UTC)
    )
    server.add_message(
        "INBOX", subject="Recent", internaldate=datetime(2026, 9, 28, 10, tzinfo=UTC)
    )
    cfg = make_cfg(server)
    run(store, cfg, clock)
    clock.now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)  # window start is now 2026-09-21
    result = run(store, cfg, clock)
    assert result.error is None and result.seqs == []
    assert [e.kind for e in events(store)] == [KIND_BASELINE]
    assert sorted(store.get_snapshot("inbox")) == ["<m2@example.test>"]
    state = store.get_source_state("inbox")
    assert state is not None and state.record_count == 1
    # ...whereas a mail still inside the window that vanished is a real removal
    server.delete_message("INBOX", 2)
    clock.advance(minutes=15)
    run(store, cfg, clock)
    assert kinds(store)[-1] == (KIND_REMOVED, "<m2@example.test>")


def test_the_same_message_id_in_two_folders_is_one_record(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", uid=7, message_id="<dup@example.test>", subject="Shared")
    server.add_message("Projects", uid=3, message_id="<dup@example.test>", subject="Shared")
    server.add_message("Projects", uid=4, message_id="<only-here@example.test>")
    cfg = make_cfg(server, folders=["INBOX", "Projects"])
    result = run(store, cfg, clock)
    assert result.error is None  # D4 never fires
    snapshot = store.get_snapshot("inbox")
    assert sorted(snapshot) == ["<dup@example.test>", "<only-here@example.test>"]
    assert snapshot["<dup@example.test>"].fields["folder"] == "INBOX"  # first by folder order
    clock.advance(minutes=15)
    assert run(store, cfg, clock).seqs == []
    # folder order decides which copy wins
    reversed_out = collect(make_cfg(server, folders=["Projects", "INBOX"]), clock)
    winners = {r.key: r.fields["folder"] for r in reversed_out.records}
    assert winners["<dup@example.test>"] == "Projects"


def test_duplicates_inside_one_folder_keep_the_lowest_uid(
    server: FakeImapServer, clock: Clock
) -> None:
    server.add_message("INBOX", uid=9, message_id="<dup@example.test>", subject="Second copy")
    server.add_message("INBOX", uid=2, message_id="<dup@example.test>", subject="First copy")
    records = collect(make_cfg(server), clock).records
    assert [(r.key, r.fields["subject"]) for r in records] == [("<dup@example.test>", "First copy")]


def test_a_mail_without_message_id_gets_a_uid_key(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_mailbox("Archive", uidvalidity=4242)
    server.add_message("INBOX", uid=5, message_id=None, subject="No id")
    server.add_message("Archive", uid=5, message_id=None, subject="No id either")
    server.add_message("INBOX", uid=6, message_id="  <spaced@example.test>  ")
    cfg = make_cfg(server, folders=["INBOX", "Archive"])
    run(store, cfg, clock)
    assert sorted(store.get_snapshot("inbox")) == [
        "<spaced@example.test>",  # stripped
        "uid:Archive/4242/5",
        "uid:INBOX/1000/5",
    ]


def test_records_are_sorted_by_key(server: FakeImapServer, clock: Clock) -> None:
    for name in ("<c@x>", "<a@x>", "<b@x>"):
        server.add_message("INBOX", message_id=name)
    server.add_message("INBOX", message_id=None)
    keys = [r.key for r in collect(make_cfg(server), clock).records]
    assert keys == sorted(keys) and len(keys) == 4


# -- header decoding -----------------------------------------------------------------------------


def fields_of(server: FakeImapServer, clock: Clock, **add: Any) -> dict[str, Any]:
    server.add_message("INBOX", uid=1, **add)
    (record,) = collect(make_cfg(server), clock).records
    return record.fields


def test_rfc2047_subject_and_from_are_decoded(server: FakeImapServer, clock: Clock) -> None:
    fields = fields_of(
        server,
        clock,
        subject="Re: =?UTF-8?B?QmVzdMOkdGlndW5n?= - =?iso-8859-1?Q?f=FCr_Sie?=",
        from_="=?UTF-8?Q?J=C3=B6rg_M=C3=BCller?= <joerg@example.test>",
    )
    assert fields["subject"] == "Re: Bestätigung - für Sie"
    assert fields["from"] == "Jörg Müller <joerg@example.test>"


def test_folded_subject_is_unfolded(server: FakeImapServer, clock: Clock) -> None:
    fields = fields_of(server, clock, subject="A very long\r\n subject that was folded")
    assert fields["subject"] == "A very long subject that was folded"


def test_raw_utf8_headers_are_decoded_and_bad_bytes_are_replaced(
    server: FakeImapServer, clock: Clock
) -> None:
    good = "Message-ID: <a@x>\r\nSubject: Grüße\r\nFrom: Jörg <j@example.test>\r\n\r\n"
    fields = fields_of(server, clock, raw_header=good.encode("utf-8"))
    assert (fields["subject"], fields["from"]) == ("Grüße", "Jörg <j@example.test>")
    server.mailboxes.clear()
    latin1 = b"Message-ID: <b@x>\r\nSubject: Gr\xfc\xdfe\r\nFrom: J\xf6rg <j@example.test>\r\n\r\n"
    fields = fields_of(server, clock, raw_header=latin1)
    assert fields["subject"] == "Gr��e"
    assert fields["from"] == "J�rg <j@example.test>"
    json.dumps(fields).encode("utf-8")  # storable: no lone surrogates


def test_missing_headers_become_empty_strings(server: FakeImapServer, clock: Clock) -> None:
    fields = fields_of(server, clock, subject=None, from_=None, to=None)
    assert (fields["subject"], fields["from"], fields["to"]) == ("", "", "")


def test_from_without_a_display_name_is_the_bare_address(
    server: FakeImapServer, clock: Clock
) -> None:
    fields = fields_of(server, clock, from_="plain@example.test")
    assert fields["from"] == "plain@example.test"


def test_to_lists_the_first_three_addresses(server: FakeImapServer, clock: Clock) -> None:
    fields = fields_of(
        server,
        clock,
        to='"Doe, Jane" <jane@example.test>, =?UTF-8?Q?Zo=C3=AB?= <zoe@example.test>, '
        "c@example.test, d@example.test, e@example.test",
    )
    assert fields["to"] == "Doe, Jane <jane@example.test>, Zoë <zoe@example.test>, c@example.test"


def test_undisclosed_recipients_fall_back_to_the_header_text(
    server: FakeImapServer, clock: Clock
) -> None:
    fields = fields_of(server, clock, to="undisclosed-recipients:;")
    assert fields["to"] == "undisclosed-recipients:;"


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("Tue, 29 Sep 2026 11:12:00 +0200", "2026-09-29T09:12:00Z"),
        ("Tue, 29 Sep 2026 09:12:00 GMT", "2026-09-29T09:12:00Z"),
        ("Tue, 29 Sep 2026 09:12:00 -0000", "2026-09-29T09:12:00Z"),
        ("Tue, 29 Sep 2026 09:12:00 +0000 (UTC)", "2026-09-29T09:12:00Z"),
        (None, "2026-09-28T12:00:00Z"),  # missing: INTERNALDATE
        ("not a date", "2026-09-28T12:00:00Z"),
        ("", "2026-09-28T12:00:00Z"),
        ("Thu, 01 Jan 1900 00:00:00 +0000", "2026-09-28T12:00:00Z"),  # implausible year
    ],
)
def test_date_is_the_date_header_in_utc_with_internaldate_as_fallback(
    server: FakeImapServer, clock: Clock, date: str | None, expected: str
) -> None:
    fields = fields_of(server, clock, date=date)
    assert fields["date"] == expected


def test_flags_and_size_become_typed_fields(server: FakeImapServer, clock: Clock) -> None:
    fields = fields_of(server, clock, flags=(SEEN, FLAGGED, "$Label1"), size=12345)
    assert (fields["seen"], fields["flagged"], fields["answered"]) == (True, True, False)
    assert fields["size"] == 12345 and isinstance(fields["size"], int)
    server.set_flags("INBOX", 1, "\\SEEN", "\\answered")  # flag names are case-insensitive
    (record,) = collect(make_cfg(server), clock).records
    assert (record.fields["seen"], record.fields["flagged"], record.fields["answered"]) == (
        True,
        False,
        True,
    )


def test_a_server_that_puts_the_literal_first_is_parsed_the_same(
    server: FakeImapServer, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Order", flags=(SEEN,), size=777)
    normal = collect(make_cfg(server), clock).records
    server.literal_first = True
    assert collect(make_cfg(server), clock).records == normal


def test_parse_fetch_skips_unsolicited_and_uid_less_responses() -> None:
    data: list[Any] = [
        (b"1 (UID 5 FLAGS (\\Seen) BODY[HEADER.FIELDS (SUBJECT)] {13}", b"Subject: A\r\n\r\n"),
        b")",
        b"2 (FLAGS (\\Seen))",  # unsolicited flag update: no UID
        b'3 (UID 9 RFC822.SIZE 42 INTERNALDATE " 1-Sep-2026 09:12:05 +0200" FLAGS ())',
        None,
    ]
    items = imapmod._parse_fetch(data)
    assert [(i.uid, i.size, i.header) for i in items] == [
        (5, 0, b"Subject: A\r\n\r\n"),
        (9, 42, b""),
    ]
    assert items[1].internaldate == datetime(2026, 9, 1, 7, 12, 5, tzinfo=UTC)
    assert items[0].flags == frozenset({"\\seen"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("01-Sep-2026 09:12:05 +0200", datetime(2026, 9, 1, 7, 12, 5, tzinfo=UTC)),
        (" 1-sep-2026 09:12:05 -0530", datetime(2026, 9, 1, 14, 42, 5, tzinfo=UTC)),
        ("31-Feb-2026 09:12:05 +0000", None),
        ("2026-09-01T09:12:05Z", None),
        ("01-Foo-2026 09:12:05 +0000", None),
    ],
)
def test_parse_internaldate(text: str, expected: datetime | None) -> None:
    assert imapmod._parse_internaldate(text) == expected


# -- batching and the newest-first cap -----------------------------------------------------------


def uid_lists(server: FakeImapServer) -> list[list[int]]:
    """The UIDs of every UID FETCH the server received."""
    sets = [c.split()[2] for c in server.commands_named("UID FETCH")]
    return [[int(u) for u in s.split(",")] for s in sets]


def test_fetch_goes_in_batches_of_100(server: FakeImapServer, clock: Clock) -> None:
    for _ in range(250):
        server.add_message("INBOX", subject="bulk")
    out = collect(make_cfg(server, max_messages=500), clock)
    assert len(out.records) == 250
    assert [len(batch) for batch in uid_lists(server)] == [100, 100, 50]


def test_only_the_newest_max_messages_are_fetched(server: FakeImapServer, clock: Clock) -> None:
    for _ in range(250):
        server.add_message("INBOX", subject="bulk")
    out = collect(make_cfg(server, max_messages=120), clock)
    assert len(out.records) == 120
    assert [len(batch) for batch in uid_lists(server)] == [100, 20]
    fetched = sorted(uid for batch in uid_lists(server) for uid in batch)
    assert fetched == list(range(131, 251))
    assert {r.key for r in out.records} == {f"<m{u}@example.test>" for u in fetched}


def test_the_cap_applies_per_folder(server: FakeImapServer, clock: Clock) -> None:
    for _ in range(5):
        server.add_message("INBOX")
        server.add_message("Other", message_id=None)
    out = collect(make_cfg(server, folders=["INBOX", "Other"], max_messages=2), clock)
    assert len(out.records) == 4


# -- read-only behaviour -------------------------------------------------------------------------


def test_the_client_only_sends_read_only_commands(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Unread")
    server.add_message("INBOX", subject="Unread too")
    server.add_message("Projects", subject="More")
    cfg = make_cfg(server, folders=["INBOX", "Projects"])
    run(store, cfg, clock)
    # Python 3.13's imaplib re-asks CAPABILITY after LOGIN; it is read-only, so ignore it here
    assert [c for c in server.command_names() if c != "CAPABILITY"] == [
        "LOGIN",
        "EXAMINE",
        "UID SEARCH",
        "UID FETCH",
        "EXAMINE",
        "UID SEARCH",
        "UID FETCH",
        "LOGOUT",
    ]
    assert set(server.command_names()) <= ALLOWED_COMMANDS
    assert server.bad_commands == []
    for fetch in server.commands_named("UID FETCH"):
        assert "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM TO DATE)]" in fetch
        assert "BODY[" not in fetch.replace("BODY.PEEK[", "")
    assert server.find("INBOX", 1).flags == []  # nothing got marked read


# -- folder names --------------------------------------------------------------------------------


def test_folder_names_are_quoted_and_sent_as_modified_utf7(
    server: FakeImapServer, clock: Clock
) -> None:
    server.add_message("台北", message_id="<tw@x>")
    server.add_message('Odd "quoted" \\ & name', message_id="<odd@x>")
    server.add_message("[Gmail]/已发邮件", message_id="<gm@x>")
    folders = ["台北", 'Odd "quoted" \\ & name', "[Gmail]/已发邮件"]
    out = collect(make_cfg(server, folders=folders), clock)
    assert {r.key: r.fields["folder"] for r in out.records} == {
        "<tw@x>": "台北",
        "<odd@x>": 'Odd "quoted" \\ & name',
        "<gm@x>": "[Gmail]/已发邮件",
    }
    assert server.commands_named("EXAMINE") == [
        'EXAMINE "&U,BTFw-"',
        'EXAMINE "Odd \\"quoted\\" \\\\ &- name"',
        'EXAMINE "[Gmail]/&XfJT0ZCuTvY-"',
    ]


def test_a_folder_that_examine_rejects_fails_the_whole_run(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX", subject="Mail")
    server.add_message("Projects", message_id="<p@x>", subject="Project mail")
    good = make_cfg(server, folders=["INBOX", "Projects"])
    run(store, good, clock)
    server.mailboxes.pop("Projects")
    clock.advance(minutes=15)
    server.commands.clear()
    result = run(store, good, clock)
    assert result.error == 'cannot open folder "Projects"'
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]  # nothing removed
    assert sorted(store.get_snapshot("inbox")) == ["<m1@example.test>", "<p@x>"]
    assert server.command_names()[-1] == "LOGOUT"  # LOGOUT is sent even though the run failed
    # once the folder is back the source recovers, still without removed/added noise
    server.add_message("Projects", uid=1, message_id="<p@x>", subject="Project mail")
    clock.advance(minutes=15)
    run(store, good, clock)
    assert [e.kind for e in events(store)] == [
        KIND_BASELINE,
        KIND_SOURCE_ERROR,
        KIND_SOURCE_RECOVERED,
    ]


def test_a_never_working_folder_is_a_first_run_failure(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    result = run(store, make_cfg(server, folders=["Nope"]), clock)
    assert result.error == 'cannot open folder "Nope"'
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]


# -- credentials and errors ----------------------------------------------------------------------


def test_bad_password_is_reported_without_the_password(
    server: FakeImapServer, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = "wrong-pw-987"
    monkeypatch.setenv(ENV, wrong)
    server.login_error_text = f"bad password {wrong} for {server.username}"  # server echoes it
    result = run(store, make_cfg(server), clock)
    assert result.error == f"login failed for {server.username}"
    assert wrong not in dump_state(store, clock)
    assert server.command_names() == ["CAPABILITY", "LOGIN", "LOGOUT"]


def test_a_password_with_quotes_and_backslashes_works_and_never_leaks(
    server: FakeImapServer, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    tricky = 'p"w\\d-S3CR3T'
    server.password = tricky
    monkeypatch.setenv(ENV, tricky)
    server.add_message("INBOX")
    cfg = make_cfg(server)
    assert run(store, cfg, clock).error is None
    # every kind of failure, then look at everything that was stored
    server.password = "changed"
    clock.advance(minutes=15)
    assert run(store, cfg, clock).error == f"login failed for {server.username}"
    server.password = tricky
    server.mailboxes.clear()
    clock.advance(minutes=15)
    assert run(store, cfg, clock).error == 'cannot open folder "INBOX"'
    server.add_message("INBOX")
    server.drop_on = "UID FETCH"
    clock.advance(minutes=15)
    assert (run(store, cfg, clock).error or "").startswith("IMAP error:")
    server.drop_on = None
    stored = dump_state(store, clock)
    assert tricky not in stored and "S3CR3T" not in stored


def test_an_unset_password_variable_is_named_in_the_error(
    server: FakeImapServer, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV)
    result = run(store, make_cfg(server), clock)
    assert result.error == f"environment variable {ENV} is not set (or is empty)"
    assert server.connections == 0
    monkeypatch.setenv(ENV, "")
    assert run(store, make_cfg(server), clock).error == result.error


@pytest.mark.parametrize("password", ["pass\nword", "pässword", "a\rb"])
def test_a_password_that_login_cannot_carry_is_rejected_without_echoing_it(
    server: FakeImapServer,
    store: Store,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    password: str,
) -> None:
    monkeypatch.setenv(ENV, password)
    result = run(store, make_cfg(server), clock)
    assert result.error is not None and ENV in result.error and "cannot" in result.error
    assert password not in result.error and password.strip() not in result.error
    assert server.connections == 0


def test_connection_refused_is_a_source_error_and_removes_nothing(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX")
    cfg = make_cfg(server)
    run(store, cfg, clock)
    server.stop()
    clock.advance(minutes=15)
    result = run(store, cfg, clock)
    assert result.error is not None
    assert result.error.startswith(f"cannot connect to 127.0.0.1:{server.port}: ")
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    assert len(store.get_snapshot("inbox")) == 1


def test_a_silent_server_times_out_with_a_short_reason(
    server: FakeImapServer, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imapmod, "TIMEOUT_S", 0.3)
    server.silent = True
    result = run(store, make_cfg(server), clock)
    assert result.error is not None
    assert result.error.startswith(f"cannot connect to 127.0.0.1:{server.port}: ")
    assert "timed out" in result.error


def test_a_dropped_connection_is_a_source_error_and_removes_nothing(
    server: FakeImapServer, store: Store, clock: Clock
) -> None:
    server.add_message("INBOX")
    cfg = make_cfg(server)
    run(store, cfg, clock)
    server.drop_on = "UID FETCH"
    clock.advance(minutes=15)
    result = run(store, cfg, clock)
    assert result.error is not None and result.error.startswith("IMAP error: ")
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    assert len(store.get_snapshot("inbox")) == 1


# -- ssl / starttls / none, with the imaplib classes replaced ------------------------------------


class MockImap:
    """Stands in for ``imaplib.IMAP4`` / ``IMAP4_SSL``: records how it was built and used, and
    serves an empty INBOX."""

    error = IMAP_ERROR  # like the real class: ``imaplib.IMAP4.error`` / ``.abort``
    abort = IMAP_ABORT
    instances: list[MockImap] = []
    login_error: BaseException | None = None
    search_error: BaseException | None = None
    connect_error: BaseException | None = None

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        if MockImap.connect_error is not None:
            raise MockImap.connect_error
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.starttls_context: Any = None
        MockImap.instances.append(self)

    def starttls(self, ssl_context: Any = None) -> None:
        self.calls.append("starttls")
        self.starttls_context = ssl_context

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append("login")
        self.login_args = (user, password)
        if MockImap.login_error is not None:
            raise MockImap.login_error
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append("select" if not readonly else "examine")
        self.mailbox = mailbox
        return "OK", [b"0"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        return code, [b"77"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.calls.append(f"uid {command.lower()}")
        if MockImap.search_error is not None:
            raise MockImap.search_error
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append("logout")
        return "BYE", [b""]

    def shutdown(self) -> None:
        self.calls.append("shutdown")


@pytest.fixture
def mock_imap(monkeypatch: pytest.MonkeyPatch) -> type[MockImap]:
    MockImap.instances = []
    MockImap.login_error = MockImap.search_error = MockImap.connect_error = None
    monkeypatch.setattr(imapmod.imaplib, "IMAP4", MockImap)
    monkeypatch.setattr(imapmod.imaplib, "IMAP4_SSL", MockImap)
    monkeypatch.setenv(ENV, "pw-for-mock")
    return MockImap


def test_ssl_connects_with_a_verifying_default_context(mock_imap: type[MockImap]) -> None:
    (conn,) = _collect_once(mock_imap, cfg_with())
    assert (conn.host, conn.port) == ("imap.example.com", 993)
    assert conn.kwargs["timeout"] == 30
    context = conn.kwargs["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert conn.calls == ["login", "examine", "uid search", "logout"]
    assert conn.login_args == ('"me@example.com"', "pw-for-mock")
    assert conn.mailbox == '"INBOX"'


def test_ssl_uses_the_configured_port(mock_imap: type[MockImap]) -> None:
    (conn,) = _collect_once(mock_imap, cfg_with(port=1993))
    assert conn.port == 1993


def test_starttls_upgrades_before_login(mock_imap: type[MockImap]) -> None:
    (conn,) = _collect_once(mock_imap, cfg_with(security="starttls"))
    assert (conn.host, conn.port) == ("imap.example.com", 143)
    assert conn.kwargs == {"timeout": 30}
    assert conn.calls == ["starttls", "login", "examine", "uid search", "logout"]
    context = conn.starttls_context
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_security_none_is_a_plain_connection_without_starttls(mock_imap: type[MockImap]) -> None:
    (conn,) = _collect_once(mock_imap, cfg_with(host="localhost", security="none"))
    assert (conn.host, conn.port) == ("localhost", 143)
    assert conn.kwargs == {"timeout": 30}
    assert "starttls" not in conn.calls and conn.starttls_context is None


def test_ssl_mode_never_falls_back_to_a_plain_connection(
    mock_imap: type[MockImap], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("plain IMAP4 must not be used for security=ssl")

    monkeypatch.setattr(imapmod.imaplib, "IMAP4", forbidden)
    _collect_once(mock_imap, cfg_with())


def test_a_tls_failure_is_a_connection_error_not_a_crash(mock_imap: type[MockImap]) -> None:
    mock_imap.connect_error = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    )
    with pytest.raises(CollectError) as info:
        collect(cfg_with())
    assert str(info.value).startswith("cannot connect to imap.example.com:993: ")
    assert "certificate" in str(info.value)


def test_logout_is_sent_even_when_login_fails(mock_imap: type[MockImap]) -> None:
    mock_imap.login_error = IMAP_ERROR("LOGIN failed for pw-for-mock")
    with pytest.raises(CollectError) as info:
        collect(cfg_with())
    assert str(info.value) == "login failed for me@example.com"
    (conn,) = mock_imap.instances
    assert conn.calls == ["login", "logout"]


@pytest.mark.parametrize("error_type", [IMAP_ABORT, OSError, ValueError, UnicodeEncodeError])
def test_errors_from_imaplib_never_carry_the_password(
    mock_imap: type[MockImap], error_type: type[BaseException]
) -> None:
    text = 'bad thing with pw-for-mock and "pw-for-mock"'
    error = (
        UnicodeEncodeError("ascii", "pw-for-mock", 0, 1, text)
        if error_type is UnicodeEncodeError
        else error_type(text)
    )
    mock_imap.search_error = error
    with pytest.raises(CollectError) as info:
        collect(cfg_with())
    assert "pw-for-mock" not in str(info.value) and "***" in str(info.value)
    mock_imap.search_error = None
    mock_imap.login_error = IMAP_ABORT(text)
    with pytest.raises(CollectError) as info:
        collect(cfg_with())
    assert "pw-for-mock" not in str(info.value)
    assert str(info.value).startswith("connection lost during login: ")


def _collect_once(mock: type[MockImap], cfg: SourceConfig) -> list[MockImap]:
    assert collect(cfg).records == []
    return mock.instances
