# Packaging

This project uses a Python packaging helper as its build and packaging entry
point. The script works on Windows, Linux, and macOS as long as Python 3 and a
C++17 compiler are available.

## Requirements

- Python 3.8 or newer.
- A C++17 compiler:
  - Windows: MinGW-w64 `g++`, LLVM `clang++`, or MSVC `cl`. Legacy MinGW.org
    toolchains are not supported because they do not provide the C++ threading
    runtime used by the simplifier.
  - Linux: `c++`, `g++`, or `clang++`.
  - macOS: Apple Clang or another `clang++`/`c++` compiler.

The compiler can be selected with `--cxx` or the `CXX` environment variable.

## Common Commands

Build the command-line executable and shared library:

```sh
python tools/package.py build
```

Build and run the C++ unit tests:

```sh
python tools/package.py test
```

This always runs the C++ unit suite and randomized invariant-profile checks.
If the optional `pd_code_to_diagram` development package is importable, it also
renders representative C++ outputs and checks their diagram round trips;
otherwise that integration check is reported as skipped instead of making a
fresh self-contained checkout fail.

Build a redistributable directory:

```sh
python tools/package.py package --run-tests
```

Use a different compiler or debug configuration:

```sh
python tools/package.py build --cxx clang++ --config debug
```

Clean build output:

```sh
python tools/package.py clean
```

## Package Layout

The default package directory is `dist/cpp-pd-code-simplify`.

```text
dist/cpp-pd-code-simplify/
  bin/
    pd_simplify(.exe)
  lib/
    pdcode_simplify.dll
    libpdcode_simplify.so
    libpdcode_simplify.dylib
  include/
    pdcode_simplify/pdcode_simplify.hpp
  docs/
  LICENSE
  README.md
  manifest.json
```

Only the shared-library name for the current platform is generated. On Windows,
MinGW also produces `libpdcode_simplify.dll.a`; MSVC produces
`pdcode_simplify.lib`.

## Runtime Dependencies

The project itself only uses the C++ standard library and platform APIs. When
the compiler runtime is not part of the platform baseline, the package script
tries to copy the needed runtime libraries next to the executable and shared
library:

- Windows MinGW: `libstdc++-6.dll`, `libgcc_s_*.dll`, and
  `libwinpthread-1.dll` are copied when found beside the compiler or on `PATH`.
- Linux: non-system libraries reported by `ldd` are copied when `ldd` is
  available.
- macOS: non-system libraries reported by `otool -L` are copied when `otool`
  is available.

System libraries are not copied. If a custom compiler or package manager uses a
nonstandard runtime layout, inspect `manifest.json` and the `bin/` and `lib/`
directories before redistribution.

## Dynamic Library Use

The core C++ API is header-only. Include the public header directly:

```cpp
#include "pdcode_simplify/pdcode_simplify.hpp"
```

No `PDCODE_SIMPLIFY_SHARED` import macro is needed for direct C++ use. The
packaged dynamic library is kept for compatibility with packaging workflows;
new native C++ code should prefer the header-only API documented in
[Header-Only C++ Use](header-only.md).
