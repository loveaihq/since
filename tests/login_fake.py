"""A fake Playwright API for ``since login`` tests (no browser, no playwright package needed).

``FakeApi`` has what ``since.sources.web.login`` uses: ``sync_playwright()``, ``Error`` and
``TimeoutError``. ``wait_script`` says what each ``context.wait_for_event("close", ...)`` call does:
``"timeout"`` (nothing happened within the poll interval), ``"close"`` (the user closed the
browser), ``"gone"`` (the wait raises a Playwright error: the context was already closed),
``"interrupt"`` (Ctrl-C). ``"no-pages"`` is a timeout after which the last window has disappeared.
"""

from __future__ import annotations

from typing import Any


class FakeError(Exception):
    pass


class FakeTimeout(FakeError):
    pass


class FakePage:
    """Only ``goto`` and ``url``: a login must never touch the page beyond navigating to it."""

    def __init__(self, calls: list[str], goto_error: Exception | None) -> None:
        self.url = "about:blank"
        self._calls = calls
        self._goto_error = goto_error

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self._calls.append(f"goto {url} {wait_until} {timeout}")
        if self._goto_error is not None:
            raise self._goto_error


class FakeContext:
    def __init__(self, calls: list[str], wait_script: list[str], page: FakePage) -> None:
        self.pages: list[FakePage] = [page]
        self.closed = 0
        self._calls = calls
        self._script = list(wait_script)

    def new_page(self) -> FakePage:
        raise AssertionError("login must use the page the browser opened")

    def wait_for_event(self, event: str, timeout: int) -> None:
        self._calls.append(f"wait_for_event {event} {timeout}")
        step = self._script.pop(0) if self._script else "close"
        if step == "close":
            return
        if step == "gone":
            raise FakeError("Target page, context or browser has been closed")
        if step == "interrupt":
            raise KeyboardInterrupt
        if step == "no-pages":
            self.pages.clear()
        raise FakeTimeout(f"Timeout {timeout}ms exceeded")

    def close(self) -> None:
        self._calls.append("context.close")
        self.closed += 1


class FakePlaywright:
    def __init__(self, api: FakeApi) -> None:
        self.chromium = self
        self._api = api

    def launch_persistent_context(self, profile: str, **kwargs: Any) -> FakeContext:
        api = self._api
        api.calls.append("launch")
        api.launched = {"profile": profile, **kwargs}
        if api.launch_error is not None:
            raise api.launch_error
        api.context = FakeContext(api.calls, api.wait_script, FakePage(api.calls, api.goto_error))
        return api.context

    def stop(self) -> None:
        self._api.calls.append("stop")
        self._api.stopped += 1


class FakeApi:
    Error = FakeError
    TimeoutError = FakeTimeout

    def __init__(
        self,
        wait_script: list[str] | None = None,
        *,
        launch_error: Exception | None = None,
        goto_error: Exception | None = None,
    ) -> None:
        self.wait_script = wait_script or ["close"]
        self.launch_error = launch_error
        self.goto_error = goto_error
        self.calls: list[str] = []
        self.launched: dict[str, Any] = {}
        self.context: FakeContext | None = None
        self.stopped = 0

    def sync_playwright(self) -> FakeApi:
        return self

    def start(self) -> FakePlaywright:
        return FakePlaywright(self)
