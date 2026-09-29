"""``changedetection`` source tests: a fake changedetection.io API on 127.0.0.1, driven through
``run_collection``. Nothing here touches the internet."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from since.collect import CollectResult, register_sources, run_collection
from since.config import Config, ConfigError, SourceConfig
from since.diff import text_change_stats
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
)
from since.service import Service
from since.sources import CollectError, title_fields_for
from since.sources import changedetection as cdmod
from since.sources.changedetection import ChangedetectionCollector
from since.store import Store

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)
ENV = "SINCE_TEST_CD_KEY"
KEY = "k3y-ZGV0ZWN0aW9u-0123456789abcdef"
SID = "suppliers"

W1 = "0f7c1a52-3b8e-4d6a-9c11-aaaaaaaaaaa1"
W2 = "0f7c1a52-3b8e-4d6a-9c11-aaaaaaaaaaa2"
W3 = "0f7c1a52-3b8e-4d6a-9c11-aaaaaaaaaaa3"

CHANGED_1 = 1790000000  # 2026-09-21T14:13:20Z
CHANGED_1_ISO = "2026-09-21T14:13:20Z"
CHANGED_2 = 1790003600  # one hour later
CHANGED_2_ISO = "2026-09-21T15:13:20Z"

TEXT_V1 = "".join(f"Widget {n}: {10 + n} EUR\n" for n in range(20))  # > 200 chars
TEXT_V2 = TEXT_V1.replace("Widget 3: 13 EUR", "Widget 3: 15 EUR")
assert len(TEXT_V1) > 200 and TEXT_V1 != TEXT_V2


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


# -- the fake API --------------------------------------------------------------------------------


@dataclass
class Seen:
    """One request the fake received."""

    raw_path: str
    path: str
    query: dict[str, list[str]]
    api_key: str | None


@dataclass
class FakeApi:
    """A minimal changedetection.io API: ``GET /api/v1/watch[?tag=]`` and
    ``GET /api/v1/watch/<uuid>/history/latest`` with the same shapes and status codes as the real
    one (403 for a wrong key, 404 for a watch without history, tag filter by tag *name*, case
    insensitive). ``watches`` / ``texts`` are mutable between collections."""

    api_key: str | None = KEY
    watches: dict[str, dict[str, Any]] = field(default_factory=dict)
    tag_names: dict[str, list[str]] = field(default_factory=dict)
    texts: dict[str, bytes] = field(default_factory=dict)
    fail: dict[str, int] = field(default_factory=dict)  # path (or "*") -> forced status
    list_body: bytes | None = None  # raw body for the list endpoint
    redirect_to: str | None = None
    hold: threading.Event | None = None  # requests block until it is set (timeouts)
    requests: list[Seen] = field(default_factory=list)
    port: int = 0
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- lifecycle
    def start(self, port: int = 0) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                api._handle(self)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # clients that time out or disconnect are part of some tests

        self._server = Server(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self.hold is not None:
            self.hold.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- data
    def add(
        self,
        uuid: str,
        title: str | None,
        url: str = "https://example.invalid/page",
        *,
        last_changed: Any = CHANGED_1,
        last_error: Any = False,
        text: str | bytes | None = None,
        tags: tuple[str, ...] = (),
        omit: tuple[str, ...] = (),
    ) -> None:
        entry = {
            "last_changed": last_changed,
            "last_checked": 1790000100,
            "last_error": last_error,
            "link": url,
            "open_link": url,
            "page_title": "The <title> of the page",
            "tags": [f"tag-uuid-{t}" for t in tags],
            "title": title,
            "url": url,
            "viewed": True,
        }
        for name in omit:
            del entry[name]
        self.watches[uuid] = entry
        self.tag_names[uuid] = list(tags)
        if text is not None:
            self.texts[uuid] = text.encode("utf-8") if isinstance(text, str) else text

    def seen(self, path: str) -> list[Seen]:
        return [r for r in self.requests if r.path == path]

    @property
    def paths(self) -> list[str]:
        return [r.path for r in self.requests]

    # -- serving
    def _send(
        self,
        h: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        ctype: str = "application/json",
    ) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        seen = Seen(h.path, parts.path, parse_qs(parts.query), h.headers.get("x-api-key"))
        with self._lock:
            self.requests.append(seen)
        if self.hold is not None:
            self.hold.wait(10)
        if self.redirect_to is not None:
            h.send_response(302)
            h.send_header("Location", self.redirect_to)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        forced = self.fail.get(parts.path) or self.fail.get("*")
        if forced:
            self._send(h, forced, b'{"message": "forced failure"}')
            return
        if self.api_key is not None and seen.api_key != self.api_key:
            self._send(h, 403, b'{"message": "Invalid access - API key invalid."}')
            return
        if parts.path == "/api/v1/watch":
            self._send(h, 200, self._list_body(seen.query.get("tag", [""])[0]))
            return
        match = re.fullmatch(r"/api/v1/watch/([^/]+)/history/latest", parts.path)
        if match:
            text = self.texts.get(unquote(match.group(1)))
            if text is None:
                self._send(h, 404, b'"Watch found but no history exists"')
            else:
                self._send(h, 200, text, "text/plain; charset=utf-8")
            return
        self._send(h, 404, b"not found", "text/plain")

    def _list_body(self, tag: str) -> bytes:
        if self.list_body is not None:
            return self.list_body
        wanted = tag.lower()
        listing = {
            uuid: entry
            for uuid, entry in self.watches.items()
            if not wanted or wanted in (n.lower() for n in self.tag_names.get(uuid, []))
        }
        return json.dumps(listing).encode("utf-8")


@pytest.fixture(autouse=True)
def _direct_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake lives on 127.0.0.1: make sure no proxy of the dev box (env or Windows registry)
    is consulted."""
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def make_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], FakeApi]]:
    """Factory for started fake servers (all stopped at teardown); the key env var is set."""
    monkeypatch.setenv(ENV, KEY)
    started: list[FakeApi] = []

    def factory() -> FakeApi:
        api = FakeApi()
        api.start()
        started.append(api)
        return api

    yield factory
    for api in started:
        api.stop()


@pytest.fixture
def api(make_api: Callable[[], FakeApi]) -> FakeApi:
    """A fake with two watches: a supplier price list (long text) and carrier tariffs."""
    fake = make_api()
    fake.add(
        W1,
        "Supplier price list",
        "https://supplier.example/prices",
        text=TEXT_V1,
        tags=("Suppliers",),
    )
    fake.add(
        W2,
        "Carrier tariffs",
        "https://carrier.example/tariffs",
        last_changed=CHANGED_2,
        text="Tariff 2026",
        tags=("Logistics",),
    )
    return fake


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


def make_cfg(
    api: FakeApi,
    *,
    remove: tuple[str, ...] = (),
    title_fields: list[str] | None = None,
    **extra: Any,
) -> SourceConfig:
    options: dict[str, Any] = {"url": api.url, "api_key_env": ENV}
    options.update(extra)
    for name in remove:
        del options[name]
    return SourceConfig(
        id=SID,
        type="changedetection",
        priority="high",
        schedule_s=900,
        title_fields=title_fields,
        options=options,
    )


def run(store: Store, cfg: SourceConfig, minute: int = 0) -> CollectResult:
    return run_collection(store, cfg, ChangedetectionCollector(), at(minute))


def events(store: Store) -> list[Any]:
    return store.events_after(0, SID)


def kinds(store: Store) -> list[tuple[str, str | None]]:
    return [(e.kind, e.record_key) for e in events(store)]


def fields_of(store: Store, uuid: str) -> dict[str, Any]:
    return dict(store.get_snapshot(SID)[uuid].fields)


def assert_no_secret(store: Store, secret: str) -> None:
    """The secret appears in no stored event and not in the source's last error."""
    dump = json.dumps(
        [(e.detail, [c.to_dict() for c in e.field_changes]) for e in events(store)],
        ensure_ascii=False,
    )
    assert secret not in dump
    state = store.get_source_state(SID)
    assert state is not None and secret not in (state.last_error or "")


# -- module wiring / validate / labels ------------------------------------------------------------


def test_collector_identity_labels_and_default_title() -> None:
    collector = ChangedetectionCollector()
    cfg = SourceConfig(id=SID, type="changedetection", options={"url": "http://localhost:5000"})
    assert collector.type_name == "changedetection"
    assert collector.key_label(cfg) == ""
    assert collector.default_title_fields(cfg) == ["title"]
    assert title_fields_for(cfg, collector) == ["title"]


def test_validate_accepts_the_documented_config_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        ENV, raising=False
    )  # no env var and nothing listening: validate must not care
    collector = ChangedetectionCollector()
    for options in (
        {"url": "http://localhost:5000"},
        {"url": "https://cd.example.invalid/", "api_key_env": ENV, "tag": "Suppliers"},
        {"url": "http://192.168.1.5:5000", "fetch_text": False, "timeout_s": 300},
        {"url": "https://cd.example.invalid/sub/path", "timeout_s": 1},
    ):
        collector.validate(SourceConfig(id=SID, type="changedetection", options=options))


@pytest.mark.parametrize(
    ("options", "key"),
    [
        ({}, "url"),
        ({"url": ""}, "url"),
        ({"url": "  "}, "url"),
        ({"url": 5}, "url"),
        ({"url": None}, "url"),
        ({"url": "localhost:5000"}, "url"),
        ({"url": "ftp://cd.example.invalid"}, "url"),
        ({"url": "http://"}, "url"),
        ({"url": "file:///etc/passwd"}, "url"),
        ({"url": "http://user:secret@cd.example.invalid"}, "url"),
        ({"url": "http://cd.example.invalid?x=1"}, "url"),
        ({"url": "http://cd.example.invalid#frag"}, "url"),
        ({"url": "http://cd.example.invalid:99999"}, "url"),
        ({"url": "http://cd .example.invalid"}, "url"),
        ({"url": "http://cd.example.invalid", "api_key_env": ""}, "api_key_env"),
        ({"url": "http://cd.example.invalid", "api_key_env": "  "}, "api_key_env"),
        ({"url": "http://cd.example.invalid", "api_key_env": 5}, "api_key_env"),
        ({"url": "http://cd.example.invalid", "api_key_env": None}, "api_key_env"),
        ({"url": "http://cd.example.invalid", "tag": ""}, "tag"),
        ({"url": "http://cd.example.invalid", "tag": ["a"]}, "tag"),
        ({"url": "http://cd.example.invalid", "fetch_text": "yes"}, "fetch_text"),
        ({"url": "http://cd.example.invalid", "fetch_text": 1}, "fetch_text"),
        ({"url": "http://cd.example.invalid", "timeout_s": 0}, "timeout_s"),
        ({"url": "http://cd.example.invalid", "timeout_s": 301}, "timeout_s"),
        ({"url": "http://cd.example.invalid", "timeout_s": "30"}, "timeout_s"),
        ({"url": "http://cd.example.invalid", "timeout_s": 1.5}, "timeout_s"),
        ({"url": "http://cd.example.invalid", "timeout_s": True}, "timeout_s"),
        ({"url": "http://cd.example.invalid", "timeuot_s": 5}, "timeuot_s"),
        ({"url": "http://cd.example.invalid", "last_checked": True}, "last_checked"),
    ],
)
def test_validate_rejects_bad_options_naming_the_key(options: dict[str, Any], key: str) -> None:
    cfg = SourceConfig(id=SID, type="changedetection", options=options)
    with pytest.raises(ConfigError) as info:
        ChangedetectionCollector().validate(cfg)
    assert f"source '{SID}'" in str(info.value)
    assert f"key '{key}'" in str(info.value)
    assert "secret" not in str(info.value)  # a URL with credentials is never echoed


def test_register_sources_accepts_the_collector_by_injection(store: Store) -> None:
    cfg = SourceConfig(id=SID, type="changedetection", options={"url": "http://localhost:5000"})
    registration = register_sources(
        store, Config(sources=[cfg]), {"changedetection": ChangedetectionCollector()}
    )
    assert [c.id for c, _ in registration.collectable] == [SID]
    state = store.get_source_state(SID)
    assert state is not None and state.key_label == ""


def test_invalid_options_at_collect_time_are_a_source_error(api: FakeApi, store: Store) -> None:
    result = run(store, make_cfg(api, timeout_s=0))
    assert result.error is not None and "key 'timeout_s'" in result.error
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]
    assert api.requests == []


# -- baseline / diff through run_collection ------------------------------------------------------


def test_first_run_is_one_baseline_and_records_carry_the_documented_fields(
    api: FakeApi, store: Store
) -> None:
    result = run(store, make_cfg(api))

    assert result.error is None
    assert kinds(store) == [(KIND_BASELINE, None)]
    assert events(store)[0].detail == {"record_count": 2}
    snapshot = store.get_snapshot(SID)
    assert sorted(snapshot) == [W1, W2]
    # exactly these fields: no last_checked, viewed, page_title, tags, link ...
    assert fields_of(store, W1) == {
        "url": "https://supplier.example/prices",
        "title": "Supplier price list",
        "last_changed": CHANGED_1_ISO,
        "last_error": "",
        "text": TEXT_V1,
    }
    assert fields_of(store, W2)["last_changed"] == CHANGED_2_ISO
    assert fields_of(store, W2)["text"] == "Tariff 2026"
    state = store.get_source_state(SID)
    assert state is not None and state.record_count == 2 and state.baselined


def test_requests_are_the_documented_endpoints_with_the_api_key_header(
    api: FakeApi, store: Store
) -> None:
    run(store, make_cfg(api))
    assert api.paths == [
        "/api/v1/watch",
        f"/api/v1/watch/{W1}/history/latest",
        f"/api/v1/watch/{W2}/history/latest",
    ]
    assert api.requests[0].raw_path == "/api/v1/watch"  # no tag, no query string
    assert all(r.api_key == KEY for r in api.requests)


def test_unchanged_run_with_new_last_checked_produces_no_events(api: FakeApi, store: Store) -> None:
    run(store, make_cfg(api), 0)
    for entry in api.watches.values():
        entry["last_checked"] += 900  # changes on every check ...
        entry["viewed"] = False  # ... and when a human looks at it
        entry["page_title"] = "The <title> changed"  # not a field either

    result = run(store, make_cfg(api), 1)

    assert result.error is None and result.seqs == []
    assert kinds(store) == [(KIND_BASELINE, None)]


def test_added_modified_and_removed_watches_carry_titles(api: FakeApi, store: Store) -> None:
    run(store, make_cfg(api), 0)
    api.watches[W1]["title"] = "Supplier prices (renamed)"
    del api.watches[W2]
    api.add(W3, "Customs notices", "https://customs.example/n", text="Notice 1")

    result = run(store, make_cfg(api), 1)

    assert result.error is None
    assert kinds(store) == [
        (KIND_BASELINE, None),
        (KIND_MODIFIED, W1),
        (KIND_REMOVED, W2),
        (KIND_ADDED, W3),
    ]
    modified, removed, added = events(store)[1:]
    assert [(c.field, c.old, c.new) for c in modified.field_changes] == [
        ("title", "Supplier price list", "Supplier prices (renamed)")
    ]
    assert modified.detail["title"] == [["title", "Supplier prices (renamed)"]]
    assert removed.detail["title"] == [["title", "Carrier tariffs"]]  # last known title
    assert added.detail["title"] == [["title", "Customs notices"]]


def test_text_change_is_reported_as_char_counts_never_as_text(api: FakeApi, store: Store) -> None:
    run(store, make_cfg(api), 0)
    api.texts[W1] = TEXT_V2.encode("utf-8")

    result = run(store, make_cfg(api), 1)

    assert result.error is None
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_MODIFIED, W1)]
    (change,) = events(store)[1].field_changes
    added, removed = text_change_stats(TEXT_V1, TEXT_V2)
    assert (change.field, change.old, change.new) == ("text", None, None)
    assert (change.added_chars, change.removed_chars) == (added, removed)
    assert added > 0 and removed > 0
    assert fields_of(store, W1)["text"] == TEXT_V2  # the snapshot moved on


def test_a_watch_change_renders_in_the_digest_with_the_title_label(
    api: FakeApi, store: Store
) -> None:
    run(store, make_cfg(api), 0)
    api.texts[W1] = TEXT_V2.encode("utf-8")
    result = run(store, make_cfg(api), 1)
    (seq,) = result.seqs
    added, removed = text_change_stats(TEXT_V1, TEXT_V2)

    digest = Service(store, lambda: at(2)).since("default", 800)

    assert (
        f'  ~ "Supplier price list" text changed (+{added}/-{removed} chars)  since://evt/{seq}'
        in digest.splitlines()
    )
    assert "Widget" not in digest  # snapshot text never reaches a digest


def test_a_real_change_moves_last_changed_and_text_in_one_event(api: FakeApi, store: Store) -> None:
    run(store, make_cfg(api), 0)
    api.watches[W1]["last_changed"] = CHANGED_2
    api.texts[W1] = TEXT_V2.encode("utf-8")
    run(store, make_cfg(api), 1)
    added, removed = text_change_stats(TEXT_V1, TEXT_V2)

    digest = Service(store, lambda: at(2)).since("default", 800)

    assert (
        f'~ "Supplier price list" last_changed: "{CHANGED_1_ISO}" -> "{CHANGED_2_ISO}"; '
        f"text changed (+{added}/-{removed} chars)"
    ) in digest


def test_title_fields_option_overrides_the_default_title(api: FakeApi, store: Store) -> None:
    cfg = make_cfg(api, title_fields=["url"])
    run(store, cfg, 0)
    api.add(W3, "Customs notices", "https://customs.example/n")
    run(store, cfg, 1)
    assert events(store)[-1].detail["title"] == [["url", "https://customs.example/n"]]


def test_last_error_change_is_a_modification(api: FakeApi, store: Store) -> None:
    run(store, make_cfg(api), 0)
    api.watches[W2]["last_error"] = "Status Code 503 received"
    run(store, make_cfg(api), 1)
    (change,) = events(store)[-1].field_changes
    assert (change.field, change.old, change.new) == ("last_error", "", "Status Code 503 received")


# -- tag filter / fetch_text ---------------------------------------------------------------------


def test_tag_filters_the_list_and_only_matching_watches_are_fetched(
    api: FakeApi, store: Store
) -> None:
    result = run(store, make_cfg(api, tag="suppliers"))  # the API matches tag names ignoring case

    assert result.error is None
    assert sorted(store.get_snapshot(SID)) == [W1]
    (listing,) = api.seen("/api/v1/watch")
    assert listing.query == {"tag": ["suppliers"]}
    assert api.paths == ["/api/v1/watch", f"/api/v1/watch/{W1}/history/latest"]


def test_tag_is_url_encoded(api: FakeApi, store: Store) -> None:
    api.add(W3, "Odd tag", tags=("Price tracking & more",))
    run(store, make_cfg(api, tag="Price tracking & more"))

    assert sorted(store.get_snapshot(SID)) == [W3]
    (listing,) = api.seen("/api/v1/watch")
    assert listing.query == {"tag": ["Price tracking & more"]}
    assert listing.raw_path == "/api/v1/watch?tag=Price%20tracking%20%26%20more"


def test_fetch_text_false_skips_snapshots_and_has_no_text_field(api: FakeApi, store: Store) -> None:
    run(store, make_cfg(api, fetch_text=False))

    assert api.paths == ["/api/v1/watch"]
    assert "text" not in fields_of(store, W1)
    assert set(fields_of(store, W1)) == {"url", "title", "last_changed", "last_error"}


def test_no_api_key_env_sends_no_key_header(make_api: Callable[[], FakeApi], store: Store) -> None:
    fake = make_api()
    fake.api_key = None  # an instance without API key protection
    fake.add(W1, "Open watch", text="hello")

    result = run(store, make_cfg(fake, remove=("api_key_env",)))

    assert result.error is None
    assert fields_of(store, W1)["text"] == "hello"
    assert all(r.api_key is None for r in fake.requests)


def test_trailing_slash_in_the_url_is_harmless(api: FakeApi, store: Store) -> None:
    result = run(store, make_cfg(api, url=api.url + "/"))
    assert result.error is None
    assert api.paths[0] == "/api/v1/watch"


# -- missing history ------------------------------------------------------------------------------


def test_watch_without_history_has_no_text_and_is_not_an_error(api: FakeApi, store: Store) -> None:
    api.add(W3, "Fresh watch", last_changed=0)  # never fetched: no snapshot, last_changed 0

    result = run(store, make_cfg(api), 0)

    assert result.error is None
    assert (fields_of(store, W3)) == {
        "url": "https://example.invalid/page",
        "title": "Fresh watch",
        "last_changed": None,
        "last_error": "",
    }
    state = store.get_source_state(SID)
    assert state is not None and not state.in_error

    # the first snapshot appears later: a normal modification
    api.texts[W3] = b"first content"
    api.watches[W3]["last_changed"] = CHANGED_2
    assert run(store, make_cfg(api), 1).error is None
    changes = {c.field: (c.old, c.new) for c in events(store)[-1].field_changes}
    assert changes == {"last_changed": (None, CHANGED_2_ISO), "text": (None, "first content")}


# -- field mapping --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "title"),
    [
        ({"title": "Custom title"}, "Custom title"),
        ({"title": ""}, "The <title> of the page"),
        ({"title": "   "}, "The <title> of the page"),
        ({"title": None}, "The <title> of the page"),
        ({"title": None, "omit": ("title",)}, "The <title> of the page"),
        ({"title": "", "omit": ("page_title",)}, "https://example.invalid/page"),
    ],
)
def test_title_falls_back_to_page_title_then_url(kwargs: dict[str, Any], title: str) -> None:
    fake = FakeApi()
    fake.add(W1, **kwargs)
    (record,) = _collect_direct(fake, fake_start=True)
    assert record.fields["title"] == title


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"last_changed": CHANGED_1}, CHANGED_1_ISO),
        ({"last_changed": float(CHANGED_1)}, CHANGED_1_ISO),
        ({"last_changed": 0}, None),
        ({"last_changed": None}, None),
        ({"last_changed": -5}, None),
        ({"last_changed": "yesterday"}, None),
        ({"last_changed": 10**30}, None),
        ({"omit": ("last_changed",)}, None),
    ],
)
def test_last_changed_is_iso_utc_or_none(kwargs: dict[str, Any], expected: str | None) -> None:
    fake = FakeApi()
    fake.add(W1, "t", **kwargs)
    (record,) = _collect_direct(fake, fake_start=True)
    assert record.fields["last_changed"] == expected


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"last_error": False}, ""),
        ({"last_error": None}, ""),
        ({"last_error": ""}, ""),
        ({"last_error": "Status Code 403"}, "Status Code 403"),
        ({"omit": ("last_error",)}, ""),
    ],
)
def test_last_error_is_a_string(kwargs: dict[str, Any], expected: str) -> None:
    fake = FakeApi()
    fake.add(W1, "t", **kwargs)
    (record,) = _collect_direct(fake, fake_start=True)
    assert record.fields["last_error"] == expected


def _collect_direct(fake: FakeApi, *, fake_start: bool = False, **extra: Any) -> list[Any]:
    """Run ``ChangedetectionCollector.collect`` against ``fake`` (started here, stopped after)."""
    if fake_start:
        fake.api_key = None
        fake.start()
    try:
        options = {"url": fake.url, "fetch_text": False, **extra}
        result = ChangedetectionCollector().collect(
            SourceConfig(id=SID, type="changedetection", options=options)
        )
        assert isinstance(result, list)
        return result
    finally:
        if fake_start:
            fake.stop()


def test_collect_returns_records_sorted_by_key() -> None:
    fake = FakeApi()
    for uuid in (W3, W1, W2):
        fake.add(uuid, uuid)
    records = _collect_direct(fake, fake_start=True)
    assert [r.key for r in records] == [W1, W2, W3]


def test_empty_instance_is_a_valid_empty_baseline(api: FakeApi, store: Store) -> None:
    api.watches.clear()
    result = run(store, make_cfg(api))
    assert result.error is None
    assert events(store)[0].detail == {"record_count": 0}


def test_snapshot_text_is_decoded_as_utf8_and_bad_bytes_are_replaced(
    api: FakeApi, store: Store
) -> None:
    api.texts[W1] = "Preis: 12,50 € – gültig".encode()
    api.texts[W2] = b"ok \xff\xfe"
    run(store, make_cfg(api))
    assert fields_of(store, W1)["text"] == "Preis: 12,50 € – gültig"
    assert fields_of(store, W2)["text"] == "ok ��"


def test_snapshot_text_is_cut_at_the_size_cap(
    api: FakeApi, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cdmod, "MAX_TEXT_BYTES", 100)
    api.texts[W1] = ("x" * 250).encode()
    run(store, make_cfg(api))
    assert fields_of(store, W1)["text"] == "x" * 100


def test_a_list_response_over_the_cap_is_an_error(
    api: FakeApi, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cdmod, "MAX_LIST_BYTES", 50)
    result = run(store, make_cfg(api))
    assert result.error == "response from /api/v1/watch is too large"


def test_a_uuid_is_percent_encoded_in_the_snapshot_path(api: FakeApi, store: Store) -> None:
    api.add("a/b?c", "Odd id", text="odd text")
    run(store, make_cfg(api))
    assert "/api/v1/watch/a%2Fb%3Fc/history/latest" in api.paths
    assert fields_of(store, "a/b?c")["text"] == "odd text"


# -- failures: nothing is ever "removed" ----------------------------------------------------------


def _assert_failure_keeps_the_snapshot(store: Store, message: str) -> None:
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    assert events(store)[1].detail == {"error": message}
    assert sorted(store.get_snapshot(SID)) == [W1, W2]
    state = store.get_source_state(SID)
    assert state is not None and state.in_error and state.record_count == 2


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_api_key_is_reported_without_details(
    api: FakeApi, store: Store, status: int
) -> None:
    run(store, make_cfg(api), 0)
    api.fail["*"] = status

    result = run(store, make_cfg(api), 1)

    assert result.error == "API key rejected"
    _assert_failure_keeps_the_snapshot(store, "API key rejected")
    assert_no_secret(store, KEY)


def test_a_wrong_key_is_rejected_by_the_server_and_never_echoed(
    api: FakeApi, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = "wrong-key-should-not-leak-987654"
    monkeypatch.setenv(ENV, wrong)

    result = run(store, make_cfg(api))

    assert result.error == "API key rejected"
    assert [r.api_key for r in api.requests] == [wrong]
    assert_no_secret(store, wrong)
    assert_no_secret(store, KEY)


def test_a_key_rejected_on_the_snapshot_request_is_the_same_error(
    api: FakeApi, store: Store
) -> None:
    api.fail[f"/api/v1/watch/{W1}/history/latest"] = 401
    assert run(store, make_cfg(api)).error == "API key rejected"
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]  # first run: failure, no baseline


def test_connection_refused_is_a_source_error_never_removed_and_recovers(
    api: FakeApi, store: Store
) -> None:
    run(store, make_cfg(api), 0)
    port = api.port
    api.stop()

    result = run(store, make_cfg(api), 1)

    assert result.error is not None and result.error.startswith("connection failed: ")
    assert "\n" not in result.error
    _assert_failure_keeps_the_snapshot(store, result.error)
    assert_no_secret(store, KEY)

    api.start(port)
    assert run(store, make_cfg(api), 2).error is None
    assert kinds(store) == [
        (KIND_BASELINE, None),
        (KIND_SOURCE_ERROR, None),
        (KIND_SOURCE_RECOVERED, None),
    ]


@pytest.mark.parametrize("status", [404, 429, 500, 502])
def test_http_errors_report_status_and_path_only(api: FakeApi, store: Store, status: int) -> None:
    run(store, make_cfg(api, tag="suppliers"), 0)
    api.fail["/api/v1/watch"] = status

    result = run(store, make_cfg(api, tag="suppliers"), 1)

    message = f"HTTP {status} from /api/v1/watch"
    assert result.error == message  # no query string, no tag, no body
    assert sorted(store.get_snapshot(SID)) == [W1]
    assert [e.kind for e in events(store)] == [KIND_BASELINE, KIND_SOURCE_ERROR]
    assert_no_secret(store, KEY)


def test_http_error_on_a_snapshot_request_names_that_path(api: FakeApi, store: Store) -> None:
    api.fail[f"/api/v1/watch/{W2}/history/latest"] = 500
    result = run(store, make_cfg(api))
    assert result.error == f"HTTP 500 from /api/v1/watch/{W2}/history/latest"


def test_a_timeout_is_a_short_reason(api: FakeApi, store: Store) -> None:
    api.hold = threading.Event()
    result = run(store, make_cfg(api, timeout_s=1))
    assert result.error == "request to /api/v1/watch timed out after 1s"
    assert_no_secret(store, KEY)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<html>please log in</html>", "invalid JSON from /api/v1/watch"),
        (b"", "invalid JSON from /api/v1/watch"),
        (b"\xff\xfe\x00", "invalid JSON from /api/v1/watch"),
        (b"[]", "unexpected response from /api/v1/watch: expected a JSON object"),
        (b'"OK"', "unexpected response from /api/v1/watch: expected a JSON object"),
        (b'{"a": 5}', "unexpected response from /api/v1/watch: watch is not an object"),
    ],
)
def test_a_malformed_list_response_is_a_collect_error(
    api: FakeApi, store: Store, body: bytes, message: str
) -> None:
    api.list_body = body
    result = run(store, make_cfg(api))
    assert result.error == message
    assert "log in" not in message  # never echoes the body
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]


def test_redirects_are_not_followed_so_the_key_cannot_travel(
    make_api: Callable[[], FakeApi], store: Store
) -> None:
    elsewhere = make_api()
    elsewhere.add(W1, "Elsewhere")
    origin = make_api()
    origin.redirect_to = elsewhere.url + "/api/v1/watch"

    result = run(store, make_cfg(origin))

    assert result.error == "HTTP 302 from /api/v1/watch"
    assert elsewhere.requests == []  # the redirect target was never contacted
    assert_no_secret(store, KEY)


# -- the API key never appears in a message -------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_key_env_var_names_the_variable_only(
    api: FakeApi, store: Store, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ENV, value)
    result = run(store, make_cfg(api))
    assert result.error == f"environment variable {ENV} is not set (or is empty)"
    assert api.requests == []


def test_an_unset_key_env_var_names_the_variable_only(
    api: FakeApi, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV)
    result = run(store, make_cfg(api))
    assert result.error == f"environment variable {ENV} is not set (or is empty)"
    assert api.requests == []
    assert [e.kind for e in events(store)] == [KIND_SOURCE_ERROR]


@pytest.mark.parametrize("bad", [KEY + "\nX-Evil: 1", KEY + "é", KEY + "\x01"])
def test_an_unusable_key_value_is_refused_without_echoing_it(
    api: FakeApi, store: Store, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv(ENV, bad)
    result = run(store, make_cfg(api))
    assert result.error == f"environment variable {ENV} holds an invalid API key"
    assert KEY not in (result.error or "")
    assert api.requests == []


def test_any_unexpected_exception_is_scrubbed_of_the_key(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"header x-api-key: {KEY} was rejected")

    monkeypatch.setattr(cdmod._Api, "get", boom)
    with pytest.raises(CollectError) as info:
        ChangedetectionCollector().collect(
            SourceConfig(
                id=SID, type="changedetection", options={"url": api.url, "api_key_env": ENV}
            )
        )
    assert KEY not in str(info.value)
    assert str(info.value) == "RuntimeError: header x-api-key: *** was rejected"


def test_a_collect_error_carrying_the_key_is_scrubbed_too(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise CollectError(f"bad {KEY}")

    monkeypatch.setattr(cdmod._Api, "get", boom)
    with pytest.raises(CollectError) as info:
        ChangedetectionCollector().collect(
            SourceConfig(
                id=SID, type="changedetection", options={"url": api.url, "api_key_env": ENV}
            )
        )
    assert str(info.value) == "bad ***"


def test_scrub_replaces_every_occurrence_longest_secret_first() -> None:
    assert cdmod._scrub("a SECRET-123 b SECRET c", ["SECRET", "SECRET-123"]) == "a *** b *** c"
    assert cdmod._scrub("nothing here", []) == "nothing here"
    assert cdmod._scrub("keep", [""]) == "keep"
