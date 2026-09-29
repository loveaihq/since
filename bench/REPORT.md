# Since benchmark report

Generated 2026-09-29T11:23:25Z. Runs: arm A: 3, arm B: 3. Model: sonnet. Claude Code 2.1.284. Per-run budget cap: $3.

The task (one prompt for both arms, only the tool paragraph differs): list everything that needs attention since the last look, by four explicit rules; the final answer is a JSON list of `(kind, ref)` graded against the planted answer key. Arm A: raw mail, SQL and portal tools plus the notes saved at the last look (the agent diffs in its own context). Arm B: the Since MCP tools over a replay of the same world.

## The world

- Seed 20260916. Simulated time 2026-09-14T00:00:00Z to 2026-09-16T10:00:00Z; the agent last looked at 2026-09-15T09:00:00Z.
- Mail: 255 messages (118 after the last look; 54 from customers or suppliers). By sender: colleague 65, customer 31, newsletter 60, portal 2, promo 25, saas 49, supplier 23.
- PO table: 64 rows now, 63 changes over the three days (33 after the last look).
- Portal: 20 orders, 18 changes (11 after the last look); layout change at 2026-09-15T22:00:00Z, login expiry at 2026-09-16T07:00:00Z.
- Answer key: 18 planted items (email 7, po 5, portal 4, system 2); 25 decoys that must not be listed.
- Observable by Since (in its full digest): 16 of 18.
  - Not observable: portal 62000069 (after login expiry): the change was made when the portal could no longer be read; only the two system items report that.
  - Not observable: portal 62000168 (after layout change): the change was made when the portal could no longer be read; only the two system items report that.

## Results per arm

Cells are mean, then min–max over the runs of the arm.

### Arm A – raw tools (3 runs)

| Measure | Mean | Min–max |
| --- | --- | --- |
| Input tokens, total (incl. cache) | 31,285 | 25,320–42,832 |
| Input tokens, uncached | 7 | 6–8 |
| Output tokens | 1,684 | 1,467–1,801 |
| Tool calls | 7 | 6–8 |
| Turns | 8 | 7–9 |
| Recall | 70% | 67%–72% |
| Recall on observable items | 79% | 75%–81% |
| Precision | 100% | 100% |
| Cost (USD) | $0.078 | $0.067–$0.086 |
| Wall time (s) | 19.6 | 16.6–22.2 |

### Arm B – Since (3 runs)

| Measure | Mean | Min–max |
| --- | --- | --- |
| Input tokens, total (incl. cache) | 25,900 | 25,822–26,042 |
| Input tokens, uncached | 6 | 6 |
| Output tokens | 1,332 | 1,267–1,402 |
| Tool calls | 3 | 3 |
| Turns | 4 | 4 |
| Recall | 89% | 89% |
| Recall on observable items | 100% | 100% |
| Precision | 100% | 100% |
| Cost (USD) | $0.075 | $0.074–$0.077 |
| Wall time (s) | 16.7 | 15.7–18.1 |

## Runs

| Arm | Run | Status | Tool calls | Turns | Input total | Uncached | Output | Cost | Recall | Recall obs. | Precision | TP/FP/FN | Time (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 1 | ok | 8 | 9 | 42,832 | 8 | 1,783 | $0.086 | 72% | 81% | 100% | 13/0/5 | 20.1 |
| B | 1 | ok | 3 | 4 | 25,822 | 6 | 1,267 | $0.077 | 89% | 100% | 100% | 16/0/2 | 18.1 |
| A | 2 | ok | 7 | 8 | 25,320 | 6 | 1,801 | $0.080 | 72% | 81% | 100% | 13/0/5 | 22.2 |
| B | 2 | ok | 3 | 4 | 25,837 | 6 | 1,402 | $0.074 | 89% | 100% | 100% | 16/0/2 | 16.3 |
| A | 3 | ok | 6 | 7 | 25,704 | 6 | 1,467 | $0.067 | 67% | 75% | 100% | 12/0/6 | 16.6 |
| B | 3 | ok | 3 | 4 | 26,042 | 6 | 1,326 | $0.075 | 89% | 100% | 100% | 16/0/2 | 15.7 |

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
