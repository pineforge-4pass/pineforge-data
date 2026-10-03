// SPDX-License-Identifier: Apache-2.0
#include "session.hpp"
#include "binance.hpp"
#include "bybit.hpp"
#include "okx.hpp"
#include "transport.hpp"
#include <algorithm>
#include <deque>
#include <filesystem>
#include <limits>

namespace pineforge::feed {
namespace {
std::size_t trade_bytes(const Trade& trade) { return trade.price.size() + trade.qty.size() + 192; }
std::string time_line(std::int64_t ts) { return "{\"type\":\"time\",\"ts\":" + std::to_string(ts) + "}"; }
bool same_prints(const std::vector<Trade>& fetched, const std::deque<Trade>& trades) {
    if (fetched.size() != trades.size()) return false;
    for (std::size_t index = 0; index < trades.size(); ++index)
        if (!(trades[index] == fetched[index])) return false;
    return true;
}
bool same_bars(const std::vector<Kline>& fetched, const std::deque<Bar>& bars) {
    if (fetched.size() != bars.size()) return false;
    for (std::size_t index = 0; index < bars.size(); ++index)
        if (!(bars[index] == fetched[index].bar)) return false;
    return true;
}
}

// A venue's REST row for the minute that just closed can trail its WebSocket close by a moment: a
// disagreement must survive spaced re-reads (about 7 seconds) before it counts as a revision (21).
std::vector<Kline> settled_klines(Venue& venue, const Config& config, std::int64_t start, std::int64_t end,
                                  const std::function<bool(const std::vector<Kline>&)>& agrees) {
    for (unsigned int attempt = 0;; ++attempt) {
        try {
            auto fetched = venue.klines(start, end);
            if (agrees(fetched) || attempt == 3) return fetched;
        } catch (const Error& failure) {
            // A missing or still-unconfirmed newest row (20) is the same lag; it persists only after re-reads.
            if (failure.code != 20 || attempt == 3) throw;
        }
        log("warn", "rest_overlap_trailing", Json::object({{"attempt", Json::number(std::to_string(attempt + 1))}}));
        pause_for(std::chrono::milliseconds((config.allow_insecure ? 100 : 1000) << attempt));
    }
}
// The same spaced re-read for the newest emitted prints, which REST can also trail.
std::vector<Trade> settled_prints(Venue& venue, const Config& config, const std::deque<Trade>& trades) {
    for (unsigned int attempt = 0;; ++attempt) {
        auto fetched = venue.history(trades.front().id, trades.size());
        if (same_prints(fetched, trades) || attempt == 3) return fetched;
        log("warn", "rest_overlap_trailing", Json::object({{"attempt", Json::number(std::to_string(attempt + 1))}}));
        pause_for(std::chrono::milliseconds((config.allow_insecure ? 100 : 1000) << attempt));
    }
}

std::unique_ptr<Venue> make_venue(const Config& config) {
    if (config.venue == "binance" && config.market == "spot") return std::make_unique<BinanceSpot>(config);
    if (config.venue == "binance" && config.market == "usdm") return std::make_unique<BinanceUsdm>(config);
    if (config.venue == "okx" && (config.market == "spot" || config.market == "swap")) return std::make_unique<Okx>(config);
    if (config.venue == "bybit" && (config.market == "spot" || config.market == "linear")) return std::make_unique<Bybit>(config);
    throw Error(23, "unsupported venue or market");
}

Session::Session(State& state, Venue& venue, std::function<void(const std::string&)> output)
    : state_(state), venue_(venue), output_(std::move(output)) {}
void Session::emit(const std::string& line, const std::optional<Bar>& proof) {
    if (stopping) throw Stopped{};
    state_.stage(line, proof);
    ++new_messages_;
    if (state_.config().max_messages && new_messages_ >= state_.config().max_messages) {
        publish();
        throw Stopped{};
    }
    if (state_.staged() >= 4096) publish();
}
void Session::publish() {
    for (const auto& line : state_.flush()) output_(line);
}
void Session::salvage() noexcept {
    try { publish(); }
    catch (...) {
        try { log("error", "verified_messages_not_committed"); } catch (...) {}
    }
}
void Session::ingest(const VenueEvent& event) {
    try { stage(event); }
    catch (const Error&) { salvage(); throw; }
    publish();
}
void Session::replay() {
    if (!replayed_) {
        if (state_.config().resume) state_.visit(state_.config().output_from, output_);
        replayed_ = true;
    }
    log("info", "overlap_verified", state_.status());
}

FeedSession::FeedSession(State& state, Venue& venue, std::function<void(const std::string&)> output)
    : Session(state, venue, std::move(output)) {
    if (state_.config().mode == "ticks") state_.visit(0, [&](const std::string& line) {
        const auto event = parse_json(line);
        if (event.at("type").text() == "tick") {
            const auto trade = normalized_trade(event);
            if (trade.ts >= state_.cursor().cut) aggregate_.add(trade);
        }
    });
}

void FeedSession::connected() {
    publish();
    venue_.reverify();
    const auto& trades = state_.recent_trades();
    if (!trades.empty()) {
        const auto fetched = settled_prints(venue_, state_.config(), trades);
        if (fetched.size() != trades.size()) throw Error(20, "reconnect raw overlap is unavailable");
        for (std::size_t index = 0; index < trades.size(); ++index)
            if (!(trades[index] == fetched[index])) throw Error(21, "reconnect raw overlap changed");
    } else if (state_.cursor().predecessor) {
        const auto fetched = venue_.history(state_.cursor().predecessor->id, 1);
        if (!(fetched.front() == *state_.cursor().predecessor)) throw Error(21, "initial predecessor overlap changed");
    }
    const auto& proofs = state_.config().mode == "bars" ? state_.recent_bars() : state_.cursor().proofs;
    if (!proofs.empty()) {
        const auto fetched = settled_klines(venue_, state_.config(), proofs.front().ts, proofs.back().ts + 60000,
                                            [&](const auto& rows) { return same_bars(rows, proofs); });
        if (fetched.size() != proofs.size()) throw Error(20, "reconnect closed-bar overlap is unavailable");
        std::map<std::int64_t, Aggregate> verified;
        if (state_.config().mode == "ticks") state_.visit(0, [&](const std::string& line) {
            const auto event = parse_json(line);
            if (event.at("type").text() != "tick") return;
            const auto trade = normalized_trade(event);
            const auto minute = trade.ts - trade.ts % 60000;
            if (minute >= proofs.front().ts && minute <= proofs.back().ts) verified[minute].add(trade);
        });
        for (std::size_t index = 0; index < proofs.size(); ++index) {
            if (!(proofs[index] == fetched[index].bar)) throw Error(21, "reconnect already-emitted bar changed");
            if (state_.config().mode == "ticks") {
                try { verified.at(fetched[index].bar.ts).reconcile(fetched[index], false); }
                catch (const std::exception&) { throw Error(21, "reconnect closed-minute count or raw proof changed"); }
            }
        }
    }
    replay();
}

void FeedSession::anchor(std::uint64_t first) {
    if (state_.cursor().predecessor) return;
    if (first <= 1) throw Error(20, "raw predecessor cannot be proven");
    const auto predecessor = venue_.history(first - 1, 1).front();
    state_.anchor(predecessor);
}
std::uint64_t FeedSession::next_id() const {
    const auto previous = state_.cursor().seq ? state_.cursor().seq : state_.cursor().predecessor ? state_.cursor().predecessor->id : 0;
    if (!previous || previous == std::numeric_limits<std::uint64_t>::max()) throw Error(20, "raw-trade cursor is not anchored");
    return previous + 1;
}
void FeedSession::put(const Trade& trade) {
    trade.validate();
    if (trade.ts < state_.cursor().start) return;
    if (state_.cursor().seq && trade.id <= state_.cursor().seq) {
        const auto previous = state_.trade(trade.id);
        if (!previous || !(*previous == trade)) throw Error(21, "duplicate raw trade conflicts with the immutable prefix");
        return;
    }
    const auto existing = pending_.find(trade.id);
    if (existing != pending_.end()) {
        if (!(existing->second == trade)) throw Error(21, "buffered raw duplicate conflicts");
        return;
    }
    const auto bytes = trade_bytes(trade);
    if (bytes > state_.config().max_queue_bytes - std::min(pending_bytes_, state_.config().max_queue_bytes))
        throw Error(22, "unconfirmed raw-trade buffer overrun");
    pending_bytes_ += bytes;
    pending_.emplace(trade.id, trade);
}
void FeedSession::emit_trade(const Trade& trade) {
    if (trade.ts < state_.cursor().last_tick_ts) throw Error(23, "venue matched time regressed along the contiguous raw-trade chain");
    if (trade.id != next_id() || trade.ts < state_.cursor().cut || trade.ts >= state_.cursor().cut + 60000)
        throw Error(20, "raw trade violates the complete chronological minute prefix");
    const auto buffered = pending_.find(trade.id);
    if (buffered != pending_.end() && !(buffered->second == trade))
        throw Error(21, "REST healing changed a buffered raw trade");
    aggregate_.add(trade);
    emit(trade.wire());
    if (buffered != pending_.end()) {
        pending_bytes_ -= trade_bytes(buffered->second);
        pending_.erase(buffered);
    }
}
void FeedSession::heal_to(std::uint64_t target) {
    while (next_id() <= target) {
        log("warn", "raw_gap_healing", Json::object({{"from_id", Json::number(std::to_string(next_id()))}, {"through_id", Json::number(std::to_string(target))}}));
        const auto page = venue_.history(next_id());
        bool progressed = false;
        for (const auto& trade : page) {
            if (trade.id > target) break;
            const auto buffered = pending_.find(trade.id);
            if (buffered != pending_.end() && !(buffered->second == trade)) throw Error(21, "REST raw healing conflicts with WebSocket data");
            emit_trade(trade);
            progressed = true;
        }
        if (!progressed) throw Error(20, "raw gap cannot advance within available history");
    }
}
void FeedSession::drain() {
    while (!pending_.empty()) {
        const auto trade = pending_.begin()->second;
        if (trade.ts >= state_.cursor().cut + 60000) break;
        if (trade.id < next_id()) throw Error(21, "buffered cursor fell behind the immutable prefix");
        if (trade.id > next_id()) heal_to(trade.id - 1);
        emit_trade(trade);
    }
}
// A minute whose own x=true kline was missed closes only on proof: a later x=true watermark, the contiguous
// raw-ID chain from the proven predecessor, the first print of a later minute as the fence, and REST n plus
// exact OHLCV. The REST row has no x=true or f..L, and none is manufactured for it.
void FeedSession::historical_minute() {
    if (state_.cursor().cut + 60000 > watermark_) throw Error(20, "historical tick closure lacks a confirmed WebSocket watermark");
    if (!state_.cursor().predecessor) anchor(venue_.first_trade_id(state_.cursor().start));
    const auto minute = state_.cursor().cut;
    std::optional<Trade> fence;
    while (!fence) {
        const auto page = venue_.history(next_id());
        for (const auto& trade : page) {
            const auto buffered = pending_.find(trade.id);
            if (buffered != pending_.end() && !(buffered->second == trade)) throw Error(21, "historical raw overlap changed");
            if (trade.ts >= minute + 60000) {
                fence = trade;
                break;
            }
            emit_trade(trade);
        }
    }
    if (!aggregate_.count || fence->id != next_id() || fence->id != aggregate_.last + 1)
        throw Error(20, "historical minute lacks a contiguous raw prefix and next-minute fence");
    put(*fence);
    const auto kline = venue_.klines(minute, minute + 60000).front();
    aggregate_.reconcile(kline, false);
    log("info", "historical_tick_fence_verified", Json::object({{"minute", Json::number(std::to_string(minute))},
        {"first_id", Json::number(std::to_string(aggregate_.first))}, {"last_id", Json::number(std::to_string(aggregate_.last))}}));
    emit(time_line(minute + 60000), kline.bar);
    aggregate_ = Aggregate{};
}

void FeedSession::closed(const Kline& kline) {
    if (!kline.confirmed) return;
    const auto minute = kline.bar.ts;
    watermark_ = std::max(watermark_, minute + 60000);
    if (minute < state_.cursor().start) return;
    if (minute < state_.cursor().cut) {
        const auto old = state_.bar(minute);
        if (old) {
            if (!(*old == kline.bar)) throw Error(21, "already-emitted closed bar was revised");
        }
        if (state_.config().mode == "ticks") {
            Aggregate reconstructed;
            state_.visit(0, [&](const std::string& line) {
                const auto event = parse_json(line);
                if (event.at("type").text() == "tick") {
                    const auto trade = normalized_trade(event);
                    if (trade.ts >= minute && trade.ts < minute + 60000) reconstructed.add(trade);
                }
            });
            try { reconstructed.reconcile(kline, true); }
            catch (const Error& failure) {
                if (failure.code == 20 || failure.code == 21) throw Error(21, "already-emitted closed-minute fence was revised");
                throw;
            }
        } else if (!old) throw Error(21, "closed duplicate cannot be matched to the retained prefix");
        return;
    }
    if (state_.config().mode == "bars") {
        while (state_.cursor().cut < minute) {
            const auto page = venue_.klines(state_.cursor().cut, minute);
            for (const auto& historical : page) {
                if (historical.bar.ts + 60000 > watermark_) throw Error(20, "historical bar exceeds the confirmed watermark");
                emit(historical.bar.wire(), historical.bar);
            }
        }
        emit(kline.bar.wire(), kline.bar);
        return;
    }
    while (state_.cursor().cut < minute) historical_minute();
    if (!kline.count || kline.first <= 0 || kline.last < kline.first ||
        static_cast<std::uint64_t>(kline.last - kline.first) + 1 != kline.count)
        throw Error(20, "closed kline has ambiguous or noncontiguous raw bounds");
    anchor(static_cast<std::uint64_t>(kline.first));
    if (state_.cursor().seq > static_cast<std::uint64_t>(kline.last)) throw Error(21, "closed kline excludes an already-emitted trade");
    heal_to(static_cast<std::uint64_t>(kline.last));
    aggregate_.reconcile(kline, true);
    emit(time_line(minute + 60000), kline.bar);
    aggregate_ = Aggregate{};
    drain();
}
void FeedSession::stage(const VenueEvent& event) {
    if (event.kind == VenueEvent::Kind::Kline) { closed(event.kline); return; }
    if (event.kind != VenueEvent::Kind::Trade || state_.config().mode != "ticks" || event.trade.ts < state_.cursor().start) return;
    if (!state_.cursor().predecessor) anchor(venue_.first_trade_id(state_.cursor().start));
    put(event.trade);
    drain();
}

// Callers pass only closed candles: confirmed WebSocket rows, or REST rows behind a later confirmed one.
void reconcile_fenced(const Aggregate& prints, const Kline& candle) {
    if (!prints.count) {
        if (!Decimal(candle.bar.volume).zero()) throw Error(21, "confirmed candle volume shows prints the fenced chain does not contain");
        return;
    }
    if (!(Decimal(prints.open) == Decimal(candle.bar.open)) || !(Decimal(prints.high) == Decimal(candle.bar.high)) ||
        !(Decimal(prints.low) == Decimal(candle.bar.low)) || !(Decimal(prints.close) == Decimal(candle.bar.close)) ||
        !(prints.volume == Decimal(candle.bar.volume))) throw Error(21, "fenced minute exact OHLCV reconciliation failed");
}

FenceSession::FenceSession(State& state, Venue& venue, std::function<void(const std::string&)> output)
    : Session(state, venue, std::move(output)) {
    state_.visit(0, [&](const std::string& line) {
        const auto event = parse_json(line);
        if (event.at("type").text() == "tick") {
            const auto trade = normalized_trade(event);
            if (trade.ts >= state_.cursor().cut) aggregate_.add(trade);
        }
    });
}
std::uint64_t FenceSession::next_id() const {
    const auto previous = state_.cursor().seq ? state_.cursor().seq : state_.cursor().predecessor ? state_.cursor().predecessor->id : 0;
    if (!previous || previous == std::numeric_limits<std::uint64_t>::max()) throw Error(20, "print cursor is not anchored");
    return previous + 1;
}
void FenceSession::anchor() {
    if (state_.cursor().predecessor) return;
    state_.anchor(venue_.predecessor(state_.cursor().start));
}
void FenceSession::put(const Trade& trade, bool healed) {
    trade.validate();
    if (trade.ts < state_.cursor().start) {
        if (healed) throw Error(23, "venue matched time regressed: a print after the proven predecessor precedes the start minute");
        return;
    }
    // The predecessor is the last print before the start minute, and this print is at or after it.
    const auto& predecessor = state_.cursor().predecessor;
    if (predecessor && trade.id <= predecessor->id) {
        if (trade.id == predecessor->id) throw Error(21, "a print changed the proven start predecessor");
        throw Error(23, "venue matched time regressed: a print before the proven predecessor is at or after the start minute");
    }
    if (state_.cursor().seq && trade.id <= state_.cursor().seq) {
        const auto previous = state_.trade(trade.id);
        if (!previous || !(*previous == trade)) throw Error(21, "duplicate print conflicts with the immutable prefix");
        return;
    }
    const auto existing = pending_.find(trade.id);
    if (existing != pending_.end()) {
        if (!(existing->second == trade)) throw Error(21, healed ? "REST healing conflicts with WebSocket data" : "buffered duplicate print conflicts");
        return;
    }
    const auto bytes = trade_bytes(trade);
    if (bytes > state_.config().max_queue_bytes - std::min(pending_bytes_, state_.config().max_queue_bytes))
        throw Error(22, "unconfirmed print buffer overrun");
    pending_bytes_ += bytes;
    pending_.emplace(trade.id, trade);
}
void FenceSession::emit_trade(const Trade& trade) {
    if (trade.ts < state_.cursor().last_tick_ts) throw Error(23, "venue matched time regressed along the contiguous print chain");
    if (trade.id != next_id() || trade.ts < state_.cursor().cut || trade.ts >= state_.cursor().cut + 60000)
        throw Error(20, "print violates the complete chronological minute prefix");
    aggregate_.add(trade);
    emit(trade.wire());
    const auto buffered = pending_.find(trade.id);
    if (buffered != pending_.end()) {
        pending_bytes_ -= trade_bytes(buffered->second);
        pending_.erase(buffered);
    }
}
// The venue documents a finite REST print history: a gap that starts before it is unhealable, so the
// feed stops instead of asking for prints the venue no longer serves. Venue time, not the local clock,
// measures the distance.
void FenceSession::within_retention() const {
    const auto window = venue_.retention_ms();
    const auto last = state_.cursor().last_tick_ts >= 0 ? state_.cursor().last_tick_ts :
        state_.cursor().predecessor ? state_.cursor().predecessor->ts : -1;
    if (window && last >= 0 && newest_ > last && newest_ - last > window)
        throw Error(20, "print gap starts beyond the venue's REST history window");
}
std::vector<Trade> FenceSession::page(bool patient) {
    within_retention();
    // REST may lag the WebSocket print that revealed the gap: about 7 seconds of spaced re-reads, like the
    // reconnect overlap, before an empty answer counts. Looking ahead without such a print reads once.
    for (unsigned int attempt = 0; attempt < (patient ? 4U : 1U); ++attempt) {
        if (attempt) pause_for(std::chrono::milliseconds((state_.config().allow_insecure ? 100 : 1000) << (attempt - 1)));
        auto prints = venue_.history(next_id());
        if (!prints.empty()) {
            if (prints.front().id != next_id()) throw Error(20, "print history page does not start at the cursor");
            return prints;
        }
    }
    return {};
}
void FenceSession::heal() {
    const auto target = pending_.begin()->first - 1;
    log("warn", "print_gap_healing", Json::object({{"from_id", Json::number(std::to_string(next_id()))}, {"through_id", Json::number(std::to_string(target))}}));
    const auto prints = page(true);
    if (prints.empty()) throw Error(20, "print gap cannot advance within available history");
    for (const auto& trade : prints) {
        if (trade.id > target) break;
        put(trade, true);
    }
}
// A later minute was confirmed while no print beyond the chain is in hand: look for the fence by ID.
// A quiet start waits: nothing is anchored, and no minute closes, until the venue holds a print at or
// after --start. An empty page means no print beyond the chain yet, not a gap.
bool FenceSession::forward() {
    if (forward_watermark_ >= watermark_) return false;
    forward_watermark_ = watermark_;
    if (!state_.cursor().predecessor) {
        const auto predecessor = venue_.predecessor_if_ready(state_.cursor().start);
        if (!predecessor) return false;
        state_.anchor(*predecessor);
    }
    const auto prints = page(false);
    for (const auto& trade : prints) put(trade, true);
    return !prints.empty();
}
bool FenceSession::close_minute() {
    const auto minute = state_.cursor().cut;
    auto found = candles_.find(minute);
    if (found == candles_.end()) {
        // REST candles are used only behind a later confirmed WebSocket candle.
        if (watermark_ <= minute + 60000) return false;
        for (unsigned int attempt = 0;; ++attempt) {
            try {
                for (const auto& kline : venue_.klines(minute, std::min<std::int64_t>(watermark_, minute + 100 * 60000)))
                    candles_.emplace(kline.bar.ts, kline);
                break;
            } catch (const Error& failure) {
                // A REST row can trail the WebSocket confirmation briefly; then the minute is unavailable.
                if (failure.code != 20 || attempt == 2) throw;
                pause_for(std::chrono::milliseconds(500 * (attempt + 1)));
            }
        }
        found = candles_.find(minute);
        if (found == candles_.end()) throw Error(20, "confirmed candle of a fenced minute is unavailable");
        log("info", "historical_candle_verified", Json::object({{"minute", Json::number(std::to_string(minute))}}));
    }
    const auto candle = found->second;
    // Aggregate prints close on the fence and the venue's closed candle alone: the candle is not their sum.
    if (venue_.candle_is_print_sum()) reconcile_fenced(aggregate_, candle);
    emit(time_line(minute + 60000), candle.bar);
    aggregate_ = Aggregate{};
    candles_.erase(candles_.begin(), candles_.upper_bound(minute));
    return true;
}
void FenceSession::candle(const Kline& kline) {
    if (!kline.confirmed) return;
    const auto minute = kline.bar.ts;
    watermark_ = std::max(watermark_, minute + 60000);
    if (minute < state_.cursor().start) return;
    if (minute < state_.cursor().cut) {
        for (const auto& proof : state_.cursor().proofs)
            if (proof.ts == minute) {
                if (!(proof == kline.bar)) throw Error(21, "already-closed minute's confirmed candle was revised");
                return;
            }
        // Older than the retained proofs: rebuild that minute from the journal, where the candle is a sum.
        if (!venue_.candle_is_print_sum()) return;
        Aggregate rebuilt;
        state_.visit(0, [&](const std::string& line) {
            const auto event = parse_json(line);
            if (event.at("type").text() != "tick") return;
            const auto trade = normalized_trade(event);
            if (trade.ts >= minute && trade.ts < minute + 60000) rebuilt.add(trade);
        });
        try { reconcile_fenced(rebuilt, kline); }
        catch (const Error&) { throw Error(21, "already-closed minute's confirmed candle was revised"); }
        return;
    }
    if (candles_.size() >= 100000) throw Error(22, "confirmed-candle buffer overrun");
    const auto [found, inserted] = candles_.emplace(minute, kline);
    if (!inserted && !(found->second.bar == kline.bar)) throw Error(21, "confirmed candle was revised before its minute closed");
}
void FenceSession::advance() {
    for (;;) {
        if (pending_.empty()) {
            if (!(watermark_ > state_.cursor().cut + 60000 && forward())) return;
            continue;
        }
        const auto next = pending_.begin()->second;
        if (next.id < next_id()) throw Error(21, "buffered cursor fell behind the immutable prefix");
        if (next.id > next_id()) { heal(); continue; }
        if (next.ts < state_.cursor().cut + 60000) { emit_trade(next); continue; }
        // `next` is the fence: the chain is complete through it, so minute `cut` holds no other print.
        if (!close_minute()) return;
    }
}
void FenceSession::stage(const VenueEvent& event) {
    if (event.kind == VenueEvent::Kind::Kline) {
        candle(event.kline);
        advance();
        return;
    }
    if (event.kind != VenueEvent::Kind::Trade) return;
    newest_ = std::max(newest_, event.trade.ts);
    if (event.trade.ts < state_.cursor().start) return;
    anchor();
    put(event.trade, false);
    advance();
}
void FenceSession::connected() {
    publish();
    venue_.reverify();
    const auto& trades = state_.recent_trades();
    if (!trades.empty()) {
        const auto fetched = settled_prints(venue_, state_.config(), trades);
        if (fetched.size() != trades.size()) throw Error(20, "reconnect print overlap is unavailable");
        for (std::size_t index = 0; index < trades.size(); ++index)
            if (!(trades[index] == fetched[index])) throw Error(21, "reconnect print overlap changed");
    } else if (state_.cursor().predecessor) {
        const auto fetched = venue_.history(state_.cursor().predecessor->id, 1);
        if (fetched.empty()) throw Error(20, "initial predecessor overlap is unavailable");
        if (!(fetched.front() == *state_.cursor().predecessor)) throw Error(21, "initial predecessor overlap changed");
    }
    const auto& proofs = state_.cursor().proofs;
    if (!proofs.empty()) {
        const auto fetched = settled_klines(venue_, state_.config(), proofs.front().ts, proofs.back().ts + 60000,
                                            [&](const auto& rows) { return same_bars(rows, proofs); });
        if (fetched.size() != proofs.size()) throw Error(20, "reconnect closed-candle overlap is unavailable");
        for (std::size_t index = 0; index < proofs.size(); ++index)
            if (!(proofs[index] == fetched[index].bar)) throw Error(21, "reconnect already-closed candle changed");
    }
    replay();
}

std::unique_ptr<Session> make_session(State& state, Venue& venue, std::function<void(const std::string&)> output) {
    const auto& mode = state.config().mode;
    if (tick_mode(mode) && venue.tick_proof() == TickProof::NextPrintFence)
        return std::make_unique<FenceSession>(state, venue, std::move(output));
    // FeedSession proves only raw ticks through the kline ID range; any other tick mode would drop prints.
    if (tick_mode(mode) && mode != "ticks") throw Error(23, "this venue has no proof for mode " + mode);
    return std::make_unique<FeedSession>(state, venue, std::move(output));
}

void run_feed(Config config) {
    // Instrument metadata fixes the quantity units before the cursor binds them.
    const auto venue = make_venue(config);
    config.qty_multiplier = venue->qty_multiplier();
    State state(config);
    try {
        WebSocketPump pump(config, venue->connection());
        const auto session = make_session(state, *venue, &output_line);
        for (;;) {
            auto message = pump.take();
            try {
                // Group commit: verify every source message already queued (bounded), then persist once.
                for (std::size_t taken = 1;; ++taken) {
                    if (message.connected) session->connected();
                    else for (const auto& event : venue->decode(message.text)) session->stage(event);
                    if (taken >= 1024 || !pump.try_take(message)) break;
                }
            } catch (const Error&) { session->salvage(); throw; }
            session->publish();
        }
    } catch (const Stopped&) { log("info", "stopped", state.status()); }
    catch (...) { log("error", "verified_cursor_retained", state.status()); throw; }
}

void warmup(const Config& config, const std::string& output) {
    if (config.start < 0 || config.end <= config.start || config.start % 60000 || config.end % 60000)
        throw Error(23, "warmup requires a nonempty minute-aligned exclusive range");
    if ((config.end - config.start) / 60000 > 100000) throw Error(22, "warmup range exceeds 100000 bars");
    if (std::filesystem::exists(output) || std::filesystem::exists(output + ".manifest.json")) throw Error(23, "warmup output already exists");
    const auto venue = make_venue(config);
    std::int64_t watermark = -1;
    std::map<std::int64_t, Bar> confirmed;
    {
        WebSocketPump pump(config, venue->connection());
        while (watermark < config.end) {
            const auto message = pump.take();
            if (message.connected) continue;
            for (const auto& event : venue->decode(message.text)) {
                if (event.kind != VenueEvent::Kind::Kline || !event.kline.confirmed) continue;
                const auto& bar = event.kline.bar;
                watermark = std::max(watermark, bar.ts + 60000);
                if (bar.ts < config.start || bar.ts >= config.end) continue;
                const auto [found, inserted] = confirmed.emplace(bar.ts, bar);
                if (!inserted && !(found->second == bar)) throw Error(21, "confirmed WebSocket bar was revised during warmup");
            }
        }
    }
    std::string csv = "timestamp,open,high,low,close,volume\n";
    auto next = config.start;
    std::uint64_t count = 0, cross_checked = 0;
    while (next < config.end) {
        // The newest rows come from REST moments after the WS close: the confirmed bars in hand must match.
        const auto page = settled_klines(*venue, config, next, config.end, [&](const auto& rows) {
            for (const auto& row : rows) {
                const auto observed = confirmed.find(row.bar.ts);
                if (observed != confirmed.end() && !(observed->second == row.bar)) return false;
            }
            return true;
        });
        for (const auto& kline : page) {
            if (kline.bar.ts != next || next + 60000 > watermark) throw Error(20, "warmup does not adjoin a verified closed-minute cut");
            const auto& bar = kline.bar;
            const auto observed = confirmed.find(bar.ts);
            if (observed != confirmed.end()) {
                if (!(observed->second == bar)) throw Error(21, "warmup REST row differs from the confirmed WebSocket bar");
                ++cross_checked;
            }
            csv += std::to_string(bar.ts) + ',' + bar.open + ',' + bar.high + ',' + bar.low + ',' + bar.close + ',' + bar.volume + '\n';
            if (csv.size() > 16 * 1024 * 1024) throw Error(22, "warmup output exceeds 16 MiB");
            next += 60000;
            ++count;
        }
    }
    const auto manifest = Json::object({{"schema", Json::string("pineforge-feed-warmup/v1")},
        {"venue", Json::string(config.venue)}, {"market", Json::string(config.market)}, {"symbol", Json::string(config.symbol)},
        {"source", Json::string(config.rest_url + venue->kline_source())}, {"interval", Json::string("1m")},
        {"units", Json::object({{"price", Json::string("quote/base")}, {"volume", Json::string("base")}, {"timestamp", Json::string("unix-ms")}})},
        {"qty_multiplier", Json::string(venue->qty_multiplier())},
        {"start", Json::number(std::to_string(config.start))}, {"end_exclusive", Json::number(std::to_string(config.end))},
        {"cut", Json::number(std::to_string(config.end))}, {"confirmed_ws_watermark", Json::number(std::to_string(watermark))},
        {"bars", Json::number(std::to_string(count))}, {"ws_cross_checked_bars", Json::number(std::to_string(cross_checked))},
        {"sha256", Json::string(sha256(csv))}});
    atomic_file(output, csv);
    atomic_file(output + ".manifest.json", manifest.dump() + '\n');
    log("info", "warmup_verified", manifest);
}
}
