// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "core.hpp"
#include <functional>
#include <memory>
#include <optional>
#include <vector>

namespace pineforge::feed {
struct VenueEvent {
    enum class Kind { Trade, Kline, Control } kind = Kind::Control;
    Trade trade;
    Kline kline;
};
// How the WebSocket reader treats one complete text message. Control messages (subscription
// acknowledgements, keepalive replies) prove liveness but are not queued; Retire queues the message,
// then reconnects.
enum class Frame { Data, Control, Retire };
struct Connection {
    std::string path;                    // appended to the WebSocket origin
    std::vector<std::string> subscribe;  // text frames sent after every handshake
    std::string ping;                    // venue text keepalive; empty when curl's PONG suffices
    std::function<Frame(const std::string&)> classify;  // runs on the reader thread: no shared state
};
// How a tick minute is proven complete before its `time` event.
// KlineIdRange: the closed kline names the minute's raw-ID range and count (Binance spot).
// NextPrintFence: the contiguous ID chain reaches a print of a later minute and the venue has closed the
// minute (OKX trades-all, Binance USD-M aggregate prints); raw prints must also equal that candle exactly.
enum class TickProof { KlineIdRange, NextPrintFence };

class Venue {
public:
    virtual ~Venue() = default;
    virtual Connection connection() const = 0;
    // One source message can carry several events (OKX and Bybit batch their data arrays).
    virtual std::vector<VenueEvent> decode(const std::string& message) const = 0;
    // Up to `limit` contiguous prints from `from`, ascending. Fence venues may return fewer or none.
    virtual std::vector<Trade> history(std::uint64_t from, std::size_t limit = 1000) = 0;
    virtual std::uint64_t first_trade_id(std::int64_t) { throw Error(23, "this venue has no raw-trade start fence lookup"); }
    // The last print strictly before `minute`.
    virtual Trade predecessor(std::int64_t minute) {
        const auto first = first_trade_id(minute);
        if (first <= 1) throw Error(20, "raw predecessor cannot be proven");
        const auto previous = history(first - 1, 1);
        if (previous.empty() || previous.front().id != first - 1) throw Error(20, "raw predecessor is unavailable");
        return previous.front();
    }
    // The same predecessor, but only once the venue holds a print at or after `minute`: nullopt while it
    // holds none, without waiting or stopping (a quiet start). Fence venues override it.
    virtual std::optional<Trade> predecessor_if_ready(std::int64_t minute) {
        try { return predecessor(minute); }
        catch (const Error& failure) { if (failure.code == 20) return std::nullopt; throw; }
    }
    // Re-checks instrument metadata that fixes the stream's units; a change stops 21. Called on connect.
    virtual void reverify() {}
    // Contiguous closed one-minute candles in [start, end), ascending.
    virtual std::vector<Kline> klines(std::int64_t start, std::int64_t end) = 0;
    virtual TickProof tick_proof() const { return TickProof::KlineIdRange; }
    // Documented REST print history window in milliseconds; 0 when none is documented.
    virtual std::int64_t retention_ms() const { return 0; }
    // Whether a closed candle is the exact sum of the venue's prints. Raw prints are; aggregate prints are
    // not, because one aggregate can straddle a minute boundary and is never split.
    virtual bool candle_is_print_sum() const { return true; }
    // Base units per venue quantity unit (contract value times multiplier), "1" for base quantities.
    virtual std::string qty_multiplier() const { return "1"; }
    virtual std::string kline_source() const = 0;
};
std::unique_ptr<Venue> make_venue(const Config& config);
}
