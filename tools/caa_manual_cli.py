#!/usr/bin/env python3
"""CLI query facade and explicit private source-cache builder."""

from __future__ import annotations

import argparse
import json
import sys

try:
    from .caa_manual_query import configured_index
except ImportError:
    from caa_manual_query import configured_index


def configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query the local CAA AI manual projection.")
    parser.add_argument("--db", help="Path to manual.sqlite. Overrides CAA_AI_MANUAL_DB.")
    parser.add_argument("--manual-root", help="Manual root. Overrides CAA_AI_MANUAL_ROOT.")
    parser.add_argument("--caadoc-root", help="CAADoc root. Overrides CAA_CAADOC_ROOT.")
    parser.add_argument("--env-file", help="Optional UTF-8 environment file.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="Show database and source status.")
    commands.add_parser("index-sources", help="Build ignored private full-text cache from local CAADoc.")
    source_search = commands.add_parser("search-source", help="Search the private source cache; does not build it automatically.")
    source_search.add_argument("query")
    source_search.add_argument("--limit", type=int, default=5)
    source_search.add_argument("--offset", type=int, default=0)

    catalog = commands.add_parser("catalog", help="Browse Layer -> Framework -> API nodes.")
    catalog.add_argument("--parent", default="catalog:root")
    catalog.add_argument("--depth", type=int, default=1, choices=(1, 2, 3))
    catalog.add_argument("--limit", type=int, default=200)
    catalog.add_argument("--offset", type=int, default=0)

    search = commands.add_parser("search", help="Search APIs with query-relative scores.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--layer", default="")
    search.add_argument("--framework", default="")

    get_api = commands.add_parser("get-api", help="Resolve an API and request optional detail groups.")
    get_api.add_argument("reference")
    get_api.add_argument("--member", default="", help="Exact member name filter.")
    get_api.add_argument("--inherited", action="store_true", help="Include declarations from documented base classes.")
    get_api.add_argument("--member-offset", type=int, default=0)
    get_api.add_argument("--member-limit", type=int, default=100)
    get_api.add_argument("--example-offset", type=int, default=0)
    get_api.add_argument("--example-limit", type=int, default=20)
    get_api.add_argument(
        "--include",
        action="append",
        choices=("members", "overloads", "evidence", "examples"),
        default=[],
    )

    read_source = commands.add_parser("read-source", help="Read official source at an API/member anchor.")
    read_source.add_argument("reference")
    read_source.add_argument("--anchor", default="")
    read_source.add_argument("--format", choices=("text", "raw_html"), default="text")
    read_source.add_argument("--max-chars", type=int, default=12000)
    read_source.add_argument("--offset", type=int, default=0)
    read_source.add_argument("--include-context", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    index = configured_index(args.db, args.manual_root, args.caadoc_root, args.env_file)
    try:
        if args.command == "status":
            result = index.status()
        elif args.command == "index-sources":
            try:
                from .caa_manual_source_search import build_source_cache
            except ImportError:
                from caa_manual_source_search import build_source_cache
            result = build_source_cache(index)
        elif args.command == "search-source":
            result = index.search_source(args.query, args.limit, args.offset)
        elif args.command == "catalog":
            result = index.catalog(args.parent, args.depth, args.limit, args.offset)
        elif args.command == "search":
            result = index.search(args.query, args.limit, args.layer, args.framework)
        elif args.command == "get-api":
            result = index.get_api(args.reference, args.include, args.member,
                                   args.member_offset, args.member_limit,
                                   args.example_offset, args.example_limit, args.inherited)
        elif args.command == "read-source":
            result = index.read_source(
                args.reference, args.anchor, args.format, args.max_chars, args.offset, args.include_context
            )
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except Exception as exc:
        print(json.dumps({
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
