# cpp-pd-code-simplify-interface

`cpp-pd-code-simplify-interface` is a Python package for calling the
`cpp-pd-code-simplify` C++ implementation from Python.

The package is designed for PyPI distribution:

```sh
pip install cpp-pd-code-simplify-interface
```

It ships the header-only C++ core and native C wrapper inside the wheel and
source distribution. On first use, the package compiles a cached local dynamic
library through `cpp-simple-interface`; later calls reuse that library through
`ctypes`. A C++17 compiler compatible with `g++` must be available at runtime.

Runtime dependencies are handled per platform. On Windows, the interface uses
`objdump -p` or `dumpbin /DEPENDENTS` when available, then caches MinGW runtime
DLLs such as `libstdc++-6.dll`, `libgcc_s_*.dll`, and `libwinpthread-1.dll`
next to the generated DLL. On Linux it adds `$ORIGIN` rpath and can inspect
`ldd`; on macOS it adds `@loader_path` rpath and can inspect `otool -L`. Load
failures are wrapped with platform-specific dependency hints.

The core C++ header is not stored as a permanent generated copy in this
subproject. The custom Poetry build backend syncs it from the repository root
during `poetry build`, embeds it in the wheel and sdist beside the native C
wrapper, then removes the temporary copy from the working tree.

Calls use the C++ library's default preprocessing pipeline: R1-move removal,
true R2-bigon removal, and nugatory-crossing removal, then iterative
mid-simplification.

## Example

```python
import cpp_pd_code_simplify_interface as simplify

pd_code = "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
result = simplify.simplify(pd_code)
print(result["final_pd_code"])
```

Returned `final_pd_code` strings are normalized for display: each crossing is
written from the under-incoming edge, labels are renumbered along oriented
components from `1`, and crossing rows are sorted lexicographically. This is
applied only at the final JSON boundary; the C++ backend keeps its internal
numbering unchanged while simplifying.

The default `max_paths=-1` uses deterministic heuristic green-path sampling in
the C++ backend. Heuristic mode preserves the original prototype red-path order
and applies the first validated witness found in that round. Use
`ban_heuristic=True` to request exhaustive green-path enumeration for a
manageable input. Use `reduction_round=K` to cap applied mid-simplification
rounds; the default `-1` runs until stable. In default heuristic mode, the C++
backend keeps the prototype-compatible internal PD order while heuristic search
keeps succeeding. If heuristic search misses, the current diagram is
canonicalized at the non-heuristic handoff boundary before `r3_prepass`,
`non_monotone`, brute force, and the final RIII failover are tried as needed.
Use
`timeout=K` to cap a call at `K` seconds; the default `-1` has no timeout. Use
`verbose=True` to forward timestamped C++ progress logs to stderr. If a call
times out, the returned dictionary still contains the best PD code found so far
and sets `timed_out` to `True`. Use `quit_at_crossing=N`, or CLI flag
`--quit-at-crossing N`, to stop once the current PD code has at most `N`
crossings; the returned dictionary sets `stopped_by_crossing_limit` when the
threshold is reached. Brute-force green-path enumeration is streamed
by the C++ backend; pass `bruteforce_budget=N` to cap brute-force green-path
checks per PD code. The default is `200000`, and `-1` disables that cap. If the
budget is exhausted, the returned dictionary still contains the current best PD
code and sets `resource_limited` to `True`. Verbose log lines use local wall-clock time in
`YYYY-MM-DD HH:MM:SS` format. When `max_thread=-1` reaches a brute-force search
phase, verbose logs also include `actual_threads`, the worker count selected by
the C++ backend for that phase. The backend call runs in a helper process, so
`Ctrl+C` can terminate active C++ work and its worker threads cleanly. Use
`log_file=PATH`, or CLI flag `--log-file PATH`, to tee stdout and stderr into a
flushed backup log file.
Use `reapr=True`, or CLI flag `--reapr`, only for the experimental
invariant-guarded projection oracle. It can change the knot or link type;
there is no crossing-drop window, so a very small projection may be accepted
when the invariant profile matches. Accepted output includes `reapr_warning`,
determinant guard fields, and before/after invariant profile strings. Use
`reapr_retry_max=N`, or CLI flag `--reapr-retry-max N`, to control the
deterministic retry cap.
Use `show_step_pd=True`, or CLI flag `--show-step-pd`, to print
`step_pd_code[ROUND]: PD[...]` to stdout after each mid-simplification witness
is applied and canonicalized, before that round's automatic local cleanup. With
`reapr=True`, every REAPR candidate that passes the full invariant profile is
also printed with round `0` before the selected candidate's ordinary local
cleanup.

Batch use:

```python
results = simplify.simplify_many([pd_code, "PD[]"])
```

To select a compiler:

```sh
CXX=clang++ python your_script.py
```

Windows PowerShell:

```powershell
$env:CXX = "C:\path\to\g++.exe"
python your_script.py
```

On Windows, use a compiler whose target architecture matches Python. A 64-bit
Python process needs a 64-bit MinGW-w64/UCRT, Clang, or MSVC-compatible
compiler target. Legacy MinGW.org toolchains are not supported because they do
not provide the C++ threading runtime used by the simplifier.

Command-line use also supports multi-line PD-code files:

```sh
python -m cpp_pd_code_simplify_interface --pd-file inputs.pd --max-paths -1 --verbose
```

## Build And Publish

From this directory:

```sh
poetry build
poetry publish
```

Use `poetry publish --build` to build and upload in one command.

For local testing:

```sh
poetry run python -m cpp_pd_code_simplify_interface "PD[]"
```
