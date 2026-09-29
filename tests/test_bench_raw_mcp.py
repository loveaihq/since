"""Arm A of the benchmark (``bench/raw_mcp.py``, M3 T3): the raw MCP server over the world as of
NOW. One server process (``python -m bench.raw_mcp``) is driven over stdio by the SDK client in a
scripted session; the tests then assert on what it answered. The read-only guard of ``sql_query``
is also tested in-process against the same code, statement by statement.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from bench import raw_mcp
from bench.raw_mcp import MAX_LIST_LIMIT, MAX_SQL_ROWS, RawWorld, ToolFailure
from bench.world import (
    KIND_EMAIL,
    NOW,
    World,
    build_world,
    portal_text,
)

ROOT = Path(__file__).resolve().parents[1]

# Statements that must never get through, whatever they try (the guard is layered: first word,
# read-only file, query_only, authorizer, one statement only).
REFUSED = [
    "UPDATE purchase_orders SET status = 'Open'",
    "update purchase_orders set qty = 0 where po_no = '4500101'",
    "DELETE FROM purchase_orders",
    "INSERT INTO purchase_orders VALUES ('1', 's', 'Open', '2026-10-01', 1, '2026-09-01')",
    "DROP TABLE purchase_orders",
    "CREATE TABLE t (x)",
    "ALTER TABLE purchase_orders ADD COLUMN x",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA writable_schema = ON",
    "PRAGMA query_only = OFF",
    "PRAGMA table_info(purchase_orders)",
    "VACUUM",
    "REPLACE INTO purchase_orders (po_no) VALUES ('x')",
    "-- a comment first\nUPDATE purchase_orders SET qty = 0",
    "/* a comment first */ DELETE FROM purchase_orders",
    "SELECT 1; DELETE FROM purchase_orders",
    "SELECT 1; UPDATE purchase_orders SET qty = 0",
    "WITH x AS (SELECT 1) DELETE FROM purchase_orders",
    "WITH x AS (SELECT 1) UPDATE purchase_orders SET qty = 0",
    "",
    ";",
    "   ",
]

WITH_QUERY = (
    "WITH c AS (SELECT status, count(*) AS n FROM purchase_orders GROUP BY status) "
    "SELECT status, n FROM c ORDER BY status"
)
LONG_QUERY = (  # 1000 rows: cut at 500
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1000) SELECT i FROM n"
)


@pytest.fixture(scope="module")
def world() -> World:
    return build_world()


def _first_planted_email(world: World) -> str:
    return next(ref for kind, ref in world.planted if kind == KIND_EMAIL)


@pytest.fixture
def raw(world: World, tmp_path: Path) -> RawWorld:
    return RawWorld.build(world, tmp_path / "po.db")


# -- the scripted stdio session ------------------------------------------------------------------


@dataclass
class Reply:
    is_error: bool
    text: str
    structured: Any = None


@dataclass
class Session:
    instructions: str | None = None
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    replies: dict[str, Reply] = field(default_factory=dict)
    pages: list[Reply] = field(default_factory=list)  # list_emails, 100 at a time, newest first
    refused: list[Reply] = field(default_factory=list)  # one per statement in REFUSED


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None) -> Reply:
    result = await client.call_tool(name, arguments or {})
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    return Reply(result.is_error, block.text, result.structured_content)


async def drive(world: World) -> Session:
    out = Session()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bench.raw_mcp"],
        env={"PYTHONPATH": str(ROOT)},
        cwd=ROOT,
    )
    now = world.state_at(NOW)
    planted_mail = next(m for m in now.mails if m.ref == _first_planted_email(world))
    async with Client(params) as client:
        out.instructions = client.instructions
        out.tools = {t.name: t.model_dump() for t in (await client.list_tools()).tools}

        r = out.replies
        r["list_default"] = await call(client, "list_emails")
        offset = 0
        while offset < len(now.mails):
            out.pages.append(await call(client, "list_emails", {"offset": offset, "limit": 100}))
            offset += 100
        r["list_capped"] = await call(client, "list_emails", {"limit": 5000})
        r["list_beyond"] = await call(client, "list_emails", {"offset": 10_000})
        r["list_bad_folder"] = await call(client, "list_emails", {"folder": "Nope"})
        r["list_bad_limit"] = await call(client, "list_emails", {"limit": 0})
        r["read"] = await call(client, "read_email", {"uid": planted_mail.uid})
        r["read_missing"] = await call(client, "read_email", {"uid": 99_999})
        r["read_bad_folder"] = await call(client, "read_email", {"uid": 1, "folder": "Nope"})

        r["po_all"] = await call(
            client, "sql_query", {"sql": "SELECT * FROM purchase_orders ORDER BY po_no"}
        )
        r["po_count"] = await call(
            client, "sql_query", {"sql": "SELECT count(*) AS n FROM purchase_orders"}
        )
        r["po_with"] = await call(client, "sql_query", {"sql": WITH_QUERY})
        r["po_cap"] = await call(client, "sql_query", {"sql": LONG_QUERY})
        r["po_syntax"] = await call(client, "sql_query", {"sql": "SELECT FROM WHERE"})
        for statement in REFUSED:
            out.refused.append(await call(client, "sql_query", {"sql": statement}))
        r["po_after"] = await call(
            client, "sql_query", {"sql": "SELECT * FROM purchase_orders ORDER BY po_no"}
        )

        r["portal"] = await call(client, "fetch_portal")
        r["notes"] = await call(client, "list_notes")
        for name in world.notes():
            r[f"note:{name}"] = await call(client, "read_note", {"name": name})
        r["note_missing"] = await call(client, "read_note", {"name": "../since.db"})
    return out


@pytest.fixture(scope="module")
def session(world: World) -> Session:
    return anyio.run(drive, world, backend="asyncio")


def _table_lines(text: str) -> list[str]:
    """The rows of a ``sql_query`` reply: between the column line and the trailing ``(n rows)``."""
    lines = text.split("\n")
    return lines[1:-1]


# -- tools/list ----------------------------------------------------------------------------------


def test_lists_the_six_tools_with_their_parameters(session: Session) -> None:
    assert set(session.tools) == {
        "list_emails",
        "read_email",
        "sql_query",
        "fetch_portal",
        "list_notes",
        "read_note",
    }

    def props(name: str) -> dict[str, dict[str, Any]]:
        return session.tools[name]["input_schema"].get("properties", {})

    def required(name: str) -> list[str]:
        return session.tools[name]["input_schema"].get("required", [])

    assert set(props("list_emails")) == {"folder", "offset", "limit"}
    assert props("list_emails")["folder"]["default"] == "INBOX"
    assert props("list_emails")["offset"]["default"] == 0
    assert props("list_emails")["limit"]["default"] == 50
    assert required("list_emails") == []
    assert set(props("read_email")) == {"uid", "folder"}
    assert required("read_email") == ["uid"]
    assert set(props("sql_query")) == {"sql"}
    assert required("sql_query") == ["sql"]
    assert props("fetch_portal") == {}
    assert props("list_notes") == {}
    assert set(props("read_note")) == {"name"}


def test_descriptions_are_neutral_and_replies_are_plain_text(
    session: Session, world: World
) -> None:
    for name, tool in session.tools.items():
        description = tool["description"].lower()
        assert description, name
        for ref in (ref for _, ref in world.planted if ref.isdigit()):
            assert ref not in description
        for word in ("cancel", "urgent", "attention", "problem", "overdue", "expire", "layout"):
            assert word not in description, (name, word)
    for reply in session.replies.values():
        assert reply.structured is None  # text only, no structured copy


# -- mail ----------------------------------------------------------------------------------------


def test_default_page_is_50_newest_first(session: Session, world: World) -> None:
    now = world.state_at(NOW)
    lines = session.replies["list_default"].text.split("\n")
    assert lines[0] == (
        f"folder INBOX: {len(now.mails)} messages, showing 1-50 (newest first); "
        "next page: offset=50"
    )
    rows = lines[1:]
    assert len(rows) == 50
    newest = max(now.mails, key=lambda m: (m.received, m.uid))
    first = rows[0].split(" | ")
    assert first[0] == str(newest.uid)
    assert first[1] == f"{newest.received:%Y-%m-%d %H:%M}Z"
    assert first[2] == newest.from_header
    assert first[3] == newest.subject
    assert first[4] == (" ".join(newest.flags) or "-")


def test_paging_covers_every_mail_once(session: Session, world: World) -> None:
    now = world.state_at(NOW)
    assert len(now.mails) >= 200
    uids: list[int] = []
    stamps: list[str] = []
    for page in session.pages:
        assert not page.is_error
        head, *rows = page.text.split("\n")
        assert head.startswith(f"folder INBOX: {len(now.mails)} messages, showing ")
        assert len(rows) <= MAX_LIST_LIMIT
        for row in rows:
            uid, received, *_ = row.split(" | ")
            uids.append(int(uid))
            stamps.append(received)
    assert len(uids) == len(now.mails)  # the counts add up to the NOW mail count
    assert sorted(uids) == [m.uid for m in now.mails]  # each exactly once
    assert stamps == sorted(stamps, reverse=True)  # newest first across pages
    assert session.pages[-1].text.split("\n")[0].endswith("end of folder")
    assert "next page: offset=100" in session.pages[0].text.split("\n")[0]


def test_limit_is_capped_and_bad_arguments_are_errors(session: Session, world: World) -> None:
    now = world.state_at(NOW)
    capped = session.replies["list_capped"]
    assert not capped.is_error
    assert len(capped.text.split("\n")) == 1 + MAX_LIST_LIMIT
    assert f"showing 1-{MAX_LIST_LIMIT}" in capped.text.split("\n")[0]
    beyond = session.replies["list_beyond"]
    assert not beyond.is_error
    assert beyond.text == (
        f"folder INBOX: {len(now.mails)} messages, nothing at offset 10000; end of folder"
    )
    for name in ("list_bad_folder", "list_bad_limit", "read_missing", "read_bad_folder"):
        assert session.replies[name].is_error, name
        assert session.replies[name].text.startswith("error: "), name
    assert "INBOX" in session.replies["list_bad_folder"].text  # says which folders exist


def test_read_email_returns_headers_and_body(session: Session, world: World) -> None:
    now = world.state_at(NOW)
    mail = next(m for m in now.mails if m.ref == _first_planted_email(world))
    reply = session.replies["read"]
    assert not reply.is_error
    text = reply.text
    assert f"uid: {mail.uid}\n" in text
    assert f"Subject: {mail.subject}\n" in text
    assert f"From: {mail.from_header}\n" in text
    assert f"Message-ID: {mail.message_id}\n" in text
    assert f"Date: {mail.date_header}\n" in text
    assert text.endswith(mail.body)
    assert "\r" not in text


# -- PO database ---------------------------------------------------------------------------------


def test_select_returns_the_po_table_as_of_now(session: Session, world: World) -> None:
    now = world.state_at(NOW)
    count = session.replies["po_count"]
    assert not count.is_error
    assert count.text == f"n\n{len(now.po_rows)}\n(1 row)"

    everything = session.replies["po_all"].text.split("\n")
    assert everything[0] == "po_no | supplier | status | eta | qty | updated_at"
    assert everything[-1] == f"({len(now.po_rows)} rows)"
    rows = everything[1:-1]
    assert len(rows) == len(now.po_rows) >= 50
    for line, row in zip(rows, now.po_rows, strict=True):
        assert line == " | ".join(
            str(row[c]) for c in ("po_no", "supplier", "status", "eta", "qty", "updated_at")
        )

    with_reply = session.replies["po_with"]
    assert not with_reply.is_error
    assert _table_lines(with_reply.text)  # a WITH statement works too


def test_result_is_cut_at_500_rows(session: Session) -> None:
    reply = session.replies["po_cap"]
    assert not reply.is_error
    lines = reply.text.split("\n")
    assert lines[0] == "i"
    assert len(lines) == 1 + MAX_SQL_ROWS + 1
    assert lines[-1] == f"({MAX_SQL_ROWS} rows shown; the result has more, cut at {MAX_SQL_ROWS})"


def test_writes_are_refused_over_stdio_and_change_nothing(session: Session) -> None:
    assert len(session.refused) == len(REFUSED)
    for statement, reply in zip(REFUSED, session.refused, strict=True):
        assert reply.is_error, statement
        assert reply.text.startswith("error: "), statement
    assert session.replies["po_syntax"].is_error
    assert session.replies["po_after"].text == session.replies["po_all"].text


@pytest.mark.parametrize("statement", REFUSED)
def test_read_only_guard_in_process(raw: RawWorld, statement: str) -> None:
    before = raw.sql_query("SELECT * FROM purchase_orders ORDER BY po_no")
    with pytest.raises(ToolFailure):
        raw.sql_query(statement)
    assert raw.sql_query("SELECT * FROM purchase_orders ORDER BY po_no") == before


def test_authorizer_alone_stops_a_statement_that_starts_like_a_read(raw: RawWorld) -> None:
    # Starts with WITH, so the first-word check passes; the file is read-only and the authorizer
    # denies the write anyway.
    with pytest.raises(ToolFailure, match="not authorized|readonly"):
        raw.sql_query("WITH x AS (SELECT 1) DELETE FROM purchase_orders")


def test_schema_can_be_read_but_no_pragma(raw: RawWorld) -> None:
    text = raw.sql_query("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert "purchase_orders" in text


def test_a_runaway_query_is_interrupted(raw: RawWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(raw_mcp, "QUERY_TIMEOUT_S", 0.05)
    with pytest.raises(ToolFailure, match="interrupted"):
        raw.sql_query(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
        )


# -- portal and notes ----------------------------------------------------------------------------


def test_portal_is_the_login_page_as_of_now(session: Session, world: World) -> None:
    now = world.state_at(NOW)
    assert now.portal.login_expired
    reply = session.replies["portal"]
    assert not reply.is_error
    assert reply.text == portal_text(now.portal)
    assert "Session expired" in reply.text
    assert not any(order["order_no"] in reply.text for order in now.portal.rows)


def test_notes_are_the_state_at_the_last_look(session: Session, world: World) -> None:
    notes = world.notes()
    listed = session.replies["notes"].text.split("\n")
    assert (
        [line.split(" (")[0] for line in listed]
        == list(notes)
        == [
            "last_look.txt",
            "po_table.csv",
            "portal.txt",
        ]
    )
    for name, text in notes.items():
        reply = session.replies[f"note:{name}"]
        assert not reply.is_error
        assert reply.text == text
    assert re.fullmatch(r"2026-09-15T09:00:00Z\n", notes["last_look.txt"])
    missing = session.replies["note_missing"]
    assert missing.is_error
    assert missing.text.startswith("error: ")
    assert "po_table.csv" in missing.text  # says which notes exist
