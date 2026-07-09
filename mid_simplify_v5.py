"""Pure Python PD-code mid-simplification prototype.

This module is the cleaned-up Python counterpart of the C++ implementation.
It exposes both a Python API and a command-line interface using the same
PD-code input style as the project executable and `cppkh`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import heapq
import json
import multiprocessing
import os
import re
import sys
import time
from collections import deque
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


def local_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_progress_log(message: str) -> str:
    return f"[pdcode-simplify {local_timestamp()}] {message}"


class PdCodeSimplifyTimeoutError(RuntimeError):
    pass


def validate_timeout(timeout: int) -> None:
    if timeout < -1 or timeout == 0:
        raise ValueError("timeout must be -1 or a positive integer")


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
    tested_green_paths: int = 0
    witness: SimplificationResult = field(default_factory=SimplificationResult)


@dataclass
class ReductionResult:
    code: PDCode
    crossingless_components: int = 0
    mid_simplification_rounds: int = 0
    heuristic_failover_rounds: int = 0
    reidemeister_i_moves: int = 0
    nugatory_crossing_moves: int = 0
    tested_red_paths: int = 0
    tested_green_paths: int = 0
    last_path_search_mode: str = ""
    stopped_by_round_limit: bool = False
    timed_out: bool = False

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
        data["simplification_found"] = self.mid_simplification_rounds > 0
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
            "nugatory_crossing_moves": self.nugatory_crossing_moves,
            "tested_red_paths": self.tested_red_paths,
            "tested_green_paths": self.tested_green_paths,
            "last_path_search_mode": self.last_path_search_mode,
            "stopped_by_round_limit": self.stopped_by_round_limit,
            "timed_out": self.timed_out,
        })
        return data


@dataclass
class PDSimplificationResult:
    code: PDCode
    crossingless_components: int = 0
    reidemeister_i_moves: int = 0
    nugatory_crossing_moves: int = 0

    def to_json(self) -> Dict[str, object]:
        return {
            "enabled": True,
            "reidemeister_i_moves": self.reidemeister_i_moves,
            "nugatory_crossing_moves": self.nugatory_crossing_moves,
            "output_crossings": len(self.code),
        }


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
            moves += 1
            changed = True
            break
        if not changed:
            break
    return renumber_r1_order(result), crossingless_components, moves


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
    return renumber_full_dfs(result), after_removal.crossingless_components


def simplify_pd_code(
    code: PDCode,
    known_crossingless_components: int = 0,
) -> PDSimplificationResult:
    result = PDSimplificationResult(
        code=[tuple(crossing) for crossing in code],
        crossingless_components=known_crossingless_components,
    )
    result.code, result.crossingless_components, result.reidemeister_i_moves = erase_r1_moves(
        result.code,
        result.crossingless_components,
    )
    while True:
        index = find_nugatory_crossing(result.code)
        if index < 0:
            break
        result.code, result.crossingless_components = erase_one_nugatory_crossing(
            result.code,
            index,
            result.crossingless_components,
        )
        result.nugatory_crossing_moves += 1
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
_PARALLEL_TIMEOUT = -1
_PARALLEL_TIMEOUT_DEADLINE: Optional[float] = None


def _parallel_bruteforce_initializer(
    code: PDCode,
    red_lines: List[List[Endpoint]],
    require_applicable: bool,
    best_index: Any,
    best_lock: Any,
    timeout: int,
    deadline: Optional[float],
) -> None:
    global _PARALLEL_CODE
    global _PARALLEL_DIAGRAM
    global _PARALLEL_BASE_GRAPH
    global _PARALLEL_RED_LINES
    global _PARALLEL_REQUIRE_APPLICABLE
    global _PARALLEL_BEST_INDEX
    global _PARALLEL_BEST_LOCK
    global _PARALLEL_TIMEOUT
    global _PARALLEL_TIMEOUT_DEADLINE

    _PARALLEL_CODE = code
    check_timeout(timeout, deadline)
    _PARALLEL_DIAGRAM = Diagram(code)
    check_timeout(timeout, deadline)
    _PARALLEL_BASE_GRAPH = DualGraph(_PARALLEL_DIAGRAM)
    _PARALLEL_RED_LINES = red_lines
    _PARALLEL_REQUIRE_APPLICABLE = require_applicable
    _PARALLEL_BEST_INDEX = best_index
    _PARALLEL_BEST_LOCK = best_lock
    _PARALLEL_TIMEOUT = timeout
    _PARALLEL_TIMEOUT_DEADLINE = deadline


def _parallel_should_skip(red_index: int) -> bool:
    return _PARALLEL_BEST_INDEX is not None and red_index > _PARALLEL_BEST_INDEX.value


def _parallel_record_found(red_index: int) -> None:
    if _PARALLEL_BEST_INDEX is None or _PARALLEL_BEST_LOCK is None:
        return
    with _PARALLEL_BEST_LOCK:
        if red_index < _PARALLEL_BEST_INDEX.value:
            _PARALLEL_BEST_INDEX.value = red_index


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


def collect_simple_paths(
    graph: DualGraph,
    source: int,
    target: int,
    cutoff: int,
    max_paths: int,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> List[List[int]]:
    check_timeout(timeout, _timeout_deadline)
    if (
        source < 0
        or target < 0
        or source >= len(graph.faces)
        or target >= len(graph.faces)
        or cutoff <= 0
    ):
        return []
    if source == target:
        return [[source]]

    paths: List[List[int]] = []
    visited = [False for _ in graph.faces]
    current_path = [source]
    distance = heuristic_distances_to_target(graph, target, cutoff, timeout, _timeout_deadline)
    visited[source] = True

    def dfs(current: int, current_weight: int) -> None:
        check_timeout(timeout, _timeout_deadline)
        if len(current_path) - 1 >= cutoff:
            return
        if (
            current < 0
            or current >= len(distance)
            or distance[current] >= 10**9
            or current_weight + distance[current] >= cutoff
        ):
            return
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
                paths.append(list(current_path))
                if max_paths != -1 and len(paths) > max_paths:
                    visited[nxt] = False
                    current_path.pop()
                    return
            else:
                dfs(nxt, next_weight)
                if max_paths != -1 and len(paths) > max_paths:
                    visited[nxt] = False
                    current_path.pop()
                    return
            visited[nxt] = False
            current_path.pop()

    dfs(source, 0)
    return paths


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


def opposite_level(level: str) -> str:
    return "over" if level == "under" else "under"


def do_check(
    diagram: Diagram,
    graph: DualGraph,
    red_path: List[Endpoint],
    green_path: List[int],
    direction: str,
    result: SimplificationResult,
) -> bool:
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
        start_key = to_check.pop()
        queued.discard(start_key)
        cross_strand = endpoint_from_key(start_key)

        while True:
            cross_key = cross_strand.key
            current_level = check_result[cross_key]
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
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> RedPathSearchOutcome:
    check_timeout(timeout, _timeout_deadline)
    outcome = RedPathSearchOutcome()
    outcome.witness.path_search_mode = path_search_mode
    if should_skip is not None and should_skip():
        outcome.skipped = True
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

    paths: List[List[int]] = []
    cutoff = len(red_path) - 1
    for source in sources:
        for destination in destinations:
            check_timeout(timeout, _timeout_deadline)
            if should_skip is not None and should_skip():
                outcome.skipped = True
                return outcome
            if max_paths == -1 and not ban_heuristic:
                found = collect_heuristic_paths(
                    graph, source, destination, cutoff, timeout, _timeout_deadline
                )
            else:
                found = collect_simple_paths(
                    graph, source, destination, cutoff, max_paths, timeout, _timeout_deadline
                )
            paths.extend(found)
            if max_paths != -1 and len(paths) > max_paths:
                break

    for green_path in paths:
        check_timeout(timeout, _timeout_deadline)
        outcome.tested_green_paths += 1
        if len(green_path) >= len(red_path):
            continue
        if do_check(diagram, graph, red_path, green_path, "left", outcome.witness):
            if not require_applicable or witness_has_applicable_surgery(code, outcome.witness):
                outcome.found = True
                outcome.completed = True
                outcome.witness.tested_green_paths = outcome.tested_green_paths
                return outcome
            outcome.witness = SimplificationResult(path_search_mode=path_search_mode)
        if do_check(diagram, graph, red_path, green_path, "right", outcome.witness):
            if not require_applicable or witness_has_applicable_surgery(code, outcome.witness):
                outcome.found = True
                outcome.completed = True
                outcome.witness.tested_green_paths = outcome.tested_green_paths
                return outcome
            outcome.witness = SimplificationResult(path_search_mode=path_search_mode)

    outcome.completed = True
    outcome.witness.tested_green_paths = outcome.tested_green_paths
    return outcome


def _parallel_bruteforce_worker(red_index: int) -> RedPathSearchOutcome:
    check_timeout(_PARALLEL_TIMEOUT, _PARALLEL_TIMEOUT_DEADLINE)
    if (
        _PARALLEL_CODE is None
        or _PARALLEL_DIAGRAM is None
        or _PARALLEL_BASE_GRAPH is None
        or _PARALLEL_RED_LINES is None
    ):
        raise RuntimeError("Parallel brute-force worker was not initialized")
    if _parallel_should_skip(red_index):
        return RedPathSearchOutcome(skipped=True)
    outcome = search_single_red_path(
        _PARALLEL_CODE,
        _PARALLEL_DIAGRAM,
        _PARALLEL_BASE_GRAPH,
        _PARALLEL_RED_LINES[red_index],
        max_paths=-1,
        ban_heuristic=True,
        require_applicable=_PARALLEL_REQUIRE_APPLICABLE,
        path_search_mode="bruteforce",
        should_skip=lambda: _parallel_should_skip(red_index),
        timeout=_PARALLEL_TIMEOUT,
        _timeout_deadline=_PARALLEL_TIMEOUT_DEADLINE,
    )
    if outcome.found:
        _parallel_record_found(red_index)
    return outcome


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
        if not outcome.completed and not outcome.found:
            raise RuntimeError(
                f"Parallel brute-force search did not complete red path {index}"
            )
        result.tested_red_paths += 1
        result.tested_green_paths += outcome.tested_green_paths

    if first_found >= 0:
        witness = outcomes[first_found].witness
        witness.path_search_mode = path_search_mode
        witness.tested_red_paths = first_found + 1
        witness.tested_green_paths = sum(
            outcomes[index].tested_green_paths for index in range(first_found + 1)
        )
        return witness
    return result


def find_simplification_parallel_bruteforce(
    code: PDCode,
    red_lines: List[List[Endpoint]],
    require_applicable: bool,
    worker_count: int,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> SimplificationResult:
    check_timeout(timeout, _timeout_deadline)
    if not red_lines:
        return SimplificationResult(path_search_mode="bruteforce")
    outcomes: List[RedPathSearchOutcome] = [
        RedPathSearchOutcome() for _ in red_lines
    ]
    with multiprocessing.Manager() as manager:
        best_index = manager.Value("i", len(red_lines))
        best_lock = manager.Lock()
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
                    timeout,
                    _timeout_deadline,
                ),
            )
            futures = [
                executor.submit(_parallel_bruteforce_worker, index)
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
    return merge_red_path_outcomes(outcomes, "bruteforce")


def find_simplification(
    code: PDCode,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    require_applicable: bool = False,
    max_thread: int = -1,
    verbose: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    timeout: int = -1,
    _timeout_deadline: Optional[float] = None,
) -> SimplificationResult:
    if max_thread < -1 or max_thread == 0:
        raise ValueError("max_thread must be -1 or a positive integer")
    deadline = timeout_deadline(timeout, _timeout_deadline)
    check_timeout(timeout, deadline)
    result = SimplificationResult()
    if max_paths == -1 and not ban_heuristic:
        result.path_search_mode = "heuristic"
    elif max_paths == -1:
        result.path_search_mode = "bruteforce"
    else:
        result.path_search_mode = "bounded"
    diagram = Diagram(code)
    check_timeout(timeout, deadline)
    base_graph = DualGraph(diagram)
    check_timeout(timeout, deadline)
    red_lines = possible_red_lines(diagram)
    check_timeout(timeout, deadline)
    if max_paths == -1 and ban_heuristic:
        worker_count = selected_bruteforce_worker_count(max_thread, len(red_lines))
        if max_thread == -1:
            _emit_progress(
                verbose,
                progress,
                (
                    f"bruteforce_threads max_thread=-1 "
                    f"actual_threads={worker_count} red_paths={len(red_lines)}"
                ),
            )
        if worker_count > 1:
            return find_simplification_parallel_bruteforce(
                code,
                red_lines,
                require_applicable,
                worker_count,
                timeout,
                deadline,
            )

    for red_path in red_lines:
        check_timeout(timeout, deadline)
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
            timeout=timeout,
            _timeout_deadline=deadline,
        )
        result.tested_green_paths += outcome.tested_green_paths
        if outcome.found:
            witness = outcome.witness
            witness.path_search_mode = result.path_search_mode
            witness.tested_red_paths = result.tested_red_paths
            witness.tested_green_paths = result.tested_green_paths
            return witness

    return result


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


def reduce_pd_code(
    code: PDCode,
    known_crossingless_components: int = 0,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    reduction_round: int = -1,
    max_thread: int = -1,
    timeout: int = -1,
    verbose: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    show_step_pd: bool = False,
    step_pd_output: Optional[Callable[[int, PDCode], None]] = None,
    _timeout_deadline: Optional[float] = None,
) -> ReductionResult:
    if max_thread < -1 or max_thread == 0:
        raise ValueError("max_thread must be -1 or a positive integer")
    deadline = timeout_deadline(timeout, _timeout_deadline)
    output = ReductionResult(
        code=[list(crossing) for crossing in code],
        crossingless_components=known_crossingless_components,
    )
    try:
        check_timeout(timeout, deadline)
        _emit_progress(
            verbose,
            progress,
            (
                f"start input_crossings={len(code)} "
                f"known_crossingless_components={known_crossingless_components} "
                f"reduction_round={reduction_round} max_paths={max_paths} "
                f"max_thread={max_thread} timeout={timeout} "
                f"heuristic={'off' if ban_heuristic else 'on'}"
            ),
        )
        prepared = simplify_pd_code(code, known_crossingless_components)
        output.code = prepared.code
        output.crossingless_components = prepared.crossingless_components
        output.reidemeister_i_moves = prepared.reidemeister_i_moves
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
                f"nugatory_moves={prepared.nugatory_crossing_moves}"
            ),
        )

        while reduction_round < 0 or output.mid_simplification_rounds < reduction_round:
            check_timeout(timeout, deadline)
            round_index = output.mid_simplification_rounds + 1
            output.last_path_search_mode = _search_mode(max_paths, ban_heuristic)
            _emit_progress(
                verbose,
                progress,
                (
                    f"round {round_index} search_start crossings={len(output.code)} "
                    f"mode={output.last_path_search_mode} "
                    f"max_thread={max_thread}"
                ),
            )
            search = find_simplification(
                output.code,
                max_paths=max_paths,
                ban_heuristic=ban_heuristic,
                require_applicable=True,
                max_thread=max_thread,
                verbose=verbose,
                progress=progress,
                timeout=timeout,
                _timeout_deadline=deadline,
            )
            output.tested_red_paths += search.tested_red_paths
            output.tested_green_paths += search.tested_green_paths
            output.last_path_search_mode = search.path_search_mode
            _emit_progress(
                verbose,
                progress,
                (
                    f"round {round_index} search_done found={'yes' if search.found else 'no'} "
                    f"mode={search.path_search_mode} "
                    f"tested_red={search.tested_red_paths} "
                    f"tested_green={search.tested_green_paths}"
                ),
            )

            if (
                not search.found
                and max_paths == -1
                and not ban_heuristic
            ):
                output.last_path_search_mode = _search_mode(-1, True)
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} brute_fallback_start "
                        f"crossings={len(output.code)} max_thread={max_thread}"
                    ),
                )
                brute = find_simplification(
                    output.code,
                    max_paths=-1,
                    ban_heuristic=True,
                    require_applicable=True,
                    max_thread=max_thread,
                    verbose=verbose,
                    progress=progress,
                    timeout=timeout,
                    _timeout_deadline=deadline,
                )
                output.tested_red_paths += brute.tested_red_paths
                output.tested_green_paths += brute.tested_green_paths
                output.last_path_search_mode = brute.path_search_mode
                _emit_progress(
                    verbose,
                    progress,
                    (
                        f"round {round_index} brute_fallback_done "
                        f"found={'yes' if brute.found else 'no'} "
                        f"tested_red={brute.tested_red_paths} "
                        f"tested_green={brute.tested_green_paths}"
                    ),
                )
                if brute.found:
                    output.heuristic_failover_rounds += 1
                    search = brute

            if not search.found:
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
            output.mid_simplification_rounds += 1
            _emit_step_pd(show_step_pd, step_pd_output, round_index, reduced_code)
            output.code = reduced_code
            output.crossingless_components = reduced_crossingless
            check_timeout(timeout, deadline)
            prepared = simplify_pd_code(output.code, output.crossingless_components)
            output.code = prepared.code
            output.crossingless_components = prepared.crossingless_components
            output.reidemeister_i_moves += prepared.reidemeister_i_moves
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
                    f"nugatory_moves={prepared.nugatory_crossing_moves}"
                ),
            )
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
        and reduction_round >= 0
        and output.mid_simplification_rounds >= reduction_round
    )
    _emit_progress(
        verbose,
        progress,
        (
            f"done final_crossings={len(output.code)} "
            f"crossingless_components={output.crossingless_components} "
            f"mid_rounds={output.mid_simplification_rounds} "
            f"heuristic_failover_rounds={output.heuristic_failover_rounds} "
            f"stopped_by_round_limit={'yes' if output.stopped_by_round_limit else 'no'} "
            f"timed_out={'yes' if output.timed_out else 'no'}"
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
    timeout: int = -1,
    known_crossingless_components: int = 0,
    removed_crossings: Optional[Sequence[int]] = None,
    verbose: bool = False,
    show_step_pd: bool = False,
    step_label: Optional[str] = None,
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
            timeout=timeout,
            verbose=verbose,
            progress=lambda message: print(
                format_progress_log(f"{job.label}: {message}"), file=sys.stderr
            ),
            show_step_pd=show_step_pd,
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
    print(f"simplification_found: {'yes' if result.mid_simplification_rounds > 0 else 'no'}")
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
    print(f"nugatory_crossing_moves: {result.nugatory_crossing_moves}")
    print(f"tested_red_paths: {result.tested_red_paths}")
    print(f"tested_green_paths: {result.tested_green_paths}")
    print(f"last_path_search_mode: {result.last_path_search_mode}")
    print(f"stopped_by_round_limit: {'yes' if result.stopped_by_round_limit else 'no'}")
    print(f"timed_out: {'yes' if result.timed_out else 'no'}")


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
    parser.add_argument("--ban-heuristic", action="store_true",
                        help="with --max-paths -1, enumerate all green paths instead of heuristic sampling")
    parser.add_argument("--verbose", action="store_true", help="print progress logs to stderr")
    parser.add_argument(
        "--show-step-pd",
        action="store_true",
        help="print the PD code after each witness application",
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
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.reduction_round < -1:
        parser.error("--reduction-round must be -1 or a non-negative integer")
    if args.max_thread < -1 or args.max_thread == 0:
        parser.error("--max-thread must be -1 or a positive integer")
    if args.timeout < -1 or args.timeout == 0:
        parser.error("--timeout must be -1 or a positive integer")
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
                    timeout=args.timeout,
                    known_crossingless_components=args.known_crossingless_components,
                    removed_crossings=removed_crossings,
                    verbose=args.verbose,
                    show_step_pd=args.show_step_pd,
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
                if result.timed_out:
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
                    timeout=args.timeout,
                    known_crossingless_components=args.known_crossingless_components,
                    removed_crossings=removed_crossings,
                    verbose=args.verbose,
                    show_step_pd=args.show_step_pd,
                    step_label=job.label if show_labels else None,
                )
                print_text_result(result, input_components, after_removal)
                if result.timed_out:
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
