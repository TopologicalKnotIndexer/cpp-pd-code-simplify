#include "pdcode_simplify/pdcode_simplify.hpp"

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

void test_parser() {
    const auto code = pdcode_simplify::parse_pd_code("[(0, 1, 2, 3), (2, 3, 0, 1)]");
    require(code.size() == 2, "parser should create two crossings");
    require(code[0][0] == 0 && code[1][3] == 1, "parser should preserve labels");

    const auto cppkh_style = pdcode_simplify::parse_pd_code(
        "PD[X[1,5,2,4],X[3,1,4,6],X[5,3,6,2]]");
    require(cppkh_style.size() == 3, "parser should accept standard PD[...] input");
    require(cppkh_style[0][0] == 1 && cppkh_style[2][3] == 2,
            "parser should preserve standard PD[...] labels");
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
    const auto simplified_kink = pdcode_simplify::simplify_reidemeister_i_ii(kink);
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

void test_reidemeister_random_inflate_then_simplify() {
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
            options.type_ii_percentage = 60;

            const auto inflated = pdcode_simplify::randomly_increase_crossings(base, options);
            require(inflated.code.size() > base.size(),
                    std::string(sample.name) + " should gain crossings during random inflation");
            require(pdcode_simplify::analyze_components(inflated.code).total_components() == 1,
                    std::string(sample.name) + " random inflation should preserve the component count");

            const auto simplified = pdcode_simplify::simplify_reidemeister_i_ii(inflated.code);
            require(simplified.code.size() == base.size(),
                    std::string(sample.name) + " should simplify back to its original crossing count");
            require(simplified.crossingless_components == 0,
                    std::string(sample.name) + " simplification should not invent crossingless components");
            require(simplified.type_i_moves + simplified.type_ii_moves > 0,
                    std::string(sample.name) + " should use at least one Reidemeister simplification");
        }
    }
}

void test_reference_sample() {
    const char* sample = R"PD(
[(15, 7, 16, 6),
 (7, 15, 8, 14),
 (18, 61, 19, 0),
 (20, 12, 21, 11),
 (12, 24, 13, 23),
 (13, 26, 14, 27),
 (29, 22, 30, 23),
 (21, 30, 22, 31),
 (28, 33, 29, 34),
 (5, 36, 6, 37),
 (8, 36, 9, 35),
 (34, 27, 35, 28),
 (1, 41, 2, 40),
 (19, 43, 20, 42),
 (43, 25, 44, 24),
 (25, 45, 26, 44),
 (16, 45, 17, 46),
 (37, 46, 38, 47),
 (48, 39, 49, 40),
 (0, 50, 1, 49),
 (10, 51, 11, 52),
 (31, 53, 32, 52),
 (41, 50, 42, 51),
 (55, 3, 56, 2),
 (54, 9, 55, 10),
 (53, 33, 54, 32),
 (3, 57, 4, 56),
 (57, 5, 58, 4),
 (60, 17, 61, 18),
 (59, 38, 60, 39),
 (58, 47, 59, 48)]
)PD";
    const auto code = pdcode_simplify::parse_pd_code(sample);
    pdcode_simplify::SimplifierOptions options;
    const auto result = pdcode_simplify::find_simplification(code, options);
    require(result.found, "reference PD code should have a simplification witness");
    require(!result.red_path.empty(), "witness should include a red path");
    require(!result.green_path.empty(), "witness should include a green path");

    const auto reduced = pdcode_simplify::reduce_pd_code(code);
    require(reduced.code.size() == 4,
            "full reference reduction should apply witnesses and end at four crossings");
    require(reduced.mid_simplification_rounds > 0,
            "full reference reduction should use at least one mid-simplification round");
}

}  // namespace

int main() {
    try {
        test_parser();
        test_empty_code();
        test_invalid_code();
        test_common_knot_components();
        test_link_components();
        test_crossingless_component_count_after_removal();
        test_reidemeister_random_inflate_then_simplify();
        test_reference_sample();
        std::cout << "All tests passed\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& error) {
        std::cerr << "Test failed: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
