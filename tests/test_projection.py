import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tools.caa_manual_projection import (
    SOURCE_BASELINE,
    VERSION_POLICY,
    build_projection,
    create_projection_tables,
    load_catalog,
    page_template,
    release_metadata,
)


class ProjectionTests(unittest.TestCase):
    def make_store(self, entities):
        return SimpleNamespace(
            entities={entity["id"]: entity for entity in entities},
            evidence={},
            chunks={},
        )

    def test_numbered_function_page_is_a_detail_of_one_family(self):
        base = "caadoc://Doc/generated/refman/TestFramework"
        entities = [
            {
                "id": "framework:test",
                "kind": "framework",
                "name": "TestFramework",
                "layer": "CAA-refman",
                "source_path": f"{base}/framework_TestFramework.htm",
            },
            {
                "id": "document:function-index",
                "kind": "document",
                "name": "CATDo",
                "layer": "CAA-refman",
                "framework": "TestFramework",
                "source_path": f"{base}/_function_CATDo.htm",
            },
            {
                "id": "function:one",
                "kind": "function",
                "name": "CATDo",
                "layer": "CAA-refman",
                "framework": "TestFramework",
                "source_path": f"{base}/function_CATDo_100.htm",
                "signature": "int CATDo(int value)",
            },
            {
                "id": "function:two",
                "kind": "function",
                "name": "CATDo",
                "layer": "CAA-refman",
                "framework": "TestFramework",
                "source_path": f"{base}/function_CATDo_200.htm",
                "signature": "int CATDo(double value)",
            },
        ]

        projection = build_projection(self.make_store(entities), {})
        function_nodes = [
            node for node in projection.catalog_nodes
            if node["node_type"] == "function-family"
        ]

        self.assertEqual(len(function_nodes), 1)
        self.assertEqual(function_nodes[0]["english_key"], "CATDo")
        self.assertEqual(len(projection.api_pages), 3)
        self.assertEqual(
            {page["page_role"] for page in projection.api_pages},
            {"aggregate", "detail"},
        )
        self.assertEqual(
            {page["api_node_id"] for page in projection.api_pages},
            {function_nodes[0]["node_id"]},
        )

    def test_special_functions_without_aggregate_remain_distinct(self):
        base = "caadoc://Doc/generated/refman/ObjectModelerBase"
        entities = [
            {
                "id": "function:and",
                "kind": "function",
                "name": "operator&&",
                "layer": "CAA-refman",
                "framework": "ObjectModelerBase",
                "source_path": f"{base}/function_operator_100.htm",
                "signature": "operator&&(Left, Right)",
            },
            {
                "id": "function:minus",
                "kind": "function",
                "name": "operator-",
                "layer": "CAA-refman",
                "framework": "ObjectModelerBase",
                "source_path": f"{base}/function_operator_200.htm",
                "signature": "operator-(Value)",
            },
        ]

        projection = build_projection(self.make_store(entities), {})
        names = {
            node["name_en"] for node in projection.catalog_nodes
            if node["node_type"] == "function-family"
        }

        self.assertEqual(names, {"operator&&", "operator-"})

    def test_constructor_and_destructor_remain_distinct(self):
        base = "caadoc://Doc/generated/refman/System"
        entities = [
            {
                "id": "function:constructor",
                "kind": "function",
                "name": "DSYStgRep",
                "layer": "CAA-refman",
                "framework": "System",
                "source_path": f"{base}/function_DSYStgRep_100.htm",
                "signature": "DSYStgRep()",
            },
            {
                "id": "function:destructor",
                "kind": "function",
                "name": "~DSYStgRep",
                "layer": "CAA-refman",
                "framework": "System",
                "source_path": f"{base}/function_DSYStgRep_200.htm",
            },
        ]

        projection = build_projection(self.make_store(entities), {})
        names = {
            node["name_en"] for node in projection.catalog_nodes
            if node["node_type"] == "function-family"
        }

        self.assertEqual(names, {"DSYStgRep", "~DSYStgRep"})

    def test_member_locations_are_unique_within_a_physical_page(self):
        source_uri = (
            "caadoc://Doc/generated/refman/TestFramework/interface_CATOwner_1.htm"
        )
        entities = [
            {
                "id": "api:owner",
                "kind": "interface",
                "name": "CATOwner",
                "layer": "CAA-refman",
                "framework": "TestFramework",
                "source_path": source_uri,
            },
            {
                "id": "member:one",
                "kind": "method",
                "name": "Run",
                "source_path": source_uri,
                "anchor": "Run()",
            },
            {
                "id": "member:two",
                "kind": "property",
                "name": "Run",
                "source_path": source_uri,
                "anchor": "Run()",
            },
        ]

        with self.assertRaisesRegex(ValueError, "Duplicate member page/anchor"):
            build_projection(self.make_store(entities), {})

    def test_chinese_alias_keeps_official_english_key(self):
        source_uri = (
            "caadoc://Doc/generated/refman/GeometricObjects/"
            "interface_CATGeoFactory_1.htm"
        )
        entities = [{
            "id": "api:factory",
            "kind": "interface",
            "name": "CATGeoFactory",
            "layer": "CAA-refman",
            "framework": "GeometricObjects",
            "source_path": source_uri,
        }]
        catalog = {
            "api_aliases": [{
                "target": "CATGeoFactory",
                "framework": "GeometricObjects",
                "query_terms_zh": ["几何工厂"],
                "basis": "api-content",
            }]
        }

        projection = build_projection(self.make_store(entities), catalog)
        alias = next(row for row in projection.api_aliases if row["alias"] == "几何工厂")

        self.assertEqual(alias["english_key"], "CATGeoFactory")
        self.assertEqual(alias["target_type"], "api_node")

    def test_source_availability_uses_caaddoc_relative_uri(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relative = Path("Doc/generated/refman/Test/interface_CATTest_1.htm")
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text("<html></html>", encoding="utf-8")
            entities = [{
                "id": "api:test",
                "kind": "interface",
                "name": "CATTest",
                "layer": "CAA-refman",
                "framework": "Test",
                "source_path": f"caadoc://{relative.as_posix()}",
            }]

            projection = build_projection(self.make_store(entities), {}, root)

        self.assertEqual(projection.api_pages[0]["source_available"], 1)

    def test_projection_tables_preserve_physical_page_identity(self):
        source_uri = (
            "caadoc://Doc/generated/refman/TestFramework/interface_CATOwner_1.htm"
        )
        projection = build_projection(self.make_store([{
            "id": "api:owner",
            "kind": "interface",
            "name": "CATOwner",
            "layer": "CAA-refman",
            "framework": "TestFramework",
            "source_path": source_uri,
        }]), {})
        connection = sqlite3.connect(":memory:")
        cursor = connection.cursor()
        cursor.executescript(
            "CREATE TABLE entities (id TEXT, name TEXT);"
            "CREATE TABLE relations (src_id TEXT, type TEXT, dst_id TEXT);"
        )

        create_projection_tables(cursor, projection)

        self.assertEqual(
            cursor.execute("SELECT source_uri FROM api_pages").fetchone()[0],
            source_uri,
        )
        self.assertEqual(
            cursor.execute("SELECT COUNT(*) FROM api_search_fts").fetchone()[0],
            1,
        )
        self.assertEqual(
            cursor.execute(
                "SELECT value FROM projection_metadata WHERE key='source_baseline'"
            ).fetchone()[0],
            "CATIA V5R21 CAADoc",
        )
        metadata = dict(cursor.execute("SELECT key, value FROM projection_metadata"))
        self.assertEqual(projection.source_release, "R21")
        self.assertEqual(projection.source_tree_sha256, "")
        self.assertNotIn("source_tree_sha256", metadata)
        self.assertEqual(
            {key: metadata[key] for key in release_metadata("R21")},
            {"source_release": "R21", "source_baseline": SOURCE_BASELINE,
             "version_policy": VERSION_POLICY},
        )
        connection.close()

    def test_r33_metadata_preserves_r21_catalog_provenance(self):
        store = self.make_store([{
            "id": "api:owner",
            "kind": "interface",
            "name": "CATOwner",
            "layer": "CAA-refman",
            "framework": "TestFramework",
            "source_path": "caadoc://Doc/generated/refman/TestFramework/interface_CATOwner_1.htm",
        }])
        catalog = {"source_baseline": SOURCE_BASELINE}
        tree_content = b'fatherLink["CATOwner"]="CATBase";'
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tree_path = root / "Doc/generated/refman/_index/jsTree.js"
            tree_path.parent.mkdir(parents=True)
            tree_path.write_bytes(tree_content)
            projection = build_projection(store, catalog, root, source_release="R33")
            legacy = build_projection(store, catalog, root)
        self.assertEqual(projection.catalog_nodes, legacy.catalog_nodes)
        self.assertEqual(projection.api_pages, legacy.api_pages)
        self.assertEqual(projection.api_members, legacy.api_members)
        self.assertEqual(projection.api_aliases, legacy.api_aliases)
        with closing(sqlite3.connect(":memory:")) as connection:
            cursor = connection.cursor()
            cursor.executescript(
                "CREATE TABLE entities (id TEXT, name TEXT);"
                "CREATE TABLE relations (src_id TEXT, type TEXT, dst_id TEXT);"
            )
            create_projection_tables(cursor, projection)
            metadata = dict(cursor.execute("SELECT key, value FROM projection_metadata"))
        self.assertEqual(projection.source_release, "R33")
        self.assertEqual(metadata["source_release"], "R33")
        self.assertEqual(metadata["source_baseline"], "CATIA V5-6R2023 (R33) CAADoc")
        self.assertEqual(metadata["version_policy"], release_metadata("R33")["version_policy"])
        self.assertIn("not version guarantees", metadata["version_policy"])
        self.assertEqual(metadata["catalog_source_baseline"], SOURCE_BASELINE)
        self.assertEqual(projection.source_tree_sha256, hashlib.sha256(tree_content).hexdigest())
        self.assertEqual(metadata["source_tree_sha256"], projection.source_tree_sha256)

    def test_invalid_source_release_is_rejected(self):
        for release in ("", "R22", "R34", "r33", None):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ValueError, "Unsupported source_release"):
                    release_metadata(release)
                with self.assertRaisesRegex(ValueError, "Unsupported source_release"):
                    build_projection(self.make_store([]), {}, source_release=release)

    def test_catalog_accepts_supported_provenance_without_selecting_release(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.json"
            for release in ("R21", "R33"):
                with self.subTest(release=release):
                    baseline = release_metadata(release)["source_baseline"]
                    path.write_text(json.dumps({"schema_version": 1, "source_baseline": baseline}),
                                    encoding="utf-8")
                    catalog = load_catalog(path)
                    projection = build_projection(self.make_store([]), catalog)
                    self.assertEqual(projection.source_release, "R21")
                    self.assertEqual(projection.catalog_source_baseline, baseline)
            path.write_text(json.dumps({"schema_version": 1, "source_baseline": "Unknown CAADoc"}),
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported source_baseline"):
                load_catalog(path)

    def test_page_template_removes_generated_overload_suffix(self):
        self.assertEqual(
            page_template("caadoc://Doc/generated/refman/Test/function_CATDo_24400.htm"),
            ("function", "CATDo", "detail"),
        )


if __name__ == "__main__":
    unittest.main()
