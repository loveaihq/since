"""Command-line entry point. Skeleton: every subcommand is a stub until T10."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"since {args.command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
