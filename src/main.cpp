#include "pdcode_simplify/pdcode_simplify.hpp"

#include <algorithm>
#include <cctype>
#include <csignal>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
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
#else
#include <dirent.h>
#include <sys/stat.h>
#endif

namespace {

volatile std::sig_atomic_t g_interrupted = 0;

void handle_interrupt(int) {
    g_interrupted = 1;
}

bool interrupted() {
    return g_interrupted != 0;
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

void print_progress_log(const std::string& message) {
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
        << "Use --timeout K to cap each PD-code job in seconds; -1 means no timeout.\n"
        << "Use --verbose to print progress logs to stderr.\n"
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

void print_text_result(
    const pdcode_simplify::ReductionResult& result,
    const pdcode_simplify::ComponentAnalysis& input_components,
    const pdcode_simplify::ComponentAnalysis& final_components,
    const pdcode_simplify::ComponentAnalysis* after_removal_components) {
    std::cout << "simplification_found: "
              << (result.mid_simplification_rounds > 0 ? "yes" : "no") << '\n';
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
    std::cout << "nugatory_crossing_moves: " << result.nugatory_crossing_moves << '\n';
    std::cout << "tested_red_paths: " << result.tested_red_paths << '\n';
    std::cout << "tested_green_paths: " << result.tested_green_paths << '\n';
    std::cout << "last_path_search_mode: " << result.last_path_search_mode << '\n';
    std::cout << "stopped_by_round_limit: "
              << (result.stopped_by_round_limit ? "yes" : "no") << '\n';
    std::cout << "timed_out: " << (result.timed_out ? "yes" : "no") << '\n';
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
              << (result.mid_simplification_rounds > 0 ? "true" : "false") << ",\n";
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
    std::cout << "  \"nugatory_crossing_moves\": "
              << result.nugatory_crossing_moves << ",\n";
    std::cout << "  \"tested_red_paths\": " << result.tested_red_paths << ",\n";
    std::cout << "  \"tested_green_paths\": " << result.tested_green_paths << ",\n";
    std::cout << "  \"last_path_search_mode\": \""
              << json_escape(result.last_path_search_mode) << "\",\n";
    std::cout << "  \"stopped_by_round_limit\": "
              << (result.stopped_by_round_limit ? "true" : "false") << ",\n";
    std::cout << "  \"timed_out\": "
              << (result.timed_out ? "true" : "false") << "\n";
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
    try {
        std::signal(SIGINT, handle_interrupt);
        pdcode_simplify::SimplifierOptions options;
        options.progress = [](const std::string& message) {
            print_progress_log(message);
        };
        options.should_cancel = []() {
            return interrupted();
        };
        bool json = false;
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
            } else if (arg == "--ban-heuristic") {
                options.ban_heuristic = true;
            } else if (arg == "--verbose") {
                options.verbose = true;
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
            } else if (arg == "--timeout") {
                if (i + 1 >= argc) {
                    throw std::invalid_argument("--timeout requires a value");
                }
                options.timeout_seconds = std::stoi(argv[++i]);
                if (options.timeout_seconds < -1 || options.timeout_seconds == 0) {
                    throw std::invalid_argument("--timeout must be -1 or a positive integer");
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
                const auto result = pdcode_simplify::reduce_pd_code(
                    jobs[i].code,
                    job_crossingless_components,
                    job_options,
                    reduction_round);
                if (result.timed_out) {
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
