import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.caa_manual_query import CaaManualIndex


class TrackingCaaManualIndex(CaaManualIndex):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.open_connections = []

    def connect(self):
        connection = super().connect()
        self.open_connections.append(connection)
        return connection

    def close_connections(self):
        for connection in self.open_connections:
            connection.close()
        self.open_connections.clear()


class QueryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manual_root = self.root / "manual"
        self.caadoc_root = self.root / "caadoc"
        self.db_path = self.manual_root / "data" / "manual.sqlite"
        self.db_path.parent.mkdir(parents=True)
        self.caadoc_root.mkdir()
        self._write_source()
        self._write_database()
        self.index = TrackingCaaManualIndex(
            self.db_path,
            self.manual_root,
            self.caadoc_root,
        )

    def tearDown(self):
        self.index.close_connections()
        self.temp_dir.cleanup()

    def _write_source(self):
        relative_path = Path(
            "Doc/generated/refman/TestFramework/interface_CATWidget_1.htm"
        )
        source_path = self.caadoc_root / relative_path
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            """<html><body>
<a name="Run()"></a>
<h2>Run</h2>
<p>Runs the official widget operation without changing its documented meaning.</p>
<p>This sentence makes the selected member section long enough to test truncation.</p>
<a name="Stop()"></a>
<h2>Stop</h2>
<p>This neighboring member must not be returned for the Run anchor.</p>
</body></html>""",
            encoding="utf-8",
        )

    def _write_database(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE catalog_nodes (
              node_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
              node_type TEXT NOT NULL, english_key TEXT NOT NULL,
              name_en TEXT NOT NULL, name_zh TEXT NOT NULL,
              summary_zh TEXT NOT NULL, layer TEXT NOT NULL,
              framework TEXT NOT NULL, api_kind TEXT NOT NULL,
              sort_order INTEGER NOT NULL
            );
            CREATE TABLE api_pages (
              page_id TEXT PRIMARY KEY, api_node_id TEXT NOT NULL,
              page_role TEXT NOT NULL, page_template_kind TEXT NOT NULL,
              signature TEXT NOT NULL, summary_en TEXT NOT NULL,
              include_file TEXT NOT NULL, source_uri TEXT NOT NULL UNIQUE,
              source_available INTEGER NOT NULL, tags_json TEXT NOT NULL,
              raw_entity_ids_json TEXT NOT NULL,
              evidence_ids_json TEXT NOT NULL
            );
            CREATE TABLE api_members (
              member_id TEXT PRIMARY KEY, page_id TEXT NOT NULL,
              member_group_key TEXT NOT NULL, kind TEXT NOT NULL,
              name TEXT NOT NULL, signature TEXT NOT NULL,
              summary_en TEXT NOT NULL, anchor TEXT NOT NULL,
              raw_entity_id TEXT NOT NULL, evidence_ids_json TEXT NOT NULL
            );
            CREATE TABLE api_aliases (
              alias_id TEXT PRIMARY KEY, alias TEXT NOT NULL,
              normalized_alias TEXT NOT NULL, language TEXT NOT NULL,
              target_type TEXT NOT NULL, target_id TEXT NOT NULL,
              english_key TEXT NOT NULL, scope TEXT NOT NULL,
              summary_zh TEXT NOT NULL, basis TEXT NOT NULL
            );
            CREATE TABLE projection_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE evidence (
              id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
              anchor TEXT NOT NULL, quote_or_span TEXT NOT NULL,
              extraction_method TEXT NOT NULL, trust_level TEXT NOT NULL
            );
            CREATE TABLE relations (src_id TEXT, type TEXT, dst_id TEXT);
            CREATE TABLE chunks (
              id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
              heading_path_json TEXT NOT NULL, text TEXT NOT NULL,
              symbols_json TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO catalog_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "api:widget", "framework:test", "api", "CATWidget",
                    "CATWidget", "部件接口", "测试接口", "CAA-refman",
                    "TestFramework", "interface", 100,
                ),
                (
                    "api:shared:a", "framework:alpha", "api", "CATShared",
                    "CATShared", "", "", "CAA-refman", "Alpha", "class", 100,
                ),
                (
                    "api:shared:b", "framework:beta", "api", "CATShared",
                    "CATShared", "", "", "Automation", "Beta", "interface", 100,
                ),
                (
                    "api:function:mix", "framework:test", "function-family",
                    "CATMix", "CATMix", "", "", "CAA-refman",
                    "TestFramework", "function", 100,
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO api_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "page:widget", "api:widget", "detail", "interface", "",
                    "Official CATWidget summary.", "CATWidget.h",
                    "caadoc://Doc/generated/refman/TestFramework/interface_CATWidget_1.htm",
                    1, "[]", '["raw:widget"]', '["ev:page"]',
                ),
                (
                    "page:shared:a", "api:shared:a", "detail", "class", "",
                    "", "", "caadoc://Doc/generated/refman/Alpha/class_CATShared_1.htm",
                    1, "[]", '["raw:shared:a"]', "[]",
                ),
                (
                    "page:shared:b", "api:shared:b", "detail", "interface", "",
                    "", "", "caadoc://Doc/generated/interfaces/Beta/interface_CATShared_1.htm",
                    1, "[]", '["raw:shared:b"]', "[]",
                ),
                (
                    "page:mix:int", "api:function:mix", "detail", "function",
                    "CATMix(int)", "Integer overload.", "CATMix.h",
                    "caadoc://Doc/generated/refman/TestFramework/function_CATMix_100.htm",
                    1, "[]", '["raw:mix:int"]', "[]",
                ),
                (
                    "page:mix:double", "api:function:mix", "detail", "function",
                    "CATMix(double)", "Double overload.", "CATMix.h",
                    "caadoc://Doc/generated/refman/TestFramework/function_CATMix_200.htm",
                    1, "[]", '["raw:mix:double"]', "[]",
                ),
            ],
        )
        connection.execute(
            "INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "member:run", "page:widget", "method:Run", "method", "Run",
                "HRESULT Run()", "Runs the widget.", "Run()", "raw:member:run",
                '["ev:member"]',
            ),
        )
        connection.execute(
            "INSERT INTO api_aliases VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "alias:widget", "部件接口", "部件接口", "zh-CN", "api_node",
                "api:widget", "CATWidget", "api", "测试接口", "api-content",
            ),
        )
        connection.executemany(
            "INSERT INTO projection_metadata VALUES (?,?)",
            [
                ("projection_schema_version", "1"),
                ("source_baseline", "CATIA V5R21 CAADoc"),
                (
                    "version_policy",
                    "R21 documentation baseline; results may be reusable across nearby releases but are not version guarantees.",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO evidence VALUES (?,?,?,?,?,?)",
            [
                (
                    "ev:page", "caadoc://Doc/generated/refman/TestFramework/interface_CATWidget_1.htm",
                    "", "Official page evidence.", "refman-page", "official",
                ),
                (
                    "ev:member", "caadoc://Doc/generated/refman/TestFramework/interface_CATWidget_1.htm",
                    "Run()", "Official member evidence.", "member-index", "official",
                ),
            ],
        )
        connection.execute(
            "INSERT INTO relations VALUES (?,?,?)",
            ("chunk:widget", "DEMONSTRATES", "raw:widget"),
        )
        connection.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?)",
            (
                "chunk:widget", "caadoc://CAACodeExamples/WidgetSample.cpp",
                '["Widget sample"]', "CATWidget sample usage.", '["CATWidget"]',
            ),
        )
        connection.commit()
        connection.close()

    def test_chinese_alias_search_keeps_english_key_and_relevance_score(self):
        result = self.index.search("部件接口")

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["requires_selection"])
        self.assertEqual(len(result["candidate_groups"]), 1)
        candidate = result["candidate_groups"][0]
        self.assertEqual(candidate["english_key"], "CATWidget")
        self.assertEqual(candidate["matched_key_en"], "CATWidget")
        self.assertEqual(candidate["match_tier"], "exact-zh-alias")
        self.assertEqual(candidate["match_score"], 0.97)
        self.assertEqual(
            result["metadata"]["score_semantics"],
            "query-relative lexical relevance; not factual confidence",
        )

    def test_same_name_candidates_require_explicit_selection(self):
        search_result = self.index.search("CATShared")
        get_result = self.index.get_api("CATShared")

        self.assertTrue(search_result["requires_selection"])
        self.assertEqual(
            {item["node_id"] for item in search_result["candidate_groups"]},
            {"api:shared:a", "api:shared:b"},
        )
        self.assertEqual(get_result["status"], "ambiguous")
        self.assertTrue(get_result["requires_selection"])
        self.assertEqual(len(get_result["candidates"]), 2)

    def test_get_api_discloses_only_requested_includes(self):
        base = self.index.get_api("api:widget")

        self.assertEqual(base["status"], "ok")
        self.assertEqual(base["included"], [])
        for key in ("members", "overloads", "evidence", "examples"):
            self.assertNotIn(key, base)

        members_only = self.index.get_api("api:widget", include=["members"])
        self.assertEqual(members_only["included"], ["members"])
        self.assertEqual(members_only["members"][0]["name"], "Run")
        self.assertNotIn("evidence", members_only)
        self.assertNotIn("examples", members_only)
        self.assertNotIn("overloads", members_only)

        expanded = self.index.get_api(
            "api:widget",
            include=["members", "overloads", "evidence", "examples"],
        )
        self.assertEqual(len(expanded["overloads"]), 1)
        self.assertEqual(
            {item["evidence_id"] for item in expanded["evidence"]},
            {"ev:page", "ev:member"},
        )
        self.assertEqual(expanded["examples"][0]["chunk_id"], "chunk:widget")

    def test_function_family_without_aggregate_requires_overload_selection(self):
        result = self.index.get_api("api:function:mix")

        self.assertEqual(result["status"], "requires_overload_selection")
        self.assertTrue(result["requires_overload_selection"])
        self.assertIsNone(result["preferred_source"])
        self.assertEqual(
            {item["page_id"] for item in result["overloads"]},
            {"page:mix:int", "page:mix:double"},
        )

        selected = self.index.get_api("page:mix:int")
        self.assertEqual(selected["status"], "ok")
        self.assertFalse(selected["requires_overload_selection"])
        self.assertEqual(selected["preferred_source"]["page_id"], "page:mix:int")

    def test_read_source_uses_member_anchor_and_returns_plain_text_metadata(self):
        result = self.index.read_source("member:run", max_chars=100)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["source_uri"],
            "caadoc://Doc/generated/refman/TestFramework/interface_CATWidget_1.htm",
        )
        self.assertEqual(result["anchor"], "Run()")
        self.assertTrue(result["anchor_found"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["format"], "text")
        self.assertIn("Runs the official widget operation", result["content"])
        self.assertNotIn("<p>", result["content"])
        self.assertNotIn("neighboring member", result["content"])
        self.assertEqual(len(result["content"]), 100)

    def test_read_source_can_return_raw_html_for_member_anchor(self):
        result = self.index.read_source("member:run", format="raw_html")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["format"], "raw_html")
        self.assertEqual(result["anchor"], "Run()")
        self.assertFalse(result["truncated"])
        self.assertIn('<a name="Run()"></a>', result["content"])
        self.assertIn("<p>Runs the official widget operation", result["content"])
        self.assertNotIn("Stop()", result["content"])

    def test_read_source_rejects_path_traversal(self):
        with self.assertRaisesRegex(PermissionError, "Path traversal"):
            self.index.read_source("caadoc://../outside.htm")


if __name__ == "__main__":
    unittest.main()
