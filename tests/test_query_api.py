import json
import hashlib
import os
import shutil
import sqlite3
import inspect
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.caa_manual_query import CaaManualIndex
from tools.caa_manual_mcp_server import McpServer, tool_defs
from tools.caa_manual_cli import build_parser
from tools.caa_manual_source_search import build_source_cache, cache_path


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
        self.root = Path(self.temp_dir.name).resolve()
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
        with self.assertRaisesRegex(PermissionError, "configuration"):
            self.index.read_source("manual://.env")

    def test_code_is_not_parsed_as_html(self):
        source = '#include <iostream.h>\nif (a < b && c > d) {}\n'
        (self.caadoc_root / "sample.cpp").write_bytes(source.encode("utf-8"))
        result = self.index.read_source("caadoc://sample.cpp")
        self.assertEqual(result["content"], source)
        self.assertEqual(result["source_kind"], "code")

    def test_adjacent_anchor_and_static_signature_types(self):
        (self.caadoc_root / "sample.htm").write_text('''<html><body><p>Only one face.</p>
<a name="Run"></a><a name="Run(CATBody*)"></a>
<script>activateLink('HRESULT','HRESULT');</script> Run(
<script>activateLink('CATBody','CATBody');</script>* body)
<script>throw new Error('must not execute');</script>
<p>Runs.</p><a name="Stop"></a><p>Neighbor.</p></body></html>''', encoding="utf-8")
        result = self.index.read_source("caadoc://sample.htm#Run", include_context=True)
        self.assertIn("HRESULT Run(", result["content"])
        self.assertIn("CATBody* body)", result["content"])
        self.assertNotIn("Neighbor", result["content"])
        self.assertNotIn("must not execute", result["content"])
        self.assertIn("Only one face.", result["api_context"])
        signature = self.index.read_source("caadoc://sample.htm#Run%28CATBody%2A%29")
        self.assertEqual(signature["content"], result["content"])

    def test_r33_refman_context_skips_navigation_and_member_index(self):
        (self.caadoc_root / "sample.htm").write_text('''<html><head>
<title>CATWidget</title></head><body><header><nav>
<a id="multiview" href="../_index/main.htm">All Frameworks</a>
</nav></header><main><section id="maindesc">
<script>generatedFatherClass('CATWidget');</script><section id="objdesc">
<p><b>Role:</b> Operates on the widget shell.</p>
<p>Only one face is allowed.</p><a id="constraint-note"></a>
<section><p>The edge must belong to the shell boundary.</p></section>
<p>Release the operator after retrieving its result.</p></section>
<section id="idxsum"><article id="methoidx"><h2>Method Index</h2>
<table><tr><td><a href="#Run()">Run()</a></td></tr></table></article></section>
</section><section id="objdef"><article id="methoddef">
<a id="Run"></a><a id="Run()"></a><h2>Run</h2>
<p>public virtual HRESULT Run() = 0</p><p>Runs the operation.</p>
<a id="Stop()"></a><p>Neighbor member.</p>
</article></section></main></body></html>''', encoding="utf-8")
        result = self.index.read_source("caadoc://sample.htm#Run()", include_context=True)
        context = result["api_context"]
        for expected in ("Role:", "Only one face", "shell boundary", "Release the operator"):
            self.assertIn(expected, context)
        for excluded in ("All Frameworks", "Method Index", "Run()", "HRESULT", "Neighbor"):
            self.assertNotIn(excluded, context)
        self.assertFalse(result["api_context_truncated"])
        self.assertIn("public virtual HRESULT Run() = 0", result["content"])
        self.assertNotIn("Neighbor", result["content"])

    def test_r33_automation_context_prefers_maindesc_over_definition_objdesc(self):
        (self.caadoc_root / "sample.htm").write_text('''<html><body><nav>
<a id="multiview">All Frameworks</a></nav><section id="maindesc">
<p>Role: Creates widget features.</p><p>The input must be in the current container.</p>
<section id="idxsum"><article id="fctdef"><h2>Method Index</h2>
<table><tr><td>Run()</td></tr></table></article></section>
<section id="objdesc"><article id="fctdef"><a id="Run"></a>
<p>Func Run() As Widget</p><p>Creates one widget.</p></article></section>
</section></body></html>''', encoding="utf-8")
        result = self.index.read_source("caadoc://sample.htm#Run", include_context=True)
        self.assertIn("Creates widget features", result["api_context"])
        self.assertIn("current container", result["api_context"])
        for excluded in ("All Frameworks", "Method Index", "Run()", "Creates one widget"):
            self.assertNotIn(excluded, result["api_context"])
        self.assertIn("Func Run() As Widget", result["content"])

    def test_semantic_objdesc_context_stops_at_member_article_and_is_bounded(self):
        overview = "Constraint: preserve the input. " * 250
        (self.caadoc_root / "sample.htm").write_text(
            '<html><body><a id="multiview">Navigation</a><section id="objdesc"><p>'
            + overview + '</p><article id="methoddef"><a id="Run"></a>'
            '<p>HRESULT Run()</p></article></section></body></html>', encoding="utf-8")
        result = self.index.read_source("caadoc://sample.htm#Run", include_context=True)
        self.assertEqual(result["api_context"], overview[:6000])
        self.assertTrue(result["api_context_truncated"])
        self.assertIn("HRESULT Run()", result["content"])

    def test_read_pagination_reassembles_content(self):
        whole = self.index.read_source("member:run")
        offset, parts = 0, []
        while offset is not None:
            page = self.index.read_source("member:run", max_chars=100, offset=offset)
            parts.append(page["content"])
            self.assertEqual(page["total_chars"], len(whole["content"]))
            offset = page["next_offset"]
        self.assertEqual("".join(parts), whole["content"])

    def test_catalog_reports_remaining_siblings(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE catalog_nodes SET parent_id='api:widget' WHERE node_id<>'api:widget'")
        first = self.index.catalog("api:widget", limit=1)
        self.assertEqual(first["total_count"], 3)
        self.assertTrue(first["truncated"])
        second = self.index.catalog("api:widget", limit=2, offset=first["next_offset"])
        self.assertFalse(second["truncated"])
        self.assertEqual(len({row["node_id"] for row in first["children"] + second["children"]}), 3)

    def test_filter_precedes_member_limit(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            row = db.execute("SELECT * FROM api_members").fetchone()
            for i in range(40):
                values = list(row)
                values[0], values[1] = f"member:noise:{i}", "page:shared:a"
                db.execute("INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)", values)
            values = list(row)
            values[0], values[1] = "member:beta", "page:shared:b"
            db.execute("INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)", values)
        result = self.index.search("Run", framework="Beta", limit=1)
        self.assertEqual(result["candidate_groups"][0]["node_id"], "api:shared:b")

    def test_qualified_member_and_documented_inheritance(self):
        tree = self.caadoc_root / "Doc/generated/refman/_index/jsTree.js"
        tree.parent.mkdir(parents=True)
        tree.write_text('fatherLink["class_CATShared_1"]="interface_CATWidget_1";', encoding="utf-8")
        result = self.index.search("CATShared::Run", layer="CAA-refman", framework="Alpha")
        match = result["candidate_groups"][0]["match_details"][0]["matched_member"]
        self.assertEqual(match["declared_in"], "CATWidget")
        self.assertEqual(match["requested_owner"], "CATShared")
        inherited = self.index.get_api("api:shared:a", ["members"], member="Run", inherited=True)
        self.assertEqual(inherited["members"][0]["declared_in"], "api:widget")
        self.assertEqual(self.index.read_source("CATWidget::Run")["anchor"], "Run()")
        resolved = self.index.get_api("api:shared:a::Run")
        self.assertEqual(resolved["qualified_resolution"]["inheritance_chain"], ["CATShared", "CATWidget"])
        self.assertIn("qualified_resolution", self.index.read_source("CATWidget::Run"))

    def test_qualified_overloads_are_not_silently_selected(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            row = list(db.execute("SELECT * FROM api_members").fetchone())
            row[0], row[5], row[7] = "member:run:int", "HRESULT Run(int)", "Run(int)"
            db.execute("INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        result = self.index.read_source("CATWidget::Run")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)

    def test_qualified_empty_parameter_reference_reads_same_member(self):
        direct = self.index.read_source("member:run")
        result = self.index.read_source("CATWidget::Run()")
        self.assertEqual(result["content"], direct["content"])
        self.assertEqual(result["anchor"], direct["anchor"])
        self.assertEqual(self.index.get_api("CATWidget::Run()")["selected_member_id"], "member:run")

    def test_qualified_anchor_preserves_type_identity_and_ambiguity(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            template = list(db.execute("SELECT * FROM api_members").fetchone())
            for member_id, anchor in [("member:ref", "Run(NS::Vector&lt;int,double&gt;&amp;)"),
                                      ("member:ptr", "Run(NS::Vector&lt;int,double&gt;*)")]:
                row = list(template)
                row[0], row[5], row[7] = member_id, anchor, anchor
                db.execute("INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        reference = " CATWidget :: Run ( NS::Vector<int, double> &amp; ) "
        result = self.index.get_api(reference)
        self.assertEqual(result["selected_member_id"], "member:ref")
        fullwidth = "CATWidget：：Run（NS::Vector<int，double>&）"
        self.assertEqual(self.index.get_api(fullwidth)["selected_member_id"], "member:ref")
        matches = self.index.search(fullwidth)["candidate_groups"]
        self.assertEqual(matches[0]["match_details"][0]["matched_member"]["member_id"], "member:ref")
        for signature in ("Run(const NS::Vector<int,double>&)", "Run(NS::Vector<int,double>**)",
                          "Run(NS::Vector<int,float>&)", "Run(ns::Vector<int,double>&)", "Run(1.0)"):
            self.assertEqual(self.index.get_api("CATWidget::" + signature)["status"], "not_found")
        self.assertEqual(self.index.get_api("CATWidget::Run")["status"], "ambiguous")

    def test_signature_lookup_does_not_bypass_name_hiding(self):
        tree = self.caadoc_root / "Doc/generated/refman/_index/jsTree.js"
        tree.parent.mkdir(parents=True)
        tree.write_text('fatherLink["class_CATShared_1"]="interface_CATWidget_1";', encoding="utf-8")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            row = list(db.execute("SELECT * FROM api_members").fetchone())
            row[0], row[1], row[5], row[7] = "member:child", "page:shared:a", "Run(int)", "Run(int)"
            db.execute("INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        result = self.index.search("CATShared::Run()", layer="CAA-refman", framework="Alpha")
        self.assertEqual(result["candidate_groups"], [])
        result = self.index.search("CATShared::Run(int)", layer="CAA-refman", framework="Alpha")
        self.assertEqual(result["candidate_groups"][0]["match_details"][0]["matched_member"]["declared_in"], "CATShared")

    def test_duplicate_anchor_identity_remains_ambiguous(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            row = list(db.execute("SELECT * FROM api_members").fetchone())
            row[0] = "member:duplicate"
            db.execute("INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        result = self.index.get_api("CATWidget::Run()")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({row["member_id"] for row in result["candidates"]}, {"member:run", "member:duplicate"})

    def test_anchor_normalization_keeps_word_boundaries(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            row = list(db.execute("SELECT * FROM api_members").fetchone())
            row[0], row[5], row[7] = "member:unsigned", "Run(unsigned int)", "Run(unsigned int)"
            db.execute("INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        result = self.index.get_api("CATWidget::Run( unsigned  int )")
        self.assertEqual(result["selected_member_id"], "member:unsigned")
        self.assertEqual(self.index.get_api("CATWidget::Run(unsignedint)")["status"], "not_found")

    def test_spaced_member_lookup_requires_indexed_owner(self):
        result = self.index.search("CATWidget Run")
        self.assertEqual(result["query_interpretation"]["reference"], "CATWidget::Run")
        self.assertEqual(result["candidate_groups"][0]["match_tier"], "qualified-member")
        missing = self.index.search("CATWidget NoSuchMember")
        self.assertEqual(missing["candidate_groups"], [])
        unrelated = self.index.search("surface boundary")
        self.assertNotIn("query_interpretation", unrelated)

    def test_unknown_member_suggestions_are_not_resolved_as_aliases(self):
        result = self.index.get_api("CATWidget", ["members"], member="Runn")
        self.assertEqual(result["members"], [])
        self.assertEqual(result["member_name_suggestions"]["candidates"], ["Run"])
        self.assertIn("not aliases", result["member_name_suggestions"]["basis"])
        self.assertEqual(self.index.read_source("CATWidget::Runn")["status"], "not_found")

    def test_example_symbol_previews_have_explicit_truncation(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE chunks SET symbols_json=?", (json.dumps([f"Symbol{i}" for i in range(40)]),))
        result = self.index.get_api("CATWidget", ["examples"])
        example = result["examples"][0]
        self.assertEqual(len(example["symbols"]), 20)
        self.assertEqual(example["symbols_total_count"], 40)
        self.assertTrue(example["symbols_truncated"])

    def test_missing_inheritance_source_is_explicit(self):
        result = self.index.get_api("CATWidget", ["members"], inherited=True)
        self.assertEqual(result["inheritance"]["state"], "source_unavailable")

    def test_member_and_example_pagination(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE api_examples(api_node_id TEXT, chunk_id TEXT, source_uri TEXT, symbols_json TEXT)")
            db.executemany("INSERT INTO api_examples VALUES (?,?,?,?)", [
                ("api:widget", f"ex:{i}", f"caadoc://sample{i // 2}.cpp", "[]") for i in range(46)])
        result = self.index.get_api("CATWidget", ["members", "examples"], member="Unknown", example_limit=20)
        self.assertEqual(result["members"], [])
        self.assertEqual(result["examples_pagination"]["total_count"], 23)
        self.assertEqual(result["examples_pagination"]["next_offset"], 20)
        last = self.index.get_api("CATWidget", ["members", "examples"], member_offset=1, example_offset=20)
        self.assertEqual(last["members"], [])
        self.assertEqual(len(last["examples"]), 3)
        self.assertFalse(last["examples_pagination"]["has_more"])

    def test_curated_query_expansion_is_disclosed(self):
        path = self.manual_root / "config/catalog_zh.yaml"
        path.parent.mkdir()
        path.write_text(json.dumps({"query_expansions": [{"terms": ["运行部件"],
            "targets": ["CATWidget"], "basis": "fixture"}]}), encoding="utf-8")
        result = self.index.search("如何运行部件")
        self.assertEqual(result["candidate_groups"][0]["match_tier"], "curated-expansion")
        self.assertEqual(result["query_expansions"][0]["basis"], "fixture")

    def test_fts_fallback_applies_framework_before_limit(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE VIRTUAL TABLE api_search_fts USING fts5(api_node_id UNINDEXED, member_id UNINDEXED, name)")
            db.executemany("INSERT INTO api_search_fts VALUES (?,?,?)", [
                ("api:shared:a", "", "rareterm") for _ in range(20)])
            db.execute("INSERT INTO api_search_fts VALUES (?,?,?)", ("api:shared:b", "", "rareterm"))
        result = self.index.search("rareterm", framework="Beta", limit=1)
        self.assertEqual(result["candidate_groups"][0]["node_id"], "api:shared:b")
        self.assertEqual(result["candidate_groups"][0]["match_tier"], "fts-lexical")

    def test_mcp_schema_and_python_arguments_stay_aligned(self):
        methods = {"caa_status": "status", "caa_catalog": "catalog", "caa_search": "search",
                   "caa_get_api": "get_api", "caa_read_source": "read_source", "caa_search_source": "search_source"}
        for definition in tool_defs():
            parameters = set(inspect.signature(getattr(self.index, methods[definition["name"]])).parameters)
            self.assertEqual(set(definition["inputSchema"]["properties"]), parameters)
        result = McpServer(self.index).call_tool("caa_read_source", {
            "reference": "CATWidget::Run", "offset": 5, "max_chars": 100})["structuredContent"]
        self.assertEqual(result["offset"], 5)
        args = build_parser().parse_args(["get-api", "CATWidget", "--member", "Run", "--inherited", "--member-limit", "1"])
        self.assertTrue(args.inherited)
        self.assertEqual(args.member_limit, 1)

    def test_private_source_cache_search_and_stale_detection(self):
        self.assertEqual(self.index.search_source("widget")["status"], "not_indexed")
        result = build_source_cache(self.index)
        self.assertEqual(result["document_count"], 1)
        first = self.index.search_source("official widget", limit=1)
        self.assertEqual(first["total_count"], 1)
        self.assertFalse(first["results"][0]["source_changed_since_index"])
        path = self.index.resolve_source_path(first["results"][0]["source_uri"])
        path.write_text("changed", encoding="utf-8")
        self.assertTrue(self.index.search_source("official widget")["results"][0]["source_changed_since_index"])
        self.assertEqual(self.index.search_source('" OR NOT widget')["total_count"], 0)
        self.index.caadoc_root = self.root / "different"
        self.assertEqual(self.index.search_source("widget")["status"], "source_root_changed")

    def test_manifest_is_loaded_beside_selected_database(self):
        alternate_db = self.manual_root / "data/r33/manual.sqlite"
        alternate_db.parent.mkdir()
        shutil.copyfile(self.db_path, alternate_db)
        self.db_path.with_name("manifest.json").write_text('{"release":"R21"}', encoding="utf-8")
        alternate_db.with_name("manifest.json").write_text('{"release":"R33"}', encoding="utf-8")
        alternate = CaaManualIndex(alternate_db, self.manual_root, self.caadoc_root)
        self.assertEqual(alternate.status()["manifest"], {"release": "R33"})

    def test_missing_version_metadata_is_not_reported_as_r21(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM projection_metadata")
        metadata = self.index.status()["metadata"]
        self.assertEqual(metadata["source_baseline"], "unknown")
        self.assertEqual(metadata["source_release"], "unknown")
        self.assertEqual(metadata["version_policy"], "Not provided by this index.")
        self.assertNotIn("source_tree_sha256", metadata)

    def test_legacy_release_is_inferred_and_explicit_release_is_preserved(self):
        self.assertEqual(self.index.status()["metadata"]["source_release"], "R21")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO projection_metadata VALUES ('source_release','R33')")
            db.execute("UPDATE projection_metadata SET value='CATIA V5R33 CAADoc' WHERE key='source_baseline'")
        self.assertEqual(self.index.status()["metadata"]["source_release"], "R33")

    def _record_tree_fingerprint(self):
        tree = self.caadoc_root / "Doc/generated/refman/_index/jsTree.js"
        tree.parent.mkdir(parents=True, exist_ok=True)
        tree.write_text('fatherLink["class_CATShared_1"]="interface_CATWidget_1";', encoding="utf-8")
        digest = hashlib.sha256(tree.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO projection_metadata VALUES ('source_tree_sha256',?)", (digest,))
        return tree

    def test_tree_fingerprint_guards_reads_and_inheritance_after_file_change(self):
        tree = self._record_tree_fingerprint()
        self.assertIn("Runs the official widget", self.index.read_source("member:run")["content"])
        inherited = self.index.get_api("api:shared:a", ["members"], member="Run", inherited=True)
        self.assertEqual(inherited["members"][0]["name"], "Run")
        old_stat = tree.stat()
        tree.write_text(tree.read_text(encoding="utf-8").replace("CATShared", "CAXShared"), encoding="utf-8")
        self.assertEqual(tree.stat().st_size, old_stat.st_size)
        os.utime(tree, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000_000))
        for reference in ("member:run", "caadoc://Doc/generated/refman/TestFramework/interface_CATWidget_1.htm"):
            with self.assertRaisesRegex(RuntimeError, "source/version mismatch"):
                self.index.read_source(reference)
        with self.assertRaisesRegex(RuntimeError, "source/version mismatch"):
            self.index.get_api("api:shared:a", ["members"], inherited=True)
        self.assertEqual(self.index.get_api("CATWidget")["status"], "ok")

    def test_recorded_tree_must_exist_but_legacy_index_does_not_require_it(self):
        self.assertEqual(self.index.read_source("member:run")["status"], "ok")
        self._record_tree_fingerprint().unlink()
        with self.assertRaisesRegex(RuntimeError, "source/version mismatch"):
            self.index.read_source("member:run")
        with self.assertRaisesRegex(RuntimeError, "source/version mismatch"):
            self.index.get_api("CATWidget", ["members"], inherited=True)

    def test_private_source_cache_binds_database_even_with_same_source_root(self):
        alternate_db = self.db_path.with_name("alternate.sqlite")
        shutil.copyfile(self.db_path, alternate_db)
        with closing(sqlite3.connect(alternate_db)) as db, db:
            db.execute("INSERT INTO projection_metadata VALUES ('fixture','different database')")
        alternate = CaaManualIndex(alternate_db, self.manual_root, self.caadoc_root)
        built = build_source_cache(self.index)
        self.assertEqual(built["cache_schema_version"], 2)
        self.assertEqual(built["database_sha256"], hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertEqual(alternate.search_source("widget")["status"], "not_indexed")
        self.assertEqual(self.index.search_source("widget")["status"], "ok")
        build_source_cache(alternate)
        self.assertEqual(alternate.search_source("widget")["status"], "ok")
        self.assertEqual(self.index.search_source("widget")["status"], "not_indexed")

    def test_private_source_cache_rejects_changed_database_and_old_schema(self):
        build_source_cache(self.index)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO projection_metadata VALUES ('fixture','changed in place')")
        self.assertEqual(self.index.search_source("widget")["status"], "not_indexed")
        build_source_cache(self.index)
        with closing(sqlite3.connect(cache_path(self.index))) as db, db:
            db.execute("DELETE FROM metadata WHERE key IN ('cache_schema_version','database_sha256')")
        result = self.index.search_source("widget")
        self.assertEqual(result["status"], "not_indexed")
        self.assertIn("index-sources", result["next_action"])

    def test_r33_private_cache_is_separate_from_legacy_cache(self):
        build_source_cache(self.index)
        alternate_db = self.db_path.with_name("r33.sqlite")
        shutil.copyfile(self.db_path, alternate_db)
        with closing(sqlite3.connect(alternate_db)) as db, db:
            db.execute("INSERT INTO projection_metadata VALUES ('source_release','R33')")
        alternate = CaaManualIndex(alternate_db, self.manual_root, self.caadoc_root)
        self.assertEqual(cache_path(self.index), self.manual_root / "cache/r21/source-search.sqlite")
        self.assertEqual(cache_path(alternate), self.manual_root / "cache/r33/source-search.sqlite")
        self.assertEqual(alternate.search_source("widget")["status"], "not_indexed")
        build_source_cache(alternate)
        self.assertEqual(alternate.search_source("widget")["status"], "ok")
        self.assertEqual(self.index.search_source("widget")["status"], "ok")

    def test_private_cache_rejects_tree_mismatch(self):
        tree = self._record_tree_fingerprint()
        build_source_cache(self.index)
        tree.write_text("different hierarchy", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "source/version mismatch"):
            self.index.search_source("widget")
        with self.assertRaisesRegex(RuntimeError, "source/version mismatch"):
            build_source_cache(self.index)


if __name__ == "__main__":
    unittest.main()
