"""A fake changedetection.io API on 127.0.0.1 (ephemeral port), shared by the ``changedetection``
source tests and the M2 end-to-end test. Nothing here touches the internet."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

KEY = "k3y-ZGV0ZWN0aW9u-0123456789abcdef"  # default API key of a FakeApi
CHANGED_1 = 1790000000  # 2026-09-21T14:13:20Z: default ``last_changed`` of a watch


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
