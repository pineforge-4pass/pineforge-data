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
Frame binance_frame(const std::string& message) {
    try {
        const auto envelope = parse_json(message);
        const auto* wrapped = envelope.find("data");
        const auto& data = wrapped ? *wrapped : envelope;
        const auto* event = data.find("e");
        return event && event->kind == Json::Kind::String && event->text() == "serverShutdown" ? Frame::Retire : Frame::Data;
    } catch (const std::exception&) { throw Error(23, "invalid public WebSocket JSON"); }
}

Connection BinanceSpot::connection() const {
    const auto symbol = lower(config_.symbol);
    return {"/stream?streams=" + (config_.mode == "ticks" ? symbol + "@trade/" + symbol + "@kline_1m" : symbol + "@kline_1m"), {}, {}, &binance_frame};
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
    return {"/market/stream?streams=" + (config.mode == "agg-ticks" ? symbol + "@aggTrade/" + symbol + "@kline_1m" : symbol + "@kline_1m"),
            {}, {}, &binance_frame};
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
            if (type == "aggTrade") return {{VenueEvent::Kind::Trade, usdm_aggregate(data), {}}};
            const auto& row = data.at("k");
            if (row.at("s").text() != config_.symbol) throw Error(23, "kline symbol changed");
            return {{VenueEvent::Kind::Kline, {}, binance_kline(row, true)}};
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
std::uint64_t BinanceUsdm::first_trade_id(std::int64_t minute) {
    // The first aggregate at or after the start, searched over the next hour (the venue's longest time
    // window), so a quiet start minute still has a fence; the fence session closes that minute on it.
    for (unsigned int attempt = 0; attempt < 3; ++attempt) {
        if (attempt) pause_for(std::chrono::milliseconds(500 * attempt));
        const auto response = http_.get("/fapi/v1/aggTrades?symbol=" + config_.symbol + "&startTime=" + std::to_string(minute) +
            "&endTime=" + std::to_string(minute + 3599999) + "&limit=1", 20);
        const auto& rows = array(response, 1);
        if (rows.empty()) continue;
        const auto first = usdm_aggregate(rows.front());
        if (first.ts < minute || first.ts >= minute + 3600000) throw Error(20, "initial aggregate fence is unavailable or ambiguous");
        return first.id;
    }
    throw Error(20, "no aggregate after the start minute is available yet");
}
std::vector<Kline> BinanceUsdm::klines(std::int64_t start, std::int64_t end) {
    if (start < 0 || start % 60000 || end <= start || end % 60000) throw Error(23, "invalid exclusive kline range");
    const auto limit = static_cast<std::size_t>(std::min<std::int64_t>(1000, (end - start) / 60000));
    const auto response = http_.get("/fapi/v1/klines?symbol=" + config_.symbol + "&interval=1m&startTime=" + std::to_string(start) +
        "&endTime=" + std::to_string(end - 1) + "&limit=" + std::to_string(limit), usdm_kline_weight(limit));
    return contiguous_klines(response, start, end);
}
}
