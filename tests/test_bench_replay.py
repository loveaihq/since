"""Replay of the benchmark world into Since (``bench/replay.py``, M3 T3).

Two layers. Without a browser: the pieces (config, PO database, IMAP mirror feeding the real imap
source, the portal server, the observability check). With a browser: one full replay of the default
world into a tmp SINCE_HOME (module-scoped, ~1 minute), whose digests are then examined. The
browser is Playwright's Chromium unless ``SINCE_TEST_BROWSER_CHANNEL`` names an installed one
(``msedge`` on Windows dev boxes); without one those tests skip (on CI they fail).
"""

from __future__ import annotations

import http.client
import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from imap_fake import FakeImapServer
from web_site import CHANNEL

from bench.replay import (
    BENCH_AGENT,
    FULL_BUDGET,
    IMAP_PASSWORD_ENV,
    IMAP_USER,
    PO_QUERY,
    PO_URL_ENV,
    REASON_LAYOUT,
    REASON_LOGIN,
    REASON_OTHER,
    SOURCE_INBOX,
    SOURCE_PO,
    SOURCE_PORTAL,
    ImapMirror,
    Observation,
    PortalServer,
    ReplayError,
    ReplayReport,
    build_config,
    digest_text,
    main,
    observe,
    replay,
    write_po_db,
)
from bench.world import (
    FLAG_ANSWERED,
    FLAG_FLAGGED,
    FLAG_SEEN,
    KIND_EMAIL,
    KIND_PO,
    KIND_PORTAL,
    KIND_SYSTEM,
    LAST_LOOK,
    LAYOUT_CHANGE_AT,
    LOGIN_EXPIRY_AT,
    NOW,
    PORTAL_LOGIN_SELECTOR,
    PORTAL_V1_EXTRACT,
    START,
    SYSTEM_LAYOUT,
    SYSTEM_LOGIN,
    World,
    build_world,
    portal_html,
)
from since.config import Config, load_config, parse_config
from since.model import HIGHLIGHT_BONUS_DEFAULT
from since.sources.imap import ImapCollector
from since.sources.sql import SqlCollector
from since.store import Store
from since.timeutil import to_iso

SOURCE_IDS = [SOURCE_INBOX, SOURCE_PO, SOURCE_PORTAL]


@pytest.fixture(scope="module")
def world() -> World:
    return build_world()


def config_for(tmp_path: Path, **overrides: object) -> Config:
    args: dict[str, object] = {
        "imap_port": 1143,
        "portal_url": "http://127.0.0.1:8080/orders",
        "profile_dir": tmp_path / "profiles" / SOURCE_PORTAL,
    }
    args.update(overrides)
    return parse_config(build_config(**args))  # type: ignore[arg-type]


# -- configuration -------------------------------------------------------------------------------


def test_config_is_plain_and_valid(tmp_path: Path) -> None:
    data = build_config(
        imap_port=1143,
        portal_url="http://127.0.0.1:8080/orders",
        profile_dir=tmp_path / "profiles" / SOURCE_PORTAL,
        browser_channel="msedge",
    )
    text = yaml.safe_dump(data, sort_keys=False)
    (tmp_path / "since.yaml").write_text(text, encoding="utf-8")
    config = load_config(tmp_path / "since.yaml")  # the real validation of the real loader
    by_id = {s.id: s for s in config.sources}
    assert list(by_id) == SOURCE_IDS

    inbox, po, portal = (by_id[i] for i in SOURCE_IDS)
    assert (inbox.type, inbox.priority) == ("imap", "normal")
    assert inbox.options["folders"] == ["INBOX"]
    assert inbox.options["since_days"] == 14
    assert inbox.options["security"] == "none"
    assert inbox.options["host"] == "127.0.0.1"
    assert inbox.options["username"] == IMAP_USER
    assert (po.type, po.priority) == ("sql", "high")
    assert po.options["key"] == ["po_no"]
    assert po.options["query"] == PO_QUERY
    assert po.track_fields == ["status", "eta"]
    assert (portal.type, portal.priority) == ("web", "high")
    assert portal.options["extract"] == PORTAL_V1_EXTRACT
    assert portal.options["login_detect"] == {"selector": PORTAL_LOGIN_SELECTOR}
    assert portal.options["browser_channel"] == "msedge"
    assert portal.track_fields == ["status", "ship_by"]

    # D37: only CLAUDE.md's example rule (status changed to Cancelled) on po-table and sps-portal;
    # nothing else is tuned to the planted content
    rule = [("status", "changed_to", "Cancelled", HIGHLIGHT_BONUS_DEFAULT)]
    for source in (po, portal):
        assert [(r.field, r.op, r.value, r.bonus) for r in source.highlight] == rule
    assert not inbox.highlight
    # credentials only by env var name
    assert inbox.options["password_env"] == IMAP_PASSWORD_ENV
    assert po.options["url_env"] == PO_URL_ENV
    assert not re.search(r"^\s*(password|url):.*(secret|pw|pass)", text, re.MULTILINE | re.I)


def test_the_channel_is_left_out_when_not_given(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    portal = next(s for s in config.sources if s.id == SOURCE_PORTAL)
    assert "browser_channel" not in portal.options


# -- the fakes -----------------------------------------------------------------------------------


def test_po_database_holds_exactly_the_given_rows(world: World, tmp_path: Path) -> None:
    path = tmp_path / "world" / "po.db"
    before, after = world.state_at(START).po_rows, world.state_at(NOW).po_rows
    assert len(before) >= 50
    write_po_db(path, before)
    write_po_db(path, after)  # replaces, does not append
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT po_no, supplier, status, eta, qty, updated_at FROM purchase_orders "
            "ORDER BY po_no"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        (r["po_no"], r["supplier"], r["status"], r["eta"], r["qty"], r["updated_at"]) for r in after
    ]


def test_the_sql_source_reads_the_po_database(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "po.db"
    rows = world.state_at(LAST_LOOK).po_rows
    write_po_db(path, rows)
    monkeypatch.setenv(PO_URL_ENV, f"sqlite:///{path.as_posix()}")
    po = next(s for s in config_for(tmp_path).sources if s.id == SOURCE_PO)
    records = SqlCollector().collect(po)
    assert {r.key for r in records} == {row["po_no"] for row in rows}
    one = next(r for r in records if r.key == rows[0]["po_no"])
    assert one.fields["status"] == rows[0]["status"]
    assert one.fields["eta"] == rows[0]["eta"]


def test_the_imap_mirror_feeds_the_imap_source(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = secrets.token_urlsafe(12)
    monkeypatch.setenv(IMAP_PASSWORD_ENV, password)
    with FakeImapServer(username=IMAP_USER, password=password) as server:
        server.add_mailbox("INBOX")
        mirror = ImapMirror(server)
        inbox = next(
            s for s in config_for(tmp_path, imap_port=server.port).sources if s.id == SOURCE_INBOX
        )

        def collect(at: datetime) -> dict[str, dict[str, object]]:
            out = ImapCollector(now_fn=lambda: at).collect(inbox)
            return {r.key: dict(r.fields) for r in out.records}

        early = world.state_at(LAST_LOOK)
        mirror.apply(early.mails)
        seen = collect(LAST_LOOK)
        assert len(seen) == len(early.mails) > 100
        for mail in early.mails:  # every field the world says, as the source reads it
            fields = seen[mail.message_id]
            assert fields["subject"] == mail.subject
            assert fields["from"] == mail.from_header.replace('"', "")
            assert fields["received"] == to_iso(mail.received)
            assert fields["folder"] == mail.folder
            assert fields["seen"] == (FLAG_SEEN in mail.flags)
            assert fields["flagged"] == (FLAG_FLAGGED in mail.flags)
            assert fields["answered"] == (FLAG_ANSWERED in mail.flags)

        late = world.state_at(NOW)  # more mail, and flags that changed since
        mirror.apply(late.mails)
        mirror.apply(late.mails)  # idempotent
        assert len(server.mailboxes["INBOX"].messages) == len(late.mails)
        seen = collect(NOW)
        assert len(seen) == len(late.mails)
        for mail in late.mails:
            fields = seen[mail.message_id]
            assert fields["flagged"] == (FLAG_FLAGGED in mail.flags)
            assert fields["answered"] == (FLAG_ANSWERED in mail.flags)
        assert any(
            a.flags != b.flags for a, b in zip(early.mails, late.mails, strict=False)
        )  # the test does exercise flag changes


def _get(port: int, path: str) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, headers, response.read().decode("utf-8")
    finally:
        conn.close()


def test_portal_server_follows_the_world(world: World) -> None:
    v1 = world.state_at(LAST_LOOK).portal
    v2 = world.state_at(LAYOUT_CHANGE_AT).portal
    expired = world.state_at(NOW).portal
    with PortalServer(v1) as server:
        port = int(server.url.split(":")[2].split("/")[0])
        assert server.url == f"http://127.0.0.1:{port}/orders"
        status, headers, body = _get(port, "/orders")
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert body == portal_html(v1)
        assert 'id="orders"' in body

        server.portal = v2  # new layout
        status, _, body = _get(port, "/orders?x=1")
        assert (status, body) == (200, portal_html(v2))
        assert 'id="orders"' not in body

        server.portal = expired  # login gone: /orders redirects to the login page
        status, headers, _ = _get(port, "/orders")
        assert (status, headers["location"]) == (302, "/login")
        status, _, body = _get(port, "/login")
        assert status == 200
        assert 'id="login"' in body
        server.portal = v1  # the login page is the login page whatever the portal state
        assert 'id="login"' in _get(port, "/login")[2]
        assert _get(port, "/nope")[0] == 404


# -- what the replay claims ----------------------------------------------------------------------


def _fake_digest(world: World, *, refs: bool, system: bool) -> str:
    lines = ["since · agent=bench · events 1-9 (9), showing 9 · budget 100000 · next_cursor=9"]
    for kind, ref in world.planted:
        if kind != KIND_SYSTEM and refs:
            lines.append(f'  ~ "{ref}" status: "a" -> "b"  since://evt/1')
    if system:
        lines.append('  ! schema_changed: 1 extractor selector matches 0 elements ("x")')
        lines.append('  ! source_error: "login expired"; needs a human: run since login p')
    return "\n".join(lines)


def test_observe_finds_every_planted_item(world: World) -> None:
    found = observe(world, _fake_digest(world, refs=True, system=True))
    assert [(o.kind, o.ref) for o in found] == world.planted
    assert all(o.observed and o.reason == "" for o in found)


def test_observe_gives_the_reason_for_what_it_misses(world: World) -> None:
    # nothing in the digest: the system items are missing, and so is everything else
    missing = {
        (o.kind, o.ref): o for o in observe(world, _fake_digest(world, refs=False, system=False))
    }
    assert not any(o.observed for o in missing.values())
    assert missing[(KIND_SYSTEM, SYSTEM_LAYOUT)].reason == REASON_OTHER
    assert missing[(KIND_SYSTEM, SYSTEM_LOGIN)].reason == REASON_OTHER
    assert (
        missing[(KIND_PO, next(r for k, r in world.planted if k == KIND_PO))].reason == REASON_OTHER
    )
    assert (
        missing[(KIND_EMAIL, next(r for k, r in world.planted if k == KIND_EMAIL))].reason
        == REASON_OTHER
    )
    # portal orders that changed after the portal became unreadable are explained by the timing
    by_reason = {
        o.reason for o in missing.values() if o.kind == KIND_PORTAL and o.reason != REASON_OTHER
    }
    assert by_reason == {REASON_LAYOUT, REASON_LOGIN}
    for scenario in world.scenarios:
        if scenario.role != "planted" or scenario.kind != KIND_PORTAL:
            continue
        expected = (
            REASON_LOGIN
            if scenario.at >= LOGIN_EXPIRY_AT
            else REASON_LAYOUT
            if scenario.at >= LAYOUT_CHANGE_AT
            else REASON_OTHER
        )
        assert missing[(KIND_PORTAL, scenario.ref)].reason == expected


def test_a_reference_must_match_as_a_whole_number(world: World) -> None:
    ref = next(r for k, r in world.planted if k == KIND_PO)
    for text in (f'~ po_no "{ref}1"', f'~ po_no "9{ref}"', f"x{ref}9"):
        assert not next(o for o in observe(world, text) if o.ref == ref).observed
    for text in (f'~ po_no "{ref}"', f"PO {ref}, ", f"({ref})"):
        assert next(o for o in observe(world, text) if o.ref == ref).observed


def test_replay_needs_a_fresh_directory(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "used"
    home.mkdir()
    (home / "since.db").write_text("x", encoding="utf-8")
    with pytest.raises(ReplayError, match="not an empty directory"):
        replay(world, home)
    assert (home / "since.db").read_text(encoding="utf-8") == "x"  # nothing was touched
    assert main(["--home", str(home)]) == 2
    assert "error:" in capsys.readouterr().err


# -- a full replay (needs a browser) -------------------------------------------------------------


@pytest.fixture(scope="module")
def replayed(
    browser_ok: None, world: World, tmp_path_factory: pytest.TempPathFactory
) -> ReplayReport:
    home = tmp_path_factory.mktemp("replay") / "home"
    return replay(world, home, browser_channel=CHANNEL)


def test_replay_builds_a_since_home(replayed: ReplayReport, world: World) -> None:
    home = replayed.home
    assert (home / "since.db").is_file()
    assert (home / "world" / "po.db").is_file()
    config = load_config(home / "since.yaml")
    assert [s.id for s in config.sources] == SOURCE_IDS
    assert replayed.ticks == len(world.ticks)
    assert set(replayed.events) == set(SOURCE_IDS)
    assert all(n > 1 for n in replayed.events.values())  # more than the baseline
    with Store.open(home) as store:
        baselines = [e.source_id for e in store.events_after(0) if e.kind == "baseline"]
        assert sorted(baselines) == sorted(SOURCE_IDS)  # exactly one baseline per source
        assert store.list_served() == []  # nothing served yet: the agent starts fresh
        states = {s.source_id: s for s in store.list_source_states()}
    # the portal ended in error (the login expired), the others are fine
    assert states[SOURCE_PORTAL].in_error
    assert states[SOURCE_PORTAL].last_error == "login expired"
    assert not states[SOURCE_INBOX].in_error
    assert not states[SOURCE_PO].in_error
    assert states[SOURCE_INBOX].record_count == len(world.state_at(NOW).mails)
    assert states[SOURCE_PO].record_count == len(world.state_at(NOW).po_rows)
    assert IMAP_PASSWORD_ENV not in os.environ  # the replay's environment is put back
    assert PO_URL_ENV not in os.environ


def test_every_planted_item_that_can_be_observed_is_in_the_digest(
    replayed: ReplayReport, world: World
) -> None:
    assert [(o.kind, o.ref) for o in replayed.observability] == world.planted
    # what nothing could read: portal changes after the layout change / after the login expired
    unreachable = {
        scenario.ref: REASON_LOGIN if scenario.at >= LOGIN_EXPIRY_AT else REASON_LAYOUT
        for scenario in world.scenarios
        if scenario.role == "planted"
        and scenario.kind == KIND_PORTAL
        and scenario.at >= LAYOUT_CHANGE_AT
    }
    assert set(unreachable.values()) == {REASON_LAYOUT, REASON_LOGIN}
    for item in replayed.observability:
        if item.reason in (REASON_LAYOUT, REASON_LOGIN):
            assert not item.observed
        else:
            assert item.observed, f"{item.kind} {item.ref} is missing from the digest"
    listed = {o.ref: o.reason for o in replayed.unobserved()}
    assert listed == unreachable  # nothing else is missed, and each miss has its reason


def test_the_system_items_are_observed(replayed: ReplayReport) -> None:
    lines = replayed.digest.splitlines()
    assert any("schema_changed" in line for line in lines)
    assert any("login expired" in line for line in lines)
    by_ref = {o.ref: o for o in replayed.observability if o.kind == KIND_SYSTEM}
    assert by_ref[SYSTEM_LAYOUT].observed
    assert by_ref[SYSTEM_LOGIN].observed


def test_the_bench_agent_is_acked_at_the_last_look(replayed: ReplayReport) -> None:
    with Store.open(replayed.home) as store:
        events = store.events_after(0)
        stamp = to_iso(LAST_LOOK)
        at_last_look = max(e.seq for e in events if e.created_at <= stamp)
        assert store.get_cursor(BENCH_AGENT) == at_last_look == replayed.cursor
        assert store.max_seq() == replayed.max_seq > replayed.cursor
        assert all(e.created_at > stamp for e in events if e.seq > replayed.cursor)
        assert store.get_cursor("some-other-agent") == 0


def test_the_digest_starts_after_the_last_look(replayed: ReplayReport, world: World) -> None:
    header = replayed.digest.splitlines()[0]
    events = replayed.max_seq - replayed.cursor
    assert (
        f"agent={BENCH_AGENT} · events {replayed.cursor + 1}-{replayed.max_seq} ({events})"
        in header
    )
    assert f"budget {FULL_BUDGET}" in header
    assert "omitted:" not in replayed.digest  # the big budget shows everything
    # things that happened before the last look are not in it (their events are before the cursor)
    for scenario in world.scenarios:
        if scenario.role == "decoy" and scenario.at < LAST_LOOK and scenario.ref.isdigit():
            assert scenario.ref not in replayed.digest, (scenario.kind, scenario.ref)


def test_a_small_budget_omits_and_the_daemon_is_not_stale(replayed: ReplayReport) -> None:
    small = digest_text(replayed.home, 800)
    assert "omitted:" in small  # many events: most of them are left out
    assert re.search(r"^omitted: .* since://batch/\d+-\d+\?source=", small, re.MULTILINE)
    assert "warning:" not in small  # neither stale nor missing daemon heartbeat
    assert "warning:" not in replayed.digest
    assert len(small) < len(replayed.digest)
    # asking twice gives the same text and leaves no trace in the served log
    assert digest_text(replayed.home, 800) == small
    with Store.open(replayed.home) as store:
        assert store.list_served() == []


def test_the_summary_says_what_was_missed(replayed: ReplayReport) -> None:
    summary = replayed.summary()
    assert f"{len(replayed.observability) - 2}/{len(replayed.observability)}" in summary
    assert "not observed: portal" in summary
    assert REASON_LAYOUT in summary
    assert REASON_LOGIN in summary
    assert all(isinstance(o, Observation) for o in replayed.observability)
