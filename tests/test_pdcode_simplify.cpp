#include "pdcode_simplify/pdcode_simplify.hpp"

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

std::string read_text_file(const std::string& path) {
    std::ifstream input(path.c_str());
    if (!input) {
        throw std::runtime_error("Could not open fixture: " + path);
    }
    std::ostringstream buffer;
    buffer << input.rdbuf();
    return buffer.str();
}

void test_parser() {
    const auto code = pdcode_simplify::parse_pd_code("[(0, 1, 2, 3), (2, 3, 0, 1)]");
    require(code.size() == 2, "parser should create two crossings");
    require(code[0][0] == 0 && code[1][3] == 1, "parser should preserve labels");
    require(
        pdcode_simplify::format_pd_code(code) == "PD[X[0,1,2,3],X[2,3,0,1]]",
        "plain formatter should preserve internal labels");
    require(
        pdcode_simplify::format_final_pd_code(code) == "PD[X[1,3,2,4],X[2,4,1,3]]",
        "final formatter should orient crossings and renumber from one");

    const auto cppkh_style = pdcode_simplify::parse_pd_code(
        "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]");
    require(cppkh_style.size() == 3, "parser should accept standard PD[...] input");
    require(cppkh_style[0][0] == 1 && cppkh_style[2][3] == 2,
            "parser should preserve standard PD[...] labels");

    const auto orientation_repair = pdcode_simplify::parse_pd_code(
        "PD[X[1,6,2,7],X[9,4,10,5],X[8,1,7,10],X[6,3,5,2],X[4,9,3,8]]");
    require(
        pdcode_simplify::format_final_pd_code(orientation_repair) ==
            "PD[X[1,6,2,7],X[3,8,4,9],X[5,2,6,3],X[7,10,8,1],X[9,4,10,5]]",
        "final formatter should repair local crossing orientation and sort rows");
}

void test_empty_code() {
    const auto result = pdcode_simplify::find_simplification({});
    require(!result.found, "empty PD code should not have a simplification witness");
}

void test_invalid_code() {
    bool threw = false;
    try {
        const auto code = pdcode_simplify::parse_pd_code("[(0, 1, 2, 3)]");
        (void)pdcode_simplify::find_simplification(code);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    require(threw, "labels that do not appear twice should be rejected");
}

void test_common_knot_components() {
    const auto trefoil = pdcode_simplify::parse_pd_code(
        "[(1,5,2,4),(3,1,4,6),(5,3,6,2)]");
    const auto figure_eight = pdcode_simplify::parse_pd_code(
        "[(8,3,1,4),(2,6,3,5),(6,2,7,1),(4,7,5,8)]");

    const auto trefoil_components = pdcode_simplify::analyze_components(trefoil);
    const auto figure_eight_components = pdcode_simplify::analyze_components(figure_eight);

    require(trefoil_components.total_components() == 1, "trefoil should have one component");
    require(figure_eight_components.total_components() == 1, "figure-eight knot should have one component");

    pdcode_simplify::SimplifierOptions options;
    (void)pdcode_simplify::find_simplification(trefoil, options);
    (void)pdcode_simplify::find_simplification(figure_eight, options);
}

void test_link_components() {
    const auto two_component_link = pdcode_simplify::parse_pd_code(
        "[(4,0,5,3),(0,6,1,5),(6,2,7,1),(2,4,3,7)]");
    const auto components = pdcode_simplify::analyze_components(two_component_link);

    require(components.total_components() == 2, "sample link should have two components");
    require(components.components_with_crossings() == 2, "both sample link components should have crossings");
}

void test_verbose_auto_thread_log() {
    const auto trefoil = pdcode_simplify::parse_pd_code(
        "[(1,5,2,4),(3,1,4,6),(5,3,6,2)]");
    std::string log;
    pdcode_simplify::SimplifierOptions options;
    options.max_paths = -1;
    options.ban_heuristic = true;
    options.max_threads = -1;
    options.verbose = true;
    options.progress = [&](const std::string& message) {
        log += message;
        log += '\n';
    };

    (void)pdcode_simplify::reduce_pd_code(trefoil, 0, options, 1);
    require(
        log.find("bruteforce_threads max_thread=-1") != std::string::npos,
        "verbose auto-thread log should be emitted in brute-force mode");
    require(
        log.find("actual_threads=") != std::string::npos,
        "verbose auto-thread log should include the actual worker count");
}

void test_finite_round_uses_brute_fallback() {
    const auto trefoil = pdcode_simplify::parse_pd_code(
        "[(1,5,2,4),(3,1,4,6),(5,3,6,2)]");
    std::string log;
    pdcode_simplify::SimplifierOptions options;
    options.max_paths = -1;
    options.max_threads = 1;
    options.verbose = true;
    options.progress = [&](const std::string& message) {
        log += message;
        log += '\n';
    };

    const auto result = pdcode_simplify::reduce_pd_code(trefoil, 0, options, 1);
    require(result.mid_simplification_rounds == 0,
            "stable finite-round fixture should not apply a witness");
    require(
        log.find("brute_fallback_start") != std::string::npos,
        "finite reduction rounds should still use brute fallback before stopping");
}

void test_timeout_deadline() {
    const auto trefoil = pdcode_simplify::parse_pd_code(
        "[(1,5,2,4),(3,1,4,6),(5,3,6,2)]");
    pdcode_simplify::SimplifierOptions options;
    options.timeout_seconds = 1;
    options.has_timeout_deadline = true;
    options.timeout_deadline =
        std::chrono::steady_clock::now() - std::chrono::seconds(1);

    const auto result = pdcode_simplify::reduce_pd_code(trefoil, 0, options, -1);
    require(result.timed_out, "expired timeout deadline should return a timed-out result");
    require(result.code.size() == trefoil.size(), "timed-out result should keep the current best PD code");
}

void test_bruteforce_budget_limit() {
    const auto trefoil = pdcode_simplify::parse_pd_code(
        "[(1,5,2,4),(3,1,4,6),(5,3,6,2)]");
    pdcode_simplify::SimplifierOptions options;
    options.max_paths = -1;
    options.ban_heuristic = true;
    options.max_threads = 1;
    options.bruteforce_budget = 1;

    const auto result = pdcode_simplify::reduce_pd_code(trefoil, 0, options, -1);
    require(result.resource_limited,
            "brute-force budget exhaustion should be reported");
    require(!result.timed_out,
            "brute-force budget exhaustion should not be reported as a timeout");
    require(result.tested_green_paths == 1,
            "brute-force budget should cap checked green paths");
    require(result.code.size() == trefoil.size(),
            "resource-limited result should keep the current best PD code");
}

void test_crossingless_component_count_after_removal() {
    const auto trefoil = pdcode_simplify::parse_pd_code(
        "[(1,5,2,4),(3,1,4,6),(5,3,6,2)]");
    const auto removed_trefoil = pdcode_simplify::analyze_components_after_removing_crossings(
        trefoil, {0, 1, 2});

    require(removed_trefoil.components_with_crossings() == 0,
            "removing all trefoil crossings should leave no crossing-bearing components");
    require(removed_trefoil.crossingless_components == 1,
            "removing all trefoil crossings should preserve one crossingless component");
    require(removed_trefoil.total_components() == 1,
            "removal should preserve the total component count");

    const auto two_component_link = pdcode_simplify::parse_pd_code(
        "[(4,0,5,3),(0,6,1,5),(6,2,7,1),(2,4,3,7)]");
    const auto original = pdcode_simplify::analyze_components(two_component_link);
    require(original.total_components() == 2, "sample link should start with two components");

    const auto removed_one_component = pdcode_simplify::analyze_components_after_removing_crossings(
        two_component_link, original.components.front().crossing_indices);
    require(removed_one_component.components_with_crossings() == 0,
            "shared inter-component crossings can make both components crossingless");
    require(removed_one_component.crossingless_components == 2,
            "all components that lose crossings should be counted as crossingless");
    require(removed_one_component.total_components() == 2,
            "component metadata should not be lost during deletion simulation");

    const auto empty_unknot = pdcode_simplify::analyze_components({}, 1);
    require(empty_unknot.components_with_crossings() == 0,
            "an explicitly tracked empty unknot has no crossing-bearing component");
    require(empty_unknot.crossingless_components == 1,
            "an explicitly tracked empty unknot should be counted");

    const auto kink = pdcode_simplify::parse_pd_code("[(0,0,1,1)]");
    const auto simplified_kink = pdcode_simplify::simplify_pd_code(kink);
    require(simplified_kink.code.empty(),
            "a one-crossing kink should simplify to an empty PD code");
    require(simplified_kink.crossingless_components == 1,
            "a removed one-crossing kink should leave one crossingless component");

    const auto pd_simplified_kink = pdcode_simplify::simplify_pd_code(kink);
    require(pd_simplified_kink.code.empty(),
            "default PD simplification should remove a one-crossing kink");
    require(pd_simplified_kink.reidemeister_i_moves == 1,
            "default PD simplification should count one R1 move");
    require(pd_simplified_kink.crossingless_components == 1,
            "default PD simplification should preserve the crossingless kink component");
}

void test_r1_random_inflate_then_pre_simplify() {
    struct Sample {
        const char* name;
        const char* pd;
        std::size_t minimal_crossings;
    };

    const Sample samples[] = {
        {
            "trefoil",
            "[(1,5,2,4),(3,1,4,6),(5,3,6,2)]",
            3,
        },
        {
            "figure-eight",
            "[(8,3,1,4),(2,6,3,5),(6,2,7,1),(4,7,5,8)]",
            4,
        },
        {
            "cinquefoil",
            "[(8,0,1,9),(0,2,3,1),(2,4,5,3),(4,6,7,5),(6,8,9,7)]",
            5,
        },
    };

    for (const Sample& sample : samples) {
        const auto base = pdcode_simplify::parse_pd_code(sample.pd);
        require(base.size() == sample.minimal_crossings,
                std::string(sample.name) + " base crossing count should match the fixture");
        require(pdcode_simplify::analyze_components(base).total_components() == 1,
                std::string(sample.name) + " should be a one-component knot fixture");

        for (unsigned int seed = 1; seed <= 8; ++seed) {
            pdcode_simplify::RandomInflationOptions options;
            options.moves = 18;
            options.seed = seed * 97U + static_cast<unsigned int>(sample.minimal_crossings);
            options.type_ii_percentage = 0;

            const auto inflated = pdcode_simplify::randomly_increase_crossings(base, options);
            require(inflated.code.size() > base.size(),
                    std::string(sample.name) + " should gain crossings during random inflation");
            require(pdcode_simplify::analyze_components(inflated.code).total_components() == 1,
                    std::string(sample.name) + " random inflation should preserve the component count");

            const auto simplified = pdcode_simplify::simplify_pd_code(inflated.code);
            require(simplified.code.size() == base.size(),
                    std::string(sample.name) + " should simplify back to its original crossing count");
            require(simplified.crossingless_components == 0,
                    std::string(sample.name) + " simplification should not invent crossingless components");
            require(simplified.reidemeister_i_moves > 0,
                    std::string(sample.name) + " should use at least one R1 simplification");
        }
    }
}

void test_reference_sample() {
    const auto code = pdcode_simplify::parse_pd_code(
        read_text_file("tests/fixtures/reference_31_pd.txt"));
    pdcode_simplify::SimplifierOptions options;
    const auto result = pdcode_simplify::find_simplification(code, options);
    require(result.found, "reference PD code should have a simplification witness");
    require(!result.red_path.empty(), "witness should include a red path");
    require(!result.green_path.empty(), "witness should include a green path");

    const auto reduced = pdcode_simplify::reduce_pd_code(code);
    require(reduced.code.empty(),
            "full reference reduction should apply same-face green paths and end at no crossings");
    require(reduced.crossingless_components == 1,
            "full reference reduction should preserve the final crossingless component");
    require(reduced.mid_simplification_rounds > 0,
            "full reference reduction should use at least one mid-simplification round");
}

void test_reidemeister_iii_failover_16_crossing() {
    const auto code = pdcode_simplify::parse_pd_code(
        "PD[X[1,24,2,25],X[2,16,3,15],X[4,27,5,28],X[6,29,7,30],"
        "X[8,18,9,17],X[11,21,12,20],X[13,23,14,22],X[16,8,17,7],"
        "X[19,11,20,10],X[21,13,22,12],X[23,32,24,1],X[25,15,26,14],"
        "X[26,3,27,4],X[28,5,29,6],X[30,9,31,10],X[31,18,32,19]]");
    pdcode_simplify::SimplifierOptions options;
    options.max_threads = 16;

    const auto reduced = pdcode_simplify::reduce_pd_code(code, 0, options, -1);
    require(reduced.code.size() == 14,
            "RIII failover regression should reduce the 16-crossing sample to 14 crossings");
    require(reduced.reidemeister_iii_moves == 4,
            "RIII failover regression should use four deterministic RIII moves");
    require(reduced.reidemeister_ii_moves == 1,
            "RIII failover regression should expose and erase one RII bigon");
    require(pdcode_simplify::format_final_pd_code(reduced.code) ==
                "PD[X[1,22,2,23],X[3,24,4,25],X[5,26,6,27],"
                "X[8,16,9,15],X[10,18,11,17],X[12,20,13,19],"
                "X[14,8,15,7],X[16,10,17,9],X[18,12,19,11],"
                "X[20,14,21,13],X[21,28,22,1],X[23,2,24,3],"
                "X[25,4,26,5],X[27,6,28,7]]",
            "RIII failover regression should be deterministic");
}

void test_same_face_green_path_unknot() {
    const auto code = pdcode_simplify::parse_pd_code(
        "PD[X[1,5,2,4],X[2,5,3,6],X[6,3,1,4]]");
    pdcode_simplify::SimplifierOptions options;
    options.max_threads = 1;

    const auto witness = pdcode_simplify::find_simplification(code, options);
    require(witness.found, "same-face green path should be accepted as a simplification witness");

    const auto reduced = pdcode_simplify::reduce_pd_code(code, 0, options, -1);
    require(reduced.code.empty(), "same-face green path unknot should reduce to empty PD code");
    require(reduced.crossingless_components == 1,
            "same-face green path unknot should preserve the crossingless component");
}

void test_canonicalize_after_each_reduction() {
    const auto code = pdcode_simplify::parse_pd_code(
        "[[3,88,4,1],[4,2,5,1],[5,2,6,3],[9,7,10,6],"
        "[10,7,11,8],[11,9,12,8],[15,12,16,13],[16,14,17,13],"
        "[17,14,18,15],[21,19,22,18],[22,25,23,26],[23,20,24,21],"
        "[24,20,25,19],[28,31,29,32],[32,27,33,28],[33,27,34,26],"
        "[34,29,35,30],[35,31,36,30],[36,39,37,40],[37,41,38,40],"
        "[38,41,39,42],[55,53,56,52],[56,53,57,54],[57,55,58,54],"
        "[61,50,62,51],[62,50,63,49],[64,47,65,48],[66,46,67,45],"
        "[68,64,69,63],[69,48,70,49],[70,65,71,66],[71,47,72,46],"
        "[72,68,73,67],[73,61,74,60],[74,59,75,60],[75,59,76,58],"
        "[76,51,77,52],[79,43,80,42],[81,44,82,45],[83,79,84,78],"
        "[84,77,85,78],[85,82,86,83],[86,44,87,43],[87,81,88,80]]");
    pdcode_simplify::SimplifierOptions options;
    options.max_threads = 16;

    const auto reduced = pdcode_simplify::reduce_pd_code(code, 0, options, -1);
    require(reduced.code.empty(),
            "per-step canonicalization should let the 44-crossing unknot reduce to PD[]");
    require(reduced.crossingless_components == 1,
            "per-step canonicalization regression should preserve the unknot component");
}

void test_do_check_cycle_respects_timeout() {
    const auto code = pdcode_simplify::parse_pd_code(
        read_text_file("tests/fixtures/do_check_cycle_pd.txt"));
    pdcode_simplify::SimplifierOptions options;
    options.max_threads = 1;
    options.timeout_seconds = 1;
    options.has_timeout_deadline = true;
    options.timeout_deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(1);

    const auto result = pdcode_simplify::reduce_pd_code(code, 0, options, 1);
    require(result.timed_out,
            "cycle-guard regression should time out instead of hanging in do_check");
    require(result.code.size() == code.size(),
            "timed-out cycle-guard regression should keep the current best diagram");
}

void test_step_pd_callback() {
    const auto code = pdcode_simplify::parse_pd_code(
        read_text_file("tests/fixtures/reference_31_pd.txt"));
    std::vector<std::string> steps;
    pdcode_simplify::SimplifierOptions options;
    options.max_threads = 1;
    options.step_pd_output = [&](int round, const pdcode_simplify::PDCode& step_code) {
        steps.push_back(
            std::to_string(round) + ":" + pdcode_simplify::format_final_pd_code(step_code));
    };

    const auto reduced = pdcode_simplify::reduce_pd_code(code, 0, options, 1);
    require(reduced.mid_simplification_rounds == 1,
            "step callback fixture should apply one witness before RII pre-simplification");
    require(steps.size() == 1, "step callback should run once per applied witness");
    require(steps.front().find("1:PD[") == 0,
            "step callback should receive a canonical PD code after witness application");
}

void test_reapr_pd_k0_fixture_rejects_unsafe_projection() {
    const auto code = pdcode_simplify::parse_pd_code(
        read_text_file("tests/fixtures/pd_k0.txt"));
    require(code.size() == 481,
            "pd_k0 regression fixture should start with 481 crossings");

    pdcode_simplify::SimplifierOptions options;
    options.enable_reapr = true;
    options.max_threads = 1;
    options.reapr_retry_max = 1;
    const auto reduced = pdcode_simplify::reduce_pd_code(code, 0, options, 0);

    require(!reduced.reapr_used,
            "conservative REAPR guards should not accept the unsafe pd_k0 projection template");
    require(reduced.reapr_attempts == 1,
            "conservative REAPR test should only exercise the projection template");
    require(reduced.reapr_rejected,
            "conservative REAPR guards should report the unsafe pd_k0 candidate as rejected");
    require(reduced.reapr_status == "rejected_overaggressive_projection",
            "conservative REAPR guards should reject pd_k0 because the drop is too large");
    require(reduced.code.size() == code.size(),
            "rejected REAPR candidates should keep the current best PD code");
    require(!reduced.alexander_determinant_before.empty(),
            "REAPR oracle should report the determinant guard before value");
    require(!reduced.alexander_determinant_after.empty(),
            "REAPR oracle should report the determinant guard after value for a rejected candidate");
}

}  // namespace

int main() {
    try {
        test_parser();
        test_empty_code();
        test_invalid_code();
        test_common_knot_components();
        test_link_components();
        test_verbose_auto_thread_log();
        test_finite_round_uses_brute_fallback();
        test_timeout_deadline();
        test_bruteforce_budget_limit();
        test_crossingless_component_count_after_removal();
        test_r1_random_inflate_then_pre_simplify();
        test_reference_sample();
        test_reidemeister_iii_failover_16_crossing();
        test_same_face_green_path_unknot();
        test_canonicalize_after_each_reduction();
        test_do_check_cycle_respects_timeout();
        test_step_pd_callback();
        test_reapr_pd_k0_fixture_rejects_unsafe_projection();
        std::cout << "All tests passed\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& error) {
        std::cerr << "Test failed: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
