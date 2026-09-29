"""A local portal page for the ``web`` source tests and the M2 end-to-end test: static pages
served from a tmp dir on 127.0.0.1 (``Site``) and a striped-table "orders" page generator
(``orders_html``). The ``site`` / ``browser_ok`` fixtures that use them live in ``conftest.py``."""

from __future__ import annotations

import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# The browser is Playwright's Chromium unless SINCE_TEST_BROWSER_CHANNEL names an installed one.
CHANNEL = os.environ.get("SINCE_TEST_BROWSER_CHANNEL") or None

LOGIN_HTML = '<html><body><form id="login"><input name="user"></form></body></html>'


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
