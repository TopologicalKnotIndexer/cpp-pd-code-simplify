# Python Prototype

`mid_simplify_v5.py` is a refactored pure Python version of the
mid-simplification search. It exposes both a Python API and a command-line
interface.

## Environment

Create a local virtual environment for the comparison and benchmark tools:

```sh
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

On Linux and macOS, use `.venv/bin/python` instead of
`.\.venv\Scripts\python`.

## CLI

Run one PD code:

```sh
python mid_simplify_v5.py --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

Report crossingless components after removing all trefoil crossings:

```sh
python mid_simplify_v5.py --remove-crossings 0,1,2 --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

## Python API

```python
import mid_simplify_v5 as simplify

code = simplify.parse_pd_code("PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]")
result = simplify.find_simplification(code, max_paths=100)
print(result.found)
```

Component accounting is available directly:

```python
analysis = simplify.analyze_components(code)
after = simplify.analyze_components_after_removing_crossings(code, [0, 1, 2])
print(after.crossingless_components)
```

`PD[]` is represented by an empty PD code plus an explicit crossingless
component count at the CLI/job layer. This keeps the raw library representation
flexible while preserving the information that the command-line input denotes
one unknot component.

## Differential Testing

Compare the C++ executable and Python implementation:

```sh
.\.venv\Scripts\python tools\compare_cpp_python.py --include-reference
```

Run Python-only prototype checks, including crossingless component accounting:

```sh
.\.venv\Scripts\python tools\test_python_prototype.py
```

## Benchmarks

Compare runtime and peak RSS memory usage:

```sh
.\.venv\Scripts\python tools\benchmark_cpp_python.py
```

The latest local result summary is in
[Python and C++ Comparison](python-cpp-comparison.md).
