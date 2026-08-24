import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tools.caa_manual_projection import (
    build_projection,
    create_projection_tables,
    page_template,
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
        connection.close()

    def test_page_template_removes_generated_overload_suffix(self):
        self.assertEqual(
            page_template("caadoc://Doc/generated/refman/Test/function_CATDo_24400.htm"),
            ("function", "CATDo", "detail"),
        )


if __name__ == "__main__":
    unittest.main()
