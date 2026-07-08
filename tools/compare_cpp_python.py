#!/usr/bin/env python3
"""Differentially test the C++ and Python PD simplifiers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mid_simplify_v5 as pysimplify  # noqa: E402
from benchmark_dataset import BENCHMARK_CASES, ORIGINAL_BENCHMARK_CASES, RANDOM_BENCHMARK_CASES  # noqa: E402

INTERFACE_ROOT = ROOT / "python_project" / "cpp-pd-code-simplify-interface"
SUITES = {
    "all": BENCHMARK_CASES,
    "original": ORIGINAL_BENCHMARK_CASES,
    "random": RANDOM_BENCHMARK_CASES,
}


REFERENCE_PD = """PD[
X[15,7,16,6],X[7,15,8,14],X[18,61,19,0],X[20,12,21,11],
X[12,24,13,23],X[13,26,14,27],X[29,22,30,23],X[21,30,22,31],
X[28,33,29,34],X[5,36,6,37],X[8,36,9,35],X[34,27,35,28],
X[1,41,2,40],X[19,43,20,42],X[43,25,44,24],X[25,45,26,44],
X[16,45,17,46],X[37,46,38,47],X[48,39,49,40],X[0,50,1,49],
X[10,51,11,52],X[31,53,32,52],X[41,50,42,51],X[55,3,56,2],
X[54,9,55,10],X[53,33,54,32],X[3,57,4,56],X[57,5,58,4],
X[60,17,61,18],X[59,38,60,39],X[58,47,59,48]
]"""

BUILTIN_CASES: Dict[str, str] = {
    "unknot": "PD[]",
    "trefoil": "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]",
    "figure-eight": "PD[X[8,3,1,4],X[2,6,3,5],X[6,2,7,1],X[4,7,5,8]]",
    "cinquefoil": "PD[X[8,0,1,9],X[0,2,3,1],X[2,4,5,3],X[4,6,7,5],X[6,8,9,7]]",
}


def default_cpp_exe() -> str:
    candidates = [
        ROOT / "build" / "bin" / "pd_simplify.exe",
        ROOT / "build" / "bin" / "pd_simplify",
        ROOT / "build-manual" / "pd_simplify.exe",
        ROOT / "build-manual" / "pd_simplify",
        ROOT / "build" / "pd_simplify.exe",
        ROOT / "build" / "pd_simplify",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("pd_simplify")
    if found:
        return found
    raise FileNotFoundError("Could not find pd_simplify; pass --cpp-exe")


def load_cases(args: argparse.Namespace) -> Dict[str, str]:
    cases = dict(BUILTIN_CASES)
    if args.include_reference:
        cases["reference-31"] = REFERENCE_PD
    if args.include_benchmark:
        for case in SUITES[args.suite]:
            cases[f"benchmark_{case.name}"] = case.pd_text
    for literal in args.pd_code or []:
        for job in pysimplify.parse_pd_document(literal, "command-line"):
            cases[job.label] = pysimplify.format_pd_code(job.code)
    for path in args.pd_file or []:
        for job in pysimplify.read_pd_file(path):
            cases[job.label] = pysimplify.format_pd_code(job.code)
    return cases


def write_case_file(cases: Dict[str, str]) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".pd", encoding="utf-8", delete=False)
    with handle:
        for name, pd_text in cases.items():
            handle.write(f"{name}: {pd_text}\n")
    return handle.name


def as_list(payload: object) -> List[Dict[str, object]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    raise TypeError(f"unexpected JSON payload type: {type(payload)!r}")


def maybe_add_ban(command: List[str], ban_heuristic: bool) -> List[str]:
    return command + (["--ban-heuristic"] if ban_heuristic else [])


def run_cpp_batch(
    cpp_exe: str,
    pd_file: str,
    max_paths: int,
    ban_heuristic: bool,
    reduction_round: int,
    verbose: bool,
) -> List[Dict[str, object]]:
    command = [
        cpp_exe,
        "--json",
        "--pd-file",
        pd_file,
        "--max-paths",
        str(max_paths),
        "--reduction-round",
        str(reduction_round),
    ]
    if verbose:
        command.append("--verbose")
    proc = subprocess.run(
        maybe_add_ban(command, ban_heuristic),
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=None if verbose else subprocess.PIPE,
    )
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip() if proc.stderr else ""
        raise RuntimeError(f"C++ run failed ({proc.returncode}): {stderr}")
    return as_list(json.loads(proc.stdout))


def run_python_cli_batch(
    pd_file: str,
    max_paths: int,
    ban_heuristic: bool,
    reduction_round: int,
    verbose: bool,
) -> List[Dict[str, object]]:
    command = [
        sys.executable,
        str(ROOT / "mid_simplify_v5.py"),
        "--json",
        "--pd-file",
        pd_file,
        "--max-paths",
        str(max_paths),
        "--reduction-round",
        str(reduction_round),
    ]
    if verbose:
        command.append("--verbose")
    proc = subprocess.run(
        maybe_add_ban(command, ban_heuristic),
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=None if verbose else subprocess.PIPE,
    )
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip() if proc.stderr else ""
        raise RuntimeError(f"Python run failed ({proc.returncode}): {stderr}")
    return as_list(json.loads(proc.stdout))


def interface_env(interface_cxx: str | None = None) -> Dict[str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(INTERFACE_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "CPP_PD_CODE_SIMPLIFY_INTERFACE_CACHE_DIR": str(ROOT / ".cache" / "compare-interface"),
    }
    if interface_cxx:
        env["CXX"] = interface_cxx
    return env


def run_interface_batch(
    pd_file: str,
    max_paths: int,
    ban_heuristic: bool,
    reduction_round: int,
    interface_cxx: str | None,
    verbose: bool,
) -> List[Dict[str, object]]:
    command = [
        sys.executable,
        "-m",
        "cpp_pd_code_simplify_interface",
        "--pd-file",
        pd_file,
        "--max-paths",
        str(max_paths),
        "--reduction-round",
        str(reduction_round),
    ]
    if verbose:
        command.append("--verbose")
    proc = subprocess.run(
        maybe_add_ban(command, ban_heuristic),
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=None if verbose else subprocess.PIPE,
        env=interface_env(interface_cxx),
    )
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip() if proc.stderr else ""
        raise RuntimeError(
            f"Interface run failed ({proc.returncode}): {stderr}\n{proc.stdout}"
        )
    return as_list(json.loads(proc.stdout))


def canonical(data: Dict[str, object]) -> Dict[str, object]:
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpp-exe", default=None, help="path to pd_simplify executable")
    parser.add_argument("--max-paths", type=int, default=-1)
    parser.add_argument("--ban-heuristic", action="store_true")
    parser.add_argument("--reduction-round", type=int, default=-1)
    parser.add_argument("--verbose", action="store_true", help="forward progress logs from child processes")
    parser.add_argument("--include-reference", action="store_true", help="include the 31-crossing reference case")
    parser.add_argument("--include-benchmark", action="store_true", help="include the deterministic benchmark dataset")
    parser.add_argument("--suite", choices=sorted(SUITES), default="all")
    parser.add_argument("--include-interface", action="store_true", help="also compare the Python C++ interface CLI")
    parser.add_argument("--interface-cxx", help="compiler used by the Python C++ interface")
    parser.add_argument("--pd-code", action="append", help="additional literal PD[...] case")
    parser.add_argument("--pd-file", action="append", help="additional PD input file")
    args = parser.parse_args(argv)

    cpp_exe = args.cpp_exe or default_cpp_exe()
    cases = load_cases(args)
    mismatches: List[str] = []
    pd_file = write_case_file(cases)
    try:
        cpp_results = [
            canonical(item)
            for item in run_cpp_batch(
                cpp_exe,
                pd_file,
                args.max_paths,
                args.ban_heuristic,
                args.reduction_round,
                args.verbose,
            )
        ]
        py_results = [
            canonical(item)
            for item in run_python_cli_batch(
                pd_file,
                args.max_paths,
                args.ban_heuristic,
                args.reduction_round,
                args.verbose,
            )
        ]
        interface_results = None
        if args.include_interface:
            interface_results = [
                canonical(item)
                for item in run_interface_batch(
                    pd_file,
                    args.max_paths,
                    args.ban_heuristic,
                    args.reduction_round,
                    args.interface_cxx,
                    args.verbose,
                )
            ]
    finally:
        Path(pd_file).unlink(missing_ok=True)

    if len(cpp_results) != len(py_results):
        raise RuntimeError(f"result count mismatch: C++={len(cpp_results)} Python={len(py_results)}")
    if interface_results is not None and len(cpp_results) != len(interface_results):
        raise RuntimeError(
            f"result count mismatch: C++={len(cpp_results)} Interface={len(interface_results)}"
        )

    for index, (name, cpp_result, py_result) in enumerate(zip(cases, cpp_results, py_results)):
        interface_result = None
        if interface_results is not None:
            interface_result = interface_results[index]
        if cpp_result != py_result or (interface_result is not None and cpp_result != interface_result):
            mismatches.append(name)
            print(f"[FAIL] {name}")
            print("C++:")
            print(json.dumps(cpp_result, indent=2))
            print("Python:")
            print(json.dumps(py_result, indent=2))
            if interface_result is not None:
                print("Interface:")
                print(json.dumps(interface_result, indent=2))
        else:
            print(f"[OK] {name}: found={cpp_result['simplification_found']}")

    if mismatches:
        print(f"{len(mismatches)} mismatches: {', '.join(mismatches)}", file=sys.stderr)
        return 1
    print(f"All {len(cases)} cases matched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
