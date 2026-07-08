#include "pdcode_simplify/pdcode_simplify.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <deque>
#include <limits>
#include <map>
#include <numeric>
#include <queue>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

namespace pdcode_simplify {
namespace {

constexpr int kBlockedWeight = 10000;
constexpr int kHeuristicBeamWidth = 8;
constexpr int kHeuristicMinStateBudget = 128;
constexpr int kHeuristicMaxStateBudget = 4096;
constexpr int kHeuristicMinPathBudget = 24;
constexpr int kHeuristicMaxPathBudget = 384;

int positive_mod(int value, int modulus) {
    int result = value % modulus;
    return result < 0 ? result + modulus : result;
}

int endpoint_key(const Endpoint& endpoint) {
    return endpoint.crossing * 4 + endpoint.strand;
}

Endpoint endpoint_from_key(int key) {
    return Endpoint{key / 4, key % 4};
}

int max_label(const PDCode& code) {
    int result = -1;
    for (const Crossing& crossing : code) {
        for (int label : crossing) {
            result = std::max(result, label);
        }
    }
    return result;
}

long long face_pair_key(int a, int b) {
    if (a > b) {
        std::swap(a, b);
    }
    return (static_cast<long long>(a) << 32) ^ static_cast<unsigned int>(b);
}

struct CrossingState {
    std::array<Endpoint, 4> adjacent{};
    bool directions[4][4]{};
    int sign = 0;
};

using LabelMap = std::unordered_map<int, std::vector<Endpoint>>;

LabelMap build_label_map(const PDCode& code) {
    LabelMap labels;
    for (int c = 0; c < static_cast<int>(code.size()); ++c) {
        for (int i = 0; i < 4; ++i) {
            labels[code[c][i]].push_back(Endpoint{c, i});
        }
    }
    for (const auto& item : labels) {
        if (item.second.size() != 2) {
            std::ostringstream message;
            message << "PD label " << item.first << " appears " << item.second.size()
                    << " times; each label must appear exactly twice";
            throw std::invalid_argument(message.str());
        }
    }
    return labels;
}

Endpoint mate_endpoint(const PDCode& code, const LabelMap& labels, const Endpoint& endpoint) {
    const int label = code.at(endpoint.crossing).at(endpoint.strand);
    const std::vector<Endpoint>& endpoints = labels.at(label);
    if (endpoints[0] == endpoint) {
        return endpoints[1];
    }
    if (endpoints[1] == endpoint) {
        return endpoints[0];
    }
    throw std::logic_error("Endpoint is not present in its label map entry");
}

void replace_label(PDCode& code, int old_label, int new_label) {
    if (old_label == new_label) {
        return;
    }
    for (Crossing& crossing : code) {
        for (int& label : crossing) {
            if (label == old_label) {
                label = new_label;
            }
        }
    }
}

void erase_crossings(PDCode& code, int first, int second = -1) {
    if (second >= 0 && first > second) {
        std::swap(first, second);
    }
    if (second >= 0) {
        code.erase(code.begin() + second);
    }
    code.erase(code.begin() + first);
}

int label_occurrences_outside(const PDCode& code, int label, const std::set<int>& removed_crossings) {
    int count = 0;
    for (int crossing_index = 0; crossing_index < static_cast<int>(code.size()); ++crossing_index) {
        if (removed_crossings.count(crossing_index) != 0) {
            continue;
        }
        for (int strand = 0; strand < 4; ++strand) {
            if (code[crossing_index][strand] == label) {
                ++count;
            }
        }
    }
    return count;
}

int unique_label_count(const Crossing& crossing) {
    return static_cast<int>(std::set<int>(crossing.begin(), crossing.end()).size());
}

std::vector<int> pd_value_set(const PDCode& code) {
    std::set<int> values;
    for (const Crossing& crossing : code) {
        values.insert(crossing.begin(), crossing.end());
    }
    return std::vector<int>(values.begin(), values.end());
}

bool contains_value(const std::vector<int>& values, int value) {
    return std::find(values.begin(), values.end(), value) != values.end();
}

void add_undirected_vector_edge(std::map<int, std::vector<int>>& graph, int a, int b) {
    std::vector<int>& first = graph[a];
    if (std::find(first.begin(), first.end(), b) == first.end()) {
        first.push_back(b);
    }
    std::vector<int>& second = graph[b];
    if (std::find(second.begin(), second.end(), a) == second.end()) {
        second.push_back(a);
    }
}

void add_undirected_set_edge(std::map<int, std::set<int>>& graph, int a, int b) {
    graph[a].insert(b);
    graph[b].insert(a);
}

std::map<int, std::vector<int>> pd_adjacency_vector(const PDCode& code) {
    std::map<int, std::vector<int>> graph;
    for (const Crossing& crossing : code) {
        add_undirected_vector_edge(graph, crossing[0], crossing[2]);
        add_undirected_vector_edge(graph, crossing[1], crossing[3]);
    }
    return graph;
}

PDCode renumber_r1_order(PDCode code) {
    if (code.empty()) {
        return code;
    }

    const std::vector<int> values = pd_value_set(code);
    const std::map<int, std::vector<int>> graph = pd_adjacency_vector(code);
    std::vector<int> visit_order;
    for (int value : values) {
        if (contains_value(visit_order, value)) {
            continue;
        }
        if (graph.find(value) == graph.end()) {
            throw std::runtime_error("Invalid PD graph during R1 renumbering");
        }
        visit_order.push_back(value);
        while (true) {
            const int current = visit_order.back();
            std::vector<int> neighbors = graph.at(current);
            std::sort(neighbors.begin(), neighbors.end());
            bool advanced = false;
            for (int next : neighbors) {
                if (!contains_value(visit_order, next)) {
                    visit_order.push_back(next);
                    advanced = true;
                    break;
                }
            }
            if (!advanced) {
                break;
            }
        }
    }

    std::map<int, int> new_label;
    for (int i = 0; i < static_cast<int>(visit_order.size()); ++i) {
        new_label[visit_order[i]] = i;
    }
    for (Crossing& crossing : code) {
        for (int& label : crossing) {
            label = new_label.at(label);
        }
    }
    return code;
}

PDCode erase_r1_moves(PDCode code, std::size_t& crossingless_components, int& moves) {
    if (!code.empty()) {
        build_label_map(code);
    }

    while (true) {
        bool changed = false;
        for (int crossing_index = 0; crossing_index < static_cast<int>(code.size()); ++crossing_index) {
            if (unique_label_count(code[crossing_index]) > 3) {
                continue;
            }

            const Crossing crossing = code[crossing_index];
            const ComponentAnalysis after_removal =
                analyze_components_after_removing_crossings(
                    code, std::vector<int>{crossing_index}, crossingless_components);
            code.erase(code.begin() + crossing_index);

            std::vector<int> singles;
            for (int label : crossing) {
                if (std::count(crossing.begin(), crossing.end(), label) == 1) {
                    singles.push_back(label);
                }
            }
            if (singles.size() == 2) {
                replace_label(code, singles[0], singles[1]);
            }

            crossingless_components = after_removal.crossingless_components;
            ++moves;
            changed = true;
            break;
        }

        if (!changed) {
            break;
        }
    }

    return renumber_r1_order(code);
}

std::map<int, std::set<int>> base_pd_graph(const PDCode& code) {
    std::map<int, std::set<int>> graph;
    for (int i = 0; i < static_cast<int>(code.size()); ++i) {
        const int crossing_node = -i - 1;
        for (int label : code[i]) {
            add_undirected_set_edge(graph, label, crossing_node);
        }
    }
    return graph;
}

int graph_component_count(const PDCode& code) {
    const std::map<int, std::set<int>> graph = base_pd_graph(code);
    std::set<int> visited;
    int count = 0;
    for (const auto& item : graph) {
        const int start = item.first;
        if (visited.count(start) != 0) {
            continue;
        }
        ++count;
        std::vector<int> stack(1, start);
        visited.insert(start);
        while (!stack.empty()) {
            const int node = stack.back();
            stack.pop_back();
            const auto found = graph.find(node);
            if (found == graph.end()) {
                continue;
            }
            for (int next : found->second) {
                if (visited.insert(next).second) {
                    stack.push_back(next);
                }
            }
        }
    }
    return count;
}

bool is_nugatory_crossing(const PDCode& code, int crossing_index) {
    if (unique_label_count(code[crossing_index]) != 4) {
        throw std::runtime_error("Nugatory check requires an R1-free PD code");
    }
    PDCode without = code;
    without.erase(without.begin() + crossing_index);
    return graph_component_count(without) > graph_component_count(code);
}

int find_nugatory_crossing(const PDCode& code) {
    for (int i = 0; i < static_cast<int>(code.size()); ++i) {
        if (is_nugatory_crossing(code, i)) {
            return i;
        }
    }
    return -1;
}

void add_pre_next_edge(std::map<int, int>& previous, std::map<int, int>& next, int a, int b) {
    int previous_value = 0;
    int next_value = 0;
    if (std::abs(a - b) == 1) {
        previous_value = a < b ? a : b;
        next_value = a < b ? b : a;
    } else {
        previous_value = a < b ? b : a;
        next_value = a < b ? a : b;
    }
    previous[next_value] = previous_value;
    next[previous_value] = next_value;
}

std::pair<std::map<int, int>, std::map<int, int>> pre_next_maps(const PDCode& code) {
    if (!code.empty()) {
        build_label_map(code);
    }

    std::map<int, int> previous;
    std::map<int, int> next;
    for (const Crossing& crossing : code) {
        if (unique_label_count(crossing) > 2) {
            add_pre_next_edge(previous, next, crossing[0], crossing[2]);
            add_pre_next_edge(previous, next, crossing[1], crossing[3]);
        } else {
            std::vector<int> values(crossing.begin(), crossing.end());
            std::sort(values.begin(), values.end());
            values.erase(std::unique(values.begin(), values.end()), values.end());
            if (values.size() != 2) {
                throw std::runtime_error("Invalid two-value crossing in pre/next maps");
            }
            previous[values[0]] = values[1];
            next[values[0]] = values[1];
            previous[values[1]] = values[0];
            next[values[1]] = values[0];
        }
    }

    for (int label : pd_value_set(code)) {
        if (previous.count(label) == 0) {
            if (next.count(label) == 0) {
                throw std::runtime_error("Broken PD pre/next map");
            }
            previous[label] = next[label];
        }
        if (next.count(label) == 0) {
            next[label] = previous[label];
        }
    }
    return std::make_pair(previous, next);
}

PDCode replace_arc_value(PDCode code, int from, int to) {
    replace_label(code, from, to);
    return code;
}

PDCode renumber_full_dfs(PDCode code) {
    if (code.empty()) {
        return code;
    }

    const std::vector<int> values = pd_value_set(code);
    std::map<int, std::set<int>> graph;
    for (const Crossing& crossing : code) {
        add_undirected_set_edge(graph, crossing[0], crossing[2]);
        add_undirected_set_edge(graph, crossing[1], crossing[3]);
    }

    std::set<int> visited;
    std::map<int, int> new_label;
    for (int start : values) {
        if (visited.count(start) != 0) {
            continue;
        }
        std::vector<int> stack(1, start);
        while (!stack.empty()) {
            const int value = stack.back();
            stack.pop_back();
            if (visited.count(value) != 0) {
                continue;
            }
            const auto found = graph.find(value);
            if (found == graph.end()) {
                throw std::runtime_error("Invalid PD graph during renumbering");
            }
            new_label[value] = static_cast<int>(visited.size());
            visited.insert(value);
            for (std::set<int>::const_reverse_iterator it = found->second.rbegin();
                 it != found->second.rend();
                 ++it) {
                if (visited.count(*it) == 0) {
                    stack.push_back(*it);
                }
            }
        }
    }

    if (new_label.size() != values.size()) {
        throw std::runtime_error("PD renumbering failed");
    }
    for (Crossing& crossing : code) {
        for (int& label : crossing) {
            label = new_label.at(label);
        }
    }
    return code;
}

PDCode erase_one_nugatory_crossing(
    PDCode code,
    int crossing_index,
    std::size_t& crossingless_components,
    int& moves) {
    if (unique_label_count(code[crossing_index]) != 4) {
        throw std::runtime_error("Nugatory erase requires an R1-free PD code");
    }

    const Crossing crossing = code[crossing_index];
    const int ax = crossing[0];
    const int bx = crossing[1];
    const int cx = crossing[2];
    const int dx = crossing[3];
    const std::map<int, int> next = pre_next_maps(code).second;

    std::vector<int> loop(1, ax);
    const std::size_t guard = pd_value_set(code).size() + 1;
    while (true) {
        const auto found = next.find(loop.back());
        if (found == next.end()) {
            throw std::runtime_error("Broken loop while erasing nugatory crossing");
        }
        const int next_label = found->second;
        loop.push_back(next_label);
        if (next_label == ax) {
            loop.pop_back();
            break;
        }
        if (loop.size() > guard) {
            throw std::runtime_error("Failed to close PD loop while erasing nugatory crossing");
        }
    }

    const std::set<int> loop_set(loop.begin(), loop.end());
    if (loop_set.count(ax) == 0 || loop_set.count(bx) == 0 ||
        loop_set.count(cx) == 0 || loop_set.count(dx) == 0) {
        throw std::runtime_error("Nugatory crossing arcs are not in one component");
    }

    const ComponentAnalysis after_removal =
        analyze_components_after_removing_crossings(
            code, std::vector<int>{crossing_index}, crossingless_components);

    code.erase(code.begin() + crossing_index);
    code = replace_arc_value(code, ax, cx);
    code = replace_arc_value(code, dx, bx);
    crossingless_components = after_removal.crossingless_components;
    ++moves;
    return renumber_full_dfs(code);
}

std::vector<std::vector<int>> raw_faces_from_pd_code(const PDCode& code) {
    const LabelMap labels = build_label_map(code);
    const int endpoint_count = static_cast<int>(code.size() * 4);
    std::vector<char> present(endpoint_count, true);
    int remaining = endpoint_count;
    std::vector<std::vector<int>> faces;

    while (remaining > 0) {
        int first_key = -1;
        for (int key = endpoint_count - 1; key >= 0; --key) {
            if (present[key]) {
                first_key = key;
                break;
            }
        }
        if (first_key < 0) {
            break;
        }

        std::vector<int> face;
        Endpoint first = endpoint_from_key(first_key);
        Endpoint current = first;
        present[first_key] = false;
        --remaining;
        face.push_back(first_key);

        while (true) {
            const Endpoint next_corner{
                current.crossing,
                (current.strand + 1) % 4};
            const Endpoint next = mate_endpoint(code, labels, next_corner);
            if (next == first) {
                faces.push_back(std::move(face));
                break;
            }
            const int next_key = endpoint_key(next);
            if (present[next_key]) {
                present[next_key] = false;
                --remaining;
            }
            face.push_back(next_key);
            current = next;
        }
    }

    return faces;
}

struct Diagram {
    PDCode code;
    std::vector<CrossingState> crossings;

    explicit Diagram(PDCode input) : code(std::move(input)), crossings(code.size()) {
        build_adjacency();
        auto starts = component_starts_from_pd();
        orient_crossings(starts);
    }

    Endpoint opposite(const Endpoint& endpoint) const {
        return crossings.at(endpoint.crossing).adjacent.at(endpoint.strand);
    }

    Endpoint next(const Endpoint& endpoint) const {
        return crossings.at(endpoint.crossing).adjacent.at((endpoint.strand + 2) % 4);
    }

    Endpoint next_corner(const Endpoint& endpoint) const {
        return crossings.at(endpoint.crossing).adjacent.at((endpoint.strand + 1) % 4);
    }

    Endpoint rotate_endpoint(const Endpoint& endpoint, int offset) const {
        return Endpoint{endpoint.crossing, positive_mod(endpoint.strand + offset, 4)};
    }

    std::vector<Endpoint> crossing_entries() const {
        std::vector<Endpoint> entries;
        entries.reserve(crossings.size() * 2);
        for (int c = 0; c < static_cast<int>(crossings.size()); ++c) {
            if (crossings[c].sign == -1) {
                entries.push_back(Endpoint{c, 0});
                entries.push_back(Endpoint{c, 1});
            } else if (crossings[c].sign == 1) {
                entries.push_back(Endpoint{c, 0});
                entries.push_back(Endpoint{c, 3});
            } else {
                throw std::logic_error("Crossing was not oriented");
            }
        }
        return entries;
    }

private:
    void build_adjacency() {
        std::map<int, std::vector<Endpoint>> gluings;
        for (int c = 0; c < static_cast<int>(code.size()); ++c) {
            for (int i = 0; i < 4; ++i) {
                gluings[code[c][i]].push_back(Endpoint{c, i});
            }
        }

        for (std::map<int, std::vector<Endpoint>>::const_iterator it = gluings.begin();
             it != gluings.end();
             ++it) {
            const int label = it->first;
            const std::vector<Endpoint>& endpoints = it->second;
            if (endpoints.size() != 2) {
                std::ostringstream message;
                message << "PD label " << label << " appears " << endpoints.size()
                        << " times; each label must appear exactly twice";
                throw std::invalid_argument(message.str());
            }
            const Endpoint a = endpoints[0];
            const Endpoint b = endpoints[1];
            crossings[a.crossing].adjacent[a.strand] = b;
            crossings[b.crossing].adjacent[b.strand] = a;
        }
    }

    std::vector<Endpoint> component_starts_from_pd() const {
        std::set<int> labels;
        std::map<int, std::vector<Endpoint>> gluings;
        for (int c = 0; c < static_cast<int>(code.size()); ++c) {
            for (int i = 0; i < 4; ++i) {
                labels.insert(code[c][i]);
                gluings[code[c][i]].push_back(Endpoint{c, i});
            }
        }

        std::vector<Endpoint> starts;
        while (!labels.empty()) {
            const int m = *labels.begin();
            labels.erase(labels.begin());
            const auto& gluing = gluings.at(m);
            const Endpoint first = gluing[0];
            const Endpoint second = gluing[1];

            Endpoint direction;
            int next_label = m;

            if (first.crossing == second.crossing) {
                std::set<int> crossing_labels(code[first.crossing].begin(), code[first.crossing].end());
                crossing_labels.erase(m);
                if (crossing_labels.empty()) {
                    throw std::invalid_argument("A PD self-loop crossing must have another label");
                }
                next_label = *crossing_labels.begin();
                direction = Endpoint{first.crossing, index_of_label(first.crossing, next_label)};
            } else {
                const int j1 = (first.strand + 2) % 4;
                const int j2 = (second.strand + 2) % 4;
                const int l1 = code[first.crossing][j1];
                const int l2 = code[second.crossing][j2];
                if (l1 < l2) {
                    next_label = l1;
                    direction = Endpoint{first.crossing, j1};
                } else if (l2 < l1) {
                    next_label = l2;
                    direction = Endpoint{second.crossing, j2};
                } else {
                    next_label = l1;
                    if (code[second.crossing][0] == l1 || code[first.crossing][0] == m) {
                        direction = Endpoint{first.crossing, j1};
                    } else {
                        direction = Endpoint{second.crossing, j2};
                    }
                }
            }

            starts.push_back(direction);
            while (next_label != m) {
                auto removed = labels.erase(next_label);
                if (removed == 0) {
                    throw std::invalid_argument("PD component traversal encountered a repeated label");
                }
                const auto& next_gluing = gluings.at(next_label);
                const int index = next_gluing[0] == direction ? 0 : (next_gluing[1] == direction ? 1 : -1);
                if (index == -1) {
                    throw std::invalid_argument("PD component traversal lost its current endpoint");
                }
                const Endpoint other = next_gluing[1 - index];
                direction = Endpoint{other.crossing, (other.strand + 2) % 4};
                next_label = code[direction.crossing][direction.strand];
            }
        }

        return starts;
    }

    int index_of_label(int crossing, int label) const {
        for (int i = 0; i < 4; ++i) {
            if (code[crossing][i] == label) {
                return i;
            }
        }
        throw std::logic_error("Label was not present at the requested crossing");
    }

    void make_tail(int crossing, int strand) {
        const int head = (strand + 2) % 4;
        if (crossings[crossing].directions[head][strand]) {
            throw std::invalid_argument("The same crossing strand was oriented twice");
        }
        crossings[crossing].directions[strand][head] = true;
    }

    void orient_crossings(std::vector<Endpoint> starts) {
        std::set<int> remaining;
        for (int c = 0; c < static_cast<int>(crossings.size()); ++c) {
            for (int i = 0; i < 4; ++i) {
                remaining.insert(endpoint_key(Endpoint{c, i}));
            }
        }

        while (!remaining.empty()) {
            Endpoint start;
            if (!starts.empty()) {
                start = starts.back();
                starts.pop_back();
            } else {
                start = endpoint_from_key(*remaining.begin());
            }

            Endpoint current = start;
            while (true) {
                const Endpoint other = crossings[current.crossing].adjacent[current.strand];
                make_tail(other.crossing, other.strand);
                remaining.erase(endpoint_key(current));
                remaining.erase(endpoint_key(other));
                current = Endpoint{other.crossing, (other.strand + 2) % 4};
                if (current == start) {
                    break;
                }
            }
        }

        for (int c = 0; c < static_cast<int>(crossings.size()); ++c) {
            orient_crossing(c);
        }
    }

    void orient_crossing(int crossing) {
        if (crossings[crossing].directions[2][0]) {
            rotate_crossing_180(crossing);
        }

        if (crossings[crossing].directions[3][1]) {
            crossings[crossing].sign = 1;
        } else if (crossings[crossing].directions[1][3]) {
            crossings[crossing].sign = -1;
        } else {
            throw std::invalid_argument("Could not determine crossing sign from PD orientation");
        }
    }

    void rotate_crossing_180(int crossing) {
        auto old_adjacent = crossings[crossing].adjacent;
        bool old_directions[4][4]{};
        for (int a = 0; a < 4; ++a) {
            for (int b = 0; b < 4; ++b) {
                old_directions[a][b] = crossings[crossing].directions[a][b];
                crossings[crossing].directions[a][b] = false;
            }
        }

        for (int i = 0; i < 4; ++i) {
            const Endpoint other = old_adjacent[(i + 2) % 4];
            if (other.crossing != crossing) {
                crossings[other.crossing].adjacent[other.strand] = Endpoint{crossing, i};
                crossings[crossing].adjacent[i] = other;
            } else {
                crossings[crossing].adjacent[i] = Endpoint{crossing, positive_mod(other.strand - 2, 4)};
            }
        }

        for (int a = 0; a < 4; ++a) {
            for (int b = 0; b < 4; ++b) {
                if (old_directions[a][b]) {
                    crossings[crossing].directions[(a + 2) % 4][(b + 2) % 4] = true;
                }
            }
        }
    }
};

struct GraphEdge {
    int u = -1;
    int v = -1;
    int interface_u = -1;
    int interface_v = -1;
    int weight = 1;
};

struct DualGraph {
    std::vector<int> edge_to_face;
    std::vector<int> face_assignment_order;
    std::vector<std::vector<int>> faces;
    std::vector<GraphEdge> edges;
    std::vector<std::vector<int>> adjacency;
    std::unordered_map<long long, int> edge_by_faces;

    explicit DualGraph(const Diagram& diagram) {
        build_faces(diagram);
        build_edges(diagram);
    }

    int edge_index(int a, int b) const {
        const auto found = edge_by_faces.find(face_pair_key(a, b));
        if (found == edge_by_faces.end()) {
            return -1;
        }
        return found->second;
    }

    const GraphEdge* edge(int a, int b) const {
        const int index = edge_index(a, b);
        if (index < 0) {
            return nullptr;
        }
        return &edges[index];
    }

    GraphEdge* mutable_edge(int a, int b) {
        const int index = edge_index(a, b);
        if (index < 0) {
            return nullptr;
        }
        return &edges[index];
    }

    int interface_for_face(const GraphEdge& edge, int face) const {
        if (edge.u == face) {
            return edge.interface_u;
        }
        if (edge.v == face) {
            return edge.interface_v;
        }
        throw std::logic_error("Face is not incident to the requested dual edge");
    }

private:
    void build_faces(const Diagram& diagram) {
        const int endpoint_count = static_cast<int>(diagram.crossings.size() * 4);
        edge_to_face.assign(endpoint_count, -1);
        std::vector<char> present(endpoint_count, true);
        int remaining = endpoint_count;

        while (remaining > 0) {
            int first_key = -1;
            for (int key = endpoint_count - 1; key >= 0; --key) {
                if (present[key]) {
                    first_key = key;
                    break;
                }
            }
            if (first_key == -1) {
                break;
            }

            const int face_index = static_cast<int>(faces.size());
            std::vector<int> face;
            Endpoint first = endpoint_from_key(first_key);
            Endpoint current = first;
            present[first_key] = false;
            --remaining;
            edge_to_face[first_key] = face_index;
            face_assignment_order.push_back(first_key);
            face.push_back(first_key);

            while (true) {
                Endpoint next = diagram.next_corner(current);
                if (next == first) {
                    faces.push_back(std::move(face));
                    break;
                }
                const int next_key = endpoint_key(next);
                edge_to_face[next_key] = face_index;
                face_assignment_order.push_back(next_key);
                if (present[next_key]) {
                    present[next_key] = false;
                    --remaining;
                }
                face.push_back(next_key);
                current = next;
            }
        }
    }

    void build_edges(const Diagram& diagram) {
        adjacency.assign(faces.size(), {});
        for (int key : face_assignment_order) {
            const Endpoint endpoint = endpoint_from_key(key);
            const Endpoint opposite = diagram.opposite(endpoint);
            const int opposite_key = endpoint_key(opposite);
            const int face = edge_to_face[key];
            const int neighbor = edge_to_face[opposite_key];
            if (face >= neighbor) {
                continue;
            }

            const long long pair_key = face_pair_key(face, neighbor);
            const auto found = edge_by_faces.find(pair_key);
            if (found == edge_by_faces.end()) {
                GraphEdge edge;
                edge.u = face;
                edge.v = neighbor;
                edge.interface_u = key;
                edge.interface_v = opposite_key;
                edge.weight = 1;
                const int edge_index = static_cast<int>(edges.size());
                edge_by_faces[pair_key] = edge_index;
                edges.push_back(edge);
                adjacency[face].push_back(edge_index);
                adjacency[neighbor].push_back(edge_index);
            } else {
                GraphEdge& edge = edges[found->second];
                if (edge.u == face) {
                    edge.interface_u = key;
                    edge.interface_v = opposite_key;
                } else {
                    edge.interface_u = opposite_key;
                    edge.interface_v = key;
                }
            }
        }
    }
};

enum class Level {
    Under,
    Over
};

std::string level_to_string(Level level) {
    return level == Level::Under ? "under" : "over";
}

Level opposite_level(Level level) {
    return level == Level::Under ? Level::Over : Level::Under;
}

std::vector<std::vector<Endpoint>> possible_red_lines(const Diagram& diagram) {
    std::vector<std::vector<Endpoint>> long_lines;
    std::vector<Endpoint> entries = diagram.crossing_entries();

    while (!entries.empty()) {
        std::vector<Endpoint> red_line;
        Endpoint endpoint = entries.back();
        entries.pop_back();
        red_line.push_back(endpoint);
        std::unordered_set<int> crossings;
        crossings.insert(endpoint.crossing);

        while (true) {
            endpoint = diagram.next(endpoint);
            red_line.push_back(endpoint);
            if (crossings.count(endpoint.crossing) != 0) {
                break;
            }
            crossings.insert(endpoint.crossing);
        }
        long_lines.push_back(std::move(red_line));
    }

    std::vector<std::vector<Endpoint>> candidates;
    for (const auto& line : long_lines) {
        if (line.size() < 3) {
            continue;
        }
        for (std::size_t i = 0; i < line.size() - 2; ++i) {
            candidates.emplace_back(line.begin(), line.end() - static_cast<std::ptrdiff_t>(i));
        }
    }
    return candidates;
}

std::vector<LinkComponentSummary> component_summaries(const Diagram& diagram) {
    std::set<int> remaining_entries;
    const std::vector<Endpoint> entries = diagram.crossing_entries();
    for (const Endpoint& endpoint : entries) {
        remaining_entries.insert(endpoint_key(endpoint));
    }

    std::vector<LinkComponentSummary> summaries;
    while (!remaining_entries.empty()) {
        Endpoint start = endpoint_from_key(*remaining_entries.rbegin());
        Endpoint current = start;
        std::set<int> crossing_set;

        while (true) {
            remaining_entries.erase(endpoint_key(current));
            crossing_set.insert(current.crossing);
            current = diagram.next(current);
            if (current == start) {
                break;
            }
        }

        LinkComponentSummary summary;
        summary.crossing_indices.assign(crossing_set.begin(), crossing_set.end());
        summaries.push_back(std::move(summary));
    }

    return summaries;
}

std::set<int> normalized_removed_crossings(const PDCode& code, const std::vector<int>& removed_crossings) {
    std::set<int> removed;
    for (int crossing : removed_crossings) {
        if (crossing < 0 || crossing >= static_cast<int>(code.size())) {
            std::ostringstream message;
            message << "Removed crossing index " << crossing << " is out of range";
            throw std::invalid_argument(message.str());
        }
        removed.insert(crossing);
    }
    return removed;
}

void reset_weights(DualGraph& graph) {
    for (auto& edge : graph.edges) {
        edge.weight = 1;
    }
}

std::vector<int> heuristic_distances_to_target(
    const DualGraph& graph,
    int target,
    int cutoff);

void collect_simple_paths_dfs(
    const DualGraph& graph,
    int current,
    int target,
    int cutoff,
    int max_paths,
    int current_weight,
    const std::vector<int>& distance,
    std::vector<char>& visited,
    std::vector<int>& current_path,
    std::vector<std::vector<int>>& paths) {
    const int infinity = std::numeric_limits<int>::max() / 4;
    if (static_cast<int>(current_path.size()) - 1 >= cutoff) {
        return;
    }
    if (current < 0 || current >= static_cast<int>(distance.size()) ||
        distance[current] == infinity || current_weight + distance[current] >= cutoff) {
        return;
    }

    for (int edge_index : graph.adjacency[current]) {
        const GraphEdge& edge = graph.edges[edge_index];
        const int next = edge.u == current ? edge.v : edge.u;
        if (visited[next]) {
            continue;
        }
        const int next_weight = current_weight + edge.weight;
        if (next_weight >= cutoff) {
            continue;
        }
        if (next < 0 || next >= static_cast<int>(distance.size()) ||
            distance[next] == infinity || next_weight + distance[next] >= cutoff) {
            continue;
        }

        current_path.push_back(next);
        visited[next] = true;

        if (next == target) {
            paths.push_back(current_path);
            if (max_paths != -1 && static_cast<int>(paths.size()) > max_paths) {
                visited[next] = false;
                current_path.pop_back();
                return;
            }
        } else {
            collect_simple_paths_dfs(
                graph,
                next,
                target,
                cutoff,
                max_paths,
                next_weight,
                distance,
                visited,
                current_path,
                paths);
            if (max_paths != -1 && static_cast<int>(paths.size()) > max_paths) {
                visited[next] = false;
                current_path.pop_back();
                return;
            }
        }

        visited[next] = false;
        current_path.pop_back();
    }
}

std::vector<std::vector<int>> collect_simple_paths(
    const DualGraph& graph,
    int source,
    int target,
    int cutoff,
    int max_paths) {
    std::vector<std::vector<int>> paths;
    if (source == target || source < 0 || target < 0 ||
        source >= static_cast<int>(graph.faces.size()) ||
        target >= static_cast<int>(graph.faces.size()) ||
        cutoff <= 0) {
        return paths;
    }

    std::vector<char> visited(graph.faces.size(), false);
    std::vector<int> current_path{source};
    const std::vector<int> distance = heuristic_distances_to_target(graph, target, cutoff);
    visited[source] = true;
    collect_simple_paths_dfs(
        graph,
        source,
        target,
        cutoff,
        max_paths,
        0,
        distance,
        visited,
        current_path,
        paths);
    return paths;
}

std::vector<int> heuristic_distances_to_target(
    const DualGraph& graph,
    int target,
    int cutoff) {
    const int face_count = static_cast<int>(graph.faces.size());
    const int infinity = std::numeric_limits<int>::max() / 4;
    std::vector<int> distance(face_count, infinity);
    std::deque<int> queue;
    distance[target] = 0;
    queue.push_back(target);

    while (!queue.empty()) {
        const int current = queue.front();
        queue.pop_front();
        for (int edge_index : graph.adjacency[current]) {
            const GraphEdge& edge = graph.edges[edge_index];
            if (edge.weight >= cutoff) {
                continue;
            }
            const int next = edge.u == current ? edge.v : edge.u;
            if (distance[next] != infinity) {
                continue;
            }
            distance[next] = distance[current] + 1;
            queue.push_back(next);
        }
    }
    return distance;
}

struct HeuristicState {
    std::vector<int> path;
    std::vector<char> visited;
    int weight = 0;
    int branch_penalty = 0;
    int estimated_weight = 0;
    int estimated_length = 0;
    int serial = 0;
};

struct HeuristicStateWorse {
    bool operator()(const HeuristicState& lhs, const HeuristicState& rhs) const {
        if (lhs.estimated_weight != rhs.estimated_weight) {
            return lhs.estimated_weight > rhs.estimated_weight;
        }
        if (lhs.estimated_length != rhs.estimated_length) {
            return lhs.estimated_length > rhs.estimated_length;
        }
        if (lhs.branch_penalty != rhs.branch_penalty) {
            return lhs.branch_penalty > rhs.branch_penalty;
        }
        if (lhs.weight != rhs.weight) {
            return lhs.weight > rhs.weight;
        }
        if (lhs.path.size() != rhs.path.size()) {
            return lhs.path.size() > rhs.path.size();
        }
        return lhs.serial > rhs.serial;
    }
};

struct HeuristicStep {
    int next = -1;
    int edge_index = -1;
    int edge_weight = 0;
    int distance = 0;
    int degree_penalty = 0;
};

bool heuristic_step_less(const HeuristicStep& lhs, const HeuristicStep& rhs) {
    if (lhs.edge_weight != rhs.edge_weight) {
        return lhs.edge_weight < rhs.edge_weight;
    }
    if (lhs.distance != rhs.distance) {
        return lhs.distance < rhs.distance;
    }
    if (lhs.degree_penalty != rhs.degree_penalty) {
        return lhs.degree_penalty < rhs.degree_penalty;
    }
    if (lhs.next != rhs.next) {
        return lhs.next < rhs.next;
    }
    return lhs.edge_index < rhs.edge_index;
}

std::vector<std::vector<int>> collect_heuristic_paths(
    const DualGraph& graph,
    int source,
    int target,
    int cutoff) {
    std::vector<std::vector<int>> paths;
    const int face_count = static_cast<int>(graph.faces.size());
    if (source == target || source < 0 || target < 0 ||
        source >= face_count || target >= face_count || cutoff <= 0) {
        return paths;
    }

    const std::vector<int> distance = heuristic_distances_to_target(graph, target, cutoff);
    const int infinity = std::numeric_limits<int>::max() / 4;
    if (distance[source] == infinity || distance[source] >= cutoff) {
        return paths;
    }

    const int state_budget = std::max(
        kHeuristicMinStateBudget,
        std::min(kHeuristicMaxStateBudget, face_count * std::max(1, cutoff) * 8));
    const int path_budget = std::max(
        kHeuristicMinPathBudget,
        std::min(kHeuristicMaxPathBudget, face_count * 2 + cutoff * 8));

    std::priority_queue<HeuristicState, std::vector<HeuristicState>, HeuristicStateWorse> queue;
    int serial = 0;
    HeuristicState initial;
    initial.path.push_back(source);
    initial.visited.assign(face_count, false);
    initial.visited[source] = true;
    initial.estimated_weight = distance[source];
    initial.estimated_length = distance[source];
    initial.serial = serial++;
    queue.push(initial);

    std::map<long long, int> popped_by_depth_face;
    int popped_states = 0;
    while (!queue.empty() && popped_states < state_budget &&
           static_cast<int>(paths.size()) < path_budget) {
        HeuristicState state = queue.top();
        queue.pop();
        ++popped_states;

        const int current = state.path.back();
        const int depth = static_cast<int>(state.path.size()) - 1;
        if (current == target) {
            if (state.weight < cutoff) {
                paths.push_back(state.path);
            }
            continue;
        }
        if (depth >= cutoff - 1) {
            continue;
        }

        const long long beam_key =
            static_cast<long long>(depth) * static_cast<long long>(face_count) + current;
        int& beam_count = popped_by_depth_face[beam_key];
        if (beam_count >= kHeuristicBeamWidth) {
            continue;
        }
        ++beam_count;

        std::vector<HeuristicStep> steps;
        for (int edge_index : graph.adjacency[current]) {
            const GraphEdge& edge = graph.edges[edge_index];
            const int next = edge.u == current ? edge.v : edge.u;
            if (state.visited[next]) {
                continue;
            }
            if (distance[next] == infinity) {
                continue;
            }
            const int new_weight = state.weight + edge.weight;
            if (new_weight >= cutoff) {
                continue;
            }
            const int new_depth = depth + 1;
            if (new_depth + distance[next] >= cutoff) {
                continue;
            }
            HeuristicStep step;
            step.next = next;
            step.edge_index = edge_index;
            step.edge_weight = edge.weight;
            step.distance = distance[next];
            step.degree_penalty = std::max(0, static_cast<int>(graph.adjacency[next].size()) - 2);
            steps.push_back(step);
        }
        std::sort(steps.begin(), steps.end(), heuristic_step_less);

        for (const HeuristicStep& step : steps) {
            const GraphEdge& edge = graph.edges[step.edge_index];
            HeuristicState next_state;
            next_state.path = state.path;
            next_state.path.push_back(step.next);
            next_state.visited = state.visited;
            next_state.visited[step.next] = true;
            next_state.weight = state.weight + edge.weight;
            next_state.branch_penalty = state.branch_penalty + step.degree_penalty;
            next_state.estimated_weight = next_state.weight + distance[step.next];
            next_state.estimated_length =
                static_cast<int>(next_state.path.size()) - 1 + distance[step.next];
            next_state.serial = serial++;
            queue.push(std::move(next_state));
        }
    }

    return paths;
}

bool contains_endpoint_key(const std::vector<int>& endpoints, int key) {
    return std::find(endpoints.begin(), endpoints.end(), key) != endpoints.end();
}

bool do_check(
    const Diagram& diagram,
    const DualGraph& graph,
    const std::vector<Endpoint>& red_path,
    const std::vector<int>& green_path,
    Direction direction,
    SimplificationResult& result) {
    std::vector<int> green_left_cross;
    green_left_cross.reserve(green_path.size());

    for (std::size_t i = 0; i + 1 < green_path.size(); ++i) {
        const int f1 = green_path[i];
        const int f2 = green_path[i + 1];
        const GraphEdge* edge = graph.edge(f1, f2);
        if (edge == nullptr) {
            return false;
        }
        const int face_for_interface = direction == Direction::Right ? f1 : f2;
        green_left_cross.push_back(graph.interface_for_face(*edge, face_for_interface));
    }

    std::unordered_set<int> red_boundary_crossings;
    std::deque<int> to_check;
    std::unordered_set<int> queued;
    std::unordered_map<int, Level> check_result;

    auto enqueue = [&](int key) {
        if (queued.insert(key).second) {
            to_check.push_back(key);
        }
    };

    auto erase_queued = [&](int key) {
        auto found = queued.find(key);
        if (found != queued.end()) {
            queued.erase(found);
            auto it = std::find(to_check.begin(), to_check.end(), key);
            if (it != to_check.end()) {
                to_check.erase(it);
            }
        }
    };

    for (std::size_t i = 0; i + 1 < red_path.size(); ++i) {
        const Endpoint red_endpoint = red_path[i];
        red_boundary_crossings.insert(red_endpoint.crossing);
        const int offset = direction == Direction::Right ? 3 : 1;
        const Endpoint cross_strand = diagram.rotate_endpoint(red_endpoint, offset);
        const int key = endpoint_key(cross_strand);
        enqueue(key);
        check_result[key] = (cross_strand.strand % 2 == 0) ? Level::Under : Level::Over;
    }

    std::vector<GreenCrossing> green_crossings;
    std::unordered_map<int, int> green_index;
    for (int i = 0; i < static_cast<int>(green_path.size()); ++i) {
        green_index[green_path[i]] = i;
    }

    bool good_path = true;
    while (!to_check.empty() && good_path) {
        const int start_key = to_check.back();
        to_check.pop_back();
        queued.erase(start_key);
        Endpoint cross_strand = endpoint_from_key(start_key);

        while (true) {
            const int cross_key = endpoint_key(cross_strand);
            const Level current_level = check_result.at(cross_key);
            const Endpoint opposite = diagram.opposite(cross_strand);
            const int opposite_key = endpoint_key(opposite);
            const auto opposite_result = check_result.find(opposite_key);
            if (opposite_result != check_result.end() && opposite_result->second != current_level) {
                good_path = false;
                break;
            }

            if (contains_endpoint_key(green_left_cross, cross_key)) {
                const int f1 = graph.edge_to_face[cross_key];
                const int f2 = graph.edge_to_face[opposite_key];
                const auto f1_index = green_index.find(f1);
                const auto f2_index = green_index.find(f2);
                if (f1_index == green_index.end() || f2_index == green_index.end()) {
                    good_path = false;
                    break;
                }
                const bool forward = f1_index->second < f2_index->second;
                GreenCrossing green_crossing;
                green_crossing.from_face = forward ? f1 : f2;
                green_crossing.to_face = forward ? f2 : f1;
                green_crossing.strand_level = level_to_string(opposite_level(current_level));
                green_crossings.push_back(std::move(green_crossing));
                break;
            }

            check_result[opposite_key] = current_level;
            erase_queued(opposite_key);

            if (red_boundary_crossings.count(opposite.crossing) != 0) {
                break;
            }

            cross_strand = opposite;
            const Endpoint side1 = diagram.rotate_endpoint(cross_strand, 1);
            const Endpoint side2 = diagram.rotate_endpoint(cross_strand, 3);
            const int side1_key = endpoint_key(side1);
            const int side2_key = endpoint_key(side2);

            if (cross_strand.strand % 2 == 1 && current_level == Level::Under) {
                auto first = check_result.find(side1_key);
                auto second = check_result.find(side2_key);
                if ((first != check_result.end() && first->second == Level::Over) ||
                    (second != check_result.end() && second->second == Level::Over)) {
                    good_path = false;
                    break;
                }
                if (first == check_result.end()) {
                    check_result[side1_key] = Level::Under;
                    enqueue(side1_key);
                }
                if (second == check_result.end()) {
                    check_result[side2_key] = Level::Under;
                    enqueue(side2_key);
                }
            }

            if (cross_strand.strand % 2 == 0 && current_level == Level::Over) {
                auto first = check_result.find(side1_key);
                auto second = check_result.find(side2_key);
                if ((first != check_result.end() && first->second == Level::Under) ||
                    (second != check_result.end() && second->second == Level::Under)) {
                    good_path = false;
                    break;
                }
                if (first == check_result.end()) {
                    check_result[side1_key] = Level::Over;
                    enqueue(side1_key);
                }
                if (second == check_result.end()) {
                    check_result[side2_key] = Level::Over;
                    enqueue(side2_key);
                }
            }

            const Endpoint across_same_crossing = diagram.rotate_endpoint(cross_strand, 2);
            const int across_key = endpoint_key(across_same_crossing);
            check_result[across_key] = current_level;
            cross_strand = across_same_crossing;
        }
    }

    if (!good_path) {
        return false;
    }

    result.found = true;
    result.direction = direction;
    result.red_path = red_path;
    result.green_path = green_path;
    result.green_crossings = std::move(green_crossings);
    return true;
}

class DisjointSet {
public:
    int find(int value) {
        auto inserted = parent_.insert(std::make_pair(value, value));
        int parent = inserted.first->second;
        if (parent != value) {
            parent = find(parent);
            parent_[value] = parent;
        }
        return parent;
    }

    void unite(int first, int second) {
        int first_root = find(first);
        int second_root = find(second);
        if (first_root == second_root) {
            return;
        }
        if (second_root < first_root) {
            std::swap(first_root, second_root);
        }
        parent_[second_root] = first_root;
    }

private:
    std::map<int, int> parent_;
};

std::map<std::pair<int, int>, std::string> green_crossing_levels(
    const SimplificationResult& result) {
    std::map<std::pair<int, int>, std::string> levels;
    for (const GreenCrossing& crossing : result.green_crossings) {
        levels[std::make_pair(crossing.from_face, crossing.to_face)] = crossing.strand_level;
    }
    return levels;
}

void clear_witness(SimplificationResult& result) {
    result.found = false;
    result.red_path.clear();
    result.green_path.clear();
    result.green_crossings.clear();
}

}  // namespace

MidSimplificationApplyResult apply_simplification_witness(
    const PDCode& code,
    const SimplificationResult& result,
    std::size_t known_crossingless_components) {
    if (!result.found) {
        throw std::invalid_argument("Cannot apply a missing simplification witness");
    }
    if (result.red_path.size() < 2) {
        throw std::invalid_argument("Simplification witness red path is too short");
    }

    Diagram diagram(code);
    DualGraph graph(diagram);
    std::set<int> removed_crossings;
    std::map<int, int> red_entry_by_crossing;
    for (std::size_t i = 0; i + 1 < result.red_path.size(); ++i) {
        removed_crossings.insert(result.red_path[i].crossing);
        red_entry_by_crossing[result.red_path[i].crossing] = result.red_path[i].strand;
    }
    if (removed_crossings.size() != result.red_path.size() - 1) {
        throw std::invalid_argument("Simplification witness repeats a removed red crossing");
    }
    if (removed_crossings.count(result.red_path.back().crossing) != 0) {
        throw std::invalid_argument("Simplification witness ends inside the removed red arc");
    }

    const std::map<std::pair<int, int>, std::string> levels = green_crossing_levels(result);
    DisjointSet dsu;
    const int endpoint_count = static_cast<int>(code.size() * 4);
    const int new_crossing_count =
        result.green_path.empty() ? 0 : static_cast<int>(result.green_path.size()) - 1;
    const int new_base = endpoint_count;

    auto new_node = [&](int crossing_index, int strand) {
        return new_base + crossing_index * 4 + strand;
    };
    auto is_removed_node = [&](int node) {
        return node < endpoint_count && removed_crossings.count(node / 4) != 0;
    };
    auto is_removed_red_node = [&](int node) {
        if (!is_removed_node(node)) {
            return false;
        }
        const int crossing = node / 4;
        const int strand = node % 4;
        const int red_strand = red_entry_by_crossing.at(crossing);
        return strand == red_strand || strand == positive_mod(red_strand + 2, 4);
    };

    std::set<int> crossed_labels;
    struct CrossedEdge {
        int interface_from = -1;
        int interface_to = -1;
        std::string level;
    };
    std::vector<CrossedEdge> crossed_edges;
    crossed_edges.reserve(new_crossing_count);
    for (int i = 0; i < new_crossing_count; ++i) {
        const int from_face = result.green_path[i];
        const int to_face = result.green_path[i + 1];
        const GraphEdge* edge = graph.edge(from_face, to_face);
        if (edge == nullptr) {
            throw std::invalid_argument("Simplification witness green path crosses a missing dual edge");
        }
        const int interface_from = graph.interface_for_face(*edge, from_face);
        const int interface_to = graph.interface_for_face(*edge, to_face);
        if (is_removed_red_node(interface_from) || is_removed_red_node(interface_to)) {
            throw std::invalid_argument("Simplification witness crosses an edge removed with the red arc");
        }
        const int label = code[interface_from / 4][interface_from % 4];
        if (!crossed_labels.insert(label).second) {
            throw std::invalid_argument("Simplification witness crosses the same PD edge more than once");
        }
        const auto level = levels.find(std::make_pair(from_face, to_face));
        if (level == levels.end()) {
            throw std::invalid_argument("Simplification witness is missing a green crossing level");
        }
        CrossedEdge crossed;
        crossed.interface_from = interface_from;
        crossed.interface_to = interface_to;
        crossed.level = level->second;
        crossed_edges.push_back(std::move(crossed));
    }

    std::map<int, std::vector<int>> label_endpoints;
    for (int crossing_index = 0; crossing_index < static_cast<int>(code.size()); ++crossing_index) {
        for (int strand = 0; strand < 4; ++strand) {
            label_endpoints[code[crossing_index][strand]].push_back(crossing_index * 4 + strand);
        }
    }
    for (const auto& item : label_endpoints) {
        if (item.second.size() != 2) {
            std::ostringstream message;
            message << "PD label " << item.first << " appears " << item.second.size() << " times";
            throw std::invalid_argument(message.str());
        }
        if (crossed_labels.count(item.first) == 0) {
            dsu.unite(item.second[0], item.second[1]);
        }
    }

    for (const auto& item : red_entry_by_crossing) {
        const int crossing = item.first;
        const int strand = item.second;
        dsu.unite(crossing * 4 + positive_mod(strand + 1, 4),
                  crossing * 4 + positive_mod(strand + 3, 4));
    }

    int green_anchor = endpoint_key(result.red_path.front());
    for (int i = 0; i < static_cast<int>(crossed_edges.size()); ++i) {
        const CrossedEdge& crossed = crossed_edges[i];
        int existing_from_pos = -1;
        int existing_to_pos = -1;
        int green_in_pos = -1;
        int green_out_pos = -1;
        if (crossed.level == "over") {
            existing_from_pos = 0;
            green_in_pos = 1;
            existing_to_pos = 2;
            green_out_pos = 3;
        } else if (crossed.level == "under") {
            green_in_pos = 0;
            existing_to_pos = 1;
            green_out_pos = 2;
            existing_from_pos = 3;
        } else {
            throw std::invalid_argument("Unknown green crossing strand level: " + crossed.level);
        }

        dsu.unite(crossed.interface_from, new_node(i, existing_from_pos));
        dsu.unite(crossed.interface_to, new_node(i, existing_to_pos));
        dsu.unite(green_anchor, new_node(i, green_in_pos));
        green_anchor = new_node(i, green_out_pos);
    }
    dsu.unite(green_anchor, endpoint_key(result.red_path.back()));

    std::vector<int> active_nodes;
    active_nodes.reserve(endpoint_count + new_crossing_count * 4);
    for (int node = 0; node < endpoint_count; ++node) {
        if (!is_removed_node(node)) {
            active_nodes.push_back(node);
        }
    }
    for (int crossing = 0; crossing < new_crossing_count; ++crossing) {
        for (int strand = 0; strand < 4; ++strand) {
            active_nodes.push_back(new_node(crossing, strand));
        }
    }

    std::map<int, std::vector<int>> grouped;
    for (int node : active_nodes) {
        grouped[dsu.find(node)].push_back(node);
    }

    std::vector<std::vector<int>> groups;
    for (auto& item : grouped) {
        std::sort(item.second.begin(), item.second.end());
        groups.push_back(item.second);
    }
    std::sort(groups.begin(), groups.end(), [](const std::vector<int>& lhs, const std::vector<int>& rhs) {
        return lhs.front() < rhs.front();
    });

    std::map<int, int> label_by_node;
    int next_label = 0;
    for (const std::vector<int>& nodes : groups) {
        if (nodes.size() != 2) {
            std::ostringstream message;
            message << "Applied simplification produced a non-PD edge with "
                    << nodes.size() << " active endpoints";
            throw std::runtime_error(message.str());
        }
        for (int node : nodes) {
            label_by_node[node] = next_label;
        }
        ++next_label;
    }

    PDCode output;
    output.reserve(code.size() - removed_crossings.size() + static_cast<std::size_t>(new_crossing_count));
    for (int crossing_index = 0; crossing_index < static_cast<int>(code.size()); ++crossing_index) {
        if (removed_crossings.count(crossing_index) != 0) {
            continue;
        }
        Crossing crossing{};
        for (int strand = 0; strand < 4; ++strand) {
            crossing[strand] = label_by_node.at(crossing_index * 4 + strand);
        }
        output.push_back(crossing);
    }
    for (int crossing_index = 0; crossing_index < new_crossing_count; ++crossing_index) {
        Crossing crossing{};
        for (int strand = 0; strand < 4; ++strand) {
            crossing[strand] = label_by_node.at(new_node(crossing_index, strand));
        }
        output.push_back(crossing);
    }

    const std::size_t total_components =
        analyze_components(code, known_crossingless_components).total_components();
    output = renumber_full_dfs(output);
    const std::size_t crossing_components = analyze_components(output).components_with_crossings();

    MidSimplificationApplyResult applied;
    applied.code = std::move(output);
    applied.crossingless_components =
        total_components > crossing_components ? total_components - crossing_components : 0;
    return applied;
}

namespace {

bool witness_has_applicable_surgery(const PDCode& code, const SimplificationResult& result) {
    try {
        (void)apply_simplification_witness(code, result, 0);
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

void emit_progress(const SimplifierOptions& options, const std::string& message) {
    if (!options.verbose) {
        return;
    }
    if (options.progress) {
        options.progress(message);
    }
}

std::string search_mode_for_options(const SimplifierOptions& options) {
    if (options.max_paths == -1 && !options.ban_heuristic) {
        return "heuristic";
    }
    if (options.max_paths == -1) {
        return "bruteforce";
    }
    return "bounded";
}

}  // namespace

PDCode parse_pd_code(const std::string& text) {
    std::vector<int> numbers;
    for (std::size_t i = 0; i < text.size();) {
        if (text[i] == '-' || std::isdigit(static_cast<unsigned char>(text[i]))) {
            const std::size_t start = i;
            if (text[i] == '-') {
                ++i;
                if (i >= text.size() || !std::isdigit(static_cast<unsigned char>(text[i]))) {
                    throw std::invalid_argument("A minus sign must be followed by digits");
                }
            }
            while (i < text.size() && std::isdigit(static_cast<unsigned char>(text[i]))) {
                ++i;
            }
            const std::string token = text.substr(start, i - start);
            numbers.push_back(std::stoi(token));
        } else {
            ++i;
        }
    }

    if (numbers.empty()) {
        return {};
    }
    if (numbers.size() % 4 != 0) {
        throw std::invalid_argument("The input must contain a multiple of four integers");
    }

    PDCode code;
    code.reserve(numbers.size() / 4);
    for (std::size_t i = 0; i < numbers.size(); i += 4) {
        code.push_back(Crossing{numbers[i], numbers[i + 1], numbers[i + 2], numbers[i + 3]});
    }
    return code;
}

std::string format_pd_code(const PDCode& code) {
    std::ostringstream out;
    out << "PD[";
    for (std::size_t i = 0; i < code.size(); ++i) {
        if (i != 0) {
            out << ',';
        }
        out << "X[" << code[i][0] << ',' << code[i][1] << ','
            << code[i][2] << ',' << code[i][3] << ']';
    }
    out << ']';
    return out.str();
}

std::string format_endpoint(const Endpoint& endpoint) {
    std::ostringstream out;
    out << '(' << endpoint.crossing << ", " << endpoint.strand << ')';
    return out.str();
}

std::string format_direction(Direction direction) {
    return direction == Direction::Left ? "left" : "right";
}

ComponentAnalysis analyze_components(
    const PDCode& code,
    std::size_t known_crossingless_components) {
    ComponentAnalysis analysis;
    analysis.crossingless_components = known_crossingless_components;
    if (code.empty()) {
        return analysis;
    }

    Diagram diagram(code);
    analysis.components = component_summaries(diagram);
    return analysis;
}

ComponentAnalysis analyze_components_after_removing_crossings(
    const PDCode& code,
    const std::vector<int>& removed_crossings,
    std::size_t known_crossingless_components) {
    const std::set<int> removed = normalized_removed_crossings(code, removed_crossings);
    ComponentAnalysis original = analyze_components(code, known_crossingless_components);
    ComponentAnalysis reduced;
    reduced.crossingless_components = original.crossingless_components;

    for (const LinkComponentSummary& component : original.components) {
        LinkComponentSummary remaining_component;
        for (int crossing : component.crossing_indices) {
            if (removed.count(crossing) == 0) {
                remaining_component.crossing_indices.push_back(crossing);
            }
        }

        if (remaining_component.crossing_indices.empty()) {
            ++reduced.crossingless_components;
        } else {
            reduced.components.push_back(std::move(remaining_component));
        }
    }

    return reduced;
}

std::size_t count_crossingless_components_after_removing_crossings(
    const PDCode& code,
    const std::vector<int>& removed_crossings,
    std::size_t known_crossingless_components) {
    return analyze_components_after_removing_crossings(
        code, removed_crossings, known_crossingless_components).crossingless_components;
}

bool apply_reverse_type_i(PDCode& code, std::mt19937& rng) {
    if (code.empty()) {
        return false;
    }

    const LabelMap labels = build_label_map(code);
    std::uniform_int_distribution<int> endpoint_distribution(
        0, static_cast<int>(code.size() * 4 - 1));
    const Endpoint first = endpoint_from_key(endpoint_distribution(rng));
    const Endpoint second = mate_endpoint(code, labels, first);

    const int first_label = max_label(code) + 1;
    const int second_label = first_label + 1;
    const int loop_label = first_label + 2;

    std::uniform_int_distribution<int> hand_distribution(0, 1);
    code[first.crossing][first.strand] = first_label;
    code[second.crossing][second.strand] = second_label;
    if (hand_distribution(rng) == 0) {
        code.push_back(Crossing{first_label, loop_label, loop_label, second_label});
    } else {
        code.push_back(Crossing{first_label, second_label, loop_label, loop_label});
    }
    return true;
}

bool apply_reverse_type_ii(PDCode& code, std::mt19937& rng) {
    if (code.empty()) {
        return false;
    }

    const LabelMap labels = build_label_map(code);
    const std::vector<std::vector<int>> faces = raw_faces_from_pd_code(code);
    std::vector<int> eligible_faces;
    for (int i = 0; i < static_cast<int>(faces.size()); ++i) {
        if (faces[i].size() > 1) {
            eligible_faces.push_back(i);
        }
    }
    if (eligible_faces.empty()) {
        return false;
    }

    std::uniform_int_distribution<int> face_distribution(0, static_cast<int>(eligible_faces.size() - 1));
    for (int attempt = 0; attempt < 50; ++attempt) {
        const std::vector<int>& face = faces[eligible_faces[face_distribution(rng)]];
        std::uniform_int_distribution<int> corner_distribution(0, static_cast<int>(face.size() - 1));
        const int first_corner = corner_distribution(rng);
        int second_corner = corner_distribution(rng);
        if (first_corner == second_corner) {
            second_corner = (second_corner + 1) % static_cast<int>(face.size());
        }

        const Endpoint c = endpoint_from_key(face[first_corner]);
        const Endpoint d = endpoint_from_key(face[second_corner]);
        const Endpoint c_opposite = mate_endpoint(code, labels, c);
        const Endpoint d_opposite = mate_endpoint(code, labels, d);

        const std::set<int> touched{
            endpoint_key(c),
            endpoint_key(c_opposite),
            endpoint_key(d),
            endpoint_key(d_opposite)};
        if (touched.size() != 4) {
            continue;
        }

        const int base = max_label(code) + 1;
        const int d_opposite_label = base;
        const int c_label = base + 1;
        const int shared_first = base + 2;
        const int shared_second = base + 3;
        const int c_opposite_label = base + 4;
        const int d_label = base + 5;

        code[d_opposite.crossing][d_opposite.strand] = d_opposite_label;
        code[c.crossing][c.strand] = c_label;
        code[c_opposite.crossing][c_opposite.strand] = c_opposite_label;
        code[d.crossing][d.strand] = d_label;

        code.push_back(Crossing{d_opposite_label, c_label, shared_first, shared_second});
        code.push_back(Crossing{shared_first, c_opposite_label, d_label, shared_second});
        return true;
    }

    return false;
}

bool apply_type_i_simplification(
    PDCode& code,
    std::size_t& crossingless_components,
    int& type_i_moves) {
    for (int crossing_index = 0; crossing_index < static_cast<int>(code.size()); ++crossing_index) {
        const Crossing crossing = code[crossing_index];
        for (int i = 0; i < 4; ++i) {
            if (crossing[i] != crossing[(i + 1) % 4]) {
                continue;
            }

            const ComponentAnalysis after_removal =
                analyze_components_after_removing_crossings(
                    code, std::vector<int>{crossing_index}, crossingless_components);
            const int keep_label = crossing[(i + 2) % 4];
            const int merge_label = crossing[(i + 3) % 4];
            const std::set<int> removed{crossing_index};
            if (keep_label == merge_label) {
                if (label_occurrences_outside(code, keep_label, removed) != 0) {
                    continue;
                }
            } else if (label_occurrences_outside(code, keep_label, removed) != 1 ||
                       label_occurrences_outside(code, merge_label, removed) != 1) {
                continue;
            }

            crossingless_components = after_removal.crossingless_components;
            erase_crossings(code, crossing_index);
            replace_label(code, merge_label, keep_label);
            ++type_i_moves;
            return true;
        }
    }
    return false;
}

bool apply_type_ii_simplification(
    PDCode& code,
    std::size_t& crossingless_components,
    int& type_ii_moves) {
    if (code.size() < 2) {
        return false;
    }

    const LabelMap labels = build_label_map(code);
    for (int first_crossing = 0; first_crossing < static_cast<int>(code.size()); ++first_crossing) {
        for (int a = 0; a < 4; ++a) {
            const Endpoint first_mate =
                mate_endpoint(code, labels, Endpoint{first_crossing, a});
            const Endpoint second_mate =
                mate_endpoint(code, labels, Endpoint{first_crossing, (a + 1) % 4});
            const int second_crossing = first_mate.crossing;
            if (second_crossing == first_crossing || second_crossing != second_mate.crossing) {
                continue;
            }

            const int b = first_mate.strand;
            const int c = second_mate.strand;
            if (positive_mod(b - 1, 4) != c || (a + b) % 2 != 0) {
                continue;
            }

            const Crossing first = code[first_crossing];
            const Crossing second = code[second_crossing];
            const int keep_first = first[(a + 2) % 4];
            const int merge_first = second[(b + 2) % 4];
            const int keep_second = first[(a + 3) % 4];
            const int merge_second = second[(b + 1) % 4];
            const std::set<int> boundary_labels{
                keep_first,
                merge_first,
                keep_second,
                merge_second};
            const std::set<int> removed_crossings{first_crossing, second_crossing};
            if (boundary_labels.size() != 4 ||
                label_occurrences_outside(code, keep_first, removed_crossings) != 1 ||
                label_occurrences_outside(code, merge_first, removed_crossings) != 1 ||
                label_occurrences_outside(code, keep_second, removed_crossings) != 1 ||
                label_occurrences_outside(code, merge_second, removed_crossings) != 1) {
                continue;
            }

            const ComponentAnalysis after_removal =
                analyze_components_after_removing_crossings(
                    code,
                    std::vector<int>{first_crossing, second_crossing},
                    crossingless_components);
            crossingless_components = after_removal.crossingless_components;

            erase_crossings(code, first_crossing, second_crossing);
            replace_label(code, merge_first, keep_first);
            replace_label(code, merge_second, keep_second);
            ++type_ii_moves;
            return true;
        }
    }

    return false;
}

RandomInflationResult randomly_increase_crossings(
    const PDCode& code,
    const RandomInflationOptions& options) {
    if (options.moves < 0) {
        throw std::invalid_argument("Random inflation move count cannot be negative");
    }
    if (options.type_ii_percentage < 0 || options.type_ii_percentage > 100) {
        throw std::invalid_argument("Type-II percentage must be between 0 and 100");
    }

    RandomInflationResult result;
    result.code = code;
    result.seed = options.seed;
    std::mt19937 rng(options.seed);
    std::uniform_int_distribution<int> percent_distribution(0, 99);

    for (int move = 0; move < options.moves; ++move) {
        const bool prefer_type_ii = percent_distribution(rng) < options.type_ii_percentage;
        if (prefer_type_ii && apply_reverse_type_ii(result.code, rng)) {
            ++result.type_ii_moves;
        } else if (apply_reverse_type_i(result.code, rng)) {
            ++result.type_i_moves;
        } else if (apply_reverse_type_ii(result.code, rng)) {
            ++result.type_ii_moves;
        } else {
            throw std::runtime_error("Could not apply any random crossing-increasing move");
        }
    }

    return result;
}

ReidemeisterSimplificationResult simplify_reidemeister_i_ii(
    const PDCode& code,
    std::size_t known_crossingless_components) {
    ReidemeisterSimplificationResult result;
    result.code = code;
    result.crossingless_components = known_crossingless_components;

    while (true) {
        if (apply_type_i_simplification(
                result.code, result.crossingless_components, result.type_i_moves)) {
            continue;
        }
        if (apply_type_ii_simplification(
                result.code, result.crossingless_components, result.type_ii_moves)) {
            continue;
        }
        break;
    }

    return result;
}

PDSimplificationResult simplify_pd_code(
    const PDCode& code,
    std::size_t known_crossingless_components) {
    PDSimplificationResult result;
    result.code = code;
    result.crossingless_components = known_crossingless_components;

    result.code = erase_r1_moves(
        result.code,
        result.crossingless_components,
        result.reidemeister_i_moves);

    while (true) {
        const int crossing_index = find_nugatory_crossing(result.code);
        if (crossing_index < 0) {
            break;
        }
        result.code = erase_one_nugatory_crossing(
            result.code,
            crossing_index,
            result.crossingless_components,
            result.nugatory_crossing_moves);
    }

    return result;
}

SimplificationResult find_simplification(
    const PDCode& code,
    const SimplifierOptions& options) {
    SimplificationResult result;
    if (options.max_paths == -1 && !options.ban_heuristic) {
        result.path_search_mode = "heuristic";
    } else if (options.max_paths == -1) {
        result.path_search_mode = "bruteforce";
    } else {
        result.path_search_mode = "bounded";
    }
    Diagram diagram(code);
    DualGraph graph(diagram);
    const auto red_lines = possible_red_lines(diagram);

    for (const auto& red_path : red_lines) {
        ++result.tested_red_paths;
        reset_weights(graph);

        const Endpoint start = red_path.front();
        const Endpoint end = red_path.back();
        const int start_face = graph.edge_to_face[endpoint_key(start)];
        const int start_opposite_face = graph.edge_to_face[endpoint_key(diagram.opposite(start))];
        const int end_face = graph.edge_to_face[endpoint_key(end)];
        const int end_opposite_face = graph.edge_to_face[endpoint_key(diagram.opposite(end))];
        const std::array<int, 2> sources{start_face, start_opposite_face};
        const std::array<int, 2> destinations{end_face, end_opposite_face};

        for (std::size_t i = 1; i + 1 < red_path.size(); ++i) {
            const Endpoint endpoint = red_path[i];
            const int right_region = graph.edge_to_face[endpoint_key(endpoint)];
            const int left_region = graph.edge_to_face[endpoint_key(diagram.opposite(endpoint))];
            if (GraphEdge* edge = graph.mutable_edge(right_region, left_region)) {
                edge->weight = kBlockedWeight;
            }
        }

        std::vector<std::vector<int>> paths;
        const int cutoff = static_cast<int>(red_path.size()) - 1;
        for (int source : sources) {
            for (int destination : destinations) {
                std::vector<std::vector<int>> found_paths;
                if (options.max_paths == -1 && !options.ban_heuristic) {
                    found_paths = collect_heuristic_paths(graph, source, destination, cutoff);
                } else {
                    found_paths = collect_simple_paths(graph, source, destination, cutoff, options.max_paths);
                }
                paths.insert(paths.end(), found_paths.begin(), found_paths.end());
                if (options.max_paths != -1 && static_cast<int>(paths.size()) > options.max_paths) {
                    break;
                }
            }
        }

        for (const auto& green_path : paths) {
            ++result.tested_green_paths;
            if (green_path.size() >= red_path.size()) {
                continue;
            }
            if (do_check(diagram, graph, red_path, green_path, Direction::Left, result)) {
                if (!options.require_applicable || witness_has_applicable_surgery(code, result)) {
                    return result;
                }
                clear_witness(result);
            }
            if (do_check(diagram, graph, red_path, green_path, Direction::Right, result)) {
                if (!options.require_applicable || witness_has_applicable_surgery(code, result)) {
                    return result;
                }
                clear_witness(result);
            }
        }
    }

    return result;
}

ReductionResult reduce_pd_code(
    const PDCode& code,
    std::size_t known_crossingless_components,
    const SimplifierOptions& options,
    int reduction_round) {
    {
        std::ostringstream message;
        message << "start input_crossings=" << code.size()
                << " known_crossingless_components=" << known_crossingless_components
                << " reduction_round=" << reduction_round
                << " max_paths=" << options.max_paths
                << " heuristic=" << (options.ban_heuristic ? "off" : "on");
        emit_progress(options, message.str());
    }

    const PDSimplificationResult prepared = simplify_pd_code(code, known_crossingless_components);
    ReductionResult output;
    output.code = prepared.code;
    output.crossingless_components = prepared.crossingless_components;
    output.reidemeister_i_moves = prepared.reidemeister_i_moves;
    output.nugatory_crossing_moves = prepared.nugatory_crossing_moves;
    {
        std::ostringstream message;
        message << "pre_simplify input_crossings=" << code.size()
                << " output_crossings=" << output.code.size()
                << " crossingless_components=" << output.crossingless_components
                << " r1_moves=" << prepared.reidemeister_i_moves
                << " nugatory_moves=" << prepared.nugatory_crossing_moves;
        emit_progress(options, message.str());
    }

    while (reduction_round < 0 || output.mid_simplification_rounds < reduction_round) {
        SimplifierOptions search_options = options;
        search_options.require_applicable = true;
        const int round = output.mid_simplification_rounds + 1;
        {
            std::ostringstream message;
            message << "round " << round
                    << " search_start crossings=" << output.code.size()
                    << " mode=" << search_mode_for_options(search_options);
            emit_progress(options, message.str());
        }
        SimplificationResult search = find_simplification(output.code, search_options);
        output.tested_red_paths += search.tested_red_paths;
        output.tested_green_paths += search.tested_green_paths;
        output.last_path_search_mode = search.path_search_mode;
        {
            std::ostringstream message;
            message << "round " << round
                    << " search_done found=" << (search.found ? "yes" : "no")
                    << " mode=" << search.path_search_mode
                    << " tested_red=" << search.tested_red_paths
                    << " tested_green=" << search.tested_green_paths;
            emit_progress(options, message.str());
        }

        if (!search.found && reduction_round < 0 &&
            options.max_paths == -1 && !options.ban_heuristic) {
            SimplifierOptions brute_options = options;
            brute_options.max_paths = -1;
            brute_options.ban_heuristic = true;
            brute_options.require_applicable = true;
            {
                std::ostringstream message;
                message << "round " << round
                        << " brute_fallback_start crossings=" << output.code.size();
                emit_progress(options, message.str());
            }
            SimplificationResult brute = find_simplification(output.code, brute_options);
            output.tested_red_paths += brute.tested_red_paths;
            output.tested_green_paths += brute.tested_green_paths;
            output.last_path_search_mode = brute.path_search_mode;
            {
                std::ostringstream message;
                message << "round " << round
                        << " brute_fallback_done found=" << (brute.found ? "yes" : "no")
                        << " tested_red=" << brute.tested_red_paths
                        << " tested_green=" << brute.tested_green_paths;
                emit_progress(options, message.str());
            }
            if (brute.found) {
                ++output.heuristic_failover_rounds;
                search = std::move(brute);
            }
        }

        if (!search.found) {
            {
                std::ostringstream message;
                message << "round " << round
                        << " stop_no_path crossings=" << output.code.size();
                emit_progress(options, message.str());
            }
            break;
        }

        const std::size_t before_apply_crossings = output.code.size();
        const MidSimplificationApplyResult applied =
            apply_simplification_witness(output.code, search, output.crossingless_components);
        ++output.mid_simplification_rounds;
        const PDSimplificationResult simplified =
            simplify_pd_code(applied.code, applied.crossingless_components);
        output.code = simplified.code;
        output.crossingless_components = simplified.crossingless_components;
        output.reidemeister_i_moves += simplified.reidemeister_i_moves;
        output.nugatory_crossing_moves += simplified.nugatory_crossing_moves;
        {
            std::ostringstream message;
            message << "round " << round
                    << " applied crossings=" << before_apply_crossings
                    << " -> " << applied.code.size()
                    << " -> " << output.code.size()
                    << " crossingless_components=" << output.crossingless_components
                    << " r1_moves=" << simplified.reidemeister_i_moves
                    << " nugatory_moves=" << simplified.nugatory_crossing_moves;
            emit_progress(options, message.str());
        }
    }

    output.stopped_by_round_limit =
        reduction_round >= 0 && output.mid_simplification_rounds >= reduction_round;
    {
        std::ostringstream message;
        message << "done final_crossings=" << output.code.size()
                << " crossingless_components=" << output.crossingless_components
                << " mid_rounds=" << output.mid_simplification_rounds
                << " heuristic_failover_rounds=" << output.heuristic_failover_rounds
                << " stopped_by_round_limit="
                << (output.stopped_by_round_limit ? "yes" : "no");
        emit_progress(options, message.str());
    }
    return output;
}

std::ostream& operator<<(std::ostream& out, const Endpoint& endpoint) {
    out << format_endpoint(endpoint);
    return out;
}

}  // namespace pdcode_simplify
