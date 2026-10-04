// SPDX-License-Identifier: Apache-2.0
#include "binance.hpp"
#include <algorithm>
#include <cctype>
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
std::vector<Kline> contiguous_klines(const Json& response, std::int64_t start, std::int64_t end) {
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
// USD-M `q` is the aggregate's full executed quantity, RPI (retail price improvement) executions
// included; `nq` excludes those and would drop real executions from the print stream.
Trade usdm_aggregate(const Json& value) {
    Trade trade{value.at("a").integer<std::uint64_t>(), value.at("T").integer<std::int64_t>(), value.at("p").text(), value.at("q").text()};
    const auto first = value.at("f").integer<std::uint64_t>(), last = value.at("l").integer<std::uint64_t>();
    if (!first || last < first) throw Error(23, "invalid aggregate raw-trade range");
    trade.validate();
    return trade;
}
Frame binance_frame(const std::string& message, const std::vector<std::string>& streams) {
    try {
        const auto envelope = parse_json(message);
        // The reply to the liveness probe: {"result":[subscribed streams],"id":N}. Without them all, reconnect.
        if (envelope.find("id") && !envelope.find("stream") && !envelope.find("e")) {
            const auto* result = envelope.find("result");
            if (!result || result->kind != Json::Kind::Array || result->items.empty()) return Frame::Stale;
            for (const auto& stream : streams) {
                bool listed = false;
                for (const auto& item : result->items) listed = listed || (item.kind == Json::Kind::String && item.text() == stream);
                if (!listed) return Frame::Stale;
            }
            return Frame::Control;
        }
        if (envelope.find("result")) throw Error(23, "unexpected public stream reply");
        const auto* wrapped = envelope.find("data");
        const auto& data = wrapped ? *wrapped : envelope;
        const auto* event = data.find("e");
        return event && event->kind == Json::Kind::String && event->text() == "serverShutdown" ? Frame::Retire : Frame::Data;
    } catch (const std::exception&) { throw Error(23, "invalid public WebSocket JSON"); }
}

namespace {
// A combined-stream connection whose probe reply must list every one of its streams.
Connection combined(const std::string& prefix, const std::vector<std::string>& streams) {
    std::string path = prefix;
    for (const auto& stream : streams) path += (path.size() == prefix.size() ? "" : "/") + stream;
    return {path, {}, {}, [streams](const std::string& message) { return binance_frame(message, streams); },
            "{\"method\":\"LIST_SUBSCRIPTIONS\",\"id\":1}"};
}
}
Connection BinanceSpot::connection() const {
    const auto symbol = lower(config_.symbol);
    if (config_.mode == "ticks") return combined("/stream?streams=", {symbol + "@trade", symbol + "@kline_1m"});
    return combined("/stream?streams=", {symbol + "@kline_1m"});
}
std::vector<VenueEvent> BinanceSpot::decode(const std::string& message) const {
    try {
        const auto envelope = parse_json(message);
        const auto* wrapped = envelope.find("data");
        const auto& data = wrapped ? *wrapped : envelope;
        const auto* event = data.find("e");
        if (!event || event->kind != Json::Kind::String) throw Error(23, "unexpected public stream control message");
        const auto type = event->text();
        const auto stream = wrapped ? envelope.at("stream").text() : std::string();
        const auto symbol = lower(config_.symbol);
        const bool trades = stream == symbol + "@trade", klines = stream == symbol + "@kline_1m";
        // Subscribed data streams and data event types are strict: the documented shape, or stop.
        if (type == "trade" || type == "kline" || trades || klines) {
            if (wrapped && !(trades && type == "trade") && !(klines && type == "kline"))
                throw Error(23, "unsupported public stream event; aggregate prints are not raw trades");
            if (data.at("s").text() != config_.symbol) throw Error(23, "public stream symbol changed");
            if (type == "trade") return {{VenueEvent::Kind::Trade, binance_trade(data, true), {}}};
            const auto& row = data.at("k");
            if (row.at("s").text() != config_.symbol) throw Error(23, "kline symbol changed");
            return {{VenueEvent::Kind::Kline, {}, binance_kline(row, true)}};
        }
        if (type == "serverShutdown") return {};
        // A venue notice outside the data streams carries no market data; a new type must not stop the feed.
        log("warn", "unknown_stream_event", Json::object({{"stream", Json::string(stream)}, {"type", Json::string(type)}}));
        return {};
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
    // REST may lag the WebSocket print that triggered this lookup: retry like history() before stopping.
    for (unsigned int attempt = 0; attempt < 3; ++attempt) {
        if (attempt) pause_for(std::chrono::milliseconds(500 * attempt));
        const auto response = http_.get("/api/v3/aggTrades?symbol=" + config_.symbol + "&startTime=" + std::to_string(minute) +
            "&endTime=" + std::to_string(minute + 59999) + "&limit=1", 4);
        const auto& rows = array(response, 1);
        if (rows.empty()) continue;
        const auto first = rows.front().at("f").integer<std::uint64_t>();
        const auto matched = rows.front().at("T").integer<std::int64_t>();
        if (first <= 1 || matched < minute || matched >= minute + 60000) throw Error(20, "initial raw-trade fence is unavailable or ambiguous");
        return first;
    }
    throw Error(20, "initial minute has no available raw-trade start fence");
}
std::vector<Kline> BinanceSpot::klines(std::int64_t start, std::int64_t end) {
    if (start < 0 || start % 60000 || end <= start || end % 60000) throw Error(23, "invalid exclusive kline range");
    const auto response = http_.get("/api/v3/klines?symbol=" + config_.symbol + "&interval=1m&startTime=" + std::to_string(start) +
        "&endTime=" + std::to_string(end - 1) + "&limit=1000", 2);
    return contiguous_klines(response, start, end);
}

namespace {
RestPolicy usdm_rest() {
    RestPolicy policy;
    policy.endpoints = {"/fapi/v1/exchangeInfo", "/fapi/v1/aggTrades", "/fapi/v1/klines"};
    // The USD-M exchangeInfo document lists every contract and exceeds the 1 MiB parser bound.
    policy.limits_path = "/fapi/v1/exchangeInfo";
    policy.limits_weight = 1;
    policy.limits_bytes = 4 * 1024 * 1024;
    policy.rejected = [](long status, const std::string& body) {
        if (status != 400) return;
        if (body.find("\"code\":-4166") != std::string::npos)
            throw Error(20, "aggregate history is beyond the venue's 2-day aggTrades window");
        if (body.find("\"code\":-1121") != std::string::npos) throw Error(23, "unknown USD-M symbol");
    };
    return policy;
}
}
unsigned int usdm_kline_weight(std::size_t limit) { return limit < 100 ? 1 : limit < 500 ? 2 : limit <= 1000 ? 5 : 10; }
Connection usdm_connection(const Config& config) {
    const auto symbol = lower(config.symbol);
    if (config.mode == "agg-ticks") return combined("/market/stream?streams=", {symbol + "@aggTrade", symbol + "@kline_1m"});
    return combined("/market/stream?streams=", {symbol + "@kline_1m"});
}
BinanceUsdm::BinanceUsdm(const Config& config) : config_(config), http_(config, usdm_rest()) {
    // An unknown symbol is refused before any stream is opened (HTTP 400, code -1121).
    (void)http_.get("/fapi/v1/klines?symbol=" + config_.symbol + "&interval=1m&limit=1", 1);
}
std::vector<VenueEvent> BinanceUsdm::decode(const std::string& message) const {
    try {
        const auto envelope = parse_json(message);
        const auto* wrapped = envelope.find("data");
        const auto& data = wrapped ? *wrapped : envelope;
        const auto* event = data.find("e");
        if (!event || event->kind != Json::Kind::String) throw Error(23, "unexpected public stream control message");
        const auto type = event->text();
        const auto stream = wrapped ? envelope.at("stream").text() : std::string();
        const auto symbol = lower(config_.symbol);
        const bool aggregates = stream == symbol + "@aggTrade", klines = stream == symbol + "@kline_1m";
        // Raw @trade prints reach only unrouted connections and have no public REST history.
        if (type == "trade") throw Error(23, "raw USD-M trades are not a supported source; use the routed /market stream");
        if (type == "aggTrade" || type == "kline" || aggregates || klines) {
            if (wrapped && !(aggregates && type == "aggTrade" && config_.mode == "agg-ticks") && !(klines && type == "kline"))
                throw Error(23, "unsupported USD-M stream event");
            if (data.at("s").text() != config_.symbol) throw Error(23, "public stream symbol changed");
            if (type == "aggTrade") {
                const auto trade = usdm_aggregate(data);
                newest_ = std::max(newest_, trade.ts);
                return {{VenueEvent::Kind::Trade, trade, {}}};
            }
            const auto& row = data.at("k");
            if (row.at("s").text() != config_.symbol) throw Error(23, "kline symbol changed");
            const auto kline = binance_kline(row, true);
            newest_ = std::max(newest_, kline.bar.ts + 59999);
            return {{VenueEvent::Kind::Kline, {}, kline}};
        }
        if (type == "serverShutdown") return {};
        log("warn", "unknown_stream_event", Json::object({{"stream", Json::string(stream)}, {"type", Json::string(type)}}));
        return {};
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid Binance USD-M message shape"); }
}
std::vector<Trade> BinanceUsdm::history(std::uint64_t from, std::size_t limit) {
    if (!from || !limit || limit > 1000) throw Error(23, "invalid aggregate-history page request");
    const auto response = http_.get("/fapi/v1/aggTrades?symbol=" + config_.symbol + "&fromId=" + std::to_string(from) + "&limit=" + std::to_string(limit), 20);
    std::vector<Trade> trades;
    std::uint64_t expected = from;
    for (const auto& row : array(response, limit)) {
        const auto trade = usdm_aggregate(row);
        if (trade.id != expected) throw Error(20, "aggregate-history page has a missing or nonunit aggregate ID");
        trades.push_back(trade);
        if (expected == std::numeric_limits<std::uint64_t>::max()) throw Error(23, "venue aggregate ID exhausted");
        ++expected;
    }
    return trades;
}
// The first aggregate at or after the start, searched hour window by hour window (the venue's longest time
// window) up to the newest venue time seen on the WebSocket, so a quiet start of any length within the 48-hour
// history finds its fence; 0 when none is visible yet. A window that ended two minutes before that venue time
// is settled (REST trails the WebSocket by seconds) and is not read again.
std::uint64_t BinanceUsdm::first_aggregate(std::int64_t minute) {
    for (auto window = std::max(minute, quiet_until_);; window += 3600000) {
        if (window - minute >= retention_ms()) throw Error(20, "no aggregate within the venue's 48-hour history after the start minute");
        const auto response = http_.get("/fapi/v1/aggTrades?symbol=" + config_.symbol + "&startTime=" + std::to_string(window) +
            "&endTime=" + std::to_string(window + 3599999) + "&limit=1", 20);
        const auto& rows = array(response, 1);
        if (!rows.empty()) {
            const auto first = usdm_aggregate(rows.front());
            if (first.ts < window || first.ts >= window + 3600000) throw Error(20, "initial aggregate fence is unavailable or ambiguous");
            return first.id;
        }
        if (window + 3600000 + 120000 <= newest_) quiet_until_ = window + 3600000;
        if (window + 3600000 > newest_) return 0;
    }
}
std::uint64_t BinanceUsdm::first_trade_id(std::int64_t minute) {
    for (unsigned int attempt = 0; attempt < 3; ++attempt) {
        if (attempt) pause_for(std::chrono::milliseconds(500 * attempt));
        if (const auto first = first_aggregate(minute)) return first;
    }
    throw Error(20, "no aggregate after the start minute is available yet");
}
std::optional<Trade> BinanceUsdm::predecessor_if_ready(std::int64_t minute) {
    const auto first = first_aggregate(minute);
    if (!first) return std::nullopt;
    if (first <= 1) throw Error(20, "raw predecessor cannot be proven");
    const auto previous = history(first - 1, 1);
    if (previous.empty() || previous.front().id != first - 1) throw Error(20, "raw predecessor is unavailable");
    return previous.front();
}
std::vector<Kline> BinanceUsdm::klines(std::int64_t start, std::int64_t end) {
    if (start < 0 || start % 60000 || end <= start || end % 60000) throw Error(23, "invalid exclusive kline range");
    const auto limit = static_cast<std::size_t>(std::min<std::int64_t>(1000, (end - start) / 60000));
    const auto response = http_.get("/fapi/v1/klines?symbol=" + config_.symbol + "&interval=1m&startTime=" + std::to_string(start) +
        "&endTime=" + std::to_string(end - 1) + "&limit=" + std::to_string(limit), usdm_kline_weight(limit));
    return contiguous_klines(response, start, end);
}
}
