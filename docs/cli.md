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
--timeout K                    Per-PD-code timeout in seconds; -1 means no timeout.
--verbose                      Print timestamped progress logs to stderr.
--show-step-pd                 Print each post-witness PD code to stdout.
--known-crossingless-components N
                               Add N components not representable in PD code.
--remove-crossings LIST        Report component counts after removing crossings.
--help, -h                     Show help.
```

`--max-paths -1` is the default. In that mode the executable uses deterministic
heuristic green-path sampling. Add `--ban-heuristic` to run exhaustive
green-path enumeration instead. If `--max-paths` is any other integer, the
legacy bounded path collector is used.

`--reduction-round -1` is the default. It repeatedly applies valid
mid-simplification witnesses. Use `--reduction-round K` to cap the number of
applied mid-simplification rounds. In heuristic mode, whenever the heuristic
cannot find an applicable path before the round cap is exhausted, the
executable runs a brute-force enumeration pass. If brute force finds a
witness, that witness is applied and the next round starts again in heuristic
mode; if brute force also fails, the diagram is treated as stable.
Verbose log lines are prefixed with local wall-clock time in
`YYYY-MM-DD HH:MM:SS` format. When `--max-thread -1` reaches a brute-force
search phase, verbose logs also include `actual_threads`, the worker count
selected for that phase.

`--timeout -1` is the default and disables time limits. `--timeout K`, where
`K` is a positive integer, stops the current PD-code job after approximately
`K` seconds and still prints the best PD code found so far. JSON and text
output include `timed_out`; in batch mode, later jobs continue. Pressing
`Ctrl+C` requests cooperative cancellation and exits with status `130`.

`--show-step-pd` prints `step_pd_code[ROUND]: PD[...]` immediately after each
mid-simplification witness is applied and before the automatic R1/nugatory
cleanup for that round. In batch mode the line is prefixed with the input
label. This diagnostic output uses stdout and is therefore intentionally off
by default, especially when `--json` output will be parsed by another program.

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

R1-move removal followed by nugatory-crossing removal is enabled by default.
Batch mode keeps going after a single input fails; failed items are reported
with an `error` field in JSON output or an `error:` line in text output.

The process exits with code `0` when every input reaches a non-timed-out
result, including inputs that are already stable. It exits with code `2` when
at least one input reports an error or `timed_out: true`. In batch mode, errors
and timeouts are isolated to the affected item and later PD codes still run.

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
R1+nugatory preprocessing stage:

```cpp
pdcode_simplify::RandomInflationOptions options;
options.moves = 18;
options.seed = 101;
options.type_ii_percentage = 0;

auto inflated = pdcode_simplify::randomly_increase_crossings(code, options);
auto simplified = pdcode_simplify::simplify_pd_code(inflated.code);
```
