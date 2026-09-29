"""``web`` source: a logged-in web page (supplier portal, ERP screen) read through a persistent
browser profile; every row of an HTML table/list is one record.

Config keys (``SourceConfig.options``)::

    url: https://example.invalid/orders        # required, http or https
    profile_dir: ~/.since/profiles/sps         # Playwright persistent profile; default
                                               # <SINCE_HOME>/profiles/<source_id>. "~" is
                                               # expanded, the result must be absolute. The human
                                               # logs in once by hand (``since login``); Since
                                               # never types anything into the page.
    login_detect: {url_contains: /login}       # exactly one of url_contains (checked on the final
                                               # URL) or selector (element present)
    extract:
      rows: "table#orders tbody tr"            # one record per match
      key: po                                  # a field name below; empty-key rows are skipped
      fields: {po: "td:nth-child(1)", status: "td:nth-child(4)", link: "a.detail@href"}
      container: "table#orders"                # optional: must exist; then zero rows is valid
    wait_for: "table#orders"                   # optional selector awaited (attached) after load
    timeout_s: 30                              # 1-300; navigation and wait_for limit
    browser_channel: msedge                    # msedge | chrome; default: Playwright's Chromium
    fingerprint_depth: 8                       # 0-32; 0 disables the structural fingerprint

A field selector is a CSS selector evaluated inside the row; its value is the ``innerText`` of the
first match with whitespace runs collapsed and stripped. ``selector@attr`` reads the attribute
``attr`` of that element instead (``""`` when it has none). A field that matches nothing in a row is
``""``. ``container`` and ``rows`` are searched in the whole document.

Playwright is an optional extra (``since[web]``) and imported inside ``collect`` only. The browser
is only navigated and read: the DOM snapshot is taken by ONE ``page.evaluate`` (rows, which
selectors matched, structural fingerprint), so all three describe the same page state. The context
and Playwright are always shut down, whatever happens. Error messages never contain page content.

Broken extraction (D18): ``CollectOutput.broken`` lists the selectors that matched nothing: the
``container`` if configured and absent, else ``rows`` if it matches nothing; and, when ``rows``
matched, every field selector that matches in no row. Broken results are never diffed (the runner
sees to that), so a layout change cannot look like a wave of removed records.

Fingerprint (D18): the elements below ``document.body`` down to ``fingerprint_depth`` levels (body's
children are level 1; ``body`` itself is not part of any path), each as ``tag`` + its sorted classes
(``div.a.b>table.grid``); script/style/noscript/template and the whole subtrees of ``rows`` matches
(the row elements included, so zebra classes and the row count do not matter) are skipped. The
fingerprint is the sha256 (hex) of the sorted unique paths joined by newlines.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from since.collect import MAX_FIELD_NAME_CHARS, _field_name_allowed
from since.config import ConfigError, SourceConfig
from since.model import Record
from since.paths import since_home
from since.sources import CollectError, CollectOutput

_KNOWN_OPTIONS = (
    "url",
    "profile_dir",
    "login_detect",
    "extract",
    "wait_for",
    "timeout_s",
    "browser_channel",
    "fingerprint_depth",
)
_LOGIN_KEYS = ("url_contains", "selector")
_EXTRACT_KEYS = ("rows", "key", "fields", "container")
_CHANNELS = ("msedge", "chrome")

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
DEFAULT_FINGERPRINT_DEPTH = 8
MAX_FINGERPRINT_DEPTH = 32

_NOT_INSTALLED = (
    "Playwright is not installed: install since[web] and run `playwright install chromium`"
)
_MAX_MESSAGE_CHARS = 200

# ``selector@attr``: the attribute name must follow the LAST "@" up to the end of the string, and
# the selector must not end in a backslash (``.a\@b`` is an escaped "@", not an attribute).
_ATTR_SUFFIX = re.compile(
    r"^(?P<selector>.*[^\\])@(?P<attr>[A-Za-z_:][-A-Za-z0-9_:.]*)$", re.DOTALL
)

# The one function evaluated in the page. Input: {container, rows, fields: [[selector, attr]],
# depth}. Output (plain data): {invalid: the first selector the browser rejected, or None;
# container_found: bool | None (None = no container configured); row_count; rows: one list of
# strings per matched row in field order; field_matched: per field, true if it matched in at least
# one row; paths: fingerprint paths | None}. Rows are arrays (not objects) so that a field named
# ``__proto__`` cannot misbehave.
_EXTRACT_JS = r"""
(cfg) => {
  const out = {invalid: null, container_found: null, row_count: 0, rows: [],
               field_matched: [], paths: null};
  const collapse = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  const valid = (selector) => {
    try { document.querySelector(selector); return true; } catch (e) { return false; }
  };
  const selectors = [cfg.rows, ...cfg.fields.map((f) => f[0])];
  if (cfg.container !== null) selectors.unshift(cfg.container);
  for (const selector of selectors) {
    if (!valid(selector)) { out.invalid = selector; return out; }
  }
  if (cfg.container !== null) out.container_found = document.querySelector(cfg.container) !== null;
  const rows = Array.from(document.querySelectorAll(cfg.rows));
  out.row_count = rows.length;
  out.field_matched = cfg.fields.map(() => false);
  for (const row of rows) {
    out.rows.push(cfg.fields.map((f, i) => {
      const el = row.querySelector(f[0]);
      if (el === null) return "";
      out.field_matched[i] = true;
      if (f[1] !== null) return el.getAttribute(f[1]) || "";
      return collapse(el.innerText !== undefined ? el.innerText : el.textContent);
    }));
  }
  if (cfg.depth > 0 && document.body) {
    const skipTags = new Set(["script", "style", "noscript", "template"]);
    const skipRows = new Set(rows);
    const paths = new Set();
    const walk = (parent, prefix, depth) => {
      for (const el of parent.children) {
        if (skipTags.has(el.localName.toLowerCase()) || skipRows.has(el)) continue;
        const classes = Array.from(el.classList).sort();
        const segment = el.localName + classes.map((c) => "." + c).join("");
        const path = prefix === "" ? segment : prefix + ">" + segment;
        paths.add(path);
        if (depth < cfg.depth) walk(el, path, depth + 1);
      }
    };
    walk(document.body, "", 1);
    out.paths = Array.from(paths);
  } else if (cfg.depth > 0) {
    out.paths = [];
  }
  return out;
}
"""


# -- options -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    name: str
    selector: str  # CSS selector, without the ``@attr`` suffix
    attr: str | None


@dataclass(frozen=True)
class _Options:
    url: str
    profile_dir: Path | None  # None = the default under SINCE_HOME
    login_url_contains: str | None
    login_selector: str | None
    rows: str
    key: str
    fields: tuple[_Field, ...]
    container: str | None
    wait_for: str | None
    timeout_s: int
    browser_channel: str | None
    fingerprint_depth: int


def _bad(cfg: SourceConfig, key: str, message: str) -> ConfigError:
    return ConfigError(f"source '{cfg.id}': key '{key}': {message}")


def _unknown(
    cfg: SourceConfig, mapping: Mapping[Any, Any], prefix: str, allowed: tuple[str, ...]
) -> None:
    """Raise for the first key of ``mapping`` that is not in ``allowed``."""
    for key in sorted(mapping, key=str):
        if key not in allowed:
            name = f"{prefix}{key}"
            raise _bad(cfg, name, f"unknown option (accepted here: {', '.join(allowed)})")


def _selector(cfg: SourceConfig, name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(cfg, name, "must be a non-empty CSS selector string")
    return value.strip()


def _int_option(cfg: SourceConfig, name: str, default: int, low: int, high: int) -> int:
    value = cfg.options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad(cfg, name, f"must be an integer from {low} to {high}")
    return value


def split_field_selector(raw: str) -> tuple[str, str | None]:
    """``"a.detail@href"`` -> ``("a.detail", "href")``; a selector without a valid ``@attr``
    suffix -> ``(selector, None)``. Surrounding whitespace is stripped."""
    text = raw.strip()
    match = _ATTR_SUFFIX.match(text)
    if match is None:
        return text, None
    return match["selector"].strip(), match["attr"]


def _parse_fields(cfg: SourceConfig, raw: Any) -> tuple[_Field, ...]:
    if not isinstance(raw, Mapping) or not raw:
        raise _bad(cfg, "extract.fields", "required, must be a non-empty mapping name -> selector")
    fields: list[_Field] = []
    for name, value in raw.items():
        if not isinstance(name, str) or not _field_name_allowed(name):
            raise _bad(
                cfg,
                "extract.fields",
                f"field names must be non-empty strings of at most {MAX_FIELD_NAME_CHARS} "
                "characters without control characters",
            )
        where = f"extract.fields.{name}"
        if not isinstance(value, str) or not value.strip():
            raise _bad(cfg, where, "must be a non-empty CSS selector string")
        if value.strip().startswith("@"):
            raise _bad(cfg, where, "the selector before '@attr' is missing")
        selector, attr = split_field_selector(value)
        fields.append(_Field(name, selector, attr))
    return tuple(fields)


def _parse_url(cfg: SourceConfig) -> str:
    value = cfg.options.get("url")
    if not isinstance(value, str) or not value.strip():
        raise _bad(cfg, "url", "required, must be an http(s) URL")
    url = value.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise _bad(cfg, "url", "is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise _bad(cfg, "url", "must be an http:// or https:// URL")
    return url


def _parse_profile_dir(cfg: SourceConfig) -> Path | None:
    if "profile_dir" not in cfg.options:
        return None
    value = cfg.options["profile_dir"]
    if not isinstance(value, str) or not value.strip():
        raise _bad(cfg, "profile_dir", "must be a non-empty path string")
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:  # "~user" that does not exist
        raise _bad(cfg, "profile_dir", f"cannot expand '~': {exc}") from None
    if not path.is_absolute():
        raise _bad(cfg, "profile_dir", "must be an absolute path (or start with ~)")
    return path


def _parse_login_detect(cfg: SourceConfig) -> tuple[str | None, str | None]:
    if "login_detect" not in cfg.options:
        return None, None
    raw = cfg.options["login_detect"]
    if not isinstance(raw, Mapping):
        raise _bad(cfg, "login_detect", "must be a mapping with url_contains or selector")
    _unknown(cfg, raw, "login_detect.", _LOGIN_KEYS)
    present = [k for k in _LOGIN_KEYS if k in raw]
    if len(present) != 1:
        raise _bad(cfg, "login_detect", "must have exactly one of url_contains / selector")
    name = present[0]
    value = raw[name]
    if not isinstance(value, str) or not value.strip():
        raise _bad(cfg, f"login_detect.{name}", "must be a non-empty string")
    return (value, None) if name == "url_contains" else (None, value.strip())


def _parse_options(cfg: SourceConfig) -> _Options:
    """Check ``cfg.options`` (no I/O); ``ConfigError`` naming the offending key."""
    _unknown(cfg, cfg.options, "", _KNOWN_OPTIONS)
    url = _parse_url(cfg)
    profile_dir = _parse_profile_dir(cfg)
    login_url_contains, login_selector = _parse_login_detect(cfg)

    extract = cfg.options.get("extract")
    if not isinstance(extract, Mapping):
        raise _bad(cfg, "extract", "required, must be a mapping with rows, key and fields")
    _unknown(cfg, extract, "extract.", _EXTRACT_KEYS)
    rows = _selector(cfg, "extract.rows", extract.get("rows"))
    fields = _parse_fields(cfg, extract.get("fields"))
    key = extract.get("key")
    if not isinstance(key, str) or not key:
        raise _bad(cfg, "extract.key", "required, must be one of the field names")
    if key not in {f.name for f in fields}:
        raise _bad(cfg, "extract.key", f"'{key}' is not one of the extract.fields names")
    container = None
    if "container" in extract:
        container = _selector(cfg, "extract.container", extract["container"])

    wait_for = None
    if "wait_for" in cfg.options:
        wait_for = _selector(cfg, "wait_for", cfg.options["wait_for"])
    channel = cfg.options.get("browser_channel")
    if "browser_channel" in cfg.options and channel not in _CHANNELS:
        raise _bad(cfg, "browser_channel", f"must be one of {', '.join(_CHANNELS)}")
    return _Options(
        url=url,
        profile_dir=profile_dir,
        login_url_contains=login_url_contains,
        login_selector=login_selector,
        rows=rows,
        key=key,
        fields=fields,
        container=container,
        wait_for=wait_for,
        timeout_s=_int_option(cfg, "timeout_s", DEFAULT_TIMEOUT_S, 1, MAX_TIMEOUT_S),
        browser_channel=channel,
        fingerprint_depth=_int_option(
            cfg, "fingerprint_depth", DEFAULT_FINGERPRINT_DEPTH, 0, MAX_FINGERPRINT_DEPTH
        ),
    )


# -- result interpretation (pure) ----------------------------------------------------------------


def fingerprint_of(paths: Iterable[str]) -> str:
    """sha256 (hex) of the sorted unique structural paths joined by newlines."""
    joined = "\n".join(sorted(set(paths)))
    return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()


def _unexpected(what: str) -> CollectError:
    return CollectError(f"unexpected result from the page ({what})")


def _interpret(opts: _Options, raw: Any) -> CollectOutput:
    """Turn what ``_EXTRACT_JS`` returned into a ``CollectOutput``. The page is untrusted (it can
    tamper with its own scripting environment), so the shape of ``raw`` is checked, not assumed."""
    if not isinstance(raw, dict):
        raise _unexpected("not a mapping")
    invalid = raw.get("invalid")
    if invalid is not None:
        configured = {opts.rows, opts.container, *(f.selector for f in opts.fields)}
        if not isinstance(invalid, str) or invalid not in configured:
            raise _unexpected("wrong selector")  # never echo what the page returns
        raise CollectError(f"invalid CSS selector in the config: {invalid[:120]!r}")
    row_count = raw.get("row_count")
    rows = raw.get("rows")
    matched = raw.get("field_matched")
    paths = raw.get("paths")
    n_fields = len(opts.fields)
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or not isinstance(rows, list)
        or len(rows) != row_count
        or not isinstance(matched, list)
        or len(matched) != n_fields
        or not all(isinstance(m, bool) for m in matched)
        or (paths is not None and not isinstance(paths, list))
    ):
        raise _unexpected("wrong structure")
    key_index = next(i for i, f in enumerate(opts.fields) if f.name == opts.key)

    records: list[Record] = []
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != n_fields
            or not all(isinstance(v, str) for v in row)
        ):
            raise _unexpected("wrong row")
        if not row[key_index].strip():
            continue
        records.append(
            Record.make(row[key_index], {f.name: v for f, v in zip(opts.fields, row, strict=True)})
        )
    records.sort(key=lambda r: r.key)

    broken: list[str] = []
    if opts.container is not None:
        if raw.get("container_found") is not True:
            broken.append(opts.container)
    elif row_count == 0:
        broken.append(opts.rows)
    if row_count > 0:
        broken.extend(f.selector for f, ok in zip(opts.fields, matched, strict=True) if not ok)
    broken = list(dict.fromkeys(broken))

    fingerprint = None
    if opts.fingerprint_depth > 0:
        if paths is None or not all(isinstance(p, str) for p in paths):
            raise _unexpected("wrong fingerprint")
        fingerprint = fingerprint_of(paths)
    return CollectOutput(records, fingerprint=fingerprint, broken=broken)


# -- browser -------------------------------------------------------------------------------------


def _describe(exc: BaseException, url: str) -> str:
    """A short, page-content-free description of a Playwright error: its first line, with the
    configured URL (which may carry a token) removed, capped."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    text = lines[0] if lines else type(exc).__name__
    if url:
        text = text.replace(url, "<url>")
    if "Executable doesn't exist" in text:
        text = text.split(" at ")[0] + " (run `playwright install chromium`)"
    return "".join(c for c in text if c.isprintable())[:_MAX_MESSAGE_CHARS]


def _import_playwright() -> Any:
    try:
        import playwright.sync_api as api
    except ImportError:
        raise CollectError(_NOT_INSTALLED) from None
    return api


def _logged_out(page: Any, opts: _Options) -> bool:
    """login_detect: ``url_contains`` on the final URL, or ``selector`` present in the page."""
    if opts.login_url_contains is not None and opts.login_url_contains in page.url:
        return True
    return opts.login_selector is not None and page.locator(opts.login_selector).count() > 0


def _read_page(api: Any, page: Any, opts: _Options) -> Any:
    """Navigate and take the DOM snapshot; returns the raw ``_EXTRACT_JS`` result."""
    ms = opts.timeout_s * 1000
    try:
        page.goto(opts.url, wait_until="load", timeout=ms)
    except api.TimeoutError:
        raise CollectError(f"timed out after {opts.timeout_s}s loading the page") from None
    except api.Error as exc:
        raise CollectError(f"cannot load the page: {_describe(exc, opts.url)}") from None
    if _logged_out(page, opts):
        raise CollectError("login expired")
    if opts.wait_for is not None:
        try:
            page.wait_for_selector(opts.wait_for, state="attached", timeout=ms)
        except api.TimeoutError:
            if _logged_out(page, opts):  # the login page does not have the awaited element
                raise CollectError("login expired") from None
            raise CollectError(
                f"timed out after {opts.timeout_s}s waiting for selector {opts.wait_for[:120]!r}"
            ) from None
        if _logged_out(page, opts):
            raise CollectError("login expired")
    argument = {
        "container": opts.container,
        "rows": opts.rows,
        "fields": [[f.selector, f.attr] for f in opts.fields],
        "depth": opts.fingerprint_depth,
    }
    try:
        return page.evaluate(_EXTRACT_JS, argument)
    except api.Error as exc:
        # The page's own scripts can throw from inside our function, and what they say is page
        # content: never echo it.
        if "Execution context was destroyed" in str(exc):
            raise CollectError("the page navigated while it was being read") from None
        raise CollectError("cannot read the page: the extraction script failed") from None


def _profile_dir(cfg: SourceConfig, opts: _Options) -> Path:
    """The profile directory (default under SINCE_HOME), created if missing. It holds the login
    state, so a newly created one is private (POSIX 0700)."""
    path = opts.profile_dir or since_home() / "profiles" / cfg.id
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path, 0o700)
    return path


class WebCollector:
    type_name = "web"

    def validate(self, cfg: SourceConfig) -> None:
        _parse_options(cfg)

    def key_label(self, cfg: SourceConfig) -> str:
        """The name of the key field (tolerant of a malformed config)."""
        extract = cfg.options.get("extract")
        if isinstance(extract, Mapping) and isinstance(extract.get("key"), str):
            return extract["key"]
        return ""

    def collect(self, cfg: SourceConfig) -> CollectOutput:
        try:
            opts = _parse_options(cfg)
        except ConfigError as exc:
            raise CollectError(str(exc)) from None
        api = _import_playwright()
        profile = _profile_dir(cfg, opts)

        pw = context = None
        try:
            pw = api.sync_playwright().start()
            context = pw.chromium.launch_persistent_context(
                str(profile), headless=True, channel=opts.browser_channel
            )
            page = context.pages[0] if context.pages else context.new_page()
            raw = _read_page(api, page, opts)
        except api.TimeoutError:
            raise CollectError(f"timed out after {opts.timeout_s}s") from None
        except api.Error as exc:
            raise CollectError(_describe(exc, opts.url)) from None
        finally:
            # A browser that already died must not mask the real outcome.
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if pw is not None:
                with contextlib.suppress(Exception):
                    pw.stop()
        return _interpret(opts, raw)
