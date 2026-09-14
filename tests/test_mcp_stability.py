import io
import json
import unittest
from unittest.mock import Mock, patch

from tools.caa_manual_mcp_server import McpServer


class McpStabilityTests(unittest.TestCase):
    def setUp(self):
        self.index = Mock()
        self.index.search.return_value = {"status": "ok", "candidate_groups": []}
        self.server = McpServer(self.index)

    def test_invalid_arguments_do_not_reach_index(self):
        cases = [
            ("caa_search", None), ("caa_search", []), ("caa_search", {}),
            ("caa_search", {"query": ""}), ("caa_search", {"query": "   "}),
            ("caa_search", {"query": 2}), ("caa_search", {"query": "Run", "limit": True}),
            ("caa_search", {"query": "Run", "limit": "8"}),
            ("caa_search", {"query": "Run", "limit": 31}),
            ("caa_search", {"query": "Run", "limt": 2}),
            ("caa_get_api", {"reference": "CATWidget", "include": "members"}),
            ("caa_get_api", {"reference": "CATWidget", "include": ["everything"]}),
            ("caa_get_api", {"reference": "CATWidget", "include": ["members", "members"]}),
            ("caa_get_api", {"reference": "CATWidget", "inherited": "false"}),
            ("caa_get_api", {"reference": "CATWidget", "member": "Run"}),
            ("caa_read_source", {"reference": "CATWidget", "offset": -1}),
            ("caa_read_source", {"reference": "CATWidget", "format": "html"}),
            ("caa_read_source", {"reference": "CATWidget", "max_chars": 40001}),
            ("caa_status", {"unknown": True}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                result = self.server.call_tool(name, args)
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["error_code"], "invalid_arguments")
                self.assertIn("next_action", result["structuredContent"])
        self.assertEqual(self.index.mock_calls, [])

    def test_valid_call_after_invalid_call(self):
        self.server.call_tool("caa_search", {"query": "Run", "limit": "2"})
        result = self.server.call_tool("caa_search", {"query": "Run", "limit": 2})
        self.assertEqual(result["structuredContent"]["status"], "ok")
        self.index.search.assert_called_once_with("Run", 2, "", "")

    def test_unknown_tool_is_actionable(self):
        result = self.server.call_tool("invented", {})["structuredContent"]
        self.assertEqual(result["error_code"], "unknown_tool")
        self.assertIn("tools/list", result["next_action"])

    def test_jsonrpc_invalid_envelopes(self):
        for value in (None, 1, [], {}, {"jsonrpc": "1.0", "method": "ping", "id": 1}):
            with self.subTest(value=value):
                self.assertEqual(self.server.handle(value)["error"]["code"], -32600)
        value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}
        self.assertEqual(self.server.handle(value)["error"]["code"], -32602)
        value["params"] = {"name": "caa_search", "arguments": None}
        self.assertTrue(self.server.handle(value)["result"]["isError"])

    def test_stdio_recovers_and_responds_to_batch(self):
        lines = ["{broken", "null", "[]", json.dumps([
            1, {"jsonrpc": "2.0", "id": 7, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"}]),
            json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping"})]
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(lines))), patch("sys.stdout", output):
            self.assertEqual(self.server.serve(), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["error"]["code"], -32600)
        self.assertEqual(responses[2]["error"]["code"], -32600)
        self.assertEqual(responses[3][0]["error"]["code"], -32600)
        self.assertEqual(responses[3][1]["id"], 7)
        self.assertEqual(len(responses[3]), 2)
        self.assertEqual(responses[4]["id"], 8)


if __name__ == "__main__":
    unittest.main()
