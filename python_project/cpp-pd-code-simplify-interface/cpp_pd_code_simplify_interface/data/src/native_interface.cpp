#include "pdcode_simplify/pdcode_simplify.hpp"

#include <cstdlib>
#include <cstring>
#include <exception>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace {

bool denotes_crossingless_unknot(const std::string& text) {
    std::string compact;
    for (char c : text) {
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') {
            compact.push_back(c);
        }
    }
    return compact == "PD[]" || compact == "[]";
}

std::string json_escape(const std::string& text) {
    std::ostringstream escaped;
    for (char c : text) {
        switch (c) {
            case '\\':
                escaped << "\\\\";
                break;
            case '"':
                escaped << "\\\"";
                break;
            case '\n':
                escaped << "\\n";
                break;
            case '\r':
                escaped << "\\r";
                break;
            case '\t':
                escaped << "\\t";
                break;
            default:
                escaped << c;
                break;
        }
    }
    return escaped.str();
}

void append_component_counts(
    std::ostringstream& out,
    const pdcode_simplify::ComponentAnalysis& analysis) {
    out << "\"components_with_crossings\":" << analysis.components_with_crossings()
        << ",\"crossingless_components\":" << analysis.crossingless_components
        << ",\"total_components\":" << analysis.total_components();
}

std::string result_to_json(
    const pdcode_simplify::ReductionResult& result,
    const pdcode_simplify::ComponentAnalysis& input_components,
    const pdcode_simplify::ComponentAnalysis& final_components,
    const pdcode_simplify::ComponentAnalysis* after_removal_components) {
    std::ostringstream out;
    out << "{";
    out << "\"simplification_found\":"
        << (result.mid_simplification_rounds > 0 ? "true" : "false") << ",";
    out << "\"input_components\":{";
    append_component_counts(out, input_components);
    out << "},";
    if (after_removal_components != nullptr) {
        out << "\"after_removal_components\":{";
        append_component_counts(out, *after_removal_components);
        out << "},";
    }
    out << "\"final_pd_code\":\"" << json_escape(pdcode_simplify::format_pd_code(result.code))
        << "\",";
    out << "\"final_crossings\":" << result.code.size() << ",";
    out << "\"final_components\":{";
    append_component_counts(out, final_components);
    out << "},";
    out << "\"mid_simplification_rounds\":" << result.mid_simplification_rounds << ",";
    out << "\"heuristic_failover_rounds\":" << result.heuristic_failover_rounds << ",";
    out << "\"reidemeister_i_moves\":" << result.reidemeister_i_moves << ",";
    out << "\"nugatory_crossing_moves\":" << result.nugatory_crossing_moves << ",";
    out << "\"tested_red_paths\":" << result.tested_red_paths << ",";
    out << "\"tested_green_paths\":" << result.tested_green_paths << ",";
    out << "\"last_path_search_mode\":\"" << json_escape(result.last_path_search_mode) << "\",";
    out << "\"stopped_by_round_limit\":"
        << (result.stopped_by_round_limit ? "true" : "false");
    out << "}";
    return out.str();
}

char* copy_string(const std::string& text) {
    char* result = static_cast<char*>(std::malloc(text.size() + 1));
    if (result == nullptr) {
        return nullptr;
    }
    std::memcpy(result, text.c_str(), text.size() + 1);
    return result;
}

}  // namespace

extern "C" {

#if defined(_WIN32)
__declspec(dllexport)
#endif
char* pdcode_simplify_run_json(
    const char* pd_text,
    int max_paths,
    int ban_heuristic,
    int reduction_round,
    int verbose,
    unsigned long long known_crossingless_components,
    const int* removed_crossings,
    unsigned long long removed_crossing_count) {
    try {
        if (pd_text == nullptr) {
            return copy_string("{\"error\":\"pd_text must not be null\"}");
        }

        const std::string text(pd_text);
        pdcode_simplify::SimplifierOptions options;
        options.max_paths = max_paths;
        options.ban_heuristic = ban_heuristic != 0;
        options.verbose = verbose != 0;
        options.progress = [](const std::string& message) {
            std::cerr << "[pdcode-simplify] " << message << '\n';
        };

        const pdcode_simplify::PDCode code = pdcode_simplify::parse_pd_code(text);
        std::size_t crossingless = static_cast<std::size_t>(known_crossingless_components);
        if (denotes_crossingless_unknot(text)) {
            ++crossingless;
        }

        const auto input_components = pdcode_simplify::analyze_components(code, crossingless);
        pdcode_simplify::ComponentAnalysis after_removal_components;
        const bool has_removal = removed_crossings != nullptr && removed_crossing_count > 0;
        if (has_removal) {
            std::vector<int> removed(
                removed_crossings,
                removed_crossings + static_cast<std::size_t>(removed_crossing_count));
            after_removal_components =
                pdcode_simplify::analyze_components_after_removing_crossings(
                    code, removed, crossingless);
        }

        const auto result =
            pdcode_simplify::reduce_pd_code(code, crossingless, options, reduction_round);
        const auto final_components =
            pdcode_simplify::analyze_components(result.code, result.crossingless_components);
        return copy_string(result_to_json(
            result,
            input_components,
            final_components,
            has_removal ? &after_removal_components : nullptr));
    } catch (const std::exception& error) {
        return copy_string(std::string("{\"error\":\"") + json_escape(error.what()) + "\"}");
    } catch (...) {
        return copy_string("{\"error\":\"unknown C++ exception\"}");
    }
}

#if defined(_WIN32)
__declspec(dllexport)
#endif
void pdcode_simplify_free_string(char* text) {
    std::free(text);
}

}  // extern "C"
