#!/usr/bin/env python3
"""Compare C++ heuristic green-path sampling against brute-force enumeration."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from benchmark_dataset import RANDOM_BENCHMARK_CASES, BenchmarkCase, case_names, cases_by_name


ROOT = Path(__file__).resolve().parents[1]


RawRow = Dict[str, object]
SummaryRow = Dict[str, object]


def default_cpp_exe() -> str:
    candidates = [
        ROOT / "build" / "bin" / "pd_simplify.exe",
        ROOT / "build" / "bin" / "pd_simplify",
        ROOT / "build" / "pd_simplify.exe",
        ROOT / "build" / "pd_simplify",
        ROOT / "build-manual" / "pd_simplify.exe",
        ROOT / "build-manual" / "pd_simplify",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("pd_simplify")
    if found:
        return found
    raise FileNotFoundError("Could not find pd_simplify; pass --cpp-exe")


def selected_cases(names: Optional[Sequence[str]]) -> List[BenchmarkCase]:
    if not names:
        return list(RANDOM_BENCHMARK_CASES)
    lookup = cases_by_name()
    return [lookup[name] for name in names]


def run_cpp(
    cpp_exe: str,
    case: BenchmarkCase,
    *,
    ban_heuristic: bool,
    reduction_round: int,
    timeout: Optional[float],
    verbose: bool,
) -> Tuple[float, int, Mapping[str, object]]:
    command = [
        cpp_exe,
        "--json",
        "--pd-code",
        case.pd_text,
        "--max-paths",
        "-1",
        "--reduction-round",
        str(reduction_round),
    ]
    if verbose:
        command.append("--verbose")
    if ban_heuristic:
        command.append("--ban-heuristic")

    start = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=None if verbose else subprocess.PIPE,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - start
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip() if proc.stderr else ""
        raise RuntimeError(
            f"C++ run failed for {case.name} ({proc.returncode}): {stderr}"
        )
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict):
        raise TypeError(f"unexpected JSON payload for {case.name}: {type(payload)!r}")
    return elapsed, proc.returncode, payload


def output_crossings(payload: Mapping[str, object], fallback: int) -> int:
    value = payload.get("final_crossings")
    if isinstance(value, int):
        return value
    return fallback


def reduction_crossings(case: BenchmarkCase, payload: Mapping[str, object]) -> int:
    return max(0, case.crossings - output_crossings(payload, case.crossings))


def benchmark(
    cpp_exe: str,
    cases: Sequence[BenchmarkCase],
    repeat: int,
    reduction_round: int,
    timeout: Optional[float],
    verbose: bool,
) -> List[RawRow]:
    rows: List[RawRow] = []
    modes = [
        ("heuristic", False),
        ("bruteforce", True),
    ]
    for repeat_index in range(1, repeat + 1):
        for case in cases:
            for mode, ban_heuristic in modes:
                timed_out = False
                try:
                    elapsed, return_code, payload = run_cpp(
                        cpp_exe,
                        case,
                        ban_heuristic=ban_heuristic,
                        reduction_round=reduction_round,
                        timeout=timeout,
                        verbose=verbose,
                    )
                except subprocess.TimeoutExpired:
                    if timeout is None:
                        raise
                    elapsed = timeout
                    return_code = -1
                    payload = {}
                    timed_out = True
                reduced = reduction_crossings(case, payload)
                row: RawRow = {
                    "case": case.name,
                    "family": case.family,
                    "mode": mode,
                    "repeat": repeat_index,
                    "input_crossings": case.crossings,
                    "output_crossings": output_crossings(payload, case.crossings),
                    "reduction_crossings": reduced,
                    "reduction_percent": 100.0 * reduced / case.crossings if case.crossings else 0.0,
                    "time_seconds": elapsed,
                    "return_code": return_code,
                    "timed_out": timed_out,
                    "simplification_found": bool(payload.get("simplification_found")),
                    "tested_red_paths": int(payload.get("tested_red_paths", 0)),
                    "tested_green_paths": int(payload.get("tested_green_paths", 0)),
                    "mid_simplification_rounds": int(payload.get("mid_simplification_rounds", 0)),
                    "heuristic_failover_rounds": int(payload.get("heuristic_failover_rounds", 0)),
                    "last_path_search_mode": str(payload.get("last_path_search_mode", "")),
                }
                rows.append(row)
                print(
                    f"{case.name:24s} {mode:10s} repeat={repeat_index:2d} "
                    f"time={elapsed:8.3f}s "
                    f"reduction={reduced:4d}/{case.crossings:<4d} "
                    f"({row['reduction_percent']:6.2f}%) "
                    f"green={row['tested_green_paths']} "
                    f"timeout={'yes' if timed_out else 'no'}"
                )
    return rows


def summarize(rows: Iterable[RawRow]) -> List[SummaryRow]:
    grouped: Dict[str, List[RawRow]] = defaultdict(list)
    for row in rows:
        grouped[str(row["mode"])].append(row)

    summary: List[SummaryRow] = []
    mode_order = {"heuristic": 0, "bruteforce": 1}
    for mode, values in sorted(grouped.items(), key=lambda item: mode_order.get(item[0], 99)):
        total_input = sum(int(row["input_crossings"]) for row in values)
        total_reduced = sum(int(row["reduction_crossings"]) for row in values)
        summary.append(
            {
                "mode": mode,
                "runs": len(values),
                "case_count": len({str(row["case"]) for row in values}),
                "avg_time_seconds": mean(float(row["time_seconds"]) for row in values),
                "total_time_seconds": sum(float(row["time_seconds"]) for row in values),
                "avg_reduction_percent": mean(float(row["reduction_percent"]) for row in values),
                "total_reduction_percent": 100.0 * total_reduced / total_input if total_input else 0.0,
                "avg_reduction_crossings": mean(float(row["reduction_crossings"]) for row in values),
                "avg_tested_green_paths": mean(float(row["tested_green_paths"]) for row in values),
                "found_count": sum(1 for row in values if row["simplification_found"]),
                "timeout_count": sum(1 for row in values if row.get("timed_out")),
            }
        )
    return summary


def write_csv(path: Path, rows: Sequence[Mapping[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def plot_summary(
    path: Path,
    summary: Sequence[SummaryRow],
    *,
    repeat: int,
    case_count: int,
    reduction_round: int,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    by_mode = {str(row["mode"]): row for row in summary}
    modes = ["heuristic", "bruteforce"]
    labels = ["Heuristic", "Brute force"]
    colors = ["#2563eb", "#f97316"]
    metrics = [
        ("total_reduction_percent", "Reduction / Original Crossings", "%"),
        ("avg_time_seconds", "Average Time Per PD Code", "seconds"),
    ]

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(1, 2, figsize=(9.5, 4.6), dpi=180)
    fig.patch.set_facecolor("white")

    for axis, (metric, title, unit) in zip(axes, metrics):
        values = [float(by_mode[mode][metric]) for mode in modes]
        bars = axis.bar(labels, values, color=colors, width=0.62)
        axis.set_title(title, fontsize=12, pad=12)
        axis.set_ylabel(unit)
        axis.tick_params(axis="x", labelsize=11)
        axis.tick_params(axis="y", labelsize=9)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        limit = max(values) * 1.22 if values else 1.0
        axis.set_ylim(0, limit if limit > 0 else 1.0)
        for bar, value in zip(bars, values):
            label = f"{value:.2f}" if value >= 1 else f"{value:.3f}"
            axis.annotate(
                label,
                xy=(bar.get_x() + bar.get_width() / 2, value),
                xytext=(0, 5),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=9,
                color="#111827",
            )

    heuristic_time = float(by_mode["heuristic"]["avg_time_seconds"])
    brute_time = float(by_mode["bruteforce"]["avg_time_seconds"])
    time_ratio = brute_time / heuristic_time if heuristic_time else 0.0
    fig.suptitle("C++ Green-Path Search: Heuristic vs Brute Force", fontsize=14, y=0.98)
    fig.text(
        0.5,
        0.02,
        (
            f"C++ only, max_paths=-1, {case_count} zip-random large cases, "
            f"reduction_round={reduction_round}, {repeat} repeat(s). "
            f"Brute force took {time_ratio:.1f}x heuristic time."
        ),
        ha="center",
        va="bottom",
        fontsize=8.5,
        color="#374151",
    )
    fig.tight_layout(rect=[0, 0.06, 1, 0.94])
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def print_summary(summary: Sequence[SummaryRow]) -> None:
    print("\nC++ heuristic comparison")
    print(
        "mode,runs,case_count,avg_time_seconds,total_reduction_percent,"
        "avg_reduction_percent,avg_tested_green_paths,found_count"
        ",timeout_count"
    )
    for row in summary:
        print(
            f"{row['mode']},{row['runs']},{row['case_count']},"
            f"{float(row['avg_time_seconds']):.6f},"
            f"{float(row['total_reduction_percent']):.6f},"
            f"{float(row['avg_reduction_percent']):.6f},"
            f"{float(row['avg_tested_green_paths']):.3f},"
            f"{row['found_count']},"
            f"{row['timeout_count']}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpp-exe", default=None, help="path to pd_simplify executable")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--reduction-round", type=int, default=-1)
    parser.add_argument("--case", action="append", choices=case_names(), help="case to run; default uses random suite")
    parser.add_argument("--timeout", type=float, default=0.0, help="per-run timeout in seconds; 0 disables it")
    parser.add_argument("--verbose", action="store_true", help="forward progress logs from child processes")
    parser.add_argument("--raw-csv", type=Path, help="write raw measurement rows")
    parser.add_argument("--summary-csv", type=Path, help="write summary rows")
    parser.add_argument("--json", type=Path, help="write raw and summary results")
    parser.add_argument("--plot", type=Path, help="write a matplotlib-style comparison chart")
    args = parser.parse_args(argv)

    if args.repeat <= 0:
        raise ValueError("--repeat must be positive")

    cases = selected_cases(args.case)
    if not cases:
        raise ValueError("no benchmark cases selected")

    cpp_exe = args.cpp_exe or default_cpp_exe()
    timeout = args.timeout if args.timeout > 0 else None
    rows = benchmark(cpp_exe, cases, args.repeat, args.reduction_round, timeout, args.verbose)
    summary = summarize(rows)
    print_summary(summary)

    if args.raw_csv:
        write_csv(
            args.raw_csv,
            rows,
            [
                "case",
                "family",
                "mode",
                "repeat",
                "input_crossings",
                "output_crossings",
                "reduction_crossings",
                "reduction_percent",
                "time_seconds",
                "return_code",
                "timed_out",
                "simplification_found",
                "tested_red_paths",
                "tested_green_paths",
                "mid_simplification_rounds",
                "heuristic_failover_rounds",
                "last_path_search_mode",
            ],
        )
    if args.summary_csv:
        write_csv(
            args.summary_csv,
            summary,
            [
                "mode",
                "runs",
                "case_count",
                "avg_time_seconds",
                "total_time_seconds",
                "avg_reduction_percent",
                "total_reduction_percent",
                "avg_reduction_crossings",
                "avg_tested_green_paths",
                "found_count",
                "timeout_count",
            ],
        )
    if args.json:
        write_json(
            args.json,
            {
                "max_paths": -1,
                "reduction_round": args.reduction_round,
                "repeat": args.repeat,
                "suite": "random",
                "verbose": args.verbose,
                "raw": rows,
                "summary": summary,
            },
        )
    if args.plot:
        plot_summary(
            args.plot,
            summary,
            repeat=args.repeat,
            case_count=len(cases),
            reduction_round=args.reduction_round,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
