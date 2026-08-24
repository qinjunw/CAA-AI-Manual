#!/usr/bin/env python3
"""Dependency-free stdio MCP facade for the CAA manual query API."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

try:
    from .caa_manual_query import CaaManualIndex, configured_index
except ImportError:
    from caa_manual_query import CaaManualIndex, configured_index


PROTOCOL_VERSION = "2024-11-05"


def configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def tool_defs() -> list[dict[str, Any]]:
    return [
        {
            "name": "caa_status",
            "description": (
                "Return database health, content mode, projection counts, source roots, and "
                "the advisory CATIA V5R21 CAADoc baseline metadata."
            ),
            "inputSchema": {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        },
        {
            "name": "caa_catalog",
            "description": (
                "Browse the logical Layer -> Framework -> API/function-family catalog. "
                "Use returned node_id values to continue unambiguously."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "parent": {
                        "type": "string",
                        "default": "catalog:root",
                        "description": "Catalog node_id, official English key, or configured Chinese key.",
                    },
                    "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "caa_search",
            "description": (
                "Search official API keys, configured Chinese keys, members, signatures, and "
                "index tags. match_score is query-relative lexical relevance, not factual confidence."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8},
                    "layer": {
                        "type": "string",
                        "description": "Optional exact layer filter, e.g. CAA-refman or Automation.",
                    },
                    "framework": {
                        "type": "string",
                        "description": "Optional exact official Framework filter.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "caa_get_api",
            "description": (
                "Resolve one logical API/function family and progressively include members, "
                "overloads, evidence, or examples. Ambiguous references return structured candidates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string",
                        "description": "API node_id, page_id, member_id, source_uri, exact official key, or Chinese key.",
                    },
                    "include": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["members", "overloads", "evidence", "examples"],
                        },
                        "uniqueItems": True,
                        "default": [],
                    },
                },
                "required": ["reference"],
                "additionalProperties": False,
            },
        },
        {
            "name": "caa_read_source",
            "description": (
                "Read the exact official source from the configured local CAADoc root, selected "
                "by a node/page/member/caadoc URI. Defaults to plain text near the anchor."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string",
                        "description": "API node_id, page_id, member_id, caadoc:// URI, or exact API key.",
                    },
                    "anchor": {
                        "type": "string",
                        "description": "Optional official HTML anchor; overrides a member's stored anchor.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "raw_html"],
                        "default": "text",
                    },
                    "max_chars": {
                        "type": "integer", "minimum": 100, "maximum": 40000, "default": 12000,
                    },
                },
                "required": ["reference"],
                "additionalProperties": False,
            },
        },
    ]


def text_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
    }


def error_result(message: str) -> dict[str, Any]:
    payload = {"status": "error", "error": message}
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
    }


class McpServer:
    def __init__(self, index: CaaManualIndex):
        self.index = index

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "caa_status":
                return text_result(self.index.status())
            if name == "caa_catalog":
                return text_result(self.index.catalog(
                    args.get("parent", "catalog:root"),
                    int(args.get("depth", 1)),
                    int(args.get("limit", 200)),
                ))
            if name == "caa_search":
                return text_result(self.index.search(
                    args["query"], int(args.get("limit", 8)),
                    args.get("layer", ""), args.get("framework", ""),
                ))
            if name == "caa_get_api":
                return text_result(self.index.get_api(args["reference"], args.get("include", [])))
            if name == "caa_read_source":
                return text_result(self.index.read_source(
                    args["reference"], args.get("anchor", ""), args.get("format", "text"),
                    int(args.get("max_chars", 12000)),
                ))
            return error_result(f"Unknown tool: {name}")
        except Exception as exc:
            return error_result(f"{type(exc).__name__}: {exc}")

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if method == "notifications/initialized":
            return None
        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "caa-ai-manual", "version": "0.2.0"},
                    },
                }
            if method == "ping":
                return {"jsonrpc": "2.0", "id": message_id, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": tool_defs()}}
            if method == "tools/call":
                result = self.call_tool(params.get("name", ""), params.get("arguments") or {})
                return {"jsonrpc": "2.0", "id": message_id, "result": result}
            return {
                "jsonrpc": "2.0", "id": message_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0", "id": message_id,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }

    def serve(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                messages = payload if isinstance(payload, list) else [payload]
                for message in messages:
                    response = self.handle(message)
                    if response is not None and message.get("id") is not None:
                        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                        sys.stdout.flush()
            except Exception as exc:
                error = {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse/server error: {exc}"},
                }
                sys.stdout.write(json.dumps(error, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        return 0


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="MCP stdio server for the CAA AI manual.")
    parser.add_argument("--db", help="Path to manual.sqlite. Overrides CAA_AI_MANUAL_DB.")
    parser.add_argument("--manual-root", help="Manual root. Overrides CAA_AI_MANUAL_ROOT.")
    parser.add_argument("--caadoc-root", help="CAAdoc root. Overrides CAA_CAADOC_ROOT.")
    parser.add_argument("--env-file", help="Optional UTF-8 environment file.")
    args = parser.parse_args(argv)
    index = configured_index(args.db, args.manual_root, args.caadoc_root, args.env_file)
    return McpServer(index).serve()


if __name__ == "__main__":
    raise SystemExit(main())
