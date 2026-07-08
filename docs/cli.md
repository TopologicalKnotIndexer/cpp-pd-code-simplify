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
--verbose                      Print progress logs to stderr.
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
mid-simplification witnesses. In heuristic mode, if the heuristic can no
longer find an applicable path, the executable runs one brute-force
enumeration pass before declaring the diagram stable. Use
`--reduction-round K` to cap the number of applied mid-simplification rounds.

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

The process exits with code `0` when every input is processed successfully,
including inputs that are already stable. It exits with code `2` when at
least one input reports an error. In batch mode, errors are isolated to the
failing item and later PD codes still run.

## C++ Library Use

```cpp
#include "pdcode_simplify/pdcode_simplify.hpp"

auto code = pdcode_simplify::parse_pd_code("PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]");
auto components = pdcode_simplify::analyze_components(code);
auto prepared = pdcode_simplify::simplify_pd_code(code);
auto result = pdcode_simplify::reduce_pd_code(code);
std::cout << pdcode_simplify::format_pd_code(result.code) << "\n";
```

The library also includes deterministic test helpers for Reidemeister I/II
stress tests:

```cpp
pdcode_simplify::RandomInflationOptions options;
options.moves = 18;
options.seed = 101;

auto inflated = pdcode_simplify::randomly_increase_crossings(code, options);
auto simplified = pdcode_simplify::simplify_reidemeister_i_ii(inflated.code);
```
