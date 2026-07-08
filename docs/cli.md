# Command-Line Interface

The `pd_simplify` executable follows the same input style as `cppkh`: pass a
literal `PD[...]` string, a file, or every `.txt` and `.pd` file in a
directory.

## Build Scripts

Windows:

```powershell
.\scripts\build.ps1
```

Linux and macOS:

```sh
./scripts/build.sh
```

Manual CMake commands:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
ctest --test-dir build --build-config Release --output-on-failure
```

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
--max-paths N                  Cap accepted green paths; use -1 for unlimited.
--known-crossingless-components N
                               Add N components not representable in PD code.
--remove-crossings LIST        Report component counts after removing crossings.
--help, -h                     Show help.
```

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

The process exits with code `0` when a witness is found, `1` when no witness
is found, and `2` for invalid input or runtime errors.

## C++ Library Use

```cpp
#include "pdcode_simplify/pdcode_simplify.hpp"

auto code = pdcode_simplify::parse_pd_code("PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]");
auto components = pdcode_simplify::analyze_components(code);
auto result = pdcode_simplify::find_simplification(code);
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
