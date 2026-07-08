#!/usr/bin/env python3
"""Benchmark runtime and peak RSS of the C++ and Python simplifiers."""

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

import psutil

from benchmark_dataset import BENCHMARK_CASES, BenchmarkCase, case_names, cases_by_name

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


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


def rss_tree(process: psutil.Process) -> int:
    total = 0
    try:
        total += process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    return total


def run_peak(command: List[str], sample_interval: float = 0.01) -> Tuple[float, float, int]:
    start = time.perf_counter()
    proc = psutil.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak = rss_tree(proc)
    while proc.poll() is None:
        peak = max(peak, rss_tree(proc))
        time.sleep(sample_interval)
    peak = max(peak, rss_tree(proc))
    stdout, stderr = proc.communicate()
    elapsed = time.perf_counter() - start
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(command)}\n{stderr}\n{stdout}"
        )
    return elapsed, peak / (1024 * 1024), proc.returncode


def commands_for_case(cpp_exe: str, case: BenchmarkCase, max_paths: int) -> Mapping[str, List[str]]:
    return {
        "cpp": [cpp_exe, "--json", "--pd-code", case.pd_text, "--max-paths", str(max_paths)],
        "python": [
            PYTHON,
            str(ROOT / "mid_simplify_v5.py"),
            "--json",
            "--pd-code",
            case.pd_text,
            "--max-paths",
            str(max_paths),
        ],
    }


def select_cases(names: Optional[Sequence[str]]) -> List[BenchmarkCase]:
    if not names:
        return list(BENCHMARK_CASES)
    lookup = cases_by_name()
    return [lookup[name] for name in names]


def run_benchmark(
    cpp_exe: str,
    cases: Sequence[BenchmarkCase],
    max_paths: int,
    repeat: int,
    sample_interval: float,
) -> List[RawRow]:
    rows: List[RawRow] = []
    for case in cases:
        commands = commands_for_case(cpp_exe, case, max_paths)
        for repeat_index in range(1, repeat + 1):
            for engine in ("cpp", "python"):
                elapsed, peak_mib, return_code = run_peak(commands[engine], sample_interval)
                row: RawRow = {
                    "case": case.name,
                    "family": case.family,
                    "crossings": case.crossings,
                    "engine": engine,
                    "repeat": repeat_index,
                    "time_seconds": elapsed,
                    "peak_rss_mib": peak_mib,
                    "return_code": return_code,
                }
                rows.append(row)
                print(
                    f"{case.name:20s} {engine:6s} repeat={repeat_index:2d} "
                    f"time={elapsed:8.3f}s peak_rss={peak_mib:8.2f} MiB "
                    f"return={return_code}"
                )
    return rows


def summarize_rows(rows: Iterable[RawRow]) -> List[SummaryRow]:
    grouped: Dict[Tuple[str, str], List[RawRow]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["case"]), str(row["engine"]))].append(row)

    summary: List[SummaryRow] = []
    for (case_name, engine), values in sorted(grouped.items()):
        first = values[0]
        return_codes = sorted({int(row["return_code"]) for row in values})
        summary.append(
            {
                "case": case_name,
                "family": first["family"],
                "crossings": first["crossings"],
                "engine": engine,
                "runs": len(values),
                "avg_time_seconds": mean(float(row["time_seconds"]) for row in values),
                "avg_peak_rss_mib": mean(float(row["peak_rss_mib"]) for row in values),
                "return_codes": ";".join(str(code) for code in return_codes),
            }
        )
    return summary


def aggregate_by_engine(summary_rows: Sequence[SummaryRow]) -> Dict[str, Dict[str, float]]:
    grouped: Dict[str, List[SummaryRow]] = defaultdict(list)
    for row in summary_rows:
        grouped[str(row["engine"])].append(row)
    return {
        engine: {
            "avg_time_seconds": mean(float(row["avg_time_seconds"]) for row in rows),
            "avg_peak_rss_mib": mean(float(row["avg_peak_rss_mib"]) for row in rows),
        }
        for engine, rows in grouped.items()
    }


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


def plot_aggregate(
    path: Path,
    aggregate: Mapping[str, Mapping[str, float]],
    case_count: int,
    repeat: int,
    max_paths: int,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    engines = ["cpp", "python"]
    labels = ["C++", "Python"]
    colors = ["#2563eb", "#f97316"]
    metrics = [
        ("avg_time_seconds", "Average time", "seconds"),
        ("avg_peak_rss_mib", "Average peak RSS", "MiB"),
    ]

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(1, 2, figsize=(9.5, 4.6), dpi=180)
    fig.patch.set_facecolor("white")

    for axis, (metric, title, unit) in zip(axes, metrics):
        values = [aggregate[engine][metric] for engine in engines]
        bars = axis.bar(labels, values, color=colors, width=0.62)
        axis.set_title(title, fontsize=12, pad=12)
        axis.set_ylabel(unit)
        axis.tick_params(axis="x", labelsize=11)
        axis.tick_params(axis="y", labelsize=9)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        limit = max(values) * 1.22 if values else 1.0
        axis.set_ylim(0, limit)
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

    time_speedup = aggregate["python"]["avg_time_seconds"] / aggregate["cpp"]["avg_time_seconds"]
    rss_ratio = aggregate["python"]["avg_peak_rss_mib"] / aggregate["cpp"]["avg_peak_rss_mib"]
    fig.suptitle("C++ vs Python PD-Code Simplification Benchmark", fontsize=14, y=0.98)
    fig.text(
        0.5,
        0.02,
        (
            f"Arithmetic mean over {case_count} deterministic cases, {repeat} repeat(s), "
            f"max_paths={max_paths}. C++ is {time_speedup:.1f}x faster; "
            f"Python uses {rss_ratio:.1f}x peak RSS."
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


def print_summary(summary: Sequence[SummaryRow], aggregate: Mapping[str, Mapping[str, float]]) -> None:
    print("\nPer-case averages")
    print("case,engine,runs,avg_time_seconds,avg_peak_rss_mib,return_codes")
    for row in summary:
        print(
            f"{row['case']},{row['engine']},{row['runs']},"
            f"{float(row['avg_time_seconds']):.6f},"
            f"{float(row['avg_peak_rss_mib']):.3f},{row['return_codes']}"
        )

    print("\nAggregate averages")
    print("engine,avg_time_seconds,avg_peak_rss_mib")
    for engine in ("cpp", "python"):
        print(
            f"{engine},{aggregate[engine]['avg_time_seconds']:.6f},"
            f"{aggregate[engine]['avg_peak_rss_mib']:.3f}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpp-exe", default=None, help="path to pd_simplify executable")
    parser.add_argument("--max-paths", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=3, help="measurement repeats per case and engine")
    parser.add_argument("--case", action="append", choices=case_names(), help="case to run; default runs all")
    parser.add_argument("--sample-interval", type=float, default=0.01)
    parser.add_argument("--raw-csv", type=Path, help="write raw measurement rows")
    parser.add_argument("--summary-csv", type=Path, help="write per-case average rows")
    parser.add_argument("--json", type=Path, help="write raw, summary, and aggregate results")
    parser.add_argument("--plot", type=Path, help="write a matplotlib-style aggregate bar chart")
    args = parser.parse_args(argv)

    if args.repeat <= 0:
        raise ValueError("--repeat must be positive")

    cpp_exe = args.cpp_exe or default_cpp_exe()
    cases = select_cases(args.case)
    rows = run_benchmark(cpp_exe, cases, args.max_paths, args.repeat, args.sample_interval)
    summary = summarize_rows(rows)
    aggregate = aggregate_by_engine(summary)
    print_summary(summary, aggregate)

    if args.raw_csv:
        write_csv(
            args.raw_csv,
            rows,
            [
                "case",
                "family",
                "crossings",
                "engine",
                "repeat",
                "time_seconds",
                "peak_rss_mib",
                "return_code",
            ],
        )
    if args.summary_csv:
        write_csv(
            args.summary_csv,
            summary,
            [
                "case",
                "family",
                "crossings",
                "engine",
                "runs",
                "avg_time_seconds",
                "avg_peak_rss_mib",
                "return_codes",
            ],
        )
    if args.json:
        write_json(
            args.json,
            {
                "max_paths": args.max_paths,
                "repeat": args.repeat,
                "raw": rows,
                "summary": summary,
                "aggregate": aggregate,
            },
        )
    if args.plot:
        plot_aggregate(args.plot, aggregate, len(cases), args.repeat, args.max_paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
