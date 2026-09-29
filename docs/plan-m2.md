# M2 plan — sources

Scope (CLAUDE.md "Milestones"): `imap`, `web` (persistent profile, YAML extractor, login_detect, structural
fingerprint), `changedetection` (REST API). Plus the core support they need: record titles, collection
windows, page-structure tracking, and `since login` for the one-time manual web login.

Spec clarifications for M2 are D17–D23 in `docs/decisions.md`. Conventions from `docs/plan-m1.md` still apply.

## Conventions (additions)

- New dependency only: extra `web = ["playwright>=1.45"]` (T5). imap uses stdlib `imaplib` + `email`;
  changedetection uses stdlib `urllib.request` + `json`. Nothing else without asking.
- Tests never touch the internet: every server (IMAP, changedetection API, web page) is a local fake on
  127.0.0.1 started by a fixture.
- Credentials only via env vars (D9): imap `password_env`, changedetection `api_key_env`. Error messages never
  contain a password or API key (test it).
- Collectors are read-only against the source: imap uses EXAMINE and BODY.PEEK only (never SELECT, STORE,
  EXPUNGE); web only navigates and reads the DOM.
- Browser tests: `web` tests need a Chromium. They use the bundled one (`playwright install chromium`) unless
  `SINCE_TEST_BROWSER_CHANNEL` is set (e.g. `msedge` on Windows dev boxes). If no browser can be launched they
  skip with a clear reason; CI installs Chromium so they always run there.

## Shared definitions

**Title (D17).** Core source option `title_fields`: list of 1–3 field names, validated like `track_fields`.
If absent, the collector's default is used (`Collector.default_title_fields(cfg) -> list[str]`, optional
method; missing = `[]`): imap `[subject, from]`, changedetection `[title]`, others `[]`.
For added/modified/removed events the runner stores `detail["title"] = [[field, value], …]` for the title
fields present in the record (new fields for added/modified, last known fields for removed), in title-field
order. Rendering: when `detail.title` is non-empty the record label is the title instead of
`key_label "key"`: first value `q(v, cap)`, then ` field q(v, cap)` for each further one
(`"Re: DJ ASN rejection" from "edi@supplier.example"`). Cap for title values: 80 in digests/batch,
GET_CAP in `get`. The `get` evt `record:` line uses the same label followed by the rec handle.

**Collection window (D21).** `CollectOutput.window: Window(field, start) | None`. A record in the old
snapshot that is absent from the new result and whose `fields[field]` (ISO UTC string) is `< start` has aged
out: it is dropped from the snapshot (mark_removed) **without** an event. Everything else absent is `removed`
as usual.

**Page structure (D18).** `CollectOutput.fingerprint: str | None` and `CollectOutput.broken: list[str]`
(extractor selectors that matched nothing). Stored per source in new columns `fingerprint`, `broken_json`
(store schema v2; opening a v1 DB migrates it with `ALTER TABLE` inside the write path).
Runner, when a collector returns a fingerprint:
- Not baselined yet: broken non-empty → run failure (`source_error`) with message
  `extractor selector(s) match 0 elements: "a", "b"`; otherwise store fingerprint/broken and baseline.
- Baselined, broken non-empty: snapshot untouched, no record events. If `(fingerprint, broken)` differs from
  the stored pair → append `schema_changed {selectors: broken}`. Set in_error (error_since if not already),
  last_error = the message above, last_error_at; store the pair. Later runs in the same broken state append
  nothing. When extraction works again the usual `source_recovered` follows.
- Baselined, broken empty, stored fingerprint not None and different → append
  `schema_changed {selectors: []}`, store it, then diff normally.
Render: zero selectors → `! schema_changed: page layout changed; extractor selectors still match`
(evt view: `page layout changed; extractor selectors still match`).

## Sources

### changedetection (D22)
Options: `url` (base URL of the changedetection.io instance), `api_key_env` (optional; sends `x-api-key`),
`tag` (optional; list filter), `fetch_text` (bool, default true), `timeout_s` (default 30).
API: `GET {url}/api/v1/watch[?tag=…]` → `{uuid: {url, title, last_changed, last_error, …}}`;
`GET {url}/api/v1/watch/{uuid}/history/latest` → latest snapshot text. The implementer checks the real API
docs if reachable and reports any mismatch instead of guessing.
Record per watch: key = uuid; fields `url`, `title` (watch title or its url), `last_changed` (ISO UTC or None),
`last_error` (string, "" when none), `text` (latest snapshot, only if fetch_text and available; a missing
history is not an error). Never `last_checked` (changes on every check). 401/403 → CollectError
`API key rejected`; other HTTP/connection errors → CollectError with status/reason only.

### imap (D20)
Options: `host`, `port` (default 993 for ssl, else 143), `security` (`ssl` | `starttls` | `none`, default ssl),
`username`, `password_env`, `folders` (default `[INBOX]`), `since_days` (default 14, 1–365),
`max_messages` (per folder, default 500, newest UIDs first).
Per folder: EXAMINE; `UID SEARCH SINCE <date>`; newest max_messages UIDs; `UID FETCH` in batches of 100 with
`(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM TO DATE)])`.
Fields: `subject` (RFC 2047 decoded), `from` (decoded, `Name <addr>`), `to` (first 3 decoded addresses joined
by `, `), `date` (Date header → ISO UTC, fallback INTERNALDATE), `folder`, `seen`, `flagged`, `answered`
(bools from flags), `size` (int).
Key = Message-ID (stripped); missing → `uid:<folder>/<uidvalidity>/<uid>`. The same key more than once (same
mail in several folders/labels) → keep the first by (folder order, UID) — collector-level dedupe, so D4 never
fires for imap. Window: `Window("date", <start of the SINCE day, ISO UTC>)`. key_label `""`.
Login failure → CollectError `login failed for <username>`; never include the password.

### web
Options: `url`; `profile_dir` (default `<SINCE_HOME>/profiles/<source_id>`); `login_detect` — exactly one of
`{url_contains: "/login"}` (checked on the final URL) or `{selector: "form#login"}` (element present);
`extract`: `rows` (selector), `key` (a field name), `fields` (`{name: selector}`; `selector@attr` reads an
attribute instead of text), optional `container` (selector that must exist; with it, zero rows is valid
data); `wait_for` (optional selector, waited for up to timeout); `timeout_s` (default 30);
`browser_channel` (optional: `msedge` / `chrome`; default bundled Chromium); `fingerprint_depth`
(default 8, 0 disables fingerprinting).
Collect: `launch_persistent_context(profile_dir, headless=True, channel=…)`, goto url (wait `load`),
optional wait_for, login_detect → CollectError `login expired`. Rows: each `rows` match; field value = text
of the first match of its selector inside the row (innerText, whitespace runs collapsed, stripped) or the
attribute; no match → "". Rows whose key value is empty are skipped. Always close the context.
Broken: `container` if set and matches nothing, else `rows` if it matches nothing; plus (when rows > 0) any
field selector that matches in no row.
Fingerprint: from `document.body`, every element down to `fingerprint_depth`, path of `tag` + sorted
classes (`div.a.b>table.grid`), skipping script/style/noscript/template and the subtrees of `rows` matches;
sha256 of the sorted unique paths.

### `since login <source_id>` (D19)
Web sources only (others → exit 2). Opens a **headed** persistent context on the source's profile_dir at its
url, prints `Log in to <id> in the browser window, then close it.`, waits until the window is closed, exits 0.
Profile locked (the daemon is collecting) → exit 1 with `profile in use (the daemon may be collecting); try
again in a minute`. Never types anything into the page (no auto-login).

## Tasks

Order: T1 → T2 → (T3 ∥ T4 ∥ T5) → T6 → T7.

### T1 — titles (`config.py`, `sources/__init__.py`, `collect.py`, `render.py`, `digest.py`, `service.py`, tests)
`title_fields` option + collector default + `detail.title` on record events + label rendering in digest,
batch and evt views.
Acceptance: config validation; titles stored for added/modified/removed (removed uses last known fields);
label falls back to key when no title field is present; hostile title values stay quoted, single-line,
capped at 80; new golden with titled events; existing goldens unchanged.

### T2 — window + page structure (`sources/__init__.py`, `collect.py`, `store.py`, `render.py`, `service.py`, tests)
`Window`, `fingerprint`, `broken` on CollectOutput; store schema v2 + migration; runner rules from
"Collection window" and "Page structure"; zero-selector render texts.
Acceptance: aged-out records vanish without events while in-window absences are `removed`; every bullet of
"Page structure" as a runner test with a fake collector (incl. first-run broken → source_error, repeated
broken state → no new event, recovery, layout-only change → `schema_changed` with no selectors then a normal
diff); a v1 DB file opens and is migrated; read-only open of a v2 DB still takes no write lock.

### T3 — changedetection (`sources/changedetection.py`, tests with a local fake API server)
Acceptance: baseline/added/removed/modified through run_collection; text change → `text changed (+a/-b chars)`;
tag filter; missing history ok; 401 → `API key rejected`; connection refused → source_error, no removed;
API key never in stored errors.

### T4 — imap (`sources/imap.py`, `tests/imap_fake.py` minimal threaded IMAP4rev1 server, tests)
The fake supports CAPABILITY, LOGIN, EXAMINE (with UIDVALIDITY), UID SEARCH SINCE, UID FETCH of the items above,
LOGOUT, and records every command so tests can assert read-only behaviour.
Acceptance: baseline; new mail → titled `+` line; flag change → modified `seen`; deleted mail → removed;
aged-out mail → no event; duplicate Message-ID across two folders → one record; missing Message-ID →
uid key; RFC 2047 subjects; bad password → `login failed for …` without the password; no SELECT/STORE/
EXPUNGE ever sent; `security: none` against the fake (ssl/starttls paths unit-tested by mocking).

### T5 — web (`sources/web.py`, `pyproject.toml`/`uv.lock` extra `web`, tests with a local http.server page)
Acceptance: rows/fields/attr extraction; empty-key rows skipped; login_detect by url and by selector →
`login expired`; container + zero rows = valid empty; rows selector broken → schema_changed, no removed;
layout-only change → schema_changed with no selectors; fingerprint unchanged when only row count/text
changes; context always closed; timeouts → source_error.

### T6 — `since login`, CI, README
`cli.py` login command (browser launch injectable for tests); CI installs Chromium
(`uv run playwright install --with-deps chromium`); README sections for the three new sources and
`since login`.

### T7 — M2 end-to-end (`tests/test_e2e_m2.py`)
Subprocess CLI run with all three fakes: baseline via `daemon --once`, mutate (new mail, flag change, watch
text change, portal status change, portal layout break, login expiry), collect, digest ordering and lines,
get handles, ack.
