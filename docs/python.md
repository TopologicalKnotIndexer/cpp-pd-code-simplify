# Python Prototype

`mid_simplify_v5.py` is a refactored pure Python version of the
mid-simplification search. It exposes both a Python API and a command-line
interface. Its CLI and `run_job` helper run pure Python R1-move removal, true
R2-bigon removal, and pure Python nugatory-crossing removal before the search
by default.

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
mid-simplification rounds; the default `-1` runs until stable. In heuristic
mode, each round first tries the prototype-compatible deterministic heuristic
route. With one worker this is the legacy first-hit route; with multiple
workers and an initial input of at least 500 crossings, red paths are searched
in deterministic batches and the best validated witness in the batch window is
selected by actual crossing reduction. Jobs that start below 500 crossings keep
the legacy first-hit route. While heuristic search keeps succeeding, the
internal PD row and label order is preserved. If heuristic search misses, the
current PD code is canonicalized at the non-heuristic handoff boundary, then
`r3_prepass`, `non_monotone`, brute force, and the final RIII failover are
tried as needed.
Use `--timeout K` to cap each PD-code job at
`K` seconds; the default `-1` has no timeout. A timed-out job returns the best
PD code found so far and sets `timed_out` in JSON/text output.
With a positive timeout, ordinary inputs below the 500-crossing multi-worker
threshold give each heuristic stage a 20 second soft slice before handing the
round to adaptive helper stages.
Use `--quit-at-crossing N` to stop early once the current PD code has at most
`N` crossings; the default `-1` disables this and output sets
`stopped_by_crossing_limit` when the threshold is reached. Brute-force
green-path enumeration is streamed rather than stored in one large list; use
`--bruteforce-budget N` to cap brute-force green-path checks per PD code. The
default is `200000`, and `-1` disables that cap. A budget stop returns the
current best PD code and sets `resource_limited`. Use `--verbose` to print
timestamped progress logs to stderr. Verbose log lines use local wall-clock time in
`YYYY-MM-DD HH:MM:SS` format. When `--max-thread -1` reaches a brute-force
search phase, verbose logs also include `actual_threads`, the worker count
selected for that phase. `Ctrl+C` cancels active multiprocessing workers and
exits with status `130`. Final output PD-code strings are normalized for
display: each crossing is written from the under-incoming edge, labels are
renumbered along oriented components from `1`, and crossing rows are sorted
lexicographically.

Use `--show-step-pd` to print `step_pd_code[ROUND]: PD[...]` to stdout after
each mid-simplification witness is applied and canonicalized, before that
round's automatic local cleanup. With `--reapr`, every REAPR candidate that
passes the full invariant profile is also printed with round `0` before the
selected candidate's ordinary local cleanup.
This diagnostic output is disabled by default because it can be large and
shares stdout with JSON/text results.
Use `--reapr` to enable the same experimental invariant-guarded projection
oracle as the C++ implementation. It checks component count, Alexander
determinant, and Alexander roots over `F_11`, `F_19`, and `F_31` before
accepting a candidate. There is no crossing-drop window: a very small
projection may be accepted when the profile matches. Accepted candidates return
to the normal iterative simplification loop. It is disabled by default and can
still change the knot or link type; output includes `reapr_warning` when the
oracle is used. Use `--reapr-retry-max N` to cap the deterministic retry
sequence; the default is `3`, and `0` disables REAPR candidate attempts.
Use `--log-file FILEPATH` to tee stdout and stderr into a flushed backup log
file while keeping the normal terminal output unchanged.

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
to enumerate all green paths for a manageable input. Default heuristic mode
keeps the original prototype red-path order and returns the first validated
witness found in the current round. `reduce_pd_code` is the high-level API that
applies witnesses and returns the internal final PD code.
The Python prototype uses the same deterministic non-monotone failover as the
C++ implementation; repeated no-timeout searches inside one Python process
cache exact search results for identical canonical PD codes and search
settings.
Use `result.to_json()["final_pd_code"]` or `format_final_pd_code(result.code)`
when presenting the final PD code to users. The plain `format_pd_code`
function preserves the internal tuple order and labels.
If `reduce_pd_code(..., timeout=K)` exceeds its deadline, it returns the
current best result with `result.timed_out == True`.
If `reduce_pd_code(..., bruteforce_budget=N)` exhausts its brute-force budget,
it returns the current best result with `result.resource_limited == True`.
Pass `quit_at_crossing=N` to stop once the current PD code has at most `N`
crossings; `-1` disables the threshold.
Pass `show_step_pd=True` to `reduce_pd_code` to print each post-witness PD
code and each accepted REAPR candidate, or pass `step_pd_output=callable` to
receive `(round_index, code)` in Python code.
Pass `reapr=True` only for experimental invariant-guarded projection
candidates; verify independent invariants when it is used. Pass
`reapr_retry_max=N` to control the deterministic retry cap.

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

Run randomized invariant-profile checks. This compares component count,
Alexander determinant fingerprint, and Alexander root sets modulo 11, 19, and
31 for the input and simplified output:

```sh
.\.venv\Scripts\python tools\test_random_invariant_profile.py --include-interface
```

## Benchmarks

Compare runtime and peak RSS memory usage:

```sh
.\.venv\Scripts\python tools\benchmark_cpp_python.py --repeat 1
```

The benchmark runner also checks C++ CLI, Python C++ interface, and Python JSON
outputs in the same run. The dataset, chart-generation commands, and latest
local results are documented in [Benchmarking](benchmarking.md).
