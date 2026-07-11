"""Pure Python PD-code mid-simplification prototype.

This module is the cleaned-up Python counterpart of the C++ implementation.
It exposes both a Python API and a command-line interface using the same
PD-code input style as the project executable and `cppkh`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import heapq
import json
import multiprocessing
import os
import re
import sys
import time
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple


PDCode = List[Tuple[int, int, int, int]]
BLOCKED_WEIGHT = 10_000
HEURISTIC_BEAM_WIDTH = 8
HEURISTIC_MIN_STATE_BUDGET = 128
HEURISTIC_MAX_STATE_BUDGET = 4096
HEURISTIC_MIN_PATH_BUDGET = 24
HEURISTIC_MAX_PATH_BUDGET = 384
HEURISTIC_BEST_LOOKAHEAD_BATCHES = 8
HEURISTIC_PARALLEL_MIN_CROSSINGS = 500
R3_FAILOVER_MAX_DEPTH = 8
R3_FAILOVER_MAX_STATES = 4096
R3_FAILOVER_TIME_SLICE_SECONDS = 30
R3_PREPASS_MAX_DEPTH = 4
R3_PREPASS_MAX_STATES = 256
R3_PREPASS_TIME_SLICE_SECONDS = 15
MID_SEARCH_TIME_SLICE_SECONDS = 20
NON_MONOTONE_TIME_SLICE_SECONDS = 60
NON_MONOTONE_MAX_RED_LENGTH = 80
NON_MONOTONE_MAX_DEPTH = 72
NON_MONOTONE_BEAM_WIDTH = 32
NON_MONOTONE_MAX_CANDIDATES_PER_STATE = 96
NON_MONOTONE_MAX_CANDIDATES_PER_LENGTH = 4
NON_MONOTONE_MAX_RED_SCANS_PER_LENGTH = 48
NON_MONOTONE_MAX_RED_TESTS_PER_NODE = 64
NON_MONOTONE_EXTRA_CROSSINGS = 2
NON_MONOTONE_MAX_TOTAL_INCREASE = 14
NON_MONOTONE_R3_MOVES_PER_STATE = 16
NON_MONOTONE_HEURISTIC_STATE_BUDGET = 384
NON_MONOTONE_HEURISTIC_PATH_BUDGET = 8
NON_MONOTONE_MAX_GREEN_TESTS_PER_STATE = 4096
NON_MONOTONE_MAX_TOTAL_GREEN_TESTS = 4_000_000
UINT64_MASK = (1 << 64) - 1


class TeeTextIO:
    def __init__(self, primary: Any, log_file: Path, lock: threading.RLock):
        self._primary = primary
        self._log_file = log_file
        self._lock = lock

    def write(self, text: str) -> int:
        with self._lock:
            written = self._primary.write(text)
            self._primary.flush()
            with self._log_file.open("a", encoding="utf-8") as backup:
                backup.write(text)
                backup.flush()
        return written

    def flush(self) -> None:
        with self._lock:
            self._primary.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


@contextmanager
def tee_standard_streams(log_file: Optional[str]):
    if not log_file:
        yield
        return
    lock = threading.RLock()
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    log_path = Path(log_file)
    log_path.write_text("", encoding="utf-8")
    sys.stdout = TeeTextIO(original_stdout, log_path, lock)  # type: ignore[assignment]
    sys.stderr = TeeTextIO(original_stderr, log_path, lock)  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = original_stdout
        sys.stderr = original_stderr


def log_file_arg(argv: Sequence[str]) -> Optional[str]:
    log_file: Optional[str] = None
    index = 0
    while index < len(argv):
        if argv[index] == "--log-file":
            if index + 1 >= len(argv):
                raise ValueError("--log-file requires a file path")
            log_file = argv[index + 1]
            index += 2
        elif argv[index].startswith("--log-file="):
            log_file = argv[index].split("=", 1)[1]
            if not log_file:
                raise ValueError("--log-file requires a file path")
            index += 1
        else:
            index += 1
    return log_file


def local_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_progress_log(message: str) -> str:
    return f"[pdcode-simplify {local_timestamp()}] {message}"


class PdCodeSimplifyTimeoutError(RuntimeError):
    pass


DEFAULT_BRUTEFORCE_BUDGET = 200_000
REAPR_WARNING = (
    "WARNING: --reapr uses a deterministic internal reembedding/projection oracle "
    "guarded by component count, Alexander determinant, and finite-field "
    "Alexander root checks. The resulting PD code may still represent "
    "a different knot or link; verify additional invariants independently."
)
_ALEXANDER_FINGERPRINT_PRIMES = (1_000_003, 1_000_033, 1_000_037)
_SIMPLIFICATION_SEARCH_CACHE: Dict[Tuple[str, int, bool, bool, int, int], SimplificationResult] = {}
_NON_MONOTONE_CACHE: Dict[Tuple[str, int], NonMonotoneSearchResult] = {}


def validate_timeout(timeout: int) -> None:
    if timeout < -1 or timeout == 0:
        raise ValueError("timeout must be -1 or a positive integer")


def validate_bruteforce_budget(bruteforce_budget: int) -> None:
    if bruteforce_budget < -1 or bruteforce_budget == 0:
        raise ValueError("bruteforce_budget must be -1 or a positive integer")


@dataclass
class BruteForceBudget:
    limit: int = DEFAULT_BRUTEFORCE_BUDGET
    used: int = 0
    exhausted: bool = False

    def take(self) -> bool:
        if self.limit < 0:
            return True
        if self.used >= self.limit:
            self.exhausted = True
            return False
        self.used += 1
        return True


def timeout_deadline(timeout: int, existing: Optional[float] = None) -> Optional[float]:
    validate_timeout(timeout)
    if timeout > 0 and existing is None:
        return time.monotonic() + timeout
    return existing


def check_timeout(timeout: int, deadline: Optional[float]) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise PdCodeSimplifyTimeoutError(f"timeout after {timeout} seconds")


def remaining_timeout_seconds(timeout: int, deadline: Optional[float]) -> Optional[float]:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PdCodeSimplifyTimeoutError(f"timeout after {timeout} seconds")
    return remaining


def timeout_expired(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def with_time_slice(
    timeout: int,
    deadline: Optional[float],
    seconds: int,
) -> Tuple[int, Optional[float]]:
    soft_deadline = time.monotonic() + seconds
    if deadline is None or soft_deadline < deadline:
        return seconds, soft_deadline
    return timeout, deadline


@dataclass(frozen=True, order=True)
class Endpoint:
    crossing: int
    strand: int

    @property
    def key(self) -> int:
        return self.crossing * 4 + self.strand

    @staticmethod
    def from_key(key: int) -> "Endpoint":
        return Endpoint(key // 4, key % 4)


@dataclass
class GreenCrossing:
    from_face: int
    to_face: int
    strand_level: str

    def to_json(self) -> Dict[str, object]:
        return {
            "from_face": self.from_face,
            "to_face": self.to_face,
            "strand_level": self.strand_level,
        }


@dataclass
class ComponentSummary:
    crossing_indices: List[int]


@dataclass
class ComponentAnalysis:
    components: List[ComponentSummary] = field(default_factory=list)
    crossingless_components: int = 0

    @property
    def components_with_crossings(self) -> int:
        return len(self.components)

    @property
    def total_components(self) -> int:
        return self.components_with_crossings + self.crossingless_components

    def to_json(self) -> Dict[str, int]:
        return {
            "components_with_crossings": self.components_with_crossings,
            "crossingless_components": self.crossingless_components,
            "total_components": self.total_components,
        }


@dataclass
class SimplificationResult:
    found: bool = False
    direction: str = "left"
    path_search_mode: str = ""
    red_path: List[Endpoint] = field(default_factory=list)
    green_path: List[int] = field(default_factory=list)
    green_crossings: List[GreenCrossing] = field(default_factory=list)
    tested_red_paths: int = 0
    tested_green_paths: int = 0
    resource_limited: bool = False
    crossing_reduction: int = 0
    resulting_crossings: int = sys.maxsize

    def to_json(
        self,
        input_components: Optional[ComponentAnalysis] = None,
        after_removal_components: Optional[ComponentAnalysis] = None,
        pd_simplification: Optional[PDSimplificationResult] = None,
        search_components: Optional[ComponentAnalysis] = None,
        label: Optional[str] = None,
    ) -> Dict[str, object]:
        data: Dict[str, object] = {}
        if label is not None:
            data["label"] = label
        data["simplification_found"] = self.found
        if input_components is not None:
            data["input_components"] = input_components.to_json()
        if after_removal_components is not None:
            data["after_removal_components"] = after_removal_components.to_json()
        if pd_simplification is not None and search_components is not None:
            data["pd_simplification"] = pd_simplification.to_json()
            data["search_components"] = search_components.to_json()
        data["tested_red_paths"] = self.tested_red_paths
        data["tested_green_paths"] = self.tested_green_paths
        data["resource_limited"] = self.resource_limited
        data["path_search_mode"] = self.path_search_mode
        if self.found:
            data["direction"] = self.direction
            data["red_path"] = [
                {"crossing": endpoint.crossing, "strand": endpoint.strand}
                for endpoint in self.red_path
            ]
            data["green_path"] = list(self.green_path)
            data["green_crossings"] = [
                crossing.to_json() for crossing in self.green_crossings
            ]
        return data


@dataclass
class RedPathSearchOutcome:
    completed: bool = False
    skipped: bool = False
    found: bool = False
    resource_limited: bool = False
    tested_green_paths: int = 0
    witness: SimplificationResult = field(default_factory=SimplificationResult)


@dataclass
class ReductionResult:
    code: PDCode
    crossingless_components: int = 0
    mid_simplification_rounds: int = 0
    heuristic_failover_rounds: int = 0
    reidemeister_i_moves: int = 0
    reidemeister_ii_moves: int = 0
    reidemeister_iii_moves: int = 0
    nugatory_crossing_moves: int = 0
    tested_red_paths: int = 0
    tested_green_paths: int = 0
    last_path_search_mode: str = ""
    reapr_used: bool = False
    reapr_rejected: bool = False
    reapr_rounds: int = 0
    reapr_attempts: int = 0
    reapr_status: str = ""
    reapr_warning: str = ""
    alexander_determinant_before: str = ""
    alexander_determinant_after: str = ""
    reapr_invariants_before: str = ""
    reapr_invariants_after: str = ""
    stopped_by_round_limit: bool = False
    stopped_by_crossing_limit: bool = False
    timed_out: bool = False
    resource_limited: bool = False

    def to_json(
        self,
        input_components: Optional[ComponentAnalysis] = None,
        after_removal_components: Optional[ComponentAnalysis] = None,
        final_components: Optional[ComponentAnalysis] = None,
        label: Optional[str] = None,
    ) -> Dict[str, object]:
        data: Dict[str, object] = {}
        if label is not None:
            data["label"] = label
        data["simplification_found"] = (
            self.mid_simplification_rounds > 0
            or self.reidemeister_i_moves > 0
            or self.reidemeister_ii_moves > 0
            or self.reidemeister_iii_moves > 0
            or self.nugatory_crossing_moves > 0
            or self.reapr_used
        )
        if input_components is not None:
            data["input_components"] = input_components.to_json()
        if after_removal_components is not None:
            data["after_removal_components"] = after_removal_components.to_json()
        data.update({
            "final_pd_code": format_final_pd_code(self.code),
            "final_crossings": len(self.code),
            "final_components": (
                final_components.to_json()
                if final_components is not None
                else analyze_components(self.code, self.crossingless_components).to_json()
            ),
            "mid_simplification_rounds": self.mid_simplification_rounds,
            "heuristic_failover_rounds": self.heuristic_failover_rounds,
            "reidemeister_i_moves": self.reidemeister_i_moves,
            "reidemeister_ii_moves": self.reidemeister_ii_moves,
            "reidemeister_iii_moves": self.reidemeister_iii_moves,
            "nugatory_crossing_moves": self.nugatory_crossing_moves,
            "tested_red_paths": self.tested_red_paths,
            "tested_green_paths": self.tested_green_paths,
            "last_path_search_mode": self.last_path_search_mode,
            "reapr_used": self.reapr_used,
            "reapr_rounds": self.reapr_rounds,
            "reapr_attempts": self.reapr_attempts,
            "reapr_rejected": self.reapr_rejected,
            "reapr_status": self.reapr_status,
            "reapr_warning": self.reapr_warning,
            "alexander_determinant_before": self.alexander_determinant_before,
            "alexander_determinant_after": self.alexander_determinant_after,
            "reapr_invariants_before": self.reapr_invariants_before,
            "reapr_invariants_after": self.reapr_invariants_after,
            "stopped_by_round_limit": self.stopped_by_round_limit,
            "stopped_by_crossing_limit": self.stopped_by_crossing_limit,
            "timed_out": self.timed_out,
            "resource_limited": self.resource_limited,
        })
        return data


@dataclass
class AdaptiveStageStats:
    successes: int = 0
    misses: int = 0
    timeouts: int = 0
    consecutive_successes: int = 0
    consecutive_misses: int = 0
    consecutive_timeouts: int = 0


@dataclass
class AdaptiveScheduler:
    r3_prepass: AdaptiveStageStats = field(default_factory=AdaptiveStageStats)
    heuristic_search: AdaptiveStageStats = field(default_factory=AdaptiveStageStats)
    non_monotone: AdaptiveStageStats = field(default_factory=AdaptiveStageStats)


ADAPTIVE_STAGE_ORDER = ("r3_prepass", "heuristic_search", "non_monotone")
ADAPTIVE_STAGE_BASE_SCORE = {
    "r3_prepass": 300,
    "heuristic_search": 240,
    "non_monotone": 120,
}


def _adaptive_stage_stats(scheduler: AdaptiveScheduler, stage: str) -> AdaptiveStageStats:
    if stage == "r3_prepass":
        return scheduler.r3_prepass
    if stage == "heuristic_search":
        return scheduler.heuristic_search
    if stage == "non_monotone":
        return scheduler.non_monotone
    raise ValueError(f"unknown adaptive stage: {stage}")


def _adaptive_stage_score(scheduler: AdaptiveScheduler, stage: str) -> int:
    stats = _adaptive_stage_stats(scheduler, stage)
    return (
        ADAPTIVE_STAGE_BASE_SCORE[stage]
        + stats.successes * 70
        + stats.consecutive_successes * 180
        - stats.misses * 35
        - stats.consecutive_misses * 45
        - stats.timeouts * 140
        - stats.consecutive_timeouts * 260
    )


def _adaptive_stage_order(scheduler: AdaptiveScheduler) -> List[str]:
    return sorted(
        ADAPTIVE_STAGE_ORDER,
        key=lambda stage: (
            -_adaptive_stage_score(scheduler, stage),
            ADAPTIVE_STAGE_ORDER.index(stage),
        ),
    )


def _record_adaptive_success(scheduler: AdaptiveScheduler, stage: str) -> None:
    stats = _adaptive_stage_stats(scheduler, stage)
    stats.successes += 1
    stats.consecutive_successes += 1
    stats.consecutive_misses = 0
    stats.consecutive_timeouts = 0


def _record_adaptive_miss(scheduler: AdaptiveScheduler, stage: str) -> None:
    stats = _adaptive_stage_stats(scheduler, stage)
    stats.misses += 1
    stats.consecutive_misses += 1
    stats.consecutive_successes = 0
    stats.consecutive_timeouts = 0


def _record_adaptive_timeout(scheduler: AdaptiveScheduler, stage: str) -> None:
    stats = _adaptive_stage_stats(scheduler, stage)
    stats.timeouts += 1
    stats.consecutive_timeouts += 1
    stats.consecutive_misses += 1
    stats.consecutive_successes = 0


def _adaptive_scheduler_log(
    round_index: int,
    scheduler: AdaptiveScheduler,
    order: Sequence[str],
) -> str:
    parts = [f"round {round_index} adaptive_order"]
    for stage in order:
        stats = _adaptive_stage_stats(scheduler, stage)
        parts.append(
            (
                f"{stage}(score={_adaptive_stage_score(scheduler, stage)},"
                f"successes={stats.successes},misses={stats.misses},"
                f"timeouts={stats.timeouts},"
                f"success_streak={stats.consecutive_successes},"
                f"miss_streak={stats.consecutive_misses},"
                f"timeout_streak={stats.consecutive_timeouts})"
            )
        )
    return " ".join(parts)


@dataclass
class PDSimplificationResult:
    code: PDCode
    crossingless_components: int = 0
    reidemeister_i_moves: int = 0
    reidemeister_ii_moves: int = 0
    nugatory_crossing_moves: int = 0

    def to_json(self) -> Dict[str, object]:
        return {
            "enabled": True,
            "reidemeister_i_moves": self.reidemeister_i_moves,
            "reidemeister_ii_moves": self.reidemeister_ii_moves,
            "nugatory_crossing_moves": self.nugatory_crossing_moves,
            "output_crossings": len(self.code),
        }


@dataclass
class NonMonotoneStep:
    code: PDCode = field(default_factory=list)
    crossingless_components: int = 0
    kind: str = ""
    red_length: int = 0
    green_length: int = 0
    reidemeister_i_moves: int = 0
    reidemeister_ii_moves: int = 0
    reidemeister_iii_moves: int = 0
    nugatory_crossing_moves: int = 0


@dataclass
class NonMonotoneNode:
    code: PDCode = field(default_factory=list)
    crossingless_components: int = 0
    steps: List[NonMonotoneStep] = field(default_factory=list)
    depth: int = 0
    r3_potential: int = 0
    serial: int = 0


@dataclass
class NonMonotoneSearchResult:
    found: bool = False
    code: PDCode = field(default_factory=list)
    crossingless_components: int = 0
    steps: List[NonMonotoneStep] = field(default_factory=list)
    tested_red_paths: int = 0
    tested_green_paths: int = 0
    applied_candidates: int = 0
    generated_states: int = 0
    depth: int = 0
    reidemeister_i_moves: int = 0
    reidemeister_ii_moves: int = 0
    reidemeister_iii_moves: int = 0
    nugatory_crossing_moves: int = 0


@dataclass
class PDJob:
    label: str
    code: PDCode = field(default_factory=list)
    implied_crossingless_components: int = 0
    error: str = ""


def endpoint_key(endpoint: Endpoint) -> int:
    return endpoint.key


def endpoint_from_key(key: int) -> Endpoint:
    return Endpoint.from_key(key)


def face_pair_key(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def parse_pd_code(text: str) -> PDCode:
    numbers = [int(token) for token in re.findall(r"-?\d+", text)]
    if not numbers:
        return []
    if len(numbers) % 4 != 0:
        raise ValueError("The input must contain a multiple of four integers")
    return [
        (numbers[i], numbers[i + 1], numbers[i + 2], numbers[i + 3])
        for i in range(0, len(numbers), 4)
    ]


def format_pd_code(code: PDCode) -> str:
    parts = ["X[{},{},{},{}]".format(*crossing) for crossing in code]
    return "PD[" + ",".join(parts) + "]"


def compact_text(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace())


def stable_hash_text(text: str) -> int:
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & UINT64_MASK
    return value


def denotes_crossingless_unknot(text: str) -> bool:
    compact = compact_text(text)
    return compact in {"PD[]", "[]"}


def trim(text: str) -> str:
    return text.strip(" \t\r\n")


class Diagram:
    def __init__(self, code: PDCode):
        self.code = list(code)
        self.adjacent: List[List[Endpoint]] = [
            [Endpoint(-1, -1) for _ in range(4)] for _ in self.code
        ]
        self.directions: List[List[List[bool]]] = [
            [[False for _ in range(4)] for _ in range(4)] for _ in self.code
        ]
        self.signs: List[int] = [0 for _ in self.code]
        self.rotation_offsets: List[int] = [0 for _ in self.code]
        self._build_adjacency()
        starts = self._component_starts_from_pd()
        self._orient_crossings(starts)

    def opposite(self, endpoint: Endpoint) -> Endpoint:
        return self.adjacent[endpoint.crossing][endpoint.strand]

    def next(self, endpoint: Endpoint) -> Endpoint:
        return self.adjacent[endpoint.crossing][(endpoint.strand + 2) % 4]

    def next_corner(self, endpoint: Endpoint) -> Endpoint:
        return self.adjacent[endpoint.crossing][(endpoint.strand + 1) % 4]

    @staticmethod
    def rotate_endpoint(endpoint: Endpoint, offset: int) -> Endpoint:
        return Endpoint(endpoint.crossing, (endpoint.strand + offset) % 4)

    def label_at(self, crossing: int, strand: int) -> int:
        return self.code[crossing][(strand + self.rotation_offsets[crossing]) % 4]

    def crossing_entries(self) -> List[Endpoint]:
        entries: List[Endpoint] = []
        for crossing, sign in enumerate(self.signs):
            if sign == -1:
                entries.extend([Endpoint(crossing, 0), Endpoint(crossing, 1)])
            elif sign == 1:
                entries.extend([Endpoint(crossing, 0), Endpoint(crossing, 3)])
            else:
                raise RuntimeError("Crossing was not oriented")
        return entries

    def _build_adjacency(self) -> None:
        gluings: Dict[int, List[Endpoint]] = {}
        for crossing, labels in enumerate(self.code):
            for strand, label in enumerate(labels):
                gluings.setdefault(label, []).append(Endpoint(crossing, strand))
        for label, endpoints in gluings.items():
            if len(endpoints) != 2:
                raise ValueError(
                    f"PD label {label} appears {len(endpoints)} times; "
                    "each label must appear exactly twice"
                )
            first, second = endpoints
            self.adjacent[first.crossing][first.strand] = second
            self.adjacent[second.crossing][second.strand] = first

    def _component_starts_from_pd(self) -> List[Endpoint]:
        labels: Set[int] = set()
        gluings: Dict[int, List[Endpoint]] = {}
        for crossing, crossing_labels in enumerate(self.code):
            for strand, label in enumerate(crossing_labels):
                labels.add(label)
                gluings.setdefault(label, []).append(Endpoint(crossing, strand))

        starts: List[Endpoint] = []
        while labels:
            minimum = min(labels)
            labels.remove(minimum)
            first, second = gluings[minimum]
            if first.crossing == second.crossing:
                other_labels = set(self.code[first.crossing]) - {minimum}
                if not other_labels:
                    raise ValueError("A PD self-loop crossing must have another label")
                next_label = min(other_labels)
                direction = Endpoint(
                    first.crossing, self.code[first.crossing].index(next_label)
                )
            else:
                j1 = (first.strand + 2) % 4
                j2 = (second.strand + 2) % 4
                l1 = self.code[first.crossing][j1]
                l2 = self.code[second.crossing][j2]
                if l1 < l2:
                    next_label = l1
                    direction = Endpoint(first.crossing, j1)
                elif l2 < l1:
                    next_label = l2
                    direction = Endpoint(second.crossing, j2)
                else:
                    next_label = l1
                    if self.code[second.crossing][0] == l1 or self.code[first.crossing][0] == minimum:
                        direction = Endpoint(first.crossing, j1)
                    else:
                        direction = Endpoint(second.crossing, j2)
            starts.append(direction)
            while next_label != minimum:
                if next_label not in labels:
                    raise ValueError("PD component traversal encountered a repeated label")
                labels.remove(next_label)
                next_gluing = gluings[next_label]
                if next_gluing[0] == direction:
                    other = next_gluing[1]
                elif next_gluing[1] == direction:
                    other = next_gluing[0]
                else:
                    raise ValueError("PD component traversal lost its current endpoint")
                direction = Endpoint(other.crossing, (other.strand + 2) % 4)
                next_label = self.code[direction.crossing][direction.strand]
        return starts

    def _make_tail(self, crossing: int, strand: int) -> None:
        head = (strand + 2) % 4
        if self.directions[crossing][head][strand]:
            raise ValueError("The same crossing strand was oriented twice")
        self.directions[crossing][strand][head] = True

    def _orient_crossings(self, starts: List[Endpoint]) -> None:
        remaining = {Endpoint(crossing, strand).key for crossing in range(len(self.code)) for strand in range(4)}
        starts = list(starts)
        while remaining:
            if starts:
                start = starts.pop()
            else:
                start = endpoint_from_key(min(remaining))
            current = start
            while True:
                other = self.adjacent[current.crossing][current.strand]
                self._make_tail(other.crossing, other.strand)
                remaining.discard(current.key)
                remaining.discard(other.key)
                current = Endpoint(other.crossing, (other.strand + 2) % 4)
                if current == start:
                    break
        for crossing in range(len(self.code)):
            self._orient_crossing(crossing)

    def _orient_crossing(self, crossing: int) -> None:
        if self.directions[crossing][2][0]:
            self._rotate_crossing_180(crossing)
        if self.directions[crossing][3][1]:
            self.signs[crossing] = 1
        elif self.directions[crossing][1][3]:
            self.signs[crossing] = -1
        else:
            raise ValueError("Could not determine crossing sign from PD orientation")

    def _rotate_crossing_180(self, crossing: int) -> None:
        old_adjacent = list(self.adjacent[crossing])
        old_directions = [row[:] for row in self.directions[crossing]]
        self.directions[crossing] = [[False for _ in range(4)] for _ in range(4)]
        self.rotation_offsets[crossing] = (self.rotation_offsets[crossing] + 2) % 4

        for i in range(4):
            other = old_adjacent[(i + 2) % 4]
            if other.crossing != crossing:
                self.adjacent[other.crossing][other.strand] = Endpoint(crossing, i)
                self.adjacent[crossing][i] = other
            else:
                self.adjacent[crossing][i] = Endpoint(crossing, (other.strand - 2) % 4)

        for a in range(4):
            for b in range(4):
                if old_directions[a][b]:
                    self.directions[crossing][(a + 2) % 4][(b + 2) % 4] = True


def format_final_pd_code(code: PDCode) -> str:
    if not code:
        return format_pd_code(code)

    diagram = Diagram(code)

    oriented: List[Tuple[int, int, int, int]] = []
    labels: Set[int] = set()
    next_label: Dict[int, int] = {}

    for crossing in range(len(code)):
        row = tuple(diagram.label_at(crossing, strand) for strand in range(4))
        oriented.append(row)  # type: ignore[arg-type]
        labels.update(row)

        if not diagram.directions[crossing][0][2]:
            raise ValueError("Could not orient final PD crossing from an under-incoming strand")
        for tail in range(4):
            for head in range(4):
                if not diagram.directions[crossing][tail][head]:
                    continue
                in_label = diagram.label_at(crossing, tail)
                out_label = diagram.label_at(crossing, head)
                previous = next_label.get(in_label)
                if previous is not None and previous != out_label:
                    raise ValueError("Final PD component orientation is inconsistent")
                next_label[in_label] = out_label

    relabel: Dict[int, int] = {}
    next_output_label = 1
    for start in sorted(labels):
        if start in relabel:
            continue
        current = start
        while current not in relabel:
            relabel[current] = next_output_label
            next_output_label += 1
            if current not in next_label:
                raise ValueError("Final PD component orientation is incomplete")
            current = next_label[current]
        if current != start:
            raise ValueError("Final PD component orientation reached another component")

    canonical = [
        tuple(relabel[label] for label in crossing)
        for crossing in oriented
    ]
    canonical.sort()
    return format_pd_code(canonical)


@dataclass
class GraphEdge:
    u: int
    v: int
    interface_u: int
    interface_v: int
    weight: int = 1

    def interface_for_face(self, face: int) -> int:
        if face == self.u:
            return self.interface_u
        if face == self.v:
            return self.interface_v
        raise RuntimeError("Face is not incident to the requested dual edge")


class DualGraph:
    def __init__(self, diagram: Diagram):
        self.edge_to_face: List[int] = []
        self.face_assignment_order: List[int] = []
        self.faces: List[List[int]] = []
        self.edges: List[GraphEdge] = []
        self.adjacency: List[List[int]] = []
        self.edge_by_faces: Dict[Tuple[int, int], int] = {}
        self._build_faces(diagram)
        self._build_edges(diagram)

    def edge_index(self, a: int, b: int) -> Optional[int]:
        return self.edge_by_faces.get(face_pair_key(a, b))

    def edge(self, a: int, b: int) -> Optional[GraphEdge]:
        index = self.edge_index(a, b)
        if index is None:
            return None
        return self.edges[index]

    def _build_faces(self, diagram: Diagram) -> None:
        endpoint_count = len(diagram.code) * 4
        self.edge_to_face = [-1 for _ in range(endpoint_count)]
        present = [True for _ in range(endpoint_count)]
        remaining = endpoint_count

        while remaining > 0:
            first_key = next(key for key in range(endpoint_count - 1, -1, -1) if present[key])
            face_index = len(self.faces)
            face: List[int] = []
            first = endpoint_from_key(first_key)
            current = first
            present[first_key] = False
            remaining -= 1
            self.edge_to_face[first_key] = face_index
            self.face_assignment_order.append(first_key)
            face.append(first_key)

            while True:
                nxt = diagram.next_corner(current)
                if nxt == first:
                    self.faces.append(face)
                    break
                next_key = nxt.key
                self.edge_to_face[next_key] = face_index
                self.face_assignment_order.append(next_key)
                if present[next_key]:
                    present[next_key] = False
                    remaining -= 1
                face.append(next_key)
                current = nxt

    def _build_edges(self, diagram: Diagram) -> None:
        self.adjacency = [[] for _ in self.faces]
        for key in self.face_assignment_order:
            endpoint = endpoint_from_key(key)
            opposite = diagram.opposite(endpoint)
            opposite_key = opposite.key
            face = self.edge_to_face[key]
            neighbor = self.edge_to_face[opposite_key]
            if face >= neighbor:
                continue
            pair_key = face_pair_key(face, neighbor)
            found = self.edge_by_faces.get(pair_key)
            if found is None:
                edge = GraphEdge(face, neighbor, key, opposite_key)
                edge_index = len(self.edges)
                self.edge_by_faces[pair_key] = edge_index
                self.edges.append(edge)
                self.adjacency[face].append(edge_index)
                self.adjacency[neighbor].append(edge_index)
            else:
                edge = self.edges[found]
                if edge.u == face:
                    edge.interface_u = key
                    edge.interface_v = opposite_key
                else:
                    edge.interface_u = opposite_key
                    edge.interface_v = key


def possible_red_lines(diagram: Diagram) -> List[List[Endpoint]]:
    long_lines: List[List[Endpoint]] = []
    entries = diagram.crossing_entries()
    while entries:
        red_line: List[Endpoint] = []
        endpoint = entries.pop()
        red_line.append(endpoint)
        crossings = {endpoint.crossing}
        while True:
            endpoint = diagram.next(endpoint)
            red_line.append(endpoint)
            if endpoint.crossing in crossings:
                break
            crossings.add(endpoint.crossing)
        long_lines.append(red_line)

    candidates: List[List[Endpoint]] = []
    for line in long_lines:
        if len(line) < 3:
            continue
        for i in range(len(line) - 2):
            candidates.append(line[: len(line) - i])
    return candidates


def component_summaries(diagram: Diagram) -> List[ComponentSummary]:
    remaining = {endpoint.key for endpoint in diagram.crossing_entries()}
    summaries: List[ComponentSummary] = []
    while remaining:
        start = endpoint_from_key(max(remaining))
        current = start
        crossings: Set[int] = set()
        while True:
            remaining.discard(current.key)
            crossings.add(current.crossing)
            current = diagram.next(current)
            if current == start:
                break
        summaries.append(ComponentSummary(sorted(crossings)))
    return summaries


def analyze_components(code: PDCode, known_crossingless_components: int = 0) -> ComponentAnalysis:
    analysis = ComponentAnalysis(crossingless_components=known_crossingless_components)
    if not code:
        return analysis
    analysis.components = component_summaries(Diagram(code))
    return analysis


def analyze_components_after_removing_crossings(
    code: PDCode,
    removed_crossings: Sequence[int],
    known_crossingless_components: int = 0,
) -> ComponentAnalysis:
    removed = set(removed_crossings)
    for crossing in removed:
        if crossing < 0 or crossing >= len(code):
            raise ValueError(f"Removed crossing index {crossing} is out of range")
    original = analyze_components(code, known_crossingless_components)
    reduced = ComponentAnalysis(crossingless_components=original.crossingless_components)
    for component in original.components:
        remaining = [crossing for crossing in component.crossing_indices if crossing not in removed]
        if remaining:
            reduced.components.append(ComponentSummary(remaining))
        else:
            reduced.crossingless_components += 1
    return reduced


def unique_label_count(crossing: Sequence[int]) -> int:
    return len(set(crossing))


def value_set(code: PDCode) -> List[int]:
    return sorted({label for crossing in code for label in crossing})


def replace_label(code: PDCode, old_label: int, new_label: int) -> PDCode:
    if old_label == new_label:
        return [tuple(crossing) for crossing in code]
    return [
        tuple(new_label if label == old_label else label for label in crossing)  # type: ignore[misc]
        for crossing in code
    ]


def add_vector_edge(graph: Dict[int, List[int]], a: int, b: int) -> None:
    graph.setdefault(a, [])
    graph.setdefault(b, [])
    if b not in graph[a]:
        graph[a].append(b)
    if a not in graph[b]:
        graph[b].append(a)


def pd_adjacency_vector(code: PDCode) -> Dict[int, List[int]]:
    graph: Dict[int, List[int]] = {}
    for crossing in code:
        add_vector_edge(graph, crossing[0], crossing[2])
        add_vector_edge(graph, crossing[1], crossing[3])
    return graph


def renumber_r1_order(code: PDCode) -> PDCode:
    if not code:
        return []
    graph = pd_adjacency_vector(code)
    visit_order: List[int] = []
    for value in value_set(code):
        if value in visit_order:
            continue
        if value not in graph:
            raise ValueError("Invalid PD graph during R1 renumbering")
        visit_order.append(value)
        while True:
            current = visit_order[-1]
            advanced = False
            for nxt in sorted(graph[current]):
                if nxt not in visit_order:
                    visit_order.append(nxt)
                    advanced = True
                    break
            if not advanced:
                break
    new_label = {value: index for index, value in enumerate(visit_order)}
    return [tuple(new_label[label] for label in crossing) for crossing in code]  # type: ignore[misc]


def erase_r1_moves(
    code: PDCode,
    crossingless_components: int,
    canonicalize_result: bool = True,
) -> Tuple[PDCode, int, int]:
    if code:
        Diagram(code)
    result = [tuple(crossing) for crossing in code]
    moves = 0
    while True:
        changed = False
        for index, crossing in enumerate(result):
            if unique_label_count(crossing) > 3:
                continue
            after_removal = analyze_components_after_removing_crossings(
                result,
                [index],
                crossingless_components,
            )
            result.pop(index)
            singles = [
                label for label in crossing if list(crossing).count(label) == 1
            ]
            if len(singles) == 2:
                result = replace_label(result, singles[0], singles[1])
            crossingless_components = after_removal.crossingless_components
            if canonicalize_result:
                result = _canonical_output_code(result)
            moves += 1
            changed = True
            break
        if not changed:
            break
    result = renumber_r1_order(result)
    if canonicalize_result:
        result = _canonical_output_code(result)
    return result, crossingless_components, moves


def add_set_edge(graph: Dict[int, Set[int]], a: int, b: int) -> None:
    graph.setdefault(a, set()).add(b)
    graph.setdefault(b, set()).add(a)


def graph_component_count(code: PDCode) -> int:
    graph: Dict[int, Set[int]] = {}
    for crossing_index, crossing in enumerate(code):
        crossing_node = -crossing_index - 1
        for label in crossing:
            add_set_edge(graph, label, crossing_node)
    visited: Set[int] = set()
    count = 0
    for start in graph:
        if start in visited:
            continue
        count += 1
        stack = [start]
        visited.add(start)
        while stack:
            node = stack.pop()
            for nxt in graph.get(node, set()):
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
    return count


def is_nugatory_crossing(code: PDCode, index: int) -> bool:
    if unique_label_count(code[index]) != 4:
        raise ValueError("Nugatory check requires an R1-free PD code")
    without = list(code)
    without.pop(index)
    return graph_component_count(without) > graph_component_count(code)


def find_nugatory_crossing(code: PDCode) -> int:
    for index in range(len(code)):
        if is_nugatory_crossing(code, index):
            return index
    return -1


def add_pre_next_edge(previous: Dict[int, int], nxt: Dict[int, int], a: int, b: int) -> None:
    if abs(a - b) == 1:
        previous_value, next_value = (a, b) if a < b else (b, a)
    else:
        previous_value, next_value = (b, a) if a < b else (a, b)
    previous[next_value] = previous_value
    nxt[previous_value] = next_value


def pre_next_maps(code: PDCode) -> Tuple[Dict[int, int], Dict[int, int]]:
    if code:
        Diagram(code)
    previous: Dict[int, int] = {}
    nxt: Dict[int, int] = {}
    for crossing in code:
        if unique_label_count(crossing) > 2:
            add_pre_next_edge(previous, nxt, crossing[0], crossing[2])
            add_pre_next_edge(previous, nxt, crossing[1], crossing[3])
        else:
            values = sorted(set(crossing))
            if len(values) != 2:
                raise ValueError("Invalid two-value crossing in pre/next maps")
            previous[values[0]] = values[1]
            nxt[values[0]] = values[1]
            previous[values[1]] = values[0]
            nxt[values[1]] = values[0]

    for label in value_set(code):
        if label not in previous:
            if label not in nxt:
                raise ValueError("Broken PD pre/next map")
            previous[label] = nxt[label]
        if label not in nxt:
            nxt[label] = previous[label]
    return previous, nxt


def renumber_full_dfs(code: PDCode) -> PDCode:
    if not code:
        return []
    graph: Dict[int, Set[int]] = {}
    for crossing in code:
        add_set_edge(graph, crossing[0], crossing[2])
        add_set_edge(graph, crossing[1], crossing[3])

    visited: Set[int] = set()
    new_label: Dict[int, int] = {}
    for start in value_set(code):
        if start in visited:
            continue
        stack = [start]
        while stack:
            value = stack.pop()
            if value in visited:
                continue
            if value not in graph:
                raise ValueError("Invalid PD graph during renumbering")
            new_label[value] = len(visited)
            visited.add(value)
            for nxt in sorted(graph[value], reverse=True):
                if nxt not in visited:
                    stack.append(nxt)
    if len(new_label) != len(value_set(code)):
        raise ValueError("PD renumbering failed")
    return [tuple(new_label[label] for label in crossing) for crossing in code]  # type: ignore[misc]


def erase_one_nugatory_crossing(
    code: PDCode,
    index: int,
    crossingless_components: int,
    canonicalize_result: bool = True,
) -> Tuple[PDCode, int]:
    if unique_label_count(code[index]) != 4:
        raise ValueError("Nugatory erase requires an R1-free PD code")

    crossing = code[index]
    ax, bx, cx, dx = crossing
    _, nxt = pre_next_maps(code)
    loop = [ax]
    guard = len(value_set(code)) + 1
    while True:
        if loop[-1] not in nxt:
            raise ValueError("Broken loop while erasing nugatory crossing")
        next_label = nxt[loop[-1]]
        loop.append(next_label)
        if next_label == ax:
            loop.pop()
            break
        if len(loop) > guard:
            raise ValueError("Failed to close PD loop while erasing nugatory crossing")

    loop_set = set(loop)
    if not {ax, bx, cx, dx}.issubset(loop_set):
        raise ValueError("Nugatory crossing arcs are not in one component")

    after_removal = analyze_components_after_removing_crossings(
        code,
        [index],
        crossingless_components,
    )
    result = list(code)
    result.pop(index)
    result = replace_label(result, ax, cx)
    result = replace_label(result, dx, bx)
    result = renumber_full_dfs(result)
    if canonicalize_result:
        result = _canonical_output_code(result)
    return result, after_removal.crossingless_components


def endpoint_pairing(code: PDCode) -> List[int]:
    pairing = [-1 for _ in range(len(code) * 4)]
    labels: Dict[int, List[int]] = {}
    for crossing_index, crossing in enumerate(code):
        for strand, label in enumerate(crossing):
            labels.setdefault(label, []).append(crossing_index * 4 + strand)
    for label, endpoints in labels.items():
        if len(endpoints) != 2:
            raise ValueError(f"PD label {label} appears {len(endpoints)} times")
        first, second = endpoints
        pairing[first] = second
        pairing[second] = first
    return pairing


def assign_pair(pairing: List[int], first: int, second: int) -> None:
    if first < 0 or second < 0 or first >= len(pairing) or second >= len(pairing):
        raise IndexError("Endpoint pairing assignment is out of range")
    if first == second:
        raise ValueError("Endpoint pairing assignment created a self-pair")
    pairing[first] = second
    pairing[second] = first


def code_from_endpoint_pairing(
    pairing: Sequence[int],
    crossing_count: int,
    removed_crossings: Sequence[int] = (),
) -> PDCode:
    removed = set(removed_crossings)
    active = {
        crossing * 4 + strand
        for crossing in range(crossing_count)
        if crossing not in removed
        for strand in range(4)
    }
    label_by_endpoint: Dict[int, int] = {}
    seen: Set[int] = set()
    next_label = 0
    for endpoint in sorted(active):
        if endpoint in seen:
            continue
        mate = pairing[endpoint]
        if mate not in active or pairing[mate] != endpoint:
            raise ValueError("Endpoint rewrite produced a broken PD edge")
        seen.add(endpoint)
        seen.add(mate)
        label_by_endpoint[endpoint] = next_label
        label_by_endpoint[mate] = next_label
        next_label += 1

    output: PDCode = []
    for crossing in range(crossing_count):
        if crossing in removed:
            continue
        output.append(tuple(label_by_endpoint[crossing * 4 + strand] for strand in range(4)))
    return _canonical_output_code(output)


@dataclass(frozen=True)
class ReidemeisterIIMove:
    first_crossing: int
    first_strand: int
    second_crossing: int
    second_strand: int


def find_reidemeister_ii_move(code: PDCode) -> Optional[ReidemeisterIIMove]:
    if len(code) < 2:
        return None
    diagram = Diagram(code)
    for crossing in range(len(code)):
        for strand in range(4):
            first_neighbor = diagram.adjacent[crossing][strand]
            second_neighbor = diagram.adjacent[crossing][(strand + 1) % 4]
            if first_neighbor.crossing == crossing:
                continue
            if first_neighbor.crossing != second_neighbor.crossing:
                continue
            if (first_neighbor.strand - 1) % 4 != second_neighbor.strand:
                continue
            if (strand + first_neighbor.strand) % 2 != 0:
                continue
            return ReidemeisterIIMove(
                crossing,
                strand,
                first_neighbor.crossing,
                first_neighbor.strand,
            )
    return None


def erase_one_reidemeister_ii_move(
    code: PDCode,
    move: ReidemeisterIIMove,
    crossingless_components: int,
) -> Tuple[PDCode, int]:
    pairing = endpoint_pairing(code)
    a = move.first_crossing
    b = move.second_crossing
    strand = move.first_strand
    other_strand = move.second_strand
    w = pairing[a * 4 + (strand + 2) % 4]
    x = pairing[a * 4 + (strand + 3) % 4]
    y = pairing[b * 4 + (other_strand + 1) % 4]
    z = pairing[b * 4 + (other_strand + 2) % 4]
    assign_pair(pairing, w, z)
    assign_pair(pairing, x, y)
    after_removal = analyze_components_after_removing_crossings(
        code,
        [a, b],
        crossingless_components,
    )
    return code_from_endpoint_pairing(pairing, len(code), [a, b]), after_removal.crossingless_components


@dataclass(frozen=True)
class ReidemeisterIIIMove:
    corners: Tuple[Endpoint, Endpoint, Endpoint]


def possible_reidemeister_iii_moves(code: PDCode) -> List[ReidemeisterIIIMove]:
    if len(code) < 3:
        return []
    diagram = Diagram(code)
    graph = DualGraph(diagram)
    moves: List[ReidemeisterIIIMove] = []
    seen: Set[Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]] = set()
    for face in graph.faces:
        if len(face) != 3:
            continue
        corners = [endpoint_from_key(key) for key in face]
        parity_sum = sum(endpoint.strand % 2 for endpoint in corners)
        if parity_sum not in (1, 2):
            continue
        for _ in range(3):
            if corners[1].strand % 2 == 0 and corners[2].strand % 2 == 1:
                break
            corners = [corners[1], corners[2], corners[0]]
        if corners[1].strand % 2 != 0 or corners[2].strand % 2 != 1:
            continue
        if len({endpoint.crossing for endpoint in corners}) != 3:
            continue
        key = tuple((endpoint.crossing, endpoint.strand) for endpoint in corners)
        if key in seen:
            continue
        seen.add(key)
        moves.append(ReidemeisterIIIMove(tuple(corners)))  # type: ignore[arg-type]
    moves.sort(key=lambda move: tuple((endpoint.crossing, endpoint.strand) for endpoint in move.corners))
    return moves


def apply_reidemeister_iii_move(code: PDCode, move: ReidemeisterIIIMove) -> PDCode:
    crossing_count = len(code)
    endpoint_count = crossing_count * 4
    pairing = endpoint_pairing(code)
    pairing.extend([-1 for _ in range(12)])
    a_corner, b_corner, c_corner = move.corners

    old_border = [
        Endpoint(c_corner.crossing, (c_corner.strand - 1) % 4),
        Endpoint(c_corner.crossing, (c_corner.strand - 2) % 4),
        Endpoint(a_corner.crossing, (a_corner.strand - 1) % 4),
        Endpoint(a_corner.crossing, (a_corner.strand - 2) % 4),
        Endpoint(b_corner.crossing, (b_corner.strand - 1) % 4),
        Endpoint(b_corner.crossing, (b_corner.strand - 2) % 4),
    ]
    temporary: List[Tuple[int, int]] = []
    for index, endpoint in enumerate(old_border):
        border = endpoint.key
        mate = pairing[border]
        first_temp = endpoint_count + 2 * index
        second_temp = first_temp + 1
        temporary.append((first_temp, second_temp))
        assign_pair(pairing, first_temp, border)
        assign_pair(pairing, second_temp, mate)

    new_border = [
        Endpoint(a_corner.crossing, a_corner.strand % 4),
        Endpoint(b_corner.crossing, (b_corner.strand + 1) % 4),
        Endpoint(b_corner.crossing, b_corner.strand % 4),
        Endpoint(c_corner.crossing, (c_corner.strand + 1) % 4),
        Endpoint(c_corner.crossing, c_corner.strand % 4),
        Endpoint(a_corner.crossing, (a_corner.strand + 1) % 4),
    ]
    for index, endpoint in enumerate(new_border):
        assign_pair(pairing, endpoint.key, temporary[index][0])

    assign_pair(
        pairing,
        Endpoint(a_corner.crossing, (a_corner.strand - 1) % 4).key,
        Endpoint(b_corner.crossing, (b_corner.strand + 2) % 4).key,
    )
    assign_pair(
        pairing,
        Endpoint(b_corner.crossing, (b_corner.strand - 1) % 4).key,
        Endpoint(c_corner.crossing, (c_corner.strand + 2) % 4).key,
    )
    assign_pair(
        pairing,
        Endpoint(c_corner.crossing, (c_corner.strand - 1) % 4).key,
        Endpoint(a_corner.crossing, (a_corner.strand + 2) % 4).key,
    )

    for first_temp, second_temp in temporary:
        first_mate = pairing[first_temp]
        second_mate = pairing[second_temp]
        assign_pair(pairing, first_mate, second_mate)
        pairing[first_temp] = -1
        pairing[second_temp] = -1
    return code_from_endpoint_pairing(pairing[:endpoint_count], crossing_count)


def simplify_pd_code(
    code: PDCode,
    known_crossingless_components: int = 0,
    timeout: int = -1,
    deadline: Optional[float] = None,
    allow_reidemeister_ii: bool = True,
    canonicalize_input: bool = True,
) -> PDSimplificationResult:
    check_timeout(timeout, deadline)
    result = PDSimplificationResult(
        code=(
            _canonical_output_code([tuple(crossing) for crossing in code])
            if canonicalize_input
            else [tuple(crossing) for crossing in code]
        ),
        crossingless_components=known_crossingless_components,
    )
    while True:
        check_timeout(timeout, deadline)
        result.code, result.crossingless_components, r1_delta = erase_r1_moves(
            result.code,
            result.crossingless_components,
            canonicalize_input,
        )
        result.reidemeister_i_moves += r1_delta
        if r1_delta:
            continue

        if allow_reidemeister_ii:
            check_timeout(timeout, deadline)
            r2_move = find_reidemeister_ii_move(result.code)
            if r2_move is not None:
                result.code, result.crossingless_components = erase_one_reidemeister_ii_move(
                    result.code,
                    r2_move,
                    result.crossingless_components,
                )
                result.reidemeister_ii_moves += 1
                continue

        check_timeout(timeout, deadline)
        index = find_nugatory_crossing(result.code)
        if index < 0:
            break
        result.code, result.crossingless_components = erase_one_nugatory_crossing(
            result.code,
            index,
            result.crossingless_components,
            canonicalize_input,
        )
        result.nugatory_crossing_moves += 1
    return result


@dataclass
class ReidemeisterIIIFailoverResult:
    found: bool = False
    code: PDCode = field(default_factory=list)
    crossingless_components: int = 0
    depth: int = 0
    visited_states: int = 0
    reidemeister_i_moves: int = 0
    reidemeister_ii_moves: int = 0
    reidemeister_iii_moves: int = 0
    nugatory_crossing_moves: int = 0


def find_reidemeister_iii_failover(
    code: PDCode,
    crossingless_components: int,
    timeout: int = -1,
    deadline: Optional[float] = None,
    max_depth: int = R3_FAILOVER_MAX_DEPTH,
    max_states: int = R3_FAILOVER_MAX_STATES,
) -> ReidemeisterIIIFailoverResult:
    check_timeout(timeout, deadline)
    result = ReidemeisterIIIFailoverResult(
        code=[tuple(crossing) for crossing in code],
        crossingless_components=crossingless_components,
    )
    target_crossings = len(code)
    if target_crossings < 3:
        return result

    queue: Deque[Tuple[PDCode, int, int]] = deque()
    queue.append(([tuple(crossing) for crossing in code], crossingless_components, 0))
    seen: Set[str] = {format_final_pd_code(code)}

    while queue and len(seen) <= max_states:
        check_timeout(timeout, deadline)
        state_code, state_crossingless, depth = queue.popleft()
        result.visited_states += 1
        if depth >= max_depth:
            continue

        for move in possible_reidemeister_iii_moves(state_code):
            check_timeout(timeout, deadline)
            moved = apply_reidemeister_iii_move(state_code, move)
            simplified = simplify_pd_code(moved, state_crossingless, timeout, deadline)
            if len(simplified.code) < target_crossings:
                result.found = True
                result.code = _canonical_output_code(simplified.code)
                result.crossingless_components = simplified.crossingless_components
                result.depth = depth + 1
                result.reidemeister_i_moves = simplified.reidemeister_i_moves
                result.reidemeister_ii_moves = simplified.reidemeister_ii_moves
                result.reidemeister_iii_moves = depth + 1
                result.nugatory_crossing_moves = simplified.nugatory_crossing_moves
                return result
            if len(simplified.code) != target_crossings:
                continue

            key = format_final_pd_code(simplified.code)
            if key in seen:
                continue
            seen.add(key)
            queue.append((
                _canonical_output_code(simplified.code),
                simplified.crossingless_components,
                depth + 1,
            ))
            if len(seen) >= max_states:
                break

    return result


def reset_weights(graph: DualGraph) -> None:
    for edge in graph.edges:
        edge.weight = 1


def clone_dual_graph(graph: DualGraph) -> DualGraph:
    clone = object.__new__(DualGraph)
    clone.edge_to_face = list(graph.edge_to_face)
    clone.face_assignment_order = list(graph.face_assignment_order)
    clone.faces = [list(face) for face in graph.faces]
    clone.edges = [
        GraphEdge(edge.u, edge.v, edge.interface_u, edge.interface_v, edge.weight)
        for edge in graph.edges
    ]
    clone.adjacency = [list(edges) for edges in graph.adjacency]
    clone.edge_by_faces = dict(graph.edge_by_faces)
    return clone


def detected_worker_count() -> int:
    reported = os.cpu_count() or 1
    if reported <= 2:
        return reported
    return reported - 1


def selected_bruteforce_worker_count(max_thread: int, task_count: int) -> int:
    if task_count <= 1:
        return 1
    requested = detected_worker_count() if max_thread == -1 else max_thread
    if requested < 1:
        requested = 1
    if max_thread == -1 and task_count < 32:
        return 1
    return max(1, min(requested, task_count))


_PARALLEL_CODE: Optional[PDCode] = None
_PARALLEL_DIAGRAM: Optional[Diagram] = None
_PARALLEL_BASE_GRAPH: Optional[DualGraph] = None
_PARALLEL_RED_LINES: Optional[List[List[Endpoint]]] = None
_PARALLEL_REQUIRE_APPLICABLE = False
_PARALLEL_BEST_INDEX: Any = None
_PARALLEL_BEST_LOCK: Any = None
_PARALLEL_BRUTE_BUDGET_LIMIT = DEFAULT_BRUTEFORCE_BUDGET
_PARALLEL_BRUTE_BUDGET_USED: Any = None
_PARALLEL_BRUTE_BUDGET_LOCK: Any = None
_PARALLEL_TIMEOUT = -1
_PARALLEL_TIMEOUT_DEADLINE: Optional[float] = None
_PARALLEL_MAX_PATHS = -1
_PARALLEL_BAN_HEURISTIC = True
_PARALLEL_PATH_SEARCH_MODE = "bruteforce"
_PARALLEL_COLLECT_BEST = False


def _parallel_bruteforce_initializer(
    code: PDCode,
    red_lines: List[List[Endpoint]],
    require_applicable: bool,
    best_index: Any,
    best_lock: Any,
    budget_limit: int,
    budget_used: Any,
    budget_lock: Any,
    timeout: int,
    deadline: Optional[float],
    max_paths: int,
    ban_heuristic: bool,
    path_search_mode: str,
    collect_best: bool = False,
) -> None:
    global _PARALLEL_CODE
    global _PARALLEL_DIAGRAM
    global _PARALLEL_BASE_GRAPH
    global _PARALLEL_RED_LINES
    global _PARALLEL_REQUIRE_APPLICABLE
    global _PARALLEL_BEST_INDEX
    global _PARALLEL_BEST_LOCK
    global _PARALLEL_BRUTE_BUDGET_LIMIT
    global _PARALLEL_BRUTE_BUDGET_USED
    global _PARALLEL_BRUTE_BUDGET_LOCK
    global _PARALLEL_TIMEOUT
    global _PARALLEL_TIMEOUT_DEADLINE
    global _PARALLEL_MAX_PATHS
    global _PARALLEL_BAN_HEURISTIC
    global _PARALLEL_PATH_SEARCH_MODE
    global _PARALLEL_COLLECT_BEST

    _PARALLEL_CODE = code
    check_timeout(timeout, deadline)
    _PARALLEL_DIAGRAM = Diagram(code)
    check_timeout(timeout, deadline)
    _PARALLEL_BASE_GRAPH = DualGraph(_PARALLEL_DIAGRAM)
    _PARALLEL_RED_LINES = red_lines
    _PARALLEL_REQUIRE_APPLICABLE = require_applicable
    _PARALLEL_BEST_INDEX = best_index
    _PARALLEL_BEST_LOCK = best_lock
    _PARALLEL_BRUTE_BUDGET_LIMIT = budget_limit
    _PARALLEL_BRUTE_BUDGET_USED = budget_used
    _PARALLEL_BRUTE_BUDGET_LOCK = budget_lock
    _PARALLEL_TIMEOUT = timeout
    _PARALLEL_TIMEOUT_DEADLINE = deadline
    _PARALLEL_MAX_PATHS = max_paths
    _PARALLEL_BAN_HEURISTIC = ban_heuristic
    _PARALLEL_PATH_SEARCH_MODE = path_search_mode
    _PARALLEL_COLLECT_BEST = collect_best


def _parallel_should_skip(red_index: int) -> bool:
    return _PARALLEL_BEST_INDEX is not None and red_index > _PARALLEL_BEST_INDEX.value


def _parallel_record_found(red_index: int) -> None:
    if _PARALLEL_BEST_INDEX is None or _PARALLEL_BEST_LOCK is None:
        return
    with _PARALLEL_BEST_LOCK:
        if red_index < _PARALLEL_BEST_INDEX.value:
            _PARALLEL_BEST_INDEX.value = red_index


def _parallel_budget_exhausted() -> bool:
    if _PARALLEL_BRUTE_BUDGET_LIMIT < 0 or _PARALLEL_BRUTE_BUDGET_USED is None:
        return False
    return _PARALLEL_BRUTE_BUDGET_USED.value >= _PARALLEL_BRUTE_BUDGET_LIMIT


def _parallel_take_budget() -> bool:
    if _PARALLEL_BRUTE_BUDGET_LIMIT < 0:
        return True
    if _PARALLEL_BRUTE_BUDGET_USED is None or _PARALLEL_BRUTE_BUDGET_LOCK is None:
        return True
    with _PARALLEL_BRUTE_BUDGET_LOCK:
        if _PARALLEL_BRUTE_BUDGET_USED.value >= _PARALLEL_BRUTE_BUDGET_LIMIT:
            return False
        _PARALLEL_BRUTE_BUDGET_USED.value += 1
        return True


def terminate_process_pool(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    processes = getattr(executor, "_processes", None)
    if processes:
        for process in list(processes.values()):
            if process is not None and process.is_alive():
                process.terminate()
    executor.shutdown(wait=False, cancel_futures=True)


def crossing_graph_component_count(code: PDCode) -> int:
    if not code:
        return 0

    parent = list(range(len(code)))

    def find(value: int) -> int:
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        if second_root < first_root:
            first_root, second_root = second_root, first_root
        parent[second_root] = first_root

    label_crossings: Dict[int, List[int]] = {}
    for crossing_index, crossing in enumerate(code):
        for label in crossing:
            label_crossings.setdefault(label, []).append(crossing_index)
    for label, crossings in label_crossings.items():
        if len(crossings) != 2:
            raise ValueError(
                f"PD label {label} appears {len(crossings)} times; "
                "each label must appear exactly twice"
            )
        union(crossings[0], crossings[1])

    return len({find(crossing_index) for crossing_index in range(len(code))})


def is_planar_pd_code(code: PDCode) -> bool:
    if not code:
        return True
    diagram = Diagram(code)
    graph = DualGraph(diagram)
    vertices = len(code)
    edges = 2 * vertices
    faces = len(graph.faces)
    graph_components = crossing_graph_component_count(code)
    return vertices - edges + faces == 2 * graph_components


def visit_simple_paths(
    graph: DualGraph,
    source: int,
    target: int,
    cutoff: int,
    visitor: Callable[[List[int]], bool],
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> bool:
    check_timeout(timeout, _timeout_deadline)
    if (
        source < 0
        or target < 0
        or source >= len(graph.faces)
        or target >= len(graph.faces)
        or cutoff <= 0
    ):
        return True
    if source == target:
        return visitor([source])

    visited = [False for _ in graph.faces]
    current_path = [source]
    distance = heuristic_distances_to_target(graph, target, cutoff, timeout, _timeout_deadline)
    visited[source] = True

    def dfs(current: int, current_weight: int) -> bool:
        check_timeout(timeout, _timeout_deadline)
        if len(current_path) - 1 >= cutoff:
            return True
        if (
            current < 0
            or current >= len(distance)
            or distance[current] >= 10**9
            or current_weight + distance[current] >= cutoff
        ):
            return True
        for edge_index in graph.adjacency[current]:
            edge = graph.edges[edge_index]
            nxt = edge.v if edge.u == current else edge.u
            if visited[nxt]:
                continue
            next_weight = current_weight + edge.weight
            if next_weight >= cutoff:
                continue
            if (
                nxt < 0
                or nxt >= len(distance)
                or distance[nxt] >= 10**9
                or next_weight + distance[nxt] >= cutoff
            ):
                continue
            current_path.append(nxt)
            visited[nxt] = True
            if nxt == target:
                keep_going = visitor(list(current_path))
            else:
                keep_going = dfs(nxt, next_weight)
            visited[nxt] = False
            current_path.pop()
            if not keep_going:
                return False
        return True

    return dfs(source, 0)


def heuristic_distances_to_target(
    graph: DualGraph,
    target: int,
    cutoff: int,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> List[int]:
    infinity = 10**9
    distance = [infinity for _ in graph.faces]
    queue: Deque[int] = deque([target])
    distance[target] = 0
    while queue:
        check_timeout(timeout, _timeout_deadline)
        current = queue.popleft()
        for edge_index in graph.adjacency[current]:
            edge = graph.edges[edge_index]
            if edge.weight >= cutoff:
                continue
            nxt = edge.v if edge.u == current else edge.u
            if distance[nxt] != infinity:
                continue
            distance[nxt] = distance[current] + 1
            queue.append(nxt)
    return distance


def collect_heuristic_paths(
    graph: DualGraph,
    source: int,
    target: int,
    cutoff: int,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> List[List[int]]:
    check_timeout(timeout, _timeout_deadline)
    face_count = len(graph.faces)
    if (
        source < 0
        or target < 0
        or source >= face_count
        or target >= face_count
        or cutoff <= 0
    ):
        return []
    if source == target:
        return [[source]]

    infinity = 10**9
    distance = heuristic_distances_to_target(graph, target, cutoff, timeout, _timeout_deadline)
    if distance[source] == infinity or distance[source] >= cutoff:
        return []

    state_budget = max(
        HEURISTIC_MIN_STATE_BUDGET,
        min(HEURISTIC_MAX_STATE_BUDGET, face_count * max(1, cutoff) * 8),
    )
    path_budget = max(
        HEURISTIC_MIN_PATH_BUDGET,
        min(HEURISTIC_MAX_PATH_BUDGET, face_count * 2 + cutoff * 8),
    )

    paths: List[List[int]] = []
    serial = 0
    # heap item:
    # (estimated_weight, estimated_length, branch_penalty, weight, path_length, serial, path, visited, weight, branch_penalty)
    heap: List[Tuple[int, int, int, int, int, int, List[int], Tuple[bool, ...], int, int]] = []
    initial_visited = [False for _ in graph.faces]
    initial_visited[source] = True
    heapq.heappush(
        heap,
        (
            distance[source],
            distance[source],
            0,
            0,
            1,
            serial,
            [source],
            tuple(initial_visited),
            0,
            0,
        ),
    )
    serial += 1

    popped_by_depth_face: Dict[Tuple[int, int], int] = {}
    popped_states = 0
    while heap and popped_states < state_budget and len(paths) < path_budget:
        check_timeout(timeout, _timeout_deadline)
        (
            _estimated_weight,
            _estimated_length,
            _branch_key,
            _weight_key,
            _path_length_key,
            _serial_key,
            path,
            visited_tuple,
            weight,
            branch_penalty,
        ) = heapq.heappop(heap)
        popped_states += 1

        current = path[-1]
        depth = len(path) - 1
        if current == target:
            if weight < cutoff:
                paths.append(path)
            continue
        if depth >= cutoff - 1:
            continue

        beam_key = (depth, current)
        beam_count = popped_by_depth_face.get(beam_key, 0)
        if beam_count >= HEURISTIC_BEAM_WIDTH:
            continue
        popped_by_depth_face[beam_key] = beam_count + 1

        visited = list(visited_tuple)
        steps = []
        for edge_index in graph.adjacency[current]:
            edge = graph.edges[edge_index]
            nxt = edge.v if edge.u == current else edge.u
            if visited[nxt] or distance[nxt] == infinity:
                continue
            new_weight = weight + edge.weight
            if new_weight >= cutoff:
                continue
            new_depth = depth + 1
            if new_depth + distance[nxt] >= cutoff:
                continue
            degree_penalty = max(0, len(graph.adjacency[nxt]) - 2)
            steps.append((edge.weight, distance[nxt], degree_penalty, nxt, edge_index))
        steps.sort()

        for _edge_weight, _distance, degree_penalty, nxt, _edge_index in steps:
            next_path = path + [nxt]
            next_visited = list(visited)
            next_visited[nxt] = True
            next_weight = weight + _edge_weight
            next_branch_penalty = branch_penalty + degree_penalty
            estimated_weight = next_weight + distance[nxt]
            estimated_length = len(next_path) - 1 + distance[nxt]
            heapq.heappush(
                heap,
                (
                    estimated_weight,
                    estimated_length,
                    next_branch_penalty,
                    next_weight,
                    len(next_path),
                    serial,
                    next_path,
                    tuple(next_visited),
                    next_weight,
                    next_branch_penalty,
                ),
            )
            serial += 1

    return paths


def collect_limited_heuristic_paths(
    graph: DualGraph,
    source: int,
    target: int,
    cutoff: int,
    state_budget: int,
    path_budget: int,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> List[List[int]]:
    check_timeout(timeout, _timeout_deadline)
    face_count = len(graph.faces)
    if (
        source < 0
        or target < 0
        or source >= face_count
        or target >= face_count
        or cutoff <= 0
        or state_budget <= 0
        or path_budget <= 0
    ):
        return []
    if source == target:
        return [[source]]

    infinity = 10**9
    distance = heuristic_distances_to_target(graph, target, cutoff, timeout, _timeout_deadline)
    if distance[source] == infinity or distance[source] >= cutoff:
        return []

    paths: List[List[int]] = []
    serial = 0
    heap: List[Tuple[int, int, int, int, int, int, List[int], Tuple[bool, ...], int, int]] = []
    initial_visited = [False for _ in graph.faces]
    initial_visited[source] = True
    heapq.heappush(
        heap,
        (
            distance[source],
            distance[source],
            0,
            0,
            1,
            serial,
            [source],
            tuple(initial_visited),
            0,
            0,
        ),
    )
    serial += 1

    popped_by_depth_face: Dict[Tuple[int, int], int] = {}
    popped_states = 0
    beam_limit = max(2, HEURISTIC_BEAM_WIDTH // 2)
    while heap and popped_states < state_budget and len(paths) < path_budget:
        check_timeout(timeout, _timeout_deadline)
        (
            _estimated_weight,
            _estimated_length,
            _branch_key,
            _weight_key,
            _path_length_key,
            _serial_key,
            path,
            visited_tuple,
            weight,
            branch_penalty,
        ) = heapq.heappop(heap)
        popped_states += 1

        current = path[-1]
        depth = len(path) - 1
        if current == target:
            if weight < cutoff:
                paths.append(path)
            continue
        if depth >= cutoff - 1:
            continue

        beam_key = (depth, current)
        beam_count = popped_by_depth_face.get(beam_key, 0)
        if beam_count >= beam_limit:
            continue
        popped_by_depth_face[beam_key] = beam_count + 1

        visited = list(visited_tuple)
        steps = []
        for edge_index in graph.adjacency[current]:
            edge = graph.edges[edge_index]
            nxt = edge.v if edge.u == current else edge.u
            if visited[nxt] or distance[nxt] == infinity:
                continue
            new_weight = weight + edge.weight
            if new_weight >= cutoff:
                continue
            new_depth = depth + 1
            if new_depth + distance[nxt] >= cutoff:
                continue
            degree_penalty = max(0, len(graph.adjacency[nxt]) - 2)
            steps.append((edge.weight, distance[nxt], degree_penalty, nxt, edge_index))
        steps.sort()

        for edge_weight, _distance, degree_penalty, nxt, _edge_index in steps:
            next_path = path + [nxt]
            next_visited = list(visited)
            next_visited[nxt] = True
            next_weight = weight + edge_weight
            next_branch_penalty = branch_penalty + degree_penalty
            estimated_weight = next_weight + distance[nxt]
            estimated_length = len(next_path) - 1 + distance[nxt]
            heapq.heappush(
                heap,
                (
                    estimated_weight,
                    estimated_length,
                    next_branch_penalty,
                    next_weight,
                    len(next_path),
                    serial,
                    next_path,
                    tuple(next_visited),
                    next_weight,
                    next_branch_penalty,
                ),
            )
            serial += 1

    return paths


def opposite_level(level: str) -> str:
    return "over" if level == "under" else "under"


def do_check(
    diagram: Diagram,
    graph: DualGraph,
    red_path: List[Endpoint],
    green_path: List[int],
    direction: str,
    result: SimplificationResult,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> bool:
    check_timeout(timeout, _timeout_deadline)
    green_left_cross: List[int] = []
    for i in range(len(green_path) - 1):
        f1 = green_path[i]
        f2 = green_path[i + 1]
        edge = graph.edge(f1, f2)
        if edge is None:
            return False
        face_for_interface = f1 if direction == "right" else f2
        green_left_cross.append(edge.interface_for_face(face_for_interface))

    red_boundary_crossings: Set[int] = set()
    to_check: Deque[int] = deque()
    queued: Set[int] = set()
    check_result: Dict[int, str] = {}

    def enqueue(key: int) -> None:
        if key not in queued:
            queued.add(key)
            to_check.append(key)

    def erase_queued(key: int) -> None:
        if key in queued:
            queued.remove(key)
            try:
                to_check.remove(key)
            except ValueError:
                pass

    for red_endpoint in red_path[:-1]:
        red_boundary_crossings.add(red_endpoint.crossing)
        offset = 3 if direction == "right" else 1
        cross_strand = Diagram.rotate_endpoint(red_endpoint, offset)
        key = cross_strand.key
        enqueue(key)
        check_result[key] = "under" if cross_strand.strand % 2 == 0 else "over"

    green_index = {face: index for index, face in enumerate(green_path)}
    green_crossings: List[GreenCrossing] = []
    good_path = True

    while to_check and good_path:
        check_timeout(timeout, _timeout_deadline)
        start_key = to_check.pop()
        queued.discard(start_key)
        cross_strand = endpoint_from_key(start_key)
        trace_seen: Set[Tuple[int, str]] = set()

        while True:
            check_timeout(timeout, _timeout_deadline)
            cross_key = cross_strand.key
            current_level = check_result[cross_key]
            trace_state = (cross_key, current_level)
            if trace_state in trace_seen:
                good_path = False
                break
            trace_seen.add(trace_state)
            opposite = diagram.opposite(cross_strand)
            opposite_key = opposite.key
            opposite_result = check_result.get(opposite_key)
            if opposite_result is not None and opposite_result != current_level:
                good_path = False
                break

            if cross_key in green_left_cross:
                f1 = graph.edge_to_face[cross_key]
                f2 = graph.edge_to_face[opposite_key]
                if f1 not in green_index or f2 not in green_index:
                    good_path = False
                    break
                forward = green_index[f1] < green_index[f2]
                green_crossings.append(
                    GreenCrossing(
                        from_face=f1 if forward else f2,
                        to_face=f2 if forward else f1,
                        strand_level=opposite_level(current_level),
                    )
                )
                break

            check_result[opposite_key] = current_level
            erase_queued(opposite_key)
            if opposite.crossing in red_boundary_crossings:
                break

            cross_strand = opposite
            side1 = Diagram.rotate_endpoint(cross_strand, 1)
            side2 = Diagram.rotate_endpoint(cross_strand, 3)
            side1_key = side1.key
            side2_key = side2.key

            if cross_strand.strand % 2 == 1 and current_level == "under":
                if check_result.get(side1_key) == "over" or check_result.get(side2_key) == "over":
                    good_path = False
                    break
                if side1_key not in check_result:
                    check_result[side1_key] = "under"
                    enqueue(side1_key)
                if side2_key not in check_result:
                    check_result[side2_key] = "under"
                    enqueue(side2_key)

            if cross_strand.strand % 2 == 0 and current_level == "over":
                if check_result.get(side1_key) == "under" or check_result.get(side2_key) == "under":
                    good_path = False
                    break
                if side1_key not in check_result:
                    check_result[side1_key] = "over"
                    enqueue(side1_key)
                if side2_key not in check_result:
                    check_result[side2_key] = "over"
                    enqueue(side2_key)

            across = Diagram.rotate_endpoint(cross_strand, 2)
            check_result[across.key] = current_level
            cross_strand = across

    if not good_path:
        return False
    result.found = True
    result.direction = direction
    result.red_path = list(red_path)
    result.green_path = list(green_path)
    result.green_crossings = green_crossings
    return True


def witness_has_applicable_surgery(code: PDCode, result: SimplificationResult) -> bool:
    if not result.found or len(result.red_path) < 2:
        return False
    try:
        diagram = Diagram(code)
        graph = DualGraph(diagram)
    except Exception:
        return False
    removed_crossings = {endpoint.crossing for endpoint in result.red_path[:-1]}
    if len(removed_crossings) != len(result.red_path) - 1:
        return False
    if result.red_path[-1].crossing in removed_crossings:
        return False
    red_entry_by_crossing = {
        endpoint.crossing: endpoint.strand for endpoint in result.red_path[:-1]
    }
    levels = {(crossing.from_face, crossing.to_face) for crossing in result.green_crossings}
    crossed_labels: Set[int] = set()

    def removed_red_node(node: int) -> bool:
        crossing = node // 4
        if crossing not in removed_crossings:
            return False
        strand = node % 4
        red_strand = red_entry_by_crossing[crossing]
        return strand == red_strand or strand == (red_strand + 2) % 4

    for index in range(len(result.green_path) - 1):
        from_face = result.green_path[index]
        to_face = result.green_path[index + 1]
        if (from_face, to_face) not in levels:
            return False
        edge = graph.edge(from_face, to_face)
        if edge is None:
            return False
        interface_from = edge.interface_for_face(from_face)
        interface_to = edge.interface_for_face(to_face)
        if removed_red_node(interface_from) or removed_red_node(interface_to):
            return False
        label = code[interface_from // 4][interface_from % 4]
        if label in crossed_labels:
            return False
        crossed_labels.add(label)
    try:
        apply_simplification_witness(code, result, 0)
    except Exception:
        return False
    return True


def witness_better_than(candidate: SimplificationResult, current: SimplificationResult) -> bool:
    if not current.found:
        return True
    if candidate.crossing_reduction != current.crossing_reduction:
        return candidate.crossing_reduction > current.crossing_reduction
    if candidate.resulting_crossings != current.resulting_crossings:
        return candidate.resulting_crossings < current.resulting_crossings
    if len(candidate.red_path) != len(current.red_path):
        return len(candidate.red_path) > len(current.red_path)
    if len(candidate.green_path) != len(current.green_path):
        return len(candidate.green_path) < len(current.green_path)
    return False


def score_witness_candidate(
    code: PDCode,
    candidate: SimplificationResult,
    require_applicable: bool,
) -> bool:
    try:
        applied_code, _crossingless = apply_simplification_witness(code, candidate, 0)
        candidate.resulting_crossings = len(applied_code)
        candidate.crossing_reduction = len(code) - len(applied_code)
        return (not require_applicable) or candidate.crossing_reduction > 0
    except Exception:
        if require_applicable:
            return False
        candidate.resulting_crossings = len(code)
        candidate.crossing_reduction = 0
        return True


def search_single_red_path(
    code: PDCode,
    diagram: Diagram,
    base_graph: DualGraph,
    red_path: List[Endpoint],
    max_paths: int,
    ban_heuristic: bool,
    require_applicable: bool,
    path_search_mode: str,
    should_skip: Optional[Callable[[], bool]] = None,
    bruteforce_budget: Optional[BruteForceBudget] = None,
    take_budget: Optional[Callable[[], bool]] = None,
    budget_exhausted: Optional[Callable[[], bool]] = None,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> RedPathSearchOutcome:
    check_timeout(timeout, _timeout_deadline)
    outcome = RedPathSearchOutcome()
    outcome.witness.path_search_mode = path_search_mode
    if should_skip is not None and should_skip():
        outcome.skipped = True
        return outcome
    if budget_exhausted is not None:
        if budget_exhausted():
            outcome.resource_limited = True
            return outcome
    elif bruteforce_budget is not None and bruteforce_budget.exhausted:
        outcome.resource_limited = True
        return outcome

    graph = clone_dual_graph(base_graph)
    start = red_path[0]
    end = red_path[-1]
    sources = [
        graph.edge_to_face[start.key],
        graph.edge_to_face[diagram.opposite(start).key],
    ]
    destinations = [
        graph.edge_to_face[end.key],
        graph.edge_to_face[diagram.opposite(end).key],
    ]

    for endpoint in red_path[1:-1]:
        check_timeout(timeout, _timeout_deadline)
        right_region = graph.edge_to_face[endpoint.key]
        left_region = graph.edge_to_face[diagram.opposite(endpoint).key]
        edge = graph.edge(right_region, left_region)
        if edge is not None:
            edge.weight = BLOCKED_WEIGHT

    cutoff = len(red_path) - 1

    def take_one_budget() -> bool:
        if take_budget is not None:
            return take_budget()
        if bruteforce_budget is not None:
            return bruteforce_budget.take()
        return True

    choose_best_within_red = max_paths != -1

    def consider_candidate(candidate: SimplificationResult) -> bool:
        candidate.path_search_mode = path_search_mode
        if not score_witness_candidate(code, candidate, require_applicable):
            return True
        candidate.found = True
        if witness_better_than(candidate, outcome.witness):
            outcome.witness = candidate
            outcome.found = True
        if not choose_best_within_red:
            outcome.completed = True
            outcome.witness.tested_green_paths = outcome.tested_green_paths
            return False
        return True

    def test_green_path(green_path: List[int]) -> bool:
        check_timeout(timeout, _timeout_deadline)
        if should_skip is not None and should_skip():
            outcome.skipped = True
            return False
        if not take_one_budget():
            outcome.resource_limited = True
            return False
        outcome.tested_green_paths += 1
        if len(green_path) >= len(red_path):
            return True
        candidate = SimplificationResult(path_search_mode=path_search_mode)
        if do_check(
            diagram,
            graph,
            red_path,
            green_path,
            "left",
            candidate,
            timeout,
            _timeout_deadline,
        ):
            if not consider_candidate(candidate):
                return False
        candidate = SimplificationResult(path_search_mode=path_search_mode)
        if do_check(
            diagram,
            graph,
            red_path,
            green_path,
            "right",
            candidate,
            timeout,
            _timeout_deadline,
        ):
            if not consider_candidate(candidate):
                return False
        return True

    for source in sources:
        for destination in destinations:
            check_timeout(timeout, _timeout_deadline)
            if should_skip is not None and should_skip():
                outcome.skipped = True
                return outcome
            if max_paths == -1 and not ban_heuristic:
                found_paths = collect_heuristic_paths(
                    graph, source, destination, cutoff, timeout, _timeout_deadline
                )
                for green_path in found_paths:
                    if not test_green_path(green_path):
                        return outcome
            else:
                visited_for_pair = 0

                def visitor(green_path: List[int]) -> bool:
                    nonlocal visited_for_pair
                    if max_paths != -1 and visited_for_pair > max_paths:
                        return False
                    visited_for_pair += 1
                    return test_green_path(green_path)

                completed = visit_simple_paths(
                    graph,
                    source,
                    destination,
                    cutoff,
                    visitor,
                    timeout,
                    _timeout_deadline,
                )
                if not completed or outcome.found or outcome.skipped or outcome.resource_limited:
                    return outcome

    outcome.completed = True
    outcome.witness.tested_green_paths = outcome.tested_green_paths
    if outcome.found:
        outcome.witness.tested_green_paths = outcome.tested_green_paths
    return outcome


def _parallel_red_path_worker(red_index: int) -> RedPathSearchOutcome:
    check_timeout(_PARALLEL_TIMEOUT, _PARALLEL_TIMEOUT_DEADLINE)
    if (
        _PARALLEL_CODE is None
        or _PARALLEL_DIAGRAM is None
        or _PARALLEL_BASE_GRAPH is None
        or _PARALLEL_RED_LINES is None
    ):
        raise RuntimeError("Parallel brute-force worker was not initialized")
    if not _PARALLEL_COLLECT_BEST and _parallel_should_skip(red_index):
        return RedPathSearchOutcome(skipped=True)
    if not _PARALLEL_COLLECT_BEST and _parallel_budget_exhausted():
        return RedPathSearchOutcome(resource_limited=True)
    outcome = search_single_red_path(
        _PARALLEL_CODE,
        _PARALLEL_DIAGRAM,
        _PARALLEL_BASE_GRAPH,
        _PARALLEL_RED_LINES[red_index],
        max_paths=_PARALLEL_MAX_PATHS,
        ban_heuristic=_PARALLEL_BAN_HEURISTIC,
        require_applicable=_PARALLEL_REQUIRE_APPLICABLE,
        path_search_mode=_PARALLEL_PATH_SEARCH_MODE,
        should_skip=None if _PARALLEL_COLLECT_BEST else lambda: _parallel_should_skip(red_index),
        take_budget=_parallel_take_budget if _PARALLEL_BAN_HEURISTIC else None,
        budget_exhausted=_parallel_budget_exhausted if _PARALLEL_BAN_HEURISTIC else None,
        timeout=_PARALLEL_TIMEOUT,
        _timeout_deadline=_PARALLEL_TIMEOUT_DEADLINE,
    )
    if outcome.found and not _PARALLEL_COLLECT_BEST:
        _parallel_record_found(red_index)
    return outcome


def _parallel_bruteforce_worker(red_index: int) -> RedPathSearchOutcome:
    return _parallel_red_path_worker(red_index)


def merge_red_path_outcomes(
    outcomes: List[RedPathSearchOutcome],
    path_search_mode: str,
) -> SimplificationResult:
    result = SimplificationResult(path_search_mode=path_search_mode)
    first_found = next(
        (index for index, outcome in enumerate(outcomes) if outcome.found),
        -1,
    )
    limit = first_found if first_found >= 0 else len(outcomes) - 1
    for index in range(limit + 1):
        outcome = outcomes[index]
        if outcome.resource_limited:
            result.resource_limited = True
        if (
            not outcome.completed
            and not outcome.found
            and not outcome.resource_limited
            and not result.resource_limited
        ):
            raise RuntimeError(
                f"Parallel brute-force search did not complete red path {index}"
            )
        if (
            outcome.completed
            or outcome.found
            or outcome.resource_limited
            or outcome.tested_green_paths > 0
        ):
            result.tested_red_paths += 1
        result.tested_green_paths += outcome.tested_green_paths

    if first_found >= 0:
        witness = outcomes[first_found].witness
        witness.path_search_mode = path_search_mode
        witness.tested_red_paths = first_found + 1
        witness.tested_green_paths = sum(
            outcomes[index].tested_green_paths for index in range(first_found + 1)
        )
        witness.resource_limited = any(
            outcomes[index].resource_limited for index in range(first_found + 1)
        )
        return witness
    return result


def merge_best_red_path_batch(
    outcomes: List[RedPathSearchOutcome],
    path_search_mode: str,
) -> SimplificationResult:
    result = SimplificationResult(path_search_mode=path_search_mode)
    best_found = -1
    for index, outcome in enumerate(outcomes):
        if outcome.resource_limited:
            result.resource_limited = True
        if (
            not outcome.completed
            and not outcome.found
            and not outcome.resource_limited
            and not outcome.skipped
        ):
            raise RuntimeError(
                f"Parallel heuristic search did not complete red path batch entry {index}"
            )
        if (
            outcome.completed
            or outcome.found
            or outcome.resource_limited
            or outcome.tested_green_paths > 0
        ):
            result.tested_red_paths += 1
        result.tested_green_paths += outcome.tested_green_paths
        if outcome.found and (
            best_found < 0
            or witness_better_than(outcome.witness, outcomes[best_found].witness)
        ):
            best_found = index

    if best_found >= 0:
        witness = outcomes[best_found].witness
        witness.path_search_mode = path_search_mode
        witness.tested_red_paths = result.tested_red_paths
        witness.tested_green_paths = result.tested_green_paths
        witness.resource_limited = result.resource_limited
        return witness
    return result


def find_simplification_parallel_heuristic_batches(
    code: PDCode,
    red_lines: List[List[Endpoint]],
    require_applicable: bool,
    worker_count: int,
    max_paths: int,
    ban_heuristic: bool,
    path_search_mode: str,
    bruteforce_budget: int = DEFAULT_BRUTEFORCE_BUDGET,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> SimplificationResult:
    check_timeout(timeout, _timeout_deadline)
    aggregate = SimplificationResult(path_search_mode=path_search_mode)
    if not red_lines:
        return aggregate
    best_witness = SimplificationResult(path_search_mode=path_search_mode)
    batches_since_best = 0
    with multiprocessing.Manager() as manager:
        best_index = manager.Value("i", len(red_lines))
        best_lock = manager.Lock()
        budget_used = manager.Value("q", 0)
        budget_lock = manager.Lock()
        executor: Optional[concurrent.futures.ProcessPoolExecutor] = None
        try:
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=worker_count,
                initializer=_parallel_bruteforce_initializer,
                initargs=(
                    code,
                    red_lines,
                    require_applicable,
                    best_index,
                    best_lock,
                    bruteforce_budget,
                    budget_used,
                    budget_lock,
                    timeout,
                    _timeout_deadline,
                    max_paths,
                    ban_heuristic,
                    path_search_mode,
                    True,
                ),
            )
            for batch_start in range(0, len(red_lines), worker_count):
                check_timeout(timeout, _timeout_deadline)
                batch_indices = list(
                    range(batch_start, min(len(red_lines), batch_start + worker_count))
                )
                futures = [
                    executor.submit(_parallel_red_path_worker, index)
                    for index in batch_indices
                ]
                outcomes: List[RedPathSearchOutcome] = [
                    RedPathSearchOutcome() for _ in batch_indices
                ]
                for local_index, future in enumerate(futures):
                    check_timeout(timeout, _timeout_deadline)
                    try:
                        outcomes[local_index] = future.result(
                            timeout=remaining_timeout_seconds(timeout, _timeout_deadline)
                        )
                    except concurrent.futures.TimeoutError as exc:
                        raise PdCodeSimplifyTimeoutError(
                            f"timeout after {timeout} seconds"
                        ) from exc
                batch = merge_best_red_path_batch(outcomes, path_search_mode)
                aggregate.tested_red_paths += batch.tested_red_paths
                aggregate.tested_green_paths += batch.tested_green_paths
                aggregate.resource_limited = (
                    aggregate.resource_limited or batch.resource_limited
                )
                if batch.found:
                    if witness_better_than(batch, best_witness):
                        best_witness = batch
                        batches_since_best = 0
                    elif best_witness.found:
                        batches_since_best += 1
                elif best_witness.found:
                    batches_since_best += 1
                if best_witness.found and (
                    batches_since_best >= HEURISTIC_BEST_LOOKAHEAD_BATCHES
                    or batch_indices[-1] == len(red_lines) - 1
                    or aggregate.resource_limited
                ):
                    best_witness.tested_red_paths = aggregate.tested_red_paths
                    best_witness.tested_green_paths = aggregate.tested_green_paths
                    best_witness.resource_limited = aggregate.resource_limited
                    return best_witness
                if aggregate.resource_limited:
                    return aggregate
        except (KeyboardInterrupt, PdCodeSimplifyTimeoutError):
            if executor is not None:
                terminate_process_pool(executor)
                executor = None
            raise
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
    return aggregate


def find_simplification_parallel_red_paths(
    code: PDCode,
    red_lines: List[List[Endpoint]],
    require_applicable: bool,
    worker_count: int,
    max_paths: int,
    ban_heuristic: bool,
    path_search_mode: str,
    bruteforce_budget: int = DEFAULT_BRUTEFORCE_BUDGET,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> SimplificationResult:
    check_timeout(timeout, _timeout_deadline)
    if not red_lines:
        return SimplificationResult(path_search_mode=path_search_mode)
    outcomes: List[RedPathSearchOutcome] = [
        RedPathSearchOutcome() for _ in red_lines
    ]
    with multiprocessing.Manager() as manager:
        best_index = manager.Value("i", len(red_lines))
        best_lock = manager.Lock()
        budget_used = manager.Value("q", 0)
        budget_lock = manager.Lock()
        executor: Optional[concurrent.futures.ProcessPoolExecutor] = None
        try:
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=worker_count,
                initializer=_parallel_bruteforce_initializer,
                initargs=(
                    code,
                    red_lines,
                    require_applicable,
                    best_index,
                    best_lock,
                    bruteforce_budget,
                    budget_used,
                    budget_lock,
                    timeout,
                    _timeout_deadline,
                    max_paths,
                    ban_heuristic,
                    path_search_mode,
                ),
            )
            futures = [
                executor.submit(_parallel_red_path_worker, index)
                for index in range(len(red_lines))
            ]
            for index, future in enumerate(futures):
                check_timeout(timeout, _timeout_deadline)
                try:
                    outcomes[index] = future.result(
                        timeout=remaining_timeout_seconds(timeout, _timeout_deadline)
                    )
                except concurrent.futures.TimeoutError as exc:
                    raise PdCodeSimplifyTimeoutError(
                        f"timeout after {timeout} seconds"
                    ) from exc
        except (KeyboardInterrupt, PdCodeSimplifyTimeoutError):
            if executor is not None:
                terminate_process_pool(executor)
                executor = None
            raise
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
    return merge_red_path_outcomes(outcomes, path_search_mode)


def find_simplification_parallel_bruteforce(
    code: PDCode,
    red_lines: List[List[Endpoint]],
    require_applicable: bool,
    worker_count: int,
    bruteforce_budget: int = DEFAULT_BRUTEFORCE_BUDGET,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> SimplificationResult:
    return find_simplification_parallel_red_paths(
        code,
        red_lines,
        require_applicable,
        worker_count,
        -1,
        True,
        "bruteforce",
        bruteforce_budget,
        timeout,
        _timeout_deadline,
    )


def find_simplification(
    code: PDCode,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    require_applicable: bool = False,
    max_thread: int = -1,
    bruteforce_budget: int = DEFAULT_BRUTEFORCE_BUDGET,
    verbose: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
    force_heuristic_parallel: bool = False,
) -> SimplificationResult:
    if max_thread < -1 or max_thread == 0:
        raise ValueError("max_thread must be -1 or a positive integer")
    validate_bruteforce_budget(bruteforce_budget)
    deadline = timeout_deadline(timeout, _timeout_deadline)
    check_timeout(timeout, deadline)
    result = SimplificationResult()
    if max_paths == -1 and not ban_heuristic:
        result.path_search_mode = "heuristic"
    elif max_paths == -1:
        result.path_search_mode = "bruteforce"
    else:
        result.path_search_mode = "bounded"

    cache_key: Optional[Tuple[str, int, bool, bool, int, int, bool]] = None
    if not verbose and deadline is None:
        cache_key = (
            format_final_pd_code(code),
            max_paths,
            ban_heuristic,
            require_applicable,
            max_thread,
            bruteforce_budget,
            force_heuristic_parallel,
        )
        cached = _SIMPLIFICATION_SEARCH_CACHE.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

    def store_and_return(value: SimplificationResult) -> SimplificationResult:
        if cache_key is not None:
            _SIMPLIFICATION_SEARCH_CACHE[cache_key] = copy.deepcopy(value)
        return value

    diagram = Diagram(code)
    check_timeout(timeout, deadline)
    base_graph = DualGraph(diagram)
    check_timeout(timeout, deadline)
    red_lines = possible_red_lines(diagram)
    heuristic_mode = max_paths == -1 and not ban_heuristic
    heuristic_parallel_enabled = (
        heuristic_mode
        and (force_heuristic_parallel or len(code) >= HEURISTIC_PARALLEL_MIN_CROSSINGS)
    )
    red_path_parallel_candidate = max_paths == -1 and (
        not heuristic_mode or heuristic_parallel_enabled
    )
    worker_count = (
        selected_bruteforce_worker_count(max_thread, len(red_lines))
        if red_path_parallel_candidate
        else 1
    )
    if heuristic_mode:
        if worker_count > 1:
            heuristic_message = (
                "heuristic_parallel_batches first_hit=no "
                f"lookahead_batches={HEURISTIC_BEST_LOOKAHEAD_BATCHES} "
                f"min_crossings={HEURISTIC_PARALLEL_MIN_CROSSINGS} "
                f"red_paths={len(red_lines)}"
            )
        else:
            heuristic_message = (
                f"heuristic_legacy first_hit=yes red_paths={len(red_lines)}"
            )
        _emit_progress(
            verbose,
            progress,
            heuristic_message,
        )
    check_timeout(timeout, deadline)
    if red_path_parallel_candidate:
        if max_thread == -1:
            _emit_progress(
                verbose,
                progress,
                (
                    f"{result.path_search_mode}_threads max_thread=-1 "
                    f"actual_threads={worker_count} red_paths={len(red_lines)} "
                    f"bruteforce_budget={bruteforce_budget}"
                ),
            )
        elif worker_count > 1:
            _emit_progress(
                verbose,
                progress,
                (
                    f"{result.path_search_mode}_threads max_thread={max_thread} "
                    f"actual_threads={worker_count} red_paths={len(red_lines)} "
                    f"bruteforce_budget={bruteforce_budget}"
                ),
            )
        if worker_count > 1:
            if heuristic_mode:
                return store_and_return(
                    find_simplification_parallel_heuristic_batches(
                        code,
                        red_lines,
                        require_applicable,
                        worker_count,
                        max_paths,
                        ban_heuristic,
                        result.path_search_mode,
                        bruteforce_budget,
                        timeout,
                        deadline,
                    )
                )
            return store_and_return(
                find_simplification_parallel_red_paths(
                    code,
                    red_lines,
                    require_applicable,
                    worker_count,
                    max_paths,
                    ban_heuristic,
                    result.path_search_mode,
                    bruteforce_budget,
                    timeout,
                    deadline,
                )
            )

    brute_budget = (
        BruteForceBudget(bruteforce_budget)
        if max_paths == -1 and ban_heuristic
        else None
    )
    for red_index, red_path in enumerate(red_lines):
        check_timeout(timeout, deadline)
        if verbose and (red_index == 0 or red_index % 1024 == 0):
            _emit_progress(
                verbose,
                progress,
                (
                    f"search_progress mode={result.path_search_mode} "
                    f"red_index={red_index} red_paths={len(red_lines)} "
                    f"red_length={len(red_path)} "
                    f"tested_green={result.tested_green_paths}"
                ),
            )
        result.tested_red_paths += 1
        outcome = search_single_red_path(
            code,
            diagram,
            base_graph,
            red_path,
            max_paths,
            ban_heuristic,
            require_applicable,
            result.path_search_mode,
            bruteforce_budget=brute_budget,
            timeout=timeout,
            _timeout_deadline=deadline,
        )
        result.tested_green_paths += outcome.tested_green_paths
        result.resource_limited = result.resource_limited or outcome.resource_limited
        if outcome.resource_limited:
            return store_and_return(result)
        if outcome.found:
            witness = outcome.witness
            witness.path_search_mode = result.path_search_mode
            witness.tested_red_paths = result.tested_red_paths
            witness.tested_green_paths = result.tested_green_paths
            witness.resource_limited = outcome.resource_limited
            return store_and_return(witness)
    return store_and_return(result)


class DisjointSet:
    def __init__(self) -> None:
        self.parent: Dict[int, int] = {}

    def find(self, value: int) -> int:
        parent = self.parent.setdefault(value, value)
        if parent != value:
            parent = self.find(parent)
            self.parent[value] = parent
        return parent

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        if second_root < first_root:
            first_root, second_root = second_root, first_root
        self.parent[second_root] = first_root


def green_crossing_levels(result: SimplificationResult) -> Dict[Tuple[int, int], str]:
    path_edges = {
        (result.green_path[index], result.green_path[index + 1])
        for index in range(len(result.green_path) - 1)
    }
    levels: Dict[Tuple[int, int], str] = {}
    for crossing in result.green_crossings:
        edge_key = (crossing.from_face, crossing.to_face)
        if edge_key not in path_edges:
            raise ValueError(
                "Simplification witness has a green crossing outside the green path"
            )
        previous = levels.get(edge_key)
        if previous is not None and previous != crossing.strand_level:
            raise ValueError(
                "Simplification witness has conflicting green crossing levels"
            )
        levels[edge_key] = crossing.strand_level
    for edge_key in path_edges:
        if edge_key not in levels:
            raise ValueError("Simplification witness is missing a green crossing level")
    return levels


def apply_simplification_witness(
    code: PDCode,
    result: SimplificationResult,
    known_crossingless_components: int = 0,
) -> Tuple[PDCode, int]:
    if not result.found:
        raise ValueError("Cannot apply a missing simplification witness")
    if len(result.red_path) < 2:
        raise ValueError("Simplification witness red path is too short")

    diagram = Diagram(code)
    graph = DualGraph(diagram)
    removed_crossings = {endpoint.crossing for endpoint in result.red_path[:-1]}
    if len(removed_crossings) != len(result.red_path) - 1:
        raise ValueError("Simplification witness repeats a removed red crossing")
    if result.red_path[-1].crossing in removed_crossings:
        raise ValueError("Simplification witness ends inside the removed red arc")

    red_entry_by_crossing = {
        endpoint.crossing: endpoint.strand for endpoint in result.red_path[:-1]
    }
    levels = green_crossing_levels(result)
    dsu = DisjointSet()
    endpoint_count = len(code) * 4
    new_crossing_count = max(0, len(result.green_path) - 1)
    new_base = endpoint_count

    def new_node(crossing_index: int, strand: int) -> int:
        return new_base + crossing_index * 4 + strand

    def is_removed_node(node: int) -> bool:
        return node < endpoint_count and (node // 4) in removed_crossings

    def is_removed_red_node(node: int) -> bool:
        if not is_removed_node(node):
            return False
        crossing = node // 4
        strand = node % 4
        red_strand = red_entry_by_crossing[crossing]
        return strand == red_strand or strand == (red_strand + 2) % 4

    crossed_labels: Set[int] = set()
    crossed_edges: List[Tuple[int, int, str]] = []
    for index in range(new_crossing_count):
        from_face = result.green_path[index]
        to_face = result.green_path[index + 1]
        edge = graph.edge(from_face, to_face)
        if edge is None:
            raise ValueError("Simplification witness green path crosses a missing dual edge")
        interface_from = edge.interface_for_face(from_face)
        interface_to = edge.interface_for_face(to_face)
        if is_removed_red_node(interface_from) or is_removed_red_node(interface_to):
            raise ValueError("Simplification witness crosses an edge removed with the red arc")
        label = code[interface_from // 4][interface_from % 4]
        if label in crossed_labels:
            raise ValueError("Simplification witness crosses the same PD edge more than once")
        crossed_labels.add(label)
        level = levels.get((from_face, to_face))
        if level is None:
            raise ValueError("Simplification witness is missing a green crossing level")
        crossed_edges.append((interface_from, interface_to, level))

    label_endpoints: Dict[int, List[int]] = {}
    for crossing_index, crossing in enumerate(code):
        for strand, label in enumerate(crossing):
            label_endpoints.setdefault(label, []).append(crossing_index * 4 + strand)
    for label, endpoints in label_endpoints.items():
        if len(endpoints) != 2:
            raise ValueError(f"PD label {label} appears {len(endpoints)} times")
        if label not in crossed_labels:
            dsu.union(endpoints[0], endpoints[1])

    for crossing, strand in red_entry_by_crossing.items():
        dsu.union(crossing * 4 + ((strand + 1) % 4), crossing * 4 + ((strand + 3) % 4))

    green_anchor = result.red_path[0].key
    for index, (interface_from, interface_to, level) in enumerate(crossed_edges):
        if level == "over":
            existing_from_pos = 0
            existing_to_pos = 2
            green_in_pos = 3
            green_out_pos = 1
        elif level == "under":
            existing_from_pos = 1
            green_in_pos = 0
            green_out_pos = 2
            existing_to_pos = 3
        else:
            raise ValueError(f"Unknown green crossing strand level: {level!r}")

        dsu.union(interface_from, new_node(index, existing_from_pos))
        dsu.union(interface_to, new_node(index, existing_to_pos))
        dsu.union(green_anchor, new_node(index, green_in_pos))
        green_anchor = new_node(index, green_out_pos)

    dsu.union(green_anchor, result.red_path[-1].key)

    active_nodes: List[int] = [
        node for node in range(endpoint_count) if not is_removed_node(node)
    ]
    for index in range(new_crossing_count):
        active_nodes.extend(new_node(index, strand) for strand in range(4))

    grouped: Dict[int, List[int]] = {}
    for node in active_nodes:
        grouped.setdefault(dsu.find(node), []).append(node)

    label_by_node: Dict[int, int] = {}
    for new_label, nodes in enumerate(sorted(grouped.values(), key=lambda item: min(item))):
        if len(nodes) != 2:
            raise ValueError(
                "Applied simplification produced a non-PD edge with "
                f"{len(nodes)} active endpoints"
            )
        for node in nodes:
            label_by_node[node] = new_label

    output: PDCode = []
    for crossing_index in range(len(code)):
        if crossing_index in removed_crossings:
            continue
        crossing = tuple(label_by_node[crossing_index * 4 + strand] for strand in range(4))
        output.append(crossing)  # type: ignore[arg-type]
    for index in range(new_crossing_count):
        crossing = tuple(label_by_node[new_node(index, strand)] for strand in range(4))
        output.append(crossing)  # type: ignore[arg-type]

    total_components = analyze_components(code, known_crossingless_components).total_components
    output = renumber_full_dfs(output)
    if not is_planar_pd_code(output):
        raise ValueError("Applied simplification produced a non-planar PD code")
    crossing_components = analyze_components(output).components_with_crossings if output else 0
    crossingless_components = max(0, total_components - crossing_components)
    return output, crossingless_components


def safe_r3_potential(
    code: PDCode,
    timeout: int = -1,
    deadline: Optional[float] = None,
) -> int:
    check_timeout(timeout, deadline)
    if len(code) < 3:
        return 0
    return len(possible_reidemeister_iii_moves(code))


def _non_monotone_node_sort_key(
    node: NonMonotoneNode,
    target_crossings: int,
) -> Tuple[int, int, int, int, int]:
    delta = len(node.code) - target_crossings
    return (delta, -node.r3_potential, node.depth, len(node.steps), node.serial)


def _accumulate_non_monotone_counts(
    result: NonMonotoneSearchResult,
    step: NonMonotoneStep,
) -> None:
    result.reidemeister_i_moves += step.reidemeister_i_moves
    result.reidemeister_ii_moves += step.reidemeister_ii_moves
    result.reidemeister_iii_moves += step.reidemeister_iii_moves
    result.nugatory_crossing_moves += step.nugatory_crossing_moves


def _add_non_monotone_candidate(
    parent: NonMonotoneNode,
    raw_code: PDCode,
    raw_crossingless_components: int,
    raw_step: NonMonotoneStep,
    accepted_states: Set[str],
    candidate_states: Set[str],
    max_allowed_crossings: int,
    target_crossings: int,
    serial_box: List[int],
    candidates: List[NonMonotoneNode],
    result: NonMonotoneSearchResult,
    timeout: int,
    deadline: Optional[float],
) -> bool:
    check_timeout(timeout, deadline)
    key = format_final_pd_code(raw_code)
    if key in accepted_states or key in candidate_states:
        return False
    candidate_states.add(key)
    code = parse_pd_code(key)
    if len(code) > max_allowed_crossings:
        return False

    step = NonMonotoneStep(
        code=code,
        crossingless_components=raw_crossingless_components,
        kind=raw_step.kind,
        red_length=raw_step.red_length,
        green_length=raw_step.green_length,
        reidemeister_i_moves=raw_step.reidemeister_i_moves,
        reidemeister_ii_moves=raw_step.reidemeister_ii_moves,
        reidemeister_iii_moves=raw_step.reidemeister_iii_moves,
        nugatory_crossing_moves=raw_step.nugatory_crossing_moves,
    )
    node = NonMonotoneNode(
        code=code,
        crossingless_components=raw_crossingless_components,
        steps=list(parent.steps) + [step],
        depth=parent.depth + 1,
        r3_potential=safe_r3_potential(code, timeout, deadline),
        serial=serial_box[0],
    )
    serial_box[0] += 1

    result.generated_states += 1
    if len(node.code) < target_crossings:
        result.found = True
        result.code = node.code
        result.crossingless_components = node.crossingless_components
        result.steps = node.steps
        result.depth = node.depth
        for stored_step in result.steps:
            _accumulate_non_monotone_counts(result, stored_step)
        return True

    candidates.append(node)
    return True


def _generate_non_monotone_r3_candidates(
    node: NonMonotoneNode,
    accepted_states: Set[str],
    candidate_states: Set[str],
    max_allowed_crossings: int,
    target_crossings: int,
    serial_box: List[int],
    candidates: List[NonMonotoneNode],
    result: NonMonotoneSearchResult,
    timeout: int,
    deadline: Optional[float],
    verbose: bool,
    progress: Optional[Callable[[str], None]],
) -> None:
    check_timeout(timeout, deadline)
    if len(candidates) >= NON_MONOTONE_MAX_CANDIDATES_PER_STATE:
        return
    _emit_progress(
        verbose,
        progress,
        (
            f"non_monotone_r3_start node_depth={node.depth} "
            f"crossings={len(node.code)} candidates={len(candidates)}"
        ),
    )
    tried = 0
    for move in possible_reidemeister_iii_moves(node.code):
        check_timeout(timeout, deadline)
        if (
            tried >= NON_MONOTONE_R3_MOVES_PER_STATE
            or len(candidates) >= NON_MONOTONE_MAX_CANDIDATES_PER_STATE
        ):
            break
        tried += 1
        moved = apply_reidemeister_iii_move(node.code, move)
        simplified = simplify_pd_code(
            moved,
            node.crossingless_components,
            timeout,
            deadline,
        )
        step = NonMonotoneStep(
            kind="r3",
            reidemeister_i_moves=simplified.reidemeister_i_moves,
            reidemeister_ii_moves=simplified.reidemeister_ii_moves,
            reidemeister_iii_moves=1,
            nugatory_crossing_moves=simplified.nugatory_crossing_moves,
        )
        _add_non_monotone_candidate(
            node,
            simplified.code,
            simplified.crossingless_components,
            step,
            accepted_states,
            candidate_states,
            max_allowed_crossings,
            target_crossings,
            serial_box,
            candidates,
            result,
            timeout,
            deadline,
        )
        if result.found:
            return
    _emit_progress(
        verbose,
        progress,
        (
            f"non_monotone_r3_done node_depth={node.depth} tried={tried} "
            f"candidates={len(candidates)} generated_states={result.generated_states}"
        ),
    )


def _generate_non_monotone_surgery_candidates(
    node: NonMonotoneNode,
    accepted_states: Set[str],
    candidate_states: Set[str],
    max_allowed_crossings: int,
    target_crossings: int,
    serial_box: List[int],
    total_green_tests_box: List[int],
    candidates: List[NonMonotoneNode],
    result: NonMonotoneSearchResult,
    timeout: int,
    deadline: Optional[float],
    verbose: bool,
    progress: Optional[Callable[[str], None]],
) -> None:
    check_timeout(timeout, deadline)
    state_key = format_final_pd_code(node.code)
    state_hash = stable_hash_text(state_key)
    diagram = Diagram(node.code)
    base_graph = DualGraph(diagram)
    red_lines = possible_red_lines(diagram)

    red_by_length: Dict[int, List[int]] = {}
    for index, red_line in enumerate(red_lines):
        length = len(red_line)
        if length > NON_MONOTONE_MAX_RED_LENGTH:
            break
        red_by_length.setdefault(length, []).append(index)

    state_green_tests = 0
    state_red_tests = 0
    length_order = list(red_by_length)

    for red_length in length_order:
        indices = red_by_length[red_length]
        if not indices:
            continue
        accepted_for_length = 0
        length_done = False
        start_slot = (
            (
                state_hash
                + ((red_length * 11400714819323198485) & UINT64_MASK)
            )
            & UINT64_MASK
        ) % len(indices)
        scan_limit = min(len(indices), NON_MONOTONE_MAX_RED_SCANS_PER_LENGTH)

        for slot_offset in range(scan_limit):
            check_timeout(timeout, deadline)
            if (
                result.found
                or len(candidates) >= NON_MONOTONE_MAX_CANDIDATES_PER_STATE
                or state_red_tests >= NON_MONOTONE_MAX_RED_TESTS_PER_NODE
                or state_green_tests >= NON_MONOTONE_MAX_GREEN_TESTS_PER_STATE
                or total_green_tests_box[0] >= NON_MONOTONE_MAX_TOTAL_GREEN_TESTS
            ):
                return
            if accepted_for_length >= NON_MONOTONE_MAX_CANDIDATES_PER_LENGTH:
                break

            red_index = indices[(start_slot + slot_offset) % len(indices)]
            red_path = red_lines[red_index]
            state_red_tests += 1
            result.tested_red_paths += 1
            if verbose and (
                result.tested_red_paths <= 8
                or result.tested_red_paths % 64 == 0
            ):
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"non_monotone_progress node_depth={node.depth} "
                        f"red_length={red_length} "
                        f"tested_red={result.tested_red_paths} "
                        f"tested_green={result.tested_green_paths} "
                        f"applied_candidates={result.applied_candidates} "
                        f"candidates={len(candidates)} "
                        f"state_green_tests={state_green_tests} "
                        f"total_green_tests={total_green_tests_box[0]}"
                    ),
                )

            graph = clone_dual_graph(base_graph)
            start = red_path[0]
            end = red_path[-1]
            sources = [
                graph.edge_to_face[start.key],
                graph.edge_to_face[diagram.opposite(start).key],
            ]
            destinations = [
                graph.edge_to_face[end.key],
                graph.edge_to_face[diagram.opposite(end).key],
            ]

            for endpoint in red_path[1:-1]:
                right_region = graph.edge_to_face[endpoint.key]
                left_region = graph.edge_to_face[diagram.opposite(endpoint).key]
                edge = graph.edge(right_region, left_region)
                if edge is not None:
                    edge.weight = BLOCKED_WEIGHT

            cutoff = red_length + NON_MONOTONE_EXTRA_CROSSINGS
            for source in sources:
                for destination in destinations:
                    check_timeout(timeout, deadline)
                    green_paths = collect_limited_heuristic_paths(
                        graph,
                        source,
                        destination,
                        cutoff,
                        NON_MONOTONE_HEURISTIC_STATE_BUDGET,
                        NON_MONOTONE_HEURISTIC_PATH_BUDGET,
                        timeout,
                        deadline,
                    )
                    for green_path in green_paths:
                        check_timeout(timeout, deadline)
                        if (
                            result.found
                            or len(candidates) >= NON_MONOTONE_MAX_CANDIDATES_PER_STATE
                            or state_green_tests >= NON_MONOTONE_MAX_GREEN_TESTS_PER_STATE
                            or total_green_tests_box[0] >= NON_MONOTONE_MAX_TOTAL_GREEN_TESTS
                        ):
                            return
                        if accepted_for_length >= NON_MONOTONE_MAX_CANDIDATES_PER_LENGTH:
                            length_done = True
                            break
                        if len(green_path) > red_length + NON_MONOTONE_EXTRA_CROSSINGS:
                            continue
                        state_green_tests += 1
                        total_green_tests_box[0] += 1
                        result.tested_green_paths += 1

                        for direction in ("left", "right"):
                            witness = SimplificationResult(path_search_mode="non_monotone")
                            if not do_check(
                                diagram,
                                graph,
                                red_path,
                                green_path,
                                direction,
                                witness,
                                timeout,
                                deadline,
                            ):
                                continue
                            try:
                                applied_code, applied_crossingless = apply_simplification_witness(
                                    node.code,
                                    witness,
                                    node.crossingless_components,
                                )
                                simplified = simplify_pd_code(
                                    applied_code,
                                    applied_crossingless,
                                    timeout,
                                    deadline,
                                )
                                result.applied_candidates += 1
                                step = NonMonotoneStep(
                                    kind="surgery",
                                    red_length=red_length,
                                    green_length=len(green_path),
                                    reidemeister_i_moves=simplified.reidemeister_i_moves,
                                    reidemeister_ii_moves=simplified.reidemeister_ii_moves,
                                    nugatory_crossing_moves=simplified.nugatory_crossing_moves,
                                )
                                accepted = _add_non_monotone_candidate(
                                    node,
                                    simplified.code,
                                    simplified.crossingless_components,
                                    step,
                                    accepted_states,
                                    candidate_states,
                                    max_allowed_crossings,
                                    target_crossings,
                                    serial_box,
                                    candidates,
                                    result,
                                    timeout,
                                    deadline,
                                )
                                if result.found:
                                    return
                                if accepted:
                                    accepted_for_length += 1
                                    if (
                                        accepted_for_length
                                        >= NON_MONOTONE_MAX_CANDIDATES_PER_LENGTH
                                    ):
                                        length_done = True
                                        break
                            except Exception:
                                continue
                        if length_done:
                            break
                    if length_done:
                        break
                if length_done:
                    break
            if length_done:
                break


def _select_non_monotone_beam(
    candidates: List[NonMonotoneNode],
    target_crossings: int,
) -> List[NonMonotoneNode]:
    selected: List[NonMonotoneNode] = []
    selected_serials: Set[int] = set()

    def take_candidate(
        candidate: NonMonotoneNode,
        selected_by_delta: Dict[int, int],
    ) -> None:
        if candidate.serial in selected_serials:
            return
        delta = len(candidate.code) - target_crossings
        same_crossing_cap = max(3, NON_MONOTONE_BEAM_WIDTH // 3)
        other_crossing_cap = max(2, NON_MONOTONE_BEAM_WIDTH // 5)
        cap = same_crossing_cap if delta == 0 else other_crossing_cap
        if (
            selected_by_delta.get(delta, 0) >= cap
            and len(selected) + 1 < NON_MONOTONE_BEAM_WIDTH
        ):
            return
        selected_by_delta[delta] = selected_by_delta.get(delta, 0) + 1
        selected_serials.add(candidate.serial)
        selected.append(candidate)

    candidates.sort(key=lambda node: _non_monotone_node_sort_key(node, target_crossings))
    crossing_first_by_delta: Dict[int, int] = {}
    for candidate in candidates:
        take_candidate(candidate, crossing_first_by_delta)
        if len(selected) >= NON_MONOTONE_BEAM_WIDTH // 2:
            break

    candidates.sort(
        key=lambda node: (
            -node.r3_potential,
            *_non_monotone_node_sort_key(node, target_crossings),
        )
    )
    r3_first_by_delta: Dict[int, int] = {}
    for candidate in candidates:
        take_candidate(candidate, r3_first_by_delta)
        if len(selected) >= NON_MONOTONE_BEAM_WIDTH:
            break
    return selected


def find_non_monotone_reduction(
    code: PDCode,
    crossingless_components: int,
    timeout: int = -1,
    deadline: Optional[float] = None,
    verbose: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> NonMonotoneSearchResult:
    check_timeout(timeout, deadline)
    result = NonMonotoneSearchResult(
        code=[tuple(crossing) for crossing in code],
        crossingless_components=crossingless_components,
    )
    cache_key: Optional[Tuple[str, int]] = None
    if not verbose and deadline is None:
        cache_key = (format_final_pd_code(code), crossingless_components)
        cached = _NON_MONOTONE_CACHE.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

    def store_and_return(value: NonMonotoneSearchResult) -> NonMonotoneSearchResult:
        if cache_key is not None:
            _NON_MONOTONE_CACHE[cache_key] = copy.deepcopy(value)
        return value

    target_crossings = len(code)
    max_allowed_crossings = target_crossings + NON_MONOTONE_MAX_TOTAL_INCREASE

    accepted_states: Set[str] = {format_final_pd_code(code)}
    serial_box = [0]
    initial = NonMonotoneNode(
        code=[tuple(crossing) for crossing in code],
        crossingless_components=crossingless_components,
        depth=0,
        r3_potential=safe_r3_potential(code, timeout, deadline),
        serial=serial_box[0],
    )
    serial_box[0] += 1
    beam = [initial]
    total_green_tests_box = [0]

    for depth in range(NON_MONOTONE_MAX_DEPTH):
        if not beam:
            break
        check_timeout(timeout, deadline)
        candidates: List[NonMonotoneNode] = []
        candidate_states: Set[str] = set()
        for node in beam:
            check_timeout(timeout, deadline)
            _generate_non_monotone_r3_candidates(
                node,
                accepted_states,
                candidate_states,
                max_allowed_crossings,
                target_crossings,
                serial_box,
                candidates,
                result,
                timeout,
                deadline,
                verbose,
                progress,
            )
            if result.found:
                return store_and_return(result)
            if len(candidates) >= NON_MONOTONE_MAX_CANDIDATES_PER_STATE:
                break
            _generate_non_monotone_surgery_candidates(
                node,
                accepted_states,
                candidate_states,
                max_allowed_crossings,
                target_crossings,
                serial_box,
                total_green_tests_box,
                candidates,
                result,
                timeout,
                deadline,
                verbose,
                progress,
            )
            if result.found:
                return store_and_return(result)
            if len(candidates) >= NON_MONOTONE_MAX_CANDIDATES_PER_STATE:
                break
            if total_green_tests_box[0] >= NON_MONOTONE_MAX_TOTAL_GREEN_TESTS:
                break

        beam = _select_non_monotone_beam(candidates, target_crossings)
        for node in beam:
            accepted_states.add(format_final_pd_code(node.code))
        if verbose:
            message = (
                f"non_monotone_depth depth={depth + 1} beam={len(beam)} "
                f"generated_states={result.generated_states} "
                f"tested_red={result.tested_red_paths} "
                f"tested_green={result.tested_green_paths} "
                f"applied_candidates={result.applied_candidates} "
                f"total_green_budget={total_green_tests_box[0]}"
            )
            if beam:
                message += (
                    f" best_crossings={len(beam[0].code)} "
                    f"best_r3_potential={beam[0].r3_potential}"
                )
            _emit_progress(verbose, progress, message)
        if total_green_tests_box[0] >= NON_MONOTONE_MAX_TOTAL_GREEN_TESTS:
            break

    return store_and_return(result)


def _emit_progress(
    verbose: bool,
    progress: Optional[Callable[[str], None]],
    message: str,
) -> None:
    if not verbose:
        return
    if progress is not None:
        progress(message)


def _emit_step_pd(
    show_step_pd: bool,
    step_pd_output: Optional[Callable[[int, PDCode], None]],
    round_index: int,
    code: PDCode,
) -> None:
    if not show_step_pd:
        return
    if step_pd_output is not None:
        step_pd_output(round_index, code)
        return
    print(f"step_pd_code[{round_index}]: {format_final_pd_code(code)}", flush=True)


def _search_mode(max_paths: int, ban_heuristic: bool) -> str:
    if max_paths == -1 and not ban_heuristic:
        return "heuristic"
    if max_paths == -1:
        return "bruteforce"
    return "bounded"


def _canonical_output_code(code: PDCode) -> PDCode:
    return parse_pd_code(format_final_pd_code(code))


def _mod_pow(base: int, exponent: int, modulus: int) -> int:
    result = 1 % modulus
    base %= modulus
    while exponent > 0:
        if exponent & 1:
            result = (result * base) % modulus
        base = (base * base) % modulus
        exponent >>= 1
    return result


def _determinant_mod_prime(matrix: List[List[int]], modulus: int) -> int:
    size = len(matrix)
    if size == 0:
        return 1
    determinant = 1
    for column in range(size):
        pivot = column
        while pivot < size and matrix[pivot][column] == 0:
            pivot += 1
        if pivot == size:
            return 0
        if pivot != column:
            matrix[pivot], matrix[column] = matrix[column], matrix[pivot]
            determinant = (-determinant) % modulus
        pivot_value = matrix[column][column]
        determinant = (determinant * pivot_value) % modulus
        inverse = _mod_pow(pivot_value, modulus - 2, modulus)
        for row in range(column + 1, size):
            if matrix[row][column] == 0:
                continue
            factor = (matrix[row][column] * inverse) % modulus
            for index in range(column, size):
                matrix[row][index] = (
                    matrix[row][index] - factor * matrix[column][index]
                ) % modulus
    residue = determinant % modulus
    return min(residue, (-residue) % modulus)


def _alexander_determinant_fingerprint(raw_code: PDCode) -> List[int]:
    if not raw_code:
        return [1 for _ in _ALEXANDER_FINGERPRINT_PRIMES]

    code = _canonical_output_code(raw_code)
    label_to_index: Dict[int, int] = {}
    for crossing in code:
        for label in crossing:
            if label not in label_to_index:
                label_to_index[label] = len(label_to_index)

    dsu = DisjointSet()
    for index in label_to_index.values():
        dsu.find(index)
    for crossing in code:
        dsu.union(label_to_index[crossing[1]], label_to_index[crossing[3]])

    root_to_class: Dict[int, int] = {}
    for index in label_to_index.values():
        root = dsu.find(index)
        if root not in root_to_class:
            root_to_class[root] = len(root_to_class)

    rows = len(code)
    columns = len(root_to_class)
    if rows <= 1 or columns <= 1:
        return [1 for _ in _ALEXANDER_FINGERPRINT_PRIMES]

    fingerprint: List[int] = []
    for modulus in _ALEXANDER_FINGERPRINT_PRIMES:
        matrix = [[0 for _ in range(columns)] for _ in range(rows)]
        for row, crossing in enumerate(code):
            under_in = root_to_class[dsu.find(label_to_index[crossing[0]])]
            over = root_to_class[dsu.find(label_to_index[crossing[1]])]
            under_out = root_to_class[dsu.find(label_to_index[crossing[2]])]
            matrix[row][over] = (matrix[row][over] + 2) % modulus
            matrix[row][under_in] = (matrix[row][under_in] - 1) % modulus
            matrix[row][under_out] = (matrix[row][under_out] - 1) % modulus

        minor_size = min(rows, columns) - 1
        minor = [
            [matrix[row][column] for column in range(minor_size)]
            for row in range(minor_size)
        ]
        fingerprint.append(_determinant_mod_prime(minor, modulus))
    return fingerprint


def _determinant_fingerprint_string(fingerprint: Sequence[int]) -> str:
    if not fingerprint:
        return ""
    if all(value == fingerprint[0] for value in fingerprint):
        return str(fingerprint[0])
    return ",".join(str(value) for value in fingerprint)


def _single_small_determinant_value(fingerprint: Sequence[int]) -> int:
    if not fingerprint:
        return -1
    first = fingerprint[0]
    return first if all(value == first for value in fingerprint) else -1


@dataclass
class ReaprInvariantProfile:
    components: int = 0
    determinant_fingerprint: Tuple[int, ...] = ()
    alexander_roots_evaluated: bool = False
    alexander_roots_mod_11: Tuple[int, ...] = ()
    alexander_roots_mod_19: Tuple[int, ...] = ()
    alexander_roots_mod_31: Tuple[int, ...] = ()


def _int_vector_string(values: Sequence[int]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


def _alexander_roots_mod_prime(
    raw_code: PDCode,
    modulus: int,
    timeout: int,
    deadline: Optional[float],
) -> Tuple[int, ...]:
    if not raw_code:
        return ()

    code = _canonical_output_code(raw_code)
    diagram = Diagram(code)
    label_to_index: Dict[int, int] = {}
    for crossing in range(len(code)):
        for strand in range(4):
            label = diagram.label_at(crossing, strand)
            if label not in label_to_index:
                label_to_index[label] = len(label_to_index)

    dsu = DisjointSet()
    for value in label_to_index.values():
        dsu.find(value)
    for crossing in range(len(code)):
        dsu.union(
            label_to_index[diagram.label_at(crossing, 1)],
            label_to_index[diagram.label_at(crossing, 3)],
        )

    root_to_class: Dict[int, int] = {}
    for value in label_to_index.values():
        root = dsu.find(value)
        if root not in root_to_class:
            root_to_class[root] = len(root_to_class)

    rows = len(code)
    columns = len(root_to_class)
    if rows <= 1 or columns <= 1:
        return ()
    minor_size = min(rows, columns) - 1
    row_specs: List[Tuple[int, int, int, int]] = []
    for row in range(minor_size):
        row_specs.append((
            root_to_class[dsu.find(label_to_index[diagram.label_at(row, 0)])],
            root_to_class[dsu.find(label_to_index[diagram.label_at(row, 1)])],
            root_to_class[dsu.find(label_to_index[diagram.label_at(row, 2)])],
            diagram.signs[row],
        ))

    roots: List[int] = []
    for t_value in range(1, modulus):
        check_timeout(timeout, deadline)
        minor = [[0 for _ in range(minor_size)] for _ in range(minor_size)]
        one_minus_t = (1 - t_value) % modulus
        for row, (under_in, over, under_out, sign) in enumerate(row_specs):
            def add_value(column: int, value: int) -> None:
                if column >= minor_size:
                    return
                minor[row][column] = (minor[row][column] + value) % modulus

            if sign == 1:
                add_value(over, one_minus_t)
                add_value(under_in, t_value)
                add_value(under_out, -1)
            else:
                add_value(over, one_minus_t)
                add_value(under_in, -1)
                add_value(under_out, t_value)
        if _determinant_mod_prime(minor, modulus) == 0:
            roots.append(t_value)
    return tuple(roots)


def _reapr_invariant_profile(
    code: PDCode,
    crossingless_components: int,
    timeout: int,
    deadline: Optional[float],
    include_alexander_roots: bool,
) -> ReaprInvariantProfile:
    profile = ReaprInvariantProfile(
        components=analyze_components(code, crossingless_components).total_components,
        determinant_fingerprint=tuple(_alexander_determinant_fingerprint(code)),
    )
    if include_alexander_roots:
        profile.alexander_roots_evaluated = True
        profile.alexander_roots_mod_11 = _alexander_roots_mod_prime(code, 11, timeout, deadline)
        profile.alexander_roots_mod_19 = _alexander_roots_mod_prime(code, 19, timeout, deadline)
        profile.alexander_roots_mod_31 = _alexander_roots_mod_prime(code, 31, timeout, deadline)
    return profile


def _reapr_basic_profiles_equal(
    before: ReaprInvariantProfile,
    after: ReaprInvariantProfile,
) -> bool:
    return (
        before.components == after.components
        and before.determinant_fingerprint == after.determinant_fingerprint
    )


def _reapr_profiles_equal(
    before: ReaprInvariantProfile,
    after: ReaprInvariantProfile,
) -> bool:
    return (
        _reapr_basic_profiles_equal(before, after)
        and before.alexander_roots_evaluated
        and after.alexander_roots_evaluated
        and before.alexander_roots_mod_11 == after.alexander_roots_mod_11
        and before.alexander_roots_mod_19 == after.alexander_roots_mod_19
        and before.alexander_roots_mod_31 == after.alexander_roots_mod_31
    )


def _reapr_invariant_profile_string(profile: ReaprInvariantProfile) -> str:
    roots = (
        (
            f"alexander_roots_mod_11={_int_vector_string(profile.alexander_roots_mod_11)}; "
            f"alexander_roots_mod_19={_int_vector_string(profile.alexander_roots_mod_19)}; "
            f"alexander_roots_mod_31={_int_vector_string(profile.alexander_roots_mod_31)}"
        )
        if profile.alexander_roots_evaluated
        else (
            "alexander_roots_mod_11=not_evaluated; "
            "alexander_roots_mod_19=not_evaluated; "
            "alexander_roots_mod_31=not_evaluated"
        )
    )
    return (
        f"components={profile.components}; "
        f"determinant={_determinant_fingerprint_string(profile.determinant_fingerprint)}; "
        f"{roots}"
    )


def _torus_2_odd_pd_code(crossings: int) -> PDCode:
    if crossings <= 0 or crossings % 2 == 0:
        return []
    code: PDCode = [(2 * crossings - 1, crossings, 0, crossings - 1)]
    for index in range(1, crossings):
        if index % 2:
            code.append((
                crossings - index - 1,
                2 * crossings - index,
                crossings - index,
                2 * crossings - index - 1,
            ))
        else:
            code.append((
                2 * crossings - index - 1,
                crossings - index,
                2 * crossings - index,
                crossings - index - 1,
            ))
    return _canonical_output_code(code)


def _splitmix64_next(state: int) -> Tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & UINT64_MASK
    value = state
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & UINT64_MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & UINT64_MASK
    return state, (value ^ (value >> 31)) & UINT64_MASK


def _deterministic_random_int(state: int, low: int, high: int) -> Tuple[int, int]:
    if high <= low:
        return state, low
    state, value = _splitmix64_next(state)
    return state, low + int(value % (high - low + 1))


def _reapr_seed_for_attempt(attempt: int, determinant: int, crossings: int) -> int:
    state = 0xD6E8FEB86659FD93
    state ^= ((attempt + 1) * 0x9E3779B97F4A7C15) & UINT64_MASK
    state ^= (max(0, determinant) * 0xBF58476D1CE4E5B9) & UINT64_MASK
    state ^= (max(0, crossings) * 0x94D049BB133111EB) & UINT64_MASK
    _, value = _splitmix64_next(state & UINT64_MASK)
    return value


def _closed_braid_pd_code(strands: int, word: Sequence[int]) -> PDCode:
    if strands < 2 or not word:
        return []
    current = list(range(strands))
    next_label = strands
    code: PDCode = []
    for generator in word:
        position = abs(generator) - 1
        if position < 0 or position + 1 >= strands:
            return []
        top_left = current[position]
        top_right = current[position + 1]
        bottom_left = next_label
        bottom_right = next_label + 1
        next_label += 2
        if generator > 0:
            code.append((top_right, bottom_right, bottom_left, top_left))
        else:
            code.append((top_right, top_left, bottom_left, bottom_right))
        current[position] = bottom_left
        current[position + 1] = bottom_right

    replaced: PDCode = []
    for crossing in code:
        replaced.append(tuple(
            current[label] if 0 <= label < strands else label
            for label in crossing
        ))  # type: ignore[arg-type]
    try:
        return _canonical_output_code(replaced)
    except Exception:
        return []


def _reapr_candidate_codes_for_attempt(
    determinant: int,
    original_crossings: int,
    attempt: int,
) -> List[PDCode]:
    candidates: List[PDCode] = []
    seen: Set[str] = set()
    maximum_candidate_crossings = max(0, original_crossings - 1)

    def append_candidate(candidate: PDCode, allow_empty: bool) -> None:
        if not candidate:
            if allow_empty and "PD[]" not in seen:
                seen.add("PD[]")
                candidates.append(candidate)
            return
        if len(candidate) >= original_crossings:
            return
        key = format_final_pd_code(candidate)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    if attempt == 0:
        if determinant == 1:
            append_candidate([], True)
        elif determinant > 1 and determinant % 2 and determinant < original_crossings:
            append_candidate(_torus_2_odd_pd_code(determinant), False)
        return candidates

    if maximum_candidate_crossings < 3:
        return candidates

    state = _reapr_seed_for_attempt(attempt, determinant, original_crossings)
    if determinant > 1 and determinant % 2 and determinant <= maximum_candidate_crossings:
        append_candidate(_closed_braid_pd_code(2, [-1] * determinant), False)

    candidate_count = 4 if original_crossings > 160 else 12
    for _candidate_index in range(candidate_count):
        state, strands = _deterministic_random_int(state, 3, 7)
        minimum_length = 3
        if determinant > 1 and determinant < maximum_candidate_crossings:
            minimum_length = max(
                minimum_length,
                min(maximum_candidate_crossings, determinant - 4),
            )
        if minimum_length > maximum_candidate_crossings:
            minimum_length = maximum_candidate_crossings
        state, length = _deterministic_random_int(
            state,
            minimum_length,
            maximum_candidate_crossings,
        )
        word: List[int] = []
        previous = 0
        for _index in range(length):
            state, generator = _deterministic_random_int(state, 1, strands - 1)
            state, sign_roll = _deterministic_random_int(state, 0, 99)
            sign = -1 if sign_roll < 40 else 1
            if previous and generator == abs(previous) and sign == (-1 if previous > 0 else 1):
                sign *= -1
            signed_generator = sign * generator
            word.append(signed_generator)
            previous = signed_generator
        append_candidate(_closed_braid_pd_code(strands, word), False)
    return candidates


@dataclass
class ReaprOracleResult:
    accepted: bool = False
    rejected: bool = False
    attempts: int = 0
    code: PDCode = field(default_factory=list)
    crossingless_components: int = 0
    status: str = ""
    determinant_before: str = ""
    determinant_after: str = ""
    invariants_before: str = ""
    invariants_after: str = ""
    matched_step_codes: List[PDCode] = field(default_factory=list)


def _crossingless_components_for_candidate(
    original: PDCode,
    original_crossingless: int,
    candidate: PDCode,
) -> int:
    total_components = analyze_components(original, original_crossingless).total_components
    if not candidate:
        return total_components
    candidate_components = analyze_components(candidate).components_with_crossings
    return max(0, total_components - candidate_components)


def _try_reapr_oracle(
    code: PDCode,
    crossingless_components: int,
    timeout: int,
    deadline: Optional[float],
    reapr_retry_max: int,
) -> ReaprOracleResult:
    result = ReaprOracleResult()
    before = _alexander_determinant_fingerprint(code)
    result.determinant_before = _determinant_fingerprint_string(before)
    result.determinant_after = result.determinant_before

    components = analyze_components(code, crossingless_components)
    if components.total_components != 1:
        result.status = "skipped_non_knot_input"
        return result
    if not code:
        result.status = "skipped_empty_input"
        return result

    determinant = _single_small_determinant_value(before)
    if determinant < 0:
        result.status = "skipped_ambiguous_determinant_fingerprint"
        return result

    if reapr_retry_max == 0:
        result.status = "skipped_reapr_retry_max_zero"
        return result

    saw_candidate = False
    saw_rejected_candidate = False
    before_basic_profile: Optional[ReaprInvariantProfile] = None
    before_full_profile: Optional[ReaprInvariantProfile] = None
    best_candidate: Optional[
        Tuple[int, str, PDCode, int, str, str, str]
    ] = None

    def store_candidate(
        candidate: PDCode,
        candidate_crossingless_components: int,
        cleaned_code: PDCode,
        status: str,
        determinant_after: str,
        invariants_after: str,
    ) -> None:
        nonlocal best_candidate
        key = format_final_pd_code(cleaned_code)
        item = (
            len(cleaned_code),
            key,
            [tuple(crossing) for crossing in candidate],
            candidate_crossingless_components,
            status,
            determinant_after,
            invariants_after,
        )
        if best_candidate is None or item[0] > best_candidate[0] or (
            item[0] == best_candidate[0] and item[1] < best_candidate[1]
        ):
            best_candidate = item

    for attempt in range(reapr_retry_max):
        check_timeout(timeout, deadline)
        result.attempts = attempt + 1
        candidates = _reapr_candidate_codes_for_attempt(determinant, len(code), attempt)
        for candidate_index, candidate in enumerate(candidates):
            check_timeout(timeout, deadline)
            if len(candidate) >= len(code):
                continue
            saw_candidate = True
            candidate_crossingless_components = _crossingless_components_for_candidate(
                code, crossingless_components, candidate
            )
            if before_basic_profile is None:
                before_basic_profile = _reapr_invariant_profile(
                    code,
                    crossingless_components,
                    timeout,
                    deadline,
                    False,
                )
                result.determinant_before = _determinant_fingerprint_string(
                    before_basic_profile.determinant_fingerprint
                )
                result.invariants_before = _reapr_invariant_profile_string(before_basic_profile)
            after_profile = _reapr_invariant_profile(
                candidate,
                candidate_crossingless_components,
                timeout,
                deadline,
                False,
            )
            result.determinant_after = _determinant_fingerprint_string(
                after_profile.determinant_fingerprint
            )
            result.invariants_after = _reapr_invariant_profile_string(after_profile)
            if not _reapr_basic_profiles_equal(before_basic_profile, after_profile):
                saw_rejected_candidate = True
                result.rejected = True
                continue

            if before_full_profile is None:
                before_full_profile = _reapr_invariant_profile(
                    code,
                    crossingless_components,
                    timeout,
                    deadline,
                    True,
                )
                result.invariants_before = _reapr_invariant_profile_string(before_full_profile)
            after_profile = _reapr_invariant_profile(
                candidate,
                candidate_crossingless_components,
                timeout,
                deadline,
                True,
            )
            result.invariants_after = _reapr_invariant_profile_string(after_profile)
            if not _reapr_profiles_equal(before_full_profile, after_profile):
                saw_rejected_candidate = True
                result.rejected = True
                continue

            cleaned_candidate = simplify_pd_code(
                candidate,
                candidate_crossingless_components,
                timeout,
                deadline,
            )
            cleaned_code = _canonical_output_code(cleaned_candidate.code)

            accepted_status = (
                "accepted_empty_projection"
                if not candidate
                else (
                    "accepted_projection_template"
                    if attempt == 0
                    else "accepted_retry_projection"
                )
            )
            store_candidate(
                candidate,
                candidate_crossingless_components,
                cleaned_code,
                accepted_status,
                result.determinant_after,
                result.invariants_after,
            )
            result.matched_step_codes.append(_canonical_output_code(candidate))
            if len(cleaned_code) + 1 == len(code):
                assert best_candidate is not None
                result.accepted = True
                result.status = best_candidate[4]
                result.code = best_candidate[2]
                result.crossingless_components = best_candidate[3]
                result.determinant_after = best_candidate[5]
                result.invariants_after = best_candidate[6]
                return result

    if best_candidate is not None:
        result.accepted = True
        result.status = best_candidate[4]
        result.code = best_candidate[2]
        result.crossingless_components = best_candidate[3]
        result.determinant_after = best_candidate[5]
        result.invariants_after = best_candidate[6]
    elif saw_rejected_candidate:
        result.status = "rejected_invariant_changed"
    elif saw_candidate:
        result.status = "rejected_no_matching_profile"
    else:
        result.status = "skipped_no_smaller_projection_template"
    return result


def reduce_pd_code(
    code: PDCode,
    known_crossingless_components: int = 0,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    reduction_round: int = -1,
    max_thread: int = -1,
    bruteforce_budget: int = DEFAULT_BRUTEFORCE_BUDGET,
    timeout: int = -1,
    quit_at_crossing: int = -1,
    verbose: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    show_step_pd: bool = False,
    step_pd_output: Optional[Callable[[int, PDCode], None]] = None,
    reapr: bool = False,
    reapr_retry_max: int = 3,
    _timeout_deadline: Optional[float] = None,
) -> ReductionResult:
    if max_thread < -1 or max_thread == 0:
        raise ValueError("max_thread must be -1 or a positive integer")
    validate_bruteforce_budget(bruteforce_budget)
    if quit_at_crossing < -1:
        raise ValueError("quit_at_crossing must be -1 or a non-negative integer")
    if reapr_retry_max < 0:
        raise ValueError("reapr_retry_max must be a non-negative integer")
    deadline = timeout_deadline(timeout, _timeout_deadline)
    output = ReductionResult(
        code=[list(crossing) for crossing in code],
        crossingless_components=known_crossingless_components,
    )
    def stop_if_quit_at_crossing(stage: str) -> bool:
        if quit_at_crossing < 0 or len(output.code) > quit_at_crossing:
            return False
        output.stopped_by_crossing_limit = True
        _emit_progress(
            verbose,
            progress,
            (
                f"{stage} stop_quit_at_crossing threshold={quit_at_crossing} "
                f"crossings={len(output.code)} "
                f"crossingless_components={output.crossingless_components}"
            ),
        )
        return True

    try:
        check_timeout(timeout, deadline)
        top_level_heuristic_mode = max_paths == -1 and not ban_heuristic
        large_initial_heuristic_mode = (
            top_level_heuristic_mode and len(code) >= HEURISTIC_PARALLEL_MIN_CROSSINGS
        )
        _emit_progress(
            verbose,
            progress,
            (
                f"start input_crossings={len(code)} "
                f"known_crossingless_components={known_crossingless_components} "
                f"reduction_round={reduction_round} max_paths={max_paths} "
                f"max_thread={max_thread} bruteforce_budget={bruteforce_budget} "
                f"timeout={timeout} quit_at_crossing={quit_at_crossing} "
                f"heuristic={'off' if ban_heuristic else 'on'} "
                f"heuristic_parallel_initial={'on' if large_initial_heuristic_mode else 'off'} "
                f"reapr={'on' if reapr else 'off'} "
                f"reapr_retry_max={reapr_retry_max}"
            ),
        )
        prepared = simplify_pd_code(
            code,
            known_crossingless_components,
            timeout,
            deadline,
            allow_reidemeister_ii=not top_level_heuristic_mode,
            canonicalize_input=not top_level_heuristic_mode,
        )
        output.code = (
            [tuple(crossing) for crossing in prepared.code]
            if top_level_heuristic_mode
            else _canonical_output_code(prepared.code)
        )
        output.crossingless_components = prepared.crossingless_components
        output.reidemeister_i_moves = prepared.reidemeister_i_moves
        output.reidemeister_ii_moves = prepared.reidemeister_ii_moves
        output.nugatory_crossing_moves = prepared.nugatory_crossing_moves
        check_timeout(timeout, deadline)
        _emit_progress(
            verbose,
            progress,
            (
                f"pre_simplify input_crossings={len(code)} "
                f"output_crossings={len(output.code)} "
                f"crossingless_components={output.crossingless_components} "
                f"r1_moves={prepared.reidemeister_i_moves} "
                f"r2_moves={prepared.reidemeister_ii_moves} "
                f"nugatory_moves={prepared.nugatory_crossing_moves}"
            ),
        )

        stop_if_quit_at_crossing("pre_simplify")

        if not output.stopped_by_crossing_limit and reapr:
            _emit_progress(
                verbose,
                progress,
                (
                    f"reapr_oracle_start crossings={len(output.code)} "
                    f"crossingless_components={output.crossingless_components}"
                ),
            )
            before_reapr_crossings = len(output.code)
            reapr_result = _try_reapr_oracle(
                output.code,
                output.crossingless_components,
                timeout,
                deadline,
                reapr_retry_max,
            )
            output.alexander_determinant_before = reapr_result.determinant_before
            output.alexander_determinant_after = reapr_result.determinant_after
            output.reapr_invariants_before = reapr_result.invariants_before
            output.reapr_invariants_after = reapr_result.invariants_after
            output.reapr_status = reapr_result.status
            output.reapr_rejected = reapr_result.rejected
            output.reapr_attempts = reapr_result.attempts
            if reapr_result.accepted:
                output.reapr_used = True
                output.reapr_rounds += 1
                output.reapr_warning = REAPR_WARNING
                output.code = _canonical_output_code(reapr_result.code)
                output.crossingless_components = reapr_result.crossingless_components
                if reapr_result.matched_step_codes:
                    for step_code in reapr_result.matched_step_codes:
                        _emit_step_pd(show_step_pd, step_pd_output, 0, step_code)
                else:
                    _emit_step_pd(show_step_pd, step_pd_output, 0, output.code)
                check_timeout(timeout, deadline)
                prepared = simplify_pd_code(output.code, output.crossingless_components, timeout, deadline)
                output.code = _canonical_output_code(prepared.code)
                output.crossingless_components = prepared.crossingless_components
                output.reidemeister_i_moves += prepared.reidemeister_i_moves
                output.reidemeister_ii_moves += prepared.reidemeister_ii_moves
                output.nugatory_crossing_moves += prepared.nugatory_crossing_moves
                stop_if_quit_at_crossing("reapr_oracle")
            _emit_progress(
                verbose,
                progress,
                (
                    f"reapr_oracle_done status={output.reapr_status} "
                    f"accepted={'yes' if output.reapr_used else 'no'} "
                    f"attempts={output.reapr_attempts} "
                    f"input_crossings={before_reapr_crossings} "
                    f"output_crossings={len(output.code)} "
                    f"determinant_before={output.alexander_determinant_before} "
                    f"determinant_after={output.alexander_determinant_after} "
                    f"invariants_before=\"{output.reapr_invariants_before}\" "
                    f"invariants_after=\"{output.reapr_invariants_after}\""
                ),
            )

        adaptive_scheduler = AdaptiveScheduler()

        while (
            not output.stopped_by_crossing_limit
            and (reduction_round < 0 or output.mid_simplification_rounds < reduction_round)
        ):
            check_timeout(timeout, deadline)
            round_index = output.mid_simplification_rounds + 1
            adaptive_mode = max_paths == -1 and not ban_heuristic
            search = SimplificationResult()
            restart_round = False

            def run_r3_prepass() -> bool:
                nonlocal restart_round
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} r3_prepass_start "
                        f"crossings={len(output.code)} "
                        f"max_depth={R3_PREPASS_MAX_DEPTH} "
                        f"max_states={R3_PREPASS_MAX_STATES}"
                    ),
                )
                r3_prepass = ReidemeisterIIIFailoverResult()
                stage_timeout = False
                stage_error = False
                try:
                    stage_timeout_value, stage_deadline = with_time_slice(
                        timeout,
                        deadline,
                        R3_PREPASS_TIME_SLICE_SECONDS,
                    )
                    r3_prepass = find_reidemeister_iii_failover(
                        output.code,
                        output.crossingless_components,
                        timeout=stage_timeout_value,
                        deadline=stage_deadline,
                        max_depth=R3_PREPASS_MAX_DEPTH,
                        max_states=R3_PREPASS_MAX_STATES,
                    )
                except PdCodeSimplifyTimeoutError:
                    if timeout_expired(deadline):
                        raise
                    stage_timeout = True
                    _emit_progress(
                        verbose,
                        progress,
                        (
                            f"round {round_index} r3_prepass_timeout "
                            f"seconds={R3_PREPASS_TIME_SLICE_SECONDS}"
                        ),
                    )
                except Exception as exc:
                    stage_error = True
                    _emit_progress(
                        verbose,
                        progress,
                        f"round {round_index} r3_prepass_error message=\"{exc}\"",
                    )
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} r3_prepass_done "
                        f"found={'yes' if r3_prepass.found else 'no'} "
                        f"depth={r3_prepass.depth} "
                        f"visited_states={r3_prepass.visited_states} "
                        f"final_crossings={len(r3_prepass.code) if r3_prepass.found else len(output.code)} "
                        f"r1_moves={r3_prepass.reidemeister_i_moves} "
                        f"r2_moves={r3_prepass.reidemeister_ii_moves} "
                        f"r3_moves={r3_prepass.reidemeister_iii_moves} "
                        f"nugatory_moves={r3_prepass.nugatory_crossing_moves}"
                    ),
                )
                if stage_timeout:
                    _record_adaptive_timeout(adaptive_scheduler, "r3_prepass")
                    return False
                if stage_error:
                    _record_adaptive_miss(adaptive_scheduler, "r3_prepass")
                    return False
                if r3_prepass.found:
                    _record_adaptive_success(adaptive_scheduler, "r3_prepass")
                    output.code = _canonical_output_code(r3_prepass.code)
                    output.crossingless_components = r3_prepass.crossingless_components
                    output.reidemeister_i_moves += r3_prepass.reidemeister_i_moves
                    output.reidemeister_ii_moves += r3_prepass.reidemeister_ii_moves
                    output.reidemeister_iii_moves += r3_prepass.reidemeister_iii_moves
                    output.nugatory_crossing_moves += r3_prepass.nugatory_crossing_moves
                    if stop_if_quit_at_crossing("r3_prepass"):
                        restart_round = True
                        return True
                    restart_round = True
                    return True
                _record_adaptive_miss(adaptive_scheduler, "r3_prepass")
                return False

            def run_heuristic_search(use_soft_slice: bool) -> bool:
                nonlocal search
                output.last_path_search_mode = _search_mode(max_paths, ban_heuristic)
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} search_start crossings={len(output.code)} "
                        f"mode={output.last_path_search_mode} "
                        f"max_thread={max_thread} "
                        f"soft_slice={MID_SEARCH_TIME_SLICE_SECONDS if use_soft_slice else -1}"
                    ),
                )
                stage_timeout = False
                try:
                    stage_timeout_value = timeout
                    stage_deadline = deadline
                    if adaptive_mode and use_soft_slice:
                        stage_timeout_value, stage_deadline = with_time_slice(
                            timeout,
                            deadline,
                            MID_SEARCH_TIME_SLICE_SECONDS,
                        )
                    search = find_simplification(
                        output.code,
                        max_paths=max_paths,
                        ban_heuristic=ban_heuristic,
                        require_applicable=True,
                        max_thread=max_thread,
                        bruteforce_budget=bruteforce_budget,
                        verbose=verbose,
                        progress=progress,
                        timeout=stage_timeout_value,
                        _timeout_deadline=stage_deadline,
                        force_heuristic_parallel=large_initial_heuristic_mode,
                    )
                except PdCodeSimplifyTimeoutError:
                    if not use_soft_slice or timeout_expired(deadline):
                        raise
                    stage_timeout = True
                    search = SimplificationResult(
                        path_search_mode=_search_mode(max_paths, ban_heuristic)
                    )
                    _emit_progress(
                        verbose,
                        progress,
                        (
                            f"round {round_index} search_timeout "
                            f"seconds={MID_SEARCH_TIME_SLICE_SECONDS}"
                        ),
                    )
                output.tested_red_paths += search.tested_red_paths
                output.tested_green_paths += search.tested_green_paths
                output.last_path_search_mode = search.path_search_mode
                output.resource_limited = output.resource_limited or search.resource_limited
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} search_done found={'yes' if search.found else 'no'} "
                        f"mode={search.path_search_mode} "
                        f"tested_red={search.tested_red_paths} "
                        f"tested_green={search.tested_green_paths} "
                        f"resource_limited={'yes' if search.resource_limited else 'no'}"
                    ),
                )
                if adaptive_mode:
                    if stage_timeout:
                        _record_adaptive_timeout(adaptive_scheduler, "heuristic_search")
                    elif search.found:
                        _record_adaptive_success(adaptive_scheduler, "heuristic_search")
                    else:
                        _record_adaptive_miss(adaptive_scheduler, "heuristic_search")
                return search.found

            def run_non_monotone() -> bool:
                nonlocal restart_round
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} non_monotone_start "
                        f"crossings={len(output.code)} "
                        f"max_depth={NON_MONOTONE_MAX_DEPTH} "
                        f"beam_width={NON_MONOTONE_BEAM_WIDTH} "
                        f"max_red_length={NON_MONOTONE_MAX_RED_LENGTH} "
                        f"max_total_green_tests={NON_MONOTONE_MAX_TOTAL_GREEN_TESTS}"
                    ),
                )
                non_monotone = NonMonotoneSearchResult()
                stage_timeout = False
                stage_error = False
                try:
                    stage_timeout_value, stage_deadline = with_time_slice(
                        timeout,
                        deadline,
                        NON_MONOTONE_TIME_SLICE_SECONDS,
                    )
                    non_monotone = find_non_monotone_reduction(
                        output.code,
                        output.crossingless_components,
                        timeout=stage_timeout_value,
                        deadline=stage_deadline,
                        verbose=verbose,
                        progress=progress,
                    )
                except PdCodeSimplifyTimeoutError:
                    if timeout_expired(deadline):
                        raise
                    stage_timeout = True
                    _emit_progress(
                        verbose,
                        progress,
                        (
                            f"round {round_index} non_monotone_timeout "
                            f"seconds={NON_MONOTONE_TIME_SLICE_SECONDS}"
                        ),
                    )
                except Exception as exc:
                    stage_error = True
                    _emit_progress(
                        verbose,
                        progress,
                        f"round {round_index} non_monotone_error message=\"{exc}\"",
                    )
                output.tested_red_paths += non_monotone.tested_red_paths
                output.tested_green_paths += non_monotone.tested_green_paths
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} non_monotone_done "
                        f"found={'yes' if non_monotone.found else 'no'} "
                        f"depth={non_monotone.depth} "
                        f"steps={len(non_monotone.steps)} "
                        f"tested_red={non_monotone.tested_red_paths} "
                        f"tested_green={non_monotone.tested_green_paths} "
                        f"applied_candidates={non_monotone.applied_candidates} "
                        f"generated_states={non_monotone.generated_states} "
                        f"final_crossings={len(non_monotone.code) if non_monotone.found else len(output.code)} "
                        f"r1_moves={non_monotone.reidemeister_i_moves} "
                        f"r2_moves={non_monotone.reidemeister_ii_moves} "
                        f"r3_moves={non_monotone.reidemeister_iii_moves} "
                        f"nugatory_moves={non_monotone.nugatory_crossing_moves}"
                    ),
                )
                if stage_timeout:
                    _record_adaptive_timeout(adaptive_scheduler, "non_monotone")
                    return False
                if stage_error:
                    _record_adaptive_miss(adaptive_scheduler, "non_monotone")
                    return False
                if non_monotone.found:
                    _record_adaptive_success(adaptive_scheduler, "non_monotone")
                    for step in non_monotone.steps:
                        if (
                            reduction_round >= 0
                            and output.mid_simplification_rounds >= reduction_round
                        ):
                            break
                        step_round = output.mid_simplification_rounds + 1
                        before_step_crossings = len(output.code)
                        output.mid_simplification_rounds += 1
                        output.code = _canonical_output_code(step.code)
                        output.crossingless_components = step.crossingless_components
                        output.reidemeister_i_moves += step.reidemeister_i_moves
                        output.reidemeister_ii_moves += step.reidemeister_ii_moves
                        output.reidemeister_iii_moves += step.reidemeister_iii_moves
                        output.nugatory_crossing_moves += step.nugatory_crossing_moves
                        _emit_step_pd(
                            show_step_pd,
                            step_pd_output,
                            step_round,
                            output.code,
                        )
                        _emit_progress(
                            verbose,
                            progress,
                            (
                                f"round {step_round} non_monotone_applied "
                                f"kind={step.kind} "
                                f"crossings={before_step_crossings} -> {len(output.code)} "
                                f"red_length={step.red_length} "
                                f"green_length={step.green_length} "
                                f"crossingless_components={output.crossingless_components} "
                                f"r1_moves={step.reidemeister_i_moves} "
                                f"r2_moves={step.reidemeister_ii_moves} "
                                f"r3_moves={step.reidemeister_iii_moves} "
                                f"nugatory_moves={step.nugatory_crossing_moves}"
                            ),
                        )
                        if stop_if_quit_at_crossing("non_monotone"):
                            break
                    restart_round = True
                    return True
                _record_adaptive_miss(adaptive_scheduler, "non_monotone")
                return False

            if adaptive_mode:
                use_heuristic_soft_slice = (
                    timeout > 0 and not large_initial_heuristic_mode
                )
                if not run_heuristic_search(use_heuristic_soft_slice):
                    output.code = _canonical_output_code(output.code)
                    _emit_progress(
                        verbose,
                        progress,
                        "non_heuristic_handoff canonicalized=yes",
                    )
                    stage_order = _adaptive_stage_order(adaptive_scheduler)
                    _emit_progress(
                        verbose,
                        progress,
                        _adaptive_scheduler_log(round_index, adaptive_scheduler, stage_order),
                    )
                    for stage in stage_order:
                        if stage == "heuristic_search":
                            continue
                        if stage == "r3_prepass":
                            if run_r3_prepass():
                                break
                        else:
                            if run_non_monotone():
                                break
                if output.stopped_by_crossing_limit:
                    break
                if restart_round:
                    continue
            else:
                run_heuristic_search(False)

            if not search.found and adaptive_mode:
                output.last_path_search_mode = _search_mode(-1, True)
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} brute_fallback_start "
                        f"crossings={len(output.code)} max_thread={max_thread} "
                        f"bruteforce_budget={bruteforce_budget}"
                    ),
                )
                brute = find_simplification(
                    output.code,
                    max_paths=-1,
                    ban_heuristic=True,
                    require_applicable=True,
                    max_thread=max_thread,
                    bruteforce_budget=bruteforce_budget,
                    verbose=verbose,
                    progress=progress,
                    timeout=timeout,
                    _timeout_deadline=deadline,
                )
                output.tested_red_paths += brute.tested_red_paths
                output.tested_green_paths += brute.tested_green_paths
                output.last_path_search_mode = brute.path_search_mode
                output.resource_limited = output.resource_limited or brute.resource_limited
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} brute_fallback_done "
                        f"found={'yes' if brute.found else 'no'} "
                        f"tested_red={brute.tested_red_paths} "
                        f"tested_green={brute.tested_green_paths} "
                        f"resource_limited={'yes' if brute.resource_limited else 'no'}"
                    ),
                )
                if brute.found:
                    output.heuristic_failover_rounds += 1
                    search = brute

            if not search.found:
                if output.resource_limited:
                    _emit_progress(
                        verbose,
                        progress,
                        (
                            f"round {round_index} stop_resource_limited "
                            f"crossings={len(output.code)} "
                            f"tested_red_total={output.tested_red_paths} "
                            f"tested_green_total={output.tested_green_paths}"
                        ),
                    )
                    break

                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} r3_failover_start "
                        f"crossings={len(output.code)} "
                        f"max_depth={R3_FAILOVER_MAX_DEPTH} "
                        f"max_states={R3_FAILOVER_MAX_STATES}"
                    ),
                )
                r3 = find_reidemeister_iii_failover(
                    output.code,
                    output.crossingless_components,
                    timeout=timeout,
                    deadline=deadline,
                )
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} r3_failover_done "
                        f"found={'yes' if r3.found else 'no'} "
                        f"depth={r3.depth} "
                        f"visited_states={r3.visited_states} "
                        f"final_crossings={len(r3.code) if r3.found else len(output.code)} "
                        f"r1_moves={r3.reidemeister_i_moves} "
                        f"r2_moves={r3.reidemeister_ii_moves} "
                        f"r3_moves={r3.reidemeister_iii_moves} "
                        f"nugatory_moves={r3.nugatory_crossing_moves}"
                    ),
                )
                if r3.found:
                    output.code = _canonical_output_code(r3.code)
                    output.crossingless_components = r3.crossingless_components
                    output.reidemeister_i_moves += r3.reidemeister_i_moves
                    output.reidemeister_ii_moves += r3.reidemeister_ii_moves
                    output.reidemeister_iii_moves += r3.reidemeister_iii_moves
                    output.nugatory_crossing_moves += r3.nugatory_crossing_moves
                    if stop_if_quit_at_crossing("r3_failover"):
                        break
                    continue

                _emit_progress(
                    verbose,
                    progress,
                    f"round {round_index} stop_no_path crossings={len(output.code)}",
                )
                break

            before_apply_crossings = len(output.code)
            check_timeout(timeout, deadline)
            reduced_code, reduced_crossingless = apply_simplification_witness(
                output.code,
                search,
                output.crossingless_components,
            )
            heuristic_witness = search.path_search_mode == "heuristic"
            step_code = _canonical_output_code(reduced_code)
            reduced_code = (
                [tuple(crossing) for crossing in reduced_code]
                if heuristic_witness
                else step_code
            )
            output.mid_simplification_rounds += 1
            _emit_step_pd(show_step_pd, step_pd_output, round_index, step_code)
            output.code = reduced_code
            output.crossingless_components = reduced_crossingless
            check_timeout(timeout, deadline)
            prepared = simplify_pd_code(
                output.code,
                output.crossingless_components,
                timeout,
                deadline,
                allow_reidemeister_ii=not heuristic_witness,
                canonicalize_input=not heuristic_witness,
            )
            output.code = (
                [tuple(crossing) for crossing in prepared.code]
                if heuristic_witness
                else _canonical_output_code(prepared.code)
            )
            output.crossingless_components = prepared.crossingless_components
            output.reidemeister_i_moves += prepared.reidemeister_i_moves
            output.reidemeister_ii_moves += prepared.reidemeister_ii_moves
            output.nugatory_crossing_moves += prepared.nugatory_crossing_moves
            check_timeout(timeout, deadline)
            _emit_progress(
                verbose,
                progress,
                (
                    f"round {round_index} applied crossings={before_apply_crossings} "
                    f"-> {len(reduced_code)} -> {len(output.code)} "
                    f"crossingless_components={output.crossingless_components} "
                    f"r1_moves={prepared.reidemeister_i_moves} "
                    f"r2_moves={prepared.reidemeister_ii_moves} "
                    f"nugatory_moves={prepared.nugatory_crossing_moves}"
                ),
            )
            stop_if_quit_at_crossing("mid_simplification")
    except PdCodeSimplifyTimeoutError as exc:
        output.timed_out = True
        _emit_progress(
            verbose,
            progress,
            (
                f"{exc}; returning_current_best crossings={len(output.code)} "
                f"crossingless_components={output.crossingless_components} "
                f"mid_rounds={output.mid_simplification_rounds}"
            ),
        )

    output.stopped_by_round_limit = (
        not output.timed_out
        and not output.resource_limited
        and not output.stopped_by_crossing_limit
        and reduction_round >= 0
        and output.mid_simplification_rounds >= reduction_round
    )
    output.code = _canonical_output_code(output.code)
    _emit_progress(
        verbose,
        progress,
        (
            f"done final_crossings={len(output.code)} "
            f"crossingless_components={output.crossingless_components} "
            f"mid_rounds={output.mid_simplification_rounds} "
            f"heuristic_failover_rounds={output.heuristic_failover_rounds} "
            f"reapr_used={'yes' if output.reapr_used else 'no'} "
            f"reapr_status={output.reapr_status} "
            f"r2_moves={output.reidemeister_ii_moves} "
            f"r3_moves={output.reidemeister_iii_moves} "
            f"stopped_by_round_limit={'yes' if output.stopped_by_round_limit else 'no'} "
            f"stopped_by_crossing_limit={'yes' if output.stopped_by_crossing_limit else 'no'} "
            f"timed_out={'yes' if output.timed_out else 'no'} "
            f"resource_limited={'yes' if output.resource_limited else 'no'}"
        ),
    )
    return output


def label_for_block(text: str, block_start: int, label_prefix: str, index: int) -> str:
    line_start = text.rfind("\n", 0, block_start)
    line_start = 0 if line_start == -1 else line_start + 1
    before_block = text[line_start:block_start]
    colon = before_block.find(":")
    if colon != -1:
        line_label = trim(before_block[:colon])
        if line_label:
            return f"{label_prefix}:{line_label}"
    return label_prefix if index == 0 else f"{label_prefix}#{index + 1}"


def parse_pd_document(text: str, label_prefix: str = "input") -> List[PDJob]:
    jobs: List[PDJob] = []
    pos = 0
    index = 0
    while True:
        start = text.find("PD[", pos)
        if start == -1:
            break
        depth = 0
        end = -1
        for i in range(start + 2, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            jobs.append(
                PDJob(
                    label=f"{label_prefix}#{index + 1}",
                    error="Unterminated PD[...] block",
                )
            )
            break
        block = text[start : end + 1]
        job = PDJob(label=label_for_block(text, start, label_prefix, index))
        try:
            job.code = parse_pd_code(block)
            job.implied_crossingless_components = 1 if denotes_crossingless_unknot(block) else 0
        except Exception as exc:
            job.error = str(exc)
        jobs.append(job)
        index += 1
        pos = end + 1

    if jobs:
        return jobs

    for line in text.splitlines():
        cleaned = trim(line)
        if not cleaned or cleaned.startswith("#"):
            continue
        payload = cleaned
        label = label_prefix
        if ":" in cleaned:
            line_label, payload = cleaned.split(":", 1)
            line_label = trim(line_label)
            payload = trim(payload)
            if line_label:
                label = f"{label}:{line_label}"
        elif jobs:
            label = f"{label}#{len(jobs) + 1}"
        if not any(ch.isdigit() for ch in payload) and not denotes_crossingless_unknot(payload):
            continue
        job = PDJob(label=label)
        try:
            job.code = parse_pd_code(payload)
            job.implied_crossingless_components = 1 if denotes_crossingless_unknot(payload) else 0
        except Exception as exc:
            job.error = str(exc)
        jobs.append(job)
    return jobs


def read_pd_file(path: str) -> List[PDJob]:
    text = Path(path).read_text(encoding="utf-8")
    jobs = parse_pd_document(text, path)
    if len(jobs) == 1:
        jobs[0].label = path
    return jobs


def list_input_files(directory: str) -> List[str]:
    paths = []
    for entry in Path(directory).iterdir():
        if entry.is_file() and entry.suffix.lower() in {".pd", ".txt"}:
            paths.append(str(entry))
    return sorted(paths)


def run_job(
    job: PDJob,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    reduction_round: int = -1,
    max_thread: int = -1,
    bruteforce_budget: int = DEFAULT_BRUTEFORCE_BUDGET,
    timeout: int = -1,
    quit_at_crossing: int = -1,
    known_crossingless_components: int = 0,
    removed_crossings: Optional[Sequence[int]] = None,
    verbose: bool = False,
    show_step_pd: bool = False,
    step_label: Optional[str] = None,
    reapr: bool = False,
    reapr_retry_max: int = 3,
) -> Tuple[
    ReductionResult,
    ComponentAnalysis,
    Optional[ComponentAnalysis],
]:
    if job.error:
        raise ValueError(job.error)
    crossingless = known_crossingless_components + job.implied_crossingless_components
    input_components = analyze_components(job.code, crossingless)
    after_removal = None
    if removed_crossings is not None:
        after_removal = analyze_components_after_removing_crossings(
            job.code, removed_crossings, crossingless
        )
    return (
        reduce_pd_code(
            job.code,
            known_crossingless_components=crossingless,
            max_paths=max_paths,
            ban_heuristic=ban_heuristic,
            reduction_round=reduction_round,
            max_thread=max_thread,
            bruteforce_budget=bruteforce_budget,
            timeout=timeout,
            quit_at_crossing=quit_at_crossing,
            verbose=verbose,
            progress=lambda message: print(
                format_progress_log(f"{job.label}: {message}"), file=sys.stderr
            ),
            show_step_pd=show_step_pd,
            reapr=reapr,
            reapr_retry_max=reapr_retry_max,
            step_pd_output=(
                lambda round_index, step_code: print(
                    (
                        f"{step_label}: " if step_label else ""
                    )
                    + f"step_pd_code[{round_index}]: {format_final_pd_code(step_code)}",
                    flush=True,
                )
            ),
        ),
        input_components,
        after_removal,
    )


def print_text_result(
    result: ReductionResult,
    input_components: ComponentAnalysis,
    after_removal_components: Optional[ComponentAnalysis] = None,
) -> None:
    final_components = analyze_components(result.code, result.crossingless_components)
    simplification_found = (
        result.mid_simplification_rounds > 0
        or result.reidemeister_i_moves > 0
        or result.reidemeister_ii_moves > 0
        or result.reidemeister_iii_moves > 0
        or result.nugatory_crossing_moves > 0
        or result.reapr_used
    )
    print(f"simplification_found: {'yes' if simplification_found else 'no'}")
    print(f"input_components_with_crossings: {input_components.components_with_crossings}")
    print(f"input_crossingless_components: {input_components.crossingless_components}")
    print(f"input_total_components: {input_components.total_components}")
    if after_removal_components is not None:
        print(
            "after_removal_components_with_crossings: "
            f"{after_removal_components.components_with_crossings}"
        )
        print(
            "after_removal_crossingless_components: "
            f"{after_removal_components.crossingless_components}"
        )
        print(f"after_removal_total_components: {after_removal_components.total_components}")
    print(f"final_pd_code: {format_final_pd_code(result.code)}")
    print(f"final_crossings: {len(result.code)}")
    print(f"final_components_with_crossings: {final_components.components_with_crossings}")
    print(f"final_crossingless_components: {final_components.crossingless_components}")
    print(f"final_total_components: {final_components.total_components}")
    print(f"mid_simplification_rounds: {result.mid_simplification_rounds}")
    print(f"heuristic_failover_rounds: {result.heuristic_failover_rounds}")
    print(f"reidemeister_i_moves: {result.reidemeister_i_moves}")
    print(f"reidemeister_ii_moves: {result.reidemeister_ii_moves}")
    print(f"reidemeister_iii_moves: {result.reidemeister_iii_moves}")
    print(f"nugatory_crossing_moves: {result.nugatory_crossing_moves}")
    print(f"tested_red_paths: {result.tested_red_paths}")
    print(f"tested_green_paths: {result.tested_green_paths}")
    print(f"last_path_search_mode: {result.last_path_search_mode}")
    print(f"reapr_used: {'yes' if result.reapr_used else 'no'}")
    print(f"reapr_rounds: {result.reapr_rounds}")
    print(f"reapr_attempts: {result.reapr_attempts}")
    print(f"reapr_rejected: {'yes' if result.reapr_rejected else 'no'}")
    print(f"reapr_status: {result.reapr_status}")
    if result.reapr_warning:
        print(f"reapr_warning: {result.reapr_warning}")
    print(f"alexander_determinant_before: {result.alexander_determinant_before}")
    print(f"alexander_determinant_after: {result.alexander_determinant_after}")
    print(f"reapr_invariants_before: {result.reapr_invariants_before}")
    print(f"reapr_invariants_after: {result.reapr_invariants_after}")
    print(f"stopped_by_round_limit: {'yes' if result.stopped_by_round_limit else 'no'}")
    print(f"stopped_by_crossing_limit: {'yes' if result.stopped_by_crossing_limit else 'no'}")
    print(f"timed_out: {'yes' if result.timed_out else 'no'}")
    print(f"resource_limited: {'yes' if result.resource_limited else 'no'}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simplify PD code by applying R1, nugatory, and mid-simplification moves."
    )
    parser.add_argument("inputs", nargs="*", help="PD strings, files, or directories")
    parser.add_argument("--pd-code", "-c", action="append", help="literal PD[...] string")
    parser.add_argument("--pd-file", "-f", action="append", help="read one PD input file")
    parser.add_argument("--pd-dir", "-d", action="append", help="read every .txt/.pd file in a directory")
    parser.add_argument("--input", "-i", action="append", help="alias for --pd-file")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--max-paths", type=int, default=-1, help="green path cap, or -1 for heuristic sampling")
    parser.add_argument(
        "--max-thread",
        type=int,
        default=-1,
        help="maximum brute-force worker processes, or -1 to choose automatically",
    )
    parser.add_argument(
        "--bruteforce-budget",
        type=int,
        default=DEFAULT_BRUTEFORCE_BUDGET,
        help="maximum brute-force green-path checks per PD code, or -1 for no cap",
    )
    parser.add_argument("--ban-heuristic", action="store_true",
                        help="with --max-paths -1, enumerate all green paths instead of heuristic sampling")
    parser.add_argument("--verbose", action="store_true", help="print progress logs to stderr")
    parser.add_argument(
        "--log-file",
        help="tee stdout and stderr output into this flushed log file",
    )
    parser.add_argument(
        "--show-step-pd",
        action="store_true",
        help="print the PD code after each witness application",
    )
    parser.add_argument(
        "--reapr",
        action="store_true",
        help="enable the experimental invariant-guarded projection oracle",
    )
    parser.add_argument(
        "--reapr-retry-max",
        type=int,
        default=3,
        help="maximum deterministic REAPR retry attempts",
    )
    parser.add_argument(
        "--reduction-round",
        type=int,
        default=-1,
        help="maximum mid-simplification rounds, or -1 to continue until stable",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=-1,
        help="per-PD-code timeout in seconds, or -1 for no timeout",
    )
    parser.add_argument(
        "--quit-at-crossing",
        type=int,
        default=-1,
        help="stop once crossings are at most N, or -1 to disable",
    )
    parser.add_argument(
        "--known-crossingless-components",
        type=int,
        default=0,
        help="components already missing from the PD code",
    )
    parser.add_argument(
        "--remove-crossings",
        help="comma-separated zero-based crossing indices for deletion accounting",
    )
    return parser


def collect_jobs(args: argparse.Namespace) -> List[PDJob]:
    jobs: List[PDJob] = []
    files: List[str] = []

    for literal in args.pd_code or []:
        jobs.extend(parse_pd_document(literal, "command-line"))
    for path in args.pd_file or []:
        files.append(path)
    for path in args.input or []:
        files.append(path)
    for directory in args.pd_dir or []:
        files.extend(list_input_files(directory))

    for item in args.inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(list_input_files(str(path)))
        elif path.is_file():
            files.append(str(path))
        else:
            jobs.extend(parse_pd_document(item, "command-line"))

    if not files and not jobs:
        files.append("PD.txt")

    for path in files:
        jobs.extend(read_pd_file(path))
    if not jobs:
        raise ValueError("No PD code found")
    return jobs


def parse_removed_crossings(text: Optional[str]) -> Optional[List[int]]:
    if text is None:
        return None
    return [int(token) for token in re.findall(r"-?\d+", text)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        selected_log_file = log_file_arg(raw_argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with tee_standard_streams(selected_log_file):
        return main_impl(raw_argv)


def main_impl(argv: Sequence[str]) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.reduction_round < -1:
        parser.error("--reduction-round must be -1 or a non-negative integer")
    if args.max_thread < -1 or args.max_thread == 0:
        parser.error("--max-thread must be -1 or a positive integer")
    if args.bruteforce_budget < -1 or args.bruteforce_budget == 0:
        parser.error("--bruteforce-budget must be -1 or a positive integer")
    if args.timeout < -1 or args.timeout == 0:
        parser.error("--timeout must be -1 or a positive integer")
    if args.quit_at_crossing < -1:
        parser.error("--quit-at-crossing must be -1 or a non-negative integer")
    if args.reapr_retry_max < 0:
        parser.error("--reapr-retry-max must be a non-negative integer")
    jobs = collect_jobs(args)
    removed_crossings = parse_removed_crossings(args.remove_crossings)
    show_labels = len(jobs) > 1
    had_error = False
    interrupted = False

    if args.json:
        payload = []
        for job in jobs:
            try:
                result, input_components, after_removal = run_job(
                    job,
                    max_paths=args.max_paths,
                    ban_heuristic=args.ban_heuristic,
                    reduction_round=args.reduction_round,
                    max_thread=args.max_thread,
                    bruteforce_budget=args.bruteforce_budget,
                    timeout=args.timeout,
                    quit_at_crossing=args.quit_at_crossing,
                    known_crossingless_components=args.known_crossingless_components,
                    removed_crossings=removed_crossings,
                    verbose=args.verbose,
                    show_step_pd=args.show_step_pd,
                    reapr=args.reapr,
                    reapr_retry_max=args.reapr_retry_max,
                    step_label=job.label if show_labels else None,
                )
                final_components = analyze_components(
                    result.code, result.crossingless_components
                )
                payload.append(
                    result.to_json(
                        input_components=input_components,
                        after_removal_components=after_removal,
                        final_components=final_components,
                        label=job.label if show_labels else None,
                    )
                )
                if result.timed_out or result.resource_limited:
                    had_error = True
            except KeyboardInterrupt:
                had_error = True
                interrupted = True
                item = {"error": "interrupted by Ctrl+C"}
                if show_labels:
                    item["label"] = job.label
                payload.append(item)
                break
            except Exception as exc:
                had_error = True
                item: Dict[str, object] = {"error": str(exc)}
                if show_labels:
                    item["label"] = job.label
                payload.append(item)
        print(json.dumps(payload if show_labels else payload[0], indent=2))
    else:
        for index, job in enumerate(jobs):
            if show_labels:
                print(f"{job.label}:")
            try:
                result, input_components, after_removal = run_job(
                    job,
                    max_paths=args.max_paths,
                    ban_heuristic=args.ban_heuristic,
                    reduction_round=args.reduction_round,
                    max_thread=args.max_thread,
                    bruteforce_budget=args.bruteforce_budget,
                    timeout=args.timeout,
                    quit_at_crossing=args.quit_at_crossing,
                    known_crossingless_components=args.known_crossingless_components,
                    removed_crossings=removed_crossings,
                    verbose=args.verbose,
                    show_step_pd=args.show_step_pd,
                    reapr=args.reapr,
                    reapr_retry_max=args.reapr_retry_max,
                    step_label=job.label if show_labels else None,
                )
                print_text_result(result, input_components, after_removal)
                if result.timed_out or result.resource_limited:
                    had_error = True
            except KeyboardInterrupt:
                had_error = True
                interrupted = True
                print("error: interrupted by Ctrl+C")
                break
            except Exception as exc:
                had_error = True
                print(f"error: {exc}")
            if show_labels and index + 1 < len(jobs):
                print()
    if interrupted:
        return 130
    if had_error:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI guard
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
