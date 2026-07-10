# Python C++ Interface

The `python_project/cpp-pd-code-simplify-interface` subproject is a PyPI-ready
Python package named `cpp-pd-code-simplify-interface`.

It follows the same source-embedding pattern as `cppkh-interface`: built
distributions include the header-only C++ core plus the native C wrapper, and
the package compiles a cached local dynamic library on first use through
`cpp-simple-interface`. Python calls that library through `ctypes`.

The repository does not keep generated copies of the core C++ header inside
the Python package tree. The custom Poetry build backend temporarily copies the
current public header into the package data directory, injects it into the
wheel and sdist beside `native_interface.cpp`, and then removes the temporary
copy from the working tree.

The interface uses the C++ library's default preprocessing pipeline: R1-move
removal, true R2-bigon removal, and nugatory-crossing removal before the
mid-simplification search.

## Install

After publication:

```sh
pip install cpp-pd-code-simplify-interface
```

A C++17 compiler compatible with `g++` must be available at runtime. Set `CXX`
to select a compiler:

```sh
CXX=clang++ python your_script.py
```

Windows PowerShell:

```powershell
$env:CXX = "C:\path\to\g++.exe"
python your_script.py
```

On Windows, the compiler target must match the Python process architecture.
For example, 64-bit Python needs a 64-bit MinGW-w64/UCRT, Clang, or
MSVC-compatible toolchain. Legacy MinGW.org toolchains are not supported
because they do not provide the C++ threading runtime used by the simplifier.
After compilation, the interface inspects the generated DLL with `objdump -p`
or `dumpbin /DEPENDENTS` when available and copies MinGW runtime DLLs into the
same cache directory as the generated DLL.

On Linux, the generated shared object is built with `$ORIGIN` rpath and the
interface can use `ldd` to diagnose missing dependencies. On macOS, the dynamic
library is built with `@loader_path` rpath and the interface can use `otool -L`
for dependency diagnostics. When loading still fails, the Python exception
includes platform-specific hints for `LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`, or
rebuilding the cache.

## Use

```python
import cpp_pd_code_simplify_interface as simplify

result = simplify.simplify(
    "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]",
    max_paths=-1,
    reduction_round=-1,
)
print(result["final_pd_code"])
```

Returned `final_pd_code` strings are normalized for display: each crossing is
written from the under-incoming edge, labels are renumbered along oriented
components from `1`, and crossing rows are sorted lexicographically. This
normalization is applied at the final JSON boundary; the C++ backend keeps its
internal numbering unchanged while simplifying.

`max_paths=-1` is the default and enables deterministic heuristic green-path
sampling in the C++ backend. Pass `ban_heuristic=True` to request exhaustive
green-path enumeration for the same input. `reduction_round=-1` is the
default and applies mid-simplification witnesses until stable. In the default
heuristic mode, a heuristic miss is followed by the native deterministic
non-monotone failover, then by a brute-force proof pass, and finally by the
RIII failover before a diagram is treated as stable. Pass
`timeout=K` to cap a call at `K` seconds; the default `-1` has no timeout. Pass
`verbose=True` to forward timestamped C++ progress logs to stderr. If a call
exceeds its timeout, the returned dictionary still contains the best PD code
found so far and sets `timed_out` to `True`. Brute-force green-path
enumeration is streamed by the C++ backend; pass `bruteforce_budget=N` to cap
brute-force green-path checks per PD code. The default is `200000`, and `-1`
disables that cap. If the budget is exhausted, the returned dictionary still
contains the current best PD code and sets `resource_limited` to `True`.
Verbose log lines use local
wall-clock time in `YYYY-MM-DD HH:MM:SS` format. When `max_thread=-1` reaches a
brute-force search phase, verbose logs also include `actual_threads`, the
worker count selected by the C++ backend for that phase. Calls run the C++
backend in a helper process, so `Ctrl+C` can terminate active C++ work and its
worker threads cleanly before the Python process exits. Pass `log_file=PATH`,
or use CLI flag `--log-file PATH`, to tee stdout and stderr output into a
flushed backup log file.

Pass `reapr=True`, or use CLI flag `--reapr`, to enable the experimental
invariant-guarded projection oracle in the native backend. It is disabled by
default and can still change the knot or link type. For `n` current crossings,
the raw candidate and its R1/R2/nugatory cleanup must both keep at least
`n - max(4, ceil(n / 20))` crossings before the invariant profile is allowed
to accept it. Accepted output includes `reapr_warning`, determinant guard
fields, and before/after invariant profile strings for independent checking.
Pass `reapr_retry_max=N`, or CLI flag `--reapr-retry-max N`, to control the
deterministic retry cap; the default is `3`.

Pass `show_step_pd=True`, or use CLI flag `--show-step-pd`, to print
`step_pd_code[ROUND]: PD[...]` to stdout after each mid-simplification witness
is applied and canonicalized, before that round's automatic local cleanup.
This is a diagnostic stream and is disabled by default because it can be large.

Batch use:

```python
results = simplify.simplify_many([
    "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]",
    "PD[]",
])
```

Command line:

```sh
python -m cpp_pd_code_simplify_interface "PD[]"
```

Batch command-line use:

```sh
python -m cpp_pd_code_simplify_interface --pd-file inputs.pd --max-paths -1 --verbose
```

The input file may contain one PD code per line. Optional `label: PD[...]`
prefixes are accepted.

## Build And Publish

From `python_project/cpp-pd-code-simplify-interface`:

```sh
poetry build
poetry publish
```

Use `poetry publish --build` to build and upload in one command after package
metadata and PyPI credentials are configured.

For local wheel testing:

```sh
poetry build
python -m pip install --force-reinstall dist/cpp_pd_code_simplify_interface-*.whl
python -m cpp_pd_code_simplify_interface "PD[]"
```
