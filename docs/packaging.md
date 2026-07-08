# Packaging

This project uses a Python packaging helper as its build and packaging entry
point. The script works on Windows, Linux, and macOS as long as Python 3 and a
C++14 compiler are available.

## Requirements

- Python 3.8 or newer.
- A C++14 compiler:
  - Windows: MinGW `g++`, LLVM `clang++`, or MSVC `cl`.
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

Include the public header:

```cpp
#include "pdcode_simplify/pdcode_simplify.hpp"
```

When consuming the Windows DLL from another C++ target, define
`PDCODE_SIMPLIFY_SHARED` before including the header so the API is imported
with `__declspec(dllimport)`.

The exported C++ API uses standard-library types, so consumers should use a
compiler and runtime ABI compatible with the package that produced the dynamic
library.
