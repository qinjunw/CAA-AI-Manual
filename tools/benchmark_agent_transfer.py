"""Paired model check of one frozen query implementation change.

Both arms use identical prompts, tool definitions and model settings. Raw traces
stay in outputs/. No fixture expected values or snapshot code reach the model.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re

from .benchmark_agent_stability import run_case, assess, TokenBudget
from .benchmark_reference_transfer import baseline_index
from .caa_manual_query import configured_index
from .caa_manual_mcp_server import McpServer, tool_defs
from .summarize_agent_stability import aggregate


def paired_summary(runs):
    pairs = {}
    for row in runs:
        pairs.setdefault((row["task"], row["model_requested"], row["repeat"]), {})[row["arm"]] = row
    counts = {"both_pass": 0, "candidate_only": 0, "baseline_only": 0, "both_fail": 0, "incomplete_pairs": 0}
    common = {"baseline": [], "candidate": []}
    for pair in pairs.values():
        if set(pair) != {"baseline", "candidate"}:
            counts["incomplete_pairs"] += 1
            continue
        old, new = (pair[arm]["assessment"]["automation_pass"] for arm in ("baseline", "candidate"))
        counts["both_pass" if old and new else "candidate_only" if new else "baseline_only" if old else "both_fail"] += 1
        if old and new:
            for arm in common:
                common[arm].append(pair[arm])
    return {"pair_outcomes": counts, "arms": {arm: aggregate([row for row in runs if row["arm"] == arm]) for arm in common},
            "both_successful": {arm: aggregate(rows) for arm, rows in common.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--models", nargs="+", choices=("deepseek-flash", "deepseek-v4-pro"), default=["deepseek-flash", "deepseek-v4-pro"])
    parser.add_argument("--repeats", type=int, choices=(1, 2), default=2)
    parser.add_argument("--token-budget", type=int, default=900000)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label) or not 1000 <= args.token_budget <= 1500000:
        parser.error("Use an alphanumeric label and token budget between 1000 and 1500000")
    index = configured_index()
    root = index.manual_root
    output = root / f"outputs/{args.label}.json"
    if output.exists():
        parser.error("Output exists; choose a new label")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if hashlib.sha256(index.db_path.read_bytes()).hexdigest() != snapshot["db_sha256"]:
        parser.error("Database differs from frozen baseline")
    baseline = baseline_index(snapshot, index)
    fixture_path = root / "tests/fixtures/reference_transfer_agent.json"
    suite = json.loads(fixture_path.read_text(encoding="utf-8"))
    config = json.loads(args.credentials.read_text(encoding="utf-8-sig"))
    key = next(value["testingAPIKey"] for value in config.values() if isinstance(value, dict) and value.get("testingAPIKey"))
    del config
    definitions = [row for row in tool_defs() if row["name"] != "caa_status"]
    budget = TokenBudget(args.token_budget)
    results = {"label": args.label, "db_sha256": snapshot["db_sha256"], "baseline_query_sha256": snapshot["query_sha256"],
               "candidate_query_sha256": hashlib.sha256((root / "tools/caa_manual_query.py").read_bytes()).hexdigest(),
               "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
               "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "tool_definitions_sha256": hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest(),
               "models_requested": args.models, "repeats": args.repeats, "json_mode": True, "use_guide": False,
               "max_rounds": 6, "max_output_tokens": 1200, "token_budget": args.token_budget, "runs": []}
    def run_pair(case, model, repeat, position):
        prompt = suite["prompt_template"].format(reference=case["reference"])
        arms = [("baseline", baseline), ("candidate", index)]
        if (position + repeat) % 2:
            arms.reverse()
        rows = []
        for arm, target in arms:
            run = run_case(key, model, prompt, McpServer(target), definitions, budget, 6, True)
            rows.append({"task": case["id"], "framework": case["framework"], "split": "holdout", "variant": 0,
                         "repeat": repeat, "arm": arm, **run, "assessment": assess(case, run)})
        return rows
    jobs = [(case, model, repeat, position) for repeat in range(args.repeats)
            for position, case in enumerate(suite["cases"]) for model in dict.fromkeys(args.models)]
    output.parent.mkdir(exist_ok=True)
    # Each pair is sequential; two independent pairs may be in flight. Budget
    # checks happen before every request, so queued pairs cannot bypass the cap.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_pair, *job) for job in jobs]
        for future in as_completed(futures):
            rows = future.result()
            results["runs"].extend(rows)
            results["summary"] = paired_summary(results["runs"])
            output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps([{key: row[key] for key in ("task", "model_requested", "repeat", "arm", "completed", "tool_calls", "assessment", "usage")} for row in rows]), flush=True)


if __name__ == "__main__":
    main()
