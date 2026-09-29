"""The benchmark runner (M3 T4, D35): Arm A (raw tools) against Arm B (Since tools), headless.

``python -m bench.run [--arms A,B] [--runs 3] [--model sonnet] [--max-budget-usd 3] [--seed N]
[--channel msedge] [--out DIR]`` builds the world, replays it once into ``<out>/since-home`` (reused
when it is already there), then runs the task prompt once per arm and run through the Claude Code
CLI in headless mode, and grades the final answer against the planted list.

Isolation. The benchmarked agent must see its arm's MCP server and nothing else of this machine:
``--strict-mcp-config`` (only the arm's server), ``--tools ""`` (no built-in tools),
``--setting-sources ""`` (no user / project / local settings: no CLAUDE.md, no hooks; checked with a
request-capturing fake API endpoint), ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1``, a fresh empty temp
directory as cwd, and an environment without ``CLAUDECODE`` / ``CLAUDE_*`` session variables.
``--bare`` is not used: it never reads OAuth logins (Anthropic auth would have to be an API key);
``--safe-mode`` is not used either: it drops ``--mcp-config``.

Login. The CLI must be able to authenticate on its own: ``claude auth login`` once in a terminal,
or ``ANTHROPIC_API_KEY`` / ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``) in the environment
(both are passed through; nothing else is). A session that only holds the login of a host
application (a desktop app) has none: the CLI then says "Not logged in" and the runner stops.

Per run the raw stream is saved as ``<out>/<arm>-<n>.jsonl``; metrics and the grade are appended to
``<out>/results.json``; the report ``<out>/REPORT.md`` is copied to ``bench/REPORT.md``. The
runner never starts more runs than requested and stops at the first failure to start the CLI (or
to get an answer out of it: not logged in, MCP server not connected).

Local paths. Everything the runner writes (streams, ``results.json``, the reports) has this user's
home directory replaced by ``~`` (``scrub_text``), in every spelling a path takes in JSON or text.

``--report-only --out DIR`` starts nothing: it re-reads an existing results directory, scrubs it the
same way, recomputes the metrics and grades from the saved streams (and the world facts from the
seed), and rewrites ``DIR/REPORT.md`` and ``bench/REPORT.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.grade import (
    ARM_A_LIMITS,
    Item,
    arm_a_observable_in,
    extract_items,
    grade,
    normalise,
)
from bench.prompt import ARMS, EFFORT, build_prompt, system_prompt
from bench.replay import (
    MIN_SCHEDULE_S,
    Observation,
    ReplayError,
    digest_text,
    observe,
    replay,
)
from bench.world import DEFAULT_SEED, World, build_world
from since.daemon import META_HEARTBEAT, META_MIN_SCHEDULE
from since.store import DB_FILENAME, Store
from since.timeutil import to_iso

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_VERSION = 1
SYNTHETIC_MODEL = "<synthetic>"  # the CLI's own messages (e.g. "Not logged in"), not the model's

# CLAUDE_* variables that carry the login or the provider choice, not the calling session
_KEEP_ENV = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    }
)


class BenchError(Exception):
    """The benchmark cannot go on (bad setup, CLI cannot be started, replay failed)."""


@dataclass(frozen=True)
class ArmSpec:
    key: str
    label: str
    server: str  # MCP server name in the config; its tools are mcp__<server>__<tool>

    @property
    def allowed_tools(self) -> str:
        return f"mcp__{self.server}"


ARM_SPECS = {
    "A": ArmSpec("A", "raw tools", "raw"),
    "B": ArmSpec("B", "Since", "since"),
}


# -- the claude CLI ------------------------------------------------------------------------------


def _version_key(name: str) -> tuple[int, ...]:
    parts = name.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else (-1,)


def find_claude(
    environ: dict[str, str] | None = None,
    *,
    platform: str | None = None,
    which: Any = shutil.which,
) -> str:
    """The Claude Code CLI: ``SINCE_BENCH_CLAUDE`` if set; on Windows the newest
    ``%APPDATA%\\Claude\\claude-code\\<version>\\claude.exe``; else ``claude`` on PATH."""
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    override = env.get("SINCE_BENCH_CLAUDE")
    if override:
        if Path(override).is_file():
            return override
        found = which(override)
        if found:
            return str(found)
        raise BenchError(f"SINCE_BENCH_CLAUDE={override!r} is neither a file nor a command on PATH")
    appdata = env.get("APPDATA")
    if platform == "win32" and appdata:
        root = Path(appdata) / "Claude" / "claude-code"
        if root.is_dir():
            versions = [d for d in root.iterdir() if (d / "claude.exe").is_file()]
            if versions:
                newest = max(versions, key=lambda d: (_version_key(d.name), d.name))
                return str(newest / "claude.exe")
    found = which("claude")
    if found:
        return str(found)
    raise BenchError(
        "the claude CLI was not found: install Claude Code, put `claude` on PATH, or set "
        "SINCE_BENCH_CLAUDE to its full path"
    )


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of the CLI: the caller's, without ``CLAUDECODE`` and the ``CLAUDE_*``
    session variables (a login or provider choice is kept), and with auto-memory off."""
    env = {}
    for name, value in (os.environ if base is None else base).items():
        upper = name.upper()
        if upper == "CLAUDECODE" or (upper.startswith("CLAUDE_") and upper not in _KEEP_ENV):
            continue
        env[name] = value
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def mcp_config(
    arm: str,
    *,
    seed: int,
    repo_root: Path = REPO_ROOT,
    since_home: Path | None = None,
) -> dict[str, Any]:
    """The ``--mcp-config`` JSON of an arm: A runs the raw server of ``bench.raw_mcp`` over the
    world of ``seed``; B runs ``since mcp`` on ``since_home`` (a private copy of the replayed
    one)."""
    spec = ARM_SPECS[arm]
    if arm == "A":
        server: dict[str, Any] = {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "bench.raw_mcp", "--seed", str(seed)],
            "env": {"PYTHONPATH": str(repo_root)},
        }
    else:
        if since_home is None:
            raise ValueError("arm B needs a SINCE_HOME")
        server = {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "since", "mcp"],
            "env": {"SINCE_HOME": str(since_home)},
        }
    return {"mcpServers": {spec.server: server}}


def claude_command(
    claude: str, config_path: Path, arm: str, model: str, max_budget_usd: float
) -> list[str]:
    """The argument list (no shell). The prompt is not in it: it goes to the CLI's stdin."""
    return [
        claude,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        "--tools",
        "",
        "--allowedTools",
        ARM_SPECS[arm].allowed_tools,
        "--setting-sources",
        "",
        "--system-prompt",
        system_prompt(),
        "--effort",
        EFFORT,
        "--model",
        model,
        "--max-budget-usd",
        f"{max_budget_usd:g}",
        "--no-session-persistence",
    ]


# -- running a process ---------------------------------------------------------------------------


@dataclass
class ProcResult:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    wall_s: float
    timed_out: bool


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill the process and everything it started (the CLI starts the MCP servers)."""
    with contextlib.suppress(Exception):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


def run_process(
    cmd: list[str],
    *,
    stdin_bytes: bytes,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
) -> ProcResult:
    """Run ``cmd`` (a list, no shell) feeding ``stdin_bytes``; kill it and its children after
    ``timeout_s``. Raises ``BenchError`` if the program cannot be started at all."""
    started = time.monotonic()
    posix = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **posix,
        )
    except OSError as exc:
        raise BenchError(f"cannot start {cmd[0]!r}: {exc}") from exc
    timed_out = False
    try:
        out, err = proc.communicate(stdin_bytes, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    return ProcResult(out, err, proc.returncode, time.monotonic() - started, timed_out)


# -- the stream ----------------------------------------------------------------------------------

USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _usage(raw: Any) -> dict[str, int]:
    data = raw if isinstance(raw, dict) else {}
    return {name: _int(data.get(name)) for name in USAGE_FIELDS}


def _total_input(usage: dict[str, int]) -> int:
    return (
        usage["input_tokens"]
        + usage["cache_creation_input_tokens"]
        + usage["cache_read_input_tokens"]
    )


@dataclass
class RunMetrics:
    """What one run cost and did. ``usage`` is the token count used in the report: the ``result``
    event's totals when the stream has one (``usage_source == "result"``), else the sum over the
    assistant messages; ``turn_usage`` is always the latter (each message counted once). Input
    counts of the two agree; the output count of an assistant event is the value at the start of
    the message (the CLI does not update it), so ``turn_usage`` undercounts output tokens: the
    ``result`` totals are the ones to trust. ``final_context_tokens`` is the input (including
    cache) of the last model request: the size of the context the model held when it answered."""

    status: str  # ok | error | no_result | <the result subtype, e.g. error_max_budget_usd>
    is_error: bool
    error: str
    input_tokens: int  # not served from / written to the cache
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    total_input_tokens: int  # the three input kinds together, summed over all requests
    final_context_tokens: int  # the three input kinds of the last model request alone
    cost_usd: float | None
    num_turns: int
    api_calls: int  # distinct assistant messages produced by the model
    duration_s: float
    tool_calls: int
    tool_errors: int
    tool_calls_by_name: dict[str, int]
    usage_source: str
    turn_usage: dict[str, int]
    permission_denials: int
    terminal_reason: str
    init_seen: bool
    model: str
    claude_version: str
    mcp_servers: list[dict[str, Any]]
    bad_lines: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Parsed:
    metrics: RunMetrics
    final_text: str


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def parse_stream(text: str, wall_s: float = 0.0) -> Parsed:
    """Metrics and the final answer text from a ``--output-format stream-json`` transcript.

    Assistant events that carry the same message id are one API message (the CLI emits one event
    per content block): usage is counted once per id (the largest values seen), a tool_use block
    once per its id. Totals come from the last ``result`` event when there is one; a stream
    without it (crash, kill, timeout) is counted from the assistant messages and marked as such.
    Lines that are not JSON objects are skipped and counted."""
    init: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    order: list[str] = []
    models: dict[str, str] = {}
    usage_by_msg: dict[str, dict[str, int]] = {}
    texts: dict[str, list[str]] = {}
    tools: dict[str, str] = {}
    tool_errors = 0
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(event, dict):
            bad += 1
            continue
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            init = event
        elif kind == "result":
            result = event
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            message = event["message"]
            mid = message.get("id")
            if not isinstance(mid, str) or not mid:
                mid = f"no-id-{len(order)}"
            if mid not in usage_by_msg:
                order.append(mid)
                usage_by_msg[mid] = _usage(None)
                texts[mid] = []
            models[mid] = str(message.get("model") or models.get(mid, ""))
            seen = _usage(message.get("usage"))
            usage_by_msg[mid] = {k: max(usage_by_msg[mid][k], seen[k]) for k in USAGE_FIELDS}
            for block_text in _text_blocks(message.get("content")):
                if block_text not in texts[mid]:
                    texts[mid].append(block_text)
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    key = tool_id if isinstance(tool_id, str) and tool_id else f"no-id-{len(tools)}"
                    tools.setdefault(key, str(block.get("name") or "?"))
        elif kind == "user" and isinstance(event.get("message"), dict):
            content = event["message"].get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_errors += 1 if block.get("is_error") else 0

    real = [mid for mid in order if models.get(mid) != SYNTHETIC_MODEL]
    turn_usage = {name: sum(usage_by_msg[mid][name] for mid in real) for name in USAGE_FIELDS}

    if result is not None and isinstance(result.get("usage"), dict):
        usage, source = _usage(result["usage"]), "result"
    else:
        usage, source = dict(turn_usage), "assistant"

    subtype = str((result or {}).get("subtype") or "")
    if result is None:
        status, is_error = "no_result", True
        error = "the stream ended without a result event"
    else:
        is_error = bool(result.get("is_error"))
        ok = not is_error and subtype in ("", "success")
        status = "ok" if ok else (subtype if subtype not in ("", "success") else "error")
        error = ""
        if not ok:
            errors = result.get("errors")
            error = str(
                result.get("result")
                or ("; ".join(map(str, errors)) if isinstance(errors, list) and errors else "")
                or result.get("terminal_reason")
                or subtype
            )[:300]

    final_text = ""
    if result is not None and isinstance(result.get("result"), str) and result["result"].strip():
        final_text = result["result"]
    else:
        for mid in reversed(order):
            if texts[mid]:
                final_text = "\n".join(texts[mid])
                break

    cost = (result or {}).get("total_cost_usd")
    duration_ms = (result or {}).get("duration_ms")
    denials = (result or {}).get("permission_denials")
    servers = init.get("mcp_servers")
    metrics = RunMetrics(
        status=status,
        is_error=is_error,
        error=error,
        input_tokens=usage["input_tokens"],
        cache_creation_input_tokens=usage["cache_creation_input_tokens"],
        cache_read_input_tokens=usage["cache_read_input_tokens"],
        output_tokens=usage["output_tokens"],
        total_input_tokens=_total_input(usage),
        final_context_tokens=_total_input(usage_by_msg[real[-1]]) if real else 0,
        cost_usd=float(cost)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool)
        else None,
        num_turns=_int((result or {}).get("num_turns")) or len(real),
        api_calls=len(real),
        duration_s=_int(duration_ms) / 1000 if duration_ms is not None else wall_s,
        tool_calls=len(tools),
        tool_errors=tool_errors,
        tool_calls_by_name=dict(sorted(Counter(tools.values()).items())),
        usage_source=source,
        turn_usage=turn_usage,
        permission_denials=len(denials) if isinstance(denials, list) else 0,
        terminal_reason=str((result or {}).get("terminal_reason") or ""),
        init_seen=bool(init),
        model=str(init.get("model") or ""),
        claude_version=str(init.get("claude_code_version") or ""),
        mcp_servers=[s for s in servers if isinstance(s, dict)]
        if isinstance(servers, list)
        else [],
        bad_lines=bad,
    )
    return Parsed(metrics, final_text)


def cli_failure(metrics: RunMetrics, arm: str, proc: ProcResult) -> str:
    """Why the CLI could not do its job (so that further runs would fail the same way), or ``""``:
    no init event, the arm's MCP server did not connect, or an error before the model produced
    anything (not logged in, unknown model, ...)."""
    tail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
    if proc.timed_out:
        return ""
    if not metrics.init_seen:
        return f"claude produced no output (exit code {proc.returncode}): {tail or 'no stderr'}"
    server = ARM_SPECS[arm].server
    status = {entry.get("name"): entry.get("status") for entry in metrics.mcp_servers}
    if status.get(server) != "connected":
        return f"MCP server {server!r} did not connect (status: {status.get(server, 'not loaded')})"
    if metrics.is_error and metrics.api_calls == 0:
        why = f"claude failed before the model answered: {metrics.error or tail or 'unknown'}"
        if "not logged in" in why.lower():
            why += (
                " (log in with `claude auth login`, or set ANTHROPIC_API_KEY or "
                "CLAUDE_CODE_OAUTH_TOKEN, see `claude setup-token`)"
            )
        return why
    return ""


# -- local paths ---------------------------------------------------------------------------------

HOME_MARK = "~"
_SEP = r"[\\/]+"  # one path separator in any spelling: \, \\ (as JSON writes it) or /
# a home directory whose user name was replaced by hand, in any spelling
_PLACEHOLDER_HOME = rf"(?:[A-Za-z]:)?{_SEP}(?:Users|home){_SEP}<user>"


def local_homes() -> list[str]:
    """The home directory of the user running the benchmark (as given, and resolved)."""
    homes: list[str] = []
    try:
        candidates = [Path.home(), Path.home().resolve()]
    except (RuntimeError, OSError):
        return homes
    for candidate in candidates:
        if str(candidate) not in homes:
            homes.append(str(candidate))
    return homes


def _home_regex(home: str) -> str | None:
    """A regex for ``home`` with any separator spelling (see ``_SEP``) and any letter case;
    ``None`` for a filesystem root (it would match every path)."""
    parts = [part for part in re.split(r"[\\/]+", home) if part]
    if not parts or Path(home).parent == Path(home):
        return None
    lead = _SEP if home[:1] in ("\\", "/") else ""
    return lead + _SEP.join(re.escape(part) for part in parts)


def scrub_text(text: str, homes: Iterable[str | Path] | None = None) -> str:
    """``text`` with the user's home directory (``homes``; default: this user's) replaced by
    ``~``. The path may be plain (``C:\\Users\\me\\x``), as JSON escapes it (``C:\\\\Users\\\\me``),
    written with forward slashes, or in another letter case; the tail of the path is kept. A path
    that only starts with the same characters (``C:\\Users\\me2``) is left alone. The ``<user>``
    placeholder of a hand-scrubbed file is normalised the same way. Idempotent."""
    candidates = local_homes() if homes is None else [str(home) for home in homes]
    alternatives = sorted(filter(None, map(_home_regex, candidates)), key=len, reverse=True)
    alternatives.append(_PLACEHOLDER_HOME)
    pattern = re.compile("(?:" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE)
    return pattern.sub(HOME_MARK, text)


def scrub_bytes(data: bytes, homes: Iterable[str | Path] | None = None) -> bytes:
    """``scrub_text`` for raw output (bytes that are not UTF-8 pass through unchanged)."""
    text = data.decode("utf-8", "surrogateescape")
    return scrub_text(text, homes).encode("utf-8", "surrogateescape")


def scrub_file(path: Path, homes: Iterable[str | Path] | None = None) -> bool:
    """Scrub a file in place; returns whether it changed (an unchanged file is not touched)."""
    data = path.read_bytes()
    clean = scrub_bytes(data, homes)
    if clean == data:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(clean)
    os.replace(tmp, path)
    return True


# -- one run -------------------------------------------------------------------------------------


def copy_since_home(src: Path, dst: Path, now: datetime | None = None) -> None:
    """A private copy of the replayed SINCE_HOME (the database only: ``since mcp`` reads nothing
    else) so that a run's served log and cursor never leak into the next run. The heartbeat is
    refreshed like the last step of the replay, so a copy made later shows no stale-daemon
    warning."""
    dst.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(Path(src) / DB_FILENAME)
    target = sqlite3.connect(Path(dst) / DB_FILENAME)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    with Store.open(dst) as store:
        store.set_meta(META_HEARTBEAT, to_iso(now or datetime.now(UTC)))
        store.set_meta(META_MIN_SCHEDULE, MIN_SCHEDULE_S)


def _say(text: str) -> None:
    """Print a progress line that any console encoding can show (paths may hold odd characters)."""
    print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (a crash never leaves half a results file), without local paths."""
    tmp = path.with_name(path.name + ".tmp")
    text = scrub_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def execute_run(
    *,
    arm: str,
    n: int,
    out: Path,
    claude: str,
    model: str,
    max_budget_usd: float,
    seed: int,
    since_home: Path,
    planted: list[Item],
    observable: list[Item],
    observable_a: list[Item],
    timeout_s: float,
) -> dict[str, Any]:
    """One headless run of one arm: launch, save the stream (without local paths), parse, grade.
    Returns the record that goes into ``results.json`` (``cli_failure`` is non-empty when the
    runs must stop)."""
    work = Path(tempfile.mkdtemp(prefix=f"since-bench-{arm}{n}-"))
    try:
        cwd = work / "cwd"
        cwd.mkdir()
        run_home = None
        if arm == "B":
            run_home = work / "since-home"
            copy_since_home(since_home, run_home)
        config = mcp_config(arm, seed=seed, since_home=run_home)
        config_path = work / "mcp.json"
        config_path.write_text(json.dumps(config, indent=1), encoding="utf-8")
        command = claude_command(claude, config_path, arm, model, max_budget_usd)
        started = datetime.now(UTC)
        proc = run_process(
            command,
            stdin_bytes=build_prompt(arm).encode("utf-8"),
            cwd=cwd,
            env=child_env(),
            timeout_s=timeout_s,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    stream_name = f"{arm}-{n}.jsonl"
    stdout = scrub_bytes(proc.stdout)
    (out / stream_name).write_bytes(stdout)
    if proc.stderr.strip():
        (out / f"{arm}-{n}.stderr.txt").write_bytes(scrub_bytes(proc.stderr))
    parsed = parse_stream(stdout.decode("utf-8", "replace"), proc.wall_s)
    metrics = parsed.metrics
    if proc.timed_out:
        metrics.status, metrics.is_error = "timeout", True
        metrics.error = f"killed after {timeout_s:g} s"
    extraction = extract_items(parsed.final_text)
    result = grade(extraction.items, planted, observable, observable_a)
    return {
        "arm": arm,
        "run": n,
        "model": model,
        "max_budget_usd": max_budget_usd,
        "started": to_iso(started),
        "command": command,
        "mcp_config": config,
        "returncode": proc.returncode,
        "timed_out": proc.timed_out,
        "wall_s": round(proc.wall_s, 1),
        "stream": stream_name,
        "metrics": metrics.to_dict(),
        "malformed": extraction.malformed,
        "extract_note": extraction.note,
        "reported": [list(item) for item in extraction.items],
        "grade": result.to_dict(),
        "final_text": parsed.final_text,
        "stderr_tail": proc.stderr.decode("utf-8", "replace").strip()[-500:],
        "cli_failure": cli_failure(metrics, arm, proc),
    }


# -- results and report --------------------------------------------------------------------------


def world_facts(world: World) -> dict[str, Any]:
    """The part of the world block that follows from the world alone (no replay needed)."""
    return {
        "summary": world.summary(),
        "planted": [list(item) for item in world.planted],
        "observable_a": [list(item) for item in arm_a_observable_in(world)],
        "decoys": [{"kind": k, "ref": r, "why": why} for k, r, why in world.decoys],
    }


def world_block(world: World, observations: list[Observation]) -> dict[str, Any]:
    """The part of ``results.json`` the report needs to describe the world and the answer key:
    ``world_facts`` plus what the replay says Since can observe."""
    return {
        **world_facts(world),
        "observable": [[o.kind, o.ref] for o in observations if o.observed],
        "unobservable": [
            {"kind": o.kind, "ref": o.ref, "reason": o.reason}
            for o in observations
            if not o.observed
        ],
    }


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _num(value: float) -> str:
    return f"{value:,.0f}"


def _count(value: float) -> str:
    """A small count (requests, tool calls): a mean like 3.3 keeps its decimal, a whole 3 not."""
    return f"{value:,.1f}".removesuffix(".0")


def _usd(value: float) -> str:
    return f"${value:.3f}"


def _flt(value: float) -> str:
    return f"{value:.1f}"


def _cell(value: float | None, fmt: Any) -> str:
    return "n/a" if value is None else str(fmt(value))


def _metric(name: str) -> Any:
    return lambda r: r["metrics"].get(name)


def _grade_of(name: str) -> Any:
    return lambda r: r["grade"].get(name)


def _measures(world: dict[str, Any]) -> tuple[tuple[str, Any, Any], ...]:
    """(row label, getter over a run record -> number or None, formatter). The recall rows say
    how many planted items they are out of (the ceiling of that row)."""
    planted = len(world["planted"])
    a_items = world.get("observable_a")
    a_ceiling = "" if a_items is None else f" ({len(a_items)} of {planted})"
    return (
        ("Model requests", _metric("api_calls"), _count),
        ("Tool calls", _metric("tool_calls"), _count),
        ("Input tokens, total (incl. cache)", _metric("total_input_tokens"), _num),
        ("Final-request context tokens", _metric("final_context_tokens"), _num),
        ("Output tokens", _metric("output_tokens"), _num),
        (f"Recall, all planted ({planted})", _grade_of("recall"), _pct),
        (
            f"Recall, observable by Since ({len(world['observable'])} of {planted})",
            _grade_of("recall_observable"),
            _pct,
        ),
        (
            f"Recall, observable by arm A's tools{a_ceiling}",
            _grade_of("recall_observable_a"),
            _pct,
        ),
        ("Precision", _grade_of("precision"), _pct),
        ("Cost (USD)", _metric("cost_usd"), _usd),
        ("Wall time (s)", lambda r: r.get("wall_s", r["metrics"].get("duration_s")), _flt),
    )


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _stats(runs: list[dict[str, Any]], getter: Any, fmt: Any) -> tuple[str, str, str]:
    """Mean, median and min–max of a measure over the runs (a run without a value is left out)."""
    values = [v for v in (getter(r) for r in runs) if v is not None]
    if not values:
        return "n/a", "n/a", "n/a"
    mean, median = sum(values) / len(values), statistics.median(values)
    low, high = min(values), max(values)
    return fmt(mean), fmt(median), fmt(low) if low == high else f"{fmt(low)}–{fmt(high)}"


def _common(runs: list[dict[str, Any]], which: str) -> list[tuple[Item, int]]:
    counts: Counter[Item] = Counter()
    for run in runs:
        for kind, ref in run["grade"][which]:
            counts[(kind, ref)] += 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _world_lines(world: dict[str, Any]) -> list[str]:
    s = world["summary"]
    by_kind = s.get("planted_by_kind", {})
    kinds = ", ".join(f"{k} {v}" for k, v in by_kind.items())
    senders = ", ".join(f"{k} {v}" for k, v in s.get("mails_by_sender_kind", {}).items())
    planted, observable = len(world["planted"]), len(world["observable"])
    business = s.get("mails_business_after_last_look")
    after = f"{s['mails_after_last_look']} after the last look"
    if business is not None:
        after += f"; {business} of those from customers or suppliers"
    lines = [
        f"- Seed {s['seed']}. Simulated time {s['start']} to {s['now']}; the agent last looked at "
        f"{s['last_look']}.",
        f"- Mail: {s['mails_total']} messages ({after}). By sender: {senders}.",
        f"- PO table: {s['po_rows_now']} rows now, {s['po_changes']} changes over the three days "
        f"({s['po_changes_after_last_look']} after the last look).",
        f"- Portal: {s['portal_orders']} orders, {s['portal_changes']} changes "
        f"({s['portal_changes_after_last_look']} after the last look); layout change at "
        f"{s['layout_change_at']}, login expiry at {s['login_expiry_at']}.",
        f"- Answer key: {planted} planted items ({kinds}); {s['decoys_total']} decoys that "
        "must not be listed.",
        f"- Observable by Since (in its full digest): {observable} of {planted}.",
    ]
    for item in world["unobservable"]:
        lines.append(
            f"  - Not observable: {item['kind']} {item['ref']} ({item['reason']}): the change "
            "was made when the portal could no longer be read; only the two system items "
            "report that."
        )
    a_items = world.get("observable_a")
    if a_items is not None:
        reachable = {normalise(*item) for item in a_items}
        beyond = [f"{k} {r}" for k, r in world["planted"] if normalise(k, r) not in reachable]
        line = f"- Observable by arm A's tools: {len(a_items)} of {planted}."
        if beyond:
            line += f" {ARM_A_LIMITS}. Out of A's reach: {', '.join(beyond)}."
        lines.append(line)
    return lines


def _flag_value(runs: list[dict[str, Any]], flag: str) -> str | None:
    """The value after ``flag`` in the command line recorded for the runs (first that has it)."""
    for run in runs:
        command = run.get("command")
        if isinstance(command, list) and flag in command:
            at = command.index(flag) + 1
            if at < len(command):
                return str(command[at])
    return None


def _method_lines(runs: list[dict[str, Any]], cap: float) -> list[str]:
    """The method section: how the runs were made and what the numbers are (D35). Values that
    the record of the runs holds (version, model, effort, system prompt) are taken from it."""
    versions = sorted({r["metrics"].get("claude_version") or "" for r in runs} - {""})
    aliases = sorted({r["model"] for r in runs})
    resolved = sorted({r["metrics"].get("model") or "" for r in runs} - {""})
    model = ", ".join(aliases) + (f" (resolved to {', '.join(resolved)})" if resolved else "")
    effort = _flag_value(runs, "--effort") or EFFORT
    prompt = _flag_value(runs, "--system-prompt") or system_prompt()
    return [
        "Every run is one headless Claude Code session (`claude -p --output-format stream-json "
        "--verbose`), started by `bench/run.py` in an empty temp directory. Both arms get the "
        "same task prompt; only its tool paragraph differs.",
        "",
        f"- Claude Code {', '.join(versions) or 'unknown'}; model {model}.",
        "- Only the arm's MCP server is loaded (`--strict-mcp-config`); no built-in tools "
        '(`--tools ""`); `--allowedTools` names that server only.',
        '- Nothing else of this machine reaches the agent: `--setting-sources ""` (no user, '
        "project or local settings, so no CLAUDE.md and no hooks), auto-memory off "
        "(`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), and an environment without the calling "
        "session's `CLAUDECODE` / `CLAUDE_*` variables.",
        "- System prompt: a one-line neutral `--system-prompt` that states the simulated now "
        f'("{prompt}"). '
        "It is much shorter than Claude Code's default system prompt, so absolute token counts "
        "here are lower than in normal use; both arms get the same one.",
        f"- `--effort {effort}`; per-run budget cap ${cap:g} (`--max-budget-usd`); "
        "`--no-session-persistence`.",
        "- Counted from the raw stream: model requests = distinct assistant message ids "
        "(`api_calls`; the CLI's own `num_turns`, tool calls + 1, is not used); tool calls = "
        "`tool_use` blocks; input tokens = uncached + cache-creation + cache-read input, summed "
        "over all requests (so the context is counted again at every request); final-request "
        "context tokens = the same three kinds for the last request alone, the size of what the "
        "model had to hold when it answered; output tokens and cost are the CLI's totals.",
        "- Grading: references are normalised and duplicates counted once; recall is scored "
        "against three denominators (all planted items, the items Since can observe, the items "
        "arm A's tools can reach); precision = correct reported / reported.",
        f"- Local home directories in the committed results (`results.json`, the raw streams, "
        f"this report) are replaced by `{HOME_MARK}`.",
    ]


def _misses_lines(arm_runs: list[dict[str, Any]], world: dict[str, Any]) -> list[str]:
    unobservable = {
        normalise(u["kind"], u["ref"]): u["reason"] for u in world.get("unobservable", [])
    }
    decoys = {normalise(d["kind"], d["ref"]): d["why"] for d in world.get("decoys", [])}
    n = len(arm_runs)
    lines = ["Missed (planted, not reported), most common first:"]
    missed = _common(arm_runs, "fn")[:8]
    for item, count in missed:
        note = unobservable.get(normalise(*item))
        lines.append(f"- {item[0]} {item[1]}: {count}/{n} runs" + (f" ({note})" if note else ""))
    if not missed:
        lines.append("- none")
    lines += ["", "False positives (reported, not in the answer key), most common first:"]
    extra = _common(arm_runs, "fp")[:8]
    for item, count in extra:
        why = decoys.get(normalise(*item), "not a planted item and not a known decoy")
        lines.append(f"- {item[0]} {item[1]}: {count}/{n} runs ({why})")
    if not extra:
        lines.append("- none")
    return lines


def render_report(results: dict[str, Any]) -> str:
    """REPORT.md from a results dict (see ``results.json``)."""
    world, runs = results["world"], results["runs"]
    valid = [r for r in runs if not r.get("cli_failure")]
    per_arm = {arm: [r for r in valid if r["arm"] == arm] for arm in ARMS}
    counts = ", ".join(f"arm {arm}: {len(rs)}" for arm, rs in per_arm.items() if rs) or "none"
    cap = max((r["max_budget_usd"] for r in runs), default=0)
    lines = [
        "# Since benchmark report",
        "",
        f"Generated {results.get('updated', '')}. Runs: {counts}.",
        "",
        "The task (one prompt for both arms, only the tool paragraph differs): list everything "
        "that needs attention since the last look, by four explicit rules; the final answer is "
        "a JSON list of `(kind, ref)` graded against the planted answer key. Arm A: raw mail, "
        "SQL and portal tools plus the notes saved at the last look (the agent diffs in its own "
        "context). Arm B: the Since MCP tools over a replay of the same world.",
        "",
        "## Method",
        "",
        *_method_lines(runs, cap),
        "",
        "## The world",
        "",
        *_world_lines(world),
        "",
        "## Results per arm",
        "",
        "Cells are mean, median, then min–max over the runs of the arm. Recall is scored "
        "against three denominators, and the ceiling of each is in its row label: all planted "
        "items; the items Since can observe; the items arm A's tools can reach.",
    ]
    measures = _measures(world)
    for arm, arm_runs in per_arm.items():
        if not arm_runs:
            continue
        bad = [r for r in arm_runs if r["metrics"]["status"] != "ok"]
        note = f", {len(bad)} not ok" if bad else ""
        noun = "run" if len(arm_runs) == 1 else "runs"
        lines += ["", f"### Arm {arm} – {ARM_SPECS[arm].label} ({len(arm_runs)} {noun}{note})", ""]
        rows = [[label, *_stats(arm_runs, getter, fmt)] for label, getter, fmt in measures]
        lines += _table(["Measure", "Mean", "Median", "Min–max"], rows)
    lines += ["", "## Runs", ""]
    headers = [
        "Arm",
        "Run",
        "Status",
        "Tool calls",
        "Requests",
        "Input total",
        "Final ctx",
        "Uncached",
        "Output",
        "Cost",
        "Recall",
        "Recall (Since)",
        "Recall (A)",
        "Precision",
        "TP/FP/FN",
        "Time (s)",
    ]
    rows = []
    for r in runs:
        m, g = r["metrics"], r["grade"]
        status = m["status"] + (", no valid answer" if r.get("malformed") else "")
        if r.get("cli_failure"):
            status = "CLI failure"
        rows.append(
            [
                r["arm"],
                str(r["run"]),
                status,
                str(m["tool_calls"]),
                _cell(m.get("api_calls"), _num),
                _num(m["total_input_tokens"]),
                _cell(m.get("final_context_tokens"), _num),
                _num(m["input_tokens"]),
                _num(m["output_tokens"]),
                _cell(m["cost_usd"], _usd),
                _pct(g["recall"]),
                _cell(g.get("recall_observable"), _pct),
                _cell(g.get("recall_observable_a"), _pct),
                _pct(g["precision"]),
                f"{len(g['tp'])}/{len(g['fp'])}/{len(g['fn'])}",
                _flt(r.get("wall_s", m["duration_s"])),
            ]
        )
    lines += _table(headers, rows)
    lines += [
        "",
        "Requests = model requests. Final ctx = final-request context tokens. Uncached = input "
        "tokens neither read from nor written to the cache. Recall (Since) / (A) = recall on the "
        "items observable by Since / by arm A's tools.",
    ]
    lines += ["", "## Most common misses and false positives"]
    for arm, arm_runs in per_arm.items():
        if arm_runs:
            lines += [
                "",
                f"### Arm {arm} – {ARM_SPECS[arm].label}",
                "",
                *_misses_lines(arm_runs, world),
            ]
    return "\n".join(lines) + "\n"


def write_reports(out: Path, results: dict[str, Any], publish_to: Path | None) -> Path:
    """Write ``<out>/REPORT.md`` (without local paths) and, if ``publish_to`` is given, a copy
    there."""
    report = out / "REPORT.md"
    report.write_text(scrub_text(render_report(results)), encoding="utf-8", newline="\n")
    if publish_to is not None:
        shutil.copyfile(report, publish_to)
    return report


# -- rebuilding a report from saved results (--report-only) --------------------------------------


def refresh_run(
    record: dict[str, Any],
    out: Path,
    planted: list[Item],
    observable: list[Item],
    observable_a: list[Item],
) -> None:
    """Recompute one run record from its saved stream, if the stream is there: the metrics (a
    stream holds everything but the process wall time and the timeout, which stay as recorded),
    the answer, and the grade against the three answer keys. Without a stream the reported items
    of the record are graded again."""
    stream = out / Path(str(record.get("stream", ""))).name
    items = [(str(kind), str(ref)) for kind, ref in record.get("reported", [])]
    if stream.is_file():
        text = stream.read_bytes().decode("utf-8", "replace")
        parsed = parse_stream(text, float(record.get("wall_s") or 0.0))
        metrics = parsed.metrics.to_dict()
        if record.get("timed_out"):  # a kill leaves no trace in the stream
            for key in ("status", "is_error", "error"):
                if key in record.get("metrics", {}):
                    metrics[key] = record["metrics"][key]
        extraction = extract_items(parsed.final_text)
        record.update(
            metrics=metrics,
            malformed=extraction.malformed,
            extract_note=extraction.note,
            reported=[list(item) for item in extraction.items],
            final_text=parsed.final_text,
        )
        items = extraction.items
    record["grade"] = grade(items, planted, observable, observable_a).to_dict()


def refresh_results(out: Path, results: dict[str, Any]) -> None:
    """Bring a loaded ``results.json`` up to date without running anything: the world facts are
    rebuilt from the seed (which must still produce the same answer key), and every run is
    recomputed from its stream. What the replay said Since can observe is kept as recorded."""
    block = results.get("world")
    try:
        seed = int(block["summary"]["seed"])
    except (KeyError, TypeError, ValueError):
        raise BenchError("results.json has no world summary with a seed") from None
    facts = world_facts(build_world(seed))
    if block.get("planted") != facts["planted"]:
        raise BenchError(
            f"the world of seed {seed} no longer gives the answer key these results were graded "
            "against; rebuild the report with the code version that made them"
        )
    block.update(facts)
    planted = [(k, r) for k, r in block["planted"]]
    observable = [(k, r) for k, r in block["observable"]]
    observable_a = [(k, r) for k, r in block["observable_a"]]
    for record in results.get("runs", []):
        refresh_run(record, out, planted, observable, observable_a)


def rebuild_report(out: Path, publish_to: Path | None) -> Path:
    """``--report-only``: scrub the results directory ``out``, recompute it from the streams and
    rewrite ``<out>/REPORT.md`` (and ``publish_to``). Starts no process."""
    results_path = out / "results.json"
    if not results_path.is_file():
        raise BenchError(f"{results_path} not found: --out must be an existing results directory")
    for path in [*sorted(out.glob("*.jsonl")), *sorted(out.glob("*.stderr.txt"))]:
        scrub_file(path)
    try:
        results = json.loads(scrub_text(results_path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise BenchError(f"{results_path} is not valid JSON: {exc}") from exc
    refresh_results(out, results)
    if not any(not r.get("cli_failure") for r in results.get("runs", [])):
        raise BenchError(f"{results_path} holds no valid run: nothing to report")
    _write_json(results_path, results)
    return write_reports(out, results, publish_to)


# -- the command line ----------------------------------------------------------------------------


def parse_arms(text: str) -> list[str]:
    arms: list[str] = []
    for part in text.split(","):
        arm = part.strip().upper()
        if arm not in ARMS:
            raise BenchError(f"unknown arm {part!r}; arms are {', '.join(ARMS)}")
        if arm not in arms:
            arms.append(arm)
    return arms


def _load_results(path: Path, seed: int) -> dict[str, Any]:
    if not path.exists():
        return {"version": RESULTS_VERSION, "runs": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("world", {}).get("summary", {}).get("seed", seed) != seed:
        raise BenchError(f"{path} was written for another seed; use another --out")
    return data


def _replay_or_reuse(world: World, home: Path, channel: str | None) -> list[Observation]:
    if (home / DB_FILENAME).exists():
        _say(f"reusing the replayed home {home}")
        return observe(world, digest_text(home))
    _say("replaying the world into a fresh SINCE_HOME (about a minute) ...")
    try:
        return replay(world, home, browser_channel=channel).observability
    except ReplayError as exc:
        raise BenchError(f"replay failed: {exc}") from exc
    except Exception as exc:  # a browser that cannot start is the usual cause
        raise BenchError(
            f"replay failed: {type(exc).__name__}: {exc} (the portal needs a browser: "
            "--channel msedge, or `playwright install chromium`)"
        ) from exc


def _next_run(runs: list[dict[str, Any]], arm: str) -> int:
    return 1 + max((r["run"] for r in runs if r["arm"] == arm), default=0)


def _summary_line(record: dict[str, Any]) -> str:
    m, g = record["metrics"], record["grade"]
    cost = "n/a" if m["cost_usd"] is None else _usd(m["cost_usd"])
    return (
        f"{record['arm']}-{record['run']}: {m['status']}, {m['tool_calls']} tool calls, "
        f"{m['api_calls']} model requests, {m['total_input_tokens']:,} input tokens, {cost}, "
        f"recall {_pct(g['recall'])} ({len(g['tp'])}/{len(g['tp']) + len(g['fn'])}), "
        f"precision {_pct(g['precision'])}"
    )


def _run_all(args: argparse.Namespace) -> int:
    arms = parse_arms(args.arms)
    if args.runs < 1:
        raise BenchError("--runs must be at least 1")
    if args.max_budget_usd <= 0:
        raise BenchError("--max-budget-usd must be positive")
    claude = find_claude()
    out = (
        Path(args.out)
        if args.out
        else BENCH_DIR / "results" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.json"
    results = _load_results(results_path, args.seed)

    world = build_world(args.seed)
    observations = _replay_or_reuse(world, out / "since-home", args.channel)
    results["world"] = world_block(world, observations)
    planted = [tuple(item) for item in results["world"]["planted"]]
    observable = [tuple(item) for item in results["world"]["observable"]]
    observable_a = [tuple(item) for item in results["world"]["observable_a"]]
    _say(
        f"world: {len(planted)} planted items, {len(observable)} observable by Since, "
        f"{len(observable_a)} by arm A's tools; claude: {claude}"
    )

    failure = ""
    try:
        for _ in range(args.runs):
            for arm in arms:
                n = _next_run(results["runs"], arm)
                _say(f"running {arm}-{n} ({args.model}, cap ${args.max_budget_usd:g}) ...")
                record = execute_run(
                    arm=arm,
                    n=n,
                    out=out,
                    claude=claude,
                    model=args.model,
                    max_budget_usd=args.max_budget_usd,
                    seed=args.seed,
                    since_home=out / "since-home",
                    planted=planted,
                    observable=observable,
                    observable_a=observable_a,
                    timeout_s=args.timeout_s,
                )
                results["runs"].append(record)
                results["updated"] = to_iso(datetime.now(UTC))
                _write_json(results_path, results)
                _say(_summary_line(record))
                if record["cli_failure"]:
                    failure = record["cli_failure"]
                    raise BenchError(f"stopping after {arm}-{n}: {failure}")
    finally:
        if any(not r["cli_failure"] for r in results["runs"]):  # nothing to report otherwise
            report = write_reports(out, results, BENCH_DIR / "REPORT.md")
            _say(f"report: {report} (copied to {BENCH_DIR / 'REPORT.md'})")
    return 0


def _report_only(args: argparse.Namespace) -> int:
    if args.out is None:
        raise BenchError("--report-only needs --out <an existing results directory>")
    out = Path(args.out).resolve()
    report = rebuild_report(out, BENCH_DIR / "REPORT.md")
    _say(f"report: {report} (copied to {BENCH_DIR / 'REPORT.md'}); nothing was run")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench.run",
        description="Run the Since benchmark: Arm A (raw tools) vs Arm B (Since tools), headless.",
    )
    parser.add_argument("--arms", default="A,B", help="comma-separated arms (default A,B)")
    parser.add_argument("--runs", type=int, default=3, help="runs per arm (default 3)")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--max-budget-usd", type=float, default=3.0, help="cap per run")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--channel", choices=("msedge", "chrome"), default=None)
    parser.add_argument("--out", type=Path, default=None, help="default bench/results/<UTC stamp>")
    parser.add_argument("--timeout-s", type=float, default=1800, help="wall-time cap per run")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="run nothing: re-read --out, scrub local paths, recompute from the streams and "
        "rewrite REPORT.md (and bench/REPORT.md)",
    )
    args = parser.parse_args(argv)
    try:
        return _report_only(args) if args.report_only else _run_all(args)
    except BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
