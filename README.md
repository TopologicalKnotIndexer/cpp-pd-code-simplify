# cpp-pd-code-simplify

A dependency-free C++17 project for simplifying knot and link planar diagram
codes. The repository also includes a refactored Python prototype for
differential testing. User-facing tools first remove R1 moves, true R2 bigons,
and nugatory crossings, then iteratively find and apply mid-simplification
moves until the configured round limit is reached or no further move is found.

## Quickstart

Build the C++ CLI:

```sh
python tools/package.py build
```

Run one PD code:

```sh
./build/bin/pd_simplify --pd-code "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]"
```

On Windows, use `.\build\bin\pd_simplify.exe` for the executable path.
For CLI options, see [Command-line interface](docs/cli.md).

Install and use the Python C++ interface:

```sh
g++ --version
pip install cpp-pd-code-simplify-interface
python -m cpp_pd_code_simplify_interface "PD[]"
```

The PyPI package compiles a cached local dynamic library on first use, so
`g++` must be available from `PATH` before running it. From Python:

```python
import cpp_pd_code_simplify_interface as simplify

result = simplify.simplify("PD[]")
print(result["final_pd_code"])
```

For Python, packaging, testing, benchmarking, and direct header-only C++ use,
see the manuals below.

## Benchmark Snapshot

Original lightweight benchmark:

![Original benchmark bar chart comparing C++ CLI, Python C++ interface, and Python](docs/assets/benchmark_original_cpp_python.png)

| Engine | Avg Time / PD Code (s) | Peak RSS (MiB) |
| --- | ---: | ---: |
| C++ CLI | 0.006480 | 6.344 |
| Python C++ interface | 0.578396 | 79.598 |
| Python | 1.700927 | 440.652 |

Zip-random large-case benchmark:

![Zip-random benchmark bar chart comparing C++ CLI, Python C++ interface, and Python](docs/assets/benchmark_random_cpp_python.png)

| Engine | Avg Time / PD Code (s) | Peak RSS (MiB) |
| --- | ---: | ---: |
| C++ CLI | 1.346003 | 13.270 |
| Python C++ interface | 1.970066 | 84.641 |
| Python | 5.994071 | 458.363 |

This local run uses the deterministic benchmark set documented in
[Benchmarking](docs/benchmarking.md). The lightweight suite is measured with
`--max-paths -1 --ban-heuristic --reduction-round -1 --max-thread 16
--bruteforce-budget -1`. The large zip-random throughput chart uses one
hundred active zip-random cases with `--max-paths -1 --reduction-round -1
--max-thread 16 --bruteforce-budget 200000`; the benchmark checks C++ CLI,
Python C++ interface, and Python outputs for exact JSON agreement in the same
batch-mode run that measures time and peak RSS.

## Documentation

- [Command-line interface](docs/cli.md)
- [Header-only C++ use](docs/header-only.md)
- [Python prototype and comparison tools](docs/python.md)
- [Python C++ interface package](docs/python-interface.md)
- [Algorithm and correctness](docs/algorithm-and-correctness.md)
- [Heuristic path sampling](docs/heuristic-path-sampling.md)
- [Packaging](docs/packaging.md)
- [Benchmarking](docs/benchmarking.md)
- [C++ zip-random time analysis](docs/cpp-time-analysis.md)
- [SnapPy/Spherogram-flavor comparison](docs/snappy-flavor-comparison.md)
- [Python and C++ comparison results](docs/python-cpp-comparison.md)

## Acknowledgements

The algorithm and the original `mid_simplify_v5.py` prototype were implemented
by [zzhouhe](https://github.com/zzhouhe), also available on Bilibili at
[space.bilibili.com/37877654](https://space.bilibili.com/37877654). This
project does not claim original algorithmic contributions; it ports that
algorithm to C++ and adds command-line tooling, documentation, tests,
benchmarks, and component-accounting infrastructure around the port.

## Notes

Plain PD codes cannot encode components with no crossings. Both the C++ and
Python implementations expose component-accounting APIs and CLI options so
that crossingless components are counted explicitly instead of being lost.

## Citation

If you use this project, please cite it as:

```bibtex
@misc{cpp_pd_code_simplify_2026,
  author = {{GGN-2015}},
  title = {{cpp-pd-code-simplify}: A C++ Port of a PD-Code Mid-Simplification Algorithm},
  year = {2026},
  url = {https://github.com/TopologicalKnotIndexer/cpp-pd-code-simplify},
  note = {The underlying algorithm and original Python prototype were implemented by zzhouhe.}
}
```
