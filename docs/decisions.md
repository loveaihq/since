# Spec decisions

Small spec changes/clarifications made by the architect. Direction changes go to Vincent.

- **D1 (M1) Event `detail` column.** Events get a JSON `detail` dict for source-level facts
  (baseline record count, error message, recovered-from info, schema_changed selectors). The spec's event
  fields had nowhere to put them.
- **D2 (M1) `get` takes `agent_id="default"`.** The served log must say which agent was served; the spec's
  `get(handle, budget_tokens)` had no agent. Optional, so the contract stays compatible.
- **D3 (M1) Batch paging uses `&after=<seq>`.** Page numbers would depend on the caller's budget; a seq
  continuation is stable. Handle: `since://batch/<from>-<to>?source=<id>[&after=<seq>]`.
- **D4 (M1) Duplicate record keys fail the run** (`source_error`), instead of silently picking one. A key that
  isn't unique is a config problem the human must see; picking one would flap.
- **D5 (M1, revised after QA) `dir`: a file that exists but cannot be read keeps its last known record.**
  Originally any unreadable file failed the whole run; QA showed that on Windows an app holding a file open
  (e.g. an Office document) then hides the entire folder all day. Now the collector reports such keys as
  *unavailable*: the runner carries the last known record forward (no event, never `removed`), and a key never
  seen before is simply left out until it becomes readable. A directory that cannot be listed, or a missing
  root, still fails the whole run (its contents are unknown).
- **D6 (M1) `modified` always counts as "tracked-field modified" (weight 4).** With `track_fields` unset all
  fields are tracked; with it set, untracked changes produce no event at all.
- **D7 (M1) Filtered `since(source=…)` shows no `next_cursor` and no ack hint.** The cursor is one number per
  agent; acking after a filtered view would silently skip other sources' events.
- **D8 (M1) Times shown in UTC (`2026-09-29T09:12Z`).** Deterministic and unambiguous for agents.
- **D9 (M1) Credentials: env var only in M1** (`url_env`). OS keyring support deferred; config rejects
  credential-like keys and inline sql `url`.
- **D10 (M1) Long text in `get`.** "Never shown in full" applies to diffs/digests (they only report
  `+a/-b chars`). `get(since://rec/…)` is a deliberate drill-down and shows field values capped at 1000 chars.
- **D11 (M1) Digest lines are not column-aligned** (two spaces before the handle). Padding costs tokens.
- **D12 (M1) Target `mcp` 2.x (`mcp>=2.2,<3`).** 2.x is the current major; v1's `FastMCP` is
  `mcp.server.mcpserver.MCPServer` there. A new project should not start on the previous major.
- **D13 (M1) No `(low)`-style label on `omitted:` lines.** CLAUDE.md's target shape shows
  `omitted: inbox 26 (low) …`; omitted events are by construction the least important ones, so the label adds
  tokens without information. Omitted lines are ordered by the max importance of each source's *omitted* events.
- **D14 (M1) Field names are validated like keys.** Field names can come from the source (e.g. `select *`), so a
  name with control/format characters or longer than 64 chars fails the run; they are printed unquoted.
- **D15 (M1) A stale collection result is discarded.** If a newer run of the same source (daemon or
  `since collect`) already stored its result, an older in-flight result is dropped instead of reverting the
  snapshot (which would produce flip-flop events).
- **D16 (M1) `daemon --once` leaves its heartbeat.** It removes only `daemon_pid`, so a scheduled `--once`
  (cron / Task Scheduler) is judged stale by the normal 2x-schedule rule instead of always "not running".
- **Note for M2 (imap):** Message-IDs repeat in practice (same mail in several folders, or missing). D4 would then
  fail the source permanently; the imap key must be designed around this (e.g. folder + UID validity + UID).
