import sqlite3
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.export_public_index import export_public_index, write_public_manifest
from tools.caa_manual_query import CaaManualIndex


class PublicIndexExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source_db = self.root / "full.sqlite"
        self.output_db = self.root / "public.sqlite"
        self._write_source_database()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_source_database(self):
        connection = sqlite3.connect(self.source_db)
        connection.executescript(
            """
            CREATE TABLE entities (
              id TEXT PRIMARY KEY, kind TEXT, name TEXT, framework TEXT,
              layer TEXT, source_path TEXT, anchor TEXT, signature TEXT,
              summary TEXT, tags_json TEXT, evidence_json TEXT, extra_json TEXT
            );
            CREATE TABLE chunks (
              id TEXT PRIMARY KEY, source_path TEXT, doc_kind TEXT,
              heading_path_json TEXT, text TEXT, symbols_json TEXT,
              capabilities_json TEXT, evidence_rank TEXT
            );
            CREATE TABLE evidence (
              id TEXT PRIMARY KEY, source_path TEXT, anchor TEXT,
              quote_or_span TEXT, extraction_method TEXT, trust_level TEXT
            );
            CREATE TABLE relations (
              src_id TEXT, type TEXT, dst_id TEXT, confidence REAL,
              source_path TEXT, evidence_id TEXT
            );
            CREATE TABLE catalog_nodes (
              node_id TEXT PRIMARY KEY, parent_id TEXT, node_type TEXT,
              english_key TEXT, name_en TEXT, name_zh TEXT, summary_zh TEXT,
              layer TEXT, framework TEXT, api_kind TEXT, sort_order INTEGER
            );
            CREATE TABLE api_pages (
              page_id TEXT PRIMARY KEY, api_node_id TEXT, page_role TEXT,
              page_template_kind TEXT, signature TEXT, summary_en TEXT,
              include_file TEXT, source_uri TEXT, source_available INTEGER,
              tags_json TEXT, raw_entity_ids_json TEXT, evidence_ids_json TEXT
            );
            CREATE TABLE api_members (
              member_id TEXT PRIMARY KEY, page_id TEXT, member_group_key TEXT,
              kind TEXT, name TEXT, signature TEXT, summary_en TEXT,
              anchor TEXT, raw_entity_id TEXT, evidence_ids_json TEXT
            );
            CREATE TABLE api_aliases (
              alias_id TEXT PRIMARY KEY, alias TEXT, normalized_alias TEXT,
              language TEXT, target_type TEXT, target_id TEXT,
              english_key TEXT, scope TEXT, summary_zh TEXT, basis TEXT
            );
            CREATE TABLE projection_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE VIRTUAL TABLE entities_fts USING fts5(
              id UNINDEXED, name, kind, framework, summary, signature, tags
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
              id UNINDEXED, heading, text, symbols, capabilities
            );
            CREATE VIRTUAL TABLE api_search_fts USING fts5(
              api_node_id UNINDEXED, page_id UNINDEXED, member_id UNINDEXED,
              record_type UNINDEXED, name, owner_name, framework, summary,
              signature, tags, aliases
            );
            """
        )
        source_uri = "caadoc://Doc/generated/refman/Test/interface_CATWidget_1.htm"
        connection.execute(
            "INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "raw:widget", "interface", "CATWidget", "Test", "CAA-refman",
                source_uri, "", "", "Embedded entity prose.", "[]", "[]", "{}",
            ),
        )
        connection.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
            (
                "chunk:widget", source_uri, "reference", '["Official heading"]',
                "Embedded chunk prose.", '["CATWidget"]', '["factory"]', "official",
            ),
        )
        connection.execute(
            "INSERT INTO evidence VALUES (?,?,?,?,?,?)",
            (
                "ev:widget", source_uri, "Run()", "Embedded evidence prose.",
                "refman-page", "official",
            ),
        )
        connection.execute(
            "INSERT INTO relations VALUES (?,?,?,?,?,?)",
            ("chunk:widget", "DEMONSTRATES", "raw:widget", 1.0, source_uri, "ev:widget"),
        )
        connection.execute(
            "INSERT INTO catalog_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "api:widget", "framework:test", "api", "CATWidget", "CATWidget",
                "部件接口", "部件查询入口。", "CAA-refman", "Test", "interface", 1,
            ),
        )
        connection.execute(
            "INSERT INTO api_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "page:widget", "api:widget", "detail", "interface", "",
                "Embedded page prose.", "CATWidget.h", source_uri, 1,
                '["factory"]', '["raw:widget"]', '["ev:widget"]',
            ),
        )
        connection.execute(
            "INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "member:run", "page:widget", "method:Run", "method", "Run",
                "HRESULT Run()", "Embedded member prose.", "Run()",
                "raw:widget", '["ev:widget"]',
            ),
        )
        connection.execute(
            "INSERT INTO api_aliases VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "alias:widget", "部件接口", "部件接口", "zh-CN", "api_node",
                "api:widget", "CATWidget", "api", "部件查询入口。", "maintainer",
            ),
        )
        connection.execute(
            "INSERT INTO projection_metadata VALUES (?,?)",
            ("projection_schema_version", "1"),
        )
        connection.execute(
            "INSERT INTO entities_fts VALUES (?,?,?,?,?,?,?)",
            ("raw:widget", "CATWidget", "interface", "Test", "Embedded prose", "", ""),
        )
        connection.execute(
            "INSERT INTO chunks_fts VALUES (?,?,?,?,?)",
            ("chunk:widget", "Official heading", "Embedded prose", "CATWidget", "factory"),
        )
        connection.execute(
            "INSERT INTO api_search_fts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "api:widget", "page:widget", "", "api_node", "CATWidget", "",
                "Test", "Embedded prose", "", "factory", "部件接口",
            ),
        )
        connection.commit()
        connection.close()

    def test_export_removes_prose_and_preserves_source_locations(self):
        result = export_public_index(self.source_db, self.output_db)

        self.assertEqual(result["content_mode"], "index-only")
        self.assertFalse(result["embedded_official_text"])

        with closing(sqlite3.connect(self.source_db)) as source:
            self.assertEqual(
                source.execute("SELECT summary FROM entities").fetchone()[0],
                "Embedded entity prose.",
            )

        with closing(sqlite3.connect(self.output_db)) as public:
            self.assertEqual(public.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0)
            self.assertEqual(public.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
            self.assertEqual(public.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)
            self.assertEqual(public.execute("SELECT quote_or_span FROM evidence").fetchone()[0], "")
            self.assertEqual(public.execute("SELECT summary_en FROM api_pages").fetchone()[0], "")
            self.assertEqual(public.execute("SELECT summary_en FROM api_members").fetchone()[0], "")
            self.assertEqual(
                public.execute("SELECT source_uri FROM api_pages").fetchone()[0],
                "caadoc://Doc/generated/refman/Test/interface_CATWidget_1.htm",
            )
            metadata = dict(public.execute("SELECT key, value FROM projection_metadata"))
            self.assertEqual(metadata["content_mode"], "index-only")
            self.assertEqual(metadata["embedded_official_text"], "false")
            self.assertEqual(
                public.execute("SELECT source_uri FROM api_examples").fetchone()[0],
                "caadoc://Doc/generated/refman/Test/interface_CATWidget_1.htm",
            )
            self.assertEqual(
                public.execute(
                    "SELECT COUNT(*) FROM api_search_fts WHERE api_search_fts MATCH 'Embedded'"
                ).fetchone()[0],
                0,
            )
            self.assertGreater(
                public.execute(
                    "SELECT COUNT(*) FROM api_search_fts WHERE api_search_fts MATCH 'CATWidget'"
                ).fetchone()[0],
                0,
            )

        index = CaaManualIndex(self.output_db, self.root, None)
        result = index.get_api("CATWidget", include=["evidence", "examples"])
        self.assertEqual(result["metadata"]["content_mode"], "index-only")
        self.assertFalse(result["preferred_source"]["source_available"])
        self.assertFalse(result["evidence"][0]["content_embedded"])
        self.assertFalse(result["examples"][0]["content_embedded"])

        with self.assertRaisesRegex(ValueError, "full local build"):
            export_public_index(self.output_db, self.root / "second-public.sqlite")

    def test_public_manifest_keeps_source_counts_and_replaces_artifact_counts(self):
        source_manifest = self.root / "source-manifest.json"
        output_manifest = self.root / "manifest.json"
        source_manifest.write_text(
            json.dumps({"source_counts": {"htm": 2}, "artifact_counts": {"chunks": 1}}),
            encoding="utf-8",
        )
        result = export_public_index(self.source_db, self.output_db)

        write_public_manifest(source_manifest, output_manifest, result)

        manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_counts"], {"htm": 2})
        self.assertEqual(manifest["content_mode"], "index-only")
        self.assertFalse(manifest["embedded_official_text"])
        self.assertEqual(manifest["artifact_counts"]["entities"], 0)


if __name__ == "__main__":
    unittest.main()
