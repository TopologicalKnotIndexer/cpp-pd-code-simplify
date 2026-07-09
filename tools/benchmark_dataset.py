#!/usr/bin/env python3
"""Deterministic PD-code benchmark cases.

The default benchmark set is intentionally small enough to run in CI or on a
laptop, but it still covers several input shapes:

- small prime-knot seeds,
- a scalable T(2, n) torus-knot family,
- diagrams inflated by deterministic reverse Reidemeister-I moves, and
- the historical 31-crossing reference case used by this repository, and
- one hundred deterministic random samples extracted from a local PD-code
  corpus fixture.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mid_simplify_v5 as pysimplify  # noqa: E402


Crossing = Tuple[int, int, int, int]
PDCode = Tuple[Crossing, ...]
Endpoint = Tuple[int, int]


REFERENCE_31 = """PD[
X[15,7,16,6],X[7,15,8,14],X[18,61,19,0],X[20,12,21,11],
X[12,24,13,23],X[13,26,14,27],X[29,22,30,23],X[21,30,22,31],
X[28,33,29,34],X[5,36,6,37],X[8,36,9,35],X[34,27,35,28],
X[1,41,2,40],X[19,43,20,42],X[43,25,44,24],X[25,45,26,44],
X[16,45,17,46],X[37,46,38,47],X[48,39,49,40],X[0,50,1,49],
X[10,51,11,52],X[31,53,32,52],X[41,50,42,51],X[55,3,56,2],
X[54,9,55,10],X[53,33,54,32],X[3,57,4,56],X[57,5,58,4],
X[60,17,61,18],X[59,38,60,39],X[58,47,59,48]
]"""

RANDOM_FIXTURE = ROOT / "tests" / "benchmark_random_pd_codes.txt"
RANDOM_BENCHMARK_LIMIT = 100


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    family: str
    code: PDCode
    description: str

    @property
    def crossings(self) -> int:
        return len(self.code)

    @property
    def pd_text(self) -> str:
        return pysimplify.format_pd_code([tuple(crossing) for crossing in self.code])


def to_pd_code(code: Iterable[Sequence[int]]) -> PDCode:
    return tuple(tuple(int(value) for value in crossing) for crossing in code)  # type: ignore[return-value]


def max_label(code: PDCode) -> int:
    return max(label for crossing in code for label in crossing)


def label_endpoints(code: PDCode) -> Dict[int, List[Endpoint]]:
    labels: Dict[int, List[Endpoint]] = {}
    for crossing_index, crossing in enumerate(code):
        for strand_index, label in enumerate(crossing):
            labels.setdefault(label, []).append((crossing_index, strand_index))
    for label, endpoints in labels.items():
        if len(endpoints) != 2:
            raise ValueError(f"PD label {label} appears {len(endpoints)} times")
    return labels


def mate_endpoint(code: PDCode, endpoint: Endpoint) -> Endpoint:
    crossing_index, strand_index = endpoint
    label = code[crossing_index][strand_index]
    first, second = label_endpoints(code)[label]
    return second if first == endpoint else first


def apply_reverse_type_i(code: PDCode, endpoint: Endpoint, hand: int) -> PDCode:
    """Split one arc and add a removable Reidemeister-I crossing."""

    if not code:
        raise ValueError("Cannot inflate an empty PD code")

    first = endpoint
    second = mate_endpoint(code, first)
    first_label = max_label(code) + 1
    second_label = first_label + 1
    loop_label = first_label + 2

    mutable = [list(crossing) for crossing in code]
    mutable[first[0]][first[1]] = first_label
    mutable[second[0]][second[1]] = second_label
    if hand % 2 == 0:
        mutable.append([first_label, loop_label, loop_label, second_label])
    else:
        mutable.append([first_label, second_label, loop_label, loop_label])
    return to_pd_code(mutable)


def inflate_by_type_i(code: PDCode, moves: int, seed: int) -> PDCode:
    rng = random.Random(seed)
    result = code
    for _ in range(moves):
        endpoint = (rng.randrange(len(result)), rng.randrange(4))
        result = apply_reverse_type_i(result, endpoint, rng.randrange(2))
    return result


def torus_2_odd(n: int) -> PDCode:
    """Return a compact PD code for the prime T(2, n) torus knot, n odd."""

    if n < 3 or n % 2 == 0:
        raise ValueError("T(2, n) benchmark cases require odd n >= 3")
    crossings: List[Crossing] = [(2 * n - 2, 0, 1, 2 * n - 1)]
    for k in range(1, n):
        crossings.append((2 * k - 2, 2 * k, 2 * k + 1, 2 * k - 1))
    return tuple(crossings)


def build_original_cases() -> Tuple[BenchmarkCase, ...]:
    trefoil = to_pd_code([(1, 5, 2, 4), (3, 1, 4, 6), (5, 3, 6, 2)])
    figure_eight = to_pd_code(
        [(8, 3, 1, 4), (2, 6, 3, 5), (6, 2, 7, 1), (4, 7, 5, 8)]
    )
    cinquefoil = torus_2_odd(5)
    torus_7 = torus_2_odd(7)
    torus_9 = torus_2_odd(9)
    reference_31 = to_pd_code(pysimplify.parse_pd_code(REFERENCE_31))

    cases = [
        BenchmarkCase(
            "3_1_trefoil",
            "prime seed",
            trefoil,
            "Three-crossing trefoil seed.",
        ),
        BenchmarkCase(
            "4_1_figure_eight",
            "prime seed",
            figure_eight,
            "Four-crossing figure-eight seed.",
        ),
        BenchmarkCase(
            "5_1_torus",
            "torus family",
            cinquefoil,
            "Five-crossing T(2, 5) torus-knot seed.",
        ),
        BenchmarkCase(
            "7_1_torus",
            "torus family",
            torus_7,
            "Seven-crossing T(2, 7) torus-knot input.",
        ),
        BenchmarkCase(
            "9_1_torus",
            "torus family",
            torus_9,
            "Nine-crossing T(2, 9) torus-knot input.",
        ),
        BenchmarkCase(
            "trefoil_r1x12",
            "inflated",
            inflate_by_type_i(trefoil, moves=12, seed=3101),
            "Trefoil with twelve deterministic reverse type-I moves.",
        ),
        BenchmarkCase(
            "figure_eight_r1x12",
            "inflated",
            inflate_by_type_i(figure_eight, moves=12, seed=4101),
            "Figure-eight knot with twelve deterministic reverse type-I moves.",
        ),
        BenchmarkCase(
            "reference_31",
            "reference hard case",
            reference_31,
            "Historical 31-crossing reference input bundled with the project.",
        ),
    ]

    return tuple(cases)


def load_random_cases() -> Tuple[BenchmarkCase, ...]:
    cases: List[BenchmarkCase] = []
    if RANDOM_FIXTURE.exists():
        for job in pysimplify.read_pd_file(str(RANDOM_FIXTURE)):
            name = job.label.split(":")[-1]
            cases.append(
                BenchmarkCase(
                    name,
                    "zip random sample",
                    to_pd_code(job.code),
                    "Deterministic random sample from the local pd_code.zip corpus.",
                )
            )
    return tuple(cases[:RANDOM_BENCHMARK_LIMIT])


ORIGINAL_BENCHMARK_CASES = build_original_cases()
RANDOM_BENCHMARK_CASES = load_random_cases()
BENCHMARK_CASES = ORIGINAL_BENCHMARK_CASES + RANDOM_BENCHMARK_CASES


def case_names() -> List[str]:
    return [case.name for case in BENCHMARK_CASES]


def cases_by_name() -> Dict[str, BenchmarkCase]:
    return {case.name: case for case in BENCHMARK_CASES}


if __name__ == "__main__":
    for case in BENCHMARK_CASES:
        print(f"{case.name}: {case.crossings} crossings")
        print(case.pd_text)
