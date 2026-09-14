"""Offline known-query regression and warm in-process timing; no model/API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from .benchmark_agent_retrieval import load_baseline
from .caa_manual_query import configured_index


EXACT = ["CATGeoFactory", "CATTopology", "CATICGMSkinExtrapol", "CATICGMSkinExtrapolation",
         "CATCGMCreateSkinExtrapol", "CATBRepDecode", "CATIFeaturize", "CATCreateIntersection"]
EXTRAPOL = ["CATICGMSkinExtrapol", "CATICGMSkinExtrapolation", "CATCGMCreateSkinExtrapol",
            "CATCGMCreateSkinExtrapolation", "CATSkinExtrapol", "CATSkinExtrapolation", "CATIGSMExtrapol"]
SUITE = [("exact", name, [name]) for name in EXACT] + [
    ("member", "GetAllCells", ["CATTopology"]),
    ("member", "CreateBoundaryIterator", ["CATCell"]),
    ("member", "SetDefaultExtrapolationValue", ["CATICGMSkinExtrapol", "CATSkinExtrapol"]),
    ("member", "FeaturizeBorder", ["CATIFeaturize"]),
    ("member", "CreateExtrapol", ["CATIGSMFactory"]),
    ("member", "GetExtremities", ["CATWire"]),
    ("qualified", "CATTopology::GetAllCells", ["CATTopology"]),
    ("qualified", "CATBody::GetAllCells", ["CATTopology"]),
    ("qualified", "CATEdge::GetCurve", ["CATEdge"]),
    ("qualified", "CATIGSMFactory::CreateExtrapol", ["CATIGSMFactory"]),
    ("intent", "几何工厂", ["CATGeoFactory"]),
    *[("intent", term, EXTRAPOL) for term in ["曲面外插", "外插", "曲面取边后外插延伸", "skin extrapolation"]],
    ("intent", "boundary of the shell", ["CATICGMSkinExtrapol", "CATSkinExtrapol"]),
    ("stem", "extrapolat", EXTRAPOL), ("stem", "extrapol", EXTRAPOL),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="7c30b1469a7e04451159511511db94507e478765")
    parser.add_argument("--out", type=Path, default=Path("outputs/search-benchmark.json"))
    args = parser.parse_args()
    current = configured_index()
    baseline, _ = load_baseline(current.manual_root, args.baseline, current)
    report = {"baseline_commit": args.baseline, "repeats": 5, "warmup": 1,
              "db_sha256": hashlib.sha256(current.db_path.read_bytes()).hexdigest(),
              "query_sha256": hashlib.sha256((current.manual_root / "tools/caa_manual_query.py").read_bytes()).hexdigest(),
              "limitations": "Known diagnostic queries, not held-out. In-process warm timing; excludes CLI startup, model latency and raw-document retrieval.",
              "arms": {}}
    for label, index in [("baseline", baseline.index), ("current", current)]:
        searches, timings = [], []
        for category, query, expected in SUITE:
            result = index.search(query, limit=5)
            names = [row["name_en"] for row in result["candidate_groups"]]
            searches.append({"category": category, "query": query, "expected_any": expected,
                             "actual_top5": names, "hit_at_5": bool(set(names) & set(expected))})
        for query in EXACT:
            index.search(query, limit=5)
            samples = []
            for _ in range(5):
                start = time.perf_counter()
                index.search(query, limit=5)
                samples.append(round(1000 * (time.perf_counter() - start), 3))
            timings.append({"query": query, "samples_ms": samples, "median_ms": statistics.median(samples)})
        report["arms"][label] = {"searches": searches, "timings": timings,
                                  "hits": sum(row["hit_at_5"] for row in searches),
                                  "mean_query_median_ms": round(statistics.mean(row["median_ms"] for row in timings), 3)}
    report["source_phrase"] = {k: v for k, v in current.search_source("boundary of the shell").items()
                                if k not in {"results", "built_at"}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({arm: {"hits": data["hits"], "mean_query_median_ms": data["mean_query_median_ms"]}
                      for arm, data in report["arms"].items()}))


if __name__ == "__main__":
    main()
