"""What a human has to do about a login problem (D24 revised): the ``imap`` hint, end to end through
the runner and the digest. (The ``web`` and ``changedetection`` hints are tested with their
sources.)"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from imap_fake import FakeImapServer

from since.collect import run_collection
from since.config import SourceConfig
from since.model import KIND_SOURCE_ERROR
from since.service import Service
from since.sources import LoginRequired
from since.sources.imap import ImapCollector
from since.store import Store

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)
ENV = "SINCE_TEST_IMAP_PW"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeImapServer]:
    with FakeImapServer() as fake:
        monkeypatch.setenv(ENV, "not-the-password")
        yield fake


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


def make_cfg(server: FakeImapServer) -> SourceConfig:
    options = {
        "host": server.host,
        "port": server.port,
        "security": "none",
        "username": server.username,
        "password_env": ENV,
    }
    return SourceConfig(id="inbox", type="imap", priority="normal", schedule_s=900, options=options)


def test_a_refused_imap_login_says_where_the_app_password_lives(server: FakeImapServer) -> None:
    with pytest.raises(LoginRequired) as info:
        ImapCollector(now_fn=lambda: T0).collect(make_cfg(server))

    assert str(info.value) == f"login failed for {server.username}"
    assert info.value.hint == f"check the app password in {ENV}"


def test_the_imap_hint_reaches_the_event_and_the_digest(
    server: FakeImapServer, store: Store
) -> None:
    cfg = make_cfg(server)

    result = run_collection(store, cfg, ImapCollector(now_fn=lambda: T0), T0)

    assert result.error == f"login failed for {server.username}"
    [event] = [e for e in store.events_after(0) if e.kind == KIND_SOURCE_ERROR]
    assert event.detail == {
        "error": f"login failed for {server.username}",
        "hint": f"check the app password in {ENV}",
    }
    digest = Service(store, lambda: T0).since().splitlines()
    shown = f'"login failed for {server.username}"; needs a human: check the app password in {ENV}'
    assert f"  ! source_error: {shown}  since://evt/1" in digest
    assert "not-the-password" not in "\n".join(digest)
