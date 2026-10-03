// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "state.hpp"
#include "venue.hpp"
#include <map>

namespace pineforge::feed {
class Session {
protected:
    State& state_;
    Venue& venue_;
    std::function<void(const std::string&)> output_;
    std::uint64_t new_messages_ = 0;
    bool replayed_ = false;
    void emit(const std::string& line, const std::optional<Bar>& proof = std::nullopt);
    void replay();
public:
    Session(State& state, Venue& venue, std::function<void(const std::string&)> output);
    virtual ~Session() = default;
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    virtual void connected() = 0;
    // stage() verifies an event and stages its messages; publish() group-commits them, then writes them
    // out; salvage() publishes what was already verified before a stop. ingest() is all three for one event.
    virtual void stage(const VenueEvent& event) = 0;
    void publish();
    void salvage() noexcept;
    void ingest(const VenueEvent& event);
};

// Confirmed bars for every venue, and Binance spot raw ticks proven by the closed kline's ID range.
class FeedSession final : public Session {
    Aggregate aggregate_;
    std::map<std::uint64_t, Trade> pending_;
    std::size_t pending_bytes_ = 0;
    std::int64_t watermark_ = -1;
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
    void connected() override;
    void stage(const VenueEvent& event) override;
};

// Ticks proven by a next-print fence (OKX trades-all, Binance USD-M aggregate prints). A minute closes
// only when the contiguous ID chain from the proven predecessor reaches a print of a later minute, and
// the minute's confirmed candle (from the WebSocket, or from REST behind a later confirmed WebSocket
// candle) equals those prints exactly in OHLC and volume. A quiet minute closes on the same fence when its
// confirmed candle shows zero volume. Prints beyond the closed minute stay buffered until that proof.
class FenceSession final : public Session {
    Aggregate aggregate_;
    std::map<std::uint64_t, Trade> pending_;
    std::size_t pending_bytes_ = 0;
    std::map<std::int64_t, Kline> candles_;
    std::int64_t watermark_ = -1, newest_ = -1, forward_watermark_ = -1;
    std::uint64_t next_id() const;
    void anchor();
    void put(const Trade& trade, bool healed);
    void emit_trade(const Trade& trade);
    void within_retention() const;
    std::vector<Trade> page();
    void heal();
    bool forward();
    bool close_minute();
    void candle(const Kline& kline);
    void advance();
public:
    FenceSession(State& state, Venue& venue, std::function<void(const std::string&)> output);
    void connected() override;
    void stage(const VenueEvent& event) override;
};
// REST closed candles for [start, end), re-read with spaced pauses while `agrees` rejects them: a row for
// the minute that just closed can trail the WebSocket close. The caller decides what persists.
std::vector<Kline> settled_klines(Venue& venue, const Config& config, std::int64_t start, std::int64_t end,
                                  const std::function<bool(const std::vector<Kline>&)>& agrees);
std::vector<Trade> settled_prints(Venue& venue, const Config& config, const std::deque<Trade>& trades);
// Exact OHLC and volume of a fenced minute's prints against its confirmed candle; zero volume when empty.
void reconcile_fenced(const Aggregate& prints, const Kline& candle);

std::unique_ptr<Session> make_session(State& state, Venue& venue, std::function<void(const std::string&)> output);
void run_feed(Config config);
void warmup(const Config& config, const std::string& output);
}
