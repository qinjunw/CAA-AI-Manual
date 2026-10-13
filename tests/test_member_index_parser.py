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

    def test_legacy_heading_accepts_embedded_anchor(self):
        raw = """
        <dl><h2><a name="PropertyIndex"></a>Property Index</h2>
        <dt><a href="#Visible"><b>Visible</b></a><dd>Visibility setting.</dl>
        <dl><h2><a name="MethodIndex"></a>Method Index</h2>
        <dt><a href="#Run()"><b>Run</b></a>()<dd>Runs the test.</dl>
        """
        self.assertEqual(self.parse(raw), {("property", "Visible"), ("method", "Run")})

    def test_table_index_keeps_overloads_and_separates_signature_from_summary(self):
        raw = """
        <table><tbody><tr><th colspan='2'>Method Index</th></tr>
        <tr><td id='tdleft'><a href="#Create(CATItem&amp;)"><b>Create</b></a>(CATItem&amp;)</td>
        <td id='tdright'>Creates a <tt>test</tt> item.</td></tr>
        <tr><td id='tdleft'><a href='#Create(int*)'><b>Create</b></a>(int*)</td>
        <td id='tdright'>Creates from a count pointer.</td></tr>
        </tbody></table>
        """
        self.parse(raw)
        members = {item["anchor"]: item for item in self.store.entities.values() if item["kind"] == "method"}
        self.assertEqual(set(members), {"Create(CATItem&amp;)", "Create(int*)"})
        self.assertEqual(members["Create(CATItem&amp;)"]["signature"], "(CATItem&)")
        self.assertEqual(members["Create(CATItem&amp;)"]["summary"], "Creates a test item.")
        self.assertEqual(members["Create(int*)"]["signature"], "(int*)")
        self.assertEqual(members["Create(int*)"]["summary"], "Creates from a count pointer.")

    def test_table_sections_keep_member_kinds_with_unclosed_data_rows(self):
        raw = """
        <table><tr><th colspan='2'>Property Index</th></tr>
        <tr><td><a href="#Visible"><b>Visible</b></a></td><td>Visibility setting.</td></tr>
        <tr><td><a href="#Color"><b>Color</b></a></td><td>Color setting.</td></tr></table>
        <table><tr><th colspan='2'>Method Index</th></tr>
        <tr><td><a href="#Run()"><b>Run</b></a>()</td><td>Runs the test.</td></tr></table>
        <table><tr><th colspan='2'>Data Member Index</th></tr>
        <tr><td><a href="#_Count"><b>_Count</b></a></td><td>Stored count.</td><tr>
        <tr><td><a href="#_State"><b>_State</b></a></td><td>Stored state.</td><tr></table>
        """
        self.assertEqual(self.parse(raw), {
            ("property", "Visible"), ("property", "Color"), ("method", "Run"),
            ("data-member", "_Count"), ("data-member", "_State"),
        })

    def test_table_index_does_not_capture_enums_or_definition_cross_references(self):
        raw = """
        <table><tr><th>Method Index</th></tr>
        <tr><td><a href="#Run()"><b>Run</b></a>()</td><td>Runs the test.</td></tr></table>
        <table><tr><th>Enumeration Index</th></tr>
        <tr><td><a href="#Mode"><b>Mode</b></a></td><td>Mode enum.</td></tr></table>
        <h2>Methods</h2><dl><dt><a id="Run()"></a>Run
        <dd>See <a href="#Helper()"><b>Helper</b></a>()<dd>Not an index member.</dl>
        <table><tr><td><a href="#Other()"><b>Other</b></a>()</td><td>Example only.</td></tr></table>
        """
        self.assertEqual(self.parse(raw), {("method", "Run")})

    def test_table_index_reads_members_beyond_previous_size_limit(self):
        rows = [
            f'<tr><td><a href="#Run{i}()"><b>Run{i}</b></a>()</td>'
            f'<td>{"Synthetic summary. " * 80}</td></tr>'
            for i in range(100)
        ]
        raw = "<table><tr><th>Method Index</th></tr>" + "".join(rows) + "</table>"
        self.assertGreater(len(raw), 120000)
        self.assertEqual(self.parse(raw), {("method", f"Run{i}") for i in range(100)})

    def test_legacy_member_signature_and_summary_are_unchanged(self):
        raw = """
        <h2>Method Index</h2><dl>
        <dt>o <a href="#Create(CATItem&amp;)"><b>Create</b></a>(CATItem&amp;)
        <dd>Creates a <tt>test</tt> item.
        <dt>o <a href="#Create(int*)"><b>Create</b></a>(int*)
        <dd>Creates from a count pointer.</dl>
        <h2>Methods</h2><dl>
        <dt>See <a href="#Helper()"><b>Helper</b></a>()<dd>Not an index member.</dl>
        """
        self.parse(raw)
        members = {item["anchor"]: item for item in self.store.entities.values() if item["kind"] == "method"}
        self.assertEqual(set(members), {"Create(CATItem&amp;)", "Create(int*)"})
        self.assertEqual(members["Create(CATItem&amp;)"]["signature"], "(CATItem&)")
        self.assertEqual(members["Create(CATItem&amp;)"]["summary"], "Creates a test item.")


class InferLayerTests(unittest.TestCase):
    def test_refman_path_wins_over_automation_words_in_body(self):
        path = Path("CAADoc/Doc/generated/refman/GSMInterfaces/interface_CATIExample_1.htm")

        self.assertEqual(infer_layer(path, "HybridShapeFactory returns an object)"), "CAA-refman")

    def test_interfaces_path_is_automation(self):
        path = Path("CAADoc/Doc/generated/interfaces/GSMInterfaces/interface_HybridShape_1.htm")

        self.assertEqual(infer_layer(path, ""), "Automation")


if __name__ == "__main__":
    unittest.main()
