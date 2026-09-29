"""``since ui`` (D32): every page, escaping of hostile data, headers, Host/method/path/query
handling, read-only behaviour, and the CLI command. The server runs on port 0 in a thread; pages are
fetched with ``http.client`` (which lets a test set the Host header and the method itself)."""

from __future__ import annotations

import hashlib
import html
import http.client
import itertools
import json
import re
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from test_store import make_v2_database

import since.store as store_mod
import since.ui as ui
from since.cli import build_parser, main
from since.daemon import META_HEARTBEAT, META_MIN_SCHEDULE
from since.model import FieldChange
from since.paths import config_path, db_path
from since.render import event_body
from since.sanitize import DIGEST_CAP
from since.service import Service
from since.store import Store
from since.timeutil import to_iso

T0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=30)  # what the server under test believes "now" is

XSS = "<script>alert(1)</script>"
BREAKOUT = '"><img src=x onerror=alert(1)>'
RLO = "\u202e"  # right-to-left override

CSP = "default-src 'none'; style-src 'unsafe-inline'"

# The only tags a page may contain, and the only attribute shapes: anything a source injects that
# survived escaping would show up as another tag or another attribute.
ALLOWED_TAGS = {
    "html", "head", "meta", "title", "style", "body", "nav", "a", "h1", "h2", "p", "span",
    "table", "thead", "tbody", "tr", "th", "td", "code", "pre", "footer",
}  # fmt: skip
ALLOWED_ATTRS = re.compile(r'(href="[^"<>]*"|class="[a-z ]+"|lang="en"|charset="utf-8")')


# --- helpers ---------------------------------------------------------------------------------


class Resp(NamedTuple):
    status: int
    headers: dict[str, str]  # lower-case names
    body: str
    raw: bytes


def fetch(
    port: int,
    path: str,
    *,
    method: str = "GET",
    host: str | list[str] | None = "default",
    headers: tuple[tuple[str, str], ...] = (),
) -> Resp:
    """One request. ``host="default"`` sends ``127.0.0.1:<port>``, ``None`` sends no Host header,
    a list sends several."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        hosts = [f"127.0.0.1:{port}"] if host == "default" else host
        for value in [] if hosts is None else [hosts] if isinstance(hosts, str) else hosts:
            conn.putheader("Host", value)
        for name, value in headers:
            conn.putheader(name, value)
        if method not in ("GET", "HEAD"):
            conn.putheader("Content-Length", "0")
        conn.endheaders()
        response = conn.getresponse()
        raw = response.read()
        return Resp(
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            raw.decode("utf-8"),
            raw,
        )
    finally:
        conn.close()


def pres(body: str) -> list[str]:
    """The text of each ``<pre>`` as a browser shows it (one leading newline is dropped)."""
    texts = []
    for raw in re.findall(r"<pre>(.*?)</pre>", body, re.DOTALL):
        texts.append(html.unescape(raw[1:] if raw.startswith("\n") else raw))
    return texts


def section(body: str, heading: str) -> str:
    start = body.index(f"<h2>{heading}")
    end = body.find("<h2>", start + 1)
    return body[start : end if end != -1 else len(body)]


def table_rows(fragment: str) -> list[list[str]]:
    """Visible cell texts of every body row of the tables in ``fragment``."""
    rows = []
    for row in re.findall(r"<tr>(.*?)</tr>", fragment, re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if cells:
            rows.append([html.unescape(re.sub(r"<[^>]+>", "", cell)) for cell in cells])
    return rows


def calm(text: str) -> str:
    """What a table cell shows of a value: control/format characters become spaces."""
    return " ".join(text.replace(RLO, " ").split())


def hrefs(body: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', body)]


def assert_page_is_inert(path: str, body: str) -> None:
    """No injected markup: only known tags and attribute shapes, no raw hostile text."""
    markup = re.sub(r"<style>.*?</style>", "", body, flags=re.DOTALL)
    assert set(re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)", markup)) <= ALLOWED_TAGS, path
    for attrs in re.findall(r"<[a-zA-Z0-9]+((?:\s[^<>]*)?)>", markup):
        for attr in re.findall(r'[a-z-]+="[^"]*"', attrs):
            assert ALLOWED_ATTRS.fullmatch(attr), (path, attr)
        assert re.sub(r'\s*[a-z-]+="[^"]*"', "", attrs) == "", (path, attrs)
    assert "<script" not in body.lower(), path
    assert "<img" not in body.lower(), path
    assert RLO not in body, path
    for href in re.findall(r'href="([^"]*)"', body):
        assert re.fullmatch(r"/[A-Za-z0-9/_?&;=%.~-]*", href), (path, href)


class Running:
    """A live server plus the shortcuts the tests use."""

    def __init__(self, server: ui.AuditServer) -> None:
        self.server = server
        self.port = server.port

    def get(self, path: str, **kw: Any) -> Resp:
        return fetch(self.port, path, **kw)

    def ok(self, path: str) -> Resp:
        response = self.get(path)
        assert response.status == 200, (path, response.status, response.body[:200])
        return response


@pytest.fixture
def running() -> Iterator[Running]:
    server = ui.AuditServer(0, now_fn=lambda: NOW)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield Running(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


# --- the world: sources, events of every kind, hostile values, two agents ---------------------


def build_world() -> None:
    """Fill the database (SINCE_HOME is a tmp dir, see conftest) through the Store/Service API.

    Events: 1 baseline, 2 added, 3 added (hostile key), 4 modified, 5 removed, 6 schema_changed,
    7 source_error, 8 source_recovered, 9 an unknown kind and 10 a baseline with damaged detail (the
    last two written with SQL: what a tampered or damaged database could hold)."""
    ticks = itertools.count(1)

    def clock() -> datetime:
        return T0 + timedelta(minutes=next(ticks), seconds=7)

    with Store.open() as store:
        store.upsert_source("inbox", "imap", "normal", 900, "mail")
        store.upsert_source("po-table", "sql", "high", 900, "PO")
        store.upsert_source("sps-portal", "web", "high", 1800, "PO")
        store.upsert_source("fresh", "dir", "low", 60)
        store.upsert_source("gone", "dir", "low", 60, configured=False)
        store.upsert_source('"><script>alert(5)</script>', "dir", "normal", 60)  # a tampered id
        store.update_source_state(
            "inbox", last_success_at=T0 + timedelta(seconds=5), record_count=31, baselined=True
        )
        store.update_source_state("po-table", last_success_at=T0, record_count=52, baselined=True)
        store.update_source_state("gone", last_success_at=T0 - timedelta(days=2), record_count=3)
        store.update_source_state(
            "sps-portal",
            in_error=True,
            error_since=T0,
            last_error_at=T0,
            last_error=XSS + RLO + " tail",
            last_success_at=T0 - timedelta(hours=3),
            record_count=20,
        )
        store.append_event("inbox", "baseline", now=T0, detail={"record_count": 31})
        store.append_event(
            "inbox",
            "added",
            now=T0 + timedelta(minutes=5),
            record_key="m1",
            field_changes=[FieldChange("note", None, BREAKOUT)],
            importance=6,
            detail={"title": [["subject", XSS], ["from", "edi@x" + RLO + "gpj.exe"]]},
        )
        store.append_event(
            "inbox", "added", now=T0 + timedelta(minutes=6), record_key=BREAKOUT, importance=6
        )
        store.append_event(
            "po-table",
            "modified",
            now=T0 + timedelta(minutes=9),
            record_key="4500123",
            field_changes=[
                FieldChange("status", "Open", XSS),
                FieldChange("<b>notes</b>", None, None, added_chars=300, removed_chars=10),
            ],
            importance=12,
        )
        store.append_event(
            "po-table", "removed", now=T0 + timedelta(minutes=10), record_key="4500124"
        )
        store.append_event(
            "sps-portal",
            "schema_changed",
            now=T0 + timedelta(minutes=11),
            importance=15,
            detail={"selectors": ["table#orders tbody tr", XSS]},
        )
        store.append_event(
            "sps-portal",
            "source_error",
            now=T0 + timedelta(minutes=12),
            importance=15,
            detail={"error": XSS + RLO, "hint": "run since login sps-portal"},
        )
        store.append_event(
            "sps-portal",
            "source_recovered",
            now=T0 + timedelta(minutes=13),
            importance=3,
            detail={"error_since": to_iso(T0), "last_error": BREAKOUT},
        )

        service = Service(store, clock)
        service.since("alice")
        service.get("since://evt/4", agent_id="alice")
        service.get("since://rec/po-table/4500123", agent_id="alice")
        service.since("bob", source="inbox")
        service.get(XSS, agent_id="bob")  # an error text quoting the hostile handle
        service.since("bob", source=XSS)  # ... and the hostile source in the arguments
        store.set_cursor("alice", 5, clock())
        # A row a tampered database could hold: hostile agent/tool/via/args and a raw text that
        # starts with a newline and ends in spaces.
        store.log_served(
            '"><script>alert(4)</script>' + RLO,
            "since<b>" + RLO + "x",
            {"k": XSS, "n": 7},
            "\n" + XSS + "\nraw <b>text</b> & \"quotes\" 'x'  \n\n",
            "cli<i>" + RLO,
            clock(),
        )
        # Written last: a digest cannot be built over them (only the audit page has to cope).
        insert = (
            "INSERT INTO events (source_id, kind, record_key, field_changes_json, importance, "
            "detail_json, created_at) VALUES (?, ?, NULL, '[]', 1, ?, '2026-09-29T09:20:00Z')"
        )
        store._conn.execute(insert, ("inbox", XSS, "{}"))
        store._conn.execute(insert, ("inbox", "baseline", '{"record_count": "many"}'))


@pytest.fixture
def world() -> None:
    build_world()


def crawl(running: Running) -> dict[str, Resp]:
    """Every page reachable from ``/`` by following links (a bounded breadth-first walk)."""
    seen: dict[str, Resp] = {}
    queue = ["/"]
    while queue and len(seen) < 300:
        path = queue.pop(0)
        if path in seen:
            continue
        seen[path] = running.get(path)
        if seen[path].status == 200:
            queue.extend(h for h in hrefs(seen[path].body) if h not in seen)
    return seen


# --- pages -----------------------------------------------------------------------------------


def test_every_page_renders_and_links_resolve(running: Running, world: None) -> None:
    pages = crawl(running)
    assert {"/", "/events", "/served"} <= set(pages)
    assert {f"/event/{n}" for n in range(1, 11)} <= set(pages)
    assert {f"/served/{n}" for n in range(1, 8)} <= set(pages)
    for path, response in pages.items():
        if response.status != 200:
            # only the links to the tampered ids fail (their ids are not valid, so 400)
            assert response.status == 400 and "%3C" in path, (path, response.status)
            continue
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert response.headers["content-length"] == str(len(response.raw))


def test_no_page_contains_injected_markup(running: Running, world: None) -> None:
    pages = crawl(running)
    assert len(pages) >= 25  # overview, lists, 10 events, 7 responses, per-source, per-agent
    for path, response in pages.items():
        assert_page_is_inert(path, response.body)
    # the hostile text is there, as text
    assert html.escape(XSS, quote=True) in pages["/"].body
    assert html.escape(BREAKOUT, quote=True) in pages["/events"].body


def test_home_page_sources_agents_and_recent_served(running: Running, world: None) -> None:
    body = running.ok("/").body
    assert "daemon not running (no heartbeat)" in body
    assert "latest event seq 10" in body
    sources = table_rows(section(body, "Sources"))
    assert [row[1] for row in sources] == [  # high first, then normal, then low; by id within
        "po-table",
        "sps-portal",
        '"><script>alert(5)</script>',
        "inbox",
        "fresh",
        "gone",
    ]
    by_id = {row[1]: row for row in sources}
    # times are shown to the second on the audit pages (the digest shows minutes)
    assert by_id["po-table"] == ["high", "po-table", "sql", "52", "2026-09-29T09:00:00Z", "ok"]
    assert by_id["inbox"][3:] == ["31", "2026-09-29T09:00:05Z", "ok"]
    assert by_id["fresh"][3:] == ["0", "never", "never collected"]
    assert by_id["gone"][3:] == ["3", "2026-09-27T09:00:00Z", "ok (not in config)"]
    portal = by_id["sps-portal"]
    assert portal[3:5] == ["20", "2026-09-29T06:00:00Z"]
    assert portal[5] == (
        'error since 2026-09-29T09:00:00Z; latest 2026-09-29T09:00:00Z: '
        '"<script>alert(1)</script> tail"'
    )

    agents = table_rows(section(body, "Agents"))
    assert "<th>cursor acked at</th>" in section(body, "Agents")
    assert [row[0] for row in agents] == ['"><script>alert(4)</script>', "alice", "bob"]
    # agent, cursor, cursor acked at, last served, responses served
    assert agents[1] == ["alice", "5", "2026-09-29T09:07:07Z", "2026-09-29T09:03:07Z", "3"]
    assert agents[2] == ["bob", "0", "never", "2026-09-29T09:06:07Z", "3"]  # never acked

    with Store.open() as store:
        served = store.list_served()
    recent = table_rows(section(body, "Recent responses served"))
    assert [row[0] for row in recent] == [f"#{e.id}" for e in served]  # newest first
    assert recent[-1][1:4] == ["2026-09-29T09:01:07Z", "alice", "since"]
    assert recent[-1][4] == "budget_tokens=800 source=null"
    assert '<a href="/served/7">#7</a>' in body
    assert '<a href="/served?agent=alice">alice</a>' in body
    assert '<a href="/events?source=inbox">inbox</a>' in body


def test_home_page_lists_only_the_20_most_recent_responses(running: Running) -> None:
    with Store.open() as store:
        for i in range(25):
            store.log_served("a", "since", {"i": i}, f"text {i}", "mcp", T0)
    recent = table_rows(section(running.ok("/").body, "Recent responses served"))
    assert [row[0] for row in recent] == [f"#{n}" for n in range(25, 5, -1)]


def test_an_empty_database_renders_every_page_without_a_config_file(running: Running) -> None:
    Store.open().close()  # what the daemon leaves before its first collection: schema, no rows
    assert not config_path().exists()
    home_page = running.ok("/").body
    assert "daemon not running (no heartbeat)" in home_page
    assert "latest event seq 0" in home_page
    assert home_page.count("none") == 3  # no sources, no agents, no responses
    for path in ("/events", "/served"):
        assert "none" in running.ok(path).body
    assert running.get("/event/1").status == 404
    assert running.get("/served/1").status == 404
    assert not config_path().exists()


# --- read-only: no database, an older or a newer schema -----------------------------------------

EVERY_PAGE = (
    "/",
    "/events",
    "/events?source=inbox",
    "/events?before=5",
    "/served",
    "/served?agent=alice",
    "/served?before=5",
    "/event/1",
    "/served/1",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unavailable_message(response: Resp) -> str:
    """The sentence of a 503 page, as a browser shows it."""
    assert response.status == 503, (response.status, response.body[:200])
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["content-length"] == str(len(response.raw))
    match = re.search(r'<p class="err">(.*?)</p>', response.body, re.DOTALL)
    assert match, response.body
    assert_page_is_inert("503", response.body)
    return html.unescape(match[1])


def test_a_missing_database_is_a_503_page_and_nothing_is_created(
    running: Running, since_home_dir: Path
) -> None:
    assert not since_home_dir.exists()
    expected = f"no database at {db_path()} — run `since daemon` or `since collect` first"
    for path in EVERY_PAGE:
        assert unavailable_message(running.get(path)) == expected, path
        head = running.get(path, method="HEAD")  # HEAD is a GET without the body
        assert (head.status, head.raw) == (503, b""), path
    assert not since_home_dir.exists()  # neither the home directory ...
    assert not db_path().exists()  # ... nor the database

    body = running.get("/").body
    assert "<h1>Since audit page</h1>" in body
    assert f'<p class="err">{html.escape(expected, quote=True)}</p>' in body

    # requests that are wrong whatever the database says stay 400/404, and create nothing either
    assert running.get("/nope").status == 404
    assert running.get("/events?before=x").status == 400
    assert not since_home_dir.exists()


def test_a_home_without_a_database_file_or_with_an_empty_one_gets_no_file(
    running: Running, since_home_dir: Path
) -> None:
    since_home_dir.mkdir()
    for path in EVERY_PAGE:
        assert unavailable_message(running.get(path)).startswith("no database at "), path
    assert list(since_home_dir.iterdir()) == []  # no since.db, no -wal, no -shm

    db_path().write_bytes(b"")  # e.g. a file some other tool left behind
    for path in EVERY_PAGE:
        assert unavailable_message(running.get(path)).startswith("no database at "), path
    assert db_path().read_bytes() == b""  # no schema was put into it
    assert [p.name for p in since_home_dir.iterdir()] == ["since.db"]


def test_an_older_schema_is_a_503_page_and_the_file_is_not_migrated(
    running: Running, since_home_dir: Path
) -> None:
    path = make_v2_database(since_home_dir)
    before = sha256(path)
    expected = "database schema v2 is older than this Since (v3); run the daemon once to migrate"
    for target in EVERY_PAGE:
        assert unavailable_message(running.get(target)) == expected, target
    assert sha256(path) == before  # byte for byte what it was: a GET never migrates
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as con:
        stored = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        columns = {row[1] for row in con.execute("PRAGMA table_info(sources)")}
    assert stored == ("2",)
    assert "announced_error" not in columns  # the v3 column was not added
    # what the message asks for works: once a read-write open (the daemon) migrated it, it is served
    Store.open().close()
    assert "portal" in running.ok("/").body


def test_a_newer_schema_is_a_503_page_and_the_file_is_untouched(
    running: Running, since_home_dir: Path
) -> None:
    with Store.open() as store:
        store.set_meta("schema_version", "99")
    before = sha256(db_path())
    expected = (
        "database schema v99 was written by a newer Since "
        "(this one understands up to v3); upgrade Since"
    )
    for target in EVERY_PAGE:
        assert unavailable_message(running.get(target)) == expected, target
    assert sha256(db_path()) == before


def test_a_current_database_keeps_its_bytes_through_a_crawl(running: Running, world: None) -> None:
    before = sha256(db_path())
    pages = crawl(running)
    assert len(pages) >= 25 and all(p.status != 503 for p in pages.values())
    for path in EVERY_PAGE:
        running.get(path)
    assert sha256(db_path()) == before


def test_daemon_line_says_what_status_says(running: Running) -> None:
    def line() -> str:
        match = re.search(r"<p><span[^>]*>(daemon[^<]*)</span>", running.ok("/").body)
        assert match
        return match[1]

    Store.open().close()  # the page reads an existing database; it never creates one
    assert line() == "daemon not running (no heartbeat)"
    with Store.open() as store:
        store.set_meta(META_HEARTBEAT, to_iso(NOW - timedelta(minutes=5)))
        store.set_meta(META_MIN_SCHEDULE, 900)
    assert line() == "daemon heartbeat 5m ago"
    assert '<span class="err">daemon heartbeat' not in running.ok("/").body
    with Store.open() as store:
        store.set_meta(META_HEARTBEAT, to_iso(NOW - timedelta(hours=3)))
    assert line() == "daemon heartbeat stale (3h ago; shortest schedule 15m)"
    assert '<span class="err">daemon heartbeat stale' in running.ok("/").body


# --- events ----------------------------------------------------------------------------------


def test_events_page_shows_the_digest_line_of_each_event_newest_first(
    running: Running, world: None
) -> None:
    body = running.ok("/events").body
    rows = table_rows(body)
    assert [row[0] for row in rows] == [str(n) for n in range(10, 0, -1)]
    with Store.open() as store:
        labels = {s.source_id: s.key_label for s in store.list_source_states()}
        for row in rows:
            event = store.get_event(int(row[0]))
            assert event is not None
            unreadable = event.seq == 10  # the baseline whose record_count is not a number
            expected = (
                "? unreadable baseline event"
                if unreadable
                else event_body(event, labels[event.source_id], DIGEST_CAP)
            )
            assert row[1:] == [
                event.created_at,
                event.source_id,
                expected,
                f"since://evt/{event.seq}",
            ]
    by_seq = {int(row[0]): row for row in rows}
    assert by_seq[4][3] == (
        '~ PO "4500123" status: "Open" -> "<script>alert(1)</script>"; '
        "<b>notes</b> changed (+300/-10 chars)"
    )
    assert by_seq[7][3].startswith('! source_error: "<script>alert(1)</script>"; needs a human: ')
    assert by_seq[9][3] == '? "<script>alert(1)</script>"'
    assert '<a href="/event/4">since://evt/4</a>' in body
    assert "older events" not in body and "newest events" not in body


def test_events_page_filters_by_source(running: Running, world: None) -> None:
    body = running.ok("/events?source=sps-portal").body
    assert [row[0] for row in table_rows(body)] == ["8", "7", "6"]
    assert "<h1>Events of sps-portal</h1>" in body
    assert '<a href="/events">all sources</a>' in body
    assert table_rows(running.ok("/events?source=inbox").body)[-1][0] == "1"
    empty = running.ok("/events?source=nothing-here").body  # a well-formed name that has no events
    assert table_rows(empty) == [] and "none" in empty


def test_events_pagination_goes_older_by_seq_and_keeps_the_source(running: Running) -> None:
    with Store.open() as store:
        for n in range(1, 121):
            store.append_event("a" if n % 2 else "b", "added", now=T0, record_key=f"k{n}")

    def seqs(path: str) -> list[int]:
        return [int(row[0]) for row in table_rows(running.ok(path).body)]

    first = running.ok("/events").body
    assert seqs("/events") == list(range(120, 70, -1))  # 50 per page
    assert '<a href="/events?before=71">older events</a>' in first
    assert "newest events" not in first
    assert seqs("/events?before=71") == list(range(70, 20, -1))
    second = running.ok("/events?before=71").body
    assert '<a href="/events?before=21">older events</a>' in second
    assert '<a href="/events">newest events</a>' in second
    last = running.ok("/events?before=21").body
    assert seqs("/events?before=21") == list(range(20, 0, -1))
    assert "older events" not in last

    only_a = seqs("/events?source=a")
    assert only_a == list(range(119, 19, -2))  # 60 events of source a: 50 on the first page
    assert len(only_a) == 50
    page = running.ok("/events?source=a").body
    assert f'<a href="/events?source=a&amp;before={only_a[-1]}">older events</a>' in page
    assert seqs(f"/events?source=a&before={only_a[-1]}") == list(range(19, 0, -2))
    assert (
        '<a href="/events?source=a">newest events</a>'
        in running.ok(f"/events?source=a&before={only_a[-1]}").body
    )


def test_event_page_is_the_get_text_plus_the_stored_data(running: Running, world: None) -> None:
    with Store.open() as store:
        service = Service(store, lambda: NOW)
        expected = {
            seq: service.get(f"since://evt/{seq}", agent_id="probe") for seq in range(1, 10)
        }
    for seq, text in expected.items():
        blocks = pres(running.ok(f"/event/{seq}").body)
        assert len(blocks) == 2
        assert blocks[0] == text, seq
        stored = json.loads(blocks[1].replace("\\u202e", RLO))  # the JSON view writes it visibly
        assert stored["seq"] == seq
    body = running.ok("/event/4").body
    assert "<h1>Event <code>since://evt/4</code></h1>" in body
    stored = json.loads(pres(body)[1])
    assert stored["kind"] == "modified" and stored["record_key"] == "4500123"
    assert stored["importance"] == 12 and stored["created_at"] == "2026-09-29T09:09:00Z"
    assert stored["field_changes"][1] == {
        "field": "<b>notes</b>",
        "old": None,
        "new": None,
        "added_chars": 300,
        "removed_chars": 10,
    }
    assert '<a href="/events?source=po-table">events of po-table</a>' in body
    # control/format characters are shown, not applied
    assert "\\u202e" in running.ok("/event/7").body and RLO not in running.ok("/event/7").body
    # an event whose stored data cannot be turned into text still gets a page
    damaged = running.ok("/event/10").body
    assert pres(damaged)[0] == "(this event's stored data cannot be rendered as text)"
    assert json.loads(pres(damaged)[1])["detail"] == {"record_count": "many"}


TAMPERED_ROWS = (  # (field_changes_json, detail_json): what a damaged or edited database can hold
    ("not json at all", "{}"),
    ("[]", '{"unfinished": '),
    ('{"a": 1}', "{}"),  # JSON of the wrong shape
    ('[{"no_field_key": 1}]', "{}"),
    ("[]", "<script>alert(1)</script>" + RLO),  # hostile text in a raw column
    ("[" * 100_000, "{}"),  # nested beyond what the JSON decoder accepts
)


def test_an_undecodable_event_row_is_listed_and_shown_raw(running: Running) -> None:
    insert = (
        "INSERT INTO events (source_id, kind, record_key, field_changes_json, importance, "
        "detail_json, created_at) VALUES ('inbox', 'added', 'k', ?, 3, ?, '2026-09-29T09:20:05Z')"
    )
    with Store.open() as store:
        store.append_event("inbox", "baseline", now=T0, detail={"record_count": 4})  # 1: fine
        for changes, detail in TAMPERED_ROWS:  # 2 .. 7
            store._conn.execute(insert, (changes, detail))
        store.append_event("inbox", "removed", now=T0, record_key="last")  # 8: fine

    listing = running.ok("/events").body
    rows = {int(row[0]): row for row in table_rows(listing)}
    assert sorted(rows) == list(range(1, 9))  # one bad row does not take the page down
    assert rows[1][3] == "= baseline: 4 records"
    assert rows[8][3] == '- "last" removed'
    for seq in range(2, 8):
        assert rows[seq] == [
            str(seq),
            "2026-09-29T09:20:05Z",
            "inbox",
            "? unreadable event",
            f"since://evt/{seq}",
        ]
    assert running.ok("/events?source=inbox").body.count("? unreadable event") == 6
    assert_page_is_inert("/events", listing)

    for seq, (changes, detail) in zip(range(2, 8), TAMPERED_ROWS, strict=True):
        body = running.ok(f"/event/{seq}").body
        served, stored = pres(body)
        assert served == "(this event's stored data cannot be decoded)"
        raw = json.loads(stored.replace("\\u202e", RLO))  # the JSON view writes it visibly
        assert raw == {  # the columns exactly as stored
            "seq": seq,
            "source_id": "inbox",
            "kind": "added",
            "record_key": "k",
            "importance": 3,
            "created_at": "2026-09-29T09:20:05Z",
            "field_changes_json": changes,
            "detail_json": detail,
        }
        assert '<a href="/events?source=inbox">events of inbox</a>' in body
        assert_page_is_inert(f"/event/{seq}", body)
    hostile = running.ok("/event/6").body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in hostile and "<script" not in hostile
    assert "\\u202e" in hostile  # shown, not applied
    assert running.ok("/event/1").body.count("<pre>") == 2  # the rest still has its normal page


def test_bytes_that_are_not_utf8_do_not_break_a_page(running: Running) -> None:
    with Store.open() as store:
        served_id = store.log_served("a", "since", {}, "x", "mcp", T0)
        store._conn.execute(
            "UPDATE served_log SET text = CAST(x'6f6bff6b' AS TEXT) WHERE id = ?", (served_id,)
        )
        store._conn.execute(
            "INSERT INTO events (source_id, kind, field_changes_json, importance, detail_json, "
            "created_at) VALUES ('inbox', 'baseline', '[]', 1, "
            "CAST(x'7b2261223a2022ff227d' AS TEXT), '2026-09-29T09:20:00Z')"
        )
    replacement = "�"  # what a byte that is not UTF-8 is read as
    assert pres(running.ok(f"/served/{served_id}").body) == [f"ok{replacement}k"]
    assert running.ok("/served").status == 200
    assert running.ok("/events").status == 200
    assert json.loads(pres(running.ok("/event/1").body)[1])["detail"] == {"a": replacement}


# --- served log ------------------------------------------------------------------------------


def test_served_page_lists_rows_newest_first_and_filters_by_agent(
    running: Running, world: None
) -> None:
    rows = table_rows(running.ok("/served").body)
    assert [row[0] for row in rows] == [f"#{n}" for n in range(7, 0, -1)]
    assert rows[-1][:6] == [
        "#1",
        "2026-09-29T09:01:07Z",
        "alice",
        "since",
        "budget_tokens=800 source=null",
        "mcp",
    ]
    assert rows[-1][6].startswith("~") and int(rows[-1][6][1:]) > 10
    assert rows[-2][3:6] == ["get", 'budget_tokens=1500 handle="since://evt/4"', "mcp"]
    assert rows[0][2:6] == [
        '"><script>alert(4)</script>',
        "since<b> x",
        'k="<script>alert(1)</script>" n=7',
        "cli<i>",  # the override is gone from every cell
    ]

    alice = running.ok("/served?agent=alice").body
    assert [row[0] for row in table_rows(alice)] == ["#3", "#2", "#1"]
    assert {row[2] for row in table_rows(alice)} == {"alice"}
    assert "<h1>Served responses of alice</h1>" in alice
    bob = table_rows(running.ok("/served?agent=bob").body)
    assert [row[0] for row in bob] == ["#6", "#5", "#4"]
    assert bob[0][4] == 'budget_tokens=800 source="<script>alert(1)</script>"'
    assert [row[3] for row in bob] == ["since error", "get error", "since"]  # #6 and #5 failed
    # #3 (a record the world never stored) failed too
    assert [row[3] for row in table_rows(alice)] == ["get error", "get", "since"]
    assert running.ok("/served?agent=nobody").body.count("none") == 1


def test_served_pagination_goes_older_by_id_and_keeps_the_agent(running: Running) -> None:
    with Store.open() as store:
        for n in range(1, 121):
            store.log_served("a" if n % 2 else "b", "since", {"n": n}, f"t{n}", "mcp", T0)

    def ids(path: str) -> list[int]:
        return [int(row[0][1:]) for row in table_rows(running.ok(path).body)]

    assert ids("/served") == list(range(120, 70, -1))
    assert '<a href="/served?before=71">older responses</a>' in running.ok("/served").body
    assert ids("/served?before=71") == list(range(70, 20, -1))
    assert ids("/served?before=21") == list(range(20, 0, -1))
    assert "older responses" not in running.ok("/served?before=21").body
    a_ids = ids("/served?agent=a")
    assert a_ids == list(range(119, 19, -2))
    older = f'<a href="/served?agent=a&amp;before={a_ids[-1]}">older responses</a>'
    assert older in running.ok("/served?agent=a").body
    assert ids(f"/served?agent=a&before={a_ids[-1]}") == list(range(19, 0, -2))
    newest = '<a href="/served?agent=a">newest responses</a>'
    assert newest in running.ok(f"/served?agent=a&before={a_ids[-1]}").body


def test_served_entry_page_shows_exactly_what_was_served(running: Running, world: None) -> None:
    with Store.open() as store:
        entries = store.list_served()
    assert len(entries) == 7
    for entry in entries:
        body = running.ok(f"/served/{entry.id}").body
        assert pres(body) == [entry.text], entry.id  # the only <pre>, exactly the stored text
        facts = dict(table_rows(body))
        assert facts["id"] == str(entry.id)
        assert facts["agent"] == calm(entry.agent_id)
        assert facts["tool"] == calm(entry.tool)
        assert facts["via"] == calm(entry.via)
        assert facts["served at"] == entry.at
        assert json.loads(facts["arguments"].replace("\\u202e", RLO)) == entry.args
        assert facts["size"].startswith(f"{len(entry.text)} characters, ~")
    tampered = running.ok(f"/served/{entries[0].id}").body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in tampered
    assert pres(tampered)[0].startswith("\n<script>alert(1)</script>\nraw <b>text</b>")
    assert pres(tampered)[0].endswith("'x'  \n\n")
    digest = running.ok("/served/1").body
    assert pres(digest)[0].startswith("since · agent=alice · events 1-8")
    assert '<a href="/served?agent=alice">alice</a>' in digest


def test_invisible_characters_in_a_served_text_get_a_second_escaped_view(
    running: Running,
) -> None:
    # Cc (NUL, BEL, tab), Cf (RLO, and a tag character beyond the BMP), Zl, Co: 7 characters
    text = "safe‮evil\x00\x07 tab\there \U000e0001\n<b>&amp;</b>"
    with Store.open() as store:
        served_id = store.log_served("a", "since", {}, text, "mcp", T0)
    body = running.ok(f"/served/{served_id}").body
    exact, escaped = pres(body)
    assert exact == text  # the served text itself is still exact
    assert escaped == (
        "safe⟨U+202E⟩evil⟨U+0000⟩⟨U+0007⟩ tab⟨U+0009⟩here⟨U+2028⟩⟨U+E000⟩⟨U+E0001⟩\n<b>&amp;</b>"
    )
    note = "contains 7 invisible characters (shown as ⟨U+XXXX⟩ in the escaped view below)"
    assert f'<p class="err">{note}</p>' in body
    assert body.index(note) < body.index("<pre>")  # the line is above the text
    assert body.count("<pre>") == 2
    assert "&lt;b&gt;&amp;amp;&lt;/b&gt;" in body  # both views are escaped like all data
    # nothing to spell out in a text of visible characters (a newline is visible enough)
    with Store.open() as store:
        plain = store.log_served("a", "since", {}, "one\ntwo <b>", "mcp", T0)
    plain_body = running.ok(f"/served/{plain}").body
    assert pres(plain_body) == ["one\ntwo <b>"] and "invisible" not in plain_body


def test_a_text_that_is_only_invisible_characters_is_counted_and_spelled_out(
    running: Running,
) -> None:
    with Store.open() as store:
        served_id = store.log_served("a", "since", {}, "\r\n​", "mcp", T0)
    body = running.ok(f"/served/{served_id}").body
    assert "contains 2 invisible characters (shown as ⟨U+XXXX⟩" in body  # CR and ZWSP; not LF
    assert pres(body) == ["\r\n​", "⟨U+000D⟩\n⟨U+200B⟩"]


def test_served_responses_that_start_with_error_get_an_error_badge(running: Running) -> None:
    texts = {
        "error: unknown source": True,
        "error: ": True,
        "since · agent=a · events 1-1 (1)": False,
        " error: leading space": False,
        "error:no space": False,
        "Error: capitalised": False,
        "the error: is in the middle": False,
    }
    ids = {}
    with Store.open() as store:
        for text in texts:
            ids[text] = store.log_served("a", "get", {"handle": "x"}, text, "mcp", T0)
    badge = '<span class="badge">error</span>'

    for path in ("/served", "/"):  # the list, and the recent responses on the overview
        body = running.ok(path).body
        fragment = section(body, "Recent responses") if path == "/" else body
        rows = {int(row[0][1:]): row for row in table_rows(fragment)}
        for text, is_error in texts.items():
            assert rows[ids[text]][3] == ("get error" if is_error else "get"), (path, text)
        assert body.count(badge) == 2, path
    only_agent = running.ok("/served?agent=a").body
    assert only_agent.count(badge) == 2

    for text, is_error in texts.items():
        body = running.ok(f"/served/{ids[text]}").body
        facts = dict(table_rows(body))
        assert ("result" in facts) == is_error, text
        assert (badge in body) == is_error, text
        if is_error:
            assert facts["result"] == "error"


def test_a_long_served_text_is_shown_whole(running: Running) -> None:
    text = "line one\nline two\n" + "x" * 50_000 + "\n<end>"
    with Store.open() as store:
        served_id = store.log_served("a", "since", {}, text, "mcp", T0)
    body = running.ok(f"/served/{served_id}").body
    assert pres(body) == [text]
    assert "invisible" not in body  # nothing to spell out: one view only


def test_the_ui_never_writes(running: Running, world: None) -> None:
    def state() -> tuple[Any, ...]:
        with Store.open() as store:
            return (
                store.list_served(limit=1000),
                store.events_after(0),
                store.list_agents(),
                store.list_source_states(),
                store.get_meta("last_pruned_at"),
            )

    before = state()
    crawl(running)  # opens every event page, which would log a response if it used ``get``
    assert state() == before


# --- headers, Host, methods, paths, queries ---------------------------------------------------


def test_every_response_carries_the_security_headers(running: Running, world: None) -> None:
    paths = [
        "/", "/events", "/event/1", "/served", "/served/1",  # 200
        "/nope", "/event/999", "/served/999",  # 404
        "/events?before=x", "/?x=1",  # 400
    ]  # fmt: skip
    responses = [running.get(p) for p in paths]
    responses.append(running.get("/", method="POST"))  # 405
    responses.append(running.get("/", host="evil.example"))  # 421
    assert [r.status for r in responses] == [200] * 5 + [404] * 3 + [400] * 2 + [405, 421]
    for path, response in zip([*paths, "POST /", "bad host"], responses, strict=True):
        assert response.headers["content-security-policy"] == CSP, path
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["x-frame-options"] == "DENY", path
        assert response.headers["referrer-policy"] == "no-referrer", path
        assert response.headers["cache-control"] == "no-store", path
        assert "python" not in response.headers["server"].lower(), path
    assert "<script" not in "".join(r.body for r in responses).lower()


def test_the_host_header_must_name_this_server(running: Running, world: None) -> None:
    port = running.port
    assert running.get("/", host=f"127.0.0.1:{port}").status == 200
    assert running.get("/", host=f"localhost:{port}").status == 200
    assert running.get("/event/4", host=f"localhost:{port}").status == 200
    for bad in (
        "127.0.0.1",
        "localhost",
        f"127.0.0.1:{port + 1}",
        f"localhost:{port + 1}",
        f"evil.example:{port}",
        "evil.example",
        f"localhost:{port}.evil.example",
        f"127.0.0.1:{port}.evil.example",
        f"[::1]:{port}",
        f" 127.0.0.1:{port}x",
        "",
    ):
        response = running.get("/", host=bad)
        assert response.status == 421, bad
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert response.body == "421 Misdirected Request: unexpected Host header\n"
    assert running.get("/", host=None).status == 421  # no Host header at all
    assert running.get("/", host=[f"127.0.0.1:{port}", f"127.0.0.1:{port}"]).status == 421
    assert running.get("/", host=[f"127.0.0.1:{port}", "evil.example"]).status == 421
    # the Host check comes before routing and before the method check
    assert running.get("/nope", host="evil.example").status == 421
    assert running.get("/", method="POST", host="evil.example").status == 421


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "FOO"])
def test_only_get_and_head_are_answered(running: Running, world: None, method: str) -> None:
    response = running.get("/", method=method)
    assert response.status == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert response.body == "405 Method Not Allowed\n"


def test_head_has_the_headers_of_get_and_no_body(running: Running, world: None) -> None:
    for path in ("/", "/events", "/event/4", "/served/1", "/nope"):
        got = running.get(path)
        head = running.get(path, method="HEAD")
        assert head.status == got.status
        assert head.raw == b""
        assert head.headers["content-length"] == got.headers["content-length"] == str(len(got.raw))
        assert head.headers["content-type"] == got.headers["content-type"]
        assert head.headers["content-security-policy"] == CSP


@pytest.mark.parametrize(
    "path",
    [
        "/nope",
        "/index.html",
        "/favicon.ico",
        "//evil.example/x",
        "/events/",
        "/event",
        "/event/",
        "/event/abc",
        "/event/0",
        "/event/007",
        "/event/-1",
        "/event/4/",
        "/event/4/extra",
        "/event/%34",
        "/event/9999",  # well-formed, no such event
        "/event/9223372036854775808",  # beyond SQLite's integer range
        "/event/99999999999999999999999",
        "/served/0",
        "/served/abc",
        "/served/9999",
        "/served/9223372036854775808",
        "/served/1/",
        "/../etc/passwd",
        "/%2e%2e/etc/passwd",
        "/EVENTS",
    ],
)
def test_unknown_paths_and_ids_are_404(running: Running, world: None, path: str) -> None:
    response = running.get(path)
    assert response.status == 404, path
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.body == "404 Not Found\n"


@pytest.mark.parametrize(
    "path",
    [
        "/events?before=abc",
        "/events?before=",
        "/events?before=0",
        "/events?before=-1",
        "/events?before=007",
        "/events?before=1.5",
        "/events?before=%EF%BC%91",  # a full-width digit
        "/events?before=9223372036854775808",
        "/events?before=1&before=2",
        "/events?before",
        "/events?before=%ff",  # not valid UTF-8
        "/events?source=",
        "/events?source=UPPER",
        "/events?source=%3Cscript%3E",
        "/events?source=a%20b",
        "/events?source=a+b",
        "/events?source=inbox&source=inbox",
        "/events?agent=alice",
        "/events?x=1",
        "/events?source=inbox&",
        "/events?" + "&".join(f"before={n}" for n in range(1, 12)),
        "/served?agent=",
        "/served?agent=has%20space",
        "/served?agent=%22%3E%3Cscript%3E",
        "/served?before=abc",
        "/served?source=inbox",
        "/?x=1",
        "/?before=1",
        "/event/4?x=1",
        "/served/1?agent=alice",
    ],
)
def test_malformed_query_parameters_are_400(running: Running, world: None, path: str) -> None:
    response = running.get(path)
    assert response.status == 400, path
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert re.fullmatch(r"400 Bad Request: [a-z ]+\n", response.body), response.body


def test_binds_to_loopback_only(running: Running) -> None:
    assert running.server.server_address[0] == "127.0.0.1"
    assert running.server.socket.getsockname()[0] == "127.0.0.1"
    assert running.server.allowed_hosts == {
        f"127.0.0.1:{running.port}",
        f"localhost:{running.port}",
    }


# --- errors and the write lock -----------------------------------------------------------------


def test_a_server_side_error_is_500_without_details_and_one_line_on_stderr(
    running: Running,
    world: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(*args: object) -> str:
        raise RuntimeError("secret detail C:\\Users\\x\\since.db\nsecond line")

    monkeypatch.setattr(ui, "_home_page", boom)
    response = running.get("/")
    assert response.status == 500
    assert response.body == "500 Internal Server Error\n"
    assert response.headers["content-security-policy"] == CSP
    err = capsys.readouterr().err
    assert err.startswith('since ui: GET "/" failed: RuntimeError: ')
    assert "secret detail" in err  # the operator sees what went wrong ...
    assert err.count("\n") == 1  # ... on one line, and there is no traceback
    assert "Traceback" not in err
    assert running.get("/events").status == 200  # the server keeps going


def test_an_unusable_database_is_500_not_a_traceback(
    running: Running, since_home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    since_home_dir.mkdir()
    (since_home_dir / "since.db").write_bytes(b"this is not a database" * 100)
    response = running.get("/")
    assert response.status == 500
    assert response.body == "500 Internal Server Error\n"
    err = capsys.readouterr().err
    assert err.startswith('since ui: GET "/" failed: ') and err.count("\n") == 1


def test_pages_load_while_another_connection_holds_the_write_lock(
    running: Running, world: None, since_home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 200)  # a page that wanted the lock would fail
    holder = sqlite3.connect(db_path(), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")  # e.g. the daemon in the middle of a collection
        started = time.monotonic()
        for path in ("/", "/events", "/event/4", "/served", "/served/1", "/served?agent=alice"):
            assert running.get(path).status == 200, path
        assert time.monotonic() - started < 5
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_requests_are_served_concurrently(running: Running, world: None) -> None:
    results: list[int] = []

    def worker() -> None:
        for _ in range(5):
            results.append(running.get("/").status)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert results == [200] * 30


# --- the command -------------------------------------------------------------------------------


def test_cli_ui_prints_the_url_and_serves_it(
    world: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[ui.AuditServer] = []

    class Recording(ui.AuditServer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(ui, "AuditServer", Recording)
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(main(["ui", "--port", "0"])))
    thread.daemon = True
    thread.start()
    out = ""
    deadline = time.monotonic() + 10
    while "Ctrl-C" not in out and time.monotonic() < deadline:
        out += capsys.readouterr().out
        time.sleep(0.02)
    match = re.fullmatch(r"Since audit page: http://127\.0\.0\.1:(\d+)/ \(Ctrl-C to stop\)\n", out)
    assert match, out
    port = int(match[1])
    assert port == started[0].port and port != 0
    try:
        page = fetch(port, "/")
        assert page.status == 200 and "<h1>Since audit page</h1>" in page.body
        assert "inbox" in page.body  # from the database; no config file exists
    finally:
        started[0].shutdown()
        thread.join(10)
    assert result == [0]
    assert not thread.is_alive()
    assert started[0].socket.fileno() == -1  # the listening socket was closed
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert not config_path().exists()


def test_serve_ends_with_exit_0_on_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    made: list[ui.AuditServer] = []

    class Interrupted(ui.AuditServer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(ui, "AuditServer", Interrupted)
    assert main(["ui", "--port", "0"]) == 0
    assert capsys.readouterr().out.startswith("Since audit page: http://127.0.0.1:")
    assert made[0].socket.fileno() == -1


def test_cli_ui_port_in_use_exits_1_with_a_short_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        code = main(["ui", "--port", str(port)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"error: port {port} is already in use (try --port 0 for a free port)\n"


@pytest.mark.parametrize("port", ["70000", "-1"])
def test_cli_ui_rejects_a_port_out_of_range(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ui", "--port", port]) == 2
    assert capsys.readouterr().err == "error: --port must be between 0 and 65535\n"


def test_cli_ui_default_port() -> None:
    assert build_parser().parse_args(["ui"]).port == 8737 == ui.DEFAULT_PORT
    assert build_parser().parse_args(["ui", "--port", "0"]).port == 0


def test_cli_ui_passes_the_port_to_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(ui, "serve", lambda port: calls.append(port) or 0)
    assert main(["ui", "--port", "9123"]) == 0
    assert calls == [9123]
