"""Freeze and compare qualified member references across Framework groups.

Snapshots contain local tool source and stay in ignored outputs/. No model calls.
The indexed member ID is the oracle for locator identity, not C++ correctness.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import html
import json
from pathlib import Path
import re
import types

from .caa_manual_query import configured_index


SEED = "caa-reference-transfer-v1"
DEVELOPMENT = {"Mathematics", "GeometricObjects", "GMModelInterfaces", "GMOperatorsInterfaces",
               "GSMInterfaces", "PartInterfaces", "TopologicalOperators", "AdvancedTopologicalOpe"}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical_anchor(value):
    text = re.sub(r"\s+", " ", html.unescape(value)).strip()
    return re.sub(r"\s*([()<>:,*&\[\]])\s*", r"\1", text)


def freeze(index):
    with index._connection() as db:
        rows = [dict(row) for row in db.execute("""
            SELECT m.member_id, m.name, m.anchor, n.name_en AS owner, n.node_id,
                   n.framework, p.source_uri FROM api_members m
            JOIN api_pages p ON p.page_id=m.page_id
            JOIN catalog_nodes n ON n.node_id=p.api_node_id
            WHERE n.layer='CAA-refman' AND m.kind='method'
            ORDER BY n.framework, n.name_en, m.member_id
        """)]
    owners = defaultdict(set)
    groups = defaultdict(list)
    for row in rows:
        owners[row["owner"]].add(row["node_id"])
        groups[(row["node_id"], row["name"])].append(row)
    frameworks, excluded = defaultdict(list), Counter()
    for row in rows:
        if len(owners[row["owner"]]) != 1:
            excluded["owner_ambiguous"] += 1
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", row["name"], flags=re.ASCII):
            excluded["non_identifier"] += 1
            continue
        anchor = canonical_anchor(row["anchor"])
        if not anchor.startswith(row["name"] + "(") or not anchor.endswith(")"):
            excluded["unsupported_anchor"] += 1
            continue
        members = groups[(row["node_id"], row["name"])]
        expected = sorted(member["member_id"] for member in members if canonical_anchor(member["anchor"]) == anchor)
        frameworks[row["framework"]].append({**row, "expected": expected,
                                             "name_expected": sorted(member["member_id"] for member in members)})
    cases = []
    for framework, members in sorted(frameworks.items()):
        members.sort(key=lambda row: digest((SEED + row["member_id"]).encode()))
        for row in members[:8]:
            cases.append({**row, "split": "development" if framework in DEVELOPMENT else "holdout"})
    return {"seed": SEED, "excluded": dict(excluded), "eligible_members": sum(map(len, frameworks.values())),
            "db_sha256": digest(index.db_path.read_bytes()),
            "sampler_sha256": digest(Path(__file__).read_bytes()),
            "query_sha256": digest((index.manual_root / "tools/caa_manual_query.py").read_bytes()),
            "query_source": (index.manual_root / "tools/caa_manual_query.py").read_text(encoding="utf-8"), "cases": cases}


def baseline_index(snapshot, current):
    module = types.ModuleType("tools._reference_transfer_baseline")
    module.__file__, module.__package__ = str(current.manual_root / "tools/caa_manual_query.py"), "tools"
    exec(compile(snapshot["query_source"], module.__file__, "exec"), module.__dict__)
    return module.CaaManualIndex(current.db_path, current.manual_root, current.caadoc_root)


def references(case):
    anchor = canonical_anchor(case["anchor"])
    qualified = case["owner"] + "::" + anchor
    spaced = case["owner"] + " :: " + html.escape(re.sub(r"([(),*&])", r" \1 ", anchor), quote=False)
    fullwidth = qualified.translate(str.maketrans({"(": "（", ")": "）", ",": "，", ":": "："}))
    return [("canonical", qualified, case["expected"]), ("spaced_html", spaced, case["expected"]),
            ("fullwidth", fullwidth, case["expected"]),
            ("name_control", case["owner"] + "::" + case["name"], case["name_expected"]),
            ("negative", case["owner"] + "::" + case["name"] + "(__CAA_Invalid_Reference_Type_91c4__)", [])]


def selected_ids(result):
    if result.get("status") == "ok":
        return [result.get("selected_member_id", "")]
    if result.get("status") == "ambiguous":
        return sorted(row["member_id"] for row in result["candidates"])
    return []


def evaluate(snapshot, index, split):
    if digest(index.db_path.read_bytes()) != snapshot["db_sha256"]:
        raise ValueError("Database changed since freeze; create a new experiment")
    baseline = baseline_index(snapshot, index)
    cases = [row for row in snapshot["cases"] if row["split"] == split]
    measurements = []
    for case in cases:
        for variant, reference, expected in references(case):
            for arm, target in (("baseline", baseline), ("candidate", index)):
                result = target.get_api(reference)
                actual = sorted(selected_ids(result))
                expected_status = "not_found" if not expected else "ok" if len(expected) == 1 else "ambiguous"
                measurements.append({"member_id": case["member_id"], "framework": case["framework"],
                    "variant": variant, "arm": arm, "status": result.get("status"),
                    "pass": actual == expected and result.get("status") == expected_status,
                    "wrong_unique_selection": result.get("status") == "ok" and (not expected or actual != expected)})
    summary = {}
    for arm in ("baseline", "candidate"):
        summary[arm] = {variant: {"total": len(rows), "passed": sum(row["pass"] for row in rows),
                                "wrong_unique_selections": sum(row["wrong_unique_selection"] for row in rows)}
                        for variant in ("canonical", "spaced_html", "fullwidth", "name_control", "negative")
                        for rows in [[row for row in measurements if row["arm"] == arm and row["variant"] == variant]]}
    return {"split": split, "members": len(cases), "frameworks": len({row["framework"] for row in cases}),
            "evaluator_sha256": digest(Path(__file__).read_bytes()), "sampling_sha256": snapshot["sampler_sha256"],
            "db_sha256": snapshot["db_sha256"], "baseline_query_sha256": snapshot["query_sha256"],
            "candidate_query_sha256": digest((index.manual_root / "tools/caa_manual_query.py").read_bytes()),
            "summary": summary, "measurements": measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "evaluate"))
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    index = configured_index()
    if args.mode == "freeze":
        if args.snapshot.exists():
            parser.error("Snapshot exists; use a new filename")
        result, output = freeze(index), args.snapshot
        summary = {"cases": len(result["cases"]), "by_split": dict(Counter(row["split"] for row in result["cases"])),
                   "query_sha256": result["query_sha256"], "db_sha256": result["db_sha256"], "excluded": result["excluded"]}
    else:
        if not args.out or args.out.exists():
            parser.error("Use a new --out path to preserve earlier measurements")
        result = evaluate(json.loads(args.snapshot.read_text(encoding="utf-8")), index, args.split)
        output = args.out
        summary = {key: value for key, value in result.items() if key != "measurements"}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
