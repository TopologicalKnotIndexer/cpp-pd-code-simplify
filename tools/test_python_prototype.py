#!/usr/bin/env python3
"""Focused tests for the Python prototype API and component accounting."""

from __future__ import annotations

import contextlib
import io
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
    cycle_code = simplify.simplify_pd_code(
        simplify.parse_pd_code((ROOT / "tests/fixtures/do_check_cycle_pd.txt").read_text())
    ).code
    cycle_diagram = simplify.Diagram(cycle_code)
    cycle_graph = simplify.DualGraph(cycle_diagram)
    cycle_red = simplify.possible_red_lines(cycle_diagram)[0]
    blocked_cycle_graph = simplify.clone_dual_graph(cycle_graph)
    for endpoint in cycle_red[1:-1]:
        right_region = blocked_cycle_graph.edge_to_face[endpoint.key]
        left_region = blocked_cycle_graph.edge_to_face[
            cycle_diagram.opposite(endpoint).key
        ]
        edge = blocked_cycle_graph.edge(right_region, left_region)
        if edge is not None:
            edge.weight = simplify.BLOCKED_WEIGHT
    cycle_green = [3, 0, 4, 5, 110, 109, 124, 112, 111, 115, 114, 46]
    cycle_witness = simplify.SimplificationResult(path_search_mode="heuristic")
    require(
        not simplify.do_check(
            cycle_diagram,
            blocked_cycle_graph,
            cycle_red,
            cycle_green,
            "right",
            cycle_witness,
        ),
        "Python do_check should reject repeated propagation states instead of looping",
    )
    canonical_regression = simplify.parse_pd_code(
        "[[3,88,4,1],[4,2,5,1],[5,2,6,3],[9,7,10,6],"
        "[10,7,11,8],[11,9,12,8],[15,12,16,13],[16,14,17,13],"
        "[17,14,18,15],[21,19,22,18],[22,25,23,26],[23,20,24,21],"
        "[24,20,25,19],[28,31,29,32],[32,27,33,28],[33,27,34,26],"
        "[34,29,35,30],[35,31,36,30],[36,39,37,40],[37,41,38,40],"
        "[38,41,39,42],[55,53,56,52],[56,53,57,54],[57,55,58,54],"
        "[61,50,62,51],[62,50,63,49],[64,47,65,48],[66,46,67,45],"
        "[68,64,69,63],[69,48,70,49],[70,65,71,66],[71,47,72,46],"
        "[72,68,73,67],[73,61,74,60],[74,59,75,60],[75,59,76,58],"
        "[76,51,77,52],[79,43,80,42],[81,44,82,45],[83,79,84,78],"
        "[84,77,85,78],[85,82,86,83],[86,44,87,43],[87,81,88,80]]"
    )
    canonical_result = simplify.reduce_pd_code(
        canonical_regression,
        max_thread=16,
    )
    require(
        canonical_result.to_json()["final_pd_code"] == "PD[]",
        "Python per-step canonicalization regression should reduce to PD[]",
    )
    require(
        canonical_result.crossingless_components == 1,
        "Python per-step canonicalization regression should preserve the unknot component",
    )
    step_stdout = io.StringIO()
    with contextlib.redirect_stdout(step_stdout):
        step_result = simplify.reduce_pd_code(same_face_green, show_step_pd=True)
    require(step_result.to_json()["final_pd_code"] == "PD[]", "step-output run should still simplify")
    require(
        "step_pd_code[1]: PD[X[1,2,2,1]]" in step_stdout.getvalue(),
        "Python show_step_pd should print the canonical PD code after applying a witness",
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
    finite_round_log = []
    stable_finite = simplify.reduce_pd_code(
        trefoil,
        max_paths=-1,
        reduction_round=1,
        max_thread=1,
        verbose=True,
        progress=finite_round_log.append,
    )
    require(
        stable_finite.mid_simplification_rounds == 0,
        "stable finite-round Python fixture should not apply a witness",
    )
    require(
        any("brute_fallback_start" in message for message in finite_round_log),
        "Python finite reduction rounds should still use brute fallback before stopping",
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
