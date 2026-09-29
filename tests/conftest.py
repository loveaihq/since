"""Shared fixtures. Every test runs with SINCE_HOME pointing at a throwaway directory."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from web_site import CHANNEL, LOGIN_HTML, Site


@pytest.fixture(autouse=True)
def since_home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SINCE_HOME at a tmp dir so no test can touch the real ``~/.since``.

    The directory is not created; the code under test creates it (like on first run)."""
    home = tmp_path / "since-home"
    monkeypatch.setenv("SINCE_HOME", str(home))
    return home


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


@pytest.fixture
def site(browser_ok: None, tmp_path: Path) -> Iterator[Site]:
    """A local portal (``web_site.Site``) with a ``login.html``; needs a launchable browser."""
    root = tmp_path / "www"
    root.mkdir()
    s = Site(root)
    s.write("login.html", LOGIN_HTML)
    try:
        yield s
    finally:
        s.close()
