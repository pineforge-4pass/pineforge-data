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
    int release() { const auto value = value_; value_ = -1; return value; }
    void reset(int value);
};

struct Cursor {
    std::string epoch, prefix_hash, overlap_hash;
    std::uint64_t message_index = 0, log_bytes = 0, seq = 0;
    std::int64_t start = 0, cut = 0, last_tick_ts = -1, last_bar = -1;
    std::optional<Trade> predecessor;
    std::deque<Bar> proofs;
    std::uint64_t segment = 0;  // base message index of the open journal segment
};

// One retained journal segment: messages [base, next segment's base), its checkpoint holding the verified
// state before its first message. `bytes` and `cut` and `seq` are that state's absolute log bytes, verified
// cut and emitted sequence.
struct Segment {
    std::uint64_t base = 0, bytes = 0, seq = 0;
    std::int64_t cut = 0, last_tick_ts = -1;
};
std::string segment_name(std::uint64_t base);

// Messages are staged (verified, cursor advanced in memory), then group-committed: one journal append and
// fsync, one cursor replacement, and only then returned for publication. The journal is a sequence of
// segments under `journal/`; a full segment is sealed and a new one opened, and sealed segments expire by
// bytes and by venue time, except those holding the protected window (the open minute and the closed-minute
// proofs the reconnect overlap re-verifies).
class State {
    Config config_;
    Cursor durable_, cursor_;
    Descriptor lock_, journal_;
    std::vector<Segment> segments_;
    std::deque<Trade> recent_trades_;
    std::deque<Bar> recent_bars_;
    std::vector<std::string> staged_;
    std::string staged_bytes_;
    bool unusable_ = false, over_budget_ = false;
    std::string journal_dir() const;
    std::string segment_path(std::uint64_t base) const;
    std::string checkpoint_path(std::uint64_t base) const;
    Json serialize(const Cursor& cursor) const;
    void persist(const Cursor& cursor) const;
    void write_checkpoint(const Cursor& cursor) const;
    Cursor read_checkpoint(std::uint64_t base) const;
    void initialize();
    void recover();
    void remember(const Json& event);
    void rotate();
    void retain();
    std::size_t segment_for(std::uint64_t index) const;
    // visit() that stops as soon as `visitor` returns false.
    void scan(std::uint64_t from, const std::function<bool(const std::string&)>& visitor) const;
public:
    explicit State(const Config& config);
    const Cursor& cursor() const { return cursor_; }
    const Cursor& durable() const { return durable_; }
    const Config& config() const { return config_; }
    std::size_t staged() const { return staged_.size(); }
    const std::vector<Segment>& segments() const { return segments_; }
    std::uint64_t first_retained() const { return segments_.front().base; }
    // Whether retention has dropped (part of) this minute or this print, so an old duplicate can no longer be
    // compared with what was emitted.
    bool expired_minute(std::int64_t minute) const;
    bool expired_seq(std::uint64_t id) const { return first_retained() && id <= segments_.front().seq; }
    // The first retained message index from which every message of `minute` (and later) is visited.
    std::uint64_t index_for_minute(std::int64_t minute) const;
    void anchor(const Trade& predecessor);
    void stage(const std::string& line, const std::optional<Bar>& proof = std::nullopt);
    std::vector<std::string> flush();
    void visit(std::uint64_t from, const std::function<void(const std::string&)>& visitor) const;
    std::optional<Trade> trade(std::uint64_t id) const;
    std::optional<Trade> recent_trade(std::uint64_t id) const;
    std::optional<Bar> bar(std::int64_t ts) const;
    std::optional<Bar> proof(std::int64_t ts) const;
    const std::deque<Trade>& recent_trades() const { return recent_trades_; }
    const std::deque<Bar>& recent_bars() const { return recent_bars_; }
    Json status() const;
};
}
