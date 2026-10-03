// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "json.hpp"
#include <atomic>
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
std::string sha256(std::string_view bytes);
std::string random_epoch();
std::int64_t timestamp(const std::string& token);
void write_all(int descriptor, std::string_view bytes);
void output_line(std::string_view line);
void atomic_file(const std::string& path, std::string_view bytes);

class Decimal {
    std::string coefficient_;
    std::size_t scale_ = 0;
    Decimal(std::string coefficient, std::size_t scale);
public:
    explicit Decimal(std::string_view token);
    bool zero() const { return coefficient_ == "0"; }
    int compare(const Decimal& other) const;
    Decimal add(const Decimal& other) const;
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
Trade normalized_trade(const Json& event);
Bar normalized_bar(const Json& event);
Json string_field(const std::string& value);

struct Config {
    std::string venue = "binance", market = "spot", symbol, mode = "bars", state_dir;
    std::string rest_url = "https://api.binance.com";
    std::string ws_url = "wss://stream.binance.com:443";
    std::int64_t start = -1, end = -1;
    bool resume = false, allow_insecure = false;
    std::uint64_t output_from = 0, max_messages = 0;
    std::uint64_t max_log_bytes = 256 * 1024 * 1024;
    std::uint64_t max_replay_seconds = 60;
    std::size_t max_queue_bytes = 16 * 1024 * 1024;
    std::uint64_t reconnect_seconds = 23 * 3600 + 55 * 60;
};
}
