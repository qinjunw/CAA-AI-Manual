"""Export stability measurements without answers, prompts, source text or keys."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics

from .benchmark_agent_stability import assess


def aggregate(runs):
    repeats = defaultdict(list)
    for row in runs:
        repeats[(row["task"], row["variant"], row["model_requested"])].append(row["assessment"]["automation_pass"])
    paired = [values for values in repeats.values() if len(values) >= 2]
    return {"samples": len(runs), "completed": sum(row["completed"] for row in runs),
            **{key: sum(row["assessment"][key] for row in runs) for key in
               ("fields_pass", "source_read_pass", "citation_pass", "json_valid", "pass", "automation_pass")},
            "repeat_groups": len(paired), "all_repeats_pass": sum(all(values) for values in paired),
            "mixed_repeat_outcomes": sum(len(set(values)) > 1 for values in paired),
            "total_tokens": sum(row["usage"].get("total_tokens", 0) for row in runs),
            "tool_calls": sum(row["tool_calls"] for row in runs),
            "median_case_seconds": round(statistics.median(row["seconds"] for row in runs), 3) if runs else 0}


def summarize(path, cases):
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for run in data["runs"]:
        row = {key: run.get(key) for key in ("task", "split", "variant", "repeat", "model_requested", "model",
                                             "completed", "tool_calls", "tool_errors", "seconds", "finish_reason", "error", "usage")}
        row["assessment"] = assess(cases[run["task"]], run)
        row["tool_statuses"] = dict(Counter(entry["result"].get("status", "unknown") for entry in run["trace"]))
        rows.append(row)
    models = sorted({row["model_requested"] for row in rows})
    allowed = {"label", "split", "max_rounds", "repeats", "json_mode", "tasks", "use_guide", "guide_sha256",
               "models_requested", "token_budget", "fingerprints", "db_sha256"}
    metadata = {key: value for key, value in data.items() if key in allowed}
    metadata["split_interpretation"] = ("Original fixture labels only; guided failure-subset results are targeted regression, not a new holdout."
                                        if data.get("use_guide") else "Fixture split at measurement time.")
    return {**metadata, "private_trace_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "aggregate": aggregate(rows), "by_model": {model: aggregate([row for row in rows if row["model_requested"] == model]) for model in models},
            "by_split": {split: aggregate([row for row in rows if row["split"] == split]) for split in ("development", "holdout")},
            "runs": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Output exists; choose a new path to preserve earlier measurements")
    root = Path(__file__).resolve().parents[1]
    suite = json.loads((root / "tests/fixtures/agent_stability.json").read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in suite["cases"]}
    phases = [summarize(path, cases) for path in args.inputs]
    report = {"schema_version": 1, "assessment_policy": "Frozen fields, whitespace-insensitive C++ types, exact or owner-qualified method names; all required source markers and cited page checks. Strict JSON scored separately from one extractable JSON code block.",
              "scope": suite["scope"], "grader_sha256": hashlib.sha256((root / "tools/benchmark_agent_stability.py").read_bytes()).hexdigest(),
              "phases": phases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({phase["label"]: {"aggregate": phase["aggregate"], "by_model": phase["by_model"], "by_split": phase["by_split"]} for phase in phases}, indent=2))


if __name__ == "__main__":
    main()
