# Command-Line Interface

The `pd_simplify` executable follows the same input style as `cppkh`: pass a
literal `PD[...]` string, a file, or every `.txt` and `.pd` file in a
directory.

## Build

Build and run the unit tests:

```sh
python tools/package.py test
```

Build the executable and shared library:

```sh
python tools/package.py build
```

Useful build variables:

```sh
python tools/package.py build --config debug
python tools/package.py build --cxx clang++
python tools/package.py build --build-dir out
```

The executable is written to `build/bin/pd_simplify` on Linux and macOS, and
to `build/bin/pd_simplify.exe` on Windows.

The helper scripts are thin wrappers around `python tools/package.py test`.

Windows:

```powershell
.\scripts\build.ps1
```

Linux and macOS:

```sh
./scripts/build.sh
```

Packaging, including dynamic-library output, is documented in
[Packaging](packaging.md).

## Inputs

Run one literal PD code:

```sh
pd_simplify --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

The final `final_pd_code` string printed by text and JSON modes is normalized
for display: each crossing is written from the under-incoming edge, labels are
renumbered along oriented components from `1`, crossing rows are sorted
lexicographically, and the simplification algorithm keeps its internal
numbering unchanged.

Read one input file:

```sh
pd_simplify --pd-file diagram.pd --json
```

Read every `.txt` and `.pd` file in a directory:

```sh
pd_simplify --pd-dir samples
```

`--pd-code` may contain one or more `PD[...]` blocks. Input files may contain
multiple PD codes, one or more standard `PD[...]` blocks, or labelled lines
such as:

```text
trefoil: PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]
```

If no input is given, the executable tries to read `PD.txt` from the current
directory. Python-style crossing lists are still accepted for compatibility.
In the CLI, standard `PD[]` input is treated as one crossingless unknot
component.

## Options

```text
--pd-code CODE, -c CODE        Read a literal PD[...] string.
--pd-file FILE, -f FILE        Read one input file.
--pd-dir DIR, -d DIR           Read every .txt and .pd file in a directory.
--json                         Print JSON output.
--max-paths N                  Cap accepted green paths; default -1.
--ban-heuristic                With --max-paths -1, enumerate all green paths.
--reduction-round K            Maximum mid-simplification rounds; -1 means until stable.
--max-thread N                 Maximum brute-force worker threads; -1 means auto.
--bruteforce-budget N          Cap brute-force green-path checks; default 200000, -1 means no cap.
--reapr                        Enable the experimental invariant-guarded projection oracle.
--reapr-retry-max N            Maximum deterministic REAPR attempts; default 3.
--timeout K                    Per-PD-code timeout in seconds; -1 means no timeout.
--quit-at-crossing N           Stop once crossings are at most N; -1 disables it.
--verbose                      Print timestamped progress logs to stderr.
--show-step-pd                 Print post-witness and accepted REAPR PD codes to stdout.
--log-file FILEPATH            Tee stdout and stderr output into a flushed log file.
--known-crossingless-components N
                               Add N components not representable in PD code.
--remove-crossings LIST        Report component counts after removing crossings.
--help, -h                     Show help.
```

`--max-paths -1` is the default. In that mode the executable uses deterministic
heuristic green-path sampling when the heuristic stage is reached. The whole
default reducer first runs a 180 second efficient adaptive phase whose initial
order is `r3_prepass`, legacy first-hit heuristic search, and `non_monotone`.
If that phase expires before the job finishes, the current best PD code is used
as the start of a deterministic multi-worker best-batch heuristic that chooses
the validated witness with the best actual crossing reduction inside the batch
lookahead window. Add `--ban-heuristic` to run exhaustive green-path
enumeration instead.
If
`--max-paths` is any other integer, the bounded DFS ordering is used.

Brute-force green-path enumeration is streamed: each candidate path is checked
as soon as it is generated, so the implementation does not keep the full set of
simple green paths in memory. `--bruteforce-budget N` caps the number of
brute-force green paths checked for one PD-code job. The default is `200000`;
use `--bruteforce-budget -1` only when an unbounded proof attempt is acceptable.
If the budget is exhausted, output still includes the current best PD code and
sets `resource_limited: true`.

`--reduction-round -1` is the default. It repeatedly applies valid
mid-simplification witnesses. Use `--reduction-round K` to cap the number of
applied mid-simplification rounds. Final JSON/text PD codes and
`--show-step-pd` output are canonicalized. The default route starts with an
efficient 180 second adaptive phase. Its initial scheduler order is
`r3_prepass`, then legacy first-hit heuristic search, then `non_monotone`; stage
scores adapt after successes, misses, and soft timeouts. If the efficient phase
expires, the current best PD code is canonicalized and the remaining search
uses the multi-worker best-batch heuristic route. In that hard-case route,
heuristic witnesses keep the prototype-compatible internal order until the next
non-heuristic handoff.
Productive helper stages gain priority; misses and soft stage timeouts lower
priority. If a helper stage reduces the diagram, the result is applied and the
next round starts again from heuristic mode. If all helper stages miss, the
executable runs a brute-force enumeration pass. A diagram is treated as stable
only after heuristic search, helper stages, brute force, and the final RIII
failover all fail on the canonical handoff state. Verbose mode prints
`non_heuristic_handoff`, `adaptive_order`, and per-stage scores.
Verbose log lines are prefixed with local wall-clock time in
`YYYY-MM-DD HH:MM:SS` format. When `--max-thread -1` reaches a brute-force
search phase, verbose logs also include `actual_threads`, the worker count
selected for that phase. If stderr is a terminal or PTY, verbose stderr logs
use ANSI colors to distinguish timestamps, package names, stage names, numeric
values, success markers, and failure or timeout markers. If stderr is a pipe,
regular file, or other non-terminal target, logs stay plain text. Set
`NO_COLOR=1` or `TERM=dumb` to force plain stderr output.

`--reapr` is disabled by default. When enabled, the executable tries an
experimental deterministic reembedding/projection oracle after the default
R1/R2/nugatory preprocessing and before the mid-simplification search. The
oracle accepts a candidate only when it has fewer crossings and the internal
invariant profile is unchanged. It does not impose a crossing-drop window, so a
very small projection may be accepted when the profile matches. Accepted
candidates return to the normal iterative simplification loop. The profile includes total
component count, Alexander determinant, and Alexander roots over `F_11`,
`F_19`, and `F_31`. This guard is stricter than determinant
alone, but it is not a proof that the output is the same knot or link. Output
therefore includes `reapr_used`, `reapr_status`, `reapr_warning`,
`alexander_determinant_before`, `alexander_determinant_after`,
`reapr_invariants_before`, and `reapr_invariants_after`. Treat any `--reapr`
result as a candidate that still needs independent invariant checks.
`--reapr-retry-max N` controls the bounded deterministic retry sequence used
after a rejected first template. `N=0` disables REAPR candidate attempts.

`--timeout -1` is the default and disables time limits. `--timeout K`, where
`K` is a positive integer, stops the current PD-code job after approximately
`K` seconds and still prints the best PD code found so far. JSON and text
output include `timed_out`; in batch mode, later jobs continue. Pressing
`Ctrl+C` requests cooperative cancellation and exits with status `130`.

`--quit-at-crossing -1` is the default and disables crossing-target early
exit. `--quit-at-crossing N`, where `N` is a non-negative integer, stops the
current job as soon as the current PD code has at most `N` crossings, even if
later rounds might simplify further. Output includes
`stopped_by_crossing_limit`.

`--show-step-pd` prints `step_pd_code[ROUND]: PD[...]` immediately after each
mid-simplification witness is applied and canonicalized, before the automatic
local cleanup for that round. When `--reapr` is enabled, every REAPR candidate
that passes the full invariant profile is also printed with round `0` before
the selected candidate's ordinary local cleanup.
In batch mode the line is prefixed with the input label. This diagnostic output
uses stdout and is therefore intentionally off by default, especially when
`--json` output will be parsed by another program.

`--log-file FILEPATH` tees everything written to stdout and stderr into the
given file and flushes that file after each write. The normal terminal output
is unchanged. The backup log file is always written without ANSI color codes,
even when terminal stderr is colorized.

## Component Accounting

Plain PD codes cannot store components with no crossings. If a previous
operation has already produced such components, pass their count explicitly:

```sh
pd_simplify --known-crossingless-components 1 --pd-file diagram.pd
```

When testing a move that removes crossings, the CLI can report how many link
components would become crossingless:

```sh
pd_simplify --remove-crossings 0,1,2 --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

R1-move removal, true R2-bigon removal, and nugatory-crossing removal are
enabled by default.
Batch mode keeps going after a single input fails; failed items are reported
with an `error` field in JSON output or an `error:` line in text output.

The process exits with code `0` when every input reaches a non-timed-out,
non-resource-limited result, including inputs that are already stable. It exits
with code `2` when at least one input reports an error, `timed_out: true`, or
`resource_limited: true`. In batch mode, errors, timeouts, and resource-limit
stops are isolated to the affected item and later PD codes still run.

## C++ Library Use

```cpp
#include "pdcode_simplify/pdcode_simplify.hpp"

auto code = pdcode_simplify::parse_pd_code("PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]");
auto components = pdcode_simplify::analyze_components(code);
auto prepared = pdcode_simplify::simplify_pd_code(code);
auto result = pdcode_simplify::reduce_pd_code(code);
std::cout << pdcode_simplify::format_final_pd_code(result.code) << "\n";
```

The library also includes deterministic crossing-increasing helpers. Set
`type_ii_percentage` to zero when you specifically want to test the default
R1-focused preprocessing stage:

```cpp
pdcode_simplify::RandomInflationOptions options;
options.moves = 18;
options.seed = 101;
options.type_ii_percentage = 0;

auto inflated = pdcode_simplify::randomly_increase_crossings(code, options);
auto simplified = pdcode_simplify::simplify_pd_code(inflated.code);
```
