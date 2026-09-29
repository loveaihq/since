"""The benchmark world and its answer key (``bench/world.py``, M3 T2).

Four layers: the world is deterministic and big enough; its states behave over time (ticks, flags,
layout change, login expiry, notes); the four rules are tested in isolation on hand-built data;
and the answer key of the generated world is the rules' output, which reproduces what the
generator meant, with decoys that the rules reject.
"""

from __future__ import annotations

import csv
import email
import email.policy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from web_site import CHANNEL, Site

from bench.world import (
    BUSINESS_KINDS,
    DEFAULT_SEED,
    ETA_SLIP_DAYS,
    FLAG_ANSWERED,
    FLAG_FLAGGED,
    FLAG_SEEN,
    FOLDER,
    KIND_EMAIL,
    KIND_PO,
    KIND_PORTAL,
    KIND_SYSTEM,
    LAST_LOOK,
    LAYOUT_CHANGE_AT,
    LOGIN_EXPIRY_AT,
    NOW,
    OUR_DOMAIN,
    PORTAL_LOGIN_SELECTOR,
    PORTAL_V1_EXTRACT,
    START,
    SYSTEM_LAYOUT,
    SYSTEM_LOGIN,
    FlagEvent,
    Mail,
    PortalState,
    RowEvent,
    World,
    WorldState,
    build_world,
    compute_planted,
    email_items,
    make_ticks,
    po_items,
    portal_html,
    portal_items,
    portal_text,
    summary,
    system_items,
)

ROOT = Path(__file__).resolve().parents[1]
HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)


@pytest.fixture(scope="module")
def world() -> World:
    return build_world()


# -- hand-built data for the rules ---------------------------------------------------------------


def mk_mail(
    uid: int = 1,
    *,
    kind: str = "customer",
    needs_action: bool = True,
    received: datetime = LAST_LOOK + HOUR,
    ref: str | None = "9100001",
    flags: tuple[str, ...] = (),
) -> Mail:
    return Mail(
        folder=FOLDER,
        uid=uid,
        message_id=f"<m{uid}@example.test>",
        received=received,
        date_header="Tue, 15 Sep 2026 10:00:00 +0000",
        from_name="Someone",
        from_addr="someone@example.test",
        to="ops@ourco.example",
        subject=f"Subject {ref}",
        body="Body",
        flags=flags,
        sender_kind=kind,
        needs_action=needs_action,
        ref=ref,
    )


def po(po_no: str, status: str = "Open", eta: str = "2026-10-01", qty: int = 100) -> dict[str, Any]:
    return {
        "po_no": po_no,
        "supplier": "Pacific Ceramics",
        "status": status,
        "eta": eta,
        "qty": qty,
        "updated_at": "2026-09-01T00:00:00Z",
    }


def orders(*pairs: tuple[str, str], layout: str = "v1", login: bool = False) -> PortalState:
    rows = [
        {"order_no": no, "customer": "Hartwell & Sons", "status": st, "ship_by": "2026-10-01"}
        for no, st in pairs
    ]
    return PortalState(rows=rows, layout=layout, login_expired=login)


def state(
    t: datetime = NOW,
    mails: list[Mail] | None = None,
    po_rows: list[dict[str, Any]] | None = None,
    portal: PortalState | None = None,
) -> WorldState:
    return WorldState(t=t, mails=mails or [], po_rows=po_rows or [], portal=portal or orders())


# -- time and ticks ------------------------------------------------------------------------------


def test_the_stated_times() -> None:
    assert START == datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    assert LAST_LOOK == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    assert NOW == datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    assert LAYOUT_CHANGE_AT == datetime(2026, 9, 15, 22, 0, tzinfo=UTC)
    assert LOGIN_EXPIRY_AT == datetime(2026, 9, 16, 7, 0, tzinfo=UTC)
    assert DEFAULT_SEED == 20260916


def test_ticks_every_two_hours_plus_last_look_and_now(world: World) -> None:
    grid = {START + i * timedelta(hours=2) for i in range(30)}
    assert grid.issuperset({NOW})
    expected = sorted(grid | {LAST_LOOK, NOW})
    assert world.ticks == expected == make_ticks()
    assert len(world.ticks) == len(set(world.ticks)) == 31
    assert world.ticks == sorted(world.ticks)
    assert world.ticks[0] == START and world.ticks[-1] == NOW
    assert LAST_LOOK in world.ticks and LAST_LOOK not in grid  # 09:00 is off the 2-hour grid
    assert all(t.tzinfo is not None and t.utcoffset() == timedelta(0) for t in world.ticks)


# -- determinism ---------------------------------------------------------------------------------


def test_same_seed_same_world_byte_for_byte() -> None:
    a, b = build_world(), build_world(DEFAULT_SEED)
    assert a == b
    assert a.to_json() == b.to_json()
    assert repr(a) == repr(b)
    for t in a.ticks:
        assert repr(a.state_at(t)) == repr(b.state_at(t))
    assert a.notes() == b.notes()
    assert a.planted == b.planted and a.decoys == b.decoys
    json.loads(a.to_json())  # a valid dump


def test_a_different_seed_gives_a_different_world(world: World) -> None:
    other = build_world(DEFAULT_SEED + 1)
    assert other.seed != world.seed
    assert other.to_json() != world.to_json()
    assert other.mails != world.mails
    assert other.po_initial != world.po_initial


def test_the_dump_does_not_depend_on_the_hash_seed() -> None:
    """Sets and dicts must never leak their (per-process) ordering into the world."""
    code = (
        "import hashlib; from bench.world import build_world; "
        "print(hashlib.sha256(build_world().to_json().encode()).hexdigest())"
    )
    digests = set()
    for hash_seed in ("1", "424242"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        digests.add(result.stdout.strip())
    assert len(digests) == 1
    assert digests == {hashlib.sha256(build_world().to_json().encode()).hexdigest()}


@pytest.mark.parametrize("seed", [0, 1, 7, 12345])
def test_other_seeds_are_complete_worlds_too(seed: int) -> None:
    """The scenarios do not depend on lucky draws: any seed reproduces the intended key."""
    w = build_world(seed)
    intended = sorted((s.kind, s.ref) for s in w.scenarios if s.role == "planted")
    assert sorted(w.planted) == intended
    assert len(w.mails) >= 200
    assert len(w.state_at(START).po_rows) >= 50
    assert {k for k, _, _ in w.decoys} == {KIND_EMAIL, KIND_PO, KIND_PORTAL, KIND_SYSTEM}


# -- size and content ----------------------------------------------------------------------------


def test_enough_data_spread_over_three_days(world: World) -> None:
    now = world.state_at(NOW)
    assert len(now.mails) >= 200 and len(world.mails) == len(now.mails)
    per_day = Counter(m.received.date().isoformat() for m in world.mails)
    assert set(per_day) == {"2026-09-14", "2026-09-15", "2026-09-16"}
    assert per_day["2026-09-14"] > 50 and per_day["2026-09-15"] > 50 and per_day["2026-09-16"] > 20
    assert all(len(world.state_at(t).po_rows) >= 50 for t in world.ticks)
    assert len(now.portal.rows) == 20
    po_days = Counter(e.t.date().isoformat() for e in world.po_events)
    assert set(po_days) == {"2026-09-14", "2026-09-15", "2026-09-16"}
    assert all(n >= 5 for n in po_days.values())
    assert world.state_at(START).po_rows != now.po_rows


def test_mostly_noise_with_every_kind_of_sender(world: World) -> None:
    kinds = Counter(m.sender_kind for m in world.mails)
    business = sum(kinds[k] for k in BUSINESS_KINDS)
    assert business < len(world.mails) / 4
    for kind in ("newsletter", "promo", "saas", "colleague", "customer", "supplier"):
        assert kinds[kind] >= 15
    assert not any(m.needs_action for m in world.mails if m.sender_kind == "newsletter")
    # some noise looks like it demands action, so the rule cannot ignore who the sender is
    demanding = Counter(m.sender_kind for m in world.mails if m.needs_action)
    assert {"colleague", "promo", "saas"} <= set(demanding)


def test_mail_model(world: World) -> None:
    mails = world.mails
    assert [m.uid for m in mails] == list(range(1, len(mails) + 1))  # per folder, ascending
    assert all(m.folder == FOLDER for m in mails)
    assert [m.received for m in mails] == sorted(m.received for m in mails)
    assert all(START <= m.received <= NOW for m in mails)
    assert len({m.message_id for m in mails}) == len(mails)
    assert all(m.flags == () for m in mails)  # as delivered; flags change through events
    # nothing lands close enough to the last look to make "after" ambiguous
    assert all(abs(m.received - LAST_LOOK) >= timedelta(minutes=10) for m in mails)
    for m in mails:
        domain = m.from_addr.split("@", 1)[1]
        assert (domain == OUR_DOMAIN) == (m.sender_kind == "colleague"), m.from_addr
        sent = parsedate_to_datetime(m.date_header)
        assert timedelta(seconds=3) <= m.received - sent <= timedelta(minutes=4)
        for text in (m.subject, m.body, m.from_name, m.from_addr, m.to, m.message_id):
            assert text.isascii() and "\r" not in text
        assert "\n" not in m.subject and m.body.strip()
        assert m.size == len(m.raw_header()) + len(m.body.encode("ascii"))


def test_the_raw_header_parses_like_a_real_one(world: World) -> None:
    for m in world.mails[:40]:
        parsed = email.message_from_bytes(m.raw_header(), policy=email.policy.default)
        assert parsed["Message-ID"] == m.message_id
        assert parsed["Subject"] == m.subject
        assert parsed["Date"] == m.date_header
        (sender,) = parsed["From"].addresses
        assert (sender.display_name, sender.addr_spec) == (m.from_name, m.from_addr)
    assert m.raw_header().endswith(b"\r\n\r\n")


def test_business_mails_carry_unique_reference_numbers(world: World) -> None:
    business = [m for m in world.mails if m.sender_kind in BUSINESS_KINDS]
    assert business and all(m.ref and m.ref.isdigit() and m.ref in m.subject for m in business)
    assert len({m.ref for m in business}) == len(business)
    assert all(m.ref is None for m in world.mails if m.sender_kind not in BUSINESS_KINDS)
    # a subject carries exactly one number (the reference), so a reader cannot pick the wrong one
    for m in business:
        assert re.findall(r"\d+", m.subject) == [m.ref]
    # ... and no reference is also a PO number or a portal order number
    identifiers = [m.ref for m in business]
    identifiers += [r["po_no"] for r in world.po_initial] + [e.key for e in world.po_events]
    identifiers += [r["order_no"] for r in world.portal_initial]
    unique_po_numbers = {r["po_no"] for r in world.po_initial} | {
        e.key for e in world.po_events if e.created
    }
    expected = len(business) + len(unique_po_numbers) + len(world.portal_initial)
    assert len(set(identifiers)) == expected
    assert all(x.isdigit() for x in identifiers)


def test_action_and_quiet_business_mail_read_differently(world: World) -> None:
    """Every business mail has a subject a reader can classify (no numbers-only or vague ones)."""
    asks = [m for m in world.mails if m.sender_kind in BUSINESS_KINDS and m.needs_action]
    quiet = [m for m in world.mails if m.sender_kind in BUSINESS_KINDS and not m.needs_action]
    assert len(asks) >= 10 and len(quiet) >= 30
    ask_patterns = "rejected|mismatch|please|held at customs|overdue|Quality issue|short shipment"
    ask_patterns += "|Chargeback|damaged|approval|needed"
    assert all(re.search(ask_patterns, m.subject, re.IGNORECASE) for m in asks)
    quiet_words = "confirmed|paid|delivered|noted|Statement|departed|for your records|attached"
    quiet_words += "|Order confirmation|accepted"
    assert all(re.search(quiet_words, m.subject, re.IGNORECASE) for m in quiet)
    assert all(re.search(r"No (action|reply)|nothing", m.body, re.IGNORECASE) for m in quiet)


# -- states over time ----------------------------------------------------------------------------


def test_state_at_lists_the_mails_received_by_then(world: World) -> None:
    counts = [len(world.state_at(t).mails) for t in world.ticks]
    assert counts == sorted(counts) and counts[0] < counts[-1]
    for t in (START, LAST_LOOK, NOW):
        assert [m.uid for m in world.state_at(t).mails] == [
            m.uid for m in world.mails if m.received <= t
        ]
    assert world.state_at(START - SECOND).mails == []
    assert len(world.state_at(NOW).mails) == len(world.mails)
    assert world.state_at(LAST_LOOK).t == LAST_LOOK


def test_flags_change_over_time(world: World) -> None:
    before, after = world.state_at(LAST_LOOK), world.state_at(NOW)
    then = {m.uid: m.flags for m in before.mails}
    changed = {m.uid: m.flags for m in after.mails if m.uid in then and m.flags != then[m.uid]}
    assert len(changed) >= 10  # mails already there at the last look whose flags moved on
    assert any(FLAG_ANSWERED in flags for flags in changed.values())
    assert any(FLAG_FLAGGED in flags for flags in changed.values())
    assert any(FLAG_SEEN in flags for flags in then.values())
    assert all(list(m.flags) == sorted(m.flags) for m in after.mails)
    mails = {m.key: m for m in world.mails}
    for e in world.flag_events:
        assert mails[(e.folder, e.uid)].received < e.t <= NOW
    # a flag never disappears, and planted mail is left alone
    assert all(set(then[m.uid]) <= set(m.flags) for m in after.mails if m.uid in then)
    planted_refs = {r for k, r in world.planted if k == KIND_EMAIL}
    planted_uids = {m.uid for m in world.mails if m.ref in planted_refs}
    assert planted_uids and not planted_uids & {e.uid for e in world.flag_events}


def test_state_at_returns_copies(world: World) -> None:
    first = world.state_at(NOW)
    first.po_rows[0]["status"] = "Tampered"
    first.portal.rows[0]["status"] = "Tampered"
    first.mails.clear()
    again = world.state_at(NOW)
    assert again.po_rows[0]["status"] != "Tampered"
    assert again.portal.rows[0]["status"] != "Tampered"
    assert again.mails


def test_po_rows_over_time(world: World) -> None:
    start, now = world.state_at(START), world.state_at(NOW)
    assert [r["po_no"] for r in now.po_rows] == sorted(r["po_no"] for r in now.po_rows)
    assert all(
        tuple(r) == ("po_no", "supplier", "status", "eta", "qty", "updated_at") for r in now.po_rows
    )
    created = [e for e in world.po_events if e.created]
    assert len(now.po_rows) == len(start.po_rows) + len(created)
    for e in created:  # a new PO exists exactly from its creation on
        assert e.key not in {r["po_no"] for r in world.state_at(e.t - SECOND).po_rows}
        assert e.key in {r["po_no"] for r in world.state_at(e.t).po_rows}
    change = next(e for e in world.po_events if not e.created)
    row = next(r for r in world.state_at(change.t).po_rows if r["po_no"] == change.key)
    assert row["updated_at"] == change.t.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert all(row[k] == v for k, v in change.changes.items())
    assert all(r["eta"] and r["qty"] > 0 for r in now.po_rows)


def test_the_layout_changes_and_the_login_expires_at_the_stated_times(world: World) -> None:
    def portal_at(t: datetime) -> PortalState:
        return world.state_at(t).portal

    assert (portal_at(START).layout, portal_at(START).login_expired) == ("v1", False)
    assert portal_at(LAST_LOOK).layout == "v1"
    assert portal_at(LAYOUT_CHANGE_AT - SECOND).layout == "v1"
    assert portal_at(LAYOUT_CHANGE_AT).layout == "v2"
    assert not portal_at(LOGIN_EXPIRY_AT - SECOND).login_expired
    assert portal_at(LOGIN_EXPIRY_AT).login_expired
    assert (portal_at(NOW).layout, portal_at(NOW).login_expired) == ("v2", True)
    by_tick = [(t, portal_at(t).layout, portal_at(t).login_expired) for t in world.ticks]
    assert [x for x in by_tick if x[1] == "v2"][0][0] == LAYOUT_CHANGE_AT
    assert [x for x in by_tick if x[2]][0][0] == LOGIN_EXPIRY_AT + HOUR  # first tick after 07:00
    # the true order data keeps changing after the page stopped showing it
    late = [e for e in world.portal_events if e.t > LOGIN_EXPIRY_AT]
    assert late and world.state_at(NOW).portal.rows != portal_at(LOGIN_EXPIRY_AT).rows


# -- the portal page -----------------------------------------------------------------------------


class Dom(HTMLParser):
    """Just enough of a DOM: tables by id (tbody rows -> cell texts), form ids, visible text."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[str]]] = {}
        self.forms: set[str] = set()
        self.scripts = 0
        self.text: list[str] = []
        self._table = ""
        self._tbody = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._title = False
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            self._table = a.get("id") or ""
            self.tables[self._table] = []
        elif tag == "tbody":
            self._tbody = True
        elif tag == "tr" and self._tbody:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []
        elif tag == "form":
            self.forms.add(a.get("id") or "")
        elif tag == "title":
            self._title = True
        elif tag == "script":
            self.scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables[self._table].append(self._row)
            self._row = None
        elif tag == "tbody":
            self._tbody = False
        elif tag == "title":
            self._title = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if not self._title and data.strip():
            self.text.append(data)


def visible(source: str) -> str:
    return " ".join(" ".join(Dom(source).text).split())


def flat(text: str) -> str:
    return " ".join(text.replace("|", " ").split())


def test_the_v1_page_matches_the_extractor_config(world: World) -> None:
    portal = world.state_at(LAST_LOOK).portal
    dom = Dom(portal_html(portal))
    config = PORTAL_V1_EXTRACT
    assert set(config) == {"container", "rows", "key", "fields"}
    assert config["container"] == "table#orders" and config["rows"] == "table#orders tbody tr"
    assert config["key"] in config["fields"]
    rows = dom.tables["orders"]
    assert len(rows) == len(portal.rows) == 20
    for cells, row in zip(rows, portal.rows, strict=True):
        for name, selector in config["fields"].items():
            match = re.fullmatch(r"td:nth-child\((\d)\)", selector)
            assert match
            assert cells[int(match.group(1)) - 1] == row[name]
    assert not dom.forms and not dom.scripts


def test_the_v2_page_breaks_the_v1_selectors(world: World) -> None:
    portal = world.state_at(LAYOUT_CHANGE_AT).portal
    page = portal_html(portal)
    dom = Dom(page)
    assert "orders" not in dom.tables  # the container `table#orders` is gone ...
    assert len(dom.tables["order-list"]) == 20  # ... the same data sits in a renamed table
    assert dom.tables["order-list"][0][0] == portal.rows[0]["customer"]  # columns reordered
    assert 'id="orders"' not in page
    assert portal_html(portal) != portal_html(world.state_at(LAST_LOOK).portal)


def test_the_login_page(world: World) -> None:
    page = portal_html(world.state_at(NOW).portal)
    dom = Dom(page)
    assert PORTAL_LOGIN_SELECTOR == "form#login" and "login" in dom.forms
    assert not dom.tables
    assert not any(row["order_no"] in page for row in world.state_at(NOW).portal.rows)
    assert "Session expired" in portal_text(world.state_at(NOW).portal)


def test_visible_text_matches_the_page(world: World) -> None:
    for t in (LAST_LOOK, LAYOUT_CHANGE_AT, NOW):
        portal = world.state_at(t).portal
        assert flat(portal_text(portal)) == visible(portal_html(portal))
    text = portal_text(world.state_at(LAST_LOOK).portal)
    assert text.startswith("Customer orders\nOrder | Customer | Status | Ship by\n")
    assert len(text.splitlines()) == 22 and "\r" not in text


def test_portal_values_are_escaped_in_the_page() -> None:
    portal = PortalState(
        rows=[
            {
                "order_no": "1",
                "customer": '<script>alert(1)</script> & "Sons"',
                "status": "Open",
                "ship_by": "2026-10-01",
            }
        ],
        layout="v1",
        login_expired=False,
    )
    for layout in ("v1", "v2"):
        page = portal_html(PortalState(portal.rows, layout, False))
        dom = Dom(page)
        assert dom.scripts == 0 and "<script>" not in page
        assert "&lt;script&gt;" in page and "&amp;" in page
        assert '<script>alert(1)</script> & "Sons"' in visible(page)
    with pytest.raises(ValueError):
        portal_html(PortalState(portal.rows, "v3", False))
    with pytest.raises(ValueError):
        portal_text(PortalState(portal.rows, "v3", False))


def test_the_extractor_config_works_in_a_browser_on_v1_and_breaks_on_v2(
    site: Site, world: World
) -> None:
    from since.config import SourceConfig
    from since.sources import LoginRequired
    from since.sources.web import WebCollector

    def collect(page: str) -> Any:
        site.write("orders.html", page)
        options: dict[str, Any] = {
            "url": site.url("/orders"),
            "extract": deepcopy(PORTAL_V1_EXTRACT),
            "login_detect": {"selector": PORTAL_LOGIN_SELECTOR},
            "timeout_s": 20,
        }
        if CHANNEL:
            options["browser_channel"] = CHANNEL
        cfg = SourceConfig(id="portal", type="web", priority="high", options=options)
        return WebCollector().collect(cfg)

    portal = world.state_at(LAST_LOOK).portal
    out = collect(portal_html(portal))
    assert out.broken == []
    assert {r.key: r.fields["status"] for r in out.records} == {
        row["order_no"]: row["status"] for row in portal.rows
    }
    assert collect(portal_html(world.state_at(LAYOUT_CHANGE_AT).portal)).broken == ["table#orders"]
    with pytest.raises(LoginRequired):
        collect(portal_html(world.state_at(NOW).portal))


# -- the notes of the last look ------------------------------------------------------------------


def test_notes_are_the_state_at_the_last_look(world: World) -> None:
    notes = world.notes()
    assert set(notes) == {"last_look.txt", "po_table.csv", "portal.txt"}
    assert notes["last_look.txt"] == "2026-09-15T09:00:00Z\n"
    assert all("\r" not in text for text in notes.values())

    at_last = world.state_at(LAST_LOOK)
    table = list(csv.DictReader(io.StringIO(notes["po_table.csv"])))
    assert [tuple(r) for r in table[:1]] == [
        ("po_no", "supplier", "status", "eta", "qty", "updated_at")
    ]
    assert table == [{k: str(v) for k, v in row.items()} for row in at_last.po_rows]
    assert len(table) >= 50
    assert notes["portal.txt"] == portal_text(at_last.portal)
    assert notes["portal.txt"].startswith("Customer orders\n")  # the v1 page
    assert all(row["order_no"] in notes["portal.txt"] for row in at_last.portal.rows)


def test_notes_know_nothing_that_happened_after_the_last_look(world: World) -> None:
    notes = world.notes()
    table = {r["po_no"]: r for r in csv.DictReader(io.StringIO(notes["po_table.csv"]))}
    assert all(r["updated_at"] <= "2026-09-15T09:00:00Z" for r in table.values())
    created_later = {e.key for e in world.po_events if e.created and e.t > LAST_LOOK}
    assert created_later and not created_later & set(table)
    for kind, ref in world.planted:
        if kind == KIND_PO:
            assert table[ref]["status"] != "Cancelled"  # cancellations are still to come
        if kind == KIND_PORTAL:
            (line,) = [x for x in notes["portal.txt"].splitlines() if x.startswith(ref)]
            assert "Cancelled" not in line  # the cancellation / hold is still to come


# -- rule 1: mail --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "needs_action", "received", "planted"),
    [
        ("customer", True, LAST_LOOK + HOUR, True),
        ("supplier", True, LAST_LOOK + HOUR, True),
        ("customer", True, LAST_LOOK + SECOND, True),
        ("customer", True, NOW, True),  # "now" itself is in
        ("customer", True, LAST_LOOK, False),  # "after" is strict
        ("customer", True, LAST_LOOK - HOUR, False),  # before the last look
        ("supplier", True, START, False),
        ("customer", True, NOW + SECOND, False),  # not received yet
        ("customer", False, LAST_LOOK + HOUR, False),  # nothing to do or worry about
        ("supplier", False, LAST_LOOK + HOUR, False),
        ("colleague", True, LAST_LOOK + HOUR, False),  # "URGENT" from a colleague
        ("promo", True, LAST_LOOK + HOUR, False),  # "URGENT: 50% off"
        ("saas", True, LAST_LOOK + HOUR, False),
        ("newsletter", True, LAST_LOOK + HOUR, False),
        ("portal", True, LAST_LOOK + HOUR, False),
    ],
)
def test_rule_1_email(kind: str, needs_action: bool, received: datetime, planted: bool) -> None:
    mail = mk_mail(kind=kind, needs_action=needs_action, received=received, ref="7300001")
    assert email_items([mail], LAST_LOOK, NOW) == (["7300001"] if planted else [])


def test_rule_1_ignores_flags_and_returns_sorted_refs() -> None:
    mails = [
        mk_mail(1, ref="9100009", flags=("\\Answered", "\\Seen")),
        mk_mail(2, ref="4100001", received=LAST_LOOK + 5 * HOUR),
        mk_mail(3, ref="5100002", kind="supplier"),
        mk_mail(4, ref="9100004", kind="colleague"),
    ]
    assert email_items(mails, LAST_LOOK, NOW) == ["4100001", "5100002", "9100009"]
    assert email_items(iter(mails), LAST_LOOK, NOW) == ["4100001", "5100002", "9100009"]


def test_rule_1_needs_a_reference_on_a_business_mail() -> None:
    with pytest.raises(ValueError, match="no reference"):
        email_items([mk_mail(ref=None)], LAST_LOOK, NOW)
    assert email_items([mk_mail(ref=None, kind="colleague")], LAST_LOOK, NOW) == []
    assert email_items([mk_mail(ref=None, needs_action=False)], LAST_LOOK, NOW) == []


# -- rule 2: PO table ----------------------------------------------------------------------------


def eta_plus(days: int, eta: str = "2026-10-01") -> str:
    return (datetime.fromisoformat(eta) + timedelta(days=days)).date().isoformat()


@pytest.mark.parametrize(
    ("before", "after", "planted"),
    [
        (po("1"), po("1", "Cancelled"), True),
        (po("1", "Confirmed"), po("1", "Cancelled"), True),
        (po("1", "Shipped"), po("1", "Cancelled"), True),
        (po("1", "Cancelled"), po("1", "Cancelled"), False),  # cancelled before the last look
        (po("1", "Cancelled"), po("1", "Open"), False),
        (po("1"), po("1"), False),  # cancelled and re-opened: before and after are the same
        (po("1"), po("1", eta=eta_plus(ETA_SLIP_DAYS + 1)), True),
        (po("1"), po("1", eta=eta_plus(ETA_SLIP_DAYS + 30)), True),
        (po("1"), po("1", eta=eta_plus(ETA_SLIP_DAYS)), False),  # exactly 3 days: not more
        (po("1"), po("1", eta=eta_plus(2)), False),
        (po("1"), po("1", eta=eta_plus(1)), False),
        (po("1"), po("1", eta=eta_plus(-10)), False),  # earlier
        (po("1"), po("1", "Cancelled", eta=eta_plus(-10)), True),  # cancelled wins
        (po("1"), po("1", "Shipped"), False),
        (po("1"), po("1", "Received", qty=5), False),
        (po("1", eta="2026-12-30"), po("1", eta="2027-01-03"), True),  # across a year end
        (po("1", eta="2026-12-30"), po("1", eta="2027-01-02"), False),
    ],
)
def test_rule_2_po(before: dict[str, Any], after: dict[str, Any], planted: bool) -> None:
    assert po_items([before], [after]) == (["1"] if planted else [])


def test_rule_2_compares_states_and_needs_the_po_in_both() -> None:
    before = [po("10"), po("20"), po("30", "Cancelled"), po("50")]
    after = [
        po("10", "Cancelled"),  # planted
        po("20", eta=eta_plus(9)),  # planted
        po("30", "Cancelled"),  # not: cancelled before
        po("40", "Cancelled"),  # created after the last look: no state to compare with
        po("60", eta=eta_plus(9)),  # created after the last look
    ]  # "50" vanished: not planted
    assert po_items(before, after) == ["10", "20"]
    assert po_items(iter(before), iter(after)) == ["10", "20"]
    assert po_items([], after) == [] and po_items(before, []) == []


def test_rule_2_over_a_timeline_cancelled_then_reopened_and_two_small_slips() -> None:
    initial = [po("1"), po("2"), po("3"), po("4"), po("5")]
    events = [
        RowEvent(LAST_LOOK - HOUR, "1", {"status": "Cancelled"}),  # before the last look
        RowEvent(LAST_LOOK + HOUR, "2", {"status": "Cancelled"}),
        RowEvent(LAST_LOOK + 2 * HOUR, "2", {"status": "Open"}),  # re-opened
        RowEvent(LAST_LOOK + HOUR, "3", {"eta": eta_plus(2)}),
        RowEvent(LAST_LOOK + 3 * HOUR, "3", {"eta": eta_plus(4)}),  # 2 + 2 days: 4 in all
        RowEvent(LAST_LOOK + HOUR, "4", {"eta": eta_plus(5)}),
        RowEvent(LAST_LOOK + 4 * HOUR, "4", {"eta": eta_plus(2)}),  # pulled back to 2 days
        RowEvent(LAST_LOOK, "5", {"status": "Cancelled"}),  # exactly at the last look: known
    ]
    tiny = World(0, [LAST_LOOK, NOW], [], [], initial, events, [], [])
    assert tiny.planted == [
        (KIND_PO, "3"),
        (KIND_SYSTEM, SYSTEM_LAYOUT),
        (KIND_SYSTEM, SYSTEM_LOGIN),
    ]
    assert tiny.state_at(LAST_LOOK).po_rows[4]["status"] == "Cancelled"  # `<=`: in the old state
    middle = tiny.state_at(LAST_LOOK + timedelta(minutes=90)).po_rows[1]
    assert middle["po_no"] == "2" and middle["status"] == "Cancelled"  # for a while, not now


# -- rule 3: portal ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("was", "now", "planted"),
    [
        ("Open", "Cancelled", True),
        ("Open", "On Hold", True),
        ("Confirmed", "On Hold", True),
        ("Shipped", "Cancelled", True),
        ("On Hold", "Cancelled", True),  # on hold before, cancelled after
        ("On Hold", "On Hold", False),  # put on hold before the last look
        ("Cancelled", "Cancelled", False),
        ("On Hold", "Open", False),  # released
        ("Cancelled", "Open", False),
        ("Open", "Open", False),
        ("Open", "Confirmed", False),
        ("Confirmed", "Shipped", False),
        ("Shipped", "Delivered", False),
    ],
)
def test_rule_3_portal(was: str, now: str, planted: bool) -> None:
    assert portal_items(orders(("77", was)), orders(("77", now))) == (["77"] if planted else [])


def test_rule_3_compares_the_true_rows_whatever_the_page_shows() -> None:
    before = orders(("1", "Open"), ("2", "Open"), ("3", "On Hold"), ("4", "Open"))
    for layout, login in (("v1", False), ("v2", False), ("v1", True), ("v2", True)):
        after = orders(
            ("1", "Cancelled"), ("2", "On Hold"), ("3", "On Hold"), layout=layout, login=login
        )
        assert portal_items(before, after) == ["1", "2"]  # "4" vanished, "3" was already on hold
    new_only = orders(("9", "On Hold"))
    assert portal_items(before, new_only) == []  # not in the old state
    assert portal_items(orders(), new_only) == []


def test_rule_3_other_field_changes_are_not_news() -> None:
    before = orders(("1", "Open"))
    after = PortalState(
        [{**before.rows[0], "ship_by": "2026-11-11", "customer": "Ashby Grand"}], "v1", False
    )
    assert portal_items(before, after) == []


# -- rule 4: what stops you from seeing a source now ---------------------------------------------


@pytest.mark.parametrize(
    ("layout", "login", "expected"),
    [
        ("v1", False, []),
        ("v2", False, [SYSTEM_LAYOUT]),
        ("v1", True, [SYSTEM_LOGIN]),
        ("v2", True, [SYSTEM_LAYOUT, SYSTEM_LOGIN]),
    ],
)
def test_rule_4_system(layout: str, login: bool, expected: list[str]) -> None:
    assert system_items(orders(("1", "Open"), layout=layout, login=login)) == expected
    assert system_items(orders(layout=layout, login=login)) == expected  # rows do not matter


def test_the_four_rules_combine_in_a_fixed_order() -> None:
    before = state(
        LAST_LOOK,
        mails=[mk_mail(1, ref="8100001", received=LAST_LOOK - HOUR)],
        po_rows=[po("5"), po("6")],
        portal=orders(("1", "Open"), ("2", "Open")),
    )
    after = state(
        NOW,
        mails=[
            mk_mail(1, ref="8100001", received=LAST_LOOK - HOUR),
            mk_mail(2, ref="9100002", kind="supplier"),
            mk_mail(3, ref="9100001"),
            mk_mail(4, ref="9100003", kind="colleague"),
        ],
        po_rows=[po("5", "Cancelled"), po("6")],
        portal=orders(("1", "On Hold"), ("2", "Open"), layout="v2", login=True),
    )
    assert compute_planted(before, after) == [
        (KIND_EMAIL, "9100001"),
        (KIND_EMAIL, "9100002"),
        (KIND_PO, "5"),
        (KIND_PORTAL, "1"),
        (KIND_SYSTEM, SYSTEM_LAYOUT),
        (KIND_SYSTEM, SYSTEM_LOGIN),
    ]
    assert compute_planted(before, before) == []  # nothing changed, portal fine


def test_a_hand_built_world_computes_its_own_answer_key() -> None:
    mails = [
        mk_mail(1, ref="1000001", received=LAST_LOOK - HOUR),
        mk_mail(2, ref="1000002", received=LAST_LOOK + HOUR),
        mk_mail(3, ref="1000003", received=LAST_LOOK + HOUR, needs_action=False),
    ]
    events = [FlagEvent(LAST_LOOK + 2 * HOUR, FOLDER, 2, ("\\Seen",))]
    portal_events = [
        RowEvent(LAST_LOOK - SECOND, "1", {"status": "On Hold"}),  # known at the last look
        RowEvent(LAST_LOOK + SECOND, "2", {"status": "Cancelled"}),
    ]
    tiny = World(
        seed=0,
        ticks=[LAST_LOOK, NOW],
        mails=mails,
        flag_events=events,
        po_initial=[po("1")],
        po_events=[RowEvent(LAST_LOOK + HOUR, "1", {"eta": eta_plus(4)})],
        portal_initial=[
            {"order_no": "1", "customer": "A", "status": "Open", "ship_by": "2026-10-01"},
            {"order_no": "2", "customer": "B", "status": "Open", "ship_by": "2026-10-01"},
        ],
        portal_events=portal_events,
        scenarios=[],
    )
    assert tiny.planted == [
        (KIND_EMAIL, "1000002"),
        (KIND_PO, "1"),
        (KIND_PORTAL, "2"),
        (KIND_SYSTEM, SYSTEM_LAYOUT),
        (KIND_SYSTEM, SYSTEM_LOGIN),
    ]
    assert tiny.decoys == []
    assert tiny.state_at(NOW).mails[1].flags == ("\\Seen",)
    assert tiny.state_at(LAST_LOOK).mails[1:] == []
    assert summary(tiny)["planted_total"] == 5


# -- the answer key of the generated world -------------------------------------------------------


def test_planted_is_computed_by_the_rules_and_reproduces_the_intent(world: World) -> None:
    assert world.planted == compute_planted(world.state_at(LAST_LOOK), world.state_at(NOW))
    intended = [(s.kind, s.ref) for s in world.scenarios if s.role == "planted"]
    assert sorted(world.planted) == sorted(intended)
    order = [KIND_EMAIL, KIND_PO, KIND_PORTAL, KIND_SYSTEM]
    assert world.planted == sorted(world.planted, key=lambda i: (order.index(i[0]), i[1]))
    assert len(set(world.planted)) == len(world.planted)


def test_every_rule_has_at_least_two_planted_items(world: World) -> None:
    by_kind = Counter(kind for kind, _ in world.planted)
    assert by_kind[KIND_EMAIL] >= 2 and by_kind[KIND_PO] >= 2 and by_kind[KIND_PORTAL] >= 2
    assert sorted(ref for kind, ref in world.planted if kind == KIND_SYSTEM) == [
        SYSTEM_LAYOUT,
        SYSTEM_LOGIN,
    ]
    assert 12 <= len(world.planted) <= 20


def test_planted_items_exist_in_the_data_and_meet_their_rule(world: World) -> None:
    last, now = world.state_at(LAST_LOOK), world.state_at(NOW)
    mails = {m.ref: m for m in world.mails if m.ref}
    old_po = {r["po_no"]: r for r in last.po_rows}
    new_po = {r["po_no"]: r for r in now.po_rows}
    old_orders = {r["order_no"]: r for r in last.portal.rows}
    new_orders = {r["order_no"]: r for r in now.portal.rows}
    for kind, ref in world.planted:
        if kind == KIND_EMAIL:
            m = mails[ref]
            assert m.sender_kind in BUSINESS_KINDS and m.needs_action and m.received > LAST_LOOK
        elif kind == KIND_PO:
            gone = new_po[ref]["status"] == "Cancelled" != old_po[ref]["status"]
            slip = (
                datetime.fromisoformat(new_po[ref]["eta"])
                - datetime.fromisoformat(old_po[ref]["eta"])
            ).days
            assert gone or slip > 3
        elif kind == KIND_PORTAL:
            assert new_orders[ref]["status"] in ("Cancelled", "On Hold")
            assert new_orders[ref]["status"] != old_orders[ref]["status"]
    # and nothing else meets the rules
    for m in world.mails:
        if m.ref not in {r for k, r in world.planted if k == KIND_EMAIL}:
            assert not (
                m.sender_kind in BUSINESS_KINDS and m.needs_action and m.received > LAST_LOOK
            )


def test_planted_items_cover_the_timeline(world: World) -> None:
    at = {(s.kind, s.ref): s.at for s in world.scenarios if s.role == "planted"}
    assert set(at) == set(world.planted)
    mail_times = [t for (k, _), t in at.items() if k == KIND_EMAIL]
    assert all(t > LAST_LOOK for t in mail_times)
    assert {t.date().day for t in mail_times} == {15, 16}
    portal_times = sorted(t for (k, _), t in at.items() if k == KIND_PORTAL)
    assert len([t for t in portal_times if t < LAYOUT_CHANGE_AT]) >= 2  # a web collector sees these
    assert any(LAYOUT_CHANGE_AT <= t < LOGIN_EXPIRY_AT for t in portal_times)  # v2: extractor blind
    assert any(t >= LOGIN_EXPIRY_AT for t in portal_times)  # behind the login page
    assert at[(KIND_SYSTEM, SYSTEM_LAYOUT)] == LAYOUT_CHANGE_AT
    assert at[(KIND_SYSTEM, SYSTEM_LOGIN)] == LOGIN_EXPIRY_AT
    assert all(LAST_LOOK < t <= NOW for t in at.values())


def test_the_tricky_planted_cases_are_in_the_world(world: World) -> None:
    """Cumulative ETA slip, and hold -> cancel across the last look, are planted."""
    events: dict[str, list[RowEvent]] = {}
    for e in world.po_events:
        events.setdefault(e.key, []).append(e)
    slips = [
        ref
        for kind, ref in world.planted
        if kind == KIND_PO
        and len([e for e in events[ref] if "eta" in e.changes and e.t > LAST_LOOK]) == 2
    ]
    assert len(slips) == 1
    last = {r["order_no"]: r for r in world.state_at(LAST_LOOK).portal.rows}
    assert any(
        last[ref]["status"] == "On Hold" for kind, ref in world.planted if kind == KIND_PORTAL
    )


def test_every_rule_has_decoys_the_rules_reject(world: World) -> None:
    decoys = world.decoys
    assert decoys == sorted(decoys) and len(set(decoys)) == len(decoys)
    assert {kind for kind, _, _ in decoys} == {KIND_EMAIL, KIND_PO, KIND_PORTAL, KIND_SYSTEM}
    assert all(why.strip() for _, _, why in decoys)
    planted = set(world.planted)
    assert not planted & {(kind, ref) for kind, ref, _ in decoys}
    assert not {ref for _, ref, _ in decoys} & {ref for _, ref in planted}


def test_email_decoys_are_near_misses(world: World) -> None:
    by_ref = {m.ref: m for m in world.mails if m.ref}
    by_place = {f"{m.folder}/{m.uid}": m for m in world.mails}
    found = [(ref, why) for kind, ref, why in world.decoys if kind == KIND_EMAIL]
    assert len(found) >= 8
    for ref, _ in found:
        mail = by_ref.get(ref) or by_place[ref]
        assert email_items([mail], LAST_LOOK, NOW) == []  # the rule rejects it
        if mail.sender_kind in BUSINESS_KINDS:
            # a real customer / supplier mail: it asks for action but is old, or is recent but
            # asks for nothing
            assert (mail.needs_action and mail.received < LAST_LOOK) or (
                not mail.needs_action and mail.received > LAST_LOOK
            )
        else:  # it demands action and is recent: only its sender rules it out
            assert mail.sender_kind in ("colleague", "promo", "saas")
            assert re.search(r"URGENT|FINAL NOTICE|Last chance|Action required", mail.subject)
            assert mail.needs_action and mail.received > LAST_LOOK
    kinds = {(by_ref.get(r) or by_place[r]).sender_kind for r, _ in found}
    assert {"customer", "supplier", "colleague", "promo", "saas"} <= kinds
    pre = [by_ref[r] for r, _ in found if r in by_ref and by_ref[r].received < LAST_LOOK]
    assert any(
        m.sender_kind == "customer" and "ASN" in m.subject and "rejected" in m.subject for m in pre
    )


def test_po_decoys_are_near_misses(world: World) -> None:
    last, now = world.state_at(LAST_LOOK), world.state_at(NOW)
    decoys = [ref for kind, ref, _ in world.decoys if kind == KIND_PO]
    assert len(decoys) >= 5
    touched = {e.key for e in world.po_events}
    assert set(decoys) <= touched  # each of them really did something that looks like news
    assert po_items(last.po_rows, now.po_rows) == sorted(
        r for k, r in world.planted if k == KIND_PO
    )
    assert not set(decoys) & set(po_items(last.po_rows, now.po_rows))
    old = {r["po_no"]: r for r in last.po_rows}
    new = {r["po_no"]: r for r in now.po_rows}

    def slip(ref: str) -> int:
        return (
            datetime.fromisoformat(new[ref]["eta"]) - datetime.fromisoformat(old[ref]["eta"])
        ).days

    slips = {slip(ref) for ref in decoys}
    assert ETA_SLIP_DAYS in slips and 2 in slips and any(s < 0 for s in slips)
    # cancelled before the last look; and cancelled after it but re-opened by now
    assert any(old[r]["status"] == "Cancelled" == new[r]["status"] and r in touched for r in decoys)
    reopened = [
        ref
        for ref in decoys
        if any(
            row["po_no"] == ref and row["status"] == "Cancelled"
            for t in world.ticks
            if t > LAST_LOOK
            for row in world.state_at(t).po_rows
        )
        and new[ref]["status"] != "Cancelled"
    ]
    assert len(reopened) == 1
    # an ETA moved by more than 3 days, but before the last look
    initial = {r["po_no"]: r["eta"] for r in world.po_initial}
    early = [
        ref
        for ref in decoys
        if (datetime.fromisoformat(old[ref]["eta"]) - datetime.fromisoformat(initial[ref])).days > 3
    ]
    assert early and all(slip(ref) == 0 for ref in early)


def test_portal_and_system_decoys_are_near_misses(world: World) -> None:
    last, now = world.state_at(LAST_LOOK), world.state_at(NOW)
    decoys = [ref for kind, ref, _ in world.decoys if kind == KIND_PORTAL]
    assert len(decoys) >= 2
    assert not set(decoys) & set(portal_items(last.portal, now.portal))
    old = {r["order_no"]: r["status"] for r in last.portal.rows}
    new = {r["order_no"]: r["status"] for r in now.portal.rows}
    assert any(old[r] == new[r] == "On Hold" for r in decoys)  # put on hold before the last look
    assert any(old[r] == new[r] == "Cancelled" for r in decoys)  # cancelled before the last look
    mails = {m.subject: m for m in world.mails if m.sender_kind == "portal"}
    system = [ref for kind, ref, _ in world.decoys if kind == KIND_SYSTEM]
    assert len(system) >= 1 and len(mails) == len(system)
    assert set(system).isdisjoint({SYSTEM_LAYOUT, SYSTEM_LOGIN})
    assert all(not m.needs_action for m in mails.values())


# -- summary -------------------------------------------------------------------------------------


def test_summary_counts_for_the_report(world: World) -> None:
    s = summary(world)
    assert s == world.summary()
    assert json.loads(json.dumps(s)) == s
    assert s["seed"] == DEFAULT_SEED and s["ticks"] == 31
    assert s["mails_total"] == len(world.mails) >= 200
    assert s["mails_before_last_look"] + s["mails_after_last_look"] == s["mails_total"]
    assert sum(s["mails_by_sender_kind"].values()) == s["mails_total"]
    assert (
        s["mails_business"]
        == s["mails_by_sender_kind"]["customer"] + s["mails_by_sender_kind"]["supplier"]
    )
    business_after = [
        m
        for m in world.state_at(NOW).mails
        if m.sender_kind in ("customer", "supplier") and m.received > LAST_LOOK
    ]
    assert s["mails_business_after_last_look"] == len(business_after)
    assert 0 < s["mails_business_after_last_look"] < s["mails_business"]
    assert s["mails_business_after_last_look"] < s["mails_after_last_look"]
    assert (
        s["po_rows_at_start"] >= 50
        and s["po_rows_now"] == s["po_rows_at_start"] + s["po_rows_created"]
    )
    assert s["po_changes"] == len([e for e in world.po_events if not e.created])
    assert s["portal_orders"] == 20
    assert s["planted_total"] == len(world.planted) == sum(s["planted_by_kind"].values())
    assert s["decoys_total"] == len(world.decoys) == sum(s["decoys_by_kind"].values())
    assert (
        set(s["planted_by_kind"]) == set(s["decoys_by_kind"]) == {"email", "po", "portal", "system"}
    )
    assert s["layout_change_at"] == "2026-09-15T22:00:00Z"
    assert s["login_expiry_at"] == "2026-09-16T07:00:00Z"
