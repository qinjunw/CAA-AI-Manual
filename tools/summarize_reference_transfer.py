"""Export paired locator measurements without answers or official source text."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import unicodedata

from .benchmark_agent_stability import assess
from .benchmark_agent_transfer import paired_summary
from .benchmark_reference_transfer import canonical_anchor
from .caa_manual_query import configured_index


def pick(value, keys):
    return {key: value[key] for key in keys if key in value}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parameter_locator_calls(trace):
    count = 0
    for entry in trace:
        arguments = entry.get("arguments", {})
        reference = unicodedata.normalize("NFKC", str(arguments.get("reference", arguments.get("query", ""))))
        count += "::" in reference and "(" in reference and "://" not in reference
    return count


def agent_summary(data, cases):
    runs = []
    for run in data["runs"]:
        row = pick(run, ("task", "framework", "split", "variant", "repeat", "arm", "model_requested", "model",
                         "completed", "tool_calls", "tool_errors", "seconds"))
        row["usage"] = pick(run["usage"], ("prompt_tokens", "completion_tokens", "total_tokens",
                                          "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"))
        row["assessment"] = assess(cases[run["task"]], run)
        if row["assessment"] != run["assessment"]:
            raise ValueError("Assessment differs from the recorded run; preserve the original grader")
        row["parameter_locator_calls"] = parameter_locator_calls(run["trace"])
        runs.append(row)
    metadata = pick(data, ("label", "db_sha256", "baseline_query_sha256", "candidate_query_sha256",
                           "fixture_sha256", "harness_sha256", "tool_definitions_sha256", "models_requested",
                           "repeats", "json_mode", "use_guide", "max_rounds", "max_output_tokens", "token_budget"))
    return {**metadata, "summary": paired_summary(runs),
            "by_model": {model: paired_summary([row for row in runs if row["model_requested"] == model])
                         for model in sorted({row["model_requested"] for row in runs})},
            "runs": runs}


def source_check(snapshot, index):
    # This is a content-equivalence check, not another model evaluation. The
    # seed and sample size are fixed independently of lookup success.
    seed = "reference-source-check-v1"
    cases = sorted((row for row in snapshot["cases"] if row["split"] == "holdout"),
                   key=lambda row: hashlib.sha256((seed + row["member_id"]).encode()).hexdigest())[:32]
    rows = []
    keys = ("status", "source_uri", "anchor", "anchor_found", "content", "total_chars", "next_offset")
    for case in cases:
        row = pick(case, ("member_id", "framework"))
        try:
            by_id = index.read_source(case["member_id"], max_chars=1000)
            by_reference = index.read_source(case["owner"] + "::" + canonical_anchor(case["anchor"]), max_chars=1000)
            row["pass"] = (by_id.get("status") == "ok" and bool(by_id.get("content"))
                           and pick(by_id, keys) == pick(by_reference, keys))
            row["content_sha256"] = hashlib.sha256(by_id.get("content", "").encode()).hexdigest()
        except (OSError, ValueError) as error:
            row.update({"pass": False, "error_type": type(error).__name__})
        rows.append(row)
    return {"seed": seed, "max_chars": 1000, "samples": len(rows), "passed": sum(row["pass"] for row in rows),
            "frameworks": len({row["framework"] for row in rows}), "measurements": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Output exists; choose a new path to preserve earlier measurements")
    index = configured_index()
    load = lambda path: json.loads(path.read_text(encoding="utf-8"))
    snapshot, agent = load(args.snapshot), load(args.agent)
    if digest(index.db_path) != snapshot["db_sha256"] or digest(index.manual_root / "tools/caa_manual_query.py") != agent["candidate_query_sha256"]:
        parser.error("Database or candidate changed; cannot attach source checks to these measurements")
    fixture = index.manual_root / "tests/fixtures/reference_transfer_agent.json"
    if digest(fixture) != agent["fixture_sha256"]:
        parser.error("Fixture changed since the model evaluation")
    cases = {case["id"]: case for case in load(fixture)["cases"]}
    offline = []
    for path in (args.development, args.holdout):
        data = load(path)
        if any(data[key] != agent[key] for key in ("db_sha256", "baseline_query_sha256", "candidate_query_sha256")):
            parser.error("Measurement inputs use different implementation or database versions")
        row = pick(data, ("split", "members", "frameworks", "evaluator_sha256", "sampling_sha256", "db_sha256",
                          "baseline_query_sha256", "candidate_query_sha256", "summary"))
        row["private_measurement_sha256"] = digest(path)
        row["failures"] = [pick(item, ("member_id", "framework", "variant", "arm", "status", "pass", "wrong_unique_selection"))
                           for item in data["measurements"] if not item["pass"] or item["wrong_unique_selection"]]
        row["sampled_member_ids"] = sorted({item["member_id"] for item in data["measurements"]})
        offline.append(row)
    result = {"schema_version": 1, "scope": "Indexed ordinary-method identity and eight declaration-reading tasks; no C++ or CATIA execution.",
              "grader_sha256": digest(index.manual_root / "tools/benchmark_agent_stability.py"),
              "exporter_sha256": digest(Path(__file__)), "private_snapshot_sha256": digest(args.snapshot),
              "sampling": pick(snapshot, ("seed", "excluded", "eligible_members")),
              "offline": offline, "source_check": source_check(snapshot, index),
              "agent": {**agent_summary(agent, cases), "private_trace_sha256": digest(args.agent)}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"source_check": pick(result["source_check"], ("samples", "passed", "frameworks")),
                      "pair_outcomes": result["agent"]["summary"]["pair_outcomes"],
                      "parameter_locator_calls": sum(row["parameter_locator_calls"] for row in result["agent"]["runs"])}))


if __name__ == "__main__":
    main()
