// SPDX-License-Identifier: Apache-2.0
#include "bybit.hpp"
#include <algorithm>
#include <charconv>

namespace pineforge::feed {
namespace {
std::int64_t milliseconds(const Json& value) {
    // WebSocket rows carry integers, REST rows decimal strings.
    const auto text = value.kind == Json::Kind::String ? value.text() : value.value;
    if (value.kind != Json::Kind::String && value.kind != Json::Kind::Number) throw Error(23, "invalid Bybit time");
    std::int64_t result = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos || parsed.ec != std::errc{} ||
        parsed.ptr != text.data() + text.size()) throw Error(23, "invalid Bybit time");
    return result;
}
RestPolicy bybit_rest() {
    RestPolicy policy;
    policy.endpoints = {"/v5/market/kline", "/v5/market/instruments-info"};
    // 600 requests per 5 seconds per IP; this spends well under a fifth of it.
    policy.milliseconds_per_request = 100;
    policy.throttled = [](const std::string& body) { return body.find("\"retCode\":10006") != std::string::npos; };
    // Bybit answers HTTP 403 for its roughly ten-minute IP rate ban: a restart after the ban recovers.
    policy.rejected = [](long status, const std::string&) {
        if (status == 403) throw Error(20, "Bybit IP rate ban (HTTP 403); restart after the venue's ban window");
    };
    // Server timeout and server error: the venue asks to retry.
    policy.transient = [](long, const std::string& body) {
        for (const char* code : {"10000", "10016"})
            for (const char* end : {",", "}"})
                if (body.find(std::string("\"retCode\":") + code + end) != std::string::npos) return true;
        return false;
    };
    return policy;
}
std::string category(const Config& config) { return config.market == "linear" ? "linear" : "spot"; }
}

Kline bybit_kline(const Json& row) {
    if (row.at("interval").text() != "1") throw Error(23, "only one-minute Bybit klines are supported");
    Kline kline;
    kline.bar = {milliseconds(row.at("start")), row.at("open").text(), row.at("high").text(), row.at("low").text(),
                 row.at("close").text(), row.at("volume").text()};
    kline.bar.validate();
    if (milliseconds(row.at("end")) != kline.bar.ts + 59999) throw Error(23, "invalid inclusive one-minute Bybit kline end");
    const auto& confirm = row.at("confirm");
    if (confirm.kind != Json::Kind::Bool) throw Error(23, "confirmation must be a JSON boolean");
    kline.confirmed = confirm.value == "true";
    return kline;
}
std::vector<Kline> bybit_rows(const Json& list, std::int64_t start, std::size_t count) {
    if (list.kind != Json::Kind::Array || list.items.size() > 1000) throw Error(23, "invalid Bybit kline list");
    std::vector<Kline> result;
    auto expected = start;
    for (auto item = list.items.rbegin(); item != list.items.rend(); ++item) {
        if (item->kind != Json::Kind::Array || item->items.size() != 7) throw Error(23, "Bybit REST kline must have seven fields");
        const auto& row = item->items;
        Kline kline;
        kline.bar = {milliseconds(row[0]), row[1].text(), row[2].text(), row[3].text(), row[4].text(), row[5].text()};
        kline.bar.validate();
        if (kline.bar.ts != expected) throw Error(20, "historical klines have a missing, unordered or unexpected minute");
        expected += 60000;
        result.push_back(kline);
    }
    if (result.size() != count) throw Error(20, "historical klines have a missing minute");
    return result;
}
Frame bybit_frame(const std::string& message) {
    try {
        const auto envelope = parse_json(message);
        const auto* operation = envelope.find("op");
        if (!operation) return Frame::Data;
        if (operation->text() == "subscribe") {
            const auto* success = envelope.find("success");
            if (!success || success->kind != Json::Kind::Bool || success->value != "true") throw Error(23, "Bybit rejected the subscription");
        }
        return Frame::Control;
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid Bybit WebSocket JSON"); }
}

Bybit::Bybit(const Config& config) : config_(config), http_(config, bybit_rest()) {
    const auto instruments = result("/v5/market/instruments-info?category=" + category(config_) + "&symbol=" + config_.symbol);
    try {
        const auto& list = instruments.at("list");
        if (list.kind != Json::Kind::Array || list.items.size() != 1) throw Error(23, "unknown Bybit symbol");
        const auto& row = list.items.front();
        if (row.at("symbol").text() != config_.symbol) throw Error(23, "Bybit instrument metadata does not match the symbol");
        if (row.at("status").text() != "Trading") throw Error(23, "Bybit instrument is not trading");
        if (config_.market == "linear") {
            const auto type = row.at("contractType").text();
            if (type != "LinearPerpetual" && type != "LinearFutures") throw Error(23, "Bybit instrument is not a linear contract");
        }
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid Bybit instrument metadata"); }
}
Json Bybit::result(const std::string& path) {
    const auto response = http_.get(path, 1);
    try {
        const auto& code = response.at("retCode");
        if (code.kind != Json::Kind::Number || code.value != "0") throw Error(23, "Bybit rejected the public request (retCode " + code.value + ")");
        return response.at("result");
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid Bybit REST envelope"); }
}
Connection Bybit::connection() const {
    return {"/v5/public/" + category(config_), {"{\"op\":\"subscribe\",\"args\":[\"kline.1." + config_.symbol + "\"]}"},
            "{\"op\":\"ping\"}", &bybit_frame};
}
std::vector<VenueEvent> Bybit::decode(const std::string& message) const {
    try {
        const auto envelope = parse_json(message);
        if (envelope.at("topic").text() != "kline.1." + config_.symbol) throw Error(23, "unsupported Bybit topic");
        const auto& data = envelope.at("data");
        if (data.kind != Json::Kind::Array || data.items.size() > 1024) throw Error(23, "invalid Bybit data array");
        // A push can carry the minute that just closed (confirm=true) and the next forming one.
        std::vector<VenueEvent> events;
        for (const auto& row : data.items) events.push_back({VenueEvent::Kind::Kline, {}, bybit_kline(row)});
        return events;
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid Bybit message shape"); }
}
std::vector<Kline> Bybit::klines(std::int64_t start, std::int64_t end) {
    if (start < 0 || start % 60000 || end <= start || end % 60000) throw Error(23, "invalid exclusive kline range");
    // `start` and `end` are inclusive candle starts and rows arrive newest first: a wider range than the
    // limit would silently drop its oldest rows, so each request asks for exactly the rows it can hold.
    const auto count = static_cast<std::size_t>(std::min<std::int64_t>(1000, (end - start) / 60000));
    const auto list = result("/v5/market/kline?category=" + category(config_) + "&symbol=" + config_.symbol + "&interval=1&start=" +
        std::to_string(start) + "&end=" + std::to_string(start + static_cast<std::int64_t>(count - 1) * 60000) + "&limit=" + std::to_string(count));
    try { return bybit_rows(list.at("list"), start, count); }
    catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid Bybit kline result"); }
}
}
