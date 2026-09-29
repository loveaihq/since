"""``web`` source tests.

Three layers:

- pure Python (validation, result interpretation, fingerprint hashing): no browser;
- a fake Playwright (error mapping, "always closed", ``since login``): no browser;
- a real headless browser against a local ``http.server`` page, driven through ``run_collection``.
  The browser is Playwright's Chromium unless ``SINCE_TEST_BROWSER_CHANNEL`` names an installed
  one (``msedge`` on Windows dev boxes). If no browser can be launched these tests skip; on CI
  (``CI`` is set) that is a failure, because CI installs Chromium.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from login_fake import FakeApi, FakeError

from since.collect import CollectResult, register_sources, run_collection
from since.config import Config, ConfigError, SourceConfig
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SCHEMA_CHANGED,
    KIND_SOURCE_ERROR,
)
from since.paths import since_home
from since.sources import CollectError, CollectOutput
from since.sources.web import (
    PROFILE_IN_USE,
    LoginError,
    WebCollector,
    _Field,
    _interpret,
    _Options,
    _parse_options,
    _profile_dir,
    _read_page,
    fingerprint_of,
    login,
    split_field_selector,
)
from since.store import Store

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)
CHANNEL = os.environ.get("SINCE_TEST_BROWSER_CHANNEL") or None
SOURCE_ID = "sps-portal"
SELECTOR_ROWS = "table#orders tbody tr"
EXTRACT: dict[str, Any] = {
    "rows": SELECTOR_ROWS,
    "key": "po",
    "fields": {
        "po": "td:nth-child(1)",
        "status": "td:nth-child(4)",
        "link": "td:nth-child(2) a@href",
    },
}


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def good_options(**changes: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "url": "https://example.invalid/orders",
        "extract": {**EXTRACT, "fields": dict(EXTRACT["fields"])},
    }
    options.update(changes)
    return options


def make_cfg(url: str = "https://example.invalid/orders", **options: Any) -> SourceConfig:
    """A sps-portal source. ``browser_channel`` comes from the environment for browser tests."""
    merged = good_options(url=url, timeout_s=20)
    if CHANNEL:
        merged["browser_channel"] = CHANNEL
    merged.update(options)
    return SourceConfig(id=SOURCE_ID, type="web", priority="high", schedule_s=1800, options=merged)


def extract_with(**changes: Any) -> dict[str, Any]:
    return {**EXTRACT, "fields": dict(EXTRACT["fields"]), **changes}


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


def run(store: Store, cfg: SourceConfig, minute: int = 0) -> CollectResult:
    return run_collection(store, cfg, WebCollector(), at(minute))


def events(store: Store) -> list[Any]:
    return store.events_after(0, SOURCE_ID)


def kinds(store: Store) -> list[str]:
    return [e.kind for e in events(store)]


def keyed(store: Store, kind: str) -> set[str | None]:
    return {e.record_key for e in events(store) if e.kind == kind}


# -- module / registry ---------------------------------------------------------------------------


def test_importing_the_module_does_not_import_playwright() -> None:
    code = "import sys, since.sources.web; assert 'playwright' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_collector_basics() -> None:
    collector = WebCollector()
    assert collector.type_name == "web"
    assert collector.key_label(make_cfg()) == "po"
    assert collector.key_label(SourceConfig(id="x", type="web", options={})) == ""
    assert collector.key_label(SourceConfig(id="x", type="web", options={"extract": 5})) == ""


def test_register_sources_stores_the_key_label(store: Store) -> None:
    register_sources(store, Config(sources=[make_cfg()]), {"web": WebCollector()})
    state = store.get_source_state(SOURCE_ID)
    assert state is not None and state.key_label == "po"


def test_missing_playwright_is_a_collect_error(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # makes the import fail
    expected = (
        "Playwright is not installed: install since[web] and run `playwright install chromium`"
    )
    with pytest.raises(CollectError) as info:
        WebCollector().collect(make_cfg())
    assert str(info.value) == expected
    assert run(store, make_cfg()).error == expected
    assert kinds(store) == [KIND_SOURCE_ERROR]


# -- validate ------------------------------------------------------------------------------------

_DEL = object()


def with_path(dotted: str, value: Any) -> dict[str, Any]:
    """``good_options()`` with ``value`` set (or, for ``_DEL``, removed) at a dotted path."""
    options = good_options()
    target = options
    *parents, last = dotted.split(".")
    for part in parents:
        target = target[part]
    if value is _DEL:
        del target[last]
    else:
        target[last] = value
    return options


def test_validate_accepts_the_documented_config_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("validate must not touch the filesystem")

    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # nor need Playwright
    cfg = SourceConfig(
        id=SOURCE_ID,
        type="web",
        options={
            "url": "https://example.invalid/orders",
            "profile_dir": "~/.since/profiles/sps",
            "login_detect": {"url_contains": "/login"},
            "extract": {
                "rows": "table#orders tbody tr",
                "key": "po",
                "fields": {"po": "td:nth-child(1)", "status": "td:nth-child(4)"},
            },
        },
    )
    WebCollector().validate(cfg)


@pytest.mark.parametrize(
    "options",
    [
        with_path("extract.container", "table#orders"),
        good_options(login_detect={"selector": "form#login"}),
        good_options(wait_for="table#orders", timeout_s=1),
        good_options(timeout_s=300, fingerprint_depth=32, browser_channel="chrome"),
        good_options(timeout_s=1, fingerprint_depth=0, browser_channel="msedge"),
        good_options(profile_dir="~/profiles/sps"),
        good_options(url="http://127.0.0.1:8080/orders?page=1"),
    ],
)
def test_validate_accepts_valid_options(options: dict[str, Any]) -> None:
    WebCollector().validate(SourceConfig(id=SOURCE_ID, type="web", options=options))


@pytest.mark.parametrize(
    ("options", "fragment"),
    [
        (with_path("url", _DEL), "key 'url'"),
        (with_path("url", ""), "key 'url'"),
        (with_path("url", 5), "key 'url'"),
        (with_path("url", "ftp://example.invalid/x"), "key 'url'"),
        (with_path("url", "example.invalid/orders"), "key 'url'"),
        (with_path("url", "http://"), "key 'url'"),
        (with_path("url", "http://host:99999/"), "key 'url'"),
        (good_options(profile_dir="profiles/sps"), "key 'profile_dir'"),
        (good_options(profile_dir=""), "key 'profile_dir'"),
        (good_options(profile_dir=5), "key 'profile_dir'"),
        (good_options(login_detect="/login"), "key 'login_detect'"),
        (good_options(login_detect={}), "key 'login_detect'"),
        (good_options(login_detect={"url_contains": "/l", "selector": "form"}), "'login_detect'"),
        (good_options(login_detect={"url_contains": ""}), "key 'login_detect.url_contains'"),
        (good_options(login_detect={"selector": 3}), "key 'login_detect.selector'"),
        (good_options(login_detect={"url_contain": "/x"}), "key 'login_detect.url_contain'"),
        (with_path("extract", _DEL), "key 'extract'"),
        (with_path("extract", "table"), "key 'extract'"),
        (with_path("extract.rows", _DEL), "key 'extract.rows'"),
        (with_path("extract.rows", "  "), "key 'extract.rows'"),
        (with_path("extract.rows", 3), "key 'extract.rows'"),
        (with_path("extract.key", _DEL), "key 'extract.key'"),
        (with_path("extract.key", "nope"), "key 'extract.key'"),
        (with_path("extract.key", 3), "key 'extract.key'"),
        (with_path("extract.fields", _DEL), "key 'extract.fields'"),
        (with_path("extract.fields", {}), "key 'extract.fields'"),
        (with_path("extract.fields", ["po"]), "key 'extract.fields'"),
        (with_path("extract.fields", {"po": ""}), "key 'extract.fields.po'"),
        (with_path("extract.fields", {"po": 5}), "key 'extract.fields.po'"),
        (with_path("extract.fields", {"po": "@href"}), "key 'extract.fields.po'"),
        (with_path("extract.fields", {"x" * 65: "td"}), "key 'extract.fields'"),
        (with_path("extract.fields", {"a\nb": "td"}), "key 'extract.fields'"),
        (with_path("extract.fields", {"": "td"}), "key 'extract.fields'"),
        (with_path("extract.fields", {5: "td"}), "key 'extract.fields'"),
        (with_path("extract.container", ""), "key 'extract.container'"),
        (with_path("extract.container", 7), "key 'extract.container'"),
        (with_path("extract.foo", 1), "key 'extract.foo'"),
        (good_options(wait_for=""), "key 'wait_for'"),
        (good_options(wait_for=5), "key 'wait_for'"),
        (good_options(timeout_s=0), "key 'timeout_s'"),
        (good_options(timeout_s=301), "key 'timeout_s'"),
        (good_options(timeout_s=True), "key 'timeout_s'"),
        (good_options(timeout_s="30"), "key 'timeout_s'"),
        (good_options(timeout_s=1.5), "key 'timeout_s'"),
        (good_options(browser_channel="firefox"), "key 'browser_channel'"),
        (good_options(browser_channel=None), "key 'browser_channel'"),
        (good_options(fingerprint_depth=-1), "key 'fingerprint_depth'"),
        (good_options(fingerprint_depth=33), "key 'fingerprint_depth'"),
        (good_options(fingerprint_depth=False), "key 'fingerprint_depth'"),
        (good_options(fingerprint_depth="8"), "key 'fingerprint_depth'"),
        (good_options(frobnicate=1), "key 'frobnicate'"),
    ],
)
def test_validate_rejects_bad_options(options: dict[str, Any], fragment: str) -> None:
    cfg = SourceConfig(id=SOURCE_ID, type="web", options=options)
    with pytest.raises(ConfigError) as info:
        WebCollector().validate(cfg)
    assert f"source '{SOURCE_ID}'" in str(info.value)
    assert fragment in str(info.value)


def test_a_relative_profile_dir_is_rejected_and_an_absolute_one_kept(tmp_path: Path) -> None:
    absolute = tmp_path / "profile"
    opts = _parse_options(make_cfg(profile_dir=str(absolute)))
    assert opts.profile_dir == absolute
    assert _parse_options(make_cfg()).profile_dir is None  # default: resolved at collect time


def test_collect_reports_invalid_options_as_a_source_error(store: Store) -> None:
    result = run(store, make_cfg(timeout_s=0))
    assert result.error is not None and "key 'timeout_s'" in result.error
    assert kinds(store) == [KIND_SOURCE_ERROR]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("td:nth-child(1)", ("td:nth-child(1)", None)),
        ("a.detail@href", ("a.detail", "href")),
        ("  td.x @data-po-id ", ("td.x", "data-po-id")),
        ("a@xlink:href", ("a", "xlink:href")),
        ('a[href*="me@example.com"]', ('a[href*="me@example.com"]', None)),
        ("a[title='@']", ("a[title='@']", None)),
        (r".mail\@x", (r".mail\@x", None)),
        ("a@b@title", ("a@b", "title")),
    ],
)
def test_split_field_selector(raw: str, expected: tuple[str, str | None]) -> None:
    assert split_field_selector(raw) == expected


def test_default_profile_dir_is_created_under_since_home() -> None:
    cfg = make_cfg()
    path = _profile_dir(cfg, _parse_options(cfg))
    assert path == since_home() / "profiles" / SOURCE_ID
    assert path.is_dir()
    assert _profile_dir(cfg, _parse_options(cfg)) == path  # existing dir: fine


def test_explicit_profile_dir_is_created_if_missing(tmp_path: Path) -> None:
    wanted = tmp_path / "deep" / "profile"
    cfg = make_cfg(profile_dir=str(wanted))
    assert _profile_dir(cfg, _parse_options(cfg)) == wanted
    assert wanted.is_dir()


# -- fingerprint hashing / interpreting a page snapshot (no browser) ------------------------------


def test_fingerprint_is_the_sha256_of_the_sorted_unique_paths() -> None:
    expected = hashlib.sha256(b"a\nb.c\nb.c>d").hexdigest()
    assert fingerprint_of(["b.c>d", "a", "b.c", "a"]) == expected
    assert fingerprint_of(["a", "b.c", "b.c>d"]) == expected
    assert fingerprint_of([]) == hashlib.sha256(b"").hexdigest()
    assert fingerprint_of(["a", "b"]) != fingerprint_of(["a", "c"])


def raw_result(**changes: Any) -> dict[str, Any]:
    """What the page function returns for EXTRACT (fields: po, status, link)."""
    raw: dict[str, Any] = {
        "invalid": None,
        "container_found": None,
        "row_count": 2,
        "rows": [["4500124", "Open", "/b"], ["4500123", "Shipped", ""]],
        "field_matched": [True, True, True],
        "paths": ["div.shell", "div.shell>table.grid"],
    }
    raw.update(changes)
    return raw


def interpret(raw: Any, **option_changes: Any) -> CollectOutput:
    return _interpret(replace(_parse_options(make_cfg()), **option_changes), raw)


def test_interpret_builds_records_sorted_by_key_and_a_fingerprint() -> None:
    out = interpret(raw_result())
    assert [r.key for r in out.records] == ["4500123", "4500124"]
    assert out.records[0].fields == {"po": "4500123", "status": "Shipped", "link": ""}
    assert out.broken == []
    assert out.fingerprint == fingerprint_of(["div.shell", "div.shell>table.grid"])


def test_interpret_skips_rows_with_an_empty_key() -> None:
    rows = [["", "Open", ""], ["  ", "x", ""], ["4500123", "Open", ""]]
    out = interpret(raw_result(row_count=3, rows=rows))
    assert [r.key for r in out.records] == ["4500123"]
    assert out.broken == []  # the selectors work; the rows are just not records


def test_fingerprint_depth_zero_means_no_fingerprint() -> None:
    assert interpret(raw_result(paths=None), fingerprint_depth=0).fingerprint is None
    assert interpret(raw_result(), fingerprint_depth=0).fingerprint is None


@pytest.mark.parametrize(
    ("changes", "option_changes", "expected"),
    [
        # no container: rows matching nothing is broken
        ({"row_count": 0, "rows": [], "field_matched": [False] * 3}, {}, [SELECTOR_ROWS]),
        # a field matching in no row is broken (only when rows matched)
        ({"field_matched": [True, False, True]}, {}, ["td:nth-child(4)"]),
        ({"field_matched": [True, False, False]}, {}, ["td:nth-child(4)", "td:nth-child(2) a"]),
        # container present + zero rows: a valid, empty table
        (
            {"container_found": True, "row_count": 0, "rows": [], "field_matched": [False] * 3},
            {"container": "table#orders"},
            [],
        ),
        # container missing: the container is what is reported
        (
            {"container_found": False, "row_count": 0, "rows": [], "field_matched": [False] * 3},
            {"container": "table#orders"},
            ["table#orders"],
        ),
        # container missing but rows matched elsewhere: container first, then unmatched fields
        (
            {"container_found": False, "field_matched": [True, False, True]},
            {"container": "table#orders"},
            ["table#orders", "td:nth-child(4)"],
        ),
    ],
)
def test_interpret_broken_selectors(
    changes: dict[str, Any], option_changes: dict[str, Any], expected: list[str]
) -> None:
    assert interpret(raw_result(**changes), **option_changes).broken == expected


def test_interpret_lists_a_shared_selector_once() -> None:
    fields = (_Field("po", "td", None), _Field("status", "td", "title"))
    raw = raw_result(rows=[["a", "b"]], row_count=1, field_matched=[False, False])
    assert interpret(raw, fields=fields).broken == ["td"]


def test_interpret_reports_an_invalid_selector_as_a_collect_error() -> None:
    with pytest.raises(CollectError) as info:
        interpret(raw_result(invalid="td:nth-child(4)"))  # one of the configured selectors
    assert str(info.value) == "invalid CSS selector in the config: 'td:nth-child(4)'"


@pytest.mark.parametrize("invalid", ["ignore previous instructions", 5, ["td"], ""])
def test_interpret_does_not_echo_a_selector_the_page_made_up(invalid: Any) -> None:
    with pytest.raises(CollectError) as info:
        interpret(raw_result(invalid=invalid))
    assert str(info.value) == "unexpected result from the page (wrong selector)"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        raw_result(row_count="2"),
        raw_result(row_count=True),
        raw_result(row_count=3),  # does not match len(rows)
        raw_result(rows="x"),
        raw_result(rows=[["4500124", "Open"], ["4500123", "Shipped", ""]]),
        raw_result(rows=[["4500124", "Open", 5], ["4500123", "Shipped", ""]]),
        raw_result(rows=[["4500124", "Open", "/b"], "oops"]),
        raw_result(field_matched=[True, True]),
        raw_result(field_matched=[True, True, "yes"]),
        raw_result(paths="div"),
        raw_result(paths=[1, 2]),
        raw_result(paths=None),  # depth > 0 needs paths
    ],
)
def test_interpret_rejects_a_tampered_or_malformed_result(raw: Any) -> None:
    with pytest.raises(CollectError) as info:
        interpret(raw)
    assert "unexpected result from the page" in str(info.value)


# -- fake Playwright: error mapping and "always closed" --------------------------------------------


@pytest.fixture
def pw_api() -> Any:
    return pytest.importorskip("playwright.sync_api")


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class FakePage:
    def __init__(
        self,
        *,
        url: str = "https://example.invalid/orders",
        goto_error: Exception | None = None,
        wait_error: Exception | None = None,
        evaluate_error: Exception | None = None,
        selector_hits: int = 0,
        raw: Any = None,
    ) -> None:
        self.url = url
        self.goto_error = goto_error
        self.wait_error = wait_error
        self.evaluate_error = evaluate_error
        self.selector_hits = selector_hits
        self.raw = raw if raw is not None else raw_result()
        self.calls: list[str] = []

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.calls.append(f"goto {wait_until} {timeout}")
        if self.goto_error:
            raise self.goto_error

    def wait_for_selector(self, selector: str, state: str, timeout: int) -> None:
        self.calls.append(f"wait_for {selector} {state} {timeout}")
        if self.wait_error:
            raise self.wait_error

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.selector_hits)

    def evaluate(self, js: str, arg: Any) -> Any:
        self.calls.append("evaluate")
        if self.evaluate_error:
            raise self.evaluate_error
        return self.raw


class FakeContext:
    def __init__(self, page: FakePage, close_error: Exception | None = None) -> None:
        self.pages = [page]
        self.closed = 0
        self.close_error = close_error

    def close(self) -> None:
        self.closed += 1
        if self.close_error:
            raise self.close_error


class FakePlaywright:
    def __init__(self, context: FakeContext, launch_error: Exception | None = None) -> None:
        self.context = context
        self.launch_error = launch_error
        self.launched: dict[str, Any] = {}
        self.stopped = 0
        self.chromium = self

    def launch_persistent_context(self, profile: str, **kwargs: Any) -> FakeContext:
        self.launched = {"profile": profile, **kwargs}
        if self.launch_error:
            raise self.launch_error
        return self.context

    def stop(self) -> None:
        self.stopped += 1


def install_fake(
    monkeypatch: pytest.MonkeyPatch,
    api: Any,
    page: FakePage | None = None,
    *,
    launch_error: Exception | None = None,
    close_error: Exception | None = None,
) -> FakePlaywright:
    rig = FakePlaywright(FakeContext(page or FakePage(), close_error), launch_error)

    class Manager:
        def start(self) -> FakePlaywright:
            return rig

    monkeypatch.setattr(api, "sync_playwright", lambda: Manager())
    return rig


def replace_options(cfg: SourceConfig, **changes: Any) -> SourceConfig:
    options = {k: v for k, v in cfg.options.items() if k not in changes}
    options.update({k: v for k, v in changes.items() if v is not None})
    return replace(cfg, options=options)


def test_fake_success_launches_headless_and_closes_everything(
    monkeypatch: pytest.MonkeyPatch, pw_api: Any, store: Store
) -> None:
    rig = install_fake(monkeypatch, pw_api)
    cfg = make_cfg(browser_channel="msedge", wait_for="table#orders", timeout_s=7)
    result = run(store, cfg)
    assert result.error is None
    assert kinds(store) == [KIND_BASELINE]
    assert rig.launched == {
        "profile": str(since_home() / "profiles" / SOURCE_ID),
        "headless": True,
        "channel": "msedge",
    }
    assert rig.context.pages[0].calls == [
        "goto load 7000",
        "wait_for table#orders attached 7000",
        "evaluate",
    ]
    assert (rig.context.closed, rig.stopped) == (1, 1)


def test_fake_without_a_channel_uses_the_bundled_chromium(
    monkeypatch: pytest.MonkeyPatch, pw_api: Any
) -> None:
    rig = install_fake(monkeypatch, pw_api)
    WebCollector().collect(replace_options(make_cfg(), browser_channel=None))
    assert rig.launched["channel"] is None


def test_a_failing_context_close_does_not_mask_the_result(
    monkeypatch: pytest.MonkeyPatch, pw_api: Any
) -> None:
    rig = install_fake(monkeypatch, pw_api, close_error=RuntimeError("browser already gone"))
    out = WebCollector().collect(make_cfg())
    assert [r.key for r in out.records] == ["4500123", "4500124"]
    assert (rig.context.closed, rig.stopped) == (1, 1)


def failing_cases(api: Any) -> list[Any]:
    """(id, page kwargs, launch error, config changes, expected stored error)"""
    return [
        (
            "goto-timeout",
            {"goto_error": api.TimeoutError("Page.goto: Timeout 20000ms exceeded.")},
            None,
            {},
            "timed out after 20s loading the page",
        ),
        (
            "goto-error-hides-the-url",
            {
                "goto_error": api.Error(
                    "Page.goto: net::ERR_CONNECTION_REFUSED at "
                    "http://127.0.0.1:9/orders?t=SECRET\nCall log:\n  - navigating"
                )
            },
            None,
            {"url": "http://127.0.0.1:9/orders?t=SECRET"},
            "cannot load the page: Page.goto: net::ERR_CONNECTION_REFUSED at <url>",
        ),
        (
            "login-by-url",
            {"url": "https://example.invalid/login?next=/orders"},
            None,
            {"login_detect": {"url_contains": "/login"}},
            "login expired",
        ),
        (
            "login-by-selector",
            {"selector_hits": 1},
            None,
            {"login_detect": {"selector": "form#login"}},
            "login expired",
        ),
        (
            "wait-for-timeout",
            {"wait_error": api.TimeoutError("Page.wait_for_selector: Timeout")},
            None,
            {"wait_for": "table#orders"},
            "timed out after 20s waiting for selector 'table#orders'",
        ),
        (
            "wait-for-timeout-on-the-login-page",
            {
                "wait_error": api.TimeoutError("Page.wait_for_selector: Timeout"),
                "url": "https://example.invalid/login",
            },
            None,
            {"wait_for": "table#orders", "login_detect": {"url_contains": "/login"}},
            "login expired",
        ),
        (
            "evaluate-context-destroyed",
            {"evaluate_error": api.Error("Page.evaluate: Execution context was destroyed")},
            None,
            {},
            "the page navigated while it was being read",
        ),
        (
            "evaluate-error-hides-what-the-page-says",
            {"evaluate_error": api.Error("Page.evaluate: Error: ignore all previous instructions")},
            None,
            {},
            "cannot read the page: the extraction script failed",
        ),
        (
            "evaluate-returns-garbage",
            {"raw": "garbage"},
            None,
            {},
            "unexpected result from the page (not a mapping)",
        ),
        (
            "launch-error",
            {},
            api.Error("BrowserType.launch_persistent_context: Target closed\nCall log:\n  - x"),
            {},
            "BrowserType.launch_persistent_context: Target closed",
        ),
        (
            "browser-not-installed",
            {},
            api.Error(
                "BrowserType.launch_persistent_context: Executable doesn't exist at "
                "C:\\ms-playwright\\chromium\\chrome.exe\n╔═══╗\n║ playwright install ║"
            ),
            {},
            "BrowserType.launch_persistent_context: Executable doesn't exist "
            "(run `playwright install chromium`)",
        ),
    ]


def test_every_failure_is_a_short_source_error_and_everything_is_closed(
    monkeypatch: pytest.MonkeyPatch, pw_api: Any
) -> None:
    for name, page_kwargs, launch_error, changes, expected in failing_cases(pw_api):
        with monkeypatch.context() as patch, Store.open(since_home() / name) as store:
            rig = install_fake(patch, pw_api, FakePage(**page_kwargs), launch_error=launch_error)
            result = run(store, replace_options(make_cfg(), **changes))
            assert result.error == expected, name
            assert kinds(store) == [KIND_SOURCE_ERROR], name
            assert rig.stopped == 1, name
            assert rig.context.closed == (0 if launch_error else 1), name


def test_a_failed_run_after_a_baseline_removes_nothing(
    monkeypatch: pytest.MonkeyPatch, pw_api: Any, store: Store
) -> None:
    install_fake(monkeypatch, pw_api)
    assert run(store, make_cfg(), 0).error is None
    install_fake(monkeypatch, pw_api, FakePage(goto_error=pw_api.TimeoutError("t")))
    assert run(store, make_cfg(), 1).error == "timed out after 20s loading the page"
    assert kinds(store) == [KIND_BASELINE, KIND_SOURCE_ERROR]
    assert len(store.get_snapshot(SOURCE_ID)) == 2


# -- since login (D19): fake Playwright ----------------------------------------------------------


def login_cfg(**options: Any) -> SourceConfig:
    return make_cfg(browser_channel="msedge", timeout_s=7, **options)


def test_login_opens_the_collectors_profile_headed_and_waits_for_the_close() -> None:
    api = FakeApi()
    opened: list[str] = []
    login(login_cfg(), on_open=lambda: opened.append("open"), api=api)

    cfg = login_cfg()
    assert api.launched == {
        "profile": str(_profile_dir(cfg, _parse_options(cfg))),  # the very profile collect uses
        "headless": False,
        "channel": "msedge",
    }
    assert api.launched["profile"] == str(since_home() / "profiles" / SOURCE_ID)
    assert Path(api.launched["profile"]).is_dir()
    # The url is opened and nothing else is done to the page (the fake page has no other method);
    # then the browser is shut down.
    assert api.calls == [
        "launch",
        "goto https://example.invalid/orders commit 7000",
        "wait_for_event close 1000",
        "context.close",
        "stop",
    ]
    assert opened == ["open"]


def test_login_uses_an_explicit_profile_dir_and_the_default_browser(tmp_path: Path) -> None:
    api = FakeApi()
    profile = tmp_path / "my-profile"
    cfg = replace_options(login_cfg(profile_dir=str(profile)), browser_channel=None)
    login(cfg, api=api)
    assert api.launched == {"profile": str(profile), "headless": False, "channel": None}
    assert profile.is_dir()


def test_login_keeps_waiting_until_the_user_closes_the_browser() -> None:
    api = FakeApi(["timeout", "timeout", "close"])
    login(login_cfg(), api=api)
    assert api.calls.count("wait_for_event close 1000") == 3
    assert api.context is not None and api.context.closed == 1


def test_login_ends_when_the_last_window_is_gone_even_if_the_browser_stays_up() -> None:
    api = FakeApi(["timeout", "no-pages"])  # macOS: closing the last window keeps the process
    login(login_cfg(), api=api)
    assert api.calls.count("wait_for_event close 1000") == 2
    assert api.context is not None and api.context.closed == 1 and api.stopped == 1


def test_login_returns_when_the_browser_was_already_closed_or_ctrl_c_is_pressed() -> None:
    for script in (["gone"], ["interrupt"]):
        api = FakeApi(script)
        login(login_cfg(), api=api)
        assert api.context is not None and api.context.closed == 1 and api.stopped == 1


def test_login_survives_a_page_that_does_not_load() -> None:
    # The window stays usable (the human can type the address); no error, still waits.
    api = FakeApi(goto_error=FakeError("net::ERR_NAME_NOT_RESOLVED"))
    login(login_cfg(), api=api)
    assert "wait_for_event close 1000" in api.calls and api.stopped == 1


@pytest.mark.parametrize(
    "message",
    [
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
        "Call log:\n  - [pid=11392] <process did exit: exitCode=21, signal=null>",
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
        "Browser logs:\nOpening in existing browser session.",
        "Failed to create a ProcessSingleton for your profile directory",
    ],
)
def test_login_reports_a_locked_profile(message: str) -> None:
    api = FakeApi(launch_error=FakeError(message))
    with pytest.raises(LoginError) as info:
        login(login_cfg(), api=api)
    assert str(info.value) == PROFILE_IN_USE
    assert PROFILE_IN_USE == "profile in use (the daemon may be collecting); try again in a minute"
    assert api.stopped == 1


def test_login_reports_other_launch_failures_briefly() -> None:
    message = (
        "BrowserType.launch_persistent_context: Executable doesn't exist at C:\\pw\\chrome.exe\n"
        "Call log:\n  - lots\n  - of\n  - noise"
    )
    with pytest.raises(LoginError) as info:
        login(login_cfg(), api=FakeApi(launch_error=FakeError(message)))
    assert str(info.value) == (
        "BrowserType.launch_persistent_context: Executable doesn't exist "
        "(run `playwright install chromium`)"
    )
    url = "https://example.invalid/orders?token=SECRET"
    error = FakeError(f"Missing X server. {url}")
    with pytest.raises(LoginError) as info:
        login(login_cfg(url=url), api=FakeApi(launch_error=error))
    assert "SECRET" not in str(info.value) and "<url>" in str(info.value)


def test_login_without_playwright_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Any:
        raise CollectError("Playwright is not installed: install since[web]")

    monkeypatch.setattr("since.sources.web._import_playwright", missing)
    with pytest.raises(LoginError, match=r"since\[web\]"):
        login(login_cfg())


def test_login_rejects_an_invalid_web_config_before_launching() -> None:
    api = FakeApi()
    with pytest.raises(ConfigError, match="extract"):
        login(replace(login_cfg(), options={"url": "https://example.invalid/"}), api=api)
    assert api.calls == []


# -- real browser --------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_ok() -> None:
    """Skip browser tests when no browser can be launched (checked once per session)."""
    api = pytest.importorskip("playwright.sync_api")
    pw = None
    try:
        pw = api.sync_playwright().start()
        pw.chromium.launch(headless=True, channel=CHANNEL).close()
    except Exception as exc:
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        reason = (
            "no browser can be launched (run `playwright install chromium`, or set "
            f"SINCE_TEST_BROWSER_CHANNEL=msedge): {first[:150]}"
        )
        if os.environ.get("CI"):  # CI installs Chromium: a browser that will not start is a failure
            pytest.fail(reason)
        pytest.skip(reason)
    finally:
        if pw is not None:
            with contextlib.suppress(Exception):
                pw.stop()


class Site:
    """Static pages from a tmp dir, served on 127.0.0.1. ``/orders`` serves ``orders.html`` (or a
    redirect to ``/login.html`` while ``logged_in`` is false); ``/slow`` answers only once
    ``release`` is set."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.logged_in = True
        self.release = threading.Event()
        site = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=str(root), **kwargs)

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def end_headers(self) -> None:
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/orders" and not site.logged_in:
                    self.send_response(302)
                    self.send_header("Location", "/login.html")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/orders":
                    self.path = "/orders.html"
                elif path == "/slow":
                    site.release.wait(30)
                    self.path = "/orders.html"
                super().do_GET()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str = "/orders") -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def write(self, name: str, html: str) -> None:
        (self.root / name).write_text(html, encoding="utf-8")

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def site(browser_ok: None, tmp_path: Path) -> Iterator[Site]:
    root = tmp_path / "www"
    root.mkdir()
    s = Site(root)
    s.write("login.html", '<html><body><form id="login"><input name="user"></form></body></html>')
    try:
        yield s
    finally:
        s.close()


ROWS = [
    ("4500123", "Widget", "Open"),
    ("4500124", "Gadget", "Open"),
    ("4500125", "Gizmo", "Shipped"),
]


def orders_html(
    rows: list[tuple[str, str, str]],
    *,
    extra_rows: str = "",
    wrapper: str = "shell",
    table_class: str = "grid wide",
    body_attr: str = "",
    head_extra: str = "",
    banner: str = "",
    leaf: str = "em",
    table_id: str = "orders",
) -> str:
    """A portal page. Row ``(po, item, status)``: ``item`` is a link text, or no link if empty.
    Rows alternate ``odd`` / ``even`` classes like a striped table."""
    body = ""
    for i, (po, item, status) in enumerate(rows):
        link = f'<a href="/po/{po}">{item}</a>' if item else "-"
        stripe = "odd" if i % 2 == 0 else "even"
        body += (
            f'<tr class="{stripe}"><td>{po}</td><td>{link}</td><td>10</td><td>{status}</td></tr>\n'
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Portal</title>{head_extra}</head>
<body{body_attr}>
<div id="app" class="{wrapper}">
{banner}<h1 class="title">Orders</h1>
<div class="l1"><div class="l2"><div class="l3"><div class="l4">
<{leaf} class="leaf">x</{leaf}>
</div></div></div></div>
<table id="{table_id}" class="{table_class}">
<thead><tr><th>PO</th><th>Item</th><th>Qty</th><th>Status</th></tr></thead>
<tbody>
{body}{extra_rows}</tbody>
</table>
</div>
</body></html>
"""


def test_baseline_extracts_rows_fields_and_attributes(site: Site, store: Store) -> None:
    site.write(
        "orders.html",
        orders_html(
            [
                ("4500123", "Widget", "Open"),
                ("4500124", "", "  Partially\n   shipped "),  # no link; whitespace collapsed
            ],
            extra_rows='<tr class="total"><td></td><td>Total</td><td>20</td><td></td></tr>',
        ),
    )
    cfg = make_cfg(site.url(), login_detect={"selector": "form#login"}, wait_for="table#orders")
    result = run(store, cfg)
    assert result.error is None
    [baseline] = events(store)
    assert baseline.kind == KIND_BASELINE
    assert baseline.detail == {"record_count": 2}  # the total row has no key: skipped
    snapshot = store.get_snapshot(SOURCE_ID)
    assert {k: r.fields for k, r in snapshot.items()} == {
        "4500123": {"po": "4500123", "status": "Open", "link": "/po/4500123"},
        "4500124": {"po": "4500124", "status": "Partially shipped", "link": ""},
    }
    state = store.get_source_state(SOURCE_ID)
    assert state is not None and state.baselined and state.record_count == 2
    assert state.fingerprint is not None and len(state.fingerprint) == 64
    assert state.broken == []
    assert (since_home() / "profiles" / SOURCE_ID).is_dir()  # the default profile dir


def test_table_lifecycle_modified_added_removed_then_emptied(site: Site, store: Store) -> None:
    cfg = make_cfg(
        site.url(),
        login_detect={"url_contains": "/login"},
        extract=extract_with(container="table#orders"),
        wait_for="table#orders tbody",  # attached, not visible: an emptied tbody has no size
        timeout_s=5,
    )
    site.write("orders.html", orders_html(ROWS))
    assert run(store, cfg, 0).error is None
    fingerprint = store.get_source_state(SOURCE_ID).fingerprint  # type: ignore[union-attr]

    # status change, one row gone, two rows added: 3 -> 4 rows, so the zebra classes change too
    site.write(
        "orders.html",
        orders_html(
            [
                ("4500123", "Widget", "Cancelled"),
                ("4500125", "Gizmo", "Shipped"),
                ("4500126", "Doohickey", "Open"),
                ("4500127", "Thing", "Open"),
            ]
        ),
    )
    assert run(store, cfg, 1).error is None
    tail = events(store)[1:]
    assert sorted((e.kind, e.record_key) for e in tail) == [
        (KIND_ADDED, "4500126"),
        (KIND_ADDED, "4500127"),
        (KIND_MODIFIED, "4500123"),
        (KIND_REMOVED, "4500124"),
    ]
    [modified] = [e for e in tail if e.kind == KIND_MODIFIED]
    [change] = modified.field_changes
    assert (change.field, change.old, change.new) == ("status", "Open", "Cancelled")
    state = store.get_source_state(SOURCE_ID)
    assert state is not None and state.record_count == 4 and not state.in_error
    assert state.fingerprint == fingerprint  # only row count/text changed: same structure

    # the container is still there and no row matches: a valid empty table, not a broken page
    site.write("orders.html", orders_html([]))
    assert run(store, cfg, 2).error is None
    assert keyed(store, KIND_REMOVED) == {"4500124", "4500123", "4500125", "4500126", "4500127"}
    state = store.get_source_state(SOURCE_ID)
    assert state is not None and state.record_count == 0 and not state.in_error
    assert state.fingerprint == fingerprint
    assert KIND_SCHEMA_CHANGED not in kinds(store)
    assert KIND_SOURCE_ERROR not in kinds(store)


def test_login_expired_by_redirect_to_a_login_url(site: Site, store: Store) -> None:
    site.write("orders.html", orders_html(ROWS))
    # wait_for is set on purpose: the login page lacks it, and that must still read "login expired"
    cfg = make_cfg(site.url(), login_detect={"url_contains": "/login"}, wait_for="table#orders")
    assert run(store, cfg, 0).error is None
    site.logged_in = False
    result = run(store, cfg, 1)
    assert result.error == "login expired"
    assert kinds(store) == [KIND_BASELINE, KIND_SOURCE_ERROR]  # nothing removed
    assert len(store.get_snapshot(SOURCE_ID)) == 3
    state = store.get_source_state(SOURCE_ID)
    assert state is not None and state.in_error and state.last_error == "login expired"


def test_login_expired_by_a_login_form_in_the_page(site: Site, store: Store) -> None:
    site.write("orders.html", (site.root / "login.html").read_text(encoding="utf-8"))
    cfg = make_cfg(site.url("/orders.html"), login_detect={"selector": "form#login"})
    result = run(store, cfg)
    assert result.error == "login expired"
    assert kinds(store) == [KIND_SOURCE_ERROR]


def test_rows_selector_matching_nothing_is_schema_changed_and_removes_nothing(
    site: Site, store: Store
) -> None:
    site.write("orders.html", orders_html(ROWS))
    cfg = make_cfg(site.url())
    assert run(store, cfg, 0).error is None
    site.write("orders.html", orders_html(ROWS, table_id="orders-v2", wrapper="shell v2"))
    result = run(store, cfg, 1)
    assert result.error is not None and "match 0 elements" in result.error
    assert kinds(store) == [KIND_BASELINE, KIND_SCHEMA_CHANGED]
    assert events(store)[1].detail == {"selectors": [SELECTOR_ROWS]}
    state = store.get_source_state(SOURCE_ID)
    assert state is not None and state.in_error and state.record_count == 3
    assert state.broken == [SELECTOR_ROWS]
    assert len(store.get_snapshot(SOURCE_ID)) == 3


def test_layout_only_change_is_schema_changed_without_selectors_then_diffed(
    site: Site, store: Store
) -> None:
    site.write("orders.html", orders_html(ROWS))
    cfg = make_cfg(site.url())
    assert run(store, cfg, 0).error is None
    rows = [("4500123", "Widget", "Cancelled"), *ROWS[1:]]
    site.write(
        "orders.html",
        orders_html(rows, wrapper="shell redesigned", banner='<div class="promo">hi</div>'),
    )
    assert run(store, cfg, 1).error is None
    assert kinds(store) == [KIND_BASELINE, KIND_SCHEMA_CHANGED, KIND_MODIFIED]
    assert events(store)[1].detail == {"selectors": []}
    state = store.get_source_state(SOURCE_ID)
    assert state is not None and not state.in_error and state.broken == []


def test_a_field_selector_matching_nowhere_fails_the_first_run(site: Site, store: Store) -> None:
    site.write(
        "orders.html",
        '<table id="orders"><tbody><tr><td>1</td><td><a href="/x">a</a></td><td>10</td></tr>'
        "</tbody></table>",
    )
    result = run(store, make_cfg(site.url()))  # there is no 4th column
    assert result.error == 'extractor selector(s) match 0 elements: "td:nth-child(4)"'
    assert kinds(store) == [KIND_SOURCE_ERROR]


@pytest.mark.parametrize(
    ("path", "changes", "expected"),
    [
        ("/slow", {}, "timed out after 1s loading the page"),
        (
            "/orders.html",
            {"wait_for": "#never-there"},
            "timed out after 1s waiting for selector '#never-there'",
        ),
    ],
)
def test_timeouts_are_source_errors(
    site: Site, store: Store, path: str, changes: dict[str, Any], expected: str
) -> None:
    site.write("orders.html", orders_html(ROWS))
    result = run(store, make_cfg(site.url(path), timeout_s=1, **changes))
    assert result.error == expected
    assert kinds(store) == [KIND_SOURCE_ERROR]


# -- the page function in a real browser: many page variants, one browser launch -----------------


class Browser:
    """One browser launch for many page variants: writes ``orders.html`` and reads it with the
    real ``_read_page`` (navigation, login checks, the extraction function)."""

    def __init__(self, api: Any, page: Any, site: Site, base: _Options) -> None:
        self.api, self.page, self.site, self.base = api, page, site, base

    def snap(self, html: str, **changes: Any) -> CollectOutput:
        self.site.write("orders.html", html)
        opts = replace(self.base, **changes)
        return _interpret(opts, _read_page(self.api, self.page, opts))


@pytest.fixture
def browser(site: Site, tmp_path: Path) -> Iterator[Browser]:
    api = pytest.importorskip("playwright.sync_api")
    base = _parse_options(make_cfg(site.url("/orders.html")))
    pw = api.sync_playwright().start()
    context = None
    try:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, channel=CHANNEL
        )
        yield Browser(api, context.pages[0], site, base)
    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                context.close()
        with contextlib.suppress(Exception):
            pw.stop()


def test_fingerprint_ignores_data_and_reacts_to_structure(browser: Browser) -> None:
    base = browser.snap(orders_html(ROWS)).fingerprint
    assert base is not None and len(base) == 64
    same = [
        # more rows, other text, striping classes come and go (rows are skipped entirely)
        orders_html([*ROWS, ("4500126", "Thing", "Open"), ("4500127", "Other", "Shipped")]),
        orders_html([]),
        orders_html([("1", "x", "y")]),
        # script/style/noscript/template/comments/text never count
        orders_html(
            ROWS,
            head_extra="<style>a{color:red}</style><script>var x = 1</script>"
            "<template><div class='t'></div></template>",
            banner="<script>1</script><noscript><p class='ns'>js off</p></noscript>"
            "<template><div class='t'></div></template>"
            "<style>b{}</style><!-- note -->some loose text\n",
        ),
        # class order does not matter, the body is not part of a path
        orders_html(ROWS, table_class="wide grid"),
        orders_html(ROWS, body_attr=' class="dark"'),
        # anything inside a row is data
        orders_html([("4500123", '<span class="new"><b>Widget</b></span>', "Open")]),
    ]
    for i, html in enumerate(same):
        assert browser.snap(html).fingerprint == base, f"variant {i} should be structure-neutral"

    different = [
        orders_html(ROWS, banner='<div class="banner">maintenance</div>'),
        orders_html(ROWS, wrapper="shell redesigned"),
        orders_html(ROWS, table_class="grid"),
        orders_html(ROWS, leaf="strong"),
        orders_html(ROWS).replace("</tbody>", "</tbody><tfoot><tr><td>sum</td></tr></tfoot>"),
    ]
    seen = {base}
    for i, html in enumerate(different):
        fingerprint = browser.snap(html).fingerprint
        assert fingerprint not in seen, f"variant {i} should change the fingerprint"
        seen.add(fingerprint)


def test_fingerprint_depth_limits_and_disables(browser: Browser) -> None:
    # em.leaf sits 6 levels below body (div#app 1, div.l1 2 ... div.l4 5, em 6)
    em, strong = orders_html(ROWS), orders_html(ROWS, leaf="strong")
    assert (
        browser.snap(em, fingerprint_depth=5).fingerprint
        == browser.snap(strong, fingerprint_depth=5).fingerprint
    )
    assert (
        browser.snap(em, fingerprint_depth=6).fingerprint
        != browser.snap(strong, fingerprint_depth=6).fingerprint
    )
    assert (
        browser.snap(em, fingerprint_depth=1).fingerprint
        != browser.snap(orders_html(ROWS, wrapper="other"), fingerprint_depth=1).fingerprint
    )
    assert browser.snap(em, fingerprint_depth=0).fingerprint is None
    # what the page function collects hashes like fingerprint_of (body is not part of a path)
    assert browser.snap(em, fingerprint_depth=2).fingerprint == fingerprint_of(
        ["div.shell", "div.shell>h1.title", "div.shell>div.l1", "div.shell>table.grid.wide"]
    )


def test_extraction_reads_text_attributes_and_reports_broken_selectors(browser: Browser) -> None:
    def fields(key: str, **selectors: str) -> tuple[_Field, ...]:
        extract = extract_with(fields=selectors, key=key)
        return _parse_options(make_cfg(extract=extract)).fields

    # text: innerText (hidden text excluded), NBSP and newlines collapsed; attributes raw
    html = orders_html(
        [
            ("4500123", "Widget", "\n  Partially&nbsp;&nbsp;shipped\t\n"),
            ("4500124", "", 'A<span style="display:none">SECRET</span>B'),
        ]
    )
    out = browser.snap(html)
    assert [r.fields for r in out.records] == [
        {"po": "4500123", "status": "Partially shipped", "link": "/po/4500123"},
        {"po": "4500124", "status": "AB", "link": ""},
    ]
    assert out.broken == []

    # a field named __proto__ is just a field
    proto = fields("__proto__", __proto__="td:nth-child(1)", status="td:nth-child(4)")
    out = browser.snap(html, fields=proto, key="__proto__")
    assert out.records[0].fields == {"__proto__": "4500123", "status": "Partially shipped"}

    # selectors: shared broken selector listed once; only when rows matched
    two_dead = fields("po", po="td:nth-child(1)", a="td.nope", b="td.nope@title", c="td.nope2")
    assert browser.snap(html, fields=two_dead).broken == ["td.nope", "td.nope2"]
    assert browser.snap(orders_html([])).broken == [SELECTOR_ROWS]

    # container: present + no rows = valid; missing = the container is reported
    container = "table#orders"
    empty = browser.snap(orders_html([]), container=container)
    assert (empty.records, empty.broken) == ([], [])
    assert browser.snap(orders_html([], table_id="v2"), container=container).broken == [container]

    # a key selector that never matches: every row is skipped, and the field is reported
    dead_key = browser.snap(html, fields=fields("po", po="td.nope", status="td:nth-child(4)"))
    assert dead_key.records == [] and dead_key.broken == ["td.nope"]

    # an invalid CSS selector is a collect error naming it
    with pytest.raises(CollectError) as info:
        browser.snap(html, fields=fields("po", po="td:nth-child(", status="td"))
    assert "td:nth-child(" in str(info.value)
    with pytest.raises(CollectError):
        browser.snap(html, rows="table[")


# -- since login: real browser -------------------------------------------------------------------


class _Delegate:
    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _LoginContext(_Delegate):
    """The real context, closed from inside the wait like a user closing the window would."""

    def __init__(self, real: Any) -> None:
        super().__init__(real)
        self.polls = 0
        self.url_when_waiting = ""

    def wait_for_event(self, event: str, timeout: int) -> Any:
        self.polls += 1
        if self.polls == 1:
            page = self._real.pages[0]
            self.url_when_waiting = page.url
            page.once("console", lambda _msg: self._real.close())
            page.evaluate("() => setTimeout(() => console.log('bye'), 200)")
        elif self.polls > 20:  # never hang the test run
            self._real.close()
        return self._real.wait_for_event(event, timeout=timeout)


class _LoginChromium(_Delegate):
    def __init__(self, real: Any, launches: list[dict[str, Any]], contexts: list[Any]) -> None:
        super().__init__(real)
        self._launches = launches
        self._contexts = contexts

    def launch_persistent_context(self, profile: str, **kwargs: Any) -> Any:
        self._launches.append({"profile": profile, **kwargs})
        # A CI machine has no display to open a window on: run the real browser headless.
        context = _LoginContext(
            self._real.launch_persistent_context(profile, **{**kwargs, "headless": True})
        )
        self._contexts.append(context)
        return context


class _LoginPlaywright(_Delegate):
    def __init__(self, real: Any, launches: list[dict[str, Any]], contexts: list[Any]) -> None:
        super().__init__(real)
        self.chromium = _LoginChromium(real.chromium, launches, contexts)


class _LoginApi(_Delegate):
    """``playwright.sync_api`` with the launches recorded (and forced headless)."""

    def __init__(self, real: Any) -> None:
        super().__init__(real)
        self.launches: list[dict[str, Any]] = []
        self.contexts: list[Any] = []

    def sync_playwright(self) -> Any:
        manager = self._real.sync_playwright()
        api = self

        class Manager:
            def start(self) -> Any:
                return _LoginPlaywright(manager.start(), api.launches, api.contexts)

        return Manager()


def test_login_with_a_real_browser_opens_the_page_and_returns_when_it_is_closed(
    site: Site,
) -> None:
    site.write("orders.html", orders_html(ROWS))
    api = _LoginApi(pytest.importorskip("playwright.sync_api"))
    opened: list[str] = []

    login(make_cfg(url=site.url()), on_open=lambda: opened.append("open"), api=api)

    profile = since_home() / "profiles" / SOURCE_ID
    assert api.launches == [{"profile": str(profile), "headless": False, "channel": CHANNEL}]
    assert opened == ["open"]
    assert profile.is_dir() and any(profile.iterdir())  # the browser really used it
    (context,) = api.contexts
    assert context.polls >= 1
    assert context.url_when_waiting.startswith(site.url())
