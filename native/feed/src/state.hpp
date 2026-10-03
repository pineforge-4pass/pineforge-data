// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "core.hpp"
#include <deque>
#include <optional>
#include <vector>

namespace pineforge::feed {
class Descriptor {
    int value_ = -1;
public:
    Descriptor() = default;
    explicit Descriptor(int value) : value_(value) {}
    ~Descriptor();
    Descriptor(const Descriptor&) = delete;
    Descriptor& operator=(const Descriptor&) = delete;
    int get() const { return value_; }
    void reset(int value);
};

struct Cursor {
    std::string epoch, prefix_hash, overlap_hash;
    std::uint64_t message_index = 0, log_bytes = 0, seq = 0;
    std::int64_t start = 0, cut = 0, last_tick_ts = -1, last_bar = -1;
    std::optional<Trade> predecessor;
    std::deque<Bar> proofs;
};

// Messages are staged (verified, cursor advanced in memory), then group-committed: one journal append and
// fsync, one cursor replacement, and only then returned for publication.
class State {
    Config config_;
    Cursor durable_, cursor_;
    Descriptor lock_, journal_;
    std::deque<Trade> recent_trades_;
    std::deque<Bar> recent_bars_;
    std::vector<std::string> staged_;
    std::string staged_bytes_;
    bool unusable_ = false;
    std::string log_path() const;
    Json serialize(const Cursor& cursor) const;
    void persist(const Cursor& cursor) const;
    void recover();
    void remember(const Json& event);
public:
    explicit State(const Config& config);
    const Cursor& cursor() const { return cursor_; }
    const Cursor& durable() const { return durable_; }
    const Config& config() const { return config_; }
    std::size_t staged() const { return staged_.size(); }
    void anchor(const Trade& predecessor);
    void stage(const std::string& line, const std::optional<Bar>& proof = std::nullopt);
    std::vector<std::string> flush();
    void visit(std::uint64_t from, const std::function<void(const std::string&)>& visitor) const;
    std::optional<Trade> trade(std::uint64_t id) const;
    std::optional<Bar> bar(std::int64_t ts) const;
    const std::deque<Trade>& recent_trades() const { return recent_trades_; }
    const std::deque<Bar>& recent_bars() const { return recent_bars_; }
    Json status() const;
};
}
