#!/usr/bin/env python3
"""Run C++ timing samples from tests/pd_code.zip and plot a scatter chart."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import re
import statistics
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = ROOT / "tests" / "pd_code.zip"
DEFAULT_CSV = ROOT / "docs" / "assets" / "cpp_zip_random_100_time_scatter.csv"
DEFAULT_JSON = ROOT / "docs" / "assets" / "cpp_zip_random_100_time_scatter.json"
DEFAULT_PNG = ROOT / "docs" / "assets" / "cpp_zip_random_100_time_scatter.png"
DEFAULT_REDUCTION_PNG = ROOT / "docs" / "assets" / "cpp_zip_random_100_crossing_reduction_scatter.png"
DEFAULT_MARKDOWN = ROOT / "docs" / "cpp-time-analysis.md"
DEFAULT_SEED = 20260709


CSV_FIELDS = [
    "sample_index",
    "zip_path",
    "crossings",
    "time_seconds",
    "return_code",
    "status",
    "timed_out",
    "resource_limited",
    "final_crossings",
    "final_pd_code",
    "mid_simplification_rounds",
    "heuristic_failover_rounds",
    "reidemeister_i_moves",
    "reidemeister_ii_moves",
    "reidemeister_iii_moves",
    "nugatory_crossing_moves",
    "tested_red_paths",
    "tested_green_paths",
    "last_path_search_mode",
    "error",
]


def executable_suffix() -> str:
    return ".exe" if os.name == "nt" else ""


def default_executable() -> Path:
    return ROOT / "build" / "bin" / ("pd_simplify" + executable_suffix())


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def parse_pd_crossings(text: str) -> int:
    numbers = re.findall(r"-?\d+", text)
    if len(numbers) % 4 != 0:
        raise ValueError("PD code does not contain a multiple of four integers")
    return len(numbers) // 4


def compact_pd_text(text: str) -> str:
    return " ".join(text.strip().split())


def read_zip_entries(zip_path: Path) -> Dict[str, str]:
    with zipfile.ZipFile(zip_path) as archive:
        entries = {
            name: archive.read(name).decode("utf-8", errors="replace").strip()
            for name in archive.namelist()
            if not name.endswith("/")
        }
    if not entries:
        raise ValueError(f"no PD-code files were found in {zip_path}")
    return entries


def select_samples(entries: Mapping[str, str], sample_size: int, seed: int) -> List[str]:
    names = sorted(entries)
    if sample_size > len(names):
        raise ValueError(f"requested {sample_size} samples from only {len(names)} entries")
    return random.Random(seed).sample(names, sample_size)


def read_completed_rows(csv_path: Path) -> Dict[str, MutableMapping[str, str]]:
    if not csv_path.exists():
        return {}
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        return {row["zip_path"]: row for row in csv.DictReader(handle)}


def write_csv(csv_path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_json_payload(stdout: str) -> Mapping[str, object]:
    payload = json.loads(stdout)
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError(f"expected one JSON result, got {len(payload)}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def run_one(
    executable: Path,
    pd_code: str,
    max_paths: int,
    reduction_round: int,
    max_thread: int,
    bruteforce_budget: int,
    timeout_seconds: float,
) -> Mapping[str, object]:
    command = [
        str(executable),
        "--pd-code",
        compact_pd_text(pd_code),
        "--json",
        "--max-paths",
        str(max_paths),
        "--reduction-round",
        str(reduction_round),
        "--max-thread",
        str(max_thread),
        "--bruteforce-budget",
        str(bruteforce_budget),
        "--timeout",
        str(int(timeout_seconds) if timeout_seconds > 0 else -1),
    ]
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds + 10.0 if timeout_seconds > 0 else None,
            check=False,
        )
        elapsed = time.perf_counter() - start
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - start
        return {
            "time_seconds": elapsed,
            "return_code": "timeout",
            "status": "timeout",
            "timed_out": True,
            "error": f"timed out after {timeout_seconds} seconds",
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }

    row: Dict[str, object] = {
        "time_seconds": elapsed,
        "return_code": proc.returncode,
        "status": "ok" if proc.returncode == 0 else "error",
        "timed_out": False,
        "resource_limited": False,
        "error": "",
    }
    try:
        payload = parse_json_payload(proc.stdout)
        if "error" in payload:
            error_text = str(payload.get("error", ""))
            row["status"] = "timeout" if "timeout" in error_text.lower() else "error"
            row["timed_out"] = row["status"] == "timeout"
            row["error"] = error_text
        elif payload.get("timed_out"):
            row["status"] = "timeout"
            row["timed_out"] = True
            row["error"] = f"timed out after {timeout_seconds} seconds"
        elif payload.get("resource_limited"):
            row["status"] = "resource_limited"
            row["resource_limited"] = True
            row["error"] = "brute-force resource budget exhausted"
        for key in [
            "timed_out",
            "resource_limited",
            "final_crossings",
            "final_pd_code",
            "mid_simplification_rounds",
            "heuristic_failover_rounds",
            "reidemeister_i_moves",
            "reidemeister_ii_moves",
            "reidemeister_iii_moves",
            "nugatory_crossing_moves",
            "tested_red_paths",
            "tested_green_paths",
            "last_path_search_mode",
        ]:
            row[key] = payload.get(key, "")
    except Exception as exc:  # noqa: BLE001 - keep benchmark running and record the failure.
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"
        if proc.stderr.strip():
            row["error"] = str(row["error"]) + "; stderr: " + proc.stderr.strip()[:1000]
    return row


def numeric_values(rows: Iterable[Mapping[str, object]], key: str) -> List[float]:
    values: List[float] = []
    for row in rows:
        value = row.get(key, "")
        if value in ("", None):
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def format_seconds(value: float) -> str:
    if value >= 3600:
        return f"{value / 3600:.2f} h"
    if value >= 60:
        return f"{value / 60:.2f} min"
    return f"{value:.3f} s"


def make_plot(rows: Sequence[Mapping[str, object]], png_path: Path) -> None:
    import matplotlib.pyplot as plt

    ok_rows = [row for row in rows if row.get("status") == "ok"]
    summary = summary_for_rows(rows)

    plt.style.use("classic")
    fig, ax = plt.subplots(figsize=(8.5, 5.4), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if ok_rows:
        ax.scatter(
            [float(row["crossings"]) for row in ok_rows],
            [float(row["time_seconds"]) for row in ok_rows],
            s=34,
            c="#1f77b4",
            alpha=0.82,
            edgecolors="white",
            linewidths=0.45,
            label="completed",
        )

    ax.set_title("C++ PD-Code Simplification Time on Completed Zip-Random Samples", pad=12)
    ax.set_xlabel("Input crossing count")
    ax.set_ylabel("Total C++ CLI time (seconds)")
    ax.grid(True, color="#d9d9d9", linewidth=0.8, alpha=0.8)
    failure_rate = float(summary.get("failure_rate_percent", 0.0))
    ax.text(
        0.02,
        0.98,
        f"completed: {summary.get('completed_count', 0)}/{summary.get('sample_count', 0)}\n"
        f"failure rate: {failure_rate:.1f}%",
        transform=ax.transAxes,
        va="top",
        ha="left",
        bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "edgecolor": "#c8c8c8", "alpha": 0.92},
    )
    fig.tight_layout()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path)
    plt.close(fig)


def make_crossing_reduction_plot(rows: Sequence[Mapping[str, object]], png_path: Path) -> None:
    import matplotlib.pyplot as plt

    ok_rows = [row for row in rows if row.get("status") == "ok"]
    summary = summary_for_rows(rows)

    plt.style.use("classic")
    fig, ax = plt.subplots(figsize=(8.5, 5.4), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if ok_rows:
        x_values = [float(row["crossings"]) for row in ok_rows]
        y_values = [float(row["final_crossings"]) for row in ok_rows]
        ax.scatter(
            x_values,
            y_values,
            s=38,
            c="#2ca02c",
            alpha=0.84,
            edgecolors="white",
            linewidths=0.45,
            label="completed",
        )
        x_padding = max(5.0, (max(x_values) - min(x_values)) * 0.05)
        ax.set_xlim(min(x_values) - x_padding, max(x_values) + x_padding)
        ax.set_ylim(-0.8, max(12.0, max(y_values) + 1.0))

    ax.set_title("C++ PD-Code Output Crossing Counts on Completed Zip-Random Samples", pad=12)
    ax.set_xlabel("Input crossing count")
    ax.set_ylabel("Final output crossing count")
    ax.grid(True, color="#d9d9d9", linewidth=0.8, alpha=0.8)
    failure_rate = float(summary.get("failure_rate_percent", 0.0))
    ax.text(
        0.02,
        0.98,
        f"completed: {summary.get('completed_count', 0)}/{summary.get('sample_count', 0)}\n"
        f"failure rate: {failure_rate:.1f}%",
        transform=ax.transAxes,
        va="top",
        ha="left",
        bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "edgecolor": "#c8c8c8", "alpha": 0.92},
    )
    fig.tight_layout()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path)
    plt.close(fig)


def summary_for_rows(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    completed = [row for row in rows if row.get("status") == "ok"]
    times = numeric_values(completed, "time_seconds")
    crossings = numeric_values(rows, "crossings")
    total_runtime = sum(times)
    summary: Dict[str, object] = {
        "sample_count": len(rows),
        "completed_count": len(completed),
        "error_count": len(rows) - len(completed),
        "failure_rate_percent": 100.0 * (len(rows) - len(completed)) / len(rows) if rows else 0.0,
        "total_completed_time_seconds": total_runtime,
    }
    if crossings:
        summary.update(
            {
                "min_crossings": int(min(crossings)),
                "median_crossings": statistics.median(crossings),
                "max_crossings": int(max(crossings)),
            }
        )
    if times:
        summary.update(
            {
                "mean_time_seconds": statistics.mean(times),
                "median_time_seconds": statistics.median(times),
                "max_time_seconds": max(times),
            }
        )
    return summary


def write_markdown(
    markdown_path: Path,
    csv_path: Path,
    json_path: Path,
    png_path: Path,
    reduction_png_path: Path,
    rows: Sequence[Mapping[str, object]],
    args: argparse.Namespace,
) -> None:
    summary = summary_for_rows(rows)
    relative_csv = csv_path.relative_to(markdown_path.parent).as_posix()
    relative_json = json_path.relative_to(markdown_path.parent).as_posix()
    relative_png = png_path.relative_to(markdown_path.parent).as_posix()
    relative_reduction_png = reduction_png_path.relative_to(markdown_path.parent).as_posix()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# C++ Zip-Random Time Analysis",
        "",
        "This page records a C++-only timing experiment on PD codes sampled from",
        "`tests/pd_code.zip`, the committed zip-random corpus fixture.",
        "",
        "## Method",
        "",
        f"- Sample size: `{args.sample_size}` PD-code files.",
        f"- Random seed: `{args.seed}`.",
        f"- C++ executable: `{display_path(args.executable)}`.",
        f"- Runtime options: `--max-paths {args.max_paths} --reduction-round {args.reduction_round} --max-thread {args.max_thread} --bruteforce-budget {args.bruteforce_budget}`.",
        f"- Per-case timeout: `{args.timeout_seconds:g}` seconds. Timed-out, resource-limited, or errored cases are counted as failures and excluded from the scatter plot.",
        "- Each point is one C++ CLI invocation, so the time includes process startup, parsing, preprocessing, simplification, and final JSON formatting.",
        f"- Generated at local time `{now}` on `{platform.platform()}` with Python `{platform.python_version()}`.",
        "",
        "## Results",
        "",
        "### Runtime",
        "",
        f"![C++ zip-random time scatter]({relative_png})",
        "",
        "### Crossing Reduction",
        "",
        "The second scatter plot uses the original input crossing count as the",
        "horizontal axis and the final crossing count reported by the completed",
        "algorithm run as the vertical axis.",
        "",
        f"![C++ zip-random final crossing scatter]({relative_reduction_png})",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Sampled cases | {summary.get('sample_count', 0)} |",
        f"| Completed cases | {summary.get('completed_count', 0)} |",
        f"| Failed cases | {summary.get('error_count', 0)} |",
        f"| Failure rate | {float(summary.get('failure_rate_percent', 0.0)):.1f}% |",
        f"| Crossing count range | {summary.get('min_crossings', '')} to {summary.get('max_crossings', '')} |",
        f"| Median crossing count | {summary.get('median_crossings', '')} |",
        f"| Total completed C++ time | {format_seconds(float(summary.get('total_completed_time_seconds', 0.0)))} |",
        f"| Mean completed time | {format_seconds(float(summary.get('mean_time_seconds', 0.0)))} |",
        f"| Median completed time | {format_seconds(float(summary.get('median_time_seconds', 0.0)))} |",
        f"| Max completed time | {format_seconds(float(summary.get('max_time_seconds', 0.0)))} |",
        "",
        "Raw artifacts:",
        "",
        f"- [CSV rows]({relative_csv})",
        f"- [JSON results]({relative_json})",
        "",
    ]
    markdown_path.write_text("\n".join(lines), encoding="utf-8")


def build_payload(rows: Sequence[Mapping[str, object]], args: argparse.Namespace) -> Mapping[str, object]:
    return {
        "metadata": {
            "zip_path": str(args.zip_path),
            "sample_size": args.sample_size,
            "seed": args.seed,
            "executable": display_path(args.executable),
            "max_paths": args.max_paths,
            "reduction_round": args.reduction_round,
            "max_thread": args.max_thread,
            "bruteforce_budget": args.bruteforce_budget,
            "timeout_seconds": args.timeout_seconds,
            "generated_at_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "summary": summary_for_rows(rows),
        "rows": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip-path", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--executable", type=Path, default=default_executable())
    parser.add_argument("--max-paths", type=int, default=-1)
    parser.add_argument("--reduction-round", type=int, default=-1)
    parser.add_argument("--max-thread", type=int, default=16)
    parser.add_argument("--bruteforce-budget", type=int, default=200000)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="per-case timeout in seconds; use 0 or a negative value to disable",
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--png", type=Path, default=DEFAULT_PNG)
    parser.add_argument("--reduction-png", type=Path, default=DEFAULT_REDUCTION_PNG)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--force", action="store_true", help="rerun rows already present in the CSV output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.zip_path.exists():
        raise FileNotFoundError(args.zip_path)
    if not args.executable.exists():
        raise FileNotFoundError(args.executable)

    entries = read_zip_entries(args.zip_path)
    selected = select_samples(entries, args.sample_size, args.seed)
    completed = {} if args.force else read_completed_rows(args.csv)
    rows: List[MutableMapping[str, object]] = []

    for sample_index, name in enumerate(selected, start=1):
        pd_code = entries[name]
        crossings = parse_pd_crossings(pd_code)
        existing = completed.get(name)
        if existing and existing.get("status") == "ok":
            row: MutableMapping[str, object] = dict(existing)
            row["sample_index"] = sample_index
            row["crossings"] = crossings
            rows.append(row)
            print(
                f"[skip {sample_index:03d}/{len(selected):03d}] {name} "
                f"crossings={crossings} time={float(row['time_seconds']):.3f}s",
                flush=True,
            )
            continue

        print(
            f"[run  {sample_index:03d}/{len(selected):03d}] {name} crossings={crossings}",
            flush=True,
        )
        result = dict(
            run_one(
                args.executable,
                pd_code,
                args.max_paths,
                args.reduction_round,
                args.max_thread,
                args.bruteforce_budget,
                args.timeout_seconds,
            )
        )
        result.update(
            {
                "sample_index": sample_index,
                "zip_path": name,
                "crossings": crossings,
            }
        )
        rows.append(result)
        print(
            f"[done {sample_index:03d}/{len(selected):03d}] {name} "
            f"status={result.get('status')} time={float(result['time_seconds']):.3f}s "
            f"final_crossings={result.get('final_crossings', '')}",
            flush=True,
        )
        write_csv(args.csv, rows)
        write_json(args.json, build_payload(rows, args))

    write_csv(args.csv, rows)
    make_plot(rows, args.png)
    make_crossing_reduction_plot(rows, args.reduction_png)
    write_json(args.json, build_payload(rows, args))
    write_markdown(args.markdown, args.csv, args.json, args.png, args.reduction_png, rows, args)
    print(f"Wrote {args.csv}", flush=True)
    print(f"Wrote {args.png}", flush=True)
    print(f"Wrote {args.reduction_png}", flush=True)
    print(f"Wrote {args.markdown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
