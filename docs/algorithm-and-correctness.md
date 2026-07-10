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
code by repeatedly removing R1 moves, true Reidemeister-II bigons, and
nugatory crossings. A nugatory crossing is not treated as an R2 move: the R2
detector specifically looks for two adjacent crossings joined by the two sides
of a removable bigon, while the nugatory detector removes a single crossing
whose deletion disconnects the crossing graph in the required way. The C++
implementation does this in C++; the Python prototype implements the same
preprocessing in Python. Both versions update the explicit
crossingless-component count when deleted crossings were the last crossings of
a component.

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
ordering. When `max_paths` is `-1`, the default search is the shared
C++/Python deterministic heuristic described in
[Heuristic Path Sampling](heuristic-path-sampling.md). Passing
`--ban-heuristic` with `max_paths=-1` restores exhaustive simple-path
enumeration for the current red path. Exhaustive enumeration is streaming:
each green path is passed to the validator immediately instead of being stored
in one large path list. This is still exact when the brute-force budget is not
exhausted, and it prunes branches whose current weight plus the shortest
possible remaining dual-graph distance is already too large to beat the red
path.

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

After applying one witness, the implementation immediately runs the same R1,
R2, and nugatory preprocessing again. This can expose additional local
simplifications before the next mid-simplification search round.

`--reduction-round K` caps the number of applied mid-simplification rounds.
After every operation that produces a new PD code, including an applied
mid-simplification witness and each local cleanup deletion, the implementation
immediately rebuilds the internal state from the canonical output form. This
canonicalization relabels each component from 1, sorts crossings, and rotates
each crossing so the displayed row starts at the under-incoming strand. It is
not a topological move; it only prevents later searches from depending on a
stale internal row order, edge numbering, or crossing orientation.

The default `--reduction-round -1` repeats until no applicable witness remains.
In default heuristic mode, each round first runs a small deterministic RIII
prepass. If this cheap prepass lowers the crossing count, the main loop starts
over from the new canonical diagram before spending time on red-green path
search. If the prepass cannot reduce the diagram, the heuristic red-green
search runs. When the heuristic cannot find a witness, the simplifier tries the
deterministic non-monotone failover described below. If that failover finds a
temporary sequence whose cleaned result lowers the crossing count, the sequence
is applied, canonicalized, and the loop continues in heuristic mode. If the
non-monotone failover also fails, the simplifier runs a brute-force pass from
the already-canonical current diagram. If brute force finds a witness, that
witness is applied, canonicalized, and the loop continues in heuristic mode. If
brute force also fails, the larger deterministic RIII failover described below
is tried before the diagram is reported as final.

Brute-force search has a separate resource guard, `bruteforce_budget`, exposed
as `--bruteforce-budget` in the CLIs. The default budget is `200000`
green-path checks per PD-code job; `-1` disables the guard. If the budget is
exhausted, the simplifier stops the current job, returns the best PD code known
so far, and sets `resource_limited`. This is a safety result rather than a
stability proof: it means the implementation deliberately stopped before
finishing the brute-force proof attempt.

## Deterministic Non-Monotone Failover

Some diagrams need a short detour before a crossing-decreasing move becomes
visible. The non-monotone failover is a deterministic beam search over
temporary diagrams whose crossing count is allowed to stay the same or rise by
a small fixed amount. It is used only after the normal heuristic red-green
search misses a direct witness and before the terminal brute-force proof pass.

Each beam node stores a canonical PD code, the explicit crossingless-component
count, and the sequence of temporary steps that produced it. Candidate steps
are generated in two ways:

- apply a bounded number of deterministic RIII moves, then immediately run the
  normal R1, R2, and nugatory cleanup;
- sample short green paths with a fixed limited heuristic budget, apply only
  witnesses accepted by the same red-green validator, and immediately run the
  same cleanup.

The candidate queue is bounded by fixed constants shared by C++ and Python:
maximum red length `80`, maximum depth `72`, beam width `32`, at most `96`
candidates per state, at most `4` accepted surgery candidates per red length,
and at most `4,000,000` green-path tests for one non-monotone call. Candidate
red paths are grouped by length, and ties are rotated by a stable FNV-1a hash of
the canonical PD code. This makes the search deterministic while avoiding a
single incidental red-path ordering from dominating every state.

The failover accepts only a state whose cleaned crossing count is strictly
smaller than the starting crossing count. When that happens, every stored step
is replayed as a counted mid-simplification round and every intermediate PD
code is canonicalized. If no such state is found within the fixed budgets, the
algorithm continues to the brute-force proof pass.

The failover is not a completeness proof: bounded beam search can miss a
useful detour. It is sound because every accepted temporary surgery still comes
from the same validated red-green witness application, every RIII candidate is
a standard crossing-preserving local move, and every cleanup uses the ordinary
R1/R2/nugatory deletion checks.

## Deterministic RIII Failover

Some diagrams cannot be reduced by the current red-green witness search from
their present crossing order, even though crossing-preserving Reidemeister-III
moves can expose a later R2 bigon. The 16-crossing regression fixture

```text
PD[X[1,24,2,25],X[2,16,3,15],X[4,27,5,28],X[6,29,7,30],
X[8,18,9,17],X[11,21,12,20],X[13,23,14,22],X[16,8,17,7],
X[19,11,20,10],X[21,13,22,12],X[23,32,24,1],X[25,15,26,14],
X[26,3,27,4],X[28,5,29,6],X[30,9,31,10],X[31,18,32,19]]
```

has exactly this shape: the witness search and brute-force green-path search
find no immediate crossing-decreasing disk, but four RIII moves expose one R2
bigon and reduce it to 14 crossings.

The prepass and failover use the same deterministic RIII engine and are shared
by C++ and Python:

- enumerate triangular faces in the current face decomposition;
- keep only triangles incident to three distinct crossings and with the local
  strand parity pattern required for an RIII move;
- sort candidate RIII moves by crossing index and strand index;
- run a breadth-first search over canonicalized diagrams, bounded by a fixed
  depth and state limit;
- after every RIII move, run the same R1/R2/nugatory preprocessing;
- accept the first canonical state whose crossing count is lower than the
  starting state.

The prepass uses a smaller depth and state budget than the final failover. It
is intended to catch fast reductions such as the 16-to-14 crossing regression
without making every heuristic round pay for the full depth-8 search. The full
failover remains after the brute-force red-green search fails.

No random choice is made. The Python C++ interface calls the same native C++
backend, so it inherits the same move ordering and the same output. If the
failover lowers the crossing count, the main simplification loop starts over
from heuristic witness search on the new canonical PD code.

## Experimental REAPR Oracle

`--reapr` enables an experimental deterministic oracle that is intentionally
outside the strict correctness proof for the default simplifier. It is meant
for hard diagrams where the certified red-green witness search cannot make
progress, and it is disabled by default.

The internal implementation does not call REAPR, Knoodle, SnapPy, or
`pd-code-to-diagram`. It computes its guard invariants directly in C++ and in
the Python prototype. The determinant code is isolated in the C++ namespace
`alexander_determinant_guard`; the stricter REAPR acceptance profile is in
`reapr_invariant_guard`. The Python prototype uses the same matrix
construction, the same finite-field primes, and the same acceptance order.

For a one-component input, the oracle tries a deterministic projection
candidate only when it can make the crossing count smaller:

- determinant `1` proposes the empty unknot projection;
- an odd determinant `d > 1` proposes the canonical `(2,d)` torus-knot
  projection template, but only when `d` is below the current crossing count.

If the first template is rejected, the oracle may continue through a bounded
deterministic retry sequence. Each retry seed generates a small closed-braid
candidate pool using the same pseudo-random integer stream in C++ and Python.
The default cap is three attempts; `--reapr-retry-max N` changes that cap, and
`0` disables REAPR candidate attempts. These retries are deterministic because
the seed for attempt `i` is derived only from `i`, the determinant, and the
current crossing count.

The candidate is canonicalized through the same final PD formatter used by the
rest of the project. It is accepted only if the following profile matches the
original diagram exactly:

- total component count, including crossingless components;
- Alexander determinant fingerprint;
- sorted Goeritz signature pair from the two checkerboard color classes;
- nonzero Alexander roots over `F_11`, `F_19`, and `F_31`.

For efficiency, the implementation first checks component count,
determinant, and Goeritz signature. The three finite-field root sets are
computed only after those faster checks match. Accepted results then run
through the ordinary R1/R2/nugatory cleanup and continue into the normal
reduction loop.

This guard is stronger than the original determinant-only screen, but it is
still not a proof that two knots or links are equivalent. The output therefore
carries `reapr_warning`, `reapr_status`, `alexander_determinant_before`,
`alexander_determinant_after`, `reapr_invariants_before`, and
`reapr_invariants_after`. Users who enable `--reapr` should still verify
independent invariants, for example with Khovanov homology. The project tests
include a `pd_k0.txt` regression fixture where the determinant-preserving
projection template is rejected because the stronger invariant profile changes.
The current retry pool is still not strong enough to simplify that fixture
under the strict guard; it leaves the diagram unchanged rather than accepting
an unsafe candidate.

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
- `simplify_pd_code` preserves this count while removing R1 moves, true R2
  bigons, and nugatory crossings before the mid-simplification search.

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
enumerates exactly the eligible green arcs when the resource budget is not
exhausted; shortest-distance pruning only removes branches that cannot possibly
satisfy the strict weight cutoff. In bounded and heuristic modes, and in a
resource-limited brute-force run, the search is intentionally incomplete, but
every candidate that reaches the validator is checked by the same
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

Heuristic and non-monotone modes do not change this soundness argument because
they only change candidate ordering, sampling, and whether a bounded detour is
searched before the terminal proof pass. They can miss a useful route; they
cannot make an unvalidated witness valid. Use
`--ban-heuristic --max-paths -1` for complete direct green-path enumeration on
inputs where that cost is acceptable.

The RIII failover is sound because each RIII step rewires only the six boundary
arcs of a triangular face according to the standard Reidemeister-III local
move. It preserves the crossing count and link type. The subsequent R1, R2,
and nugatory deletions are local Reidemeister or nugatory simplifications, and
the same half-edge pairing checks used elsewhere reject invalid rewrites.

The experimental `--reapr` oracle is not covered by this soundness argument.
Its invariant guard is a screening check, not an equivalence proof. This is
why the option is opt-in and why accepted output carries an explicit warning.

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
diagram back to the original crossing count by removing R1 moves, R2 bigons,
and any nugatory crossings it exposes.

These tests do not prove minimality for arbitrary input. They do verify that
the implementation can survive nontrivial random diagram growth while
preserving component counts and removing the artificial crossings it created.
