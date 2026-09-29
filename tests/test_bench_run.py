"""Runner, prompt, stream parser and report of the benchmark (``bench/run.py``, ``bench/prompt.py``,
M3 T4). No model calls: the CLI is faked by a small script that prints a recorded-style stream."""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

import bench.run as run_mod
from bench.grade import grade
from bench.prompt import ARMS, TOOLS, build_prompt
from bench.replay import Observation
from bench.run import (
    ARM_SPECS,
    BenchError,
    ProcResult,
    RunMetrics,
    child_env,
    claude_command,
    cli_failure,
    copy_since_home,
    execute_run,
    find_claude,
    main,
    mcp_config,
    parse_arms,
    parse_stream,
    render_report,
    run_process,
    world_block,
    write_reports,
)
from bench.world import LAST_LOOK, NOW, build_world
from since.daemon import META_HEARTBEAT
from since.store import Store

# -- the prompt ----------------------------------------------------------------------------------


def test_prompt_states_the_time_the_four_rules_and_the_answer_format() -> None:
    for arm in ARMS:
        text = build_prompt(arm)
        assert "2026-09-16 10:00 UTC" in text and "2026-09-15 09:00 UTC" in text
        assert f"{NOW:%Y-%m-%d %H:%M}" in text and f"{LAST_LOOK:%Y-%m-%d %H:%M}" in text
        assert "operations assistant for a wholesale supplier" in text
        assert (
            "Mail from a customer or a supplier that asks for action or reports a problem" in text
        )
        assert "1. " in text and "2. " in text and "3. " in text and "4. " in text
        assert "cancelled, or whose ETA has moved later by more than 3 days" in text
        assert "portal order that has been cancelled or put on hold" in text
        assert "the portal login has expired, or the portal layout has changed" in text
        assert "```json" in text and "nothing after it" in text
        assert (
            '{"items": [{"kind": "email|po|portal|system", '
            '"ref": "<reference number, or portal-login / portal-layout>"}]}'
        ) in text


def test_prompts_of_the_arms_differ_only_in_the_tool_paragraph() -> None:
    a, b = build_prompt("A"), build_prompt("B")
    assert a != b
    assert a.replace(TOOLS["A"], "<tools>") == b.replace(TOOLS["B"], "<tools>")
    assert TOOLS["A"] in a and TOOLS["B"] in b


def test_tool_paragraphs_only_name_the_tools() -> None:
    a, b = TOOLS["A"], TOOLS["B"]
    for word in ("mailbox", "purchase order database", "portal page", "notes"):
        assert word in a
    assert "agent_id" not in a and "Since" not in a
    assert "Since tools" in b and 'agent_id="bench"' in b
    assert "mailbox" not in b and "database" not in b


def test_unknown_arm_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_prompt("C")


# -- the CLI: path, environment, config, command ---------------------------------------------------


def test_find_claude_override_file_and_command(tmp_path: Path) -> None:
    exe = tmp_path / "my-claude.exe"
    exe.write_bytes(b"")
    assert find_claude({"SINCE_BENCH_CLAUDE": str(exe)}, platform="linux") == str(exe)
    which = {"my-claude": "/usr/bin/my-claude"}.get
    assert (
        find_claude({"SINCE_BENCH_CLAUDE": "my-claude"}, platform="linux", which=which)
        == "/usr/bin/my-claude"
    )
    with pytest.raises(BenchError, match="SINCE_BENCH_CLAUDE"):
        find_claude({"SINCE_BENCH_CLAUDE": "nope"}, platform="linux", which=lambda _name: None)


def test_find_claude_windows_takes_the_newest_version_numerically(tmp_path: Path) -> None:
    root = tmp_path / "Claude" / "claude-code"
    for version in ("2.1.9", "2.1.284", "2.1.281", "2.1.1000"):
        (root / version).mkdir(parents=True)
        (root / version / "claude.exe").write_bytes(b"")
    (root / "3.0.0").mkdir()  # newest by name, but holds no executable
    (root / "notes").mkdir()
    got = find_claude({"APPDATA": str(tmp_path)}, platform="win32", which=lambda _name: None)
    assert Path(got) == root / "2.1.1000" / "claude.exe"


def test_find_claude_falls_back_to_path_and_fails_clearly(tmp_path: Path) -> None:
    on_path = "/usr/local/bin/claude"
    assert find_claude({}, platform="darwin", which=lambda name: on_path) == on_path
    assert (
        find_claude({"APPDATA": str(tmp_path)}, platform="win32", which=lambda name: on_path)
        == on_path
    )  # no Claude app folder: PATH
    with pytest.raises(BenchError, match="SINCE_BENCH_CLAUDE"):
        find_claude({}, platform="linux", which=lambda _name: None)


def test_child_env_drops_session_variables_and_keeps_the_rest() -> None:
    base = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "s",
        "CLAUDE_CODE_ENTRYPOINT": "x",
        "CLAUDE_AGENT_SDK_VERSION": "1",
        "CLAUDE_EFFORT": "high",
        "CLAUDE_CODE_OAUTH_TOKEN": "keep-me",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "ANTHROPIC_API_KEY": "k",
        "PATH": "/bin",
        "SYSTEMROOT": "C:\\Windows",
    }
    env = child_env(base)
    assert env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "keep-me",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "ANTHROPIC_API_KEY": "k",
        "PATH": "/bin",
        "SYSTEMROOT": "C:\\Windows",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
    assert "CLAUDECODE" not in child_env(
        {"claudecode": "1"}
    )  # names are matched case-insensitively
    assert base["CLAUDECODE"] == "1"  # the argument is not modified


def test_mcp_config_arm_a(tmp_path: Path) -> None:
    config = mcp_config("A", seed=7, repo_root=tmp_path)
    assert list(config["mcpServers"]) == ["raw"]
    server = config["mcpServers"]["raw"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "bench.raw_mcp", "--seed", "7"]
    assert server["env"] == {"PYTHONPATH": str(tmp_path)}
    json.dumps(config)


def test_mcp_config_arm_b(tmp_path: Path) -> None:
    config = mcp_config("B", seed=7, since_home=tmp_path / "home")
    assert list(config["mcpServers"]) == ["since"]
    server = config["mcpServers"]["since"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "since", "mcp"]
    assert server["env"] == {"SINCE_HOME": str(tmp_path / "home")}
    with pytest.raises(ValueError):
        mcp_config("B", seed=7)


def test_claude_command_is_an_argument_list_with_the_isolation_flags(tmp_path: Path) -> None:
    for arm, allowed in (("A", "mcp__raw"), ("B", "mcp__since")):
        cmd = claude_command("C:/x/claude.exe", tmp_path / "mcp.json", arm, "sonnet", 3)
        assert cmd[0] == "C:/x/claude.exe" and cmd[1] == "-p"
        assert all(isinstance(part, str) for part in cmd)
        assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
        assert cmd[cmd.index("--mcp-config") + 1] == str(tmp_path / "mcp.json")
        assert "--strict-mcp-config" in cmd
        assert cmd[cmd.index("--tools") + 1] == ""  # no built-in tools at all
        assert cmd[cmd.index("--allowedTools") + 1] == allowed
        assert cmd[cmd.index("--setting-sources") + 1] == ""  # no CLAUDE.md, no hooks
        assert cmd[cmd.index("--model") + 1] == "sonnet"
        assert cmd[cmd.index("--max-budget-usd") + 1] == "3"
        assert "--no-session-persistence" in cmd
        assert "--bare" not in cmd  # it would refuse an OAuth login
    small = claude_command("c", tmp_path, "A", "sonnet", 0.5)
    assert small[small.index("--max-budget-usd") + 1] == "0.5"
    assert ARM_SPECS["A"].allowed_tools == "mcp__raw"


def test_parse_arms() -> None:
    assert parse_arms("A,B") == ["A", "B"]
    assert parse_arms(" b , a,B") == ["B", "A"]
    assert parse_arms("A") == ["A"]
    with pytest.raises(BenchError, match="unknown arm"):
        parse_arms("A,C")


# -- running a process ---------------------------------------------------------------------------


def test_run_process_passes_stdin_env_cwd_and_arguments_as_a_list(tmp_path: Path) -> None:
    script = (
        "import os, sys; "
        "print('|'.join([sys.stdin.read(), os.getcwd(), os.environ['X'], sys.argv[1]]))"
    )
    result = run_process(
        [sys.executable, "-c", script, "a b;c"],
        stdin_bytes=b"hello world",
        cwd=tmp_path,
        env={**child_env(), "X": "1"},
        timeout_s=60,
    )
    stdin, cwd, x, arg = result.stdout.decode("utf-8", "replace").strip().split("|")
    assert (stdin, x, arg) == ("hello world", "1", "a b;c")
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert result.returncode == 0 and not result.timed_out


def test_run_process_kills_a_run_that_takes_too_long(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import time; print('start', flush=True); time.sleep(120)"],
        stdin_bytes=b"",
        cwd=tmp_path,
        env=child_env(),
        timeout_s=2,
    )
    assert result.timed_out
    assert time.monotonic() - started < 60
    assert b"start" in result.stdout


def test_run_process_reports_a_program_that_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(BenchError, match="cannot start"):
        run_process(
            [str(tmp_path / "no-such-claude")], stdin_bytes=b"", cwd=tmp_path, env={}, timeout_s=5
        )


# -- the stream ----------------------------------------------------------------------------------

MODEL = "claude-sonnet-5-5"
ANSWER = (
    'Two items.\n```json\n{"items": [{"kind": "po", "ref": "4500108"}, '
    '{"kind": "system", "ref": "portal-login"}]}\n```'
)


def line(**event: Any) -> str:
    return json.dumps(event)


def init_event(server: str = "raw", status: str = "connected") -> str:
    return line(
        type="system",
        subtype="init",
        model=MODEL,
        claude_code_version="2.1.284",
        mcp_servers=[{"name": server, "status": status}],
        tools=[f"mcp__{server}__x"],
    )


def usage(inp: int, cc: int, cr: int, out: int) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": out,
    }


def assistant(mid: str, block: dict[str, Any], use: dict[str, int], model: str = MODEL) -> str:
    message = {"id": mid, "role": "assistant", "model": model, "content": [block], "usage": use}
    return line(type="assistant", message=message)


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_block(tool_id: str, name: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def tool_result(tool_id: str, is_error: bool = False) -> str:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "ok", "is_error": is_error}
    return line(type="user", message={"role": "user", "content": [block]})


def result_event(**fields: Any) -> str:
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 4321,
        "total_cost_usd": 0.1234,
        "usage": usage(60, 1200, 2200, 170),
        "result": ANSWER,
        "permission_denials": [],
    }
    return line(**{**base, **fields})


def normal_stream(
    answer: str = ANSWER, server: str = "raw", result_usage: dict[str, int] | None = None
) -> str:
    """Three API messages; message m1 arrives as three events (one per content block) with the same
    id and usage, m2 as two identical events. Real usage: 60 + 1200 + 2200 input, 170 output."""
    m1, m2, m3 = usage(10, 1000, 0, 40), usage(20, 200, 1000, 30), usage(30, 0, 1200, 100)
    tool = f"mcp__{server}__"
    totals = result_usage or usage(60, 1200, 2200, 170)
    return "\n".join(
        [
            init_event(server),
            assistant("m1", text_block("Let me look."), usage(10, 1000, 0, 5)),
            assistant("m1", tool_block("t1", tool + "list_emails"), m1),
            assistant("m1", tool_block("t2", tool + "sql_query"), m1),
            tool_result("t1"),
            tool_result("t2"),
            assistant("m2", tool_block("t3", tool + "fetch_portal"), m2),
            assistant("m2", tool_block("t3", tool + "fetch_portal"), m2),
            tool_result("t3", is_error=True),
            assistant("m3", text_block(answer), m3),
            result_event(result=answer, usage=totals),
        ]
    )


def test_parse_normal_stream_counts_duplicates_once() -> None:
    parsed = parse_stream(normal_stream())
    m = parsed.metrics
    assert m.status == "ok" and not m.is_error and m.error == ""
    assert m.tool_calls == 3 and m.tool_errors == 1
    assert m.tool_calls_by_name == {
        "mcp__raw__fetch_portal": 1,
        "mcp__raw__list_emails": 1,
        "mcp__raw__sql_query": 1,
    }
    assert m.api_calls == 3 and m.num_turns == 3
    # message ids counted once, the largest value seen per field: 10+20+30, ...
    assert m.turn_usage == usage(60, 1200, 2200, 170)
    assert m.usage_source == "result"
    assert (m.input_tokens, m.cache_creation_input_tokens, m.cache_read_input_tokens) == (
        60,
        1200,
        2200,
    )
    assert m.output_tokens == 170 and m.total_input_tokens == 60 + 1200 + 2200
    assert m.cost_usd == pytest.approx(0.1234) and m.duration_s == pytest.approx(4.321)
    assert (m.model, m.claude_version) == (MODEL, "2.1.284")
    assert m.mcp_servers == [{"name": "raw", "status": "connected"}]
    assert m.init_seen and m.bad_lines == 0
    assert parsed.final_text == ANSWER
    json.dumps(m.to_dict())


def test_result_totals_win_over_the_assistant_sums() -> None:
    m = parse_stream(normal_stream(result_usage=usage(99, 1, 2, 3))).metrics
    assert (m.input_tokens, m.cache_creation_input_tokens, m.cache_read_input_tokens) == (99, 1, 2)
    assert m.output_tokens == 3 and m.total_input_tokens == 102
    assert m.turn_usage == usage(60, 1200, 2200, 170)  # kept for cross-checking


def test_result_without_usage_falls_back_to_the_assistant_messages() -> None:
    lines = normal_stream().splitlines()
    lines[-1] = result_event(usage=None)
    m = parse_stream("\n".join(lines)).metrics
    assert m.usage_source == "assistant" and m.status == "ok"
    assert m.total_input_tokens == 3460 and m.output_tokens == 170


def test_streamed_partial_usage_takes_the_largest_value_per_message() -> None:
    lines = [
        init_event(),
        assistant("m1", text_block("a"), usage(5, 0, 0, 1)),
        assistant("m1", text_block("b"), usage(5, 0, 0, 90)),
        assistant("m1", text_block("c"), usage(5, 0, 0, 20)),
    ]
    m = parse_stream("\n".join(lines)).metrics
    assert m.turn_usage["output_tokens"] == 90 and m.turn_usage["input_tokens"] == 5


def test_budget_exceeded_result() -> None:
    lines = [
        init_event(),
        assistant("m1", tool_block("t1", "mcp__raw__list_emails"), usage(10, 100, 0, 10)),
        tool_result("t1"),
        assistant("m2", text_block("Still working on the emails"), usage(20, 0, 100, 10)),
        line(
            type="result",
            subtype="error_max_budget_usd",
            is_error=True,
            num_turns=2,
            duration_ms=1000,
            total_cost_usd=1.02,
            usage=usage(30, 100, 100, 20),
            errors=["Reached maximum budget ($1)"],
        ),
    ]
    parsed = parse_stream("\n".join(lines))
    m = parsed.metrics
    assert m.status == "error_max_budget_usd" and m.is_error
    assert "budget" in m.error
    assert m.cost_usd == pytest.approx(1.02) and m.num_turns == 2
    assert parsed.final_text == "Still working on the emails"  # no result text: last assistant text
    assert (
        cli_failure(m, "A", ProcResult(b"", b"", 1, 1.0, False)) == ""
    )  # a run, not a launch problem


def test_missing_result_is_marked_and_counted_from_the_assistant_messages() -> None:
    lines = [
        init_event(),
        assistant("m1", tool_block("t1", "mcp__raw__list_emails"), usage(10, 100, 0, 10)),
        assistant("m2", text_block("partial answer"), usage(20, 0, 100, 10)),
    ]
    parsed = parse_stream("\n".join(lines), wall_s=12.5)
    m = parsed.metrics
    assert m.status == "no_result" and m.is_error and "without a result" in m.error
    assert m.usage_source == "assistant" and m.total_input_tokens == 230
    assert m.cost_usd is None and m.duration_s == 12.5 and m.num_turns == 2
    assert parsed.final_text == "partial answer"


def test_empty_and_garbage_streams() -> None:
    parsed = parse_stream("")
    assert parsed.metrics.status == "no_result" and not parsed.metrics.init_seen
    assert parsed.final_text == ""
    messy = "\r\n".join(["", "not json", "[1, 2]", init_event(), "  ", result_event()])
    m = parse_stream(messy).metrics
    assert m.bad_lines == 2 and m.status == "ok" and m.init_seen


def test_login_failure_looks_like_a_cli_failure_not_a_run() -> None:
    lines = [
        init_event(),
        assistant(
            "x", text_block("Not logged in · Please run /login"), usage(0, 0, 0, 0), "<synthetic>"
        ),
        result_event(
            is_error=True,
            result="Not logged in · Please run /login",
            usage=usage(0, 0, 0, 0),
            total_cost_usd=0,
            terminal_reason="api_error",
        ),
    ]
    m = parse_stream("\n".join(lines)).metrics
    assert m.status == "error" and m.api_calls == 0
    why = cli_failure(m, "A", ProcResult(b"", b"", 1, 1.0, False))
    assert "before the model answered" in why and "Not logged in" in why
    assert "claude auth login" in why and "ANTHROPIC_API_KEY" in why


def test_cli_failure_cases() -> None:
    ok = ProcResult(b"", b"", 0, 1.0, False)
    assert cli_failure(parse_stream(normal_stream()).metrics, "A", ok) == ""
    down = parse_stream("\n".join([init_event(status="failed"), result_event()])).metrics
    assert "did not connect" in cli_failure(down, "A", ok)
    other = parse_stream("\n".join([init_event(server="since"), result_event()])).metrics
    assert cli_failure(other, "B", ok) == ""  # arm B looks for its own server
    assert "did not connect" in cli_failure(other, "A", ok)
    silent = parse_stream("").metrics
    why = cli_failure(silent, "A", ProcResult(b"", b"boom: bad flag", 2, 0.1, False))
    assert "no output" in why and "boom: bad flag" in why and "exit code 2" in why
    assert cli_failure(silent, "A", ProcResult(b"", b"", None, 9.0, True)) == ""  # timeout: a run


# -- one run and the whole command ---------------------------------------------------------------

FAKE_CLAUDE = textwrap.dedent(
    """
    import json, os, pathlib, sys
    prompt = sys.stdin.buffer.read().decode("utf-8")
    config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    (server,) = config["mcpServers"]
    env = config["mcpServers"][server].get("env", {})
    home = env.get("SINCE_HOME")
    entry = {
        "server": server,
        "cwd": os.getcwd(),
        "prompt": prompt,
        "claudecode": os.environ.get("CLAUDECODE"),
        "session_var": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "auto_memory": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"),
        "home_db": bool(home and pathlib.Path(home, "since.db").exists()),
    }
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\\n")
    stream = pathlib.Path(os.environ["FAKE_CLAUDE_STREAM"]).read_text(encoding="utf-8")
    sys.stdout.write(stream.replace("__SERVER__", server))
    """
)


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Make the runner start ``FAKE_CLAUDE`` instead of the real CLI; set ``stream`` to what it
    prints and read ``log`` for what it was started with."""
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    log, stream = tmp_path / "launches.jsonl", tmp_path / "stream.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", str(stream))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-calling-session")
    monkeypatch.setattr(
        run_mod,
        "claude_command",
        lambda claude, config_path, arm, model, budget: [
            sys.executable,
            str(script),
            str(config_path),
        ],
    )
    return {"log": log, "stream": stream}


def launches(fake: dict[str, Path]) -> list[dict[str, Any]]:
    if not fake["log"].exists():
        return []
    return [json.loads(x) for x in fake["log"].read_text(encoding="utf-8").splitlines()]


def stream_for(answer_items: list[tuple[str, str]]) -> str:
    """A fake CLI stream (server name filled in by the fake) whose final answer lists the items."""
    items = [{"kind": kind, "ref": ref} for kind, ref in answer_items]
    answer = "Done.\n```json\n" + json.dumps({"items": items}) + "\n```"
    return normal_stream(answer, server="__SERVER__")


@pytest.fixture(scope="module")
def world() -> Any:
    return build_world()


@pytest.fixture
def no_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the browser: the 'replay' only creates an empty since.db and says 16 of 18 planted
    items are observable."""

    def fake_replay(w: Any, home: Path, channel: str | None) -> list[Observation]:
        Store.open(home).close()
        assert channel == "msedge"
        return observations(w)

    monkeypatch.setattr(run_mod, "_replay_or_reuse", fake_replay)


def test_copy_since_home_is_private_and_refreshes_the_heartbeat(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    with Store.open(src) as store:
        store.set_meta(META_HEARTBEAT, "2020-01-01T00:00:00Z")
        store.set_cursor("bench", 0, LAST_LOOK)
    copy_since_home(src, dst)
    with Store.open(dst) as copy:
        assert copy.get_meta(META_HEARTBEAT) != "2020-01-01T00:00:00Z"
        assert copy.get_meta(META_HEARTBEAT).startswith("20") and copy.get_cursor("bench") == 0
        copy.set_meta("scribble", "1")
    with Store.open(src) as original:
        assert original.get_meta("scribble") is None  # the copy is independent
        assert original.get_meta(META_HEARTBEAT) == "2020-01-01T00:00:00Z"
    assert [p.name for p in dst.iterdir() if not p.name.startswith("since.db")] == []


def test_execute_run_launches_isolated_saves_the_stream_and_grades(
    tmp_path: Path, fake_cli: dict[str, Path], world: Any
) -> None:
    home = tmp_path / "since-home"
    Store.open(home).close()
    out = tmp_path / "out"
    out.mkdir()
    planted = [tuple(x) for x in world.planted]
    fake_cli["stream"].write_text(stream_for(planted[:6]), encoding="utf-8")
    records = {}
    for arm in ("A", "B"):
        records[arm] = execute_run(
            arm=arm,
            n=2,
            out=out,
            claude="claude",
            model="sonnet",
            max_budget_usd=1,
            seed=world.seed,
            since_home=home,
            planted=planted,
            observable=planted[:16],
            timeout_s=120,
        )
    a, b = launches(fake_cli)
    assert (a["server"], b["server"]) == ("raw", "since")
    for entry in (a, b):
        assert entry["claudecode"] is None and entry["session_var"] is None
        assert entry["auto_memory"] == "1"
        assert entry["prompt"] in (build_prompt("A"), build_prompt("B"))
        cwd = Path(entry["cwd"])
        assert cwd.name == "cwd" and not cwd.exists()  # a fresh temp directory, removed after
        assert not cwd.resolve().is_relative_to(Path(run_mod.REPO_ROOT).resolve())
    assert a["prompt"] == build_prompt("A") and b["prompt"] == build_prompt("B")
    assert (a["home_db"], b["home_db"]) == (False, True)  # B ran on a copy of the replayed home

    rec = records["A"]
    assert (rec["arm"], rec["run"], rec["model"], rec["cli_failure"]) == ("A", 2, "sonnet", "")
    assert rec["stream"] == "A-2.jsonl" and (out / "A-2.jsonl").exists()
    assert build_prompt("A")[:40] not in json.dumps(rec["command"])  # the prompt is on stdin
    assert rec["metrics"]["tool_calls"] == 3 and rec["metrics"]["status"] == "ok"
    assert rec["malformed"] is False and len(rec["reported"]) == 6
    assert rec["grade"]["precision"] == 1.0
    assert rec["grade"]["recall"] == pytest.approx(6 / 18)
    assert rec["grade"]["recall_observable"] == pytest.approx(6 / 16)
    json.dumps(rec)
    assert not list(out.glob("*.stderr.txt"))


def test_execute_run_flags_a_cli_that_cannot_work(
    tmp_path: Path, fake_cli: dict[str, Path], world: Any
) -> None:
    lines = [
        init_event("__SERVER__"),
        assistant("x", text_block("Not logged in"), usage(0, 0, 0, 0), "<synthetic>"),
        result_event(is_error=True, result="Not logged in", usage=usage(0, 0, 0, 0)),
    ]
    fake_cli["stream"].write_text("\n".join(lines), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    rec = execute_run(
        arm="A",
        n=1,
        out=out,
        claude="claude",
        model="sonnet",
        max_budget_usd=1,
        seed=world.seed,
        since_home=tmp_path,
        planted=world.planted,
        observable=world.planted,
        timeout_s=120,
    )
    assert "Not logged in" in rec["cli_failure"]
    assert rec["malformed"] and rec["grade"]["recall"] == 0.0


def run_main(out: Path, *extra: str) -> int:
    return main(["--out", str(out), "--channel", "msedge", *extra])


def test_main_runs_exactly_the_requested_runs_and_writes_the_reports(
    tmp_path: Path,
    fake_cli: dict[str, Path],
    no_replay: None,
    monkeypatch: pytest.MonkeyPatch,
    world: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()
    monkeypatch.setattr(run_mod, "BENCH_DIR", bench_dir)
    monkeypatch.setattr(run_mod, "find_claude", lambda: "claude")
    planted = [tuple(x) for x in world.planted]
    fake_cli["stream"].write_text(stream_for(planted[:9]), encoding="utf-8")
    out = tmp_path / "results" / "smoke"

    assert run_main(out, "--arms", "A,B", "--runs", "2", "--model", "sonnet") == 0
    assert len(launches(fake_cli)) == 4  # 2 arms x 2 runs, not one more
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [(r["arm"], r["run"]) for r in data["runs"]] == [("A", 1), ("B", 1), ("A", 2), ("B", 2)]
    assert data["world"]["summary"]["planted_total"] == 18
    assert (out / "REPORT.md").exists() and (bench_dir / "REPORT.md").exists()
    assert (bench_dir / "REPORT.md").read_text(encoding="utf-8") == (out / "REPORT.md").read_text(
        encoding="utf-8"
    )
    assert "### Arm A" in (out / "REPORT.md").read_text(encoding="utf-8")
    assert sorted(p.name for p in out.glob("*.jsonl")) == [
        "A-1.jsonl",
        "A-2.jsonl",
        "B-1.jsonl",
        "B-2.jsonl",
    ]
    assert "A-1: ok" in capsys.readouterr().out

    # the same out dir again: the replayed home and earlier runs are kept, numbering goes on
    assert run_main(out, "--arms", "B", "--runs", "1") == 0
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [(r["arm"], r["run"]) for r in data["runs"]][-1] == ("B", 3) and len(data["runs"]) == 5
    assert len(launches(fake_cli)) == 5


def test_main_stops_at_the_first_cli_failure(
    tmp_path: Path,
    fake_cli: dict[str, Path],
    no_replay: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()
    monkeypatch.setattr(run_mod, "BENCH_DIR", bench_dir)
    monkeypatch.setattr(run_mod, "find_claude", lambda: "claude")
    fake_cli["stream"].write_text("", encoding="utf-8")  # the CLI prints nothing at all
    out = tmp_path / "out"
    assert run_main(out, "--arms", "A,B", "--runs", "3") == 2
    assert len(launches(fake_cli)) == 1
    err = capsys.readouterr().err
    assert "stopping after A-1" in err and "no output" in err
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert len(data["runs"]) == 1 and data["runs"][0]["cli_failure"]
    # a report of nothing but a failure would overwrite a good bench/REPORT.md: not written
    assert not (bench_dir / "REPORT.md").exists() and not (out / "REPORT.md").exists()


def test_main_stops_when_the_cli_cannot_be_started(
    tmp_path: Path,
    no_replay: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_mod, "BENCH_DIR", tmp_path)
    monkeypatch.setattr(run_mod, "find_claude", lambda: str(tmp_path / "no-such-claude"))
    assert run_main(tmp_path / "out", "--runs", "3") == 2
    assert "cannot start" in capsys.readouterr().err
    assert not (tmp_path / "REPORT.md").exists()  # nothing ran, nothing to report


def test_main_rejects_bad_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_mod.main(["--arms", "Z", "--out", str(tmp_path / "o")]) == 2
    assert run_mod.main(["--runs", "0", "--out", str(tmp_path / "o")]) == 2
    assert run_mod.main(["--max-budget-usd", "0", "--out", str(tmp_path / "o")]) == 2
    err = capsys.readouterr().err
    assert "unknown arm" in err and "--runs" in err and "--max-budget-usd" in err


# -- results.json and the report -----------------------------------------------------------------


def observations(world: Any) -> list[Observation]:
    """All planted items observed except the last two portal items (as the replay reports them)."""
    portal = [ref for kind, ref in world.planted if kind == "portal"][-2:]
    return [
        Observation(kind, ref, kind != "portal" or ref not in portal, "after login expiry")
        for kind, ref in world.planted
    ]


def make_run(
    arm: str,
    n: int,
    world: Any,
    *,
    answer: list[tuple[str, str]],
    total_input: int,
    cost: float | None,
    status: str = "ok",
    cli_fail: str = "",
    malformed: bool = False,
) -> dict[str, Any]:
    observable = [(o.kind, o.ref) for o in observations(world) if o.observed]
    metrics = RunMetrics(
        status=status,
        is_error=status != "ok",
        error="",
        input_tokens=total_input // 10,
        cache_creation_input_tokens=total_input // 10,
        cache_read_input_tokens=total_input * 8 // 10,
        output_tokens=total_input // 100,
        total_input_tokens=total_input,
        cost_usd=cost,
        num_turns=4,
        api_calls=4,
        duration_s=12.5,
        tool_calls=7,
        tool_errors=0,
        tool_calls_by_name={"x": 7},
        usage_source="result",
        turn_usage={},
        permission_denials=0,
        terminal_reason="",
        init_seen=True,
        model="claude-sonnet-5-5",
        claude_version="2.1.284",
        mcp_servers=[],
        bad_lines=0,
    )
    return {
        "arm": arm,
        "run": n,
        "model": "sonnet",
        "max_budget_usd": 3.0,
        "wall_s": 20.5,
        "metrics": metrics.to_dict(),
        "malformed": malformed,
        "cli_failure": cli_fail,
        "grade": grade(answer, world.planted, observable).to_dict(),
    }


def synthetic_results(world: Any) -> dict[str, Any]:
    planted = list(world.planted)
    decoy = world.decoys[0]
    good = planted[:12]
    return {
        "version": 1,
        "updated": "2026-09-29T12:00:00Z",
        "world": world_block(world, observations(world)),
        "runs": [
            make_run("A", 1, world, answer=good + [decoy[:2]], total_input=100_000, cost=0.5),
            make_run("A", 2, world, answer=good[:6], total_input=300_000, cost=1.5),
            make_run("B", 1, world, answer=planted, total_input=20_000, cost=0.1),
            make_run(
                "B",
                2,
                world,
                answer=planted[:-1] + [("po", "9999999")],
                total_input=30_000,
                cost=None,
            ),
            make_run(
                "B",
                3,
                world,
                answer=[],
                total_input=0,
                cost=0.0,
                status="error_max_budget_usd",
                malformed=True,
            ),
            make_run(
                "B",
                4,
                world,
                answer=[],
                total_input=0,
                cost=0.0,
                status="error",
                cli_fail="not logged in",
            ),
        ],
    }


def test_world_block_describes_the_answer_key(world: Any) -> None:
    block = world_block(world, observations(world))
    assert len(block["planted"]) == 18 and len(block["observable"]) == 16
    assert len(block["unobservable"]) == 2 and block["unobservable"][0]["reason"]
    assert block["summary"]["planted_total"] == 18
    assert {"kind", "ref", "why"} <= set(block["decoys"][0])
    json.dumps(block)


def test_render_report_tables_stats_and_lists(world: Any) -> None:
    text = render_report(synthetic_results(world))
    assert text.startswith("# Since benchmark report")
    for heading in ("## The world", "## Results per arm", "## Runs", "## Most common misses"):
        assert heading in text
    # world summary
    assert (
        "18 planted items" in text and "Observable by Since (in its full digest): 16 of 18" in text
    )
    assert text.count("- Not observable:") == 2 and "after login expiry" in text
    assert "255 messages" in text and "20 orders" in text
    # arm A: total input mean and range, cost mean and range, recall mean
    assert "### Arm A – raw tools (2 runs)" in text
    assert "| Input tokens, total (incl. cache) | 200,000 | 100,000–300,000 |" in text
    assert "| Cost (USD) | $1.000 | $0.500–$1.500 |" in text
    assert "| Tool calls | 7 | 7 |" in text
    assert "| Wall time (s) | 20.5 | 20.5 |" in text  # the process wall time of the record
    # arm B: the CLI-failure run is left out of the statistics, and the cost that is unknown too
    assert "### Arm B – Since (3 runs, 1 not ok)" in text
    assert "| Cost (USD) | $0.050 | $0.000–$0.100 |" in text
    assert "n/a" in text  # run B-2 has no cost
    # per-run rows
    assert "| A | 1 | ok |" in text and "| B | 3 | error_max_budget_usd, no valid answer |" in text
    assert "| B | 4 | CLI failure |" in text
    # misses and false positives, with the reasons
    assert "Missed (planted, not reported), most common first:" in text
    assert "False positives (reported, not in the answer key)" in text
    assert f"({world.decoys[0][2]})" in text  # a decoy that was reported says why it is one
    assert "po 9999999: 1/3 runs (not a planted item and not a known decoy)" in text
    unobservable = world_block(world, observations(world))["unobservable"][0]
    assert f"{unobservable['kind']} {unobservable['ref']}" in text


def test_render_report_of_one_arm_only(world: Any) -> None:
    results = synthetic_results(world)
    results["runs"] = [r for r in results["runs"] if r["arm"] == "A"]
    text = render_report(results)
    assert "### Arm A" in text and "### Arm B" not in text and "Runs: arm A: 2" in text


def test_write_reports_copies_the_report(tmp_path: Path, world: Any) -> None:
    out = tmp_path / "out"
    out.mkdir()
    publish = tmp_path / "REPORT.md"
    path = write_reports(out, synthetic_results(world), publish)
    assert path == out / "REPORT.md"
    assert publish.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
    write_reports(out, synthetic_results(world), None)  # no copy requested: nothing else written
    assert sorted(p.name for p in tmp_path.iterdir()) == ["REPORT.md", "out"]


def test_report_can_be_rendered_from_the_json_file(tmp_path: Path, world: Any) -> None:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(synthetic_results(world)), encoding="utf-8")
    assert render_report(json.loads(path.read_text(encoding="utf-8"))).endswith("\n")
    assert "## Runs" in render_report(synthetic_results(world))


def test_report_counts_one_run_in_the_singular(world: Any) -> None:
    results = synthetic_results(world)
    results["runs"] = results["runs"][:1]
    assert "### Arm A – raw tools (1 run)" in render_report(results)


def test_progress_lines_survive_any_console_encoding(capsys: pytest.CaptureFixture[str]) -> None:
    run_mod._say("claude: C:/Users/Zoë/中文/claude.exe")
    out = capsys.readouterr().out
    assert out.isascii() and "claude.exe" in out
