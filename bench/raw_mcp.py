"""Arm A of the benchmark (D34): raw tools over the world as of now, as an MCP stdio server.

``python -m bench.raw_mcp [--seed N]`` serves ``world.state_at(NOW)`` and ``world.notes()`` with
six plain-text tools and nothing else. There is no notion of "what changed": the agent has to read
the current state and diff it against its notes from the last look inside its own context, which is
the baseline Since is measured against.

- ``list_emails`` / ``read_email``: the mailbox, newest first, and one message;
- ``sql_query``: a read-only SQL statement over the PO database. The database is built from the NOW
  state into a temp file at start-up. A statement must be a ``SELECT`` / ``WITH``; besides that the
  file is opened read-only (``mode=ro``, ``query_only``) and a SQLite authorizer denies everything
  but reading, so ``UPDATE`` / ``ATTACH`` / ``PRAGMA`` cannot get through even if the check on the
  first word were fooled; a statement that runs longer than ``QUERY_TIMEOUT_S`` is interrupted;
- ``fetch_portal``: the visible text of the portal page as it is now (the login page: the login
  expired);
- ``list_notes`` / ``read_note``: what the agent saved at its last look (``last_look.txt``,
  ``po_table.csv``, ``portal.txt``).

Every reply is plain text. A failure comes back as text starting with ``error: `` and ``isError``
set. Nothing is written to stdout except protocol frames.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from bench.replay import PO_TABLE, write_po_db
from bench.world import (
    DEFAULT_SEED,
    NOW,
    Mail,
    World,
    WorldState,
    build_world,
    portal_text,
)

SERVER_NAME = "raw"
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 50
MAX_SQL_ROWS = 500
QUERY_TIMEOUT_S = 10.0

LIST_EMAILS_DESCRIPTION = (
    "List the messages of a mailbox folder, newest first, one per line: "
    "uid | received (UTC) | from | subject | flags. The first line gives the number of messages "
    "in the folder and the page shown. offset: messages to skip; limit: page size (default 50, "
    "at most 100)."
)
READ_EMAIL_DESCRIPTION = (
    "Return one message of a mailbox folder: uid, folder, received time, flags, its headers and "
    "its full body."
)
SQL_QUERY_DESCRIPTION = (
    "Run one read-only SQL statement (SELECT or WITH) on the purchase order database and return "
    f"the rows as a plain-text table (at most {MAX_SQL_ROWS} rows). "
    f"Table {PO_TABLE}(po_no TEXT, supplier TEXT, status TEXT, eta TEXT as a date, qty INTEGER, "
    "updated_at TEXT as a UTC timestamp)."
)
FETCH_PORTAL_DESCRIPTION = (
    "Fetch the customer portal page as it is right now and return its visible text, one block "
    "per line, table cells separated by ' | '."
)
LIST_NOTES_DESCRIPTION = "List the notes saved at the last look: name and size in characters."
READ_NOTE_DESCRIPTION = "Return the text of one note saved at the last look."

_LEADING_COMMENTS = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)


class ToolFailure(Exception):
    """A tool call that cannot be answered; ``str(exc)`` is what the agent is told."""


def _authorize(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _cell(value: Any) -> str:
    text = "NULL" if value is None else str(value)
    return " ".join(text.split())


def _flags(mail: Mail) -> str:
    return " ".join(mail.flags) or "-"


def _minute(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M}Z"


@dataclass(frozen=True)
class RawWorld:
    """The world as of NOW plus the notes and the path of the PO database file."""

    state: WorldState
    notes: dict[str, str]
    db_path: Path

    @classmethod
    def build(cls, world: World, db_path: Path) -> RawWorld:
        state = world.state_at(NOW)
        write_po_db(db_path, state.po_rows)
        return cls(state, world.notes(), db_path)

    # -- mail ----------------------------------------------------------------------------------

    def _folder(self, folder: str) -> list[Mail]:
        mails = [m for m in self.state.mails if m.folder == folder]
        if not mails:
            known = sorted({m.folder for m in self.state.mails})
            raise ToolFailure(f"unknown folder {folder!r}; folders: {', '.join(known)}")
        return sorted(mails, key=lambda m: (m.received, m.uid), reverse=True)

    def list_emails(
        self, folder: str = "INBOX", offset: int = 0, limit: int = DEFAULT_LIST_LIMIT
    ) -> str:
        if offset < 0 or limit < 1:
            raise ToolFailure("offset must be >= 0 and limit >= 1")
        limit = min(limit, MAX_LIST_LIMIT)
        mails = self._folder(folder)
        page = mails[offset : offset + limit]
        if page:
            shown = f"showing {offset + 1}-{offset + len(page)} (newest first)"
        else:
            shown = f"nothing at offset {offset}"
        more = offset + len(page) < len(mails)
        head = f"folder {folder}: {len(mails)} messages, {shown}"
        head += f"; next page: offset={offset + len(page)}" if more else "; end of folder"
        lines = [head]
        for mail in page:
            lines.append(
                f"{mail.uid} | {_minute(mail.received)} | {mail.from_header} | "
                f"{mail.subject} | {_flags(mail)}"
            )
        return "\n".join(lines)

    def read_email(self, uid: int, folder: str = "INBOX") -> str:
        for mail in self._folder(folder):
            if mail.uid == uid:
                break
        else:
            raise ToolFailure(f"no message with uid {uid} in folder {folder}")
        header = mail.raw_header().decode("ascii").replace("\r\n", "\n")
        return (
            f"uid: {mail.uid}\nfolder: {mail.folder}\nreceived: {_minute(mail.received)}\n"
            f"flags: {_flags(mail)}\n{header}{mail.body}"
        )

    # -- PO database ---------------------------------------------------------------------------

    def sql_query(self, sql: str) -> str:
        head = _LEADING_COMMENTS.sub("", sql, count=1)
        word = re.match(r"[A-Za-z]+", head)
        if word is None or word.group().lower() not in ("select", "with"):
            raise ToolFailure("only read-only SELECT / WITH statements are allowed")
        conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.set_authorizer(_authorize)
            deadline = time.monotonic() + QUERY_TIMEOUT_S
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
        except sqlite3.Error as exc:
            raise ToolFailure(str(exc)) from None
        finally:
            conn.close()
        shown = rows[:MAX_SQL_ROWS]
        lines = [" | ".join(columns)]
        lines += [" | ".join(_cell(v) for v in row) for row in shown]
        if len(rows) > MAX_SQL_ROWS:
            lines.append(f"({len(shown)} rows shown; the result has more, cut at {MAX_SQL_ROWS})")
        else:
            lines.append(f"({len(shown)} row{'' if len(shown) == 1 else 's'})")
        return "\n".join(lines)

    # -- portal and notes ----------------------------------------------------------------------

    def fetch_portal(self) -> str:
        return portal_text(self.state.portal)

    def list_notes(self) -> str:
        return "\n".join(f"{name} ({len(text)} chars)" for name, text in self.notes.items())

    def read_note(self, name: str) -> str:
        if name not in self.notes:
            raise ToolFailure(f"no note named {name!r}; notes: {', '.join(self.notes)}")
        return self.notes[name]


def _reply(run: Callable[[], str]) -> CallToolResult:
    """Plain text; a ``ToolFailure`` becomes ``error: <message>`` with ``isError`` set."""
    try:
        text, failed = run(), False
    except ToolFailure as exc:
        text, failed = f"error: {exc}", True
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=failed)


def build_server(raw: RawWorld) -> MCPServer:
    """The MCP server with the six tools over ``raw``."""
    server = MCPServer(SERVER_NAME)

    @server.tool(name="list_emails", description=LIST_EMAILS_DESCRIPTION, structured_output=False)
    def list_emails(
        folder: str = "INBOX", offset: int = 0, limit: int = DEFAULT_LIST_LIMIT
    ) -> CallToolResult:
        return _reply(lambda: raw.list_emails(folder, offset, limit))

    @server.tool(name="read_email", description=READ_EMAIL_DESCRIPTION, structured_output=False)
    def read_email(uid: int, folder: str = "INBOX") -> CallToolResult:
        return _reply(lambda: raw.read_email(uid, folder))

    @server.tool(name="sql_query", description=SQL_QUERY_DESCRIPTION, structured_output=False)
    def sql_query(sql: str) -> CallToolResult:
        return _reply(lambda: raw.sql_query(sql))

    @server.tool(name="fetch_portal", description=FETCH_PORTAL_DESCRIPTION, structured_output=False)
    def fetch_portal() -> CallToolResult:
        return _reply(raw.fetch_portal)

    @server.tool(name="list_notes", description=LIST_NOTES_DESCRIPTION, structured_output=False)
    def list_notes() -> CallToolResult:
        return _reply(raw.list_notes)

    @server.tool(name="read_note", description=READ_NOTE_DESCRIPTION, structured_output=False)
    def read_note(name: str) -> CallToolResult:
        return _reply(lambda: raw.read_note(name))

    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m bench.raw_mcp",
        description="Arm A of the benchmark: raw MCP tools over the simulated world (stdio).",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    world = build_world(args.seed)
    with tempfile.TemporaryDirectory(prefix="since-bench-raw-", ignore_cleanup_errors=True) as tmp:
        raw = RawWorld.build(world, Path(tmp) / "po.db")
        build_server(raw).run("stdio")


if __name__ == "__main__":
    main()
