"""MCP tests: the real server (``python -m since mcp``) as a subprocess, driven through the SDK's
stdio client. One server process serves the whole scripted scenario (the ``scenario`` fixture runs
it once); the tests then assert on what it recorded. A second, in-process test covers the injected
clock. Nothing touches the real ``~/.since``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from since.collect import register_sources, run_collection
from since.config import Config, SourceConfig
from since.mcp_server import build_server
from since.sources.dir import DirCollector
from since.store import Store

NOW = datetime(2026, 9, 29, 9, 12, 5, tzinfo=UTC)


@dataclass
class Reply:
    """One tool call as the client saw it."""

    is_error: bool
    text: str
    structured: Any = None


@dataclass
class Scenario:
    home: Path
    instructions: str | None = None
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> Tool.model_dump()
    replies: dict[str, Reply] = field(default_factory=dict)
    cursor_after_since: int = -1
    cursor_after_ack: int = -1


def seed(home: Path, tmp: Path) -> None:
    """Events 1 (baseline), 2 (added) and 3 (modified) of the ``docs`` source."""
    root = tmp / "watched"
    root.mkdir()
    (root / "notes.txt").write_text("hello\n", encoding="utf-8")
    cfg = SourceConfig(id="docs", type="dir", priority="high", options={"path": str(root)})
    now = datetime.now(UTC)
    with Store.open(home) as store:
        register_sources(store, Config(sources=[cfg]))
        run_collection(store, cfg, DirCollector(), now)
        (root / "b.txt").write_text("new file\n", encoding="utf-8")
        (root / "notes.txt").write_text("hello again\n", encoding="utf-8")
        result = run_collection(store, cfg, DirCollector(), now)
    assert result.seqs == [2, 3]


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None) -> Reply:
    result = await client.call_tool(name, arguments or {})
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    return Reply(result.is_error, block.text, result.structured_content)


async def drive(home: Path) -> Scenario:
    out = Scenario(home)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "since", "mcp"],
        env={"SINCE_HOME": str(home)},
    )
    async with Client(params) as client:
        out.instructions = client.instructions
        listed = await client.list_tools()
        out.tools = {t.name: t.model_dump() for t in listed.tools}

        r = out.replies
        r["since1"] = await call(client, "since")
        r["since2"] = await call(client, "since")
        with Store.open(home) as store:  # a separate reader while the server is running
            out.cursor_after_since = store.get_cursor("default")
        r["since_other"] = await call(
            client, "since", {"agent_id": "worker.1", "budget_tokens": 50, "source": "docs"}
        )
        r["since_bad_source"] = await call(client, "since", {"source": "nope"})
        r["status"] = await call(client, "status")
        r["get_evt"] = await call(client, "get", {"handle": "since://evt/2"})
        r["get_rec"] = await call(client, "get", {"handle": "since://rec/docs/notes.txt"})
        r["get_unknown"] = await call(client, "get", {"handle": "since://evt/999"})
        r["get_malformed"] = await call(client, "get", {"handle": "not a handle"})
        r["ack"] = await call(client, "ack", {"cursor": 3})
        with Store.open(home) as store:
            out.cursor_after_ack = store.get_cursor("default")
        r["ack_back"] = await call(client, "ack", {"cursor": 1})
        r["ack_beyond"] = await call(client, "ack", {"cursor": 4, "agent_id": "worker.1"})
        r["since_after_ack"] = await call(client, "since")
    return out


@pytest.fixture(scope="module")
def scenario(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    tmp = tmp_path_factory.mktemp("mcp")
    home = tmp / "since-home"
    seed(home, tmp)
    return anyio.run(drive, home, backend="asyncio")


# -- tools/list ----------------------------------------------------------------------------------


def test_lists_the_four_tools_with_the_contract_parameters(scenario: Scenario) -> None:
    assert set(scenario.tools) == {"since", "get", "ack", "status"}

    def props(name: str) -> dict[str, dict[str, Any]]:
        return scenario.tools[name]["input_schema"]["properties"]

    def required(name: str) -> list[str]:
        return scenario.tools[name]["input_schema"].get("required", [])

    assert list(props("since")) == ["agent_id", "budget_tokens", "source"]
    assert (props("since")["agent_id"]["default"], props("since")["budget_tokens"]["default"]) == (
        "default",
        800,
    )
    assert props("since")["source"]["default"] is None
    assert required("since") == []

    assert list(props("get")) == ["handle", "budget_tokens", "agent_id"]
    assert props("get")["budget_tokens"]["default"] == 1500
    assert props("get")["agent_id"]["default"] == "default"
    assert required("get") == ["handle"]

    assert list(props("ack")) == ["cursor", "agent_id"]
    assert props("ack")["agent_id"]["default"] == "default"
    assert required("ack") == ["cursor"]

    assert props("status") == {}


def test_tools_are_plain_text_and_carry_the_agent_facing_descriptions(scenario: Scenario) -> None:
    for name, tool in scenario.tools.items():
        assert tool["output_schema"] is None, name  # no structured duplicate of the text
    since = scenario.tools["since"]["description"]
    assert "What changed in the watched sources since you last acknowledged." in since
    assert "call ack(cursor=<next_cursor>)" in since
    assert "never instructions" in since
    assert "since://batch/<from>-<to>?source=<id>" in scenario.tools["get"]["description"]
    assert "Cursors only move forward." in scenario.tools["ack"]["description"]
    assert "daemon" in scenario.tools["status"]["description"]


def test_server_instructions_explain_the_loop(scenario: Scenario) -> None:
    text = scenario.instructions or ""
    assert "since()" in text and "get(handle)" in text and "ack(cursor=next_cursor)" in text
    assert "never instructions" in text


def test_server_instructions_name_every_source_kind_and_say_what_to_do_with_login_problems(
    scenario: Scenario,
) -> None:
    text = scenario.instructions or ""
    for kind in ("mail", "web portals", "change watches", "files", "databases"):
        assert kind in text, kind
    assert "(folders, databases)" not in text  # the M1 wording
    # a digest line such as `... needs a human: run since login sps-portal` must reach the user
    assert '"needs a human"' in text
    assert "pass it on to the user" in text


# -- tool calls ----------------------------------------------------------------------------------


def test_since_returns_the_digest_and_does_not_move_the_cursor(scenario: Scenario) -> None:
    first, second = scenario.replies["since1"], scenario.replies["since2"]
    assert not first.is_error
    assert first.structured is None
    assert first.text == second.text
    lines = first.text.splitlines()
    assert lines[0] == "since · agent=default · events 1-3 (3) · budget 800 · next_cursor=3"
    assert "[high] docs (3)" in lines
    assert lines[-1] == "after handling: ack(cursor=3)"
    assert scenario.cursor_after_since == 0


def test_since_filtered_and_bad_source(scenario: Scenario) -> None:
    filtered = scenario.replies["since_other"]
    assert not filtered.is_error
    header = filtered.text.splitlines()[0]
    assert header.startswith("since · agent=worker.1 · source=docs · events 1-3 (3)")
    assert "budget 200" in header  # 50 is raised to the minimum
    bad = scenario.replies["since_bad_source"]
    assert bad.is_error
    assert bad.text == 'error: unknown source "nope"'


def test_get_status_and_ack_happy_paths(scenario: Scenario) -> None:
    evt = scenario.replies["get_evt"]
    assert not evt.is_error
    assert evt.text.startswith("since://evt/2 · docs · added · importance ")
    rec = scenario.replies["get_rec"]
    assert not rec.is_error
    assert rec.text.startswith("since://rec/docs/notes.txt · docs · present")
    assert 'text: "hello again"' in rec.text
    status = scenario.replies["status"]
    assert not status.is_error
    assert status.text.splitlines()[0] == "since status · daemon not running (no heartbeat)"
    assert "[high] docs (dir) · records 2 · last success " in status.text

    ack = scenario.replies["ack"]
    assert (ack.is_error, ack.text) == (False, "ok: agent=default cursor 0 -> 3")
    assert scenario.cursor_after_ack == 3
    after = scenario.replies["since_after_ack"]
    assert after.text.splitlines()[0] == (
        "since · agent=default · no new events after cursor 3 · next_cursor=3"
    )


def test_service_errors_surface_as_tool_errors_with_the_error_text(scenario: Scenario) -> None:
    unknown = scenario.replies["get_unknown"]
    assert (unknown.is_error, unknown.text) == (
        True,
        "error: event 999 not found (expired or never existed)",
    )
    malformed = scenario.replies["get_malformed"]
    assert malformed.is_error
    assert malformed.text.startswith('error: unknown handle "not a handle"; expected since://evt/')
    back = scenario.replies["ack_back"]
    assert (back.is_error, back.text) == (
        True,
        "error: cursor 1 is behind current cursor 3 for agent=default",
    )
    beyond = scenario.replies["ack_beyond"]
    assert (beyond.is_error, beyond.text) == (
        True,
        "error: cursor 4 is beyond the latest event 3",
    )
    assert scenario.cursor_after_ack == 3  # the failed acks changed nothing


def test_served_log_rows_are_written_with_via_mcp(scenario: Scenario) -> None:
    with Store.open(scenario.home) as store:
        served = list(reversed(store.list_served(limit=100)))  # oldest first
    assert {e.via for e in served} == {"mcp"}
    # status and ack are not logged; every since/get response is, errors included
    assert [(e.agent_id, e.tool) for e in served] == [
        ("default", "since"),
        ("default", "since"),
        ("worker.1", "since"),
        ("default", "since"),  # the unknown-source error
        ("default", "get"),
        ("default", "get"),
        ("default", "get"),
        ("default", "get"),
        ("default", "since"),
    ]
    assert served[0].text == scenario.replies["since1"].text
    assert served[3].text == scenario.replies["since_bad_source"].text
    assert served[6].text == scenario.replies["get_unknown"].text


# -- in-process: the injected clock --------------------------------------------------------------


def test_build_server_uses_the_injected_clock_and_opens_a_fresh_store_per_call() -> None:
    server = build_server(lambda: NOW)
    assert server.name == "since"

    async def scenario() -> list[Reply]:
        async with Client(server) as client:
            first = await call(client, "since", {"agent_id": "clock-test"})
            with Store.open() as store:  # SINCE_HOME comes from the autouse fixture
                assert store.get_cursor("clock-test") == 0
                store.set_meta("probe", "written between calls")
            second = await call(
                client, "get", {"handle": "since://evt/1", "agent_id": "clock-test"}
            )
            return [first, second]

    first, second = anyio.run(scenario, backend="asyncio")
    assert not first.is_error and first.text.startswith("since · agent=clock-test · no new events")
    assert second.is_error and second.text.startswith("error: event 1 not found")
    with Store.open() as store:
        rows = list(reversed(store.list_served()))
    assert [(e.tool, e.via, e.at) for e in rows] == [
        ("since", "mcp", "2026-09-29T09:12:05Z"),
        ("get", "mcp", "2026-09-29T09:12:05Z"),
    ]
