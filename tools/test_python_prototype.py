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

    after = simplify.analyze_components_after_removing_crossings(trefoil, [0, 1, 2])
    require(after.components_with_crossings == 0, "removed trefoil should have no crossing-bearing components")
    require(after.crossingless_components == 1, "removed trefoil should preserve one crossingless component")
    require(after.total_components == 1, "removed trefoil should preserve total component count")

    jobs = simplify.parse_pd_document("unknot: PD[]", "case")
    require(len(jobs) == 1, "PD[] document should produce one job")
    require(jobs[0].implied_crossingless_components == 1, "PD[] should imply one crossingless component")
    result, components, _ = simplify.run_job(jobs[0])
    require(not result.found, "PD[] should not have a simplification witness")
    require(components.crossingless_components == 1, "PD[] job should keep one crossingless component")
    require(components.total_components == 1, "PD[] job should report one total component")

    kink = simplify.parse_pd_code("PD[X[0,0,1,1]]")
    kink_after = simplify.analyze_components_after_removing_crossings(kink, [0])
    require(kink_after.crossingless_components == 1, "removing a one-crossing kink should leave one component")

    print("Python prototype tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
