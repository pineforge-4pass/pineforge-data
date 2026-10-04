// SPDX-License-Identifier: Apache-2.0
#include "okx.hpp"
#include <charconv>

namespace pineforge::feed {
namespace {
// OKX sends identifiers and times as decimal strings.
template<class Integer> Integer digits(const Json& value) {
    const auto text = value.text();
    Integer result{};
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    if (text.empty() || (text.size() > 1 && text.front() == '0') || text.find_first_not_of("0123456789") != std::string::npos ||
        parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size())
        throw Error(23, "invalid OKX decimal identifier or time");
    return result;
}
const std::vector<Json>& rows(const Json& value, std::size_t maximum) {
    if (value.kind != Json::Kind::Array || value.items.size() > maximum) throw Error(23, "invalid OKX data array");
    return value.items;
}
RestPolicy okx_rest() {
    RestPolicy policy;
    policy.endpoints = {"/api/v5/public/instruments", "/api/v5/market/history-trades", "/api/v5/market/history-candles"};
    // Each endpoint allows 20 requests per 2 seconds per IP: spend at most half.
    policy.milliseconds_per_request = 200;
    policy.throttled = [](const std::string& body) { return body.find("\"code\":\"50011\"") != std::string::npos; };
    // Service unavailable, endpoint timeout, system busy, system error: the venue asks to retry.
    policy.transient = [](long, const std::string& body) {
        for (const char* code : {"50001", "50004", "50013", "50026"})
            if (body.find(std::string("\"code\":\"") + code + "\"") != std::string::npos) return true;
        return false;
    };
    return policy;
}
}

Trade okx_trade(const Json& row, const std::string& symbol, const std::string& multiplier) {
    if (row.find("count")) throw Error(23, "aggregated OKX trades (count) are never raw prints");
    if (row.at("instId").text() != symbol) throw Error(23, "OKX instrument changed");
    Trade trade{digits<std::uint64_t>(row.at("tradeId")), digits<std::int64_t>(row.at("ts")), row.at("px").text(),
                scaled_quantity(row.at("sz").text(), multiplier)};
    trade.validate();
    return trade;
}
Kline okx_candle(const Json& row, const std::string& multiplier) {
    const auto& fields = rows(row, 9);
    if (fields.size() != 9) throw Error(23, "OKX candle must have nine fields");
    Kline kline;
    kline.bar = {digits<std::int64_t>(fields[0]), fields[1].text(), fields[2].text(), fields[3].text(), fields[4].text(),
                 scaled_quantity(fields[5].text(), multiplier)};
    kline.bar.validate();
    const auto confirm = fields[8].text();
    if (confirm != "0" && confirm != "1") throw Error(23, "OKX candle confirm must be \"0\" or \"1\"");
    kline.confirmed = confirm == "1";
    return kline;
}
std::string okx_multiplier(const Json& row, const std::string& market, const std::string& symbol) {
    if (row.at("instId").text() != symbol) throw Error(23, "OKX instrument metadata does not match the symbol");
    if (row.at("state").text() != "live") throw Error(23, "OKX instrument is not live");
    const auto type = row.at("instType").text();
    if (market == "spot") {
        if (type != "SPOT") throw Error(23, "OKX instrument is not a spot pair");
        return "1";
    }
    if (type != "SWAP") throw Error(23, "OKX instrument is not a perpetual swap");
    if (row.at("ctType").text() != "linear")
        throw Error(23, "inverse OKX swaps are refused: their contracts are quote-denominated, not base quantities");
    if (row.at("ctValCcy").text() != symbol.substr(0, symbol.find('-'))) throw Error(23, "OKX contract value is not in the base currency");
    const Decimal value(row.at("ctVal").text()), multiple(row.at("ctMult").text());
    if (value.zero() || multiple.zero()) throw Error(23, "OKX contract value or multiplier is zero");
    return value.multiply(multiple).str();
}
Frame okx_frame(const std::string& message) {
    if (message == "pong") return Frame::Control;
    try {
        const auto envelope = parse_json(message);
        const auto* event = envelope.find("event");
        if (!event) return Frame::Data;
        const auto name = event->text();
        if (name == "subscribe" || name == "unsubscribe") return Frame::Control;
        if (name == "error") {
            const auto* code = envelope.find("code");
            throw Error(23, "OKX rejected the subscription (code " + (code && code->kind == Json::Kind::String ? code->text() : std::string("?")) + ")");
        }
        // 64008: the venue closes this connection for an upgrade; reconnect with overlap now.
        if (name == "notice") return Frame::Retire;
        log("warn", "unknown_stream_event", Json::object({{"type", Json::string(name)}}));
        return Frame::Control;
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid OKX WebSocket JSON"); }
}

Okx::Okx(const Config& config) : config_(config), http_(config, okx_rest()) {
    multiplier_ = instrument_multiplier();
    log("info", "instrument_verified", Json::object({{"symbol", Json::string(config_.symbol)}, {"qty_multiplier", Json::string(multiplier_)}}));
}
std::string Okx::instrument_multiplier() {
    const auto& instruments = data("/api/v5/public/instruments?instType=" + std::string(config_.market == "swap" ? "SWAP" : "SPOT") +
                                   "&instId=" + config_.symbol).items;
    if (instruments.size() != 1) throw Error(23, "unknown OKX instrument");
    return okx_multiplier(instruments.front(), config_.market, config_.symbol);
}
// Every reconnect re-reads ctVal x ctMult: quantities already emitted were converted with the old one.
void Okx::reverify() {
    if (instrument_multiplier() != multiplier_) throw Error(21, "OKX contract multiplier changed during the run");
}
Json Okx::data(const std::string& path) {
    const auto response = http_.get(path, 1);
    try {
        if (response.at("code").text() != "0") throw Error(23, "OKX rejected the public request (code " + response.at("code").text() + ")");
        auto result = response.at("data");
        (void)rows(result, 1000);
        return result;
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid OKX REST envelope"); }
}
Connection Okx::connection() const {
    const auto argument = [&](const char* channel) { return std::string("{\"channel\":\"") + channel + "\",\"instId\":\"" + config_.symbol + "\"}"; };
    const auto subscribe = "{\"op\":\"subscribe\",\"args\":[" + argument("candle1m") +
        (config_.mode == "ticks" ? "," + argument("trades-all") : std::string()) + "]}";
    // Business endpoint: trades-all and candles live there. The text keepalive must precede 30 s idle.
    return {"/ws/v5/business", {subscribe}, "ping", &okx_frame, {}};
}
std::vector<VenueEvent> Okx::decode(const std::string& message) const {
    try {
        const auto envelope = parse_json(message);
        if (const auto* event = envelope.find("event")) {
            log("info", "source_notice", Json::object({{"type", Json::string(event->text())}}));
            return {};
        }
        const auto& argument = envelope.at("arg");
        const auto channel = argument.at("channel").text();
        if (argument.at("instId").text() != config_.symbol) throw Error(23, "public stream instrument changed");
        if (channel == "trades") throw Error(23, "the aggregated OKX trades channel is never a tick source");
        if (channel != "candle1m" && channel != "trades-all") throw Error(23, "unsupported OKX channel");
        std::vector<VenueEvent> events;
        for (const auto& row : rows(envelope.at("data"), 1024)) {
            if (channel == "candle1m") events.push_back({VenueEvent::Kind::Kline, {}, okx_candle(row, multiplier_)});
            else events.push_back({VenueEvent::Kind::Trade, okx_trade(row, config_.symbol, multiplier_), {}});
        }
        return events;
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid OKX message shape"); }
}
std::vector<Trade> Okx::history(std::uint64_t from, std::size_t limit) {
    if (!from || !limit || limit > 1000) throw Error(23, "invalid raw-history page request");
    std::vector<Trade> result;
    auto next = from;
    while (result.size() < limit) {
        const auto count = std::min<std::size_t>(100, limit - result.size());
        // `after` and `before` are exclusive trade-ID bounds; rows arrive newest first.
        const auto page = data("/api/v5/market/history-trades?instId=" + config_.symbol + "&type=1&after=" +
            std::to_string(next + count) + "&before=" + std::to_string(next - 1) + "&limit=" + std::to_string(count));
        const auto& items = rows(page, count);
        for (auto row = items.rbegin(); row != items.rend(); ++row) {
            const auto trade = okx_trade(*row, config_.symbol, multiplier_);
            if (trade.id != next) throw Error(20, "trade-id page has a missing or nonunit venue trade ID");
            result.push_back(trade);
            ++next;
        }
        if (items.size() < count) break;
    }
    return result;
}
// The time lookup alone is not proof: REST can trail the newest prints, and prints of one millisecond need
// not come back in ID order. Walk forward by ID until the next print is at or after the minute; only then
// is the candidate the last print before it. Nothing is anchored until that is seen.
std::optional<Trade> Okx::walk_to(std::int64_t minute) {
    // type=2 pages by time: `after` returns prints strictly earlier than the minute, newest first.
    const auto page = data("/api/v5/market/history-trades?instId=" + config_.symbol + "&type=2&after=" + std::to_string(minute) + "&limit=1");
    const auto& items = rows(page, 1);
    if (items.empty()) throw Error(20, "no print precedes the start minute within the venue history");
    auto candidate = okx_trade(items.front(), config_.symbol, multiplier_);
    if (candidate.ts >= minute) throw Error(20, "predecessor lookup returned a print at or after the start minute");
    for (;;) {
        const auto later = history(candidate.id + 1, 100);
        for (const auto& trade : later) {
            if (trade.ts >= minute) return candidate;
            candidate = trade;
        }
        if (later.size() < 100) return std::nullopt;
    }
}
// A WebSocket print at or after the minute exists: REST must show its successor within about 15 seconds.
Trade Okx::predecessor(std::int64_t minute) {
    for (unsigned int wait = 0; wait < 4; ++wait) {
        if (const auto found = walk_to(minute)) return *found;
        pause_for(std::chrono::milliseconds((config_.allow_insecure ? 100 : 1000) << wait));
    }
    throw Error(20, "the print after the start predecessor is not yet available over REST");
}
std::optional<Trade> Okx::predecessor_if_ready(std::int64_t minute) { return walk_to(minute); }
std::vector<Kline> Okx::klines(std::int64_t start, std::int64_t end) {
    if (start <= 0 || start % 60000 || end <= start || end % 60000) throw Error(23, "invalid exclusive kline range");
    std::vector<Kline> result;
    auto next = start;
    while (next < end) {
        const auto window = std::min<std::int64_t>(end, next + 100 * 60000);
        const auto page = data("/api/v5/market/history-candles?instId=" + config_.symbol + "&bar=1m&after=" + std::to_string(window) +
                               "&before=" + std::to_string(next - 1) + "&limit=100");
        const auto& items = rows(page, 100);
        bool forming = false;
        for (auto row = items.rbegin(); row != items.rend(); ++row) {
            const auto kline = okx_candle(*row, multiplier_);
            if (kline.bar.ts != next || kline.bar.ts >= window) throw Error(20, "historical candles have a missing or unexpected minute");
            // A REST row is a closed candle only with confirm "1"; the confirmed prefix ends at a forming one.
            if (!kline.confirmed) { forming = true; break; }
            result.push_back(kline);
            next += 60000;
        }
        if (forming || next < window) break;
    }
    if (result.empty()) throw Error(20, "historical minute is unavailable or not yet confirmed");
    return result;
}
}
