#!/usr/bin/env python3
"""Randomized Khovanov-homology invariance checks for simplification output."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import benchmark_dataset as dataset  # noqa: E402
import mid_simplify_v5 as pysimplify  # noqa: E402


PDCode = List[List[int]]


@dataclass(frozen=True)
class KhCase:
    name: str
    pd_text: str

    @property
    def crossings(self) -> int:
        return len(pysimplify.parse_pd_code(self.pd_text))


BASE_CODES: Mapping[str, dataset.PDCode] = {
    "trefoil": dataset.to_pd_code([(1, 5, 2, 4), (3, 1, 4, 6), (5, 3, 6, 2)]),
    "figure_eight": dataset.to_pd_code(
        [(8, 3, 1, 4), (2, 6, 3, 5), (6, 2, 7, 1), (4, 7, 5, 8)]
    ),
    "cinquefoil": dataset.torus_2_odd(5),
    "torus_7": dataset.torus_2_odd(7),
}

TARGETED_CASES: Sequence[KhCase] = (
    KhCase(
        "same_face_green_unknot",
        "PD[X[1,5,2,4],X[2,5,3,6],X[6,3,1,4]]",
    ),
)


def executable_suffix() -> str:
    return ".exe" if os.name == "nt" else ""


def default_cpp_exe() -> Path:
    candidates = [
        ROOT / "build" / "bin" / ("pd_simplify" + executable_suffix()),
        ROOT / "build-manual" / ("pd_simplify" + executable_suffix()),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("pd_simplify")
    if found:
        return Path(found)
    raise FileNotFoundError("Could not find pd_simplify; pass --cpp-exe")


def preferred_cxx() -> Optional[str]:
    for key in ("CPPKH_INTERFACE_TEST_CXX", "CPP_PD_CODE_SIMPLIFY_INTERFACE_TEST_CXX", "CXX"):
        value = os.environ.get(key)
        if value:
            return value
    candidate = (
        ROOT
        / ".local_toolchains"
        / "winlibs-x86_64-posix-seh-gcc-16.1.0-mingw-w64ucrt-14.0.0-r3"
        / "mingw64"
        / "bin"
        / "g++.exe"
    )
    if candidate.exists():
        return str(candidate)
    return None


def configure_compiler(cxx: Optional[str]) -> None:
    if not cxx:
        return
    compiler = Path(cxx)
    if compiler.parent != Path("."):
        os.environ["PATH"] = str(compiler.parent) + os.pathsep + os.environ.get("PATH", "")
    try:
        import cpp_simple_interface
    except ImportError:
        return
    cpp_simple_interface.set_gpp_filepath(cxx)


def import_cppkh_interface(cxx: Optional[str]):
    configure_compiler(cxx)
    try:
        import cppkh_interface
    except ImportError as exc:
        raise RuntimeError(
            "cppkh-interface is required for Khovanov invariance tests. "
            "Install development dependencies with `python -m pip install -r requirements-dev.txt`."
        ) from exc
    return cppkh_interface


def import_python_interface(cxx: Optional[str]):
    interface_root = ROOT / "python_project" / "cpp-pd-code-simplify-interface"
    sys.path.insert(0, str(interface_root))
    if cxx:
        os.environ["CXX"] = cxx
    os.environ.setdefault(
        "CPP_PD_CODE_SIMPLIFY_INTERFACE_CACHE_DIR",
        str(ROOT / ".cache" / "random-khovanov-interface"),
    )
    import cpp_pd_code_simplify_interface as interface

    return interface


def normalize_pd_text(code: Iterable[Sequence[int]]) -> str:
    return pysimplify.format_final_pd_code([list(crossing) for crossing in code])


def build_random_cases(sample_count: int, max_moves: int, seed: int) -> List[KhCase]:
    if sample_count < 0:
        raise ValueError("sample_count must be non-negative")
    if max_moves < 0:
        raise ValueError("max_moves must be non-negative")

    rng = random.Random(seed)
    base_items = sorted(BASE_CODES.items())
    cases = list(TARGETED_CASES)
    for index in range(sample_count):
        base_name, base_code = base_items[index % len(base_items)]
        moves = rng.randint(1, max_moves) if max_moves > 0 else 0
        inflation_seed = rng.randrange(1, 2**31)
        inflated = dataset.inflate_by_type_i(base_code, moves=moves, seed=inflation_seed)
        cases.append(
            KhCase(
                f"random_{index + 1:02d}_{base_name}_r1x{moves}_seed{inflation_seed}",
                normalize_pd_text(inflated),
            )
        )
    return cases


def parse_json_payload(stdout: str) -> Dict[str, object]:
    payload = json.loads(stdout)
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError(f"expected one C++ JSON result, got {len(payload)}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise TypeError(f"expected JSON object, got {type(payload)!r}")
    return payload


def run_cpp_simplifier(
    executable: Path,
    pd_text: str,
    max_thread: int,
    timeout: int,
) -> Dict[str, object]:
    command = [
        str(executable),
        "--pd-code",
        pd_text,
        "--json",
        "--max-paths",
        "-1",
        "--reduction-round",
        "-1",
        "--max-thread",
        str(max_thread),
        "--timeout",
        str(timeout),
    ]
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"C++ simplifier failed with exit code {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    payload = parse_json_payload(proc.stdout)
    if "error" in payload:
        raise RuntimeError(f"C++ simplifier returned an error payload: {payload}")
    if payload.get("timed_out"):
        raise RuntimeError(f"C++ simplifier timed out before a final invariance check: {payload}")
    return payload


def run_python_simplifier(pd_text: str, max_thread: int, timeout: int) -> Dict[str, object]:
    code = pysimplify.parse_pd_code(pd_text)
    result = pysimplify.reduce_pd_code(
        code,
        max_paths=-1,
        reduction_round=-1,
        max_thread=max_thread,
        timeout=timeout,
    )
    if result.timed_out:
        raise RuntimeError("Python simplifier timed out before a final invariance check")
    return result.to_json()


def run_interface_simplifier(interface, pd_text: str, max_thread: int, timeout: int) -> Dict[str, object]:
    result = interface.simplify(
        pd_text,
        max_paths=-1,
        reduction_round=-1,
        max_thread=max_thread,
        timeout=timeout,
    )
    if result.get("timed_out"):
        raise RuntimeError("Python C++ interface timed out before a final invariance check")
    return result


def kh_value(cppkh_interface, cache: Dict[str, str], pd_text: str) -> str:
    normalized = pysimplify.format_final_pd_code(pysimplify.parse_pd_code(pd_text))
    found = cache.get(normalized)
    if found is not None:
        return found
    value = cppkh_interface.solve_khovanov(
        normalized,
        de_r1=True,
        de_k8=True,
        show_real_pdcode=False,
    )
    cache[normalized] = value
    return value


def check_backend_result(
    cppkh_interface,
    kh_cache: Dict[str, str],
    case: KhCase,
    backend_name: str,
    payload: Mapping[str, object],
    input_kh: str,
) -> None:
    final_pd = payload.get("final_pd_code")
    if not isinstance(final_pd, str):
        raise RuntimeError(f"{backend_name} did not return final_pd_code for {case.name}: {payload}")
    final_kh = kh_value(cppkh_interface, kh_cache, final_pd)
    if final_kh != input_kh:
        raise AssertionError(
            f"Khovanov homology changed for {case.name} via {backend_name}\n"
            f"input crossings: {case.crossings}\n"
            f"input PD: {case.pd_text}\n"
            f"final PD: {final_pd}\n"
            f"input Kh: {input_kh}\n"
            f"final Kh: {final_kh}"
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-count", type=int, default=8)
    parser.add_argument("--max-r1-moves", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260709)
    parser.add_argument("--cpp-exe", type=Path, default=None)
    parser.add_argument("--max-thread", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--cxx", default=preferred_cxx())
    parser.add_argument(
        "--skip-python",
        action="store_true",
        help="only check C++ CLI output against the input Khovanov homology",
    )
    parser.add_argument(
        "--include-interface",
        action="store_true",
        help="also check the Python C++ interface output",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.max_thread < -1 or args.max_thread == 0:
        raise ValueError("--max-thread must be -1 or a positive integer")
    if args.timeout < -1 or args.timeout == 0:
        raise ValueError("--timeout must be -1 or a positive integer")

    executable = args.cpp_exe or default_cpp_exe()
    cppkh_interface = import_cppkh_interface(args.cxx)
    interface = import_python_interface(args.cxx) if args.include_interface else None
    kh_cache: Dict[str, str] = {}
    cases = build_random_cases(args.sample_count, args.max_r1_moves, args.seed)

    for case in cases:
        input_kh = kh_value(cppkh_interface, kh_cache, case.pd_text)
        cpp_payload = run_cpp_simplifier(executable, case.pd_text, args.max_thread, args.timeout)
        check_backend_result(cppkh_interface, kh_cache, case, "C++ CLI", cpp_payload, input_kh)

        backends = ["C++ CLI"]
        if not args.skip_python:
            python_payload = run_python_simplifier(case.pd_text, args.max_thread, args.timeout)
            check_backend_result(cppkh_interface, kh_cache, case, "Python prototype", python_payload, input_kh)
            backends.append("Python prototype")

        if interface is not None:
            interface_payload = run_interface_simplifier(interface, case.pd_text, args.max_thread, args.timeout)
            check_backend_result(
                cppkh_interface,
                kh_cache,
                case,
                "Python C++ interface",
                interface_payload,
                input_kh,
            )
            backends.append("Python C++ interface")

        final_crossings = cpp_payload.get("final_crossings", "")
        print(
            f"[OK] {case.name}: crossings {case.crossings} -> {final_crossings}; "
            f"checked {', '.join(backends)}"
        )

    print(f"Khovanov invariance checks passed ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
