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
- **D17 (M2) Record titles.** Core option `title_fields` (collector default: imap subject+from,
  changedetection title). A Message-ID or watch uuid means nothing to an agent; the title is stored in the
  event detail at creation, so removed records keep a readable label.
- **D18 (M2) Page structure tracking.** The fingerprint skips the `rows` subtrees (an emptied table is data,
  not a layout change). Broken extraction (container/rows/field selectors matching nothing) never diffs — it
  behaves like a failed run whose first event is `schema_changed` instead of `source_error` — so a layout
  change can never produce a wave of `removed`. A layout-only change (all selectors still match) is reported
  once as `schema_changed` with no selectors, then diffed normally.
- **D19 (M2) `since login <source_id>`.** CLAUDE.md says the human logs in once manually into the persistent
  profile; this command opens that profile headed at the source URL and waits for the window to close. It
  never fills anything in (auto-login stays a non-goal). It is the only Playwright use outside the daemon. It also ends when the
  profile's last page is closed (on macOS closing the last window leaves the browser process running).
- **D20 (M2) imap key and dedupe.** Key = Message-ID; a mail seen in several folders/labels (normal on Gmail)
  is kept once (first by folder order, then UID); missing Message-ID → `uid:<folder>/<uidvalidity>/<uid>`.
  Read-only: EXAMINE + BODY.PEEK only. Password via `password_env` (for Google Workspace an app password);
  OAuth and message body snippets are out of M2.
- **D21 (M2) Collection windows.** Sources that only look back N days (imap `since_days`) report a window;
  records that age out of it leave the snapshot without a `removed` event.
- **D22 (M2) changedetection fields.** `last_checked` is not a field (it changes on every check and would
  flood the digest); the latest snapshot text is, so a watch change shows as `text changed (+a/-b chars)`.
- **D23 (M2) Browser.** Bundled Playwright Chromium by default; `browser_channel: msedge|chrome` uses an
  installed browser instead (no extra download on Windows, where Edge is always present).
- **D24 (M2) Login problems are always surfaced.** Collectors raise `LoginRequired` (a CollectError) for
  errors only a human can fix: web `login expired`, imap `login failed for …`, changedetection
  `API key rejected`. While a source is already in error, a LoginRequired failure whose message differs from
  the current `last_error` still appends a `source_error` (plain errors stay deduplicated). An agent that saw
  "layout broken" must learn that the cause is now "log in again".
- **D25 (M2) Broken extraction doesn't move the good fingerprint.** In the broken state only the sorted broken
  selectors are compared (dedupe) and the stored fingerprint stays the last *good* one, so a page that comes
  back unchanged recovers with just `source_recovered` (no spurious "layout changed").
- **D26 (M2) Collector default `track_fields`.** Like titles (D17), a collector may supply default
  `track_fields`; imap uses `[folder, flagged, answered]`, so a mail merely being read (`seen`) no longer
  creates an event that outranks new mail. Config `track_fields` still overrides. Default track fields
  only filter modifications; `+` lines list fields only when `track_fields` is set in the config.
- **D27 (M2) Selector wording.** `schema_changed` says `matches 0 elements` (not `rows`): the broken selector
  can be a container or a field selector.

### M2 QA round 1 (revisions)
- **D18 revised — broken extraction is independent of the fingerprint.** `broken` is honoured even when
  `fingerprint` is None (e.g. `fingerprint_depth: 0`, or a login page without `login_detect`). The key
  selector counts as broken when rows matched but no row has a non-empty key. Field selectors that match in no
  row are broken unless the field is listed in the web option `extract.optional`.
- **D20/D21 revised — imap window follows the server's own filter.** New field `received` (INTERNALDATE, ISO
  UTC). The window is on `received`, per folder, with a one-day margin (servers apply `SEARCH SINCE` to the
  INTERNALDATE date in their own zone). When a folder has more than `max_messages` matches, the collector
  shortens that folder's SINCE date (bisection over days) so everything it keeps is complete for the dates it
  claims; only when a single day exceeds the cap does it fall back to the newest UIDs. `Window` gains an
  optional per-scope start (`scope_field` + `starts` by value) for this.
- **D24 revised — compare with the last *announced* error.** A `LoginRequired` failure appends a
  `source_error` only if its message differs from the last `source_error` announced in the current streak
  (store column `announced_error`, cleared on recovery). LoginRequired carries a human hint rendered after the
  quoted message: `; needs a human: <hint>` (web: `run since login <id>`; imap/changedetection: check the
  secret in the named env var).
- **D28 — resolved errors rank low.** In a digest, a `source_error` or selector-listing `schema_changed` with a
  later `source_recovered` for the same source is marked ` (recovered)` and ranked with the recovered weight
  (priority × 1), so it cannot push real changes into `omitted:`.
- **D29 — timestamps in titles.** imap titles are `[subject, from, received]`. A title value that is a
  Since-normalised ISO UTC timestamp renders unquoted and compact (`at 2026-09-29 09:12Z`), since Since
  produced that text itself.
- **D30 — single daemon via an OS file lock.** `daemon.lock` in SINCE_HOME, held with an exclusive
  non-blocking lock for the daemon's lifetime (released by the OS on crash). The heartbeat stays for
  staleness reporting only.
- **D31 — `*_env` options must be env var names.** Any source option ending in `_env` must match
  `^[A-Za-z_][A-Za-z0-9_]*$`; otherwise a ConfigError that does not echo the value (a pasted secret must
  never reach an error message or the DB). The credential error names `url_env / password_env / api_key_env`.

### M3
- **D32 (M3) `since ui` is read-only, localhost-only, no JavaScript.** It shows untrusted source data, so every
  value is HTML-escaped, a strict CSP forbids scripts, and the Host header is checked (DNS rebinding).
- **D33 (M3) Benchmark world.** Fixed seed and fixed simulated dates; "needing attention" is defined by four
  explicit rules in the task prompt and the answer key is computed from those rules, so recall/precision are
  objective. Sources: mail, PO table, portal (changedetection is not needed for the task).
- **D34 (M3) Arm A gets its own notes from the last look.** An agent without any memory cannot know what
  changed; CLAUDE.md's premise is that agents re-read and diff inside their context. Arm A therefore gets the
  PO table and portal text as of its last look (like notes it saved), plus raw access to everything now.
- **D35 (M3) Measurement.** Headless Claude Code with only the arm's MCP server (`--tools ""`,
  `--strict-mcp-config`), a per-run `--max-budget-usd` cap; input tokens include cached input (reported
  separately too); refs graded after normalisation; duplicates counted once.
- **D36 (M3) Benchmark spend.** Default 3 runs per arm on Sonnet with a $3 per-run cap; the harness never
  runs more than the requested runs.
