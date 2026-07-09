#!/usr/bin/env python3
"""Focused tests for the Python prototype API and component accounting."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mid_simplify_v5 as simplify  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    trefoil = simplify.parse_pd_code(
        "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
    )
    analysis = simplify.analyze_components(trefoil)
    require(analysis.total_components == 1, "trefoil should have one component")

    zero_based_trefoil = simplify.parse_pd_code(
        "PD[X[0,4,1,3],X[2,0,3,5],X[4,2,5,1]]"
    )
    zero_based_result = simplify.reduce_pd_code(
        zero_based_trefoil,
        reduction_round=0,
    )
    require(
        zero_based_result.to_json()["final_pd_code"]
        == "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]",
        "final Python JSON PD code should be one-based",
    )
    require(
        simplify.format_final_pd_code(zero_based_result.code)
        == "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]",
        "final Python text formatter should be one-based",
    )

    orientation_repair = simplify.parse_pd_code(
        "PD[X[1,6,2,7],X[9,4,10,5],X[8,1,7,10],X[6,3,5,2],X[4,9,3,8]]"
    )
    require(
        simplify.format_final_pd_code(orientation_repair)
        == "PD[X[1,6,2,7],X[3,8,4,9],X[5,2,6,3],X[7,10,8,1],X[9,4,10,5]]",
        "final Python formatter should repair local crossing orientation and sort rows",
    )

    same_face_green = simplify.parse_pd_code(
        "PD[X[1,5,2,4],X[2,5,3,6],X[6,3,1,4]]"
    )
    same_face_witness = simplify.find_simplification(same_face_green)
    require(same_face_witness.found, "Python should find same-face green path witness")
    same_face_result = simplify.reduce_pd_code(same_face_green)
    require(
        same_face_result.to_json()["final_pd_code"] == "PD[]",
        "Python same-face green path unknot should reduce to PD[]",
    )
    require(
        same_face_result.crossingless_components == 1,
        "Python same-face green path unknot should preserve one crossingless component",
    )

    progress_log = []
    simplify.reduce_pd_code(
        trefoil,
        max_paths=-1,
        ban_heuristic=True,
        reduction_round=1,
        max_thread=-1,
        verbose=True,
        progress=progress_log.append,
    )
    require(
        any("bruteforce_threads max_thread=-1" in message for message in progress_log),
        "Python verbose auto-thread log should be emitted in brute-force mode",
    )
    require(
        any("actual_threads=" in message for message in progress_log),
        "Python verbose auto-thread log should include the actual worker count",
    )
    timed_result = simplify.reduce_pd_code(
        trefoil,
        timeout=1,
        _timeout_deadline=0.0,
    )
    require(timed_result.timed_out, "expired Python timeout deadline should return timed-out result")
    require(len(timed_result.code) == len(trefoil), "timed-out Python result should keep a PD code")
    try:
        simplify.reduce_pd_code(trefoil, timeout=0)
    except ValueError:
        pass
    else:
        raise AssertionError("timeout=0 should be rejected")

    after = simplify.analyze_components_after_removing_crossings(trefoil, [0, 1, 2])
    require(after.components_with_crossings == 0, "removed trefoil should have no crossing-bearing components")
    require(after.crossingless_components == 1, "removed trefoil should preserve one crossingless component")
    require(after.total_components == 1, "removed trefoil should preserve total component count")

    jobs = simplify.parse_pd_document("unknot: PD[]", "case")
    require(len(jobs) == 1, "PD[] document should produce one job")
    require(jobs[0].implied_crossingless_components == 1, "PD[] should imply one crossingless component")
    result, components, _ = simplify.run_job(jobs[0])
    require(result.code == [], "PD[] should simplify to empty PD code")
    require(result.mid_simplification_rounds == 0, "PD[] should need no mid-simplification rounds")
    require(result.crossingless_components == 1, "PD[] result should keep one crossingless component")
    require(components.crossingless_components == 1, "PD[] job should keep one crossingless component")
    require(components.total_components == 1, "PD[] job should report one total component")

    kink = simplify.parse_pd_code("PD[X[0,0,1,1]]")
    kink_after = simplify.analyze_components_after_removing_crossings(kink, [0])
    require(kink_after.crossingless_components == 1, "removing a one-crossing kink should leave one component")

    batch = simplify.parse_pd_document(
        "bad: PD[X[1,2,3,4]]\n"
        "good: PD[]\n",
        "case",
    )
    require(len(batch) == 2, "batch parser should keep both invalid and valid jobs")
    try:
        simplify.run_job(batch[0])
    except ValueError:
        pass
    else:
        raise AssertionError("invalid batch job should report an isolated error")
    good_result, good_components, _ = simplify.run_job(batch[1])
    require(good_result.code == [], "valid batch job should still produce final PD code")
    require(good_components.crossingless_components == 1, "valid job after an invalid one should still run")

    print("Python prototype tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
