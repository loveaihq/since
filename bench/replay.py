"""Replay of the benchmark world into a SINCE_HOME (M3 T3, D33): the data Arm B works on.

``replay(world, home)`` builds a SINCE_HOME from scratch by living through ``world.ticks`` (every
2 simulated hours) the way the daemon would: at each tick the fakes are brought to
``world.state_at(tick)`` and every source is collected once with ``now`` = the tick. The fakes are

- a small IMAP server (``tests.imap_fake``, the one the ``imap`` source tests use) holding the mail,
  with each mail's per-folder UID, INTERNALDATE and its flags as of the tick;
- a SQLite file ``<home>/world/po.db`` for the ``sql`` source (the PO table);
- a local HTTP server for the ``web`` source: ``/orders`` serves ``portal_html`` of the tick's
  portal state, or redirects to ``/login`` once the login has expired.

The three sources are configured the plain way an operator would (D37: po-table and sps-portal carry
the ``highlight: [{field: status, changed_to: Cancelled}]`` rule of CLAUDE.md's example, nothing
else is keyed to the answer key); the same config is written to ``<home>/since.yaml`` for reference
(its ports belong to the replay run). At the ``LAST_LOOK`` tick the bench agent
(``agent_id="bench"``) is acked to the highest seq: what Since holds after that is "what changed
since I last looked". After the last tick a fresh heartbeat is written, so digests carry no
stale-daemon warning.

The report says, for every planted item, whether its reference shows up in the full digest of the
bench agent, and for the ones that do not, why (a portal change after the layout change or after the
login expiry cannot be read by anything; Since reports the breakage itself as the ``system`` items).

Needs a browser for the portal (``browser_channel="msedge"`` on a Windows dev box). ``python -m
bench.replay --home <empty dir> [--channel msedge] [--seed N]`` prints a short summary.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from bench.world import (
    DEFAULT_SEED,
    FOLDER,
    KIND_PORTAL,
    KIND_SYSTEM,
    LAST_LOOK,
    LAYOUT_CHANGE_AT,
    LAYOUT_V1,
    LOGIN_EXPIRY_AT,
    OUR_DOMAIN,
    PORTAL_LOGIN_SELECTOR,
    PORTAL_V1_EXTRACT,
    SYSTEM_LAYOUT,
    SYSTEM_LOGIN,
    Mail,
    PortalState,
    World,
    build_world,
    portal_html,
)
from since.collect import register_sources, run_collection
from since.config import Config, load_config
from since.daemon import META_HEARTBEAT, META_MIN_SCHEDULE
from since.service import Service
from since.sources.imap import ImapCollector
from since.sources.sql import SqlCollector
from since.sources.web import WebCollector
from since.store import DB_FILENAME, Store
from since.timeutil import to_iso

BENCH_AGENT = "bench"
SOURCE_INBOX, SOURCE_PO, SOURCE_PORTAL = "inbox", "po-table", "sps-portal"
SCHEDULE = "every 2h"  # the replay's tick step
MIN_SCHEDULE_S = 7200
FULL_BUDGET = 100_000  # "everything": far more than the digest of the whole replay needs

IMAP_PASSWORD_ENV = "SINCE_BENCH_IMAP_PASSWORD"
PO_URL_ENV = "SINCE_BENCH_PO_URL"
IMAP_USER = f"ops@{OUR_DOMAIN}"

PO_TABLE = "purchase_orders"
_PO_COLUMNS = ("po_no", "supplier", "status", "eta", "qty", "updated_at")
_PO_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PO_TABLE} (po_no TEXT PRIMARY KEY, supplier TEXT NOT NULL, "
    "status TEXT NOT NULL, eta TEXT NOT NULL, qty INTEGER NOT NULL, updated_at TEXT NOT NULL)"
)
PO_QUERY = f"select {', '.join(_PO_COLUMNS)} from {PO_TABLE}"
# D37: the highlight rule of CLAUDE.md's config example (+10 importance on status -> Cancelled)
CANCELLED_HIGHLIGHT: list[dict[str, str]] = [{"field": "status", "changed_to": "Cancelled"}]

REASON_LAYOUT = "after layout change"
REASON_LOGIN = "after login expiry"
REASON_OTHER = "other"


class ReplayError(Exception):
    """The replay could not be carried out (a source failed when it should not have, or the target
    directory is not empty)."""


# -- the fakes -----------------------------------------------------------------------------------


def write_po_db(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Make ``path`` a SQLite file whose table ``purchase_orders`` holds exactly ``rows`` (created
    if missing, replaced otherwise). The connection is closed again, so a reader (SQLAlchemy in the
    ``sql`` source, or the raw MCP server) is never locked out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(_PO_DDL)
            conn.execute(f"DELETE FROM {PO_TABLE}")
            conn.executemany(
                f"INSERT INTO {PO_TABLE} ({', '.join(_PO_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?)",
                [tuple(row[c] for c in _PO_COLUMNS) for row in rows],
            )
    finally:
        conn.close()


class ImapMirror:
    """Brings a ``FakeImapServer`` to a world state: mails it has not seen yet are added with the
    world's UID, INTERNALDATE, size and header block, and changed flags are set. Everything is
    remembered here, so ``apply`` costs one dict lookup per mail."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self._flags: dict[tuple[str, int], tuple[str, ...]] = {}

    def apply(self, mails: Iterable[Mail]) -> None:
        for mail in mails:
            known = self._flags.get(mail.key)
            if known == mail.flags:
                continue
            if known is None:
                self._server.add_message(
                    mail.folder,
                    uid=mail.uid,
                    raw_header=mail.raw_header(),
                    flags=mail.flags,
                    internaldate=mail.received,
                    size=mail.size,
                )
            else:
                self._server.set_flags(mail.folder, mail.uid, *mail.flags)
            self._flags[mail.key] = mail.flags


class PortalServer:
    """The local portal on 127.0.0.1 (ephemeral port). ``/orders`` serves ``portal_html`` of the
    current ``portal`` state, or redirects to ``/login`` when its login has expired; ``/login``
    serves the login page. Anything else is a 404. Change ``portal`` between requests."""

    def __init__(self, portal: PortalState) -> None:
        self.portal = portal
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes = b"", location: str | None = None) -> None:
                self.send_response(status)
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                portal = owner.portal
                if path == "/orders" and portal.login_expired:
                    self._send(302, location="/login")
                elif path == "/orders":
                    self._send(200, portal_html(portal).encode("utf-8"))
                elif path == "/login":
                    login_page = portal_html(replace(portal, login_expired=True))
                    self._send(200, login_page.encode("utf-8"))
                else:
                    self._send(404, b"not found")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/orders"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    def __enter__(self) -> PortalServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextlib.contextmanager
def _environ(**values: str) -> Iterator[None]:
    """Set environment variables for the duration of the block, then put them back."""
    before = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, old in before.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


# -- configuration -------------------------------------------------------------------------------


def build_config(
    *,
    imap_port: int,
    portal_url: str,
    profile_dir: Path,
    folders: Sequence[str] = (FOLDER,),
    browser_channel: str | None = None,
) -> dict[str, Any]:
    """The ``since.yaml`` of the replay as a plain dict: three sources configured the ordinary way
    (priorities, key and tracked fields, the M2 extractor written for layout v1). The only highlight
    rule is the one of CLAUDE.md's example, on po-table and sps-portal (D37); nothing else depends
    on the planted content. Credentials come from env vars only."""
    portal: dict[str, Any] = {
        "id": SOURCE_PORTAL,
        "type": "web",
        "priority": "high",
        "schedule": SCHEDULE,
        "url": portal_url,
        "profile_dir": str(profile_dir),
        "login_detect": {"selector": PORTAL_LOGIN_SELECTOR},
        "extract": copy.deepcopy(PORTAL_V1_EXTRACT),
        "track_fields": ["status", "ship_by"],
        "highlight": copy.deepcopy(CANCELLED_HIGHLIGHT),
    }
    if browser_channel:
        portal["browser_channel"] = browser_channel
    return {
        "sources": [
            {
                "id": SOURCE_INBOX,
                "type": "imap",
                "priority": "normal",
                "schedule": SCHEDULE,
                "host": "127.0.0.1",
                "port": imap_port,
                "security": "none",
                "username": IMAP_USER,
                "password_env": IMAP_PASSWORD_ENV,
                "folders": list(folders),
                "since_days": 14,
            },
            {
                "id": SOURCE_PO,
                "type": "sql",
                "priority": "high",
                "schedule": SCHEDULE,
                "url_env": PO_URL_ENV,
                "query": PO_QUERY,
                "key": ["po_no"],
                "track_fields": ["status", "eta"],
                "highlight": copy.deepcopy(CANCELLED_HIGHLIGHT),
            },
            portal,
        ]
    }


def write_config(path: Path, data: dict[str, Any]) -> Config:
    """Write the config as YAML (for reference) and load it back through the real loader."""
    header = "# written by bench.replay for reference; the ports are those of the replay run\n"
    path.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_config(path)


# -- the report ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One planted item: was its reference in the full digest? If not, ``reason`` says why:
    ``after layout change`` / ``after login expiry`` (a portal change made after the portal became
    unreadable) or ``other`` (nothing explains it)."""

    kind: str
    ref: str
    observed: bool
    reason: str = ""


@dataclass(frozen=True)
class ReplayReport:
    home: Path
    ticks: int
    elapsed_s: float
    events: dict[str, int]  # source id -> events stored
    cursor: int  # the bench agent's cursor (highest seq at LAST_LOOK)
    max_seq: int
    digest: str  # since(agent_id="bench", budget_tokens=FULL_BUDGET)
    observability: list[Observation] = field(default_factory=list)

    def unobserved(self) -> list[Observation]:
        return [item for item in self.observability if not item.observed]

    def summary(self) -> str:
        total = sum(self.events.values())
        by_source = ", ".join(f"{name} {n}" for name, n in sorted(self.events.items()))
        seen = len(self.observability) - len(self.unobserved())
        lines = [
            f"replay: {self.ticks} ticks in {self.elapsed_s:.0f}s -> {self.home}",
            f"events: {by_source} (total {total}); agent {BENCH_AGENT} cursor {self.cursor} "
            f"of {self.max_seq}",
            f"planted items observed in the full digest: {seen}/{len(self.observability)}",
        ]
        for item in self.unobserved():
            lines.append(f"  not observed: {item.kind} {item.ref} ({item.reason})")
        return "\n".join(lines)


def _in_digest(digest: str, ref: str) -> bool:
    """The reference number as a whole number (not part of a longer one)."""
    return re.search(rf"(?<!\d){re.escape(ref)}(?!\d)", digest) is not None


def _reason(world: World, kind: str, ref: str) -> str:
    """Why an item is out of reach, from when the generator made it true. Only portal changes
    depend on the portal being readable; anything else that is missed is ``other``."""
    if kind == KIND_PORTAL:
        for scenario in world.scenarios:
            if scenario.role == "planted" and (scenario.kind, scenario.ref) == (kind, ref):
                if scenario.at >= LOGIN_EXPIRY_AT:
                    return REASON_LOGIN
                if scenario.at >= LAYOUT_CHANGE_AT:
                    return REASON_LAYOUT
    return REASON_OTHER


def observe(world: World, digest: str) -> list[Observation]:
    """For each planted item of ``world``: is it in ``digest``? Mail, PO and portal items by their
    reference number; ``portal-layout`` by a ``schema_changed`` line and ``portal-login`` by a
    ``login expired`` line."""
    found: list[Observation] = []
    for kind, ref in world.planted:
        if kind == KIND_SYSTEM and ref == SYSTEM_LAYOUT:
            observed = "schema_changed" in digest
        elif kind == KIND_SYSTEM and ref == SYSTEM_LOGIN:
            observed = "login expired" in digest
        else:
            observed = _in_digest(digest, ref)
        found.append(
            Observation(kind, ref, observed, "" if observed else _reason(world, kind, ref))
        )
    return found


def _utcnow() -> datetime:
    return datetime.now(UTC)


def digest_text(home: Path, budget_tokens: int = FULL_BUDGET, agent_id: str = BENCH_AGENT) -> str:
    """``since(agent_id, budget_tokens)`` over the database of ``home``, exactly as Since renders it
    now. It runs on a copy of the database, so the served log of ``home`` stays untouched (the
    benchmark agent must start with an empty one)."""
    with tempfile.TemporaryDirectory(
        prefix="since-bench-digest-", ignore_cleanup_errors=True
    ) as tmp:
        source = sqlite3.connect(home / DB_FILENAME)
        target = sqlite3.connect(Path(tmp) / DB_FILENAME)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        with Store.open(tmp) as store:
            return Service(store, _utcnow).since(agent_id, budget_tokens, via="replay")


# -- the replay ----------------------------------------------------------------------------------


def _collectors(tick: datetime) -> dict[str, Any]:
    """The collectors as of one tick: the imap source's clock is the simulated time."""
    return {
        "imap": ImapCollector(now_fn=lambda: tick),
        "sql": SqlCollector(),
        "web": WebCollector(),
    }


def _check_home(home: Path) -> None:
    if home.exists() and (not home.is_dir() or any(home.iterdir())):
        raise ReplayError(f"{home} is not an empty directory: replay builds a fresh SINCE_HOME")


def replay(
    world: World,
    home: Path,
    *,
    browser_channel: str | None = None,
) -> ReplayReport:
    """Build a SINCE_HOME in the (new or empty) directory ``home`` by replaying ``world``.

    ``browser_channel`` (``msedge`` / ``chrome``) picks the browser of the portal source; ``None``
    is Playwright's Chromium. Raises ``ReplayError`` if a source fails when it should not (mail and
    PO table: ever; portal: before the layout change)."""
    from tests.imap_fake import FakeImapServer  # dev-only test helper, only needed to replay

    home = Path(home)
    _check_home(home)
    home.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    work = home / "world"
    po_path = work / "po.db"
    first = world.state_at(world.ticks[0])
    write_po_db(po_path, first.po_rows)
    folders = sorted({mail.folder for mail in world.mails}) or [FOLDER]
    imap_password = secrets.token_urlsafe(16)  # a throwaway secret for the local fake
    po_url = f"sqlite:///{po_path.as_posix()}"

    with (
        FakeImapServer(username=IMAP_USER, password=imap_password) as imap,
        PortalServer(first.portal) as portal,
        _environ(**{IMAP_PASSWORD_ENV: imap_password, PO_URL_ENV: po_url}),
    ):
        for folder in folders:
            imap.add_mailbox(folder)
        data = build_config(
            imap_port=imap.port,
            portal_url=portal.url,
            profile_dir=home / "profiles" / SOURCE_PORTAL,
            folders=folders,
            browser_channel=browser_channel,
        )
        config = write_config(home / "since.yaml", data)
        mirror = ImapMirror(imap)
        with Store.open(home) as store:
            registration = register_sources(store, config, _collectors(world.ticks[0]))
            if registration.skipped:
                raise ReplayError(f"sources not collectable: {registration.skipped}")
            for tick in world.ticks:
                state = world.state_at(tick)
                mirror.apply(state.mails)
                write_po_db(po_path, state.po_rows)
                portal.portal = state.portal
                collectors = _collectors(tick)
                portal_broken = state.portal.layout != LAYOUT_V1 or state.portal.login_expired
                for cfg, _ in registration.collectable:
                    result = run_collection(store, cfg, collectors[cfg.type], tick)
                    expected = cfg.id == SOURCE_PORTAL and portal_broken
                    if result.superseded or (result.error is not None and not expected):
                        raise ReplayError(
                            f"source {cfg.id} failed at {to_iso(tick)}: "
                            f"{result.error or 'result superseded'}"
                        )
                if tick == LAST_LOOK:
                    store.set_cursor(BENCH_AGENT, store.max_seq(), tick)
            store.set_meta(META_HEARTBEAT, to_iso(_utcnow()))
            store.set_meta(META_MIN_SCHEDULE, MIN_SCHEDULE_S)
            events = dict(sorted(Counter(e.source_id for e in store.events_after(0)).items()))
            cursor, max_seq = store.get_cursor(BENCH_AGENT), store.max_seq()

    digest = digest_text(home)
    return ReplayReport(
        home=home,
        ticks=len(world.ticks),
        elapsed_s=time.monotonic() - started,
        events=events,
        cursor=cursor,
        max_seq=max_seq,
        digest=digest,
        observability=observe(world, digest),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench.replay",
        description="Replay the benchmark world into a fresh SINCE_HOME (needs a browser).",
    )
    parser.add_argument("--home", type=Path, required=True, help="new or empty directory")
    parser.add_argument("--channel", choices=("msedge", "chrome"), default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    try:
        report = replay(build_world(args.seed), args.home, browser_channel=args.channel)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 1 if any(item.reason == REASON_OTHER for item in report.unobserved()) else 0


if __name__ == "__main__":
    sys.exit(main())
