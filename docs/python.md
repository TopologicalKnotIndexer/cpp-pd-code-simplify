# Python Prototype

`mid_simplify_v5.py` is a refactored pure Python version of the
mid-simplification search. It exposes both a Python API and a command-line
interface. Its CLI and `run_job` helper run pure Python R1-move removal
followed by pure Python nugatory-crossing removal before the search by
default.

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

Use `--json` for structured output containing `final_pd_code` and
`final_crossings`. Use `--reduction-round K` to cap applied
mid-simplification rounds; the default `-1` runs until stable, with a
brute-force check when heuristic mode can no longer find a path. Use
`--timeout K` to cap each PD-code job at `K` seconds; the default `-1` has no
timeout. A timed-out job returns the best PD code found so far and sets
`timed_out` in JSON/text output. Use `--verbose` to print timestamped progress
logs to stderr. Verbose log lines use local wall-clock time in
`YYYY-MM-DD HH:MM:SS` format. When `--max-thread -1` reaches a brute-force
search phase, verbose logs also include `actual_threads`, the worker count
selected for that phase. `Ctrl+C` cancels active multiprocessing workers and
exits with status `130`. Final output PD-code strings are normalized for
display: each crossing is written from the under-incoming edge, labels are
renumbered along oriented components from `1`, and crossing rows are sorted
lexicographically.

Report crossingless components after removing all trefoil crossings:

```sh
python mid_simplify_v5.py --remove-crossings 0,1,2 --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

## Python API

```python
import mid_simplify_v5 as simplify

code = simplify.parse_pd_code("PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]")
result = simplify.reduce_pd_code(code, reduction_round=-1)
print(result.to_json()["final_pd_code"])
```

`find_simplification` defaults to `max_paths=-1`, which uses deterministic
heuristic green-path sampling. Pass `ban_heuristic=True` with `max_paths=-1`
to enumerate all green paths for a manageable input. `reduce_pd_code` is the
high-level API that applies witnesses and returns the internal final PD code.
Use `result.to_json()["final_pd_code"]` or `format_final_pd_code(result.code)`
when presenting the final PD code to users. The plain `format_pd_code`
function preserves the internal tuple order and labels.
If `reduce_pd_code(..., timeout=K)` exceeds its deadline, it returns the
current best result with `result.timed_out == True`.

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
.\.venv\Scripts\python tools\benchmark_cpp_python.py --repeat 1
```

The benchmark runner also checks C++ CLI, Python C++ interface, and Python JSON
outputs in the same run. The dataset, chart-generation commands, and latest
local results are documented in [Benchmarking](benchmarking.md).
