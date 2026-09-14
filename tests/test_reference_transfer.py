import json
import unittest

from tools.benchmark_agent_stability import assess
from tools.benchmark_agent_transfer import paired_summary
from tools.benchmark_reference_transfer import references
from tools.summarize_reference_transfer import agent_summary, parameter_locator_calls


def sample(task, arm, passed=True, tokens=10):
    return {"task": task, "arm": arm, "variant": 0, "repeat": 0, "model_requested": "fixture",
            "completed": passed, "tool_calls": 1, "seconds": 1, "usage": {"total_tokens": tokens},
            "assessment": {key: passed for key in ("automation_pass", "fields_pass", "source_read_pass", "citation_pass", "json_valid", "pass")}}


class ReferenceTransferTests(unittest.TestCase):
    def test_pair_outcomes_and_cost_use_matching_successes(self):
        rows = [sample("both", "baseline"), sample("both", "candidate", tokens=5),
                sample("new", "baseline", False), sample("new", "candidate"),
                sample("old", "baseline"), sample("old", "candidate", False),
                sample("none", "baseline", False), sample("none", "candidate", False),
                sample("incomplete", "candidate")]
        result = paired_summary(rows)
        self.assertEqual(result["pair_outcomes"], {"both_pass": 1, "candidate_only": 1, "baseline_only": 1,
                                                   "both_fail": 1, "incomplete_pairs": 1})
        self.assertEqual(result["both_successful"]["baseline"]["total_tokens"], 10)
        self.assertEqual(result["both_successful"]["candidate"]["total_tokens"], 5)
        self.assertEqual(result["arms"]["candidate"]["samples"], 5)

    def test_locator_usage_excludes_uri_anchor_and_bare_name(self):
        trace = [{"arguments": value} for value in ({"reference": "Owner::Member()"},
                 {"query": "Owner：：Member（）"}, {"reference": "Owner::Member"},
                 {"reference": "caadoc://page.htm#Member(NS::Type&)"},
                 {"reference": "caadoc://page.htm", "anchor": "Member(NS::Type&)"})]
        self.assertEqual(parameter_locator_calls(trace), 2)

    def test_variants_preserve_words_and_entity_boundaries(self):
        case = {"owner": "Fixture", "name": "Get", "anchor": "Get(unsigned int,NS::Type&amp;)",
                "expected": ["member:one"], "name_expected": ["member:one"]}
        variants = {name: value for name, value, _ in references(case)}
        self.assertEqual(variants["canonical"], "Fixture::Get(unsigned int,NS::Type&)")
        self.assertIn("&amp;", variants["spaced_html"])
        self.assertIn("unsigned int", variants["spaced_html"])
        self.assertNotIn("& amp", variants["spaced_html"])

    def test_export_removes_answers_source_and_unexpected_metadata(self):
        case = {"expected": {"return_type": "void"}, "evidence_file": "fixture.htm", "read_markers": ["void Get"]}
        row = {**sample("fixture", "candidate"), "answer": '{"return_type":"void","evidence":["caadoc://fixture.htm"]}',
               "trace": [{"tool": "caa_read_source", "arguments": {"reference": "Fixture::Get()"},
                          "result": {"status": "ok", "source_uri": "caadoc://fixture.htm", "content": "void Get PRIVATE_SOURCE_SENTINEL"}}]}
        row["assessment"] = assess(case, row)
        row["usage"]["unexpected"] = "PRIVATE_USAGE_SENTINEL"
        data = {"runs": [row], "unexpected": "PRIVATE_CONFIG_SENTINEL"}
        result = agent_summary(data, {"fixture": case})
        text = json.dumps(result)
        self.assertNotIn("PRIVATE_", text)
        self.assertNotIn('"answer"', text)
        self.assertEqual(result["runs"][0]["parameter_locator_calls"], 1)
        row["assessment"]["automation_pass"] = False
        with self.assertRaises(ValueError):
            agent_summary(data, {"fixture": case})


if __name__ == "__main__":
    unittest.main()
