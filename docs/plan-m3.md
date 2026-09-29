# M3 plan — trust & proof

Scope (CLAUDE.md "Milestones"): `since ui` localhost audit page; benchmark harness in `bench/` (deterministic
3-day simulated world, Arm A raw tools vs Arm B Since tools, both via headless Claude Code; report input
tokens, tool calls, recall/precision vs the planted list). Decisions for M3 are D32–D36 in `docs/decisions.md`.
M1/M2 conventions still apply.

## `since ui` (D32)
`since ui [--port N]` (default 8737; `0` = any free port) serves a read-only audit page on **127.0.0.1 only**
and prints its URL. Stdlib `http.server` (ThreadingHTTPServer), server-side HTML, no JavaScript, no external
assets. Each request opens the store read-only (no write lock) and closes it.
Pages:
- `/` — daemon heartbeat line, sources table (priority, id, type, records, last success, state/error),
  agents table (agent_id, cursor, last served at, number of responses served), the 20 most recent served
  responses (time, agent, tool, args summary, link).
- `/events?source=<id>&before=<seq>` — 50 events per page, newest first: seq, time, source, the digest line
  body (untruncated handles), link to `/event/<seq>`; "older" link.
- `/event/<seq>` — the same text as `get(since://evt/<seq>)` in a `<pre>`, plus the stored detail JSON.
- `/served?agent=<id>&before=<id>` — served-log rows newest first; `/served/<id>` — the exact text that was
  served, in a `<pre>`, with agent, tool, args, via and time. This is the "exactly what each agent was served
  and when" page.
Security: every DB value is HTML-escaped (`html.escape(..., quote=True)`); responses carry
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`; the `Host` header must be `127.0.0.1:<port>` or
`localhost:<port>` (else 421) against DNS rebinding; only GET/HEAD (else 405); unknown paths 404.

## Benchmark (`bench/`, dev-only, not packaged)

### The world (D33)
Deterministic (fixed seed, fixed simulated dates 2026-09-14 00:00Z … 2026-09-16 10:00Z = "now").
The agent last looked at **2026-09-15 09:00Z** ("yesterday"). Sources:
- **Mail** (IMAP fake): ≥ 200 messages over the 3 days — newsletters, notifications, internal chatter, and
  supplier/customer mail. Every business mail subject carries a unique reference number (PO number, order
  number, invoice number, ASN number).
- **PO table** (SQLite, sql source): ≥ 50 rows `po_no, supplier, status, eta, qty, updated_at`.
- **Portal** (local web page, web source): an orders table of ~20 rows. A **layout change** that breaks the
  configured selectors at 2026-09-15 22:00Z and a **login expiry** at 2026-09-16 07:00Z.
"Needing attention" is defined in the task prompt (so grading is objective):
1. mail from a customer or supplier that asks for action or reports a problem, received after the last look;
2. a PO cancelled, or whose ETA moved later by more than 3 days, after the last look;
3. a portal order cancelled or put on hold after the last look;
4. any problem that stops you from seeing a source now (portal login expired, portal layout changed).
The planted answer key lists every item that meets a rule, as `(kind, ref)`: kind `email|po|portal|system`,
ref = the reference number, or `portal-login` / `portal-layout` for system items. The world also contains
decoys that must NOT be listed: the same kinds of events before the last look, ETA moves ≤ 3 days, alarming
newsletters ("URGENT: 50% off"), mail from colleagues, a PO un-cancelled back to Open, etc.

### Replay into Since
`bench/replay.py` builds a SINCE_HOME by replaying the world every 2 hours (simulated): apply the state to the
fakes, then `run_collection` for each source with `now` = the tick (imap `now_fn` = tick). At the
2026-09-15 09:00Z tick the bench agent (`agent_id="bench"`) is acked to the max seq. At the end, write a
fresh heartbeat (real now) and `daemon_min_schedule_s`, so the digest has no stale warning.

### Arm A — raw tools (D34)
`bench/raw_mcp.py`, an MCP stdio server over the world **as of now**:
`list_emails(folder="INBOX", offset=0, limit=50)` (uid, date, from, subject, flags — newest first),
`read_email(uid)` (headers + body), `sql_query(sql)` (read-only, PO database), `fetch_portal()` (visible text
of the portal page now — the login page after the expiry), `read_note(name)` / `list_notes()` over the notes
the agent saved at its last look: `last_look.txt` (the time), `po_table.csv` and `portal.txt` (the PO table
and the portal page text as of 2026-09-15 09:00Z). This is the "agent diffs inside its own context" baseline.
### Arm B — Since tools
`since mcp` with the replayed SINCE_HOME; the prompt says to use `agent_id="bench"`.

### Runner and grading (D35)
`bench/run.py --arms A,B --runs 3 --model sonnet --max-budget-usd 3`:
headless Claude Code (`claude -p … --output-format stream-json --verbose --mcp-config <arm json>
--strict-mcp-config --tools "" --allowedTools mcp__raw|mcp__since --model … --max-budget-usd …
--no-session-persistence`), in a temp cwd, with `CLAUDECODE` removed from the env; the CLI path comes from
`SINCE_BENCH_CLAUDE` or the newest `%APPDATA%\Claude\claude-code\*\claude.exe` (other OS: `claude` on PATH).
Same task prompt for both arms except the tool paragraph; it ends by requiring a final fenced JSON block
`{"items": [{"kind": "...", "ref": "..."}]}`.
From the stream: tool calls = `tool_use` blocks; input tokens = sum over assistant turns of
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens` (also report the uncached part);
output tokens; cost (`total_cost_usd`); model requests (`api_calls`); wall time.
Grading: normalise refs (strip, upper-case, digits for numeric refs); recall = planted found / planted;
precision = correct reported / reported (duplicates counted once; unknown refs are false positives).
Results: `bench/results/<UTC timestamp>/` (raw stream per run, `results.json`) and `bench/REPORT.md` (table:
per arm mean ± spread of input tokens, tool calls, recall, precision, cost; per-run rows; the world summary).

## Tasks
Order: (T1 ∥ T2) → T3 → T4 → T5 (architect runs the benchmark) → QA.

### T1 — `since ui` (`src/since/ui.py`, `cli.py`, tests)
Acceptance: every page renders from a store built with the Store API; hostile values (`<script>`, quotes,
RTL override) are escaped everywhere; Host check (421), 405, 404; security headers present; works while
another connection holds the write lock; `since ui --port 0` prints a URL that serves `/`; binds 127.0.0.1
only.

### T2 — world + answer key (`bench/__init__.py`, `bench/world.py`, `tests/test_bench_world.py`)
`build_world(seed=…) -> World` with the timeline of states at 2-hour ticks, the notes snapshot, and
`planted: list[(kind, ref)]` computed from the rules (not hand-listed), plus `decoys` for the report.
Acceptance: deterministic (same seed → identical world, byte-identical dumps); ≥ 200 mails, ≥ 50 PO rows,
one breaking layout change and one login expiry at the stated times; every rule has ≥ 2 planted items and
≥ 1 decoy; planted/decoy classification tested directly against the rules.

### T3 — replay + raw MCP (`bench/replay.py`, `bench/raw_mcp.py`, tests)
Acceptance: replay produces a SINCE_HOME whose `since(agent_id="bench")` digest contains every planted item
that Since can observe (assert it; items it cannot observe must be listed with the reason); the raw MCP
server's tools work over stdio (driven by the SDK client in a test); the portal returns the login page as of
now; notes are the 09:00Z state. Web replay uses the browser like the web tests (msedge locally).

### T4 — runner + grader + report (`bench/run.py`, `bench/grade.py`, tests)
Acceptance: parser and grader unit-tested on recorded/fake stream-json (no model calls in tests); one real
smoke run per arm with `--runs 1 --max-budget-usd 1` to prove the plumbing (report its numbers).

### T5 — run + report (architect)
3 runs per arm on Sonnet; commit `bench/REPORT.md` (and the results dir unless it is large); README section.
