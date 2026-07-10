# Heuristic Path Sampling

This document describes the deterministic green-path heuristic used when
`max_paths` is `-1`.

## Search Modes

The simplifier has three green-path search modes:

| Setting | Mode | Meaning |
| --- | --- | --- |
| `max_paths != -1` | `bounded` | Use depth-first green-path ordering and stop after the configured cap. |
| `max_paths == -1` | `heuristic` | Use deterministic priority sampling with fixed budgets. This is the default. |
| `max_paths == -1` plus `--ban-heuristic` | `bruteforce` | Stream all eligible simple green paths for each red path, unless the brute-force budget is exhausted. |

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

Brute-force mode is complete only when it is allowed to finish. The
implementation uses streaming DFS and does not cache the full path set, but it
also has a separate safety budget: `--bruteforce-budget 200000` by default,
or `--bruteforce-budget -1` for no budget. Budget exhaustion returns the
current best PD code with `resource_limited=true`.

In the full high-level reduction loop, a heuristic miss is not immediately
treated as stability. The simplifier first tries the deterministic
non-monotone failover described in
[Algorithm and Correctness](algorithm-and-correctness.md), then runs the
brute-force proof pass if the failover does not lower the crossing count.

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
complete: it can miss a witness or a useful detour that later failover stages
may find if it falls outside the sampled frontier. Use
`--ban-heuristic --max-paths -1 --bruteforce-budget -1` when complete
enumeration is required for a manageable input.
