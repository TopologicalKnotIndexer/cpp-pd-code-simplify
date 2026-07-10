#include "pdcode_simplify/pdcode_simplify.hpp"

#include <cstdlib>
#include <cstring>
#include <ctime>
#include <exception>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

namespace {

class TeeStreamBuffer : public std::streambuf {
public:
    TeeStreamBuffer(std::streambuf* primary, std::streambuf* backup, std::mutex& mutex)
        : primary_(primary), backup_(backup), mutex_(mutex) {}

protected:
    int overflow(int ch) override {
        if (ch == traits_type::eof()) {
            return sync() == 0 ? traits_type::not_eof(ch) : traits_type::eof();
        }
        std::lock_guard<std::mutex> lock(mutex_);
        const char c = static_cast<char>(ch);
        const bool ok_primary = primary_->sputc(c) != traits_type::eof();
        const bool ok_backup = backup_->sputc(c) != traits_type::eof();
        backup_->pubsync();
        return ok_primary && ok_backup ? ch : traits_type::eof();
    }

    std::streamsize xsputn(const char* text, std::streamsize count) override {
        std::lock_guard<std::mutex> lock(mutex_);
        const std::streamsize primary_count = primary_->sputn(text, count);
        const std::streamsize backup_count = backup_->sputn(text, count);
        backup_->pubsync();
        return primary_count == count && backup_count == count ? count : 0;
    }

    int sync() override {
        std::lock_guard<std::mutex> lock(mutex_);
        const int primary_status = primary_->pubsync();
        const int backup_status = backup_->pubsync();
        return primary_status == 0 && backup_status == 0 ? 0 : -1;
    }

private:
    std::streambuf* primary_;
    std::streambuf* backup_;
    std::mutex& mutex_;
};

class ScopedStreamRedirect {
public:
    ScopedStreamRedirect(std::ostream& stream, std::streambuf* replacement)
        : stream_(stream), original_(stream.rdbuf(replacement)) {}

    ~ScopedStreamRedirect() {
        stream_.rdbuf(original_);
    }

private:
    std::ostream& stream_;
    std::streambuf* original_;
};

std::string local_timestamp() {
    const std::time_t now = std::time(nullptr);
    std::tm local{};
#if defined(_WIN32)
    localtime_s(&local, &now);
#else
    localtime_r(&now, &local);
#endif
    std::ostringstream out;
    out << std::put_time(&local, "%Y-%m-%d %H:%M:%S");
    return out.str();
}

void print_progress_log(const std::string& message) {
    std::cerr << "[pdcode-simplify " << local_timestamp() << "] "
              << message << '\n';
}

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
    const bool simplification_found =
        result.mid_simplification_rounds > 0 ||
        result.reidemeister_i_moves > 0 ||
        result.reidemeister_ii_moves > 0 ||
        result.reidemeister_iii_moves > 0 ||
        result.nugatory_crossing_moves > 0 ||
        result.reapr_used;
    std::ostringstream out;
    out << "{";
    out << "\"simplification_found\":"
        << (simplification_found ? "true" : "false") << ",";
    out << "\"input_components\":{";
    append_component_counts(out, input_components);
    out << "},";
    if (after_removal_components != nullptr) {
        out << "\"after_removal_components\":{";
        append_component_counts(out, *after_removal_components);
        out << "},";
    }
    out << "\"final_pd_code\":\"" << json_escape(pdcode_simplify::format_final_pd_code(result.code))
        << "\",";
    out << "\"final_crossings\":" << result.code.size() << ",";
    out << "\"final_components\":{";
    append_component_counts(out, final_components);
    out << "},";
    out << "\"mid_simplification_rounds\":" << result.mid_simplification_rounds << ",";
    out << "\"heuristic_failover_rounds\":" << result.heuristic_failover_rounds << ",";
    out << "\"reidemeister_i_moves\":" << result.reidemeister_i_moves << ",";
    out << "\"reidemeister_ii_moves\":" << result.reidemeister_ii_moves << ",";
    out << "\"reidemeister_iii_moves\":" << result.reidemeister_iii_moves << ",";
    out << "\"nugatory_crossing_moves\":" << result.nugatory_crossing_moves << ",";
    out << "\"tested_red_paths\":" << result.tested_red_paths << ",";
    out << "\"tested_green_paths\":" << result.tested_green_paths << ",";
    out << "\"last_path_search_mode\":\"" << json_escape(result.last_path_search_mode) << "\",";
    out << "\"reapr_used\":" << (result.reapr_used ? "true" : "false") << ",";
    out << "\"reapr_rounds\":" << result.reapr_rounds << ",";
    out << "\"reapr_attempts\":" << result.reapr_attempts << ",";
    out << "\"reapr_rejected\":" << (result.reapr_rejected ? "true" : "false") << ",";
    out << "\"reapr_status\":\"" << json_escape(result.reapr_status) << "\",";
    out << "\"reapr_warning\":\"" << json_escape(result.reapr_warning) << "\",";
    out << "\"alexander_determinant_before\":\""
        << json_escape(result.alexander_determinant_before) << "\",";
    out << "\"alexander_determinant_after\":\""
        << json_escape(result.alexander_determinant_after) << "\",";
    out << "\"reapr_invariants_before\":\""
        << json_escape(result.reapr_invariants_before) << "\",";
    out << "\"reapr_invariants_after\":\""
        << json_escape(result.reapr_invariants_after) << "\",";
    out << "\"stopped_by_round_limit\":"
        << (result.stopped_by_round_limit ? "true" : "false") << ",";
    out << "\"timed_out\":" << (result.timed_out ? "true" : "false") << ",";
    out << "\"resource_limited\":" << (result.resource_limited ? "true" : "false");
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
    int max_thread,
    long long bruteforce_budget,
    int timeout_seconds,
    int verbose,
    int show_step_pd,
    int enable_reapr,
    int reapr_retry_max,
    unsigned long long known_crossingless_components,
    const int* removed_crossings,
    unsigned long long removed_crossing_count,
    const char* log_file_path) {
    try {
        if (pd_text == nullptr) {
            return copy_string("{\"error\":\"pd_text must not be null\"}");
        }

        std::ofstream log_file;
        std::mutex log_mutex;
        std::unique_ptr<TeeStreamBuffer> cout_tee;
        std::unique_ptr<TeeStreamBuffer> cerr_tee;
        std::unique_ptr<ScopedStreamRedirect> cout_redirect;
        std::unique_ptr<ScopedStreamRedirect> cerr_redirect;
        if (log_file_path != nullptr && log_file_path[0] != '\0') {
            log_file.open(log_file_path, std::ios::out | std::ios::app | std::ios::binary);
            if (!log_file) {
                return copy_string(
                    std::string("{\"error\":\"could not open log file: ")
                    + json_escape(log_file_path)
                    + "\"}");
            }
            cout_tee.reset(new TeeStreamBuffer(std::cout.rdbuf(), log_file.rdbuf(), log_mutex));
            cerr_tee.reset(new TeeStreamBuffer(std::cerr.rdbuf(), log_file.rdbuf(), log_mutex));
            cout_redirect.reset(new ScopedStreamRedirect(std::cout, cout_tee.get()));
            cerr_redirect.reset(new ScopedStreamRedirect(std::cerr, cerr_tee.get()));
        }

        const std::string text(pd_text);
        pdcode_simplify::SimplifierOptions options;
        options.max_paths = max_paths;
        options.max_threads = max_thread;
        options.bruteforce_budget = bruteforce_budget;
        options.timeout_seconds = timeout_seconds;
        options.reapr_retry_max = reapr_retry_max;
        options.ban_heuristic = ban_heuristic != 0;
        options.enable_reapr = enable_reapr != 0;
        options.verbose = verbose != 0;
        options.progress = [](const std::string& message) {
            print_progress_log(message);
        };
        if (show_step_pd != 0) {
            options.step_pd_output = [](int round, const pdcode_simplify::PDCode& step_code) {
                std::cout << "step_pd_code[" << round << "]: "
                          << pdcode_simplify::format_final_pd_code(step_code)
                          << '\n';
                std::cout.flush();
            };
        }

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
