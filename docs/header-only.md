# Header-Only C++ Use

The core simplification algorithm is implemented in
`include/pdcode_simplify/pdcode_simplify.hpp`. You can include this header
directly in a C++17 project without compiling or linking a separate
`pdcode_simplify.cpp` object file.

## Minimal Example

```cpp
#include "pdcode_simplify/pdcode_simplify.hpp"

#include <iostream>

int main() {
    auto code = pdcode_simplify::parse_pd_code(
        "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]");

    pdcode_simplify::SimplifierOptions options;
    options.max_threads = -1;

    auto result = pdcode_simplify::reduce_pd_code(code, 0, options, -1);
    std::cout << pdcode_simplify::format_final_pd_code(result.code) << "\n";
    std::cout << result.code.size() << "\n";
}
```

Compile it with any C++17 compiler:

```sh
g++ -std=c++17 -O3 -I/path/to/cpp-pd-code-simplify/include example.cpp -o example
```

On Windows, use a modern 64-bit MinGW-w64/UCRT, Clang, or MSVC-compatible
compiler when your Python or application process is 64-bit. Legacy MinGW.org
toolchains do not provide the threading runtime required by the simplifier.

## Common Calls

Parse and print PD code:

```cpp
auto code = pdcode_simplify::parse_pd_code("PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]");
std::string normalized = pdcode_simplify::format_final_pd_code(code);
```

Run only the default local cleanup:

```cpp
auto cleaned = pdcode_simplify::simplify_pd_code(code);
```

Run full reduction:

```cpp
pdcode_simplify::SimplifierOptions options;
options.max_paths = -1;
options.max_threads = 16;
options.bruteforce_budget = 200000;

auto result = pdcode_simplify::reduce_pd_code(code, 0, options, -1);
```

Track components that are already crossingless:

```cpp
auto result = pdcode_simplify::reduce_pd_code(code, 1, options, -1);
auto final_components =
    pdcode_simplify::analyze_components(result.code, result.crossingless_components);
```

## Options

`SimplifierOptions` is shared by the CLI, the Python interface, and direct
header users:

```cpp
pdcode_simplify::SimplifierOptions options;
options.max_paths = -1;          // heuristic path sampling by default
options.ban_heuristic = false;   // true forces brute-force enumeration
options.max_threads = -1;        // auto-select worker threads
options.timeout_seconds = -1;    // no timeout
options.quit_at_crossing = -1;   // stop at N crossings, or -1 to disable
options.bruteforce_budget = 200000;
options.verbose = true;
options.progress = [](const std::string& message) {
    std::cerr << message << "\n";
};
```

Set `options.enable_reapr = true` only when you want the experimental
invariant-guarded projection oracle. This option can change the knot or link
type. There is no crossing-drop window, so a very small projection may be
accepted when the invariant profile matches. Accepted output sets
`result.reapr_warning` and reports `result.reapr_invariants_before` and
`result.reapr_invariants_after` for independent checking. Set
`options.reapr_retry_max` to control the deterministic retry cap; the default
is `3`.

## Translation Units

All user-facing functions in the header are `inline`, so the header may be
included by multiple translation units in the same program.

`src/pdcode_simplify.cpp` is now only a compatibility shim that includes the
header. New projects should not compile or link it unless an existing build
script already expects that file to exist.

If a tooling step needs declarations only, define
`PDCODE_SIMPLIFY_DECLARATIONS_ONLY` before including the header:

```cpp
#define PDCODE_SIMPLIFY_DECLARATIONS_ONLY
#include "pdcode_simplify/pdcode_simplify.hpp"
```

Normal applications should not define this macro.
