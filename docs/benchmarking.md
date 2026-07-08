# Benchmarking

The benchmark harness compares the C++ executable and the refactored Python
prototype on the same PD-code inputs. It records wall-clock time and peak RSS
for each process, then produces CSV, JSON, and a matplotlib-style bar chart.

## Dataset

Public knot tables such as [KnotInfo](https://knotinfo.org/) and
[The Knot Atlas](https://katlas.org/wiki/Main_Page) are useful references for
PD notation and standard knot families. The default repository benchmark does
not download those full tables: the complete external data is larger than this
project needs, and network-dependent benchmarks are hard to reproduce. Instead,
`tools/benchmark_dataset.py` defines a deterministic, lightweight dataset with
standard seed diagrams and generated variants.

| Case | Family | Crossings | Why It Is Included |
| --- | --- | ---: | --- |
| `3_1_trefoil` | prime seed | 3 | Smallest nontrivial knot seed. |
| `4_1_figure_eight` | prime seed | 4 | Small alternating prime knot with different structure from the trefoil. |
| `5_1_torus` | torus family | 5 | Compact `T(2, 5)` torus-knot seed. |
| `7_1_torus` | torus family | 7 | Medium member of the same scalable family. |
| `9_1_torus` | torus family | 9 | Larger torus-family input with more red/green path work. |
| `trefoil_r1x12` | inflated | 15 | Trefoil after twelve deterministic reverse Reidemeister-I moves. |
| `figure_eight_r1x12` | inflated | 16 | Figure-eight knot after twelve deterministic reverse Reidemeister-I moves. |
| `reference_31` | reference hard case | 31 | Historical hard input bundled with the project. |

The inflated cases preserve the underlying knot type because each added
crossing is introduced by a reverse type-I Reidemeister move. The fixed seeds
make the generated PD codes stable across platforms.

## Running

Install development dependencies:

```sh
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Build the C++ executable, then run:

```sh
python tools/package.py build
```

```sh
.\.venv\Scripts\python tools\benchmark_cpp_python.py ^
  --repeat 3 ^
  --plot docs\assets\benchmark_cpp_python.png ^
  --summary-csv docs\assets\benchmark_summary.csv ^
  --raw-csv docs\assets\benchmark_raw.csv ^
  --json docs\assets\benchmark_results.json
```

On Linux and macOS, use `.venv/bin/python` and shell line continuations with
`\` instead of `^`.

## Local Results

The committed chart was generated on the local Windows development machine
with a MinGW `g++ -O3 -DNDEBUG` C++ executable, `max_paths=100`, and three
repeats per case and engine.

![C++ and Python benchmark bar chart](assets/benchmark_cpp_python.png)

Aggregate arithmetic means over the eight benchmark cases:

| Engine | Average Time (s) | Average Peak RSS (MiB) |
| --- | ---: | ---: |
| C++ | 0.181460 | 5.447 |
| Python | 4.147811 | 21.897 |

In this run, the C++ executable was `22.9x` faster on average, while the Python
prototype used `4.0x` the average peak RSS. Per-case averages are stored in
[`docs/assets/benchmark_summary.csv`](assets/benchmark_summary.csv), and raw
measurements are stored in [`docs/assets/benchmark_raw.csv`](assets/benchmark_raw.csv).
