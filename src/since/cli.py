"""Command-line entry point: ``since daemon|mcp|collect|login|digest|get|ack|status|ui``.

``digest`` / ``get`` / ``ack`` / ``status`` read only the database (no config file needed) and
print exactly what the MCP tools return. Exit codes: 0 ok; 1 collection failure, service error
text or daemon/database problem; 2 usage or config error. Nothing here calls an LLM.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from since import ui
from since.collect import register_sources, run_collection
from since.config import Config, ConfigError, SourceConfig, load_config
from since.daemon import Daemon, DaemonError
from since.sanitize import GET_CAP, q
from since.service import Service
from since.store import Store, StoreError

ERROR_PREFIX = "error: "  # how the service marks a failure in its returned text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="since",
        description="What changed since I last looked: ranked, token-budgeted digests for agents.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    p = sub.add_parser("daemon", help="run the collection scheduler")
    p.add_argument("--once", action="store_true", help="collect every source once, then exit")

    sub.add_parser("mcp", help="run the MCP server on stdio")

    p = sub.add_parser("collect", help="collect one source once (debug)")
    p.add_argument("source_id")

    p = sub.add_parser("login", help="log in to a web source once, by hand, in a browser window")
    p.add_argument("source_id")

    p = sub.add_parser("digest", help="print the digest an agent would get")
    p.add_argument("--agent", default="default")
    p.add_argument("--budget", type=int, default=800)
    p.add_argument("--source", default=None)

    p = sub.add_parser("get", help="drill into an event, record or batch handle")
    p.add_argument("handle")
    p.add_argument("--budget", type=int, default=1500)
    p.add_argument("--agent", default="default")

    p = sub.add_parser("ack", help="advance an agent's cursor")
    p.add_argument("cursor", type=int)
    p.add_argument("--agent", default="default")

    sub.add_parser("status", help="show source and daemon status")

    p = sub.add_parser("ui", help="serve the read-only audit page on localhost")
    p.add_argument("--port", type=int, default=ui.DEFAULT_PORT, help="0 picks a free port")
    return parser


def _force_utf8() -> None:
    """Windows consoles default to a legacy code page and crash on ``·`` and ``…``."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass  # e.g. a closed or detached stream; leave it as it is


def _now() -> datetime:
    return datetime.now(UTC)


def _fail(message: str, code: int) -> int:
    print(f"{ERROR_PREFIX}{message}", file=sys.stderr)
    return code


# -- daemon / mcp / collect ----------------------------------------------------------------------


def _cmd_daemon(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store.open()
    try:
        return Daemon(config, store, _now, time.sleep).run(once=args.once)
    except DaemonError as exc:
        return _fail(str(exc), 1)
    finally:
        store.close()


def _cmd_mcp(args: argparse.Namespace) -> int:
    from since.mcp_server import main as serve

    serve()
    return 0


def _unknown_source(config: Config, source_id: str) -> str | None:
    """An error message if ``source_id`` is not configured, else ``None``."""
    if any(cfg.id == source_id for cfg in config.sources):
        return None
    known = ", ".join(cfg.id for cfg in config.sources) or "none"
    return f"unknown source '{source_id}' (configured: {known})"


def _cmd_collect(args: argparse.Namespace) -> int:
    source_id: str = args.source_id
    config = load_config()
    unknown = _unknown_source(config, source_id)
    if unknown is not None:
        return _fail(unknown, 2)
    store = Store.open()
    try:
        registration = register_sources(store, config)
        found = next(((c, k) for c, k in registration.collectable if c.id == source_id), None)
        if found is None:  # its type has no collector yet
            reason = dict(registration.skipped).get(source_id, "cannot be collected")
            return _fail(f"source '{source_id}': {reason}", 2)
        result = run_collection(store, found[0], found[1], _now())
    finally:
        store.close()
    if result.superseded:
        print(f"{source_id}: superseded by a newer collection; nothing stored")
        return 0
    if result.error is not None:
        print(f"{source_id}: collection failed: {q(result.error, GET_CAP)}")
        return 1
    if result.seqs:
        print(f"{source_id}: {len(result.seqs)} events (seq {result.seqs[0]}-{result.seqs[-1]})")
    else:
        print(f"{source_id}: no changes")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    """D19: a headed browser on the web source's profile; the human logs in, then closes it."""
    source_id: str = args.source_id
    config = load_config()
    unknown = _unknown_source(config, source_id)
    if unknown is not None:
        return _fail(unknown, 2)
    cfg: SourceConfig = next(c for c in config.sources if c.id == source_id)
    if cfg.type != "web":
        return _fail(
            f"source '{source_id}' is a {cfg.type} source; login is only for web sources", 2
        )
    from since.sources import web  # lazy: Playwright is an optional extra

    def announce() -> None:
        print(f"Log in to {source_id} in the browser window, then close it.", flush=True)

    try:
        web.login(cfg, on_open=announce)
    except web.LoginError as exc:
        return _fail(str(exc), 1)
    return 0


# -- digest / get / ack / status -----------------------------------------------------------------


def _serve(run: Callable[..., str]) -> int:
    """Open the database, call the service, print its text; exit 1 if the text is an error."""
    store = Store.open()
    try:
        text = run(Service(store, _now))
    finally:
        store.close()
    print(text)
    return 1 if text.startswith(ERROR_PREFIX) else 0


def _cmd_digest(args: argparse.Namespace) -> int:
    return _serve(lambda s: s.since(args.agent, args.budget, args.source, via="cli"))


def _cmd_get(args: argparse.Namespace) -> int:
    return _serve(lambda s: s.get(args.handle, args.budget, args.agent, via="cli"))


def _cmd_ack(args: argparse.Namespace) -> int:
    return _serve(lambda s: s.ack(args.agent, args.cursor))


def _cmd_status(args: argparse.Namespace) -> int:
    return _serve(lambda s: s.status())


def _cmd_ui(args: argparse.Namespace) -> int:
    """D32: the read-only audit page; needs no config file, runs until Ctrl-C."""
    if not 0 <= args.port <= 65535:
        return _fail("--port must be between 0 and 65535", 2)
    try:
        return ui.serve(args.port)
    except ui.UiError as exc:
        return _fail(str(exc), 1)


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "daemon": _cmd_daemon,
    "mcp": _cmd_mcp,
    "collect": _cmd_collect,
    "login": _cmd_login,
    "digest": _cmd_digest,
    "get": _cmd_get,
    "ack": _cmd_ack,
    "status": _cmd_status,
    "ui": _cmd_ui,
}


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except ConfigError as exc:
        return _fail(str(exc), 2)
    except (StoreError, sqlite3.Error) as exc:
        return _fail(f"database: {exc}", 1)


if __name__ == "__main__":
    raise SystemExit(main())
