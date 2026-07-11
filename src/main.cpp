#include "pdcode_simplify/pdcode_simplify.hpp"

#include <algorithm>
#include <cctype>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <io.h>
#else
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace {

volatile std::sig_atomic_t g_interrupted = 0;

void handle_interrupt(int) {
    g_interrupted = 1;
}

bool interrupted() {
    return g_interrupted != 0;
}

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

std::string log_file_arg(int argc, char** argv) {
    std::string result;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--log-file") {
            if (i + 1 >= argc) {
                throw std::invalid_argument("--log-file requires a file path");
            }
            result = argv[++i];
        } else if (arg.compare(0, 11, "--log-file=") == 0) {
            result = arg.substr(11);
            if (result.empty()) {
                throw std::invalid_argument("--log-file requires a file path");
            }
        }
    }
    return result;
}

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
#ifdef _WIN32
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
#ifdef _WIN32
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
#ifdef _WIN32
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

std::string lower_copy(std::string text) {
    for (char& c : text) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return text;
}

bool contains_text(const std::string& text, const std::string& needle) {
    return text.find(needle) != std::string::npos;
}

bool text_has_any(const std::string& text, const char* const* needles, std::size_t count) {
    for (std::size_t i = 0; i < count; ++i) {
        if (contains_text(text, needles[i])) {
            return true;
        }
    }
    return false;
}

bool text_equals_any(const std::string& text, const char* const* needles, std::size_t count) {
    for (std::size_t i = 0; i < count; ++i) {
        if (text == needles[i]) {
            return true;
        }
    }
    return false;
}

bool token_is_failure_word(const std::string& token) {
    const std::string lower = lower_copy(token);
    static const char* contains_needles[] = {
        "error", "timeout", "timed_out", "resource_limited", "rejected", "exception", "panic"};
    static const char* exact_needles[] = {"fail", "failed", "failure"};
    return text_has_any(lower, contains_needles, sizeof(contains_needles) / sizeof(contains_needles[0])) ||
           text_equals_any(lower, exact_needles, sizeof(exact_needles) / sizeof(exact_needles[0]));
}

bool token_is_success_word(const std::string& token) {
    const std::string lower = lower_copy(token);
    static const char* needles[] = {
        "yes", "true", "ok", "done", "found", "accepted", "matched", "applied",
        "success", "improved", "stop_quit_at_crossing"};
    for (const char* needle : needles) {
        if (lower == needle) {
            return true;
        }
    }
    return false;
}

bool token_is_warning_word(const std::string& token) {
    const std::string lower = lower_copy(token);
    static const char* needles[] = {"skip", "skipped", "warning", "warn"};
    for (const char* needle : needles) {
        if (lower == needle) {
            return true;
        }
    }
    return false;
}

std::size_t stage_prefix_length(const std::string& token) {
    const std::string lower = lower_copy(token);
    static const char* needles[] = {
        "heuristic_search", "non_monotone", "brute_fallback", "adaptive_order",
        "pre_simplify", "r3_prepass", "r3_failover", "handoff", "progress",
        "search", "reapr", "start"};
    for (const char* needle : needles) {
        const std::size_t n = std::string(needle).size();
        if (lower.compare(0, n, needle) == 0 &&
            (lower.size() == n || lower[n] == '_' || lower[n] == '(')) {
            return n;
        }
    }
    return 0;
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
    const std::string lower = lower_copy(value);
    if (lower == "yes" || lower == "true" || lower == "ok" ||
        lower == "accepted" || lower == "matched" || lower == "applied" ||
        lower == "done" || lower == "success" || lower == "improved") {
        return ansi("1;32") + value + ansi("0");
    }
    if (lower == "no" || lower == "false" || lower == "failed" || lower == "failure" ||
        lower == "timeout" || lower == "timed_out" || lower == "rejected" ||
        lower == "resource_limited" || lower == "error") {
        return ansi("1;91") + value + ansi("0");
    }
    if (lower == "skipped" || lower == "warning" || lower == "warn") {
        return ansi("1;35") + value + ansi("0");
    }
    if (string_is_numberish(value)) {
        return ansi("1;33") + value + ansi("0");
    }
    return value;
}

std::string colorize_status_piece(const std::string& piece) {
    if (piece.empty()) {
        return piece;
    }
    if (token_is_failure_word(piece)) {
        return ansi("1;91") + piece + ansi("0");
    }
    if (token_is_success_word(piece)) {
        return ansi("1;32") + piece + ansi("0");
    }
    if (token_is_warning_word(piece)) {
        return ansi("1;35") + piece + ansi("0");
    }
    if (lower_copy(piece) == "start") {
        return ansi("1;36") + piece + ansi("0");
    }
    if (string_is_numberish(piece)) {
        return ansi("1;33") + piece + ansi("0");
    }
    return piece;
}

std::string colorize_stage_suffix(const std::string& suffix) {
    std::ostringstream output;
    std::string piece;
    for (char c : suffix) {
        if (c == '_') {
            if (!piece.empty()) {
                output << colorize_status_piece(piece);
                piece.clear();
            }
            output << ansi("2") << c << ansi("0");
        } else {
            piece.push_back(c);
        }
    }
    if (!piece.empty()) {
        output << colorize_status_piece(piece);
    }
    return output.str();
}

std::string colorize_log_token(const std::string& token);

std::string colorize_stage_payload(const std::string& payload) {
    std::ostringstream output;
    std::string piece;
    for (char c : payload) {
        if (c == '(' || c == ')' || c == ',' || c == ';') {
            if (!piece.empty()) {
                output << colorize_log_token(piece);
                piece.clear();
            }
            output << ansi("2") << c << ansi("0");
        } else {
            piece.push_back(c);
        }
    }
    if (!piece.empty()) {
        output << colorize_log_token(piece);
    }
    return output.str();
}

std::string colorize_stage_token(const std::string& token) {
    const std::size_t prefix_length = stage_prefix_length(token);
    if (prefix_length == 0) {
        return std::string();
    }
    const std::string suffix = token.substr(prefix_length);
    if (!suffix.empty() && suffix[0] == '(') {
        return ansi("1;34") + token.substr(0, prefix_length) + ansi("0") +
               colorize_stage_payload(suffix);
    }
    return ansi("1;34") + token.substr(0, prefix_length) + ansi("0") +
           colorize_stage_suffix(suffix);
}

std::string colorize_log_token(const std::string& token) {
    if (!stderr_color_enabled() || token.empty()) {
        return token;
    }
    if (token == "->") {
        return ansi("2") + token + ansi("0");
    }
    const std::string stage_token = colorize_stage_token(token);
    if (!stage_token.empty()) {
        return stage_token;
    }
    const std::size_t equals = token.find('=');
    if (equals != std::string::npos && equals + 1 < token.size()) {
        const std::string key = token.substr(0, equals + 1);
        const std::string value = token.substr(equals + 1);
        return ansi("36") + key + ansi("0") + colorize_value(value);
    }
    if (token_is_failure_word(token)) {
        return ansi("1;91") + token + ansi("0");
    }
    if (token_is_success_word(token)) {
        return ansi("1;32") + token + ansi("0");
    }
    if (token_is_warning_word(token)) {
        return ansi("1;35") + token + ansi("0");
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

struct PDJob {
    std::string label;
    pdcode_simplify::PDCode code;
    std::size_t implied_crossingless_components = 0;
    std::string error;

    bool has_error() const {
        return !error.empty();
    }
};

void print_help(const char* program) {
    std::cout
        << "Usage: " << program << " [--pd-code CODE] [--pd-file FILE] [--pd-dir DIR] [options]\n"
        << "\n"
        << "Simplify a knot or link PD code and print the final PD code.\n"
        << "Inputs follow the cppkh style: standard PD[...] strings, files, or directories.\n"
        << "Use --known-crossingless-components N when the input already has\n"
        << "components that cannot be represented by a PD code.\n"
        << "Use --remove-crossings LIST to report component counts after a\n"
        << "zero-based crossing-removal simulation.\n"
        << "R1-move removal followed by nugatory-crossing removal is enabled by default.\n"
        << "Use --reduction-round K to cap mid-simplification rounds; -1 means until stable.\n"
        << "With --max-paths -1, heuristic green-path sampling is enabled by default.\n"
        << "Use --ban-heuristic to force brute-force green-path enumeration.\n"
        << "Use --max-thread N to cap brute-force worker threads; -1 means auto.\n"
        << "Use --bruteforce-budget N to cap brute-force green-path checks; -1 means no cap.\n"
        << "Use --quit-at-crossing N to stop once crossings are at most N; -1 disables it.\n"
        << "Use --reapr to enable the experimental invariant-guarded projection oracle.\n"
        << "Use --reapr-retry-max N to cap deterministic REAPR retry attempts; default is 3.\n"
        << "Use --timeout K to cap each PD-code job in seconds; -1 means no timeout.\n"
        << "Use --verbose to print progress logs to stderr.\n"
        << "Use --show-step-pd to print the canonical PD code after each witness application.\n"
        << "Use --log-file FILEPATH to tee stdout and stderr output into a flushed log file.\n"
        << "If no input is given, the CLI tries to read PD.txt from the current directory.\n";
}

std::string read_file(const std::string& path) {
    std::ifstream input(path);
    if (!input) {
        throw std::runtime_error("Could not open input file: " + path);
    }
    std::ostringstream buffer;
    buffer << input.rdbuf();
    return buffer.str();
}

std::string trim(const std::string& value) {
    const std::size_t first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
        return "";
    }
    const std::size_t last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

std::string remove_ascii_whitespace(const std::string& value) {
    std::string result;
    for (char c : value) {
        if (!std::isspace(static_cast<unsigned char>(c))) {
            result.push_back(c);
        }
    }
    return result;
}

bool denotes_crossingless_unknot(const std::string& payload) {
    const std::string compact = remove_ascii_whitespace(payload);
    return compact == "PD[]" || compact == "[]";
}

bool file_exists(const std::string& path) {
    std::ifstream input(path);
    return static_cast<bool>(input);
}

bool is_directory(const std::string& path) {
#ifdef _WIN32
    const DWORD attributes = GetFileAttributesA(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
#else
    struct stat info;
    return stat(path.c_str(), &info) == 0 && S_ISDIR(info.st_mode);
#endif
}

bool has_pd_extension(const std::string& path) {
    std::string lower = path;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](char c) {
        return static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    });
    return lower.size() >= 3 &&
           (lower.substr(lower.size() - 3) == ".pd" ||
            (lower.size() >= 4 && lower.substr(lower.size() - 4) == ".txt"));
}

std::vector<std::string> list_input_files(const std::string& directory) {
    std::vector<std::string> files;
#ifdef _WIN32
    std::string search = directory;
    if (!search.empty() && search.back() != '\\' && search.back() != '/') {
        search += "\\";
    }
    const std::string prefix = search;
    search += "*";
    WIN32_FIND_DATAA data;
    HANDLE handle = FindFirstFileA(search.c_str(), &data);
    if (handle == INVALID_HANDLE_VALUE) {
        throw std::runtime_error("Cannot open directory: " + directory);
    }
    do {
        const std::string name = data.cFileName;
        if (name == "." || name == "..") {
            continue;
        }
        const std::string path = prefix + name;
        if ((data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) == 0 && has_pd_extension(path)) {
            files.push_back(path);
        }
    } while (FindNextFileA(handle, &data));
    FindClose(handle);
#else
    DIR* dir = opendir(directory.c_str());
    if (dir == nullptr) {
        throw std::runtime_error("Cannot open directory: " + directory);
    }
    while (dirent* entry = readdir(dir)) {
        const std::string name = entry->d_name;
        if (name == "." || name == "..") {
            continue;
        }
        std::string path = directory;
        if (!path.empty() && path.back() != '/') {
            path += "/";
        }
        path += name;
        if (!is_directory(path) && has_pd_extension(path)) {
            files.push_back(path);
        }
    }
    closedir(dir);
#endif
    std::sort(files.begin(), files.end());
    return files;
}

std::string label_for_block(const std::string& text, std::size_t block_start, const std::string& label_prefix, int index) {
    std::size_t line_start = text.rfind('\n', block_start);
    line_start = line_start == std::string::npos ? 0 : line_start + 1;
    const std::string before_block = text.substr(line_start, block_start - line_start);
    const std::size_t colon = before_block.find(':');
    if (colon != std::string::npos) {
        const std::string line_label = trim(before_block.substr(0, colon));
        if (!line_label.empty()) {
            return label_prefix + ":" + line_label;
        }
    }

    if (index == 0) {
        return label_prefix;
    }
    return label_prefix + "#" + std::to_string(index + 1);
}

std::vector<PDJob> parse_pd_document(const std::string& text, const std::string& label_prefix) {
    std::vector<PDJob> jobs;
    std::size_t pos = 0;
    int index = 0;

    while (true) {
        const std::size_t start = text.find("PD[", pos);
        if (start == std::string::npos) {
            break;
        }

        int depth = 0;
        std::size_t end = std::string::npos;
        for (std::size_t i = start + 2; i < text.size(); ++i) {
            if (text[i] == '[') {
                ++depth;
            } else if (text[i] == ']') {
                --depth;
                if (depth == 0) {
                    end = i;
                    break;
                }
            }
        }
        if (end == std::string::npos) {
            PDJob job;
            job.label = label_prefix + "#" + std::to_string(index + 1);
            job.error = "Unterminated PD[...] block";
            jobs.push_back(std::move(job));
            break;
        }

        PDJob job;
        job.label = label_for_block(text, start, label_prefix, index);
        const std::string block = text.substr(start, end - start + 1);
        try {
            job.code = pdcode_simplify::parse_pd_code(block);
            job.implied_crossingless_components = denotes_crossingless_unknot(block) ? 1 : 0;
        } catch (const std::exception& error) {
            job.error = error.what();
        }
        jobs.push_back(std::move(job));
        ++index;
        pos = end + 1;
    }

    if (!jobs.empty()) {
        return jobs;
    }

    std::istringstream lines(text);
    std::string line;
    while (std::getline(lines, line)) {
        const std::string cleaned = trim(line);
        if (cleaned.empty() || cleaned[0] == '#') {
            continue;
        }

        std::string payload = cleaned;
        std::string label = label_prefix;
        const std::size_t colon = cleaned.find(':');
        if (colon != std::string::npos) {
            const std::string line_label = trim(cleaned.substr(0, colon));
            payload = trim(cleaned.substr(colon + 1));
            if (!line_label.empty()) {
                label += ":" + line_label;
            }
        } else if (!jobs.empty()) {
            label += "#" + std::to_string(jobs.size() + 1);
        }

        const bool has_digit = std::find_if(payload.begin(), payload.end(), [](char c) {
            return std::isdigit(static_cast<unsigned char>(c)) != 0;
        }) != payload.end();
        if (!has_digit && payload.find("PD[]") == std::string::npos && payload != "[]") {
            continue;
        }

        PDJob job;
        job.label = label;
        try {
            job.code = pdcode_simplify::parse_pd_code(payload);
            job.implied_crossingless_components = denotes_crossingless_unknot(payload) ? 1 : 0;
        } catch (const std::exception& error) {
            job.error = error.what();
        }
        jobs.push_back(std::move(job));
    }

    return jobs;
}

std::vector<PDJob> read_pd_file(const std::string& path) {
    std::vector<PDJob> jobs = parse_pd_document(read_file(path), path);
    if (jobs.size() == 1) {
        jobs[0].label = path;
    }
    return jobs;
}

std::vector<int> parse_integer_list(const std::string& text) {
    std::vector<int> values;
    for (std::size_t i = 0; i < text.size();) {
        if (text[i] == '-' || std::isdigit(static_cast<unsigned char>(text[i]))) {
            const std::size_t start = i;
            if (text[i] == '-') {
                ++i;
            }
            while (i < text.size() && std::isdigit(static_cast<unsigned char>(text[i]))) {
                ++i;
            }
            values.push_back(std::stoi(text.substr(start, i - start)));
        } else {
            ++i;
        }
    }
    return values;
}

void print_component_counts(const pdcode_simplify::ComponentAnalysis& analysis, const char* prefix) {
    std::cout << prefix << "_components_with_crossings: "
              << analysis.components_with_crossings() << '\n';
    std::cout << prefix << "_crossingless_components: "
              << analysis.crossingless_components << '\n';
    std::cout << prefix << "_total_components: "
              << analysis.total_components() << '\n';
}

bool has_simplification(const pdcode_simplify::ReductionResult& result) {
    return result.mid_simplification_rounds > 0 ||
           result.reidemeister_i_moves > 0 ||
           result.reidemeister_ii_moves > 0 ||
           result.reidemeister_iii_moves > 0 ||
           result.nugatory_crossing_moves > 0 ||
           result.reapr_used;
}

void print_text_result(
    const pdcode_simplify::ReductionResult& result,
    const pdcode_simplify::ComponentAnalysis& input_components,
    const pdcode_simplify::ComponentAnalysis& final_components,
    const pdcode_simplify::ComponentAnalysis* after_removal_components) {
    std::cout << "simplification_found: "
              << (has_simplification(result) ? "yes" : "no") << '\n';
    print_component_counts(input_components, "input");
    if (after_removal_components != nullptr) {
        print_component_counts(*after_removal_components, "after_removal");
    }
    std::cout << "final_pd_code: " << pdcode_simplify::format_final_pd_code(result.code) << '\n';
    std::cout << "final_crossings: " << result.code.size() << '\n';
    print_component_counts(final_components, "final");
    std::cout << "mid_simplification_rounds: " << result.mid_simplification_rounds << '\n';
    std::cout << "heuristic_failover_rounds: " << result.heuristic_failover_rounds << '\n';
    std::cout << "reidemeister_i_moves: " << result.reidemeister_i_moves << '\n';
    std::cout << "reidemeister_ii_moves: " << result.reidemeister_ii_moves << '\n';
    std::cout << "reidemeister_iii_moves: " << result.reidemeister_iii_moves << '\n';
    std::cout << "nugatory_crossing_moves: " << result.nugatory_crossing_moves << '\n';
    std::cout << "tested_red_paths: " << result.tested_red_paths << '\n';
    std::cout << "tested_green_paths: " << result.tested_green_paths << '\n';
    std::cout << "last_path_search_mode: " << result.last_path_search_mode << '\n';
    std::cout << "reapr_used: " << (result.reapr_used ? "yes" : "no") << '\n';
    std::cout << "reapr_rounds: " << result.reapr_rounds << '\n';
    std::cout << "reapr_attempts: " << result.reapr_attempts << '\n';
    std::cout << "reapr_rejected: " << (result.reapr_rejected ? "yes" : "no") << '\n';
    std::cout << "reapr_status: " << result.reapr_status << '\n';
    if (!result.reapr_warning.empty()) {
        std::cout << "reapr_warning: " << result.reapr_warning << '\n';
    }
    std::cout << "alexander_determinant_before: "
              << result.alexander_determinant_before << '\n';
    std::cout << "alexander_determinant_after: "
              << result.alexander_determinant_after << '\n';
    std::cout << "reapr_invariants_before: "
              << result.reapr_invariants_before << '\n';
    std::cout << "reapr_invariants_after: "
              << result.reapr_invariants_after << '\n';
    std::cout << "stopped_by_round_limit: "
              << (result.stopped_by_round_limit ? "yes" : "no") << '\n';
    std::cout << "stopped_by_crossing_limit: "
              << (result.stopped_by_crossing_limit ? "yes" : "no") << '\n';
    std::cout << "timed_out: " << (result.timed_out ? "yes" : "no") << '\n';
    std::cout << "resource_limited: " << (result.resource_limited ? "yes" : "no") << '\n';
}

void print_text_error(const std::string& error) {
    std::cout << "error: " << error << '\n';
}

void print_json_component_counts(const pdcode_simplify::ComponentAnalysis& analysis) {
    std::cout << "\"components_with_crossings\":" << analysis.components_with_crossings()
              << ",\"crossingless_components\":" << analysis.crossingless_components
              << ",\"total_components\":" << analysis.total_components();
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

void print_json_result(
    const pdcode_simplify::ReductionResult& result,
    const pdcode_simplify::ComponentAnalysis& input_components,
    const pdcode_simplify::ComponentAnalysis& final_components,
    const pdcode_simplify::ComponentAnalysis* after_removal_components,
    const std::string* label = nullptr) {
    std::cout << "{\n";
    if (label != nullptr) {
        std::cout << "  \"label\": \"" << json_escape(*label) << "\",\n";
    }
    std::cout << "  \"simplification_found\": "
              << (has_simplification(result) ? "true" : "false") << ",\n";
    std::cout << "  \"input_components\": {";
    print_json_component_counts(input_components);
    std::cout << "},\n";
    if (after_removal_components != nullptr) {
        std::cout << "  \"after_removal_components\": {";
        print_json_component_counts(*after_removal_components);
        std::cout << "},\n";
    }
    std::cout << "  \"final_pd_code\": \""
              << json_escape(pdcode_simplify::format_final_pd_code(result.code)) << "\",\n";
    std::cout << "  \"final_crossings\": " << result.code.size() << ",\n";
    std::cout << "  \"final_components\": {";
    print_json_component_counts(final_components);
    std::cout << "},\n";
    std::cout << "  \"mid_simplification_rounds\": "
              << result.mid_simplification_rounds << ",\n";
    std::cout << "  \"heuristic_failover_rounds\": "
              << result.heuristic_failover_rounds << ",\n";
    std::cout << "  \"reidemeister_i_moves\": " << result.reidemeister_i_moves << ",\n";
    std::cout << "  \"reidemeister_ii_moves\": " << result.reidemeister_ii_moves << ",\n";
    std::cout << "  \"reidemeister_iii_moves\": " << result.reidemeister_iii_moves << ",\n";
    std::cout << "  \"nugatory_crossing_moves\": "
              << result.nugatory_crossing_moves << ",\n";
    std::cout << "  \"tested_red_paths\": " << result.tested_red_paths << ",\n";
    std::cout << "  \"tested_green_paths\": " << result.tested_green_paths << ",\n";
    std::cout << "  \"last_path_search_mode\": \""
              << json_escape(result.last_path_search_mode) << "\",\n";
    std::cout << "  \"reapr_used\": "
              << (result.reapr_used ? "true" : "false") << ",\n";
    std::cout << "  \"reapr_rounds\": " << result.reapr_rounds << ",\n";
    std::cout << "  \"reapr_attempts\": " << result.reapr_attempts << ",\n";
    std::cout << "  \"reapr_rejected\": "
              << (result.reapr_rejected ? "true" : "false") << ",\n";
    std::cout << "  \"reapr_status\": \""
              << json_escape(result.reapr_status) << "\",\n";
    std::cout << "  \"reapr_warning\": \""
              << json_escape(result.reapr_warning) << "\",\n";
    std::cout << "  \"alexander_determinant_before\": \""
              << json_escape(result.alexander_determinant_before) << "\",\n";
    std::cout << "  \"alexander_determinant_after\": \""
              << json_escape(result.alexander_determinant_after) << "\",\n";
    std::cout << "  \"reapr_invariants_before\": \""
              << json_escape(result.reapr_invariants_before) << "\",\n";
    std::cout << "  \"reapr_invariants_after\": \""
              << json_escape(result.reapr_invariants_after) << "\",\n";
    std::cout << "  \"stopped_by_round_limit\": "
              << (result.stopped_by_round_limit ? "true" : "false") << ",\n";
    std::cout << "  \"stopped_by_crossing_limit\": "
              << (result.stopped_by_crossing_limit ? "true" : "false") << ",\n";
    std::cout << "  \"timed_out\": "
              << (result.timed_out ? "true" : "false") << ",\n";
    std::cout << "  \"resource_limited\": "
              << (result.resource_limited ? "true" : "false") << "\n";
    std::cout << "}\n";
}

void print_json_error(const std::string& error, const std::string* label = nullptr) {
    std::cout << "{\n";
    if (label != nullptr) {
        std::cout << "  \"label\": \"" << json_escape(*label) << "\",\n";
    }
    std::cout << "  \"error\": \"" << json_escape(error) << "\"\n";
    std::cout << "}\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::ofstream log_file;
    std::mutex log_mutex;
    std::unique_ptr<TeeStreamBuffer> cout_tee;
    std::unique_ptr<TeeStreamBuffer> cerr_tee;
    std::unique_ptr<ScopedStreamRedirect> cout_redirect;
    std::unique_ptr<ScopedStreamRedirect> cerr_redirect;

    try {
        std::signal(SIGINT, handle_interrupt);
        const std::string log_path = log_file_arg(argc, argv);
        if (!log_path.empty()) {
            log_file.open(log_path.c_str(), std::ios::out | std::ios::binary);
            if (!log_file) {
                throw std::runtime_error("Could not open log file: " + log_path);
            }
            cout_tee.reset(new TeeStreamBuffer(std::cout.rdbuf(), log_file.rdbuf(), log_mutex));
            cerr_tee.reset(new TeeStreamBuffer(std::cerr.rdbuf(), log_file.rdbuf(), log_mutex));
            cout_redirect.reset(new ScopedStreamRedirect(std::cout, cout_tee.get()));
            cerr_redirect.reset(new ScopedStreamRedirect(std::cerr, cerr_tee.get()));
        }

        pdcode_simplify::SimplifierOptions options;
        options.progress = [](const std::string& message) {
            print_progress_log(message);
        };
        options.should_cancel = []() {
            return interrupted();
        };
        bool json = false;
        bool show_step_pd = false;
        int reduction_round = -1;
        std::size_t known_crossingless_components = 0;
        std::vector<int> removed_crossings;
        bool has_removal_simulation = false;
        std::vector<std::string> files;
        std::vector<PDJob> jobs;
        std::vector<std::string> positional;

        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            if (arg == "--help" || arg == "-h") {
                print_help(argv[0]);
                return 0;
            }
            if (arg == "--json") {
                json = true;
            } else if (arg == "--show-step-pd") {
                show_step_pd = true;
            } else if (arg == "--ban-heuristic") {
                options.ban_heuristic = true;
            } else if (arg == "--reapr") {
                options.enable_reapr = true;
            } else if (arg == "--reapr-retry-max") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--reapr-retry-max requires a value");
                }
                options.reapr_retry_max = std::stoi(argv[++i]);
                if (options.reapr_retry_max < 0) {
                    throw std::invalid_argument("--reapr-retry-max must be a non-negative integer");
                }
            } else if (arg == "--verbose") {
                options.verbose = true;
            } else if (arg == "--log-file") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--log-file requires a file path");
                }
                ++i;
            } else if (arg.compare(0, 11, "--log-file=") == 0) {
                if (arg.size() == 11) {
                    throw std::invalid_argument("--log-file requires a file path");
                }
            } else if (arg == "--max-paths") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--max-paths requires a value");
                }
                options.max_paths = std::stoi(argv[++i]);
            } else if (arg == "--max-thread") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--max-thread requires a value");
                }
                options.max_threads = std::stoi(argv[++i]);
                if (options.max_threads < -1 || options.max_threads == 0) {
                    throw std::invalid_argument("--max-thread must be -1 or a positive integer");
                }
            } else if (arg == "--bruteforce-budget") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--bruteforce-budget requires a value");
                }
                options.bruteforce_budget = std::stoll(argv[++i]);
                if (options.bruteforce_budget < -1 || options.bruteforce_budget == 0) {
                    throw std::invalid_argument("--bruteforce-budget must be -1 or a positive integer");
                }
            } else if (arg == "--timeout") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--timeout requires a value");
                }
                options.timeout_seconds = std::stoi(argv[++i]);
                if (options.timeout_seconds < -1 || options.timeout_seconds == 0) {
                    throw std::invalid_argument("--timeout must be -1 or a positive integer");
                }
            } else if (arg == "--quit-at-crossing") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--quit-at-crossing requires a value");
                }
                options.quit_at_crossing = std::stoi(argv[++i]);
                if (options.quit_at_crossing < -1) {
                    throw std::invalid_argument("--quit-at-crossing must be -1 or a nonnegative integer");
                }
            } else if (arg == "--reduction-round") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--reduction-round requires a value");
                }
                reduction_round = std::stoi(argv[++i]);
                if (reduction_round < -1) {
                    throw std::invalid_argument("--reduction-round must be -1 or a nonnegative integer");
                }
            } else if (arg == "--known-crossingless-components") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--known-crossingless-components requires a value");
                }
                const int value = std::stoi(argv[++i]);
                if (value < 0) {
                    throw std::invalid_argument("--known-crossingless-components cannot be negative");
                }
                known_crossingless_components = static_cast<std::size_t>(value);
            } else if (arg == "--remove-crossings") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--remove-crossings requires a list");
                }
                removed_crossings = parse_integer_list(argv[++i]);
                has_removal_simulation = true;
            } else if (arg == "--pd-code" || arg == "-c") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--pd-code requires a PD[...] string");
                }
                const std::vector<PDJob> parsed = parse_pd_document(argv[++i], "command-line");
                jobs.insert(jobs.end(), parsed.begin(), parsed.end());
            } else if (arg == "--pd-file" || arg == "-f" || arg == "--input" || arg == "-i") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument(arg + " requires a file path");
                }
                files.push_back(argv[++i]);
            } else if (arg == "--pd-dir" || arg == "-d") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--pd-dir requires a directory path");
                }
                const std::vector<std::string> directory_files = list_input_files(argv[++i]);
                files.insert(files.end(), directory_files.begin(), directory_files.end());
            } else {
                positional.push_back(arg);
            }
        }

        if (!positional.empty()) {
            if (positional.size() == 1 && is_directory(positional.front())) {
                const std::vector<std::string> directory_files = list_input_files(positional.front());
                files.insert(files.end(), directory_files.begin(), directory_files.end());
            } else if (positional.size() == 1 && file_exists(positional.front())) {
                files.push_back(positional.front());
            } else {
                std::ostringstream literal;
                for (std::size_t i = 0; i < positional.size(); ++i) {
                    if (i != 0) {
                        literal << ' ';
                    }
                    literal << positional[i];
                }
                const std::vector<PDJob> parsed = parse_pd_document(literal.str(), "command-line");
                jobs.insert(jobs.end(), parsed.begin(), parsed.end());
            }
        }

        if (files.empty() && jobs.empty()) {
            files.push_back("PD.txt");
        }

        for (const std::string& file : files) {
            const std::vector<PDJob> parsed = read_pd_file(file);
            jobs.insert(jobs.end(), parsed.begin(), parsed.end());
        }

        if (jobs.empty()) {
            throw std::runtime_error("No PD code found");
        }

        const bool show_labels = jobs.size() > 1;
        bool had_error = false;

        if (json && jobs.size() > 1) {
            std::cout << "[\n";
        }

        for (std::size_t i = 0; i < jobs.size(); ++i) {
            try {
                if (interrupted()) {
                    throw std::runtime_error("interrupted by Ctrl+C");
                }
                if (jobs[i].has_error()) {
                    throw std::runtime_error(jobs[i].error);
                }

                const std::size_t job_crossingless_components =
                    known_crossingless_components + jobs[i].implied_crossingless_components;
                const auto input_components = pdcode_simplify::analyze_components(
                    jobs[i].code, job_crossingless_components);
                pdcode_simplify::ComponentAnalysis after_removal_components;
                if (has_removal_simulation) {
                    after_removal_components = pdcode_simplify::analyze_components_after_removing_crossings(
                        jobs[i].code, removed_crossings, job_crossingless_components);
                }

                pdcode_simplify::SimplifierOptions job_options = options;
                if (options.verbose) {
                    const std::string label = jobs[i].label;
                    job_options.progress = [label](const std::string& message) {
                        print_progress_log(label + ": " + message);
                    };
                }
                if (show_step_pd) {
                    const std::string label = jobs[i].label;
                    job_options.step_pd_output = [label, show_labels](
                        int round,
                        const pdcode_simplify::PDCode& step_code) {
                        if (show_labels) {
                            std::cout << label << ": ";
                        }
                        std::cout << "step_pd_code[" << round << "]: "
                                  << pdcode_simplify::format_final_pd_code(step_code)
                                  << '\n';
                        std::cout.flush();
                    };
                }
                const auto result = pdcode_simplify::reduce_pd_code(
                    jobs[i].code,
                    job_crossingless_components,
                    job_options,
                    reduction_round);
                if (result.timed_out || result.resource_limited) {
                    had_error = true;
                }
                const auto final_components = pdcode_simplify::analyze_components(
                    result.code, result.crossingless_components);

                if (json) {
                    print_json_result(
                        result,
                        input_components,
                        final_components,
                        has_removal_simulation ? &after_removal_components : nullptr,
                        show_labels ? &jobs[i].label : nullptr);
                } else {
                    if (show_labels) {
                        std::cout << jobs[i].label << ":\n";
                    }
                    print_text_result(
                        result,
                        input_components,
                        final_components,
                        has_removal_simulation ? &after_removal_components : nullptr);
                }
            } catch (const std::exception& error) {
                had_error = true;
                if (json) {
                    print_json_error(error.what(), show_labels ? &jobs[i].label : nullptr);
                } else {
                    if (show_labels) {
                        std::cout << jobs[i].label << ":\n";
                    }
                    print_text_error(error.what());
                }
                if (interrupted()) {
                    break;
                }
            }

            if (json && jobs.size() > 1 && i + 1 < jobs.size()) {
                std::cout << ",";
            }
            if (jobs.size() > 1) {
                std::cout << "\n";
            }
        }

        if (json && jobs.size() > 1) {
            std::cout << "]\n";
        }

        if (interrupted()) {
            return 130;
        }
        if (had_error) {
            return 2;
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "error: " << error.what() << '\n';
        return interrupted() ? 130 : 2;
    }
}
