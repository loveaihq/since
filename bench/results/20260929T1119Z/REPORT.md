# Since benchmark report

Generated 2026-09-29T11:23:25Z. Runs: arm A: 3, arm B: 3.

The task (one prompt for both arms, only the tool paragraph differs): list everything that needs attention since the last look, by four explicit rules; the final answer is a JSON list of `(kind, ref)` graded against the planted answer key. Arm A: raw mail, SQL and portal tools plus the notes saved at the last look (the agent diffs in its own context). Arm B: the Since MCP tools over a replay of the same world.

## Method

Every run is one headless Claude Code session (`claude -p --output-format stream-json --verbose`), started by `bench/run.py` in an empty temp directory. Both arms get the same task prompt; only its tool paragraph differs.

- Claude Code 2.1.284; model sonnet (resolved to claude-sonnet-5-5).
- Only the arm's MCP server is loaded (`--strict-mcp-config`); no built-in tools (`--tools ""`); `--allowedTools` names that server only.
- Nothing else of this machine reaches the agent: `--setting-sources ""` (no user, project or local settings, so no CLAUDE.md and no hooks), auto-memory off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), and an environment without the calling session's `CLAUDECODE` / `CLAUDE_*` variables.
- System prompt: a one-line neutral `--system-prompt` that states the simulated now ("You are a careful assistant. Use only the tools you are given. The current date and time is 2026-09-16 10:00 UTC."). It is much shorter than Claude Code's default system prompt, so absolute token counts here are lower than in normal use; both arms get the same one.
- `--effort medium`; per-run budget cap $3 (`--max-budget-usd`); `--no-session-persistence`.
- Counted from the raw stream: model requests = distinct assistant message ids (`api_calls`; the CLI's own `num_turns`, tool calls + 1, is not used); tool calls = `tool_use` blocks; input tokens = uncached + cache-creation + cache-read input, summed over all requests (so the context is counted again at every request); final-request context tokens = the same three kinds for the last request alone, the size of what the model had to hold when it answered; output tokens and cost are the CLI's totals.
- Grading: references are normalised and duplicates counted once; recall is scored against three denominators (all planted items, the items Since can observe, the items arm A's tools can reach); precision = correct reported / reported.
- Local home directories in the committed results (`results.json`, the raw streams, this report) are replaced by `~`.

## The world

- Seed 20260916. Simulated time 2026-09-14T00:00:00Z to 2026-09-16T10:00:00Z; the agent last looked at 2026-09-15T09:00:00Z.
- Mail: 255 messages (118 after the last look; 23 of those from customers or suppliers). By sender: colleague 65, customer 31, newsletter 60, portal 2, promo 25, saas 49, supplier 23.
- PO table: 64 rows now, 63 changes over the three days (33 after the last look).
- Portal: 20 orders, 18 changes (11 after the last look); layout change at 2026-09-15T22:00:00Z, login expiry at 2026-09-16T07:00:00Z.
- Answer key: 18 planted items (email 7, po 5, portal 4, system 2); 25 decoys that must not be listed.
- Observable by Since (in its full digest): 16 of 18.
  - Not observable: portal 62000069 (after login expiry): the change was made when the portal could no longer be read; only the two system items report that.
  - Not observable: portal 62000168 (after layout change): the change was made when the portal could no longer be read; only the two system items report that.
- Observable by arm A's tools: 13 of 18. A cannot see any portal order change (the portal is behind a login at the simulated now, and A's notes are from the last look) and cannot detect portal-layout (the changed page is behind the login page too). Out of A's reach: portal 62000069, portal 62000071, portal 62000168, portal 62000469, system portal-layout.

## Results per arm

Cells are mean, median, then min–max over the runs of the arm. Recall is scored against three denominators, and the ceiling of each is in its row label: all planted items; the items Since can observe; the items arm A's tools can reach.

### Arm A – raw tools (3 runs)

| Measure | Mean | Median | Min–max |
| --- | --- | --- | --- |
| Model requests | 3.3 | 3 | 3–4 |
| Tool calls | 7 | 7 | 6–8 |
| Input tokens, total (incl. cache) | 31,285 | 25,704 | 25,320–42,832 |
| Final-request context tokens | 15,152 | 15,804 | 13,557–16,095 |
| Output tokens | 1,684 | 1,783 | 1,467–1,801 |
| Recall, all planted (18) | 70% | 72% | 67%–72% |
| Recall, observable by Since (16 of 18) | 79% | 81% | 75%–81% |
| Recall, observable by arm A's tools (13 of 18) | 97% | 100% | 92%–100% |
| Precision | 100% | 100% | 100% |
| Cost (USD) | $0.078 | $0.080 | $0.067–$0.086 |
| Wall time (s) | 19.6 | 20.1 | 16.6–22.2 |

### Arm B – Since (3 runs)

| Measure | Mean | Median | Min–max |
| --- | --- | --- | --- |
| Model requests | 3 | 3 | 3 |
| Tool calls | 3 | 3 | 3 |
| Input tokens, total (incl. cache) | 25,900 | 25,837 | 25,822–26,042 |
| Final-request context tokens | 15,749 | 15,683 | 15,675–15,889 |
| Output tokens | 1,332 | 1,326 | 1,267–1,402 |
| Recall, all planted (18) | 89% | 89% | 89% |
| Recall, observable by Since (16 of 18) | 100% | 100% | 100% |
| Recall, observable by arm A's tools (13 of 18) | 100% | 100% | 100% |
| Precision | 100% | 100% | 100% |
| Cost (USD) | $0.075 | $0.075 | $0.074–$0.077 |
| Wall time (s) | 16.7 | 16.3 | 15.7–18.1 |

## Runs

| Arm | Run | Status | Tool calls | Requests | Input total | Final ctx | Uncached | Output | Cost | Recall | Recall (Since) | Recall (A) | Precision | TP/FP/FN | Time (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 1 | ok | 8 | 4 | 42,832 | 15,804 | 8 | 1,783 | $0.086 | 72% | 81% | 100% | 100% | 13/0/5 | 20.1 |
| B | 1 | ok | 3 | 3 | 25,822 | 15,675 | 6 | 1,267 | $0.077 | 89% | 100% | 100% | 100% | 16/0/2 | 18.1 |
| A | 2 | ok | 7 | 3 | 25,320 | 16,095 | 6 | 1,801 | $0.080 | 72% | 81% | 100% | 100% | 13/0/5 | 22.2 |
| B | 2 | ok | 3 | 3 | 25,837 | 15,683 | 6 | 1,402 | $0.074 | 89% | 100% | 100% | 100% | 16/0/2 | 16.3 |
| A | 3 | ok | 6 | 3 | 25,704 | 13,557 | 6 | 1,467 | $0.067 | 67% | 75% | 92% | 100% | 12/0/6 | 16.6 |
| B | 3 | ok | 3 | 3 | 26,042 | 15,889 | 6 | 1,326 | $0.075 | 89% | 100% | 100% | 100% | 16/0/2 | 15.7 |

Requests = model requests. Final ctx = final-request context tokens. Uncached = input tokens neither read from nor written to the cache. Recall (Since) / (A) = recall on the items observable by Since / by arm A's tools.

## Most common misses and false positives

### Arm A – raw tools

Missed (planted, not reported), most common first:
- portal 62000069: 3/3 runs (after login expiry)
- portal 62000071: 3/3 runs
- portal 62000168: 3/3 runs (after layout change)
- portal 62000469: 3/3 runs
- system portal-layout: 3/3 runs
- email 91000019: 1/3 runs

False positives (reported, not in the answer key), most common first:
- none

### Arm B – Since

Missed (planted, not reported), most common first:
- portal 62000069: 3/3 runs (after login expiry)
- portal 62000168: 3/3 runs (after layout change)

False positives (reported, not in the answer key), most common first:
- none
