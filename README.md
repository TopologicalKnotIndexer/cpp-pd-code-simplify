# cpp-pd-code-simplify

A dependency-free C++14 project for finding mid-simplification witnesses in
knot and link planar diagram codes. The repository also includes a refactored
Python prototype for differential testing.

## Quickstart

Build and test:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
ctest --test-dir build --build-config Release --output-on-failure
```

Run one PD code:

```sh
pd_simplify --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

Run the Python prototype:

```sh
python mid_simplify_v5.py --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

Run C++/Python differential tests:

```sh
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python tools\compare_cpp_python.py --include-reference
```

On Linux and macOS, use `.venv/bin/python` instead of
`.\.venv\Scripts\python`.

## Documentation

- [Command-line interface](docs/cli.md)
- [Python prototype and comparison tools](docs/python.md)
- [Algorithm and correctness](docs/algorithm-and-correctness.md)
- [Python and C++ comparison results](docs/python-cpp-comparison.md)

## Notes

Plain PD codes cannot encode components with no crossings. Both the C++ and
Python implementations expose component-accounting APIs and CLI options so
that crossingless components are counted explicitly instead of being lost.
