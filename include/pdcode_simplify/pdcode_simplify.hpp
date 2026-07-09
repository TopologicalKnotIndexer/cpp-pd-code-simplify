#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <functional>
#include <iosfwd>
#include <string>
#include <vector>

#if defined(_WIN32) && (defined(PDCODE_SIMPLIFY_SHARED) || defined(PDCODE_SIMPLIFY_BUILD_SHARED))
#if defined(PDCODE_SIMPLIFY_BUILD_SHARED)
#define PDCODE_SIMPLIFY_API __declspec(dllexport)
#else
#define PDCODE_SIMPLIFY_API __declspec(dllimport)
#endif
#elif defined(PDCODE_SIMPLIFY_BUILD_SHARED) && defined(__GNUC__)
#define PDCODE_SIMPLIFY_API __attribute__((visibility("default")))
#else
#define PDCODE_SIMPLIFY_API
#endif

namespace pdcode_simplify {

struct Endpoint {
    int crossing = -1;
    int strand = -1;

    friend bool operator==(const Endpoint& lhs, const Endpoint& rhs) {
        return lhs.crossing == rhs.crossing && lhs.strand == rhs.strand;
    }

    friend bool operator!=(const Endpoint& lhs, const Endpoint& rhs) {
        return !(lhs == rhs);
    }
};

using Crossing = std::array<int, 4>;
using PDCode = std::vector<Crossing>;

enum class Direction {
    Left,
    Right
};

struct GreenCrossing {
    int from_face = -1;
    int to_face = -1;
    std::string strand_level;
};

struct SimplifierOptions {
    int max_paths = -1;
    int max_threads = -1;
    int timeout_seconds = -1;
    bool has_timeout_deadline = false;
    std::chrono::steady_clock::time_point timeout_deadline{};
    bool ban_heuristic = false;
    bool require_applicable = false;
    bool verbose = false;
    std::function<void(const std::string&)> progress;
    std::function<bool()> should_cancel;
};

struct LinkComponentSummary {
    std::vector<int> crossing_indices;
};

struct ComponentAnalysis {
    std::vector<LinkComponentSummary> components;
    std::size_t crossingless_components = 0;

    std::size_t components_with_crossings() const {
        return components.size();
    }

    std::size_t total_components() const {
        return components.size() + crossingless_components;
    }
};

struct RandomInflationOptions {
    int moves = 16;
    unsigned int seed = 1;
    int type_ii_percentage = 50;
};

struct RandomInflationResult {
    PDCode code;
    unsigned int seed = 1;
    int type_i_moves = 0;
    int type_ii_moves = 0;
};

struct PDSimplificationResult {
    PDCode code;
    std::size_t crossingless_components = 0;
    int reidemeister_i_moves = 0;
    int nugatory_crossing_moves = 0;
};

struct SimplificationResult {
    bool found = false;
    Direction direction = Direction::Left;
    std::string path_search_mode;
    std::vector<Endpoint> red_path;
    std::vector<int> green_path;
    std::vector<GreenCrossing> green_crossings;
    std::size_t tested_red_paths = 0;
    std::size_t tested_green_paths = 0;
};

struct MidSimplificationApplyResult {
    PDCode code;
    std::size_t crossingless_components = 0;
};

struct ReductionResult {
    PDCode code;
    std::size_t crossingless_components = 0;
    int mid_simplification_rounds = 0;
    int heuristic_failover_rounds = 0;
    int reidemeister_i_moves = 0;
    int nugatory_crossing_moves = 0;
    std::size_t tested_red_paths = 0;
    std::size_t tested_green_paths = 0;
    std::string last_path_search_mode;
    bool stopped_by_round_limit = false;
    bool timed_out = false;
};

PDCODE_SIMPLIFY_API PDCode parse_pd_code(const std::string& text);
PDCODE_SIMPLIFY_API std::string format_pd_code(const PDCode& code);
PDCODE_SIMPLIFY_API std::string format_final_pd_code(const PDCode& code);
PDCODE_SIMPLIFY_API std::string format_endpoint(const Endpoint& endpoint);
PDCODE_SIMPLIFY_API std::string format_direction(Direction direction);

PDCODE_SIMPLIFY_API ComponentAnalysis analyze_components(
    const PDCode& code,
    std::size_t known_crossingless_components = 0);

PDCODE_SIMPLIFY_API ComponentAnalysis analyze_components_after_removing_crossings(
    const PDCode& code,
    const std::vector<int>& removed_crossings,
    std::size_t known_crossingless_components = 0);

PDCODE_SIMPLIFY_API std::size_t count_crossingless_components_after_removing_crossings(
    const PDCode& code,
    const std::vector<int>& removed_crossings,
    std::size_t known_crossingless_components = 0);

PDCODE_SIMPLIFY_API RandomInflationResult randomly_increase_crossings(
    const PDCode& code,
    const RandomInflationOptions& options = RandomInflationOptions{});

PDCODE_SIMPLIFY_API PDSimplificationResult simplify_pd_code(
    const PDCode& code,
    std::size_t known_crossingless_components = 0);

PDCODE_SIMPLIFY_API SimplificationResult find_simplification(
    const PDCode& code,
    const SimplifierOptions& options = SimplifierOptions{});

PDCODE_SIMPLIFY_API MidSimplificationApplyResult apply_simplification_witness(
    const PDCode& code,
    const SimplificationResult& result,
    std::size_t known_crossingless_components = 0);

PDCODE_SIMPLIFY_API ReductionResult reduce_pd_code(
    const PDCode& code,
    std::size_t known_crossingless_components = 0,
    const SimplifierOptions& options = SimplifierOptions{},
    int reduction_round = -1);

PDCODE_SIMPLIFY_API std::ostream& operator<<(std::ostream& out, const Endpoint& endpoint);

}  // namespace pdcode_simplify
