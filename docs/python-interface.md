# Python C++ Interface

The `python_project/cpp-pd-code-simplify-interface` subproject is a PyPI-ready
Python package named `cpp-pd-code-simplify-interface`.

It follows the same source-embedding pattern as `cppkh-interface`: built
distributions include the C++ source files, and the package compiles a cached
local dynamic library on first use through `cpp-simple-interface`. Python calls
that library through `ctypes`.

The repository does not keep generated copies of the core C++ implementation
inside the Python package tree. The custom Poetry build backend temporarily
copies the current `src/pdcode_simplify.cpp` and public header into the package
data directory, injects those files into the wheel and sdist, and then removes
the temporary copies from the working tree.

The interface uses the C++ library's default preprocessing pipeline: R1-move
removal followed by nugatory-crossing removal before the mid-simplification
search.

## Install

After publication:

```sh
pip install cpp-pd-code-simplify-interface
```

A C++14 compiler compatible with `g++` must be available at runtime. Set `CXX`
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
For example, 64-bit Python needs a 64-bit MinGW or Clang toolchain. After
compilation, the interface inspects the generated DLL with `objdump -p` or
`dumpbin /DEPENDENTS` when available and copies MinGW runtime DLLs into the
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

Returned `final_pd_code` strings are normalized so the smallest edge label is
`1`. This normalization is applied at the final JSON boundary; the C++ backend
keeps its internal numbering unchanged while simplifying.

`max_paths=-1` is the default and enables deterministic heuristic green-path
sampling in the C++ backend. Pass `ban_heuristic=True` to request exhaustive
green-path enumeration for the same input. `reduction_round=-1` is the
default and applies mid-simplification witnesses until stable. Pass
`verbose=True` to forward timestamped C++ progress logs to stderr. Verbose log
lines use local wall-clock time in `YYYY-MM-DD HH:MM:SS` format.

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
