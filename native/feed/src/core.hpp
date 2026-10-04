// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "json.hpp"
#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <string>
#include <string_view>

namespace pineforge::feed {
using Json = pineforge::live::Json;
using pineforge::live::parse_json;

struct Error : std::runtime_error {
    int code;
    Error(int status, const std::string& reason) : std::runtime_error(reason), code(status) {}
};
struct Stopped {};
extern std::atomic<bool> stopping;
void log(const std::string& level, const std::string& event, const Json& fields = Json::object({}));
void flush_log(std::chrono::milliseconds limit);
std::string sha256(std::string_view bytes);
std::string random_epoch();
std::int64_t timestamp(const std::string& token);
void write_all(int descriptor, std::string_view bytes);
void output_line(std::string_view line);
void atomic_file(const std::string& path, std::string_view bytes);
// Test hook for crash-safety probes: PINEFORGE_FEED_CRASH_AT=NAME[:N] kills the process with SIGKILL at the
// N-th (default first) pass through the named durable step. Unset, it does nothing.
void crash_point(const char* name);

class Decimal {
    std::string coefficient_;
    std::size_t scale_ = 0;
    Decimal(std::string coefficient, std::size_t scale);
public:
    explicit Decimal(std::string_view token);
    bool zero() const { return coefficient_ == "0"; }
    int compare(const Decimal& other) const;
    Decimal add(const Decimal& other) const;
    Decimal multiply(const Decimal& other) const;
    // Canonical fixed-point token: no sign, no exponent, no trailing fractional zeros.
    std::string str() const;
    bool operator==(const Decimal& other) const { return compare(other) == 0; }
};

struct Trade {
    std::uint64_t id = 0;
    std::int64_t ts = 0;
    std::string price;
    std::string qty;
    void validate() const;
    std::string wire() const;
    bool operator==(const Trade& other) const;
};
struct Bar {
    std::int64_t ts = 0;
    std::string open, high, low, close, volume;
    void validate() const;
    std::string wire() const;
    bool operator==(const Bar& other) const;
};
struct Kline {
    Bar bar;
    std::int64_t first = -1;
    std::int64_t last = -1;
    std::uint64_t count = 0;
    bool confirmed = false;
};
struct Aggregate {
    std::uint64_t first = 0, last = 0, count = 0;
    std::int64_t last_ts = -1;
    std::string open, high, low, close;
    Decimal volume{"0"};
    void add(const Trade& trade);
    void reconcile(const Kline& kline, bool require_ids) const;
};
// Contract-to-base conversion for a venue quantity token; the token is kept verbatim when the multiplier is 1.
std::string scaled_quantity(const std::string& token, const std::string& multiplier);
// The JSON text of one top-level member, found by an exact scan; for documents above the parser bound.
std::string json_member(const std::string& document, const std::string& name);
Trade normalized_trade(const Json& event);
Bar normalized_bar(const Json& event);

struct Config {
    std::string venue = "binance", market = "spot", symbol, mode = "bars", state_dir;
    std::string rest_url = "https://api.binance.com";
    std::string ws_url = "wss://stream.binance.com:443";
    std::string qty_multiplier = "1";
    std::int64_t start = -1, end = -1;
    bool resume = false, allow_insecure = false;
    std::uint64_t output_from = 0, max_messages = 0;
    // Journal segments seal at segment_bytes; sealed segments expire beyond replay_bytes retained bytes or,
    // when replay_age_ms is set, beyond that much venue time.
    std::uint64_t segment_bytes = 16 * 1024 * 1024;
    std::uint64_t replay_bytes = 256 * 1024 * 1024;
    std::int64_t replay_age_ms = 0;
    std::uint64_t max_replay_seconds = 60;
    std::size_t max_queue_bytes = 16 * 1024 * 1024;
    std::uint64_t reconnect_seconds = 23 * 3600 + 55 * 60;
    std::uint64_t keepalive_seconds = 20;
    std::uint64_t silence_seconds = 75;  // data silence before a probe (or, without one, a reconnect)
    // serve: the same producer publishes to WebSocket clients instead of stdout.
    bool serve = false, allow_remote_listen = false;
    std::string listen = "127.0.0.1:8787";
    std::size_t client_queue_bytes = 1024 * 1024;
    std::size_t max_clients = 64;
};
// Raw ticks and USD-M aggregate prints share the tick/time journal grammar.
inline bool tick_mode(const std::string& mode) { return mode == "ticks" || mode == "agg-ticks"; }
}
