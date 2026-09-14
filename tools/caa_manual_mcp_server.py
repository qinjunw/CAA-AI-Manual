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
            "name": "caa_search_source",
            "description": "Search English words in the optional private local source cache when API-key search is insufficient. Returns evidence URIs and excerpts, not verified answers. Requires CLI index-sources first.",
            "inputSchema": {
                "type": "object", "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                }, "required": ["query"], "additionalProperties": False,
            },
        },
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
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "caa_search",
            "description": (
                "Search official API keys, configured Chinese keys, members, signatures, and "
                "index tags. Supports Class::Member or Class Member (local documented inheritance) and curated task terms. "
                "match_score is query-relative lexical relevance, not factual confidence."
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
                "overloads, evidence, or examples. Use member to filter before requesting large member lists. "
                "Defaults to declared-only members; inherited=true adds documented bases. "
                "Zero indexed examples is not proof that no official example exists. "
                "For a known method use member='MethodName', include=['members']; unfiltered member lists can be large."
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
                    "member": {"type": "string", "description": "Exact member name filter; requires include members."},
                    "inherited": {"type": "boolean", "default": False},
                    "member_offset": {"type": "integer", "minimum": 0, "default": 0},
                    "member_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                    "example_offset": {"type": "integer", "minimum": 0, "default": 0},
                    "example_limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "required": ["reference"],
                "additionalProperties": False,
            },
        },
        {
            "name": "caa_read_source",
            "description": (
                "Read local official source by API, Class::Member, member ID, or caadoc URI#anchor. "
                "HTML is converted to text; code files are preserved. Follow next_offset for more. "
                "Use include_context for class-level constraints; raw_html for source markup. "
                "If an API or Class::Member is already known, call this directly without a preliminary search or get_api."
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
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "include_context": {"type": "boolean", "default": False},
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


def error_result(message: str, code: str = "tool_error", next_action: str = "Check the reference and local source configuration before retrying.") -> dict[str, Any]:
    payload = {"status": "error", "error": message, "error_code": code, "next_action": next_action}
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
    }


def validate_arguments(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    """Validate the JSON Schema subset used by tool_defs before any query runs."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool}
    if kind not in valid or not valid[kind]:
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: expected one of {schema['enum']}")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key}: required")
        for key, item in value.items():
            if key not in properties:
                raise ValueError(f"{path}: unknown property; accepted names: {', '.join(properties)}")
            validate_arguments(item, properties[key], f"{path}.{key}")
    elif kind == "array":
        for i, item in enumerate(value):
            validate_arguments(item, schema["items"], f"{path}[{i}]")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise ValueError(f"{path}: duplicate items are not allowed")
    elif kind == "string" and not value.strip() and (schema.get("minLength", 0) > 0 or path in ("arguments.reference", "arguments.parent")):
        raise ValueError(f"{path}: expected non-blank text")
    elif kind == "integer":
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path}: minimum is {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path}: maximum is {schema['maximum']}")


def rpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


class McpServer:
    def __init__(self, index: CaaManualIndex):
        self.index = index

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        definition = next((item for item in tool_defs() if item["name"] == name), None)
        if definition is None:
            return error_result("Unknown tool", "unknown_tool", "Use tools/list to select a supported tool name.")
        try:
            validate_arguments(args, definition["inputSchema"])
            if name == "caa_get_api" and args.get("member") and "members" not in args.get("include", []):
                raise ValueError("arguments.member: requires include=['members']")
        except ValueError as exc:
            return error_result(str(exc), "invalid_arguments", "Correct the named argument using this tool's inputSchema, then retry the same lookup.")
        try:
            if name == "caa_search_source":
                return text_result(self.index.search_source(args["query"], int(args.get("limit", 5)), int(args.get("offset", 0))))
            if name == "caa_status":
                return text_result(self.index.status())
            if name == "caa_catalog":
                return text_result(self.index.catalog(
                    args.get("parent", "catalog:root"),
                    int(args.get("depth", 1)),
                    int(args.get("limit", 200)),
                    int(args.get("offset", 0)),
                ))
            if name == "caa_search":
                return text_result(self.index.search(
                    args["query"], int(args.get("limit", 8)),
                    args.get("layer", ""), args.get("framework", ""),
                ))
            if name == "caa_get_api":
                return text_result(self.index.get_api(
                    args["reference"], args.get("include", []), args.get("member", ""),
                    int(args.get("member_offset", 0)), int(args.get("member_limit", 100)),
                    int(args.get("example_offset", 0)), int(args.get("example_limit", 20)),
                    args.get("inherited", False),
                ))
            if name == "caa_read_source":
                return text_result(self.index.read_source(
                    args["reference"], args.get("anchor", ""), args.get("format", "text"),
                    int(args.get("max_chars", 12000)),
                    int(args.get("offset", 0)), args.get("include_context", False),
                ))
            return error_result(f"Unknown tool: {name}")
        except Exception as exc:
            return error_result(f"{type(exc).__name__}: {exc}")

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or ("id" in message and message["id"] is not None and type(message["id"]) not in (str, int))):
            return rpc_error(None, -32600, "Expected a JSON-RPC 2.0 request object with a string method")
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(message_id, -32602, "params must be an object")
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
                        "serverInfo": {"name": "caa-ai-manual", "version": "0.3.1"},
                    },
                }
            if method == "ping":
                return {"jsonrpc": "2.0", "id": message_id, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": tool_defs()}}
            if method == "tools/call":
                result = self.call_tool(params.get("name", ""), params.get("arguments", {}))
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
            except ValueError:
                response = rpc_error(None, -32700, "Invalid JSON; send one JSON-RPC message per line")
            else:
                responses = []
                for message in payload if isinstance(payload, list) else [payload]:
                    item = self.handle(message)
                    invalid = item and item.get("error", {}).get("code") == -32600
                    if item is not None and (invalid or isinstance(message, dict) and "id" in message):
                        responses.append(item)
                response = (responses or None) if isinstance(payload, list) else (responses[0] if responses else None)
                if payload == []:
                    response = rpc_error(None, -32600, "An empty JSON-RPC batch is invalid")
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
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
