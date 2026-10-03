// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "state.hpp"
#include "transport.hpp"
#include "venue.hpp"
#include <map>

namespace pineforge::feed {
Trade binance_trade(const Json& trade, bool websocket);
Kline binance_kline(const Json& row, bool websocket);

class BinanceSpot final : public Venue {
    Config config_;
    HttpClient http_;
public:
    explicit BinanceSpot(const Config& config) : config_(config), http_(config) {}
    std::string streams() const override;
    VenueEvent decode(const std::string& message) const override;
    std::vector<Trade> history(std::uint64_t from, std::size_t limit = 1000) override;
    std::uint64_t first_trade_id(std::int64_t minute) override;
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override;
};

class FeedSession {
    State& state_;
    Venue& venue_;
    std::function<void(const std::string&)> output_;
    Aggregate aggregate_;
    std::map<std::uint64_t, Trade> pending_;
    std::size_t pending_bytes_ = 0;
    std::int64_t watermark_ = -1;
    std::uint64_t new_messages_ = 0;
    bool replayed_ = false;
    void emit(const std::string& line, const std::optional<Bar>& proof = std::nullopt);
    void anchor(std::uint64_t first);
    std::uint64_t next_id() const;
    void put(const Trade& trade);
    void emit_trade(const Trade& trade);
    void heal_to(std::uint64_t target);
    void drain();
    void historical_minute();
    void closed(const Kline& kline);
public:
    FeedSession(State& state, Venue& venue, std::function<void(const std::string&)> output);
    void connected();
    // stage() verifies an event and stages its messages; publish() group-commits them, then writes them
    // out; salvage() publishes what was already verified before a stop. ingest() is all three for one event.
    void stage(const VenueEvent& event);
    void publish();
    void salvage() noexcept;
    void ingest(const VenueEvent& event);
};
void run_feed(const Config& config);
void warmup(const Config& config, const std::string& output);
}
