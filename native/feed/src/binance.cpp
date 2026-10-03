// SPDX-License-Identifier: Apache-2.0
#include "binance.hpp"
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <limits>

namespace pineforge::feed {
namespace {
const std::vector<Json>& array(const Json& value, std::size_t maximum) {
    if (value.kind != Json::Kind::Array || value.items.size() > maximum) throw Error(23, "invalid public market-data array");
    return value.items;
}
bool boolean(const Json& value) {
    if (value.kind != Json::Kind::Bool) throw Error(23, "confirmation must be a JSON boolean");
    return value.value == "true";
}
std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char letter) { return static_cast<char>(std::tolower(letter)); });
    return value;
}
std::size_t trade_bytes(const Trade& trade) { return trade.price.size() + trade.qty.size() + 192; }
}

Trade binance_trade(const Json& value, bool websocket) {
    Trade trade{value.at(websocket ? "t" : "id").integer<std::uint64_t>(),
                value.at(websocket ? "T" : "time").integer<std::int64_t>(),
                value.at(websocket ? "p" : "price").text(), value.at(websocket ? "q" : "qty").text()};
    trade.validate();
    return trade;
}
Kline binance_kline(const Json& value, bool websocket) {
    Kline kline;
    if (websocket) {
        if (value.at("i").text() != "1m") throw Error(23, "only UTC one-minute klines are supported");
        kline.bar = {value.at("t").integer<std::int64_t>(), value.at("o").text(), value.at("h").text(),
                      value.at("l").text(), value.at("c").text(), value.at("v").text()};
        kline.bar.validate();
        if (value.at("T").integer<std::int64_t>() != kline.bar.ts + 59999) throw Error(23, "invalid inclusive one-minute kline close");
        kline.first = value.at("f").integer<std::int64_t>();
        kline.last = value.at("L").integer<std::int64_t>();
        kline.count = value.at("n").integer<std::uint64_t>();
        kline.confirmed = boolean(value.at("x"));
        if (kline.count && (kline.first <= 0 || kline.last < kline.first)) throw Error(23, "invalid kline trade-id fence");
    } else {
        const auto& row = array(value, 12);
        if (row.size() != 12) throw Error(23, "REST kline must have twelve fields");
        kline.bar = {row[0].integer<std::int64_t>(), row[1].text(), row[2].text(), row[3].text(), row[4].text(), row[5].text()};
        kline.bar.validate();
        if (row[6].integer<std::int64_t>() != kline.bar.ts + 59999) throw Error(23, "invalid REST kline close time");
        kline.count = row[8].integer<std::uint64_t>();
    }
    return kline;
}

std::string BinanceSpot::streams() const {
    const auto symbol = lower(config_.symbol);
    return symbol + "@trade/" + symbol + "@kline_1m";
}
VenueEvent BinanceSpot::decode(const std::string& message) const {
    try {
        const auto envelope = parse_json(message);
        const auto* wrapped = envelope.find("data");
        const auto& data = wrapped ? *wrapped : envelope;
        const auto* event = data.find("e");
        if (!event) throw Error(23, "unexpected public stream control message");
        if (event->text() == "serverShutdown") return {};
        if (data.at("s").text() != config_.symbol) throw Error(23, "public stream symbol changed");
        if (event->text() == "trade") return {VenueEvent::Kind::Trade, binance_trade(data, true), {}};
        if (event->text() == "kline") {
            const auto& row = data.at("k");
            if (row.at("s").text() != config_.symbol) throw Error(23, "kline symbol changed");
            return {VenueEvent::Kind::Kline, {}, binance_kline(row, true)};
        }
        throw Error(23, "unsupported public stream event; aggregate prints are not raw trades");
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid Binance spot message shape"); }
}

std::vector<Trade> BinanceSpot::history(std::uint64_t from, std::size_t limit) {
    if (!from || !limit || limit > 1000) throw Error(23, "invalid raw-history page request");
    for (unsigned int attempt = 0; attempt < 3; ++attempt) {
        const auto response = http_.get("/api/v3/historicalTrades?symbol=" + config_.symbol + "&fromId=" + std::to_string(from) + "&limit=" + std::to_string(limit), 25);
        const auto& rows = array(response, limit);
        if (rows.empty()) continue;
        std::vector<Trade> trades;
        std::uint64_t expected = from;
        for (const auto& row : rows) {
            const auto trade = binance_trade(row, false);
            if (trade.id != expected) throw Error(20, "raw-history page has a missing or nonunit venue trade ID");
            trades.push_back(trade);
            if (expected == std::numeric_limits<std::uint64_t>::max()) throw Error(23, "venue trade ID exhausted");
            ++expected;
        }
        return trades;
    }
    throw Error(20, "raw history exhausted or unavailable within the venue retention window");
}
std::uint64_t BinanceSpot::first_trade_id(std::int64_t minute) {
    const auto response = http_.get("/api/v3/aggTrades?symbol=" + config_.symbol + "&startTime=" + std::to_string(minute) +
        "&endTime=" + std::to_string(minute + 59999) + "&limit=1", 4);
    const auto& rows = array(response, 1);
    if (rows.empty()) throw Error(20, "initial minute has no available raw-trade start fence");
    const auto first = rows.front().at("f").integer<std::uint64_t>();
    const auto matched = rows.front().at("T").integer<std::int64_t>();
    if (first <= 1 || matched < minute || matched >= minute + 60000) throw Error(20, "initial raw-trade fence is unavailable or ambiguous");
    return first;
}
std::vector<Kline> BinanceSpot::klines(std::int64_t start, std::int64_t end) {
    if (start < 0 || start % 60000 || end <= start || end % 60000) throw Error(23, "invalid exclusive kline range");
    const auto response = http_.get("/api/v3/klines?symbol=" + config_.symbol + "&interval=1m&startTime=" + std::to_string(start) +
        "&endTime=" + std::to_string(end - 1) + "&limit=1000", 2);
    std::vector<Kline> result;
    auto expected = start;
    for (const auto& row : array(response, 1000)) {
        const auto kline = binance_kline(row, false);
        if (kline.bar.ts != expected || kline.bar.ts >= end) throw Error(20, "historical klines have a missing or unexpected minute");
        expected += 60000;
        result.push_back(kline);
    }
    if (result.empty()) throw Error(20, "historical minute is unavailable");
    return result;
}

FeedSession::FeedSession(State& state, Venue& venue, std::function<void(const std::string&)> output)
    : state_(state), venue_(venue), output_(std::move(output)) {
    if (state_.config().mode == "ticks") state_.visit(0, [&](const std::string& line) {
        const auto event = parse_json(line);
        if (event.at("type").text() == "tick") {
            const auto trade = normalized_trade(event);
            if (trade.ts >= state_.cursor().cut) aggregate_.add(trade);
        }
    });
}

void FeedSession::emit(const std::string& line, const std::optional<Bar>& proof) {
    if (stopping) throw Stopped{};
    state_.commit(line, proof);
    output_(line);
    ++new_messages_;
    if (state_.config().max_messages && new_messages_ >= state_.config().max_messages) throw Stopped{};
}
void FeedSession::connected() {
    const auto& trades = state_.recent_trades();
    if (!trades.empty()) {
        const auto fetched = venue_.history(trades.front().id, trades.size());
        if (fetched.size() != trades.size()) throw Error(20, "reconnect raw overlap is unavailable");
        for (std::size_t index = 0; index < trades.size(); ++index)
            if (!(trades[index] == fetched[index])) throw Error(21, "reconnect raw overlap changed");
    } else if (state_.cursor().predecessor) {
        const auto fetched = venue_.history(state_.cursor().predecessor->id, 1);
        if (!(fetched.front() == *state_.cursor().predecessor)) throw Error(21, "initial predecessor overlap changed");
    }
    const auto& proofs = state_.config().mode == "bars" ? state_.recent_bars() : state_.cursor().proofs;
    if (!proofs.empty()) {
        const auto fetched = venue_.klines(proofs.front().ts, proofs.back().ts + 60000);
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
                auto checked = fetched[index];
                checked.confirmed = true;
                try { verified.at(checked.bar.ts).reconcile(checked, false); }
                catch (const std::exception&) { throw Error(21, "reconnect closed-minute count or raw proof changed"); }
            }
        }
    }
    if (!replayed_) {
        if (state_.config().resume) state_.visit(state_.config().output_from, output_);
        replayed_ = true;
    }
    log("info", "overlap_verified", state_.status());
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
    if (trade.id != next_id() || trade.ts < state_.cursor().cut || trade.ts >= state_.cursor().cut + 60000 || trade.ts < state_.cursor().last_tick_ts)
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
void FeedSession::historical_minute() {
    if (state_.cursor().cut + 60000 > watermark_) throw Error(20, "historical tick closure lacks a confirmed WebSocket watermark");
    if (!state_.cursor().predecessor) anchor(venue_.first_trade_id(state_.cursor().start));
    const auto minute = state_.cursor().cut;
    bool fenced = false;
    while (!fenced) {
        const auto page = venue_.history(next_id());
        for (const auto& trade : page) {
            const auto buffered = pending_.find(trade.id);
            if (buffered != pending_.end() && !(buffered->second == trade)) throw Error(21, "historical raw overlap changed");
            if (trade.ts >= minute + 60000) {
                if (trade.id != next_id()) throw Error(20, "historical next-minute raw fence is not contiguous");
                put(trade);
                fenced = true;
                break;
            }
            emit_trade(trade);
        }
    }
    auto kline = venue_.klines(minute, minute + 60000).front();
    kline.confirmed = true;
    kline.first = static_cast<std::int64_t>(aggregate_.first);
    kline.last = static_cast<std::int64_t>(aggregate_.last);
    aggregate_.reconcile(kline, true);
    log("info", "historical_tick_fence_verified", Json::object({{"minute", Json::number(std::to_string(minute))},
        {"first_id", Json::number(std::to_string(aggregate_.first))}, {"last_id", Json::number(std::to_string(aggregate_.last))}}));
    emit("{\"type\":\"time\",\"ts\":" + std::to_string(minute + 60000) + "}", kline.bar);
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
    emit("{\"type\":\"time\",\"ts\":" + std::to_string(minute + 60000) + "}", kline.bar);
    aggregate_ = Aggregate{};
    drain();
}
void FeedSession::ingest(const VenueEvent& event) {
    if (event.kind == VenueEvent::Kind::Kline) { closed(event.kline); return; }
    if (event.kind != VenueEvent::Kind::Trade || state_.config().mode != "ticks" || event.trade.ts < state_.cursor().start) return;
    if (!state_.cursor().predecessor) anchor(venue_.first_trade_id(state_.cursor().start));
    put(event.trade);
    drain();
}

void run_feed(const Config& config) {
    State state(config);
    try {
        BinanceSpot venue(config);
        WebSocketPump pump(config, venue.streams());
        FeedSession session(state, venue, &output_line);
        for (;;) {
            const auto message = pump.take();
            if (message.connected) session.connected();
            else session.ingest(venue.decode(message.text));
        }
    } catch (const Stopped&) { log("info", "stopped", state.status()); }
    catch (...) { log("error", "verified_cursor_retained", state.status()); throw; }
}

void warmup(const Config& config, const std::string& output) {
    if (config.start < 0 || config.end <= config.start || config.start % 60000 || config.end % 60000)
        throw Error(23, "warmup requires a nonempty minute-aligned exclusive range");
    if ((config.end - config.start) / 60000 > 100000) throw Error(22, "warmup range exceeds 100000 bars");
    if (std::filesystem::exists(output) || std::filesystem::exists(output + ".manifest.json")) throw Error(23, "warmup output already exists");
    BinanceSpot venue(config);
    std::int64_t watermark = -1;
    {
        WebSocketPump pump(config, venue.streams());
        while (watermark < config.end) {
            const auto message = pump.take();
            if (message.connected) continue;
            const auto event = venue.decode(message.text);
            if (event.kind == VenueEvent::Kind::Kline && event.kline.confirmed) watermark = std::max(watermark, event.kline.bar.ts + 60000);
        }
    }
    std::string csv = "timestamp,open,high,low,close,volume\n";
    auto next = config.start;
    std::uint64_t count = 0;
    while (next < config.end) {
        for (const auto& kline : venue.klines(next, config.end)) {
            if (kline.bar.ts != next || next + 60000 > watermark) throw Error(20, "warmup does not adjoin a verified closed-minute cut");
            const auto& bar = kline.bar;
            csv += std::to_string(bar.ts) + ',' + bar.open + ',' + bar.high + ',' + bar.low + ',' + bar.close + ',' + bar.volume + '\n';
            if (csv.size() > 16 * 1024 * 1024) throw Error(22, "warmup output exceeds 16 MiB");
            next += 60000;
            ++count;
        }
    }
    const auto manifest = Json::object({{"schema", Json::string("pineforge-feed-warmup/v1")},
        {"venue", Json::string(config.venue)}, {"market", Json::string(config.market)}, {"symbol", Json::string(config.symbol)},
        {"source", Json::string(config.rest_url + "/api/v3/klines")}, {"interval", Json::string("1m")},
        {"units", Json::object({{"price", Json::string("quote/base")}, {"volume", Json::string("base")}, {"timestamp", Json::string("unix-ms")}})},
        {"start", Json::number(std::to_string(config.start))}, {"end_exclusive", Json::number(std::to_string(config.end))},
        {"cut", Json::number(std::to_string(config.end))}, {"confirmed_ws_watermark", Json::number(std::to_string(watermark))},
        {"bars", Json::number(std::to_string(count))}, {"sha256", Json::string(sha256(csv))}});
    atomic_file(output, csv);
    atomic_file(output + ".manifest.json", manifest.dump() + '\n');
    log("info", "warmup_verified", manifest);
}
}
