# cpp-pd-code-simplify-interface

`cpp-pd-code-simplify-interface` is a Python package for calling the
`cpp-pd-code-simplify` C++ implementation from Python.

The package is designed for PyPI distribution:

```sh
pip install cpp-pd-code-simplify-interface
```

It ships the C++ source code inside the wheel and source distribution. On first
use, the package compiles a cached local dynamic library through
`cpp-simple-interface`; later calls reuse that library through `ctypes`. A C++14
compiler compatible with `g++` must be available at runtime.

Calls use the C++ library's default preprocessing pipeline: R1-move removal
followed by nugatory-crossing removal, then iterative mid-simplification.

## Example

```python
import cpp_pd_code_simplify_interface as simplify

pd_code = "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
result = simplify.simplify(pd_code)
print(result["final_pd_code"])
```

The default `max_paths=-1` uses deterministic heuristic green-path sampling in
the C++ backend. Use `ban_heuristic=True` to request exhaustive green-path
enumeration for a manageable input. Use `reduction_round=K` to cap applied
mid-simplification rounds; the default `-1` runs until stable. Use
`verbose=True` to forward C++ progress logs to stderr.

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
Python process needs a 64-bit compiler target.

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
