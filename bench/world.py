"""The simulated business world of the M3 benchmark and its answer key (D33).

A pure, deterministic data model: no I/O, no clock, no network. ``build_world(seed)`` returns a
``World``: three days (2026-09-14 00:00Z ... 2026-09-16 10:00Z = "now") of a small wholesale
supplier that sells to department stores and buys from factories. The world has three sources:

- a mailbox (``Mail``): newsletters, promos, software notifications, colleague chatter, and
  customer / supplier mail; the subject of every customer / supplier mail carries a unique
  reference number;
- a PO table (rows ``po_no, supplier, status, eta, qty, updated_at``);
- a customer portal (rows ``order_no, customer, status, ship_by``) that changes its layout at
  ``LAYOUT_CHANGE_AT`` (the M2 extractor config ``PORTAL_V1_EXTRACT`` stops matching) and serves a
  login page from ``LOGIN_EXPIRY_AT`` on.

``World.state_at(t)`` is the world as of any instant; the benchmark replays ``World.ticks``.

The answer key. The agent last looked at ``LAST_LOOK`` ("yesterday"). "Needing attention" is
defined by four rules, and ``World.planted`` is *computed* by the functions implementing them
(``email_items``, ``po_items``, ``portal_items``, ``system_items``, combined by ``compute_planted``)
from the state at ``LAST_LOOK`` and the state at ``NOW``:

1. mail from a customer or supplier that asks for action or reports a problem, received after
   the last look (``Mail.sender_kind`` + ``Mail.needs_action``, set by the generator; every business
   mail has a subject a reader can classify);
2. a PO cancelled, or whose ETA moved later by more than 3 days, after the last look. It is a
   comparison of two states: a PO cancelled and re-opened again by now is not planted, and two
   2-day slips that add up to 4 days are. Only POs that exist in both states count;
3. a portal order cancelled or put on hold after the last look. Also a comparison of the
   ``LAST_LOOK`` rows with the ``NOW`` rows *as they exist in the world*, regardless of what any
   tool can read: the portal keeps changing after the layout change and after the login expiry,
   and ``PortalState.rows`` always holds the true data (``layout`` / ``login_expired`` only decide
   what the page shows). Only orders that exist in both states count;
4. any problem that stops you from seeing a source now: ``portal-login`` (login expired),
   ``portal-layout`` (layout changed away from the one the extractor was written for).

Items are ``(kind, ref)``: kind ``email | po | portal | system``, ref = the reference number
(digits) or ``portal-login`` / ``portal-layout``. ``World.decoys`` lists ``(kind, ref, why)`` for
things that look alarming but meet no rule. ``World.scenarios`` records what the generator
*intended* (planted and decoy, with the moment each became true): the tests check that the rules
reproduce it, and the replay uses it to say which planted items a tool could observe.
"""

from __future__ import annotations

import csv
import html
import io
import json
import random
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, timezone
from email.utils import format_datetime, formataddr
from functools import cached_property
from typing import Any, NamedTuple

from since.timeutil import to_iso

DEFAULT_SEED = 20260916

START = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
LAST_LOOK = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
LAYOUT_CHANGE_AT = datetime(2026, 9, 15, 22, 0, tzinfo=UTC)  # from this instant: layout "v2"
LOGIN_EXPIRY_AT = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)  # from this instant: a login page
TICK_STEP = timedelta(hours=2)
ETA_SLIP_DAYS = 3  # rule 2: an ETA moved later by MORE than this many days

FOLDER = "INBOX"
OUR_DOMAIN = "ourco.example"

KIND_EMAIL, KIND_PO, KIND_PORTAL, KIND_SYSTEM = "email", "po", "portal", "system"
SYSTEM_LAYOUT, SYSTEM_LOGIN = "portal-layout", "portal-login"

SENDER_CUSTOMER, SENDER_SUPPLIER = "customer", "supplier"
SENDER_COLLEAGUE, SENDER_NEWSLETTER, SENDER_PROMO = "colleague", "newsletter", "promo"
SENDER_SAAS, SENDER_PORTAL = "saas", "portal"
BUSINESS_KINDS = (SENDER_CUSTOMER, SENDER_SUPPLIER)

FLAG_SEEN, FLAG_ANSWERED, FLAG_FLAGGED = "\\Seen", "\\Answered", "\\Flagged"

STATUS_CANCELLED = "Cancelled"
STATUS_ON_HOLD = "On Hold"

LAYOUT_V1, LAYOUT_V2 = "v1", "v2"

PO_COLUMNS = ("po_no", "supplier", "status", "eta", "qty", "updated_at")
PORTAL_COLUMNS = ("order_no", "customer", "status", "ship_by")

# The M2 ``web`` extractor config (``extract:`` in since.yaml) written for layout v1. It matches
# the v1 page and matches nothing on v2 (its container ``table#orders`` is gone). ``wait_for`` and
# ``login_detect`` are the caller's business; the login page holds ``PORTAL_LOGIN_SELECTOR``.
PORTAL_V1_EXTRACT: dict[str, Any] = {
    "container": "table#orders",
    "rows": "table#orders tbody tr",
    "key": "order_no",
    "fields": {
        "order_no": "td:nth-child(1)",
        "customer": "td:nth-child(2)",
        "status": "td:nth-child(3)",
        "ship_by": "td:nth-child(4)",
    },
}
PORTAL_LOGIN_SELECTOR = "form#login"


# -- data model ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mail:
    """One mail as the IMAP server holds it at some instant (``flags`` is the state at that time).

    ``uid`` is per folder; ``received`` is INTERNALDATE, ``date_header`` the sender's own Date
    header (a few minutes earlier, in the sender's zone). ``sender_kind`` says who wrote it;
    ``needs_action`` says the mail asks for action or reports a problem; ``ref`` is the unique
    reference number in the subject of a customer / supplier mail (else ``None``)."""

    folder: str
    uid: int
    message_id: str
    received: datetime
    date_header: str
    from_name: str
    from_addr: str
    to: str
    subject: str
    body: str
    flags: tuple[str, ...]  # a set, kept as a sorted tuple so repr / dumps are deterministic
    sender_kind: str
    needs_action: bool
    ref: str | None

    @property
    def from_header(self) -> str:
        return formataddr((self.from_name, self.from_addr))

    @property
    def key(self) -> tuple[str, int]:
        return (self.folder, self.uid)

    def raw_header(self) -> bytes:
        """The header block Since's imap source asks for (Message-ID, From, To, Subject, Date):
        CRLF lines ended by an empty line. All text is ASCII."""
        lines = [
            f"Message-ID: {self.message_id}",
            f"From: {self.from_header}",
            f"To: {self.to}",
            f"Subject: {self.subject}",
            f"Date: {self.date_header}",
        ]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    @property
    def size(self) -> int:
        """RFC822.SIZE: the header block plus the body."""
        return len(self.raw_header()) + len(self.body.encode("ascii"))


@dataclass(frozen=True)
class PortalState:
    """``rows`` (``order_no, customer, status, ship_by``) are always the true orders; ``layout``
    (``"v1"`` / ``"v2"``) and ``login_expired`` decide what ``portal_html`` shows."""

    rows: list[dict[str, str]]
    layout: str
    login_expired: bool


@dataclass(frozen=True)
class WorldState:
    t: datetime
    mails: list[Mail]  # every mail received at or before ``t``, ascending UID
    po_rows: list[dict[str, Any]]  # ascending po_no
    portal: PortalState


@dataclass(frozen=True)
class FlagEvent:
    t: datetime
    folder: str
    uid: int
    add: tuple[str, ...]


@dataclass(frozen=True)
class RowEvent:
    """A change of one table row at ``t``: ``changes`` (field -> new value) for an existing row,
    or the whole row when ``created``. A PO change also sets ``updated_at`` to ``t``."""

    t: datetime
    key: str
    changes: dict[str, Any]
    created: bool = False


@dataclass(frozen=True)
class Scenario:
    """What the generator meant a thing to be: ``role`` is ``planted`` or ``decoy``; ``at`` is the
    instant it became true (mail received, row changed, layout changed, login expired)."""

    kind: str
    ref: str
    role: str
    why: str
    at: datetime


# -- the rules (the answer key) ------------------------------------------------------------------


def email_items(mails: Iterable[Mail], last_look: datetime, now: datetime) -> list[str]:
    """Rule 1: refs of customer / supplier mails that ask for action or report a problem and were
    received after ``last_look`` (strictly) and up to ``now``."""
    refs = []
    for mail in mails:
        if mail.sender_kind not in BUSINESS_KINDS or not mail.needs_action:
            continue
        if not last_look < mail.received <= now:
            continue
        if not mail.ref:
            raise ValueError(f"business mail {mail.folder}/{mail.uid} has no reference number")
        refs.append(mail.ref)
    return sorted(refs)


def po_items(before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]]) -> list[str]:
    """Rule 2: po_no of POs (present in both states) that are cancelled in ``after`` but were not
    in ``before``, or whose ETA is more than ``ETA_SLIP_DAYS`` days later in ``after``."""
    old = {row["po_no"]: row for row in before}
    refs = []
    for row in after:
        was = old.get(row["po_no"])
        if was is None:
            continue
        cancelled = row["status"] == STATUS_CANCELLED and was["status"] != STATUS_CANCELLED
        slip = (date.fromisoformat(row["eta"]) - date.fromisoformat(was["eta"])).days
        if cancelled or slip > ETA_SLIP_DAYS:
            refs.append(row["po_no"])
    return sorted(refs)


def portal_items(before: PortalState, after: PortalState) -> list[str]:
    """Rule 3: order_no of orders (present in both states) whose status is ``Cancelled`` or
    ``On Hold`` in ``after`` and was a different status in ``before``. Compares the true rows,
    whatever ``layout`` / ``login_expired`` say."""
    old = {row["order_no"]: row for row in before.rows}
    refs = []
    for row in after.rows:
        was = old.get(row["order_no"])
        if was is None:
            continue
        if row["status"] in (STATUS_CANCELLED, STATUS_ON_HOLD) and was["status"] != row["status"]:
            refs.append(row["order_no"])
    return sorted(refs)


def system_items(portal_now: PortalState) -> list[str]:
    """Rule 4: ``portal-layout`` if the layout is no longer v1, ``portal-login`` if the login
    has expired (the two problems that stop you from reading the portal now)."""
    items = []
    if portal_now.layout != LAYOUT_V1:
        items.append(SYSTEM_LAYOUT)
    if portal_now.login_expired:
        items.append(SYSTEM_LOGIN)
    return items


def compute_planted(before: WorldState, after: WorldState) -> list[tuple[str, str]]:
    """The answer key: every ``(kind, ref)`` that meets a rule between ``before`` (the last
    look) and ``after`` (now), ordered email, po, portal, system and by ref inside a kind."""
    items = [(KIND_EMAIL, ref) for ref in email_items(after.mails, before.t, after.t)]
    items += [(KIND_PO, ref) for ref in po_items(before.po_rows, after.po_rows)]
    items += [(KIND_PORTAL, ref) for ref in portal_items(before.portal, after.portal)]
    items += [(KIND_SYSTEM, ref) for ref in system_items(after.portal)]
    return items


# -- the portal page -----------------------------------------------------------------------------

_V1_HEADERS = ("Order", "Customer", "Status", "Ship by")
_V2_HEADERS = ("Customer", "Order no.", "Ship by", "Status", "Details")
_LOGIN_LINES = ("Session expired", "Please sign in to continue.", "User", "Password", "Sign in")


def _v1_cells(row: dict[str, str]) -> list[str]:
    return [row["order_no"], row["customer"], row["status"], row["ship_by"]]


def _v2_cells(row: dict[str, str]) -> list[str]:
    return [row["customer"], row["order_no"], row["ship_by"], row["status"], "View"]


def portal_text(portal: PortalState) -> str:
    """The visible text of the portal page (what ``fetch_portal()`` returns and what the notes'
    ``portal.txt`` holds): one line per visible block, table cells joined by `` | ``."""
    if portal.login_expired:
        lines = list(_LOGIN_LINES)
    elif portal.layout == LAYOUT_V1:
        lines = ["Customer orders", " | ".join(_V1_HEADERS)]
        lines += [" | ".join(_v1_cells(row)) for row in portal.rows]
    elif portal.layout == LAYOUT_V2:
        lines = ["Welcome to the new Retail Link experience", "Orders", " | ".join(_V2_HEADERS)]
        lines += [" | ".join(_v2_cells(row)) for row in portal.rows]
    else:
        raise ValueError(f"unknown portal layout {portal.layout!r}")
    return "\n".join(lines) + "\n"


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title></head>\n<body>\n{body}</body></html>\n"
    )


def portal_html(portal: PortalState) -> str:
    """The portal page: the login page when the login expired, else the v1 or v2 order table.
    ``PORTAL_V1_EXTRACT`` reads v1 and finds nothing on v2; the login page holds ``form#login``."""
    esc = html.escape
    if portal.login_expired:
        return _page(
            "Retail Link - Sign in",
            '<div id="app" class="login-shell">\n'
            f"<h1>{_LOGIN_LINES[0]}</h1>\n<p>{_LOGIN_LINES[1]}</p>\n"
            '<form id="login" method="post" action="/login">\n'
            '<label>User <input name="user"></label>\n'
            '<label>Password <input name="password" type="password"></label>\n'
            '<button type="submit">Sign in</button>\n</form>\n</div>\n',
        )
    if portal.layout == LAYOUT_V1:
        head = "".join(f"<th>{esc(h)}</th>" for h in _V1_HEADERS)
        body = ""
        for i, row in enumerate(portal.rows):
            cells = "".join(f"<td>{esc(c)}</td>" for c in _v1_cells(row))
            body += f'<tr class="{"odd" if i % 2 == 0 else "even"}">{cells}</tr>\n'
        return _page(
            "Retail Link - Orders",
            '<div id="app" class="shell">\n<h1 class="title">Customer orders</h1>\n'
            f'<table id="orders" class="grid wide">\n<thead><tr>{head}</tr></thead>\n'
            f"<tbody>\n{body}</tbody>\n</table>\n</div>\n",
        )
    if portal.layout == LAYOUT_V2:
        head = "".join(f"<th>{esc(h)}</th>" for h in _V2_HEADERS)
        body = ""
        for row in portal.rows:
            order = esc(row["order_no"])
            body += (
                '<tr class="ol-row">'
                f'<td class="cust">{esc(row["customer"])}</td>'
                f'<td class="no">{order}</td>'
                f'<td class="due">{esc(row["ship_by"])}</td>'
                f'<td class="st"><span class="badge">{esc(row["status"])}</span></td>'
                f'<td class="go"><a href="/order/{order}">View</a></td></tr>\n'
            )
        return _page(
            "Retail Link - Orders",
            '<div id="app" class="shell-v2">\n'
            '<div class="banner">Welcome to the new Retail Link experience</div>\n'
            '<h2 class="page-title">Orders</h2>\n'
            f'<table id="order-list" class="ol-table">\n<thead><tr>{head}</tr></thead>\n'
            f"<tbody>\n{body}</tbody>\n</table>\n</div>\n",
        )
    raise ValueError(f"unknown portal layout {portal.layout!r}")


# -- the world -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """The whole timeline. The fields below the seed are the generator's raw material (initial
    data plus dated events); use ``state_at`` / ``notes`` / ``planted`` / ``decoys``."""

    seed: int
    ticks: list[datetime]
    mails: list[Mail]  # as received (flags empty), ascending UID
    flag_events: list[FlagEvent]
    po_initial: list[dict[str, Any]]
    po_events: list[RowEvent]
    portal_initial: list[dict[str, str]]
    portal_events: list[RowEvent]
    scenarios: list[Scenario] = field(default_factory=list)

    def state_at(self, t: datetime) -> WorldState:
        """The world as of ``t`` (any instant): mails received at or before ``t`` with the flags
        they have then, PO rows, and the portal (rows, layout, login)."""
        flags: dict[tuple[str, int], set[str]] = {}
        for ev in self.flag_events:
            if ev.t <= t:
                flags.setdefault((ev.folder, ev.uid), set()).update(ev.add)
        mails = [
            replace(m, flags=tuple(sorted(set(m.flags) | flags.get(m.key, set()))))
            for m in self.mails
            if m.received <= t
        ]
        po = _apply(self.po_initial, self.po_events, t, "po_no", touch="updated_at")
        rows = _apply(self.portal_initial, self.portal_events, t, "order_no")
        portal = PortalState(
            rows=rows,
            layout=LAYOUT_V2 if t >= LAYOUT_CHANGE_AT else LAYOUT_V1,
            login_expired=t >= LOGIN_EXPIRY_AT,
        )
        return WorldState(t=t, mails=mails, po_rows=po, portal=portal)

    def notes(self) -> dict[str, str]:
        """What the Arm A agent saved at its last look: ``last_look.txt`` (the time),
        ``po_table.csv`` and ``portal.txt`` (the visible text of the v1 page)."""
        state = self.state_at(LAST_LOOK)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(PO_COLUMNS)
        for row in state.po_rows:
            writer.writerow([row[c] for c in PO_COLUMNS])
        return {
            "last_look.txt": to_iso(LAST_LOOK) + "\n",
            "po_table.csv": buf.getvalue(),
            "portal.txt": portal_text(state.portal),
        }

    @cached_property
    def planted(self) -> list[tuple[str, str]]:
        """The answer key, computed by the four rules from the state at LAST_LOOK and at NOW."""
        return compute_planted(self.state_at(LAST_LOOK), self.state_at(NOW))

    @cached_property
    def decoys(self) -> list[tuple[str, str, str]]:
        """``(kind, ref, why)`` of things that look attention-worthy but meet no rule."""
        found = [(s.kind, s.ref, s.why) for s in self.scenarios if s.role == "decoy"]
        return sorted(found)

    def summary(self) -> dict[str, Any]:
        return summary(self)

    def to_json(self) -> str:
        """A canonical dump of the whole world (initial data, events, notes, answer key): equal
        worlds give byte-identical text."""
        data: dict[str, Any] = {
            "seed": self.seed,
            "ticks": [to_iso(t) for t in self.ticks],
            "mails": [_mail_dict(m) for m in self.mails],
            "flag_events": [
                {"t": to_iso(e.t), "folder": e.folder, "uid": e.uid, "add": list(e.add)}
                for e in self.flag_events
            ],
            "po_initial": self.po_initial,
            "po_events": [_event_dict(e) for e in self.po_events],
            "portal_initial": self.portal_initial,
            "portal_events": [_event_dict(e) for e in self.portal_events],
            "notes": self.notes(),
            "planted": [list(item) for item in self.planted],
            "decoys": [list(item) for item in self.decoys],
            "scenarios": [{**asdict(s), "at": to_iso(s.at)} for s in self.scenarios],
        }
        return json.dumps(data, sort_keys=True, indent=1) + "\n"


def _apply(
    initial: list[dict[str, Any]],
    events: list[RowEvent],
    t: datetime,
    key: str,
    touch: str | None = None,
) -> list[dict[str, Any]]:
    """Replay ``events`` up to ``t`` on a copy of ``initial``; rows ascending by ``key``."""
    rows = {row[key]: dict(row) for row in initial}
    for ev in sorted(events, key=lambda e: e.t):  # stable: equal times keep their order
        if ev.t > t:
            break
        if ev.created:
            rows[ev.key] = dict(ev.changes)
        else:
            rows[ev.key].update(ev.changes)
            if touch is not None:
                rows[ev.key][touch] = to_iso(ev.t)
    return [rows[k] for k in sorted(rows)]


def _mail_dict(mail: Mail) -> dict[str, Any]:
    data = asdict(mail)
    data["received"] = to_iso(mail.received)
    data["flags"] = list(mail.flags)
    return data


def _event_dict(event: RowEvent) -> dict[str, Any]:
    return {
        "t": to_iso(event.t),
        "key": event.key,
        "changes": event.changes,
        "created": event.created,
    }


def summary(world: World) -> dict[str, Any]:
    """Counts for the benchmark report (JSON-serialisable)."""
    last, now = world.state_at(LAST_LOOK), world.state_at(NOW)
    first_po = world.state_at(START)
    po_changes = [e for e in world.po_events if not e.created]
    planted, decoys = world.planted, world.decoys
    return {
        "seed": world.seed,
        "start": to_iso(START),
        "last_look": to_iso(LAST_LOOK),
        "now": to_iso(NOW),
        "layout_change_at": to_iso(LAYOUT_CHANGE_AT),
        "login_expiry_at": to_iso(LOGIN_EXPIRY_AT),
        "ticks": len(world.ticks),
        "mails_total": len(now.mails),
        "mails_before_last_look": len(last.mails),
        "mails_after_last_look": len(now.mails) - len(last.mails),
        "mails_by_sender_kind": dict(sorted(Counter(m.sender_kind for m in now.mails).items())),
        "mails_business": sum(1 for m in now.mails if m.sender_kind in BUSINESS_KINDS),
        "mails_business_after_last_look": sum(
            1 for m in now.mails if m.sender_kind in BUSINESS_KINDS and m.received > LAST_LOOK
        ),
        "mail_flag_events": len(world.flag_events),
        "po_rows_at_start": len(first_po.po_rows),
        "po_rows_now": len(now.po_rows),
        "po_rows_created": sum(1 for e in world.po_events if e.created),
        "po_changes": len(po_changes),
        "po_changes_after_last_look": sum(1 for e in po_changes if e.t > LAST_LOOK),
        "portal_orders": len(now.portal.rows),
        "portal_changes": len(world.portal_events),
        "portal_changes_after_last_look": sum(1 for e in world.portal_events if e.t > LAST_LOOK),
        "planted_total": len(planted),
        "planted_by_kind": dict(sorted(Counter(k for k, _ in planted).items())),
        "decoys_total": len(decoys),
        "decoys_by_kind": dict(sorted(Counter(k for k, _, _ in decoys).items())),
    }


def make_ticks() -> list[datetime]:
    """Every 2 hours from START to NOW, plus the exact LAST_LOOK; sorted, unique."""
    ticks = {START + i * TICK_STEP for i in range(int((NOW - START) / TICK_STEP) + 1)}
    return sorted(ticks | {LAST_LOOK, NOW})


# -- generator: shared helpers -------------------------------------------------------------------

_GUARD = timedelta(minutes=10)  # no random event within this distance of LAST_LOOK


def _rng(seed: int, part: str) -> random.Random:
    """An independent stream per part of the world (a str seed is hashed with sha512: stable)."""
    return random.Random(f"since-bench:{seed}:{part}")


def _t(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=UTC)


def _random_time(
    rng: random.Random, lo: datetime = START, hi: datetime = NOW, guard: bool = True
) -> datetime:
    """A whole-second instant in [lo, hi], never within ``_GUARD`` of LAST_LOOK."""
    span = int((hi - lo).total_seconds())
    while True:
        moment = lo + timedelta(seconds=rng.randrange(span + 1))
        if not guard or abs(moment - LAST_LOOK) >= _GUARD:
            return moment


def _hm(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M}Z"


def _shift(eta: str, days: int) -> str:
    return (date.fromisoformat(eta) + timedelta(days=days)).isoformat()


_REF_BASES = {
    "asn": 91000000,
    "order": 61000000,
    "portal_order": 62000000,
    "invoice": 700000,
    "chargeback": 33000000,
    "statement": 250000,
    "shipment": 73000000,
    "supinvoice": 5100000,
    "batch": 41000000,
    "notice": 880000,
    "dn": 6600000,
    "supplier_order": 3100000,
}


class _Refs:
    """Unique reference numbers (digits): one increasing counter per document type, so a number
    is never used twice and the types never overlap (PO numbers are 45001xx)."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._last = dict(_REF_BASES)

    def next(self, ref_type: str) -> str:
        self._last[ref_type] += self._rng.randrange(1, 40)
        return str(self._last[ref_type])


# -- generator: mail -----------------------------------------------------------------------------


class _Company(NamedTuple):
    name: str
    kind: str
    domain: str
    tz: int  # hours east of UTC, for the Date header
    contacts: tuple[tuple[str, str], ...]  # (person, mailbox)


_COMPANIES = {
    c.name: c
    for c in (
        _Company(
            "Hartwell & Sons",
            SENDER_CUSTOMER,
            "hartwell.example",
            10,
            (("Grace Liu", "vendor.support"), ("Tom Barrett", "buying")),
        ),
        _Company(
            "Marlowe Department Stores",
            SENDER_CUSTOMER,
            "marlowe.example",
            10,
            (("Priya Nair", "edi"), ("Ben Ortiz", "replenishment")),
        ),
        _Company(
            "Ashby Grand",
            SENDER_CUSTOMER,
            "ashbygrand.example",
            11,
            (("Helen Cho", "ap"), ("Dan Whitcombe", "buying")),
        ),
        _Company(
            "Kingsley's",
            SENDER_CUSTOMER,
            "kingsleys.example",
            10,
            (("Rosa Alvarez", "vendor.relations"), ("Mark Ellis", "dc.receiving")),
        ),
        _Company(
            "Northgate Stores",
            SENDER_CUSTOMER,
            "northgate.example",
            8,
            (("Sam Okafor", "edi"), ("Nina Petrov", "accounts.payable")),
        ),
        _Company(
            "Pacific Ceramics",
            SENDER_SUPPLIER,
            "pacificceramics.example",
            8,
            (("Wei Zhang", "sales"), ("Lena Fischer", "logistics")),
        ),
        _Company(
            "Nordic Glassworks",
            SENDER_SUPPLIER,
            "nordicglass.example",
            2,
            (("Olav Berg", "orders"), ("Ingrid Solheim", "accounts")),
        ),
        _Company(
            "Everbright Packaging",
            SENDER_SUPPLIER,
            "everbright-pack.example",
            8,
            (("Chen Yu", "export"), ("Mei Lin", "finance")),
        ),
        _Company(
            "Sunrise Housewares",
            SENDER_SUPPLIER,
            "sunrisehousewares.example",
            8,
            (("Anita Rao", "sales"), ("Jun Park", "ar")),
        ),
        _Company(
            "Delta Freight",
            SENDER_SUPPLIER,
            "deltafreight.example",
            1,
            (("Pieter de Vries", "ops"), ("Klara Novak", "customs")),
        ),
        _Company(
            "Jiangsu Home Textiles",
            SENDER_SUPPLIER,
            "jhtextiles.example",
            8,
            (("Li Na", "sales"), ("Zhou Tao", "shipping")),
        ),
    )
}
_CUSTOMER_NAMES = tuple(c.name for c in _COMPANIES.values() if c.kind == SENDER_CUSTOMER)
_SUPPLIER_NAMES = tuple(c.name for c in _COMPANIES.values() if c.kind == SENDER_SUPPLIER)


class _Template(NamedTuple):
    kind: str
    ref_type: str
    subject: str
    body: str
    needs_action: bool


_TEMPLATES = {
    # customers: mail that asks for action or reports a problem
    "asn_rejected": _Template(
        SENDER_CUSTOMER,
        "asn",
        "ASN {ref} rejected - please resubmit today",
        "Our receiving system rejected ASN {ref} this morning because the carton count on the ASN "
        "does not match the packing list. Please send a corrected ASN today, otherwise the "
        "delivery slot will be released and the goods will be refused at the dock.",
        True,
    ),
    "price_mismatch": _Template(
        SENDER_CUSTOMER,
        "invoice",
        "Invoice {ref} - price mismatch, credit note required",
        "Invoice {ref} bills the stoneware mug sets at 18.90 each but the price agreed on the "
        "contract is 16.40. Please issue a credit note for the difference so that we can release "
        "payment.",
        True,
    ),
    "confirm_delivery": _Template(
        SENDER_CUSTOMER,
        "order",
        "Order {ref} - please confirm delivery date by Friday",
        "We have not received a delivery date for order {ref} and the store is planning its floor "
        "set. Please confirm the delivery date by Friday, or we will reallocate the shelf space "
        "to another vendor.",
        True,
    ),
    "short_shipment": _Template(
        SENDER_CUSTOMER,
        "order",
        "Order {ref} - short shipment received",
        "Order {ref} arrived at our distribution centre with 12 cartons missing against the "
        "packing list. Please tell us whether you will reship the missing cartons or issue a "
        "credit.",
        True,
    ),
    "chargeback": _Template(
        SENDER_CUSTOMER,
        "chargeback",
        "Chargeback {ref} - late delivery penalty, please respond",
        "A late delivery penalty has been raised against your account under reference {ref}. If "
        "you dispute it, send your proof of delivery within seven days.",
        True,
    ),
    "damaged_goods": _Template(
        SENDER_CUSTOMER,
        "order",
        "Order {ref} - damaged goods on receipt, claim raised",
        "Several cartons of order {ref} arrived crushed and the glassware inside is broken. "
        "Photos are attached. A claim has been raised on our side, please confirm how you want "
        "to handle the replacement.",
        True,
    ),
    # customers: no action needed
    "remittance": _Template(
        SENDER_CUSTOMER,
        "invoice",
        "Remittance advice - invoice {ref} paid",
        "Payment for invoice {ref} was released today and should reach your bank in two business "
        "days. No action is needed.",
        False,
    ),
    "delivered_full": _Template(
        SENDER_CUSTOMER,
        "order",
        "Order {ref} - delivered in full, thank you",
        "Order {ref} was received in full at our distribution centre. Thank you, there is "
        "nothing further to do.",
        False,
    ),
    "dock_appointment": _Template(
        SENDER_CUSTOMER,
        "order",
        "Delivery appointment confirmed - order {ref}",
        "Your delivery appointment for order {ref} is confirmed as booked. No reply is needed.",
        False,
    ),
    "ship_date_noted": _Template(
        SENDER_CUSTOMER,
        "order",
        "Order {ref} - revised ship date noted",
        "We have noted the revised ship date for order {ref}. No action is needed on your side.",
        False,
    ),
    "statement": _Template(
        SENDER_CUSTOMER,
        "statement",
        "Statement of account - reference {ref}",
        "Your monthly statement of account, reference {ref}, is attached for your records. "
        "Nothing is overdue.",
        False,
    ),
    "asn_accepted": _Template(
        SENDER_CUSTOMER,
        "asn",
        "ASN {ref} accepted - earlier rejection resolved",
        "This is to confirm that ASN {ref} has now been accepted by our receiving system. The "
        "earlier rejection is resolved and there is nothing further for you to do.",
        False,
    ),
    # suppliers: mail that asks for action or reports a problem
    "shipment_held": _Template(
        SENDER_SUPPLIER,
        "shipment",
        "Shipment {ref} held at customs - decision needed",
        "Shipment {ref} is being held at customs while the certificate of origin is checked. "
        "Please tell us today whether to wait for clearance or to split the container and send "
        "the rest ahead.",
        True,
    ),
    "invoice_overdue": _Template(
        SENDER_SUPPLIER,
        "supinvoice",
        "Invoice {ref} overdue - account will be placed on hold",
        "Invoice {ref} is now past its due date. Unless payment or a payment date reaches us "
        "this week, we will place your account on hold and stop releasing new orders.",
        True,
    ),
    "quality_issue": _Template(
        SENDER_SUPPLIER,
        "batch",
        "Quality issue on batch {ref} - please advise",
        "Our inspection found a glaze defect on batch {ref}. Please advise whether we should "
        "rework the affected pieces or ship them back to you before the goods leave our factory.",
        True,
    ),
    "price_increase": _Template(
        SENDER_SUPPLIER,
        "notice",
        "Price increase notice {ref} - your approval is needed",
        "Because of higher energy costs we will raise our prices from the next order. The "
        "details are in notice {ref}. Please confirm your approval so that we can keep your "
        "orders moving.",
        True,
    ),
    # suppliers: no action needed
    "departed": _Template(
        SENDER_SUPPLIER,
        "shipment",
        "Shipment {ref} has departed",
        "Shipment {ref} left the port on schedule. Tracking details are below. No action is "
        "needed.",
        False,
    ),
    "proforma": _Template(
        SENDER_SUPPLIER,
        "supinvoice",
        "Proforma invoice {ref} for your records",
        "Proforma invoice {ref} is attached for your records. It will be replaced by the final "
        "invoice on dispatch. No action is needed.",
        False,
    ),
    "delivery_note": _Template(
        SENDER_SUPPLIER,
        "dn",
        "Delivery note {ref} attached",
        "Delivery note {ref} for your latest goods is attached for your records. No action is "
        "needed.",
        False,
    ),
    "order_confirmation": _Template(
        SENDER_SUPPLIER,
        "supplier_order",
        "Order confirmation {ref}",
        "We confirm your order under reference {ref}. Production is on schedule. No action is "
        "needed.",
        False,
    ),
}
_QUIET_TEMPLATES = {
    SENDER_CUSTOMER: (
        "remittance",
        "delivered_full",
        "dock_appointment",
        "ship_date_noted",
        "statement",
    ),
    SENDER_SUPPLIER: ("departed", "proforma", "delivery_note", "order_confirmation"),
}

# (day, hour, minute), company, template, why (planted mails have none)
_PLANTED_MAIL = (
    ((15, 9, 41), "Marlowe Department Stores", "asn_rejected"),
    ((15, 11, 26), "Delta Freight", "shipment_held"),
    ((15, 14, 12), "Ashby Grand", "price_mismatch"),
    ((15, 17, 48), "Everbright Packaging", "invoice_overdue"),
    ((15, 21, 20), "Kingsley's", "damaged_goods"),
    ((16, 2, 33), "Hartwell & Sons", "confirm_delivery"),
    ((16, 6, 17), "Pacific Ceramics", "quality_issue"),
)
_EARLY_DECOY_MAIL = (  # the same kinds of mail, before the last look
    ((14, 7, 20), "Kingsley's", "asn_rejected"),
    ((14, 13, 5), "Northgate Stores", "short_shipment"),
    ((14, 16, 40), "Nordic Glassworks", "price_increase"),
    ((14, 22, 10), "Hartwell & Sons", "chargeback"),
    ((15, 3, 35), "Sunrise Housewares", "invoice_overdue"),
    ((15, 8, 20), "Marlowe Department Stores", "confirm_delivery"),
)

_OPS = formataddr(("Ourco Ops", f"ops@{OUR_DOMAIN}"))
_TEAM = formataddr(("Ourco Team", f"team@{OUR_DOMAIN}"))

_COLLEAGUES = (
    "Amira Haddad",
    "Jonas Weber",
    "Kelly Oneil",
    "Raj Patel",
    "Sofia Lindgren",
    "Marcus Reid",
    "Tessa Nguyen",
    "Leo Fontaine",
)
_CHATTER = (
    (
        "Lunch order for Friday - reply by 11",
        "I am putting in the lunch order for Friday. Reply with your pick by 11 and I will add "
        "it to the list.",
    ),
    (
        "Cycle count schedule for aisles 4 to 9",
        "The cycle count for aisles 4 to 9 starts on Wednesday morning. Please keep pallets in "
        "place until it is done.",
    ),
    (
        "Pallet label printer jammed again",
        "The label printer at dock 2 jammed again. I cleared it but it needs a proper service. "
        "Using the spare for now.",
    ),
    (
        "Team meeting moved to 3pm",
        "Today's team meeting is moved to 3pm in the small meeting room. The agenda is the same.",
    ),
    (
        "Who left the roller door open?",
        "The roller door at dock 1 was open all night. Nothing seems to be missing, but please "
        "double check when you close up.",
    ),
    (
        "Cake in the kitchen",
        "Birthday cake in the kitchen, help yourselves before it disappears.",
    ),
    (
        "Timesheets due Monday",
        "A reminder that timesheets are due on Monday. Payroll cannot wait for late ones.",
    ),
    (
        "New starter next week",
        "We have a new starter in the warehouse next week. Please say hello and show them where "
        "the kettle is.",
    ),
    (
        "Warehouse safety reminder",
        "Please keep the aisles clear and wear hi-vis on the floor. Thanks for keeping everyone "
        "safe.",
    ),
    (
        "Van booking for Thursday",
        "I have booked the small van for Thursday morning for the showroom samples. Shout if you "
        "need it as well.",
    ),
    (
        "Can someone cover the phones from 12 to 1?",
        "I have a dentist appointment over lunch. Can someone cover the phones from 12 to 1?",
    ),
    (
        "Notes from the sales huddle",
        "Short notes from this morning's huddle: focus on spring range samples, chase two "
        "open quotes, keep the showroom tidy.",
    ),
    (
        "Coffee machine descaling today",
        "The coffee machine is being descaled today, so there will be no coffee until about "
        "lunchtime. Sorry.",
    ),
    (
        "Q3 forecast spreadsheet updated",
        "I updated the Q3 forecast spreadsheet on the shared drive. Comments welcome, no rush.",
    ),
    (
        "Forklift licence renewals",
        "Forklift licence renewals are coming up for three of us. I will book the course as a "
        "group.",
    ),
    (
        "Fridge clean-out on Friday",
        "The kitchen fridge gets cleaned out on Friday afternoon. Anything left in it will be "
        "thrown away.",
    ),
    (
        "Parking spots this week",
        "The council is resurfacing the street, so parking in the yard is first come first "
        "served this week.",
    ),
    (
        "Photos from the showroom refresh",
        "Photos from the showroom refresh are on the shared drive. The new shelving looks great.",
    ),
    (
        "Sample approvals for the spring range",
        "The spring range samples are on the meeting room table. Leave your notes on the sheet "
        "by the door.",
    ),
    (
        "Wifi password changed in the warehouse",
        "The warehouse wifi password was changed. The new one is on the whiteboard by the "
        "office door.",
    ),
)
_URGENT_COLLEAGUE = (  # look urgent, but a colleague wrote them: rule 1 needs a customer / supplier
    (
        (15, 10, 35),
        "URGENT: who has the keys to the loading dock?",
        "Nobody can find the loading dock keys and the driver is waiting. Please check your "
        "pockets and reply.",
    ),
    (
        (15, 23, 10),
        "URGENT - stocktake moved to Thursday, all hands",
        "Stocktake is moved to Thursday and we need everybody on the floor. Please confirm that "
        "you can make it.",
    ),
    (
        (16, 8, 5),
        "URGENT: printer on level 2 is down",
        "The printer on level 2 is down again. Please use the one downstairs until IT looks at it.",
    ),
)

_PROMO_SENDERS = (
    ("OfficeMart Deals", "deals@officemart.example"),
    ("PrintPro Labels", "offers@printpro.example"),
    ("Bulk Supplies Direct", "sales@bulksupplies.example"),
    ("PalletWrap Co", "hello@palletwrap.example"),
)
_PROMO_SUBJECTS = (
    "Spring range now open for pre-order",
    "New: recycled kraft cartons in stock",
    "Free shipping on orders over the minimum this week",
    "Meet our new label printer range",
    "Save on packing tape and bubble wrap",
    "Your wholesale price list has been updated",
    "Bestsellers restocked: thermal labels",
)
_PROMO_BODY = (
    "Big savings for wholesale customers. Browse the range online or reply to this email for a "
    "quote. Terms and conditions apply. You can unsubscribe at any time using the link below."
)
_ALARMING_PROMOS = (  # (day, hour, minute), sender index, subject
    ((15, 12, 5), 0, "URGENT: 50% off ends tonight"),
    ((15, 19, 50), 1, "FINAL NOTICE: your loyalty points expire in 24 hours"),
    ((16, 5, 25), 3, "Last chance: 70% off pallet wrap - do not miss out"),
)

_SAAS = (  # (sender, address, subject, body)
    (
        "Xero",
        "no-reply@xero.example",
        "Your weekly business summary",
        "Your weekly summary is ready.",
    ),
    (
        "Xero",
        "no-reply@xero.example",
        "Bank feed synced",
        "Your bank feed was synced successfully.",
    ),
    ("Xero", "no-reply@xero.example", "Invoice reminders were sent", "Reminders went out."),
    (
        "Zoom",
        "no-reply@zoom.example",
        "Cloud recording is now available",
        "Your recording is ready.",
    ),
    ("Zoom", "no-reply@zoom.example", "Your meeting summary is ready", "Open Zoom to read it."),
    ("Slack", "feedback@slack.example", "New messages in #warehouse", "You have new messages."),
    ("Slack", "feedback@slack.example", "Unread mentions in #sales", "You were mentioned."),
    (
        "Google Workspace",
        "workspace-noreply@google.example",
        "Weekly storage report",
        "Your organisation uses 61% of its storage.",
    ),
    (
        "Google Workspace",
        "workspace-noreply@google.example",
        "Invitation: weekly stock meeting",
        "You have been invited to the weekly stock meeting.",
    ),
    (
        "Microsoft 365",
        "o365-noreply@microsoft.example",
        "Message center: planned changes",
        "There are planned changes to your services.",
    ),
    (
        "Microsoft 365",
        "o365-noreply@microsoft.example",
        "Your weekly digest",
        "Here is what happened in your workspace this week.",
    ),
    (
        "Dropbox",
        "no-reply@dropbox.example",
        "Files were shared with you",
        "Open Dropbox to see them.",
    ),
    (
        "Dropbox",
        "no-reply@dropbox.example",
        "Your storage summary",
        "You are using 40% of storage.",
    ),
)

_NEWSLETTERS = (
    (
        "Retail Weekly",
        "news@retailweekly.example",
        (
            "Department stores lean into private label",
            "Holiday hiring outlook",
            "What shoppers expect from click and collect",
            "Store footfall: a mixed September",
            "Loyalty schemes under the microscope",
        ),
    ),
    (
        "Home & Living Trade Digest",
        "digest@homeliving-trade.example",
        (
            "Ceramics and glassware trends for spring",
            "Sustainable packaging: what buyers ask for",
            "Trade fair calendar for the season",
            "Linen and cotton prices ease",
            "Small appliances: the year so far",
        ),
    ),
    (
        "Wholesale Insider",
        "editor@wholesaleinsider.example",
        (
            "Margins under pressure: how suppliers respond",
            "Five tips for negotiating with department stores",
            "EDI compliance checklist",
            "Cash flow for growing wholesalers",
            "New rules on product labelling",
        ),
    ),
    (
        "Freight Watch Daily",
        "daily@freightwatch.example",
        (
            "Port congestion update",
            "Container rates this week",
            "Customs processing times",
            "Air freight capacity tightens",
            "Road freight fuel levy",
        ),
    ),
    (
        "Trade Fair Updates",
        "hello@tradefair.example",
        (
            "Early-bird tickets for the autumn fair",
            "Exhibitor hall map published",
            "Speaker line-up announced",
            "Hotel block reminder",
        ),
    ),
)
_NEWS_LINES = (
    "Buyers at the large chains say they are shortening their planning cycles.",
    "Analysts expect a soft quarter for discretionary categories.",
    "Several suppliers report longer lead times from Asian factories.",
    "Retailers are asking for more sustainable packaging in their vendor guides.",
    "Freight rates have eased slightly, but port delays remain a risk.",
    "A survey of buyers found that reliable delivery ranks above price.",
    "New labelling rules take effect at the end of the year.",
    "Exhibitors report strong pre-registration for the autumn fair.",
)

_PORTAL_SENDER = ("Retail Link Portal", "noreply@retaillink.example")


class _Spec:
    """A mail before it has a UID: everything the generator decides about it."""

    def __init__(
        self,
        received: datetime,
        kind: str,
        from_name: str,
        from_addr: str,
        to: str,
        subject: str,
        body: str,
        tz: int = 0,
        needs_action: bool = False,
        ref: str | None = None,
    ) -> None:
        self.received = received
        self.kind = kind
        self.from_name = from_name
        self.from_addr = from_addr
        self.to = to
        self.subject = subject
        self.body = body
        self.tz = tz
        self.needs_action = needs_action
        self.ref = ref
        self.planted_why: str | None = None
        self.decoy: tuple[str, str | None, str] | None = None  # (kind, ref or None, why)
        self.flag_early = False  # an actionable mail before the last look: read and flagged soon


def _business(
    rng: random.Random, refs: _Refs, when: datetime, company_name: str, template_key: str
) -> _Spec:
    company = _COMPANIES[company_name]
    template = _TEMPLATES[template_key]
    if template.kind != company.kind:
        raise ValueError(f"template {template_key} does not suit {company.kind} {company.name}")
    person, mailbox = rng.choice(company.contacts)
    ref = refs.next(template.ref_type)
    body = f"Hi team,\n\n{template.body.format(ref=ref)}\n\nRegards,\n{person}\n{company.name}\n"
    return _Spec(
        when,
        company.kind,
        f"{person} ({company.name})",
        f"{mailbox}@{company.domain}",
        _OPS,
        template.subject.format(ref=ref),
        body,
        company.tz,
        template.needs_action,
        ref,
    )


def _explicit_specs(rng: random.Random, refs: _Refs) -> list[_Spec]:
    """The mails the answer key is built around: planted ones and their decoys, at fixed times."""
    specs: list[_Spec] = []

    def when(day: int, hour: int, minute: int) -> datetime:
        return _t(day, hour, minute, rng.randrange(60))

    for (day, hour, minute), company, key in _PLANTED_MAIL:
        spec = _business(rng, refs, when(day, hour, minute), company, key)
        spec.planted_why = (
            f"{spec.kind} mail that asks for action or reports a problem, received "
            f"{_hm(spec.received)}, after the last look"
        )
        specs.append(spec)
    for (day, hour, minute), company, key in _EARLY_DECOY_MAIL:
        spec = _business(rng, refs, when(day, hour, minute), company, key)
        spec.decoy = (
            KIND_EMAIL,
            None,
            f"asks for action, but was received {_hm(spec.received)}, before the last look",
        )
        spec.flag_early = True
        specs.append(spec)
    spec = _business(rng, refs, when(15, 15, 30), "Marlowe Department Stores", "asn_accepted")
    spec.decoy = (
        KIND_EMAIL,
        None,
        "customer mail after the last look that mentions an earlier ASN rejection, but says it "
        "is resolved and asks for nothing",
    )
    specs.append(spec)

    for (day, hour, minute), subject, text in _URGENT_COLLEAGUE:
        name = rng.choice(_COLLEAGUES)
        spec = _Spec(
            when(day, hour, minute),
            SENDER_COLLEAGUE,
            name,
            f"{name.split()[0].lower()}@{OUR_DOMAIN}",
            _TEAM,
            subject,
            f"Hi all,\n\n{text}\n\nThanks,\n{name.split()[0]}\n",
            10,
            needs_action=True,
        )
        spec.decoy = (
            KIND_EMAIL,
            None,
            "looks urgent, but a colleague wrote it (not a customer or supplier)",
        )
        specs.append(spec)
    for (day, hour, minute), sender, subject in _ALARMING_PROMOS:
        name, address = _PROMO_SENDERS[sender]
        spec = _Spec(
            when(day, hour, minute),
            SENDER_PROMO,
            name,
            address,
            _OPS,
            subject,
            _PROMO_BODY + "\n",
            needs_action=True,  # it does demand something; only its sender rules it out
        )
        spec.decoy = (
            KIND_EMAIL,
            None,
            "alarming-sounding marketing promo, not a customer or supplier",
        )
        specs.append(spec)
    spec = _Spec(
        when(16, 3, 40),
        SENDER_SAAS,
        "Microsoft 365",
        "o365-noreply@microsoft.example",
        _OPS,
        "Action required: your mailbox is almost full",
        "Your mailbox is 98% full. Delete some items or ask your administrator for more storage.\n",
        needs_action=True,
    )
    spec.decoy = (KIND_EMAIL, None, "automated software notification, not a customer or supplier")
    specs.append(spec)

    name, address = _PORTAL_SENDER
    notices = (
        (
            (14, 6, 10),
            "Portal notice: scheduled maintenance on Saturday 19 September",
            "Retail Link will be unavailable on Saturday 19 September between 02:00 and 04:00 UTC "
            "for scheduled maintenance. No action is needed on your side.",
            "portal-maintenance-notice",
            "the portal vendor announces a future maintenance window; nothing stops you from "
            "seeing the portal now because of it",
        ),
        (
            (15, 13, 30),
            "Reminder: your portal password expires in 30 days",
            "Your Retail Link password will expire in 30 days. You can change it at any time under "
            "Account settings. Nothing needs to be done today.",
            "portal-password-reminder",
            "a reminder about a password that expires in 30 days; the login still worked when it "
            "was sent",
        ),
    )
    for (day, hour, minute), subject, text, ref, why in notices:
        spec = _Spec(
            when(day, hour, minute),
            SENDER_PORTAL,
            name,
            address,
            _OPS,
            subject,
            f"Hello,\n\n{text}\n\nRetail Link Support\n",
        )
        spec.decoy = (KIND_SYSTEM, ref, why)
        specs.append(spec)
    return specs


_NOISE_COUNTS = {
    "newsletter": 60,
    "promo": 22,
    "saas": 48,
    "colleague": 62,
    SENDER_CUSTOMER: 22,
    SENDER_SUPPLIER: 18,
}


def _random_specs(rng: random.Random, refs: _Refs) -> list[_Spec]:
    """Everything else: noise that meets no rule, at random times over the three days."""
    specs: list[_Spec] = []
    issues = {name: rng.randrange(120, 900) for name, _, _ in _NEWSLETTERS}
    for _ in range(_NOISE_COUNTS["newsletter"]):
        name, address, headlines = rng.choice(_NEWSLETTERS)
        issues[name] += 1
        headline = rng.choice(headlines)
        lines = " ".join(rng.sample(_NEWS_LINES, 3))
        body = (
            f"{headline}\n\n{lines}\n\nRead the full story on our website. You receive this "
            "newsletter because you subscribed; use the unsubscribe link to stop it.\n"
        )
        subject = f"{name} #{issues[name]}: {headline}"
        specs.append(
            _Spec(_random_time(rng), SENDER_NEWSLETTER, name, address, _OPS, subject, body)
        )
    for _ in range(_NOISE_COUNTS["promo"]):
        name, address = rng.choice(_PROMO_SENDERS)
        subject = rng.choice(_PROMO_SUBJECTS)
        specs.append(
            _Spec(_random_time(rng), SENDER_PROMO, name, address, _OPS, subject, _PROMO_BODY + "\n")
        )
    for _ in range(_NOISE_COUNTS["saas"]):
        name, address, subject, text = rng.choice(_SAAS)
        body = f"Hello,\n\n{text}\n\nThis is an automated message from {name}.\n"
        specs.append(_Spec(_random_time(rng), SENDER_SAAS, name, address, _OPS, subject, body))
    for _ in range(_NOISE_COUNTS["colleague"]):
        name = rng.choice(_COLLEAGUES)
        first = name.split()[0]
        subject, text = rng.choice(_CHATTER)
        if rng.random() < 0.3:
            subject = f"Re: {subject}"
        to = _TEAM if rng.random() < 0.6 else _OPS
        body = f"Hi all,\n\n{text}\n\nThanks,\n{first}\n"
        specs.append(
            _Spec(
                _random_time(rng),
                SENDER_COLLEAGUE,
                name,
                f"{first.lower()}@{OUR_DOMAIN}",
                to,
                subject,
                body,
                10,
            )
        )
    for kind, names in ((SENDER_CUSTOMER, _CUSTOMER_NAMES), (SENDER_SUPPLIER, _SUPPLIER_NAMES)):
        for _ in range(_NOISE_COUNTS[kind]):
            company = rng.choice(names)
            key = rng.choice(_QUIET_TEMPLATES[kind])
            specs.append(_business(rng, refs, _random_time(rng), company, key))
    return specs


def _build_mail(seed: int, refs: _Refs) -> tuple[list[Mail], list[FlagEvent], list[Scenario]]:
    rng = _rng(seed, "mail")
    specs = _explicit_specs(rng, refs) + _random_specs(rng, refs)
    specs.sort(key=lambda s: s.received)  # stable: ties keep generation order
    dates = _rng(seed, "mail-dates")

    mails: list[Mail] = []
    scenarios: list[Scenario] = []
    planted: set[int] = set()
    early: set[int] = set()
    for uid, spec in enumerate(specs, start=1):
        sent = spec.received - timedelta(seconds=dates.randrange(3, 240))
        header_date = format_datetime(sent.astimezone(timezone(timedelta(hours=spec.tz))))
        domain = spec.from_addr.split("@", 1)[1]
        stamp = f"{spec.received:%Y%m%d%H%M%S}"
        mails.append(
            Mail(
                folder=FOLDER,
                uid=uid,
                message_id=f"<{stamp}.{uid:04d}@{domain}>",
                received=spec.received,
                date_header=header_date,
                from_name=spec.from_name,
                from_addr=spec.from_addr,
                to=spec.to,
                subject=spec.subject,
                body=spec.body,
                flags=(),
                sender_kind=spec.kind,
                needs_action=spec.needs_action,
                ref=spec.ref,
            )
        )
        if spec.planted_why and spec.ref:
            planted.add(uid)
            scenarios.append(
                Scenario(KIND_EMAIL, spec.ref, "planted", spec.planted_why, spec.received)
            )
        if spec.decoy:
            kind, ref, why = spec.decoy
            if ref is None:
                ref = spec.ref or f"{FOLDER}/{uid}"
            scenarios.append(Scenario(kind, ref, "decoy", why, spec.received))
        if spec.flag_early:
            early.add(uid)
    return mails, _flag_events(seed, mails, planted, early), scenarios


def _flag_events(
    seed: int, mails: list[Mail], planted: set[int], early: set[int]
) -> list[FlagEvent]:
    """Flags change over time: the human reads most mail (\\Seen), answers some old mail and flags
    some after the last look (Since reports those as modified mail). Planted mails never change."""
    rng = _rng(seed, "flags")
    events: list[FlagEvent] = []

    def add(mail: Mail, moment: datetime, flag: str) -> None:
        if mail.received < moment <= NOW:
            events.append(FlagEvent(moment, mail.folder, mail.uid, (flag,)))

    before = LAST_LOOK - timedelta(hours=1)
    for mail in mails:
        if mail.uid in planted:
            continue
        if mail.uid in early:
            add(mail, mail.received + timedelta(minutes=rng.randrange(5, 20)), FLAG_SEEN)
            add(mail, mail.received + timedelta(minutes=rng.randrange(20, 90)), FLAG_FLAGGED)
        elif mail.received < before:
            if rng.random() < 0.7:
                add(mail, mail.received + timedelta(minutes=rng.randrange(2, 600)), FLAG_SEEN)
        elif mail.received > LAST_LOOK and rng.random() < 0.2:
            add(mail, mail.received + timedelta(minutes=rng.randrange(2, 300)), FLAG_SEEN)

    old = [m for m in mails if m.received < before and m.uid not in planted | early]
    people = [m for m in old if m.sender_kind in (SENDER_COLLEAGUE, *BUSINESS_KINDS)]
    late = (LAST_LOOK + _GUARD, NOW - timedelta(minutes=30))
    for mail in rng.sample(people, 6):
        add(mail, _random_time(rng, *late), FLAG_ANSWERED)
    for mail in rng.sample(old, 4):
        add(mail, _random_time(rng, *late), FLAG_FLAGGED)
    events.sort(key=lambda e: (e.t, e.uid, e.add))
    return events


# -- generator: PO table -------------------------------------------------------------------------

_PO_SUPPLIERS = _SUPPLIER_NAMES
_PO_QTY = (120, 240, 360, 480, 600, 720, 960, 1200, 1800, 2400, 3000, 4800)
_PO_ADVANCE = {"Open": "Confirmed", "Confirmed": "Shipped", "Shipped": "Received"}
_PO_FIRST = 4500101
_PO_INITIAL = 58  # rows at the start (the status counts in _build_po add up to this)
_PO_LATER = 6  # POs created during the three days


def _build_po(seed: int) -> tuple[list[dict[str, Any]], list[RowEvent], list[Scenario]]:
    rng = _rng(seed, "po")
    today = START.date()
    # Fixed status counts (shuffled), so that every seed has enough POs for every scenario below.
    statuses = ["Open"] * 20 + ["Confirmed"] * 16 + ["Shipped"] * 12 + ["Received"] * 8
    statuses += [STATUS_CANCELLED] * 2  # long cancelled: nobody needs to hear about them
    rng.shuffle(statuses)
    rows: list[dict[str, Any]] = []
    for i, status in enumerate(statuses):
        offset = {
            "Open": rng.randrange(8, 75),
            "Confirmed": rng.randrange(8, 75),
            "Shipped": rng.randrange(2, 20),
            "Received": -rng.randrange(0, 12),
            STATUS_CANCELLED: rng.randrange(8, 75),
        }[status]
        updated = START - timedelta(minutes=rng.randrange(60, 60 * 24 * 12))
        rows.append(
            {
                "po_no": str(_PO_FIRST + i),
                "supplier": rng.choice(_PO_SUPPLIERS),
                "status": status,
                "eta": (today + timedelta(days=offset)).isoformat(),
                "qty": rng.choice(_PO_QTY),
                "updated_at": to_iso(updated),
            }
        )

    events: list[RowEvent] = []
    scenarios: list[Scenario] = []
    live = [r for r in rows if r["status"] in ("Open", "Confirmed")]
    reopened = rng.choice([r for r in live if r["status"] == "Open"])
    rest = [r for r in live if r is not reopened]
    p1, p2, p3, p4, p5, d1, d2, d3, d5, d6 = rng.sample(rest, 10)
    used = {r["po_no"] for r in (reopened, p1, p2, p3, p4, p5, d1, d2, d3, d5, d6)}

    def change(row: dict[str, Any], moment: datetime, **changes: Any) -> None:
        events.append(RowEvent(moment, row["po_no"], changes))

    def note(row: dict[str, Any], role: str, moment: datetime, why: str) -> None:
        scenarios.append(Scenario(KIND_PO, row["po_no"], role, why, moment))

    # planted: rule 2
    moment = _t(15, 14, 31)
    change(p1, moment, status=STATUS_CANCELLED)
    note(p1, "planted", moment, f"cancelled {_hm(moment)}, after the last look")
    moment = _t(16, 5, 12)
    change(p2, moment, status=STATUS_CANCELLED)
    note(p2, "planted", moment, f"cancelled {_hm(moment)}, after the last look")
    moment = _t(15, 11, 47)
    change(p3, moment, eta=_shift(p3["eta"], 5))
    note(p3, "planted", moment, f"ETA moved later by 5 days at {_hm(moment)}")
    change(p4, _t(15, 12, 52), eta=_shift(p4["eta"], 2))
    moment = _t(16, 3, 20)
    change(p4, moment, eta=_shift(p4["eta"], 4))
    note(
        p4, "planted", moment, "ETA moved later by 2 days twice after the last look: 4 days in all"
    )
    moment = _t(16, 8, 40)
    change(p5, moment, eta=_shift(p5["eta"], 14))
    note(p5, "planted", moment, f"ETA moved later by 14 days at {_hm(moment)}")
    # decoys: rule 2
    moment = _t(14, 10, 12)
    change(d1, moment, status=STATUS_CANCELLED)
    note(d1, "decoy", moment, f"cancelled {_hm(moment)}, before the last look")
    moment = _t(15, 16, 5)
    change(d2, moment, eta=_shift(d2["eta"], 2))
    note(d2, "decoy", moment, "ETA moved later by 2 days (the rule needs more than 3)")
    moment = _t(15, 19, 40)
    change(d3, moment, eta=_shift(d3["eta"], 3))
    note(d3, "decoy", moment, "ETA moved later by exactly 3 days (the rule needs more than 3)")
    change(reopened, _t(15, 13, 15), status=STATUS_CANCELLED)
    moment = _t(16, 2, 45)
    change(reopened, moment, status="Open")
    note(reopened, "decoy", moment, "cancelled after the last look but re-opened again by now")
    moment = _t(14, 18, 30)
    change(d5, moment, eta=_shift(d5["eta"], 8))
    note(d5, "decoy", moment, f"ETA moved later by 8 days at {_hm(moment)}, before the last look")
    moment = _t(15, 20, 15)
    change(d6, moment, eta=_shift(d6["eta"], -6))
    note(d6, "decoy", moment, "ETA moved earlier by 6 days (not a delay)")

    # noise: routine progress that meets no rule
    free = [r for r in rows if r["po_no"] not in used and r["status"] in _PO_ADVANCE]
    for row in rng.sample(free, 30):
        first = _random_time(rng)
        status = _PO_ADVANCE[row["status"]]
        change(row, first, status=status)
        later = first + timedelta(minutes=30)
        if status in _PO_ADVANCE and later < NOW and rng.random() < 0.3:
            change(row, _random_time(rng, later, NOW), status=_PO_ADVANCE[status])
    movable = [r for r in free if r["status"] in ("Open", "Confirmed")]
    for row in rng.sample(movable, 6):
        change(row, _random_time(rng), eta=_shift(row["eta"], rng.choice([-2, -1, 1, 2])))
    for row in rng.sample(free, 8):
        qty = max(12, round(row["qty"] * rng.choice([0.85, 0.9, 1.1, 1.2]) / 12) * 12)
        change(row, _random_time(rng), qty=qty)
    for i in range(_PO_LATER):
        moment = _random_time(rng)
        events.append(
            RowEvent(
                moment,
                str(_PO_FIRST + _PO_INITIAL + i),
                {
                    "po_no": str(_PO_FIRST + _PO_INITIAL + i),
                    "supplier": rng.choice(_PO_SUPPLIERS),
                    "status": "Open",
                    "eta": (moment.date() + timedelta(days=rng.randrange(30, 60))).isoformat(),
                    "qty": rng.choice(_PO_QTY),
                    "updated_at": to_iso(moment),
                },
                created=True,
            )
        )
    events.sort(key=lambda e: e.t)
    return rows, events, scenarios


# -- generator: portal ---------------------------------------------------------------------------

_PORTAL_ADVANCE = {"Open": "Confirmed", "Confirmed": "Shipped", "Shipped": "Delivered"}
_PORTAL_ORDERS = 20


def _build_portal(
    seed: int, refs: _Refs
) -> tuple[list[dict[str, str]], list[RowEvent], list[Scenario]]:
    rng = _rng(seed, "portal")
    today = START.date()
    statuses = ["Open"] * 7 + ["Confirmed"] * 7 + ["Shipped"] * 4 + ["Delivered"] * 2
    rng.shuffle(statuses)
    rows: list[dict[str, str]] = []
    for status in statuses:
        offset = {
            "Open": rng.randrange(6, 30),
            "Confirmed": rng.randrange(6, 30),
            "Shipped": -rng.randrange(1, 6),
            "Delivered": -rng.randrange(5, 15),
        }[status]
        rows.append(
            {
                "order_no": refs.next("portal_order"),
                "customer": rng.choice(_CUSTOMER_NAMES),
                "status": status,
                "ship_by": (today + timedelta(days=offset)).isoformat(),
            }
        )
    open_rows = [r for r in rows if r["status"] == "Open"]
    confirmed = [r for r in rows if r["status"] == "Confirmed"]
    rng.shuffle(open_rows)
    rng.shuffle(confirmed)
    a, d, f, g = open_rows[:4]
    b, c, e, h = confirmed[:4]
    used = {r["order_no"] for r in (a, b, c, d, e, f, g, h)}

    events: list[RowEvent] = []
    scenarios: list[Scenario] = []

    def change(row: dict[str, str], moment: datetime, **changes: str) -> None:
        events.append(RowEvent(moment, row["order_no"], changes))

    def note(row: dict[str, str], role: str, moment: datetime, why: str) -> None:
        scenarios.append(Scenario(KIND_PORTAL, row["order_no"], role, why, moment))

    # decoys: rule 3
    moment = _t(14, 11, 10)
    change(a, moment, status=STATUS_ON_HOLD)
    note(a, "decoy", moment, f"put on hold {_hm(moment)}, before the last look, unchanged since")
    moment = _t(14, 15, 45)
    change(b, moment, status=STATUS_CANCELLED)
    note(b, "decoy", moment, f"cancelled {_hm(moment)}, before the last look")
    moment = _t(15, 20, 30)
    change(h, moment, ship_by=_shift(h["ship_by"], 5))
    note(
        h,
        "decoy",
        moment,
        "ship-by date moved later, but the order is neither cancelled nor on hold",
    )
    change(g, _t(15, 14, 0), status="Confirmed")
    change(g, _t(16, 4, 0), status="Shipped")
    # planted: rule 3 (the last two happen after the layout change / the login expiry)
    change(c, _t(14, 20, 0), status=STATUS_ON_HOLD)
    moment = _t(15, 12, 20)
    change(c, moment, status=STATUS_CANCELLED)
    note(c, "planted", moment, f"on hold before the last look, cancelled {_hm(moment)}, after it")
    moment = _t(15, 17, 5)
    change(d, moment, status=STATUS_ON_HOLD)
    note(d, "planted", moment, f"put on hold {_hm(moment)}, after the last look")
    moment = _t(16, 1, 40)
    change(e, moment, status=STATUS_CANCELLED)
    note(e, "planted", moment, f"cancelled {_hm(moment)}, after the layout change")
    moment = _t(16, 8, 15)
    change(f, moment, status=STATUS_ON_HOLD)
    note(f, "planted", moment, f"put on hold {_hm(moment)}, after the login expired")

    # noise: routine progress that meets no rule
    for row in rows:
        if row["order_no"] in used or row["status"] not in _PORTAL_ADVANCE:
            continue
        if row["status"] == "Shipped" and rng.random() < 0.5:
            continue
        change(row, _random_time(rng), status=_PORTAL_ADVANCE[row["status"]])
    events.sort(key=lambda ev: ev.t)
    return rows, events, scenarios


# -- the world -----------------------------------------------------------------------------------


def build_world(seed: int = DEFAULT_SEED) -> World:
    """The deterministic world for ``seed``: same seed, same world, byte for byte."""
    refs = _Refs(_rng(seed, "refs"))
    mails, flag_events, mail_scenarios = _build_mail(seed, refs)
    po_rows, po_events, po_scenarios = _build_po(seed)
    portal_rows, portal_events, portal_scenarios = _build_portal(seed, refs)
    system = [
        Scenario(
            KIND_SYSTEM,
            SYSTEM_LAYOUT,
            "planted",
            f"the portal layout changed at {_hm(LAYOUT_CHANGE_AT)}; the v1 selectors match nothing",
            LAYOUT_CHANGE_AT,
        ),
        Scenario(
            KIND_SYSTEM,
            SYSTEM_LOGIN,
            "planted",
            f"the portal login expired at {_hm(LOGIN_EXPIRY_AT)}; it serves a login page",
            LOGIN_EXPIRY_AT,
        ),
    ]
    return World(
        seed=seed,
        ticks=make_ticks(),
        mails=mails,
        flag_events=flag_events,
        po_initial=po_rows,
        po_events=po_events,
        portal_initial=portal_rows,
        portal_events=portal_events,
        scenarios=mail_scenarios + po_scenarios + portal_scenarios + system,
    )
