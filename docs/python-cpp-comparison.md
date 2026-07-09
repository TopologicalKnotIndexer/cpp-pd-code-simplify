# Python and C++ Comparison

The repository includes two direct implementations of the same
mid-simplification search:

- C++ executable: `pd_simplify`
- Python prototype: `mid_simplify_v5.py`

It also includes a PyPI-ready Python C++ interface package documented in
[Python C++ Interface](python-interface.md).

## Differential Testing

The differential test runner compares their JSON outputs exactly. Both
implementations use the default preprocessing pipeline first: R1-move removal,
true R2-bigon removal, then nugatory-crossing removal.

```sh
.\.venv\Scripts\python tools\compare_cpp_python.py ^
  --include-reference ^
  --include-benchmark ^
  --suite original ^
  --include-interface ^
  --max-paths -1 ^
  --ban-heuristic ^
  --reduction-round -1 ^
  --max-thread 16
```

To compare the one hundred active zip-random large cases with heuristic green-path
sampling and strict terminal stability:

```sh
.\.venv\Scripts\python tools\compare_cpp_python.py ^
  --include-benchmark ^
  --suite random ^
  --include-interface ^
  --max-paths -1 ^
  --reduction-round -1 ^
  --max-thread 16
```

On Linux and macOS, use `.venv/bin/python` instead of
`.\.venv\Scripts\python`.

Return code `0` from either simplifier means every item was processed
successfully, including inputs that are already stable. Return code `2` means
at least one item reported an error. Batch mode keeps going after item-level
errors and reports them in JSON so a bad PD code does not prevent later inputs
from being checked.

## Benchmarking

Use the benchmark runner to measure wall-clock time and peak RSS while also
checking C++ CLI, Python C++ interface, and Python JSON outputs in the same
run:

```sh
.\.venv\Scripts\python tools\benchmark_cpp_python.py --repeat 1
```

The benchmark dataset, chart-generation command, committed PNG chart, and
current local results include the Python C++ interface and are documented in
[Benchmarking](benchmarking.md).
