#!/usr/bin/env python3
"""Benchmark runtime and peak RSS of the C++ CLI, Python prototype, and interface."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import psutil

from benchmark_dataset import (
    BENCHMARK_CASES,
    ORIGINAL_BENCHMARK_CASES,
    RANDOM_BENCHMARK_CASES,
    BenchmarkCase,
    case_names,
    cases_by_name,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
INTERFACE_ROOT = ROOT / "python_project" / "cpp-pd-code-simplify-interface"


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


def run_peak(
    command: List[str],
    sample_interval: float = 0.01,
    env: Optional[Mapping[str, str]] = None,
    verbose: bool = False,
) -> Tuple[float, float, int]:
    start = time.perf_counter()
    proc = psutil.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=None if verbose else subprocess.DEVNULL,
        text=True,
        env=env,
    )
    peak = rss_tree(proc)
    while proc.poll() is None:
        peak = max(peak, rss_tree(proc))
        time.sleep(sample_interval)
    peak = max(peak, rss_tree(proc))
    proc.wait()
    elapsed = time.perf_counter() - start
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(command)}"
        )
    return elapsed, peak / (1024 * 1024), proc.returncode


def interface_env(interface_cxx: Optional[str] = None) -> Dict[str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(INTERFACE_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "CPP_PD_CODE_SIMPLIFY_INTERFACE_CACHE_DIR": str(ROOT / ".cache" / "benchmark-interface"),
    }
    if interface_cxx:
        env["CXX"] = interface_cxx
    return env


def warm_interface_cache(sample_interval: float, interface_cxx: Optional[str]) -> None:
    command = [
        PYTHON,
        "-c",
        "import cpp_pd_code_simplify_interface as s; print(s.get_simplifier_library())",
    ]
    run_peak(command, sample_interval, env=interface_env(interface_cxx))


def write_batch_file(cases: Sequence[BenchmarkCase]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".pd", encoding="utf-8", delete=False)
    with handle:
        for case in cases:
            handle.write(f"{case.name}: {case.pd_text}\n")
    return Path(handle.name)


def commands_for_batch(
    cpp_exe: str,
    pd_file: Path,
    max_paths: int,
    reduction_round: int,
) -> Mapping[str, List[str]]:
    return {
        "cpp": [
            cpp_exe,
            "--json",
            "--pd-file",
            str(pd_file),
            "--max-paths",
            str(max_paths),
            "--reduction-round",
            str(reduction_round),
        ],
        "python": [
            PYTHON,
            str(ROOT / "mid_simplify_v5.py"),
            "--json",
            "--pd-file",
            str(pd_file),
            "--max-paths",
            str(max_paths),
            "--reduction-round",
            str(reduction_round),
        ],
        "interface": [
            PYTHON,
            "-m",
            "cpp_pd_code_simplify_interface",
            "--pd-file",
            str(pd_file),
            "--max-paths",
            str(max_paths),
            "--reduction-round",
            str(reduction_round),
        ],
    }


def add_ban_heuristic(command: List[str]) -> List[str]:
    return command + ["--ban-heuristic"]


def add_verbose(command: List[str]) -> List[str]:
    return command + ["--verbose"]


SUITES: Mapping[str, Sequence[BenchmarkCase]] = {
    "all": BENCHMARK_CASES,
    "original": ORIGINAL_BENCHMARK_CASES,
    "random": RANDOM_BENCHMARK_CASES,
}


def select_cases(names: Optional[Sequence[str]], suite: str) -> List[BenchmarkCase]:
    if not names:
        return list(SUITES[suite])
    lookup = cases_by_name()
    return [lookup[name] for name in names]


def run_benchmark(
    cpp_exe: str,
    cases: Sequence[BenchmarkCase],
    max_paths: int,
    reduction_round: int,
    repeat: int,
    sample_interval: float,
    interface_cxx: Optional[str] = None,
    ban_heuristic: bool = False,
    verbose: bool = False,
) -> List[RawRow]:
    rows: List[RawRow] = []
    total_crossings = sum(case.crossings for case in cases)
    warm_interface_cache(sample_interval, interface_cxx)
    pd_file = write_batch_file(cases)
    try:
        commands = dict(commands_for_batch(cpp_exe, pd_file, max_paths, reduction_round))
        if ban_heuristic:
            commands = {engine: add_ban_heuristic(command) for engine, command in commands.items()}
        if verbose:
            commands = {engine: add_verbose(command) for engine, command in commands.items()}
        for repeat_index in range(1, repeat + 1):
            for engine in ("cpp", "interface", "python"):
                env = interface_env(interface_cxx) if engine == "interface" else None
                elapsed, peak_mib, return_code = run_peak(
                    commands[engine],
                    sample_interval,
                    env=env,
                    verbose=verbose,
                )
                row: RawRow = {
                    "case": "batch",
                    "family": "mixed",
                    "crossings": total_crossings,
                    "case_count": len(cases),
                    "engine": engine,
                    "repeat": repeat_index,
                    "time_seconds": elapsed,
                    "avg_time_per_case_seconds": elapsed / len(cases),
                    "peak_rss_mib": peak_mib,
                    "return_code": return_code,
                }
                rows.append(row)
                print(
                    f"batch[{len(cases):2d}] {engine:9s} repeat={repeat_index:2d} "
                    f"time={elapsed:8.3f}s per_case={elapsed / len(cases):8.4f}s "
                    f"peak_rss={peak_mib:8.2f} MiB "
                    f"return={return_code}"
                )
    finally:
        pd_file.unlink(missing_ok=True)
    return rows


def summarize_rows(rows: Iterable[RawRow]) -> List[SummaryRow]:
    grouped: Dict[str, List[RawRow]] = defaultdict(list)
    for row in rows:
        grouped[str(row["engine"])].append(row)

    summary: List[SummaryRow] = []
    engine_order = {"cpp": 0, "interface": 1, "python": 2}
    for engine, values in sorted(grouped.items(), key=lambda item: engine_order.get(item[0], 99)):
        first = values[0]
        return_codes = sorted({int(row["return_code"]) for row in values})
        summary.append(
            {
                "case": first["case"],
                "family": first["family"],
                "crossings": first["crossings"],
                "case_count": first["case_count"],
                "engine": engine,
                "runs": len(values),
                "avg_time_seconds": mean(float(row["time_seconds"]) for row in values),
                "avg_time_per_case_seconds": mean(
                    float(row["avg_time_per_case_seconds"]) for row in values
                ),
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
            "avg_time_per_case_seconds": mean(float(row["avg_time_per_case_seconds"]) for row in rows),
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
    reduction_round: int,
    suite: str,
    ban_heuristic: bool,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    engines = ["cpp", "interface", "python"]
    labels = ["C++ CLI", "Python C++ interface", "Python"]
    colors = ["#2563eb", "#16a34a", "#f97316"]
    metrics = [
        ("avg_time_per_case_seconds", "Average time per PD code", "seconds"),
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

    time_speedup = aggregate["python"]["avg_time_per_case_seconds"] / aggregate["cpp"]["avg_time_per_case_seconds"]
    interface_overhead = (
        aggregate["interface"]["avg_time_per_case_seconds"] / aggregate["cpp"]["avg_time_per_case_seconds"]
    )
    rss_ratio = aggregate["python"]["avg_peak_rss_mib"] / aggregate["cpp"]["avg_peak_rss_mib"]
    suite_title = {
        "all": "All Cases",
        "original": "Original Benchmark",
        "random": "Zip-Random Large Cases",
    }.get(suite, suite)
    fig.suptitle(f"PD-Code Simplification Benchmark: {suite_title}", fontsize=14, y=0.98)
    fig.text(
        0.5,
        0.02,
        (
            f"Single-process batches over {case_count} deterministic cases, {repeat} repeat(s), "
            f"max_paths={max_paths}, reduction_round={reduction_round}, "
            f"heuristic={'off' if ban_heuristic else 'on'}. "
            f"C++ is {time_speedup:.1f}x faster; "
            f"interface is {interface_overhead:.1f}x C++ time; Python uses {rss_ratio:.1f}x peak RSS."
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
    print("\nBatch averages")
    print("case_count,engine,runs,avg_time_seconds,avg_time_per_case_seconds,avg_peak_rss_mib,return_codes")
    for row in summary:
        print(
            f"{row['case_count']},{row['engine']},{row['runs']},"
            f"{float(row['avg_time_seconds']):.6f},"
            f"{float(row['avg_time_per_case_seconds']):.6f},"
            f"{float(row['avg_peak_rss_mib']):.3f},{row['return_codes']}"
        )

    print("\nAggregate averages")
    print("engine,avg_time_seconds,avg_time_per_case_seconds,avg_peak_rss_mib")
    for engine in ("cpp", "interface", "python"):
        print(
            f"{engine},{aggregate[engine]['avg_time_seconds']:.6f},"
            f"{aggregate[engine]['avg_time_per_case_seconds']:.6f},"
            f"{aggregate[engine]['avg_peak_rss_mib']:.3f}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpp-exe", default=None, help="path to pd_simplify executable")
    parser.add_argument("--max-paths", type=int, default=-1)
    parser.add_argument("--ban-heuristic", action="store_true")
    parser.add_argument("--reduction-round", type=int, default=-1)
    parser.add_argument("--verbose", action="store_true", help="forward progress logs from child processes")
    parser.add_argument("--repeat", type=int, default=3, help="measurement repeats per case and engine")
    parser.add_argument(
        "--suite",
        choices=sorted(SUITES),
        default="all",
        help="benchmark suite to run when --case is not given",
    )
    parser.add_argument("--case", action="append", choices=case_names(), help="case to run; default runs all")
    parser.add_argument("--sample-interval", type=float, default=0.01)
    parser.add_argument("--interface-cxx", help="compiler used by the Python C++ interface")
    parser.add_argument("--raw-csv", type=Path, help="write raw measurement rows")
    parser.add_argument("--summary-csv", type=Path, help="write per-case average rows")
    parser.add_argument("--json", type=Path, help="write raw, summary, and aggregate results")
    parser.add_argument("--plot", type=Path, help="write a matplotlib-style aggregate bar chart")
    args = parser.parse_args(argv)

    if args.repeat <= 0:
        raise ValueError("--repeat must be positive")

    cpp_exe = args.cpp_exe or default_cpp_exe()
    cases = select_cases(args.case, args.suite)
    if not cases:
        raise ValueError(f"benchmark suite {args.suite!r} has no cases")
    rows = run_benchmark(
        cpp_exe,
        cases,
        args.max_paths,
        args.reduction_round,
        args.repeat,
        args.sample_interval,
        interface_cxx=args.interface_cxx,
        ban_heuristic=args.ban_heuristic,
        verbose=args.verbose,
    )
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
                "case_count",
                "engine",
                "repeat",
                "time_seconds",
                "avg_time_per_case_seconds",
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
                "case_count",
                "engine",
                "runs",
                "avg_time_seconds",
                "avg_time_per_case_seconds",
                "avg_peak_rss_mib",
                "return_codes",
            ],
        )
    if args.json:
        write_json(
            args.json,
            {
                "max_paths": args.max_paths,
                "reduction_round": args.reduction_round,
                "repeat": args.repeat,
                "suite": args.suite,
                "ban_heuristic": args.ban_heuristic,
                "verbose": args.verbose,
                "raw": rows,
                "summary": summary,
                "aggregate": aggregate,
            },
        )
    if args.plot:
        plot_aggregate(
            args.plot,
            aggregate,
            len(cases),
            args.repeat,
            args.max_paths,
            args.reduction_round,
            args.suite,
            args.ban_heuristic,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
