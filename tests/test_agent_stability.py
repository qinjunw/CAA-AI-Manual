import json
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tools.benchmark_agent_stability import assess, matches, parse_answer, run_case, TokenBudget
from tools.summarize_agent_stability import summarize


class AgentStabilityTests(unittest.TestCase):
    def setUp(self):
        self.case = {"expected": {"return_type": "const Fixture*", "ready": True},
                     "evidence_file": "fixture.htm", "read_markers": ["const Fixture", "Returns a fixture"]}
        self.answer = {"return_type": "const Fixture *", "ready": True, "evidence": ["caadoc://fixture.htm#Get"]}
        self.run = {"completed": True, "answer": json.dumps(self.answer), "trace": [
            {"tool": "caa_read_source", "arguments": {"reference": "caadoc://fixture.htm#Get"},
             "result": {"status": "ok", "source_uri": "caadoc://fixture.htm", "content": "const Fixture* Get(). Returns a fixture."}}]}

    def test_fenced_answer_separates_facts_from_machine_json(self):
        self.run["answer"] = "Explanation\n```json\n" + self.run["answer"] + "\n```\nMore text"
        result = assess(self.case, self.run)
        self.assertTrue(result["pass"])
        self.assertFalse(result["automation_pass"])
        self.assertFalse(result["json_valid"])

    def test_correct_fields_without_source_fail(self):
        self.run["trace"] = []
        result = assess(self.case, self.run)
        self.assertTrue(result["fields_pass"])
        self.assertFalse(result["pass"])

    def test_method_qualification_accepts_only_evidence_owner(self):
        self.case["expected"]["method"] = "Get"
        self.case["evidence_file"] = "interface_Fixture_123.htm"
        self.answer["method"] = "Fixture::Get"
        self.run["answer"] = json.dumps(self.answer)
        self.assertTrue(assess(self.case, self.run)["fields"]["method"])
        self.answer["method"] = "WrongOwner::Get"
        self.run["answer"] = json.dumps(self.answer)
        self.assertFalse(assess(self.case, self.run)["fields"]["method"])

    def test_wrong_types_citations_and_truncated_answers_fail(self):
        self.assertFalse(matches(1, True))
        self.assertFalse(matches("true", True))
        self.assertTrue(matches("const Fixture *", "const Fixture*"))
        self.answer["evidence"] = ["caadoc://invented.htm"]
        self.run["answer"] = json.dumps(self.answer)
        self.assertFalse(assess(self.case, self.run)["pass"])
        self.assertEqual(parse_answer('{"return_type":'), {})
        self.assertEqual(parse_answer("[]"), {})
        self.run["completed"] = False
        self.assertFalse(assess(self.case, self.run)["pass"])

    def test_missing_symbol_requires_actual_negative_lookup(self):
        case = {"expected": {"verified": False}, "trace_gate": "negative_lookup"}
        run = {"completed": True, "answer": '{"verified":false,"evidence":[]}', "trace": []}
        self.assertFalse(assess(case, run)["pass"])
        run["trace"] = [{"tool": "caa_search", "arguments": {"query": "AutoHealEverything2026"},
                         "result": {"status": "ok", "candidate_groups": []}}]
        self.assertTrue(assess(case, run)["pass"])

    def test_pagination_requires_both_pages_in_order(self):
        case = {"expected": {"file_at_offset_20": "caadoc://file.cpp"}, "trace_gate": "example_offset_20"}
        run = {"completed": True, "answer": '{"file_at_offset_20":"caadoc://file.cpp","evidence":["caadoc://file.cpp"]}', "trace": []}
        first = {"tool": "caa_get_api", "arguments": {}, "result": {"status": "ok", "examples": [{"source_uri": f"caadoc://f{i}.cpp"} for i in range(20)]}}
        second = {"tool": "caa_get_api", "arguments": {"example_offset": 20}, "result": {"status": "ok", "examples": [{"source_uri": "caadoc://file.cpp"}]}}
        run["trace"] = [second, first]
        self.assertFalse(assess(case, run)["pass"])
        run["trace"] = [first, second]
        self.assertTrue(assess(case, run)["pass"])
        second["result"]["examples"] = []
        self.assertFalse(assess(case, run)["pass"])

    def test_only_prompt_reaches_model_and_length_is_not_completion(self):
        response = {"model": "fixture", "usage": {"total_tokens": 9}, "choices": [
            {"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]}
        with patch("tools.benchmark_agent_stability.request_model", return_value=response) as request:
            result = run_case("unused-test-key", "fixture", "PROMPT_ONLY", Mock(), [], TokenBudget(100), 2, True)
        payload = request.call_args.args[1]
        self.assertNotIn("expected", json.dumps(payload))
        self.assertEqual(payload["messages"][1]["content"], "PROMPT_ONLY")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(result["completed"])

    def test_frozen_suite_has_distinct_cases_and_two_variants(self):
        suite = json.loads((Path(__file__).parent / "fixtures/agent_stability.json").read_text(encoding="utf-8"))
        self.assertEqual(len({case["id"] for case in suite["cases"]}), 12)
        self.assertEqual(sum(case["split"] == "holdout" for case in suite["cases"]), 4)
        for case in suite["cases"]:
            self.assertEqual(len(case["prompts"]), 2)
            self.assertTrue(case["expected"])

    def test_public_summary_drops_private_fields_and_source_text(self):
        run = {**self.run, "task": "fixture", "split": "development", "variant": 0, "repeat": 0,
               "model_requested": "fixture", "model": "fixture", "tool_calls": 1, "tool_errors": 0,
               "seconds": 1.0, "usage": {"total_tokens": 5}}
        run["trace"][0]["result"]["content"] += " PRIVATE_SOURCE_SENTINEL"
        data = {"label": "fixture", "unexpected_private_field": "PRIVATE_KEY_SENTINEL", "runs": [run]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            report = summarize(path, {"fixture": self.case})
        encoded = json.dumps(report)
        self.assertNotIn("PRIVATE_KEY_SENTINEL", encoded)
        self.assertNotIn("PRIVATE_SOURCE_SENTINEL", encoded)
        self.assertNotIn('"answer"', encoded)
        self.assertEqual(report["aggregate"]["automation_pass"], 1)


if __name__ == "__main__":
    unittest.main()
