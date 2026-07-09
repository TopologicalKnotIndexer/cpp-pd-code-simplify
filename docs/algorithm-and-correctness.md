# Algorithm and Correctness

This document describes the core ideas used by `cpp-pd-code-simplify`.
The implementation is a standalone C++ translation of the mathematical
algorithmic structure, not a wrapper around a Python or graph library.

## Diagram Model

A planar diagram code is represented as a list of crossings

```text
[(a0, a1, a2, a3), ...]
```

where every label occurs exactly twice. Equal labels identify the two ends
of the same diagram edge. Internally, an endpoint is stored as
`(crossing_index, strand_index)`.

The implementation reconstructs the same local operations used by crossing
entry based link libraries:

- `opposite(e)` follows the diagram edge with the same PD label;
- `next(e)` moves through one crossing along the same link component;
- `next_corner(e)` moves along the boundary of a complementary face.

The code first orients all crossings consistently with the PD component
orientation convention. This gives each crossing a sign and determines the
two crossing entries used by component traversal and red path generation.

## Face Dual Graph

After the endpoint operations are available, the algorithm enumerates all
faces by repeatedly applying `next_corner`. Each face becomes a vertex in a
dual graph. Each diagram edge separates two faces, so it becomes an edge in
the dual graph.

For each dual edge, the implementation stores the two endpoint interfaces
that it crosses. This is important later: a candidate green path is a path
in the dual graph, but the over/under consistency test must know exactly
which diagram strand each dual edge crosses.

## PD Preprocessing

The command-line tools and high-level Python helpers first simplify the PD
code by removing R1 moves and then nugatory crossings. The C++ implementation
does this in C++; the Python prototype implements the same preprocessing in
Python. Both versions update the explicit crossingless-component count when a
deleted crossing was the last crossing of a component.

The lower-level `find_simplification` function still searches exactly the PD
code it receives. This keeps the mid-simplification search independently
testable while the user-facing tools run the faster default preprocessing
pipeline.

## Final PD Formatting

The simplification algorithms keep their internal crossing order and label
numbering unchanged while searching and applying moves. At the final output
boundary, `format_final_pd_code` converts the resulting diagram to a display
form:

- the existing component-orientation pass identifies the incoming endpoint of
  each under strand;
- each crossing tuple is rotated so entry `0` is that under-incoming endpoint
  and entries `1`, `2`, and `3` continue around the crossing in the same local
  order used by the rest of the library;
- labels are then renumbered by walking the directed components, processing
  components in increasing old-label order and assigning labels from `1`.
- crossing rows are sorted lexicographically after relabeling, matching the
  stable order used by diagram sanity round trips.

This is only a relabeling and local cyclic reindexing of crossing endpoints.
It does not change the pairing of PD labels, crossing signs, component count,
or the result of the simplification search.

## Red Path Enumeration

The simplification search starts from possible red boundary arcs. For each
crossing entry, the algorithm walks forward with `next` until it returns to
a crossing already seen. This gives a component arc with no repeated
crossing except the closing crossing.

Every prefix long enough to bound a nontrivial disk is considered a red
candidate. For a red path with `n` endpoints, the algorithm searches for a
shorter green path between the faces adjacent to the red path endpoints.

Interior red edges are assigned a large dual-graph weight. This prevents a
green path from simply crossing the red boundary through its interior. The
remaining dual graph is searched for simple paths with total weight less
than the red path length.

If the source and target endpoint regions are the same face, the green path
is represented by the single-face path `[face]`. This is a valid zero-crossing
dual path: the green arc stays inside one complementary region, including the
unbounded exterior region when that is the shared face.

When `max_paths` is not `-1`, the implementation uses the bounded depth-first
collector. When `max_paths` is `-1`, the default collector is the shared
C++/Python deterministic heuristic described in
[Heuristic Path Sampling](heuristic-path-sampling.md). Passing
`--ban-heuristic` with `max_paths=-1` restores exhaustive simple-path
enumeration for the current red path. The exhaustive collector is still exact,
but it prunes branches whose current weight plus the shortest possible
remaining dual-graph distance is already too large to beat the red path.

## Green Path Validation

A short green path in the dual graph is only a topological candidate. It
still has to be compatible with the crossing information of the diagram.

For each candidate green path, the checker runs twice: once treating the red
path as a left boundary and once treating it as a right boundary.

The checker propagates required strand levels from the red boundary through
the disk:

- even strand indices are treated as under strands;
- odd strand indices are treated as over strands;
- opposite endpoints of the same diagram edge must have the same level;
- local crossing constraints forbid a strand from being forced both over
  and under;
- when the propagation reaches the green path, the green crossing receives
  the complementary level.

If propagation completes with no contradiction, the red and green paths
bound a valid simplifying disk. The result records the red path, the green
path, the side used, and the green crossing data.
If the same endpoint and level are reached twice during one propagation trace
before hitting the red boundary or green path, the candidate is rejected. Such
a repeated state is a closed propagation orbit, and rejecting it keeps the
validator finite without accepting an uncertified witness.

## Applying A Witness

The high-level simplifier does not stop at the witness. It applies the
witness to produce a new PD code:

- crossings on the deleted red arc are removed;
- the non-red strand through each removed crossing is smoothed;
- every edge crossed by the green path is split;
- new crossings are inserted along the green path with the over/under levels
  computed by the validator;
- the resulting half-edge pairing is checked so every active PD label has
  exactly two ends, then labels are renumbered deterministically.

After applying one witness, the implementation immediately runs the same R1
and nugatory preprocessing again. This can expose additional local
simplifications before the next mid-simplification search round.

`--reduction-round K` caps the number of applied mid-simplification rounds.
After every operation that produces a new PD code, including an applied
mid-simplification witness and each R1/nugatory deletion, the implementation
immediately rebuilds the internal state from the canonical output form. This
canonicalization relabels each component from 1, sorts crossings, and rotates
each crossing so the displayed row starts at the under-incoming strand. It is
not a topological move; it only prevents later searches from depending on a
stale internal row order, edge numbering, or crossing orientation.

The default `--reduction-round -1` repeats until no applicable witness remains.
In default heuristic mode, if the heuristic cannot find a witness, the
simplifier runs a brute-force pass from the already-canonical current diagram.
If brute force finds a witness, that witness is applied, canonicalized, and the
loop continues in heuristic mode. The diagram is reported as final only when
the brute-force pass also fails to find a witness.

## Component Accounting

Plain PD codes cannot represent components with no crossings. This matters
when a move removes the last crossing from a connected component: if the
component is simply dropped from the PD code, the link information is lost.

The library therefore tracks component metadata separately:

- `analyze_components` reports components represented by crossings plus an
  explicit count of already crossingless components;
- `analyze_components_after_removing_crossings` simulates crossing deletion
  and increments `crossingless_components` for each component that loses all
  crossing indices;
- `simplify_pd_code` preserves this count while removing R1 moves and
  nugatory crossings before the mid-simplification search.

This makes deletion-safe simplification possible even when the resulting PD
code is empty.

## Correctness Argument

The implementation preserves the combinatorial diagram because every
endpoint operation is derived from PD label pairing and crossing-local
indices. The face enumeration is correct because `next_corner` follows the
boundary of one complementary region, and every endpoint belongs to exactly
one such region. Therefore the dual graph has exactly one vertex per face
and one edge per diagram edge separating two faces.

The red path enumeration is complete for the class of simplifications
targeted by the algorithm: every candidate disk boundary contains a red arc
following the diagram from one crossing entry to another without repeating
an interior crossing. Walking forward from every crossing entry and taking
all long enough prefixes includes each such red arc.

For a fixed red path, any valid simplifying disk must have its other
boundary arc in the complement of the red interior. Assigning large weights
to interior red dual edges excludes paths that cross through that boundary.
In brute-force mode, the simple-path search over the dual graph therefore
enumerates exactly the eligible green arcs; shortest-distance pruning only
removes branches that cannot possibly satisfy the strict weight cutoff. In
bounded and heuristic modes, the search is intentionally incomplete, but every
candidate that reaches the validator is checked by the same
crossing-consistency rules.

The validation step is sound because it checks the local crossing
constraints induced by the disk. A contradiction means some strand would be
forced to be both over and under, or two ends of the same diagram edge would
receive inconsistent levels. If no contradiction is found, all strands met
by the disk boundary admit a consistent over/under assignment, so the
reported red and green paths describe a valid simplifying witness.

The application step is sound at the PD-code level because it rewires the
diagram only along the certified disk boundary. The implementation rejects a
witness if the green path would cross a deleted red-strand half-edge, if a
crossed PD label would be split twice, or if the reconstructed active
half-edge graph cannot be paired into valid PD labels. The final renumbering
changes only labels, not the underlying diagram.

Heuristic mode does not change this soundness argument because it only changes
candidate ordering and sampling. It can miss a witness; it cannot make an
unvalidated witness valid. Use `--ban-heuristic --max-paths -1` for complete
green-path enumeration on inputs where that cost is acceptable.

The component accounting is correct because a component is represented by
the set of crossings visited while walking `next` along that component.
After deleting a crossing set, a component has no crossing-bearing
representative exactly when all of its crossing indices were removed. The
analysis increments the explicit crossingless count in precisely that case,
so the total number of link components is not lost.

## Randomized Stress Tests

The test suite includes deterministic randomized tests for trefoil,
figure-eight, and cinquefoil fixtures. For each fixture, the test generator
applies inverse Reidemeister I moves to increase the crossing count without
changing the link type. The default preprocessing stage must then reduce the
diagram back to the original crossing count by removing R1 moves and any
nugatory crossings it exposes.

These tests do not prove minimality for arbitrary input. They do verify that
the implementation can survive nontrivial random diagram growth while
preserving component counts and removing the artificial crossings it created.
