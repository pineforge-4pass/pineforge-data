// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "transport.hpp"
#include "venue.hpp"
#include <algorithm>

namespace pineforge::feed {
Trade binance_trade(const Json& trade, bool websocket);
Kline binance_kline(const Json& row, bool websocket);
// One USD-M aggregate print: `a` is the sequence, `q` the quantity. Its raw `f..l` range is validated,
// never expanded into imaginary raw trades.
Trade usdm_aggregate(const Json& row);
// A probe reply (a top-level `id`) proves the connection alive only if its `result` lists every stream in
// `streams`; an error or a partial list is a lost subscription (Stale), never market data.
Frame binance_frame(const std::string& message, const std::vector<std::string>& streams = {});
Connection usdm_connection(const Config& config);
unsigned int usdm_kline_weight(std::size_t limit);

class BinanceSpot final : public Venue {
    Config config_;
    HttpClient http_;
public:
    explicit BinanceSpot(const Config& config) : config_(config), http_(config, binance_spot_rest(config.symbol)) {}
    Connection connection() const override;
    std::vector<VenueEvent> decode(const std::string& message) const override;
    std::vector<Trade> history(std::uint64_t from, std::size_t limit = 1000) override;
    std::uint64_t first_trade_id(std::int64_t minute) override;
    // Binance spot ticks close minutes on the kline ID range, never on a next-print fence.
    std::optional<Trade> predecessor_if_ready(std::int64_t) override { throw Error(23, "Binance spot has no next-print fence"); }
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override;
    std::string kline_source() const override { return "/api/v3/klines"; }
};

// Binance USD-M perpetuals: confirmed bars, or the opt-in `agg-ticks` aggregate-print mode. Both use the
// routed /market stream; the unrouted origin no longer serves aggTrade or kline streams.
class BinanceUsdm final : public Venue {
    Config config_;
    HttpClient http_;
    mutable std::int64_t newest_ = -1;  // newest venue time decoded from the WebSocket (main thread only)
    std::int64_t quiet_until_ = -1;     // [start, quiet_until_) is settled and holds no aggregate
    std::uint64_t first_aggregate(std::int64_t minute);
public:
    explicit BinanceUsdm(const Config& config);
    Connection connection() const override { return usdm_connection(config_); }
    std::vector<VenueEvent> decode(const std::string& message) const override;
    std::vector<Trade> history(std::uint64_t from, std::size_t limit = 1000) override;
    std::uint64_t first_trade_id(std::int64_t minute) override;
    std::optional<Trade> predecessor_if_ready(std::int64_t minute) override;
    void horizon(std::int64_t ts) override { newest_ = std::max(newest_, ts); }
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override;
    TickProof tick_proof() const override { return TickProof::NextPrintFence; }
    std::int64_t retention_ms() const override { return 48LL * 3600000; }
    // Observed live: an aggregate dated in one minute held a fill the next minute's kline counts.
    bool candle_is_print_sum() const override { return false; }
    std::string kline_source() const override { return "/fapi/v1/klines"; }
};
}
