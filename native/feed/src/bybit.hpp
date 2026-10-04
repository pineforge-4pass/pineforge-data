// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "transport.hpp"
#include "venue.hpp"

namespace pineforge::feed {
// {start, end, interval, open, close, high, low, volume, turnover, confirm, timestamp}; only confirm=true closes.
Kline bybit_kline(const Json& row);
// REST rows arrive newest first: they are reversed and must cover [start, start + count minutes) exactly.
std::vector<Kline> bybit_rows(const Json& list, std::int64_t start, std::size_t count);
Frame bybit_frame(const std::string& message);

// Bybit v5 spot and linear: confirmed one-minute bars only. Ticks are refused at startup: trade IDs are
// not a contiguous cursor and the public REST cannot page back through trades. Bars never come from
// trades, because trade-built bars disagree with Bybit candles.
class Bybit final : public Venue {
    Config config_;
    HttpClient http_;
    Json result(const std::string& path);
public:
    explicit Bybit(const Config& config);
    Connection connection() const override;
    std::vector<VenueEvent> decode(const std::string& message) const override;
    std::vector<Trade> history(std::uint64_t, std::size_t) override { throw Error(23, "Bybit has no pageable trade history"); }
    std::optional<Trade> predecessor_if_ready(std::int64_t) override { throw Error(23, "Bybit has no pageable trade history"); }
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override;
    std::string kline_source() const override { return "/v5/market/kline"; }
};
}
