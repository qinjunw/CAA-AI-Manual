import os
import sqlite3
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.export_public_index import export_public_index, write_public_manifest
from tools.caa_manual_query import CaaManualIndex
from tools.caa_manual_projection import SOURCE_BASELINE, release_metadata


class PublicIndexExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
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

    def test_full_database_is_scrubbed_outside_the_repository(self):
        project = self.root / "checkout"
        with closing(sqlite3.connect(self.source_db)) as db, db:
            db.executemany("INSERT INTO projection_metadata VALUES (?,?)", release_metadata("R21").items())

        def interrupt_scrub(db):
            location = Path(db.execute("PRAGMA database_list").fetchone()[2])
            self.assertFalse(location.is_relative_to(project), f"Unscrubbed copy inside checkout: {location}")
            raise RuntimeError("interrupted before scrub")

        with patch("tools.export_public_index.PROJECT_ROOT", project), \
                patch("tools.export_public_index._clear_embedded_text", side_effect=interrupt_scrub), \
                self.assertRaisesRegex(RuntimeError, "interrupted"):
            export_public_index(self.source_db, project / "data/r21/manual.sqlite")
        self.assertFalse((project / "data/r21/manual.sqlite").exists())

    def test_failed_publication_preserves_both_artifacts(self):
        project = self.root / "checkout"
        database = project / "data/r21/manual.sqlite"
        manifest = database.with_name("manifest.json")
        database.parent.mkdir(parents=True)
        database.write_bytes(b"old index")
        manifest.write_bytes(b"old manifest")
        with closing(sqlite3.connect(self.source_db)) as db, db:
            db.executemany("INSERT INTO projection_metadata VALUES (?,?)", release_metadata("R21").items())
        replace = os.replace
        for locked in (database, manifest):
            def refuse_locked(src, dst):
                if Path(dst) == locked:
                    raise PermissionError("destination locked")
                replace(src, dst)

            with self.subTest(locked=locked), patch("tools.export_public_index.PROJECT_ROOT", project), \
                    patch("tools.export_public_index.os.replace", side_effect=refuse_locked):
                with self.assertRaises(PermissionError):
                    export_public_index(self.source_db, database)
                self.assertEqual(database.read_bytes(), b"old index")
                self.assertEqual(manifest.read_bytes(), b"old manifest")

    def test_status_identifies_an_interrupted_export_even_if_counts_match(self):
        project = self.root / "checkout"
        database = project / "data/r21/manual.sqlite"
        with closing(sqlite3.connect(self.source_db)) as db, db:
            db.executemany("INSERT INTO projection_metadata VALUES (?,?)", release_metadata("R21").items())
        with patch("tools.export_public_index.PROJECT_ROOT", project):
            export_public_index(self.source_db, database)
            first = database.read_bytes()
            with closing(sqlite3.connect(self.source_db)) as db, db:
                db.execute("UPDATE api_pages SET signature='AlternateSignature'")
            export_public_index(self.source_db, database)
            database.write_bytes(first)  # The process stopped after replacing only the manifest.
            index = CaaManualIndex(database, project, None)
            status = index.status()
            self.assertEqual(status["status"], "manifest_mismatch")
            self.assertIn("export", status["next_action"])
            export_public_index(self.source_db, database)
            self.assertEqual(index.status()["status"], "ok")

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_publishing_preserves_shared_file_permissions(self):
        self.output_db.touch()
        self.output_db.chmod(0o640)
        export_public_index(self.source_db, self.output_db)
        self.assertEqual(self.output_db.stat().st_mode & 0o777, 0o640)

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

    def test_export_preserves_r33_metadata_and_manifest_without_source_manifest(self):
        expected = {
            **release_metadata("R33"),
            "catalog_source_baseline": SOURCE_BASELINE,
            "source_tree_sha256": "0123456789abcdef" * 4,
        }
        with closing(sqlite3.connect(self.source_db)) as connection, connection:
            connection.executemany("INSERT INTO projection_metadata VALUES (?,?)", expected.items())

        result = export_public_index(self.source_db, self.output_db)

        self.assertEqual(result["source_metadata"], expected)
        with closing(sqlite3.connect(self.output_db)) as connection:
            actual = dict(connection.execute("SELECT key,value FROM projection_metadata"))
            for key, value in expected.items():
                self.assertEqual(actual[key], value, key)
            self.assertEqual(actual["content_mode"], "index-only")
            self.assertEqual(connection.execute("SELECT summary_en FROM api_pages").fetchone()[0], "")
        output_manifest = self.root / "manifest.json"
        write_public_manifest(None, output_manifest, result)
        manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
        for key, value in expected.items():
            self.assertEqual(manifest[key], value, key)

    def test_public_manifest_rejects_conflicting_database_identity_without_overwrite(self):
        expected = {
            **release_metadata("R33"),
            "catalog_source_baseline": SOURCE_BASELINE,
            "source_tree_sha256": "0123456789abcdef" * 4,
        }
        with closing(sqlite3.connect(self.source_db)) as connection, connection:
            connection.executemany("INSERT INTO projection_metadata VALUES (?,?)", expected.items())
        result = export_public_index(self.source_db, self.output_db)
        source_manifest = self.root / "source-manifest.json"
        output_manifest = self.root / "manifest.json"
        preserved = '{"existing": true}\n'
        output_manifest.write_text(preserved, encoding="utf-8")
        for key in expected:
            with self.subTest(key=key):
                conflict = {**expected, key: "different-source-value"}
                source_manifest.write_text(json.dumps(conflict), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, f"Source manifest {key} does not match"):
                    write_public_manifest(source_manifest, output_manifest, result)
                self.assertEqual(output_manifest.read_text(encoding="utf-8"), preserved)

        source_manifest.write_text(json.dumps(expected), encoding="utf-8")
        write_public_manifest(source_manifest, output_manifest, result)
        manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
        self.assertEqual({key: manifest[key] for key in expected}, expected)

    def test_legacy_r21_database_cannot_gain_r33_release_from_manifest(self):
        legacy_metadata = {key: value for key, value in release_metadata("R21").items()
                           if key != "source_release"}
        with closing(sqlite3.connect(self.source_db)) as connection, connection:
            connection.executemany("INSERT INTO projection_metadata VALUES (?,?)", legacy_metadata.items())
        result = export_public_index(self.source_db, self.output_db)
        source_manifest = self.root / "source-manifest.json"
        source_manifest.write_text(json.dumps({"source_release": "R33"}), encoding="utf-8")
        output_manifest = self.root / "manifest.json"
        with self.assertRaisesRegex(ValueError, "source_release|source_baseline"):
            write_public_manifest(source_manifest, output_manifest, result)
        self.assertFalse(output_manifest.exists())

    def test_conflicting_manifest_is_rejected_before_backup_or_output_replacement(self):
        with closing(sqlite3.connect(self.source_db)) as connection, connection:
            connection.executemany("INSERT INTO projection_metadata VALUES (?,?)", release_metadata("R33").items())
        export_public_index(self.source_db, self.output_db)
        original = self.output_db.read_bytes()
        source_original = self.source_db.read_bytes()
        source_manifest = self.root / "source-manifest.json"
        source_manifest.write_text(json.dumps({"source_release": "R21"}), encoding="utf-8")
        real_connect = sqlite3.connect
        backup_calls = []

        class TrackedConnection(sqlite3.Connection):
            def backup(self, *args, **kwargs):
                backup_calls.append(True)
                return super().backup(*args, **kwargs)

        def tracked_connect(*args, **kwargs):
            return real_connect(*args, factory=TrackedConnection, **kwargs)

        for output in (self.output_db, self.root / "new-public.sqlite"):
            with self.subTest(existing=output == self.output_db):
                with patch("tools.export_public_index.sqlite3.connect", side_effect=tracked_connect):
                    with self.assertRaisesRegex(ValueError, "Source manifest source_release does not match"):
                        export_public_index(self.source_db, output, source_manifest)
                self.assertEqual(backup_calls, [])
                self.assertEqual(self.source_db.read_bytes(), source_original)
                if output == self.output_db:
                    self.assertEqual(output.read_bytes(), original)
                else:
                    self.assertFalse(output.exists())
                self.assertFalse(output.with_name(f".{output.name}.tmp").exists())


if __name__ == "__main__":
    unittest.main()
