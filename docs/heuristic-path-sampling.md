# Heuristic Path Sampling

This document describes the deterministic green-path heuristic used when
`max_paths` is `-1`.

## Search Modes

The simplifier has three green-path search modes:

| Setting | Mode | Meaning |
| --- | --- | --- |
| `max_paths != -1` | `bounded` | Use the original depth-first path collector and stop after the configured cap. |
| `max_paths == -1` | `heuristic` | Use deterministic priority sampling with fixed budgets. This is the default. |
| `max_paths == -1` plus `--ban-heuristic` | `bruteforce` | Enumerate all eligible simple green paths for each red path. |

The command-line JSON field `last_path_search_mode` records the last search
mode used by the reduction loop. The Python prototype and the C++
implementation use the same mode names, constants, ordering rules, and
tie-breaking rules. The Python C++ interface calls the C++ backend directly.

## Motivation

For a fixed red path, the green search is a simple-path search in the face
dual graph. Brute-force enumeration is complete, but the number of simple
paths can grow quickly on large diagrams. A small fixed cap such as
`max_paths=100` is fast but brittle because it depends on incidental DFS
ordering.

The default `max_paths=-1` mode therefore does not mean "pick a hidden cap".
It switches to a separate deterministic sampling strategy that tries to spend
work on paths that are more likely to pass the disk-consistency validator.

## Scoring

For each source-target face pair, the heuristic first computes a reverse
breadth-first distance from every face to the target. This distance ignores
high-weight red-interior barriers, so it is only a reachability and length
estimate, not a proof that a final path is valid.

The sampler then expands partial paths through a priority queue. Each state
stores:

- the current face;
- the path from the source;
- the visited-face set;
- the accumulated dual-graph weight;
- a branch penalty, increased when a step has many low-priority alternatives;
- a deterministic serial number used only for stable tie-breaking.

Candidate next steps are sorted by:

1. edge weight;
2. estimated remaining distance to the target;
3. degree penalty of the next face;
4. next face id;
5. dual-edge index.

The priority queue orders states by:

1. accumulated weight plus estimated remaining distance;
2. current path length plus estimated remaining distance;
3. branch penalty;
4. accumulated weight;
5. current path length;
6. insertion serial number.

The first two keys prefer short and low-weight paths. The branch penalty keeps
the search from spending all budget inside a single locally dense area. The
serial number makes the result reproducible across platforms.

## Fixed Budgets

The heuristic uses fixed constants shared by C++ and Python:

```text
beam width per (depth, face): 8
state budget: min 128, max 4096
path budget: min 24, max 384
```

For each red path, the concrete state budget is derived from the face count and
the red-path cutoff:

```text
state_budget = clamp(face_count * cutoff * 8, 128, 4096)
path_budget  = clamp(face_count * 2 + cutoff * 8, 24, 384)
```

These budgets are not inferred from `max_paths`. They are part of the
heuristic search mode itself.

## Validation And Correctness

The heuristic only changes which green candidates are proposed to the existing
validator. It does not accept a path by score alone. Every returned witness
still passes the same over/under propagation and disk-consistency checks used
by brute-force mode.

Therefore a witness reported by heuristic mode is sound. The heuristic is not
complete: it can miss a witness that brute-force mode would find if the witness
falls outside the sampled frontier. Use `--ban-heuristic --max-paths -1` when
complete enumeration is required for a manageable input.

The C++-only benchmark in `tools/benchmark_cpp_heuristic.py` compares
heuristic mode with brute-force mode on the ten large random benchmark
diagrams. It reports both runtime and actual crossing reduction after applying
the configured number of reduction rounds. The reduction metric is:

```text
original crossings - final crossings
```

The result is divided by the original crossing count. The committed large-case
chart uses a three-round cap and a timeout budget for brute-force runs, because
full terminal brute-force stability proofs can dominate runtime on
120-150-crossing diagrams.
