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
- **D5 (M1) `dir`: any unreadable file fails the whole run.** Skipping it would look like `removed`, which a
  failed run must never produce.
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
