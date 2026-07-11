#include "pdcode_simplify/pdcode_simplify.hpp"

#include <cctype>
#include <cstdio>
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

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <io.h>
#else
#include <sys/stat.h>
#include <unistd.h>
#endif

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
        std::string backup_text;
        append_backup_char(c, backup_text);
        const bool ok_backup =
            backup_text.empty() ||
            backup_->sputn(backup_text.data(), static_cast<std::streamsize>(backup_text.size())) ==
                static_cast<std::streamsize>(backup_text.size());
        backup_->pubsync();
        return ok_primary && ok_backup ? ch : traits_type::eof();
    }

    std::streamsize xsputn(const char* text, std::streamsize count) override {
        std::lock_guard<std::mutex> lock(mutex_);
        const std::streamsize primary_count = primary_->sputn(text, count);
        std::string backup_text;
        backup_text.reserve(static_cast<std::size_t>(count));
        for (std::streamsize i = 0; i < count; ++i) {
            append_backup_char(text[i], backup_text);
        }
        const std::streamsize backup_count =
            backup_text.empty()
                ? static_cast<std::streamsize>(0)
                : backup_->sputn(
                      backup_text.data(),
                      static_cast<std::streamsize>(backup_text.size()));
        backup_->pubsync();
        return primary_count == count &&
                       (backup_text.empty() ||
                        backup_count == static_cast<std::streamsize>(backup_text.size()))
                   ? count
                   : 0;
    }

    int sync() override {
        std::lock_guard<std::mutex> lock(mutex_);
        const int primary_status = primary_->pubsync();
        const int backup_status = backup_->pubsync();
        return primary_status == 0 && backup_status == 0 ? 0 : -1;
    }

private:
    enum class AnsiState {
        Plain,
        Escape,
        Csi,
    };

    void append_backup_char(char c, std::string& output) {
        const unsigned char value = static_cast<unsigned char>(c);
        if (ansi_state_ == AnsiState::Escape) {
            ansi_state_ = c == '[' ? AnsiState::Csi : AnsiState::Plain;
            return;
        }
        if (ansi_state_ == AnsiState::Csi) {
            if (value >= 0x40 && value <= 0x7e) {
                ansi_state_ = AnsiState::Plain;
            }
            return;
        }
        if (c == '\x1b') {
            ansi_state_ = AnsiState::Escape;
            return;
        }
        output.push_back(c);
    }

    std::streambuf* primary_;
    std::streambuf* backup_;
    std::mutex& mutex_;
    AnsiState ansi_state_ = AnsiState::Plain;
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

enum class StderrTargetKind {
    Terminal,
    Pipe,
    File,
    Character,
    Other,
    Unknown,
};

bool stdout_is_terminal() {
#if defined(_WIN32)
    const intptr_t fd = _fileno(stdout);
    if (fd >= 0 && _isatty(static_cast<int>(fd))) {
        return true;
    }
    const HANDLE handle = GetStdHandle(STD_OUTPUT_HANDLE);
    if (handle == nullptr || handle == INVALID_HANDLE_VALUE) {
        return false;
    }
    DWORD mode = 0;
    return GetConsoleMode(handle, &mode) != 0;
#else
    return isatty(STDOUT_FILENO) != 0;
#endif
}

StderrTargetKind stderr_target_kind() {
#if defined(_WIN32)
    const intptr_t fd = _fileno(stderr);
    if (fd >= 0 && _isatty(static_cast<int>(fd))) {
        return StderrTargetKind::Terminal;
    }
    const HANDLE handle = GetStdHandle(STD_ERROR_HANDLE);
    if (handle == nullptr || handle == INVALID_HANDLE_VALUE) {
        return StderrTargetKind::Unknown;
    }
    DWORD mode = 0;
    if (GetConsoleMode(handle, &mode)) {
        return StderrTargetKind::Terminal;
    }
    const DWORD type = GetFileType(handle);
    if (type == FILE_TYPE_PIPE) {
        if (stdout_is_terminal()) {
            return StderrTargetKind::Terminal;
        }
        return StderrTargetKind::Pipe;
    }
    if (type == FILE_TYPE_DISK) {
        return StderrTargetKind::File;
    }
    if (type == FILE_TYPE_CHAR) {
        return StderrTargetKind::Character;
    }
    if (type == FILE_TYPE_UNKNOWN) {
        return StderrTargetKind::Unknown;
    }
    return StderrTargetKind::Other;
#else
    if (isatty(STDERR_FILENO)) {
        return StderrTargetKind::Terminal;
    }
    struct stat info;
    if (fstat(STDERR_FILENO, &info) != 0) {
        return StderrTargetKind::Unknown;
    }
    if (S_ISFIFO(info.st_mode)
#ifdef S_ISSOCK
        || S_ISSOCK(info.st_mode)
#endif
    ) {
        return StderrTargetKind::Pipe;
    }
    if (S_ISREG(info.st_mode)) {
        return StderrTargetKind::File;
    }
    if (S_ISCHR(info.st_mode)) {
        return StderrTargetKind::Character;
    }
    return StderrTargetKind::Other;
#endif
}

bool terminal_color_allowed() {
    const char* no_color = std::getenv("NO_COLOR");
    if (no_color != nullptr && no_color[0] != '\0') {
        return false;
    }
    const char* term = std::getenv("TERM");
    if (term != nullptr && std::string(term) == "dumb") {
        return false;
    }
    if (stderr_target_kind() != StderrTargetKind::Terminal) {
        return false;
    }
#if defined(_WIN32)
    const HANDLE handle = GetStdHandle(STD_ERROR_HANDLE);
    if (handle == nullptr || handle == INVALID_HANDLE_VALUE) {
        return true;
    }
    DWORD mode = 0;
    if (GetConsoleMode(handle, &mode)) {
        SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
    }
#endif
    return true;
}

bool stderr_color_enabled() {
    static const bool enabled = terminal_color_allowed();
    return enabled;
}

std::string ansi(const char* code) {
    return stderr_color_enabled() ? std::string("\x1b[") + code + "m" : std::string();
}

bool contains_text(const std::string& text, const std::string& needle) {
    return text.find(needle) != std::string::npos;
}

bool token_has_failure(const std::string& token) {
    static const char* needles[] = {
        "error", "fail", "timeout", "rejected", "resource_limited=true",
        "resource_limited=yes", "timed_out=true", "found=no", "accepted=no"};
    for (const char* needle : needles) {
        if (contains_text(token, needle)) {
            return true;
        }
    }
    return false;
}

bool token_has_success(const std::string& token) {
    static const char* needles[] = {
        "done", "found=yes", "accepted=yes", "status=ok",
        "stop_quit_at_crossing", "matched", "applied"};
    for (const char* needle : needles) {
        if (contains_text(token, needle)) {
            return true;
        }
    }
    return false;
}

bool token_has_stage(const std::string& token) {
    static const char* needles[] = {
        "start", "pre_simplify", "r3_prepass", "search", "non_monotone",
        "brute_fallback", "r3_failover", "reapr", "handoff", "adaptive_order",
        "progress"};
    for (const char* needle : needles) {
        if (contains_text(token, needle)) {
            return true;
        }
    }
    return false;
}

bool string_is_numberish(const std::string& text) {
    bool has_digit = false;
    for (char c : text) {
        if (std::isdigit(static_cast<unsigned char>(c))) {
            has_digit = true;
            continue;
        }
        if (c == '.' || c == '-' || c == '+' || c == ',') {
            continue;
        }
        return false;
    }
    return has_digit;
}

std::string colorize_value(const std::string& value) {
    if (!stderr_color_enabled() || value.empty()) {
        return value;
    }
    if (value == "yes" || value == "true" || value == "ok") {
        return ansi("1;32") + value + ansi("0");
    }
    if (value == "no" || value == "false") {
        return ansi("2") + value + ansi("0");
    }
    if (string_is_numberish(value)) {
        return ansi("1;33") + value + ansi("0");
    }
    return value;
}

std::string colorize_log_token(const std::string& token) {
    if (!stderr_color_enabled() || token.empty()) {
        return token;
    }
    if (token == "->") {
        return ansi("2") + token + ansi("0");
    }
    const std::size_t equals = token.find('=');
    if (equals != std::string::npos && equals + 1 < token.size()) {
        const std::string key = token.substr(0, equals + 1);
        const std::string value = token.substr(equals + 1);
        if (token_has_failure(token)) {
            return ansi("1;31") + key + colorize_value(value) + ansi("0");
        }
        if (token_has_success(token)) {
            return ansi("1;32") + key + colorize_value(value) + ansi("0");
        }
        return ansi("36") + key + ansi("0") + colorize_value(value);
    }
    if (token_has_failure(token)) {
        return ansi("1;31") + token + ansi("0");
    }
    if (token_has_success(token)) {
        return ansi("1;32") + token + ansi("0");
    }
    if (token_has_stage(token)) {
        return ansi("1;34") + token + ansi("0");
    }
    if (string_is_numberish(token)) {
        return ansi("1;33") + token + ansi("0");
    }
    return token;
}

std::string colorize_log_message(const std::string& message) {
    if (!stderr_color_enabled()) {
        return message;
    }
    std::ostringstream output;
    std::string token;
    for (char c : message) {
        if (std::isspace(static_cast<unsigned char>(c))) {
            if (!token.empty()) {
                output << colorize_log_token(token);
                token.clear();
            }
            output << c;
        } else {
            token.push_back(c);
        }
    }
    if (!token.empty()) {
        output << colorize_log_token(token);
    }
    return output.str();
}

void print_progress_log(const std::string& message) {
    if (stderr_color_enabled()) {
        std::cerr << ansi("2") << '[' << ansi("0")
                  << ansi("1;36") << "pdcode-simplify" << ansi("0") << ' '
                  << ansi("2;37") << local_timestamp() << ansi("0")
                  << ansi("2") << "] " << ansi("0")
                  << colorize_log_message(message) << '\n';
        return;
    }
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
    out << "\"stopped_by_crossing_limit\":"
        << (result.stopped_by_crossing_limit ? "true" : "false") << ",";
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
    int quit_at_crossing,
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
        options.quit_at_crossing = quit_at_crossing;
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
