"""Repeat read-only MCP queries concurrently and compare canonical responses."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import statistics
import time

try:
    from .caa_manual_mcp_server import McpServer
    from .caa_manual_query import configured_index
except ImportError:
    from caa_manual_mcp_server import McpServer
    from caa_manual_query import configured_index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, choices=range(1, 501), default=100)
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=8)
    args = parser.parse_args()
    index = configured_index()
    server = McpServer(index)
    cases = [
        ("exact", "caa_search", {"query": "CATGeoFactory"}, "ok"),
        ("spaced", "caa_search", {"query": "CATGeoFactory CreatePlane"}, "ok"),
        ("filtered", "caa_search", {"query": "CreatePlane", "framework": "GeometricObjects", "limit": 1}, "ok"),
        ("qualified_overloads", "caa_read_source", {"reference": "CATGeoFactory::CreatePlane"}, "ambiguous"),
        ("member", "caa_get_api", {"reference": "CATGeoFactory", "include": ["members"], "member": "CreatePlane"}, "ok"),
        ("example_page", "caa_get_api", {"reference": "CATGeoFactory", "include": ["examples"], "example_offset": 20}, "ok"),
        ("missing", "caa_read_source", {"reference": "CATGeoFactory::AutoHealEverything2026"}, "not_found"),
        ("invalid", "caa_search", {"query": "CreatePlane", "limit": "2"}, "error"),
    ]
    if index.caadoc_root:
        cases.extend([
            ("source", "caa_read_source", {"reference": "CATMathVector::Norm"}, "ok"),
            ("inheritance", "caa_read_source", {"reference": "CATBody::GetAllCells"}, "ok"),
        ])
    before = hashlib.sha256(index.db_path.read_bytes()).hexdigest()
    def call(case):
        start = time.perf_counter()
        result = server.call_tool(case[1], case[2])["structuredContent"]
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return {"case": case[0], "status": result.get("status"), "sha256": digest,
                "milliseconds": (time.perf_counter() - start) * 1000}
    expected = {case[0]: call(case) for case in cases}
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(call, cases * args.repeats))
    elapsed = time.perf_counter() - start
    after = hashlib.sha256(index.db_path.read_bytes()).hexdigest()
    failures = [row for row in results if row["sha256"] != expected[row["case"]]["sha256"]]
    status_failures = [case[0] for case in cases if expected[case[0]]["status"] != case[3]]
    times = sorted(row["milliseconds"] for row in results)
    report = {"repeats": args.repeats, "workers": args.workers, "calls": len(results),
              "case_ids": [case[0] for case in cases], "response_mismatches": len(failures),
              "unexpected_initial_status": status_failures, "db_unchanged": before == after,
              "db_sha256": after, "elapsed_seconds": round(elapsed, 3),
              "median_ms": round(statistics.median(times), 3), "p95_ms": round(times[int(0.95 * (len(times) - 1))], 3),
              "pass": not failures and not status_failures and before == after}
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
