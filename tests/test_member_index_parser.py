from pathlib import Path
import unittest

from tools.build_caa_ai_manual import Store, infer_layer, parse_member_index


class ParseMemberIndexTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = "symbol:test:framework:class:Owner"
        self.store = Store(Path("unused"))
        self.store.add_entity({
            "id": self.owner_id,
            "kind": "class",
            "name": "Owner",
            "framework": "TestFramework",
            "layer": "CAA-refman",
        })

    def parse(self, raw: str) -> set[tuple[str, str]]:
        parse_member_index(raw, self.owner_id, "Owner", Path("owner.htm"), self.store)
        return {
            (entity["kind"], entity["name"])
            for entity_id, entity in self.store.entities.items()
            if entity_id != self.owner_id
        }

    def test_property_index_stops_before_method_index(self):
        raw = """
        <h2>Property Index</h2><br>
        <dl>
          <dt> o <a href="#Visible"><b>Visible</b></a>
          <dd>Controls visibility.
        </dl>
        <h2>Method Index</h2><br>
        <dl>
          <dt> o <a href="#Run()"><b>Run</b></a>()
          <dd>Runs the operation.
        </dl>
        """

        self.assertEqual(
            self.parse(raw),
            {("property", "Visible"), ("method", "Run")},
        )

    def test_method_index_stops_before_data_member_index(self):
        raw = """
        <h2>Method Index</h2><br>
        <dl>
          <dt> o <a href="#Run()"><b>Run</b></a>()
          <dd>Runs the operation.
        </dl>
        <h2>Data Member Index</h2><br>
        <dl>
          <dt> o <a href="#PublicWorkPtr"><b>PublicWorkPtr</b></a>
          <dd>A pointer to a public work area.
        </dl>
        """

        self.assertEqual(
            self.parse(raw),
            {("method", "Run"), ("data-member", "PublicWorkPtr")},
        )


class InferLayerTests(unittest.TestCase):
    def test_refman_path_wins_over_automation_words_in_body(self):
        path = Path("CAADoc/Doc/generated/refman/GSMInterfaces/interface_CATIExample_1.htm")

        self.assertEqual(infer_layer(path, "HybridShapeFactory returns an object)"), "CAA-refman")

    def test_interfaces_path_is_automation(self):
        path = Path("CAADoc/Doc/generated/interfaces/GSMInterfaces/interface_HybridShape_1.htm")

        self.assertEqual(infer_layer(path, ""), "Automation")


if __name__ == "__main__":
    unittest.main()
