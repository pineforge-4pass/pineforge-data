// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "transport.hpp"
#include "venue.hpp"

namespace pineforge::feed {
// One trades-all print; `sz` becomes base units through the instrument multiplier. An aggregated
// `trades` row (it carries `count`) is refused: it is never a raw print.
Trade okx_trade(const Json& row, const std::string& symbol, const std::string& multiplier);
// [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]; only confirm "1" is a closed candle.
Kline okx_candle(const Json& row, const std::string& multiplier);
// Validates one public instrument row and returns base units per contract (ctVal x ctMult), "1" for spot.
std::string okx_multiplier(const Json& row, const std::string& market, const std::string& symbol);
Frame okx_frame(const std::string& message);

class Okx final : public Venue {
    Config config_;
    HttpClient http_;
    std::string multiplier_ = "1";
    Json data(const std::string& path);
    std::string instrument_multiplier();
    // The time lookup plus the walk by ID; nullopt when no print at or after the minute is visible yet.
    std::optional<Trade> walk_to(std::int64_t minute);
public:
    explicit Okx(const Config& config);
    Connection connection() const override;
    std::vector<VenueEvent> decode(const std::string& message) const override;
    std::vector<Trade> history(std::uint64_t from, std::size_t limit = 1000) override;
    Trade predecessor(std::int64_t minute) override;
    std::optional<Trade> predecessor_if_ready(std::int64_t minute) override;
    void reverify() override;
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override;
    TickProof tick_proof() const override { return TickProof::NextPrintFence; }
    // history-trades covers the last three months; 89 days stays inside every three-month span.
    std::int64_t retention_ms() const override { return 89LL * 86400000; }
    std::string qty_multiplier() const override { return multiplier_; }
    std::string kline_source() const override { return "/api/v5/market/history-candles"; }
};
}
