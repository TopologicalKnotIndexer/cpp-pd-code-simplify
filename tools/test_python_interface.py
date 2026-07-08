#!/usr/bin/env python3
"""Smoke tests for the source-embedded Python C++ interface package."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTERFACE_ROOT = ROOT / "python_project" / "cpp-pd-code-simplify-interface"
sys.path.insert(0, str(INTERFACE_ROOT))

import cpp_pd_code_simplify_interface as interface  # noqa: E402
import cpp_pd_code_simplify_interface.main as interface_main  # noqa: E402


TREFOIL = "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
ZERO_BASED_TREFOIL = "PD[X[0,4,1,3],X[2,0,3,5],X[4,2,5,1]]"


def preferred_cxx() -> str | None:
    for key in ("CPP_PD_CODE_SIMPLIFY_INTERFACE_TEST_CXX", "CXX"):
        value = os.environ.get(key)
        if value:
            return value
    candidates = [
        ROOT
        / ".local_toolchains"
        / "winlibs-x86_64-posix-seh-gcc-16.1.0-mingw-w64ucrt-14.0.0-r3"
        / "mingw64"
        / "bin"
        / "g++.exe",
        ROOT
        .parent
        .parent
        / "javakh_ori-latest"
        / "toolchains"
        / "winlibs-x86_64-posix-seh-gcc-16.1.0-mingw-w64ucrt-14.0.0-r3"
        / "mingw64"
        / "bin"
        / "g++.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def main() -> int:
    if "CPP_PD_CODE_SIMPLIFY_INTERFACE_CACHE_DIR" not in os.environ:
        os.environ["CPP_PD_CODE_SIMPLIFY_INTERFACE_CACHE_DIR"] = str(ROOT / ".cache" / "python-interface")

    library = interface.compile_simplifier(force=True, cxx=preferred_cxx())
    assert library.exists(), library
    if os.name == "nt":
        imported = (
            interface_main._objdump_dependency_names(library)
            or interface_main._dumpbin_dependency_names(library)
        )
        runtime_imports = [
            name for name in imported
            if interface_main._runtime_basename_is_cacheable(name)
        ]
        for name in runtime_imports:
            assert interface_main._find_dependency_file(name, [library.parent]) is not None, name

    trefoil = interface.simplify(TREFOIL)
    assert trefoil["simplification_found"] is False
    assert trefoil["input_components"]["total_components"] == 1
    assert trefoil["final_crossings"] == 3
    assert trefoil["final_components"]["total_components"] == 1
    assert trefoil["last_path_search_mode"] == "bruteforce"

    zero_based_trefoil = interface.simplify(ZERO_BASED_TREFOIL, reduction_round=0)
    assert zero_based_trefoil["final_pd_code"] == TREFOIL

    unknot = interface.simplify("PD[]")
    assert unknot["input_components"]["crossingless_components"] == 1
    assert unknot["final_pd_code"] == "PD[]"
    assert unknot["last_path_search_mode"] == "bruteforce"

    kink = interface.simplify("PD[X[0,0,1,1]]")
    assert kink["reidemeister_i_moves"] == 1
    assert kink["final_components"]["crossingless_components"] == 1
    assert kink["final_pd_code"] == "PD[]"

    brute = interface.simplify(TREFOIL, ban_heuristic=True, max_thread=1)
    brute_parallel = interface.simplify(TREFOIL, ban_heuristic=True, max_thread=4)
    assert brute["last_path_search_mode"] == "bruteforce"
    assert brute_parallel["final_pd_code"] == brute["final_pd_code"]
    assert brute_parallel["tested_green_paths"] == brute["tested_green_paths"]

    limited = interface.simplify(TREFOIL, reduction_round=0)
    assert limited["stopped_by_round_limit"] is True
    assert limited["final_crossings"] == 3

    batch = interface.simplify_many([TREFOIL, "PD[]"])
    assert len(batch) == 2
    assert batch[0]["input_components"]["total_components"] == 1
    assert batch[1]["input_components"]["crossingless_components"] == 1

    print("Python C++ interface tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
