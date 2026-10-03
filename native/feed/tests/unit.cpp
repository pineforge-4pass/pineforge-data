// SPDX-License-Identifier: Apache-2.0
#include "binance.hpp"
#include "bybit.hpp"
#include "okx.hpp"
#include "session.hpp"
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <thread>
#include <unistd.h>

using namespace pineforge::feed;
namespace {
struct Temporary {
    std::string path;
    Temporary() {
        char pattern[] = "/tmp/pineforge-feed-test-XXXXXX";
        const auto* result = ::mkdtemp(pattern);
        assert(result);
        path = result;
    }
    ~Temporary() { std::filesystem::remove_all(path); }
};
template<class Function> void expect(int code, Function operation) {
    bool caught = false;
    try { operation(); }
    catch (const Error& failure) { assert(failure.code == code); caught = true; }
    assert(caught);
}
void passed(const char* name) { std::cout << "PASS " << name << '\n'; }
Config config(const Temporary& temporary, const std::string& mode) {
    Config value;
    value.symbol = "TESTUSDT";
    value.mode = mode;
    value.start = 120000;
    value.state_dir = temporary.path;
    value.allow_insecure = true;  // loopback pacing: REST re-read pauses are 100 ms, not 1 s
    return value;
}
struct SyntheticVenue final : Venue {
    std::map<std::uint64_t, Trade> trades{
        {99, {99, 119999, "10.10000000", "0.10000000"}},
        {100, {100, 120001, "10.10000000", "0.10000000"}},
        {101, {101, 120050, "11.20000000", "0.20000000"}},
        {102, {102, 179999, "9.90000000", "0.30000000"}},
        {103, {103, 180001, "12.00000000", "0.40000000"}},
        {104, {104, 180050, "11.00000000", "0.10000000"}},
        {105, {105, 240001, "12.00000000", "0.10000000"}}
    };
    std::map<std::int64_t, Kline> bars{
        {120000, {{120000, "10.10000000", "11.20000000", "9.90000000", "9.90000000", "0.60000000"}, 100, 102, 3, true}},
        {180000, {{180000, "12.00000000", "12.00000000", "11.00000000", "11.00000000", "0.50000000"}, 103, 104, 2, true}},
        {240000, {{240000, "12.00000000", "12.00000000", "12.00000000", "12.00000000", "0.10000000"}, 105, 105, 1, true}}
    };
    Connection connection() const override { return {"/stream?streams=test@trade/test@kline_1m", {}, {}, &binance_frame}; }
    std::vector<VenueEvent> decode(const std::string&) const override { throw Error(23, "not used by deterministic unit venue"); }
    std::string kline_source() const override { return "/synthetic"; }
    std::size_t pages = 0;
    std::vector<Trade> history(std::uint64_t from, std::size_t limit) override {
        ++pages;
        std::vector<Trade> result;
        for (std::size_t offset = 0; offset < limit; ++offset) {
            const auto found = trades.find(from + offset);
            if (found == trades.end()) break;
            result.push_back(found->second);
        }
        if (result.empty()) throw Error(20, "synthetic exhausted history");
        return result;
    }
    std::uint64_t first_trade_id(std::int64_t minute) override { return static_cast<std::uint64_t>(bars.at(minute).first); }
    // REST rows carry no x=true and no f..L, exactly like the venue's klines endpoint.
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override {
        std::vector<Kline> result;
        for (auto minute = start; minute < end; minute += 60000) {
            if (!bars.count(minute)) throw Error(20, "synthetic missing minute");
            auto row = bars.at(minute);
            row.first = row.last = -1;
            row.confirmed = false;
            result.push_back(row);
        }
        return result;
    }
    VenueEvent tick(std::uint64_t id) const { return {VenueEvent::Kind::Trade, trades.at(id), {}}; }
    VenueEvent close(std::int64_t minute) const { return {VenueEvent::Kind::Kline, {}, bars.at(minute)}; }
};
// A next-print-fence venue (OKX trades-all, USD-M aggregates): candles carry no ID range and no count.
struct FenceVenue final : Venue {
    std::map<std::uint64_t, Trade> trades{
        {99, {99, 119999, "10.10000000", "0.10000000"}},
        {100, {100, 120001, "10.10000000", "0.10000000"}},
        {101, {101, 120050, "11.20000000", "0.20000000"}},
        {102, {102, 179999, "9.90000000", "0.30000000"}},
        {103, {103, 180001, "12.00000000", "0.40000000"}},
        {104, {104, 180050, "11.00000000", "0.10000000"}},
        {105, {105, 240001, "12.00000000", "0.10000000"}},
        {106, {106, 360001, "12.50000000", "0.10000000"}}
    };
    std::map<std::int64_t, Kline> candles{
        {120000, {{120000, "10.10000000", "11.20000000", "9.90000000", "9.90000000", "0.60000000"}, -1, -1, 0, true}},
        {180000, {{180000, "12.00000000", "12.00000000", "11.00000000", "11.00000000", "0.50000000"}, -1, -1, 0, true}},
        {240000, {{240000, "12.00000000", "12.00000000", "12.00000000", "12.00000000", "0.10000000"}, -1, -1, 0, true}},
        {300000, {{300000, "12.00000000", "12.00000000", "12.00000000", "12.00000000", "0.00000000"}, -1, -1, 0, true}},
        {360000, {{360000, "12.50000000", "12.50000000", "12.50000000", "12.50000000", "0.10000000"}, -1, -1, 0, true}}
    };
    std::int64_t retention = 0;
    bool print_sum = true;
    std::size_t pages = 0, candle_requests = 0;
    Connection connection() const override { return {"/synthetic", {}, {}, &binance_frame}; }
    std::vector<VenueEvent> decode(const std::string&) const override { throw Error(23, "not used by deterministic unit venue"); }
    std::vector<Trade> history(std::uint64_t from, std::size_t limit) override {
        ++pages;
        std::vector<Trade> result;
        for (auto found = trades.find(from); found != trades.end() && result.size() < limit && found->first == from + result.size(); ++found)
            result.push_back(found->second);
        return result;
    }
    Trade predecessor(std::int64_t minute) override {
        std::optional<Trade> last;
        for (const auto& [id, trade] : trades) if (trade.ts < minute) last = trade;
        if (!last) throw Error(20, "synthetic venue has no predecessor");
        return *last;
    }
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override {
        ++candle_requests;
        std::vector<Kline> result;
        for (auto minute = start; minute < end; minute += 60000) {
            if (!candles.count(minute)) throw Error(20, "synthetic missing minute");
            result.push_back(candles.at(minute));
        }
        return result;
    }
    TickProof tick_proof() const override { return TickProof::NextPrintFence; }
    std::int64_t retention_ms() const override { return retention; }
    bool candle_is_print_sum() const override { return print_sum; }
    std::string kline_source() const override { return "/synthetic"; }
    VenueEvent tick(std::uint64_t id) const { return {VenueEvent::Kind::Trade, trades.at(id), {}}; }
    VenueEvent close(std::int64_t minute) const { return {VenueEvent::Kind::Kline, {}, candles.at(minute)}; }
};
std::vector<std::string> kinds(const std::vector<std::string>& lines) {
    std::vector<std::string> result;
    for (const auto& line : lines) {
        const auto event = parse_json(line);
        result.push_back(event.at("type").text() == "tick" ? event.at("seq").value : "time:" + event.at("ts").value);
    }
    return result;
}
}

int main() {
    assert(Decimal("0.10000000").add(Decimal("0.20000000")) == Decimal("0.3"));
    assert(Decimal("0").compare(Decimal("0.00000001")) < 0);
    assert(Decimal("9007199254740993.00000001").compare(Decimal("9007199254740993.00000000")) > 0);
    assert(Decimal("9999999999999999.99").add(Decimal("0.01")) == Decimal("10000000000000000.00"));
    for (const auto* invalid : {"", "-1", "+1", "01", "1.", ".1", "NaN", "1e3", "1.2.3"}) expect(23, [&] { Decimal value(invalid); (void)value; });
    expect(23, [] { Decimal value("0." + std::string(33, '1')); (void)value; });
    Trade exact{9007199254740993ULL, 120001, "123.45000000", "0.00000001"};
    assert(normalized_trade(parse_json(exact.wire())) == exact);
    expect(23, [] { Trade{1, 1, "1", "0"}.validate(); });
    // Equality is by exact value: one venue number rendered two ways is not a revision; any change is.
    assert((Bar{120000, "84850.0", "84850.1", "84826.5", "84826.5", "18.30"} == Bar{120000, "84850", "84850.1", "84826.5", "84826.5", "18.3"}));
    assert(!(Bar{120000, "84850.0", "84850.1", "84826.5", "84826.5", "18.30"} == Bar{120000, "84850.01", "84850.1", "84826.5", "84826.5", "18.3"}));
    assert((Trade{7, 120001, "10.10", "0.5"} == Trade{7, 120001, "10.1", "0.50"}) && !(Trade{7, 120001, "10.1", "0.5"} == Trade{7, 120001, "10.1", "0.51"}));
    passed("decimal_tokens_and_uint64_identity");

    const auto raw = parse_json(R"({"e":"trade","E":999999,"s":"TESTUSDT","t":100,"p":"10.10000000","q":"0.10000000","T":120001,"m":false})");
    assert(binance_trade(raw, true).ts == 120001);
    const auto source = parse_json(R"({"t":120000,"T":179999,"s":"TESTUSDT","i":"1m","f":100,"L":102,"o":"10.10000000","h":"11.20000000","l":"9.90000000","c":"9.90000000","v":"0.60000000","n":3,"x":false})");
    assert(!binance_kline(source, true).confirmed);
    auto bad_source = source;
    bad_source.members["T"] = Json::number("180000");
    expect(23, [&] { binance_kline(bad_source, true); });
    bad_source = source;
    bad_source.members["x"] = Json::string("true");
    expect(23, [&] { binance_kline(bad_source, true); });
    passed("binance_matched_time_confirmation_and_inclusive_close");

    {
        Temporary temporary;
        SyntheticVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.connected();
        session.ingest(venue.tick(102));
        assert(output.size() == 3);
        session.ingest(venue.tick(100));
        assert(output.size() == 3);
        session.ingest(venue.tick(103));
        assert(output.size() == 3);
        auto unconfirmed = venue.close(120000);
        unconfirmed.kline.confirmed = false;
        session.ingest(unconfirmed);
        assert(output.size() == 3);
        session.ingest(venue.close(120000));
        assert(output.size() == 5);
        assert(parse_json(output[3]).at("type").text() == "time");
        assert(parse_json(output[4]).at("seq").integer<std::uint64_t>() == 103);
        session.ingest(venue.close(120000));
        auto conflict = venue.tick(100);
        conflict.trade.qty = "0.20000000";
        expect(21, [&] { session.ingest(conflict); });
        passed("raw_gap_healing_reordering_identical_and_conflicting_duplicates");
        passed("time_requires_full_ids_count_exact_ohlcv_and_precedes_next_minute");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        State state(config(temporary, "ticks"));
        FeedSession session(state, venue, [](const auto&) {});
        auto revised = venue.close(120000);
        revised.kline.bar.volume = "0.61000000";
        expect(21, [&] { session.ingest(revised); });
        assert(state.cursor().seq == 102 && state.cursor().cut == 120000);
        passed("exact_ohlcv_mismatch_stops_without_time");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        venue.trades.erase(101);
        State state(config(temporary, "ticks"));
        FeedSession session(state, venue, [](const auto&) {});
        expect(20, [&] { session.ingest(venue.tick(102)); });
        assert(state.cursor().seq == 100);
        passed("unhealable_raw_hole_retains_verified_cursor");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        State state(config(temporary, "bars"));
        std::vector<std::string> output;
        FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        auto forming = venue.close(240000);
        forming.kline.confirmed = false;
        session.ingest(forming);
        assert(output.empty());
        session.ingest(venue.close(240000));
        assert(output.size() == 3);
        assert(normalized_bar(parse_json(output.front())).ts == 120000);
        session.ingest(venue.close(180000));
        assert(output.size() == 3);
        auto revised = venue.close(180000);
        revised.kline.bar.volume = "0.60000000";
        expect(21, [&] { session.ingest(revised); });
        venue.bars.at(240000).bar.volume = "0.20000000";
        expect(21, [&] { session.connected(); });
        passed("confirmed_bars_watermark_healing_duplicate_and_revised_overlap");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        auto options = config(temporary, "ticks");
        std::string epoch;
        {
            State state(options);
            FeedSession session(state, venue, [](const auto&) {});
            session.ingest(venue.tick(100));
            session.ingest(venue.tick(101));
            epoch = state.cursor().epoch;
            auto second = options;
            second.resume = true;
            expect(22, [&] { State locked(second); });
        }
        { std::ofstream tail(temporary.path + "/events.jsonl", std::ios::app); tail << "uncommitted-torn-tail"; }
        options.resume = true;
        options.start = -1;
        options.output_from = 2;
        {
            State state(options);
            assert(state.cursor().epoch == epoch);
            assert(std::filesystem::file_size(temporary.path + "/events.jsonl") == state.cursor().log_bytes);
            std::vector<std::string> output;
            FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
            session.connected();
            assert(output.empty());
            session.ingest(venue.close(120000));
            assert(output.size() == 2);
            assert(parse_json(output.front()).at("seq").integer<std::uint64_t>() == 102);
            assert(parse_json(output.back()).at("type").text() == "time");
        }
        {
            std::fstream corrupt(temporary.path + "/events.jsonl", std::ios::in | std::ios::out);
            corrupt.seekp(0);
            corrupt << 'X';
        }
        expect(21, [&] { State changed(options); });
        passed("atomic_cursor_lock_torn_tail_verified_restart_and_changed_prefix");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.ingest(venue.close(180000));
        assert(state.cursor().cut == 240000 && state.cursor().seq == 104);
        assert(output.size() == 7);
        assert(parse_json(output[3]).at("type").text() == "time");
        passed("historical_ticks_require_closed_watermark_next_id_fence_and_rest_reconciliation");
    }
    {
        Temporary temporary;
        auto options = config(temporary, "bars");
        options.max_log_bytes = 4;
        State state(options);
        SyntheticVenue venue;
        FeedSession session(state, venue, [](const auto&) {});
        expect(22, [&] { session.ingest(venue.close(120000)); });
        assert(state.cursor().message_index == 0);
        passed("bounded_replay_budget_stops_before_commit");
    }
    {
        Temporary temporary;
        State state(config(temporary, "ticks"));
        SyntheticVenue venue;
        FeedSession session(state, venue, [](const auto&) {});
        session.ingest(venue.close(120000));
        auto revised = venue.close(120000);
        ++revised.kline.first;
        ++revised.kline.last;
        expect(21, [&] { session.ingest(revised); });
        ++venue.bars.at(120000).count;
        expect(21, [&] { session.connected(); });
        assert(state.cursor().message_index == 4);
        passed("closed_tick_fences_and_rest_counts_are_immutable");
    }
    {
        Temporary temporary;
        auto options = config(temporary, "bars");
        {
            State state(options);
            SyntheticVenue venue;
            std::vector<std::string> output;
            FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
            std::filesystem::create_directory(temporary.path + "/cursor.json.tmp");
            expect(22, [&] { session.ingest(venue.close(120000)); });
            assert(output.empty());
            assert(state.durable().message_index == 0);
            expect(22, [&] { session.publish(); });
            assert(output.empty());
        }
        std::filesystem::remove(temporary.path + "/cursor.json.tmp");
        options.resume = true;
        State recovered(options);
        assert(recovered.cursor().message_index == 0);
        assert(std::filesystem::file_size(temporary.path + "/events.jsonl") == 0);
        passed("failed_cursor_write_rolls_back_uncommitted_message_on_resume");
    }
    {
        Temporary temporary;
        auto options = config(temporary, "bars");
        options.max_replay_seconds = 1;
        State state(options);
        SyntheticVenue venue;
        FeedSession session(state, venue, [](const auto&) {});
        session.ingest(venue.close(120000));
        session.ingest(venue.close(180000));
        expect(22, [&] { state.visit(0, [](const auto&) { std::this_thread::sleep_for(std::chrono::milliseconds(1100)); }); });
        passed("replay_time_budget_stops_without_mutating_cursor");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.connected();
        session.ingest(venue.tick(100));
        session.ingest(venue.tick(101));
        assert(output.size() == 2);
        venue.trades.at(100).qty = "0.10000001";
        expect(21, [&] { session.connected(); });
        assert(output.size() == 2 && state.durable().message_index == 2);
        passed("reconnect_raw_overlap_change_stops_without_output");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        venue.trades.erase(venue.trades.find(101), venue.trades.end());
        for (std::uint64_t id = 100; id < 2600; ++id)
            venue.trades[id] = {id, 120001 + static_cast<std::int64_t>(id - 100) * 20, "10.00000000", "0.00000001"};
        venue.trades[2600] = {2600, 180001, "10.00000000", "0.00000001"};
        venue.bars.at(120000) = {{120000, "10.00000000", "10.00000000", "10.00000000", "10.00000000", "0.00002500"}, 100, 2599, 2500, true};
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.ingest(venue.tick(2599));
        session.ingest(venue.close(120000));
        assert(output.size() == 2501 && venue.pages >= 4);
        for (std::size_t index = 0; index < 2500; ++index)
            assert(parse_json(output[index]).at("seq").integer<std::uint64_t>() == 100 + index);
        assert(parse_json(output.back()).at("type").text() == "time");
        passed("multi_page_raw_healing_stays_contiguous");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        venue.trades.at(101).ts = 120000;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.ingest(venue.tick(100));
        expect(23, [&] { session.ingest(venue.tick(101)); });
        assert(output.size() == 1 && state.durable().seq == 100);
        passed("matched_time_regression_stops_23");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        ++venue.bars.at(120000).count;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FeedSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        expect(20, [&] { session.ingest(venue.close(180000)); });
        assert(state.durable().cut == 120000);
        for (const auto& line : output) assert(parse_json(line).at("type").text() == "tick");
        venue.bars.at(120000).count = 3;
        venue.trades.erase(103);
        Temporary second;
        State fresh(config(second, "ticks"));
        FeedSession fenced(fresh, venue, [](const auto&) {});
        expect(20, [&] { fenced.ingest(venue.close(180000)); });
        assert(fresh.durable().cut == 120000);
        passed("historical_closure_needs_rest_count_and_next_minute_fence");
    }
    {
        Temporary temporary;
        State state(config(temporary, "bars"));
        const Bar bar{120000, "10.10000000", "11.20000000", "9.90000000", "9.90000000", "0.60000000"};
        state.stage(bar.wire(), bar);
        assert(state.staged() == 1 && state.cursor().message_index == 1 && state.durable().message_index == 0);
        assert(std::filesystem::file_size(temporary.path + "/events.jsonl") == 0);
        std::size_t seen = 0;
        state.visit(0, [&](const auto&) { ++seen; });
        assert(seen == 1 && state.bar(120000));
        const auto lines = state.flush();
        assert(lines.size() == 1 && state.staged() == 0 && state.durable().message_index == 1);
        assert(std::filesystem::file_size(temporary.path + "/events.jsonl") == lines.front().size() + 1);
        passed("group_commit_stages_then_persists_before_publication");
    }
    {
        Temporary temporary;
        { std::ofstream empty(temporary.path + "/events.jsonl"); }
        { State state(config(temporary, "bars")); assert(state.durable().message_index == 0); }
        Temporary second;
        { std::ofstream used(second.path + "/events.jsonl"); used << "x\n"; }
        expect(23, [&] { State state(config(second, "bars")); });
        passed("crashed_initialization_without_cursor_is_reinitialized");
    }

    {
        assert(Decimal("1.64").multiply(Decimal("0.01")).str() == "0.0164");
        assert(Decimal("2").multiply(Decimal("0.01")).str() == "0.02");
        assert(Decimal("100").multiply(Decimal("0.01")).str() == "1");
        assert(Decimal("0.5").multiply(Decimal("0")).str() == "0");
        assert(Decimal("12.30").str() == "12.3" && Decimal("0.00012").str() == "0.00012");
        assert(scaled_quantity("1.640", "1") == "1.640");
        assert(scaled_quantity("1.64", "0.01") == "0.0164");
        // Contract conversion distributes exactly over the candle volume: no float, no rounding.
        assert(Decimal(scaled_quantity("0.07", "0.01")).add(Decimal(scaled_quantity("186.80", "0.01"))) ==
               Decimal(scaled_quantity("186.87", "0.01")));
        expect(23, [] { Decimal("0." + std::string(20, '1')).multiply(Decimal("0." + std::string(20, '1'))); });
        passed("decimal_multiply_exact_contract_units");
    }
    {
        const std::string document = R"({"timezone":"UTC","note":"x]}\"","rateLimits":[{"limit":2400,"s":"[{"}],"symbols":[{"symbol":"A"}]})";
        assert(json_member(document, "rateLimits") == R"([{"limit":2400,"s":"[{"}])");
        assert(json_member(document, "note") == R"("x]}\"")");
        assert(json_member(document, "symbols") == R"([{"symbol":"A"}])");
        expect(23, [&] { json_member(document, "missing"); });
        expect(23, [] { json_member("[1]", "rateLimits"); });
        passed("json_member_extracts_one_member_beyond_the_parser_bound");
    }
    {
        auto row = parse_json(R"({"instId":"TEST-USDT-SWAP","tradeId":"100","px":"10.1","sz":"1.64","side":"buy","ts":"120001","source":"0"})");
        const auto swap = okx_trade(row, "TEST-USDT-SWAP", "0.01");
        assert(swap.id == 100 && swap.ts == 120001 && swap.price == "10.1" && swap.qty == "0.0164");
        assert(okx_trade(row, "TEST-USDT-SWAP", "1").qty == "1.64");
        expect(23, [&] { okx_trade(row, "OTHER-USDT-SWAP", "0.01"); });
        auto aggregated = row;
        aggregated.members["count"] = Json::string("3");
        expect(23, [&] { okx_trade(aggregated, "TEST-USDT-SWAP", "0.01"); });
        auto padded = row;
        padded.members["tradeId"] = Json::string("0100");
        expect(23, [&] { okx_trade(padded, "TEST-USDT-SWAP", "0.01"); });
        const auto candle = okx_candle(parse_json(R"(["120000","10.1","11.2","9.9","9.9","60","0.6","6.06","1"])"), "0.01");
        assert(candle.confirmed && candle.bar.volume == "0.6" && candle.bar.ts == 120000 && candle.count == 0 && candle.first == -1);
        assert(!okx_candle(parse_json(R"(["120000","10.1","11.2","9.9","9.9","60","0.6","6.06","0"])"), "0.01").confirmed);
        expect(23, [] { okx_candle(parse_json(R"(["120000","10.1","11.2","9.9","9.9","60","0.6","6.06","true"])"), "1"); });
        expect(23, [] { okx_candle(parse_json(R"(["120000","10.1","11.2","9.9","9.9","60","0.6","1"])"), "1"); });
        auto instrument = parse_json(R"({"instId":"TEST-USDT-SWAP","instType":"SWAP","state":"live","ctType":"linear","ctValCcy":"TEST","ctVal":"0.01","ctMult":"1"})");
        assert(okx_multiplier(instrument, "swap", "TEST-USDT-SWAP") == "0.01");
        auto inverse = instrument;
        inverse.members["ctType"] = Json::string("inverse");
        expect(23, [&] { okx_multiplier(inverse, "swap", "TEST-USDT-SWAP"); });
        auto quoted = instrument;
        quoted.members["ctValCcy"] = Json::string("USD");
        expect(23, [&] { okx_multiplier(quoted, "swap", "TEST-USDT-SWAP"); });
        auto suspended = instrument;
        suspended.members["state"] = Json::string("suspend");
        expect(23, [&] { okx_multiplier(suspended, "swap", "TEST-USDT-SWAP"); });
        assert(okx_multiplier(parse_json(R"({"instId":"TEST-USDT","instType":"SPOT","state":"live"})"), "spot", "TEST-USDT") == "1");
        assert(okx_frame("pong") == Frame::Control);
        assert(okx_frame(R"({"event":"subscribe","arg":{"channel":"candle1m","instId":"TEST-USDT"},"connId":"a"})") == Frame::Control);
        assert(okx_frame(R"({"event":"notice","code":"64008","msg":"upgrade","connId":"a"})") == Frame::Retire);
        assert(okx_frame(R"({"arg":{"channel":"candle1m","instId":"TEST-USDT"},"data":[]})") == Frame::Data);
        expect(23, [] { okx_frame(R"({"event":"error","code":"60012","msg":"bad"})"); });
        expect(23, [] { okx_frame("ping-not-json"); });
        passed("okx_trades_all_candle_confirm_count_contract_units_and_frames");
    }
    {
        const auto closing = bybit_kline(parse_json(R"({"start":120000,"end":179999,"interval":"1","open":"10.1","close":"9.9","high":"11.2","low":"9.9","volume":"0.6","turnover":"6","confirm":true,"timestamp":180001})"));
        assert(closing.confirmed && closing.bar.ts == 120000 && closing.bar.volume == "0.6");
        assert(!bybit_kline(parse_json(R"({"start":180000,"end":239999,"interval":"1","open":"9.9","close":"9.9","high":"9.9","low":"9.9","volume":"0.1","turnover":"1","confirm":false,"timestamp":180001})")).confirmed);
        expect(23, [] { bybit_kline(parse_json(R"({"start":120000,"end":179999,"interval":"1","open":"1","close":"1","high":"1","low":"1","volume":"1","turnover":"1","confirm":"true","timestamp":1})")); });
        expect(23, [] { bybit_kline(parse_json(R"({"start":120000,"end":239999,"interval":"1","open":"1","close":"1","high":"1","low":"1","volume":"1","turnover":"1","confirm":true,"timestamp":1})")); });
        const auto newest_first = parse_json(R"([["180000","12","12","11","11","0.5","6"],["120000","10.1","11.2","9.9","9.9","0.6","6"]])");
        const auto ascending = bybit_rows(newest_first, 120000, 2);
        assert(ascending.size() == 2 && ascending.front().bar.ts == 120000 && ascending.back().bar.ts == 180000);
        expect(20, [] { bybit_rows(parse_json(R"([["120000","10.1","11.2","9.9","9.9","0.6","6"],["180000","12","12","11","11","0.5","6"]])"), 120000, 2); });
        expect(20, [&] { bybit_rows(newest_first, 120000, 3); });
        assert(bybit_frame(R"({"success":true,"ret_msg":"pong","conn_id":"a","op":"ping"})") == Frame::Control);
        assert(bybit_frame(R"({"success":true,"ret_msg":"","conn_id":"a","op":"subscribe"})") == Frame::Control);
        assert(bybit_frame(R"({"topic":"kline.1.TESTUSDT","data":[],"ts":1,"type":"snapshot"})") == Frame::Data);
        expect(23, [] { bybit_frame(R"({"success":false,"ret_msg":"bad","op":"subscribe"})"); });
        passed("bybit_confirm_flag_two_entry_push_and_reverse_order_rest_rows");
    }
    {
        Config options;
        options.symbol = "TESTUSDT";
        assert(usdm_connection(options).path == "/market/stream?streams=testusdt@kline_1m");
        options.mode = "agg-ticks";
        assert(usdm_connection(options).path == "/market/stream?streams=testusdt@aggTrade/testusdt@kline_1m");
        const auto aggregate = usdm_aggregate(parse_json(R"({"e":"aggTrade","E":1,"s":"TESTUSDT","a":500,"p":"10.1","q":"0.5","nq":"0.4","f":1000,"l":1004,"T":120001,"m":true,"st":1})"));
        assert(aggregate.id == 500 && aggregate.ts == 120001 && aggregate.qty == "0.5");
        expect(23, [] { usdm_aggregate(parse_json(R"({"a":500,"p":"10.1","q":"0.5","f":1004,"l":1000,"T":120001})")); });
        assert(usdm_kline_weight(32) == 1 && usdm_kline_weight(100) == 2 && usdm_kline_weight(1000) == 5);
        assert(binance_frame(R"({"stream":"!x","data":{"e":"serverShutdown"}})") == Frame::Retire);
        passed("usdm_routed_stream_aggregate_q_and_kline_weights");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.connected();
        for (const std::uint64_t id : {100, 101, 102}) session.ingest(venue.tick(id));
        // The fence alone does not close the minute: its confirmed candle must reconcile first.
        session.ingest(venue.tick(103));
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102"}));
        session.ingest(venue.close(120000));
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102", "time:180000", "103"}));
        // The confirmed candle alone does not close the minute either: it waits for the next print.
        session.ingest(venue.close(180000));
        assert(output.size() == 5);
        session.ingest(venue.tick(104));
        assert(output.size() == 6);
        session.ingest(venue.tick(105));
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102", "time:180000", "103", "104", "time:240000", "105"}));
        session.ingest(venue.close(180000));
        auto revised = venue.close(180000);
        revised.kline.bar.volume = "0.60000000";
        expect(21, [&] { session.ingest(revised); });
        passed("fence_time_needs_contiguous_ids_next_minute_print_and_exact_candle");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        for (const std::uint64_t id : {100, 101, 102, 103}) session.ingest(venue.tick(id));
        auto revised = venue.close(120000);
        revised.kline.bar.high = "11.30000000";
        expect(21, [&] { session.ingest(revised); });
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102"}) && state.durable().cut == 120000);
        passed("fence_exact_ohlcv_mismatch_stops_21_without_time");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.ingest(venue.tick(103));
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102"}) && venue.pages == 1);
        session.ingest(venue.close(120000));
        session.ingest(venue.close(180000));
        session.ingest(venue.close(240000));
        session.ingest(venue.close(300000));
        session.ingest(venue.tick(106));
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102", "time:180000", "103", "104", "time:240000", "105",
                                                          "time:300000", "time:360000", "106"}));
        passed("fence_gap_heals_by_id_pages_and_quiet_minute_closes_on_zero_volume_candle");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        venue.candles.at(300000).bar.volume = "0.10000000";
        State state(config(temporary, "ticks"));
        FenceSession session(state, venue, [](const auto&) {});
        for (const std::int64_t minute : {120000, 180000, 240000}) session.ingest(venue.close(minute));
        expect(21, [&] { session.ingest(venue.close(300000)); });
        assert(state.durable().cut == 300000);
        passed("fence_quiet_minute_with_candle_volume_stops_21");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        for (const std::uint64_t id : {100, 101, 102, 103}) session.ingest(venue.tick(id));
        assert(output.size() == 3 && venue.candle_requests == 0);
        session.ingest(venue.close(180000));
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102", "time:180000", "103"}) && venue.candle_requests == 1);
        passed("fence_rest_candle_only_behind_a_later_confirmed_websocket_candle");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        venue.retention = 60000;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.ingest(venue.tick(100));
        const auto before = venue.pages;
        expect(20, [&] { session.ingest(venue.tick(105)); });
        assert(venue.pages == before && kinds(output) == std::vector<std::string>({"100"}));
        venue.retention = 0;
        Temporary second;
        State healing(config(second, "ticks"));
        FenceSession healed(healing, venue, [](const auto&) {});
        healed.ingest(venue.tick(100));
        healed.ingest(venue.tick(102));
        assert(healing.durable().seq == 102);
        passed("fence_gap_beyond_the_venue_history_window_stops_20_without_rest");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        State state(config(temporary, "ticks"));
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.ingest(venue.tick(100));
        session.ingest(venue.tick(101));
        venue.trades.at(100).qty = "0.10000001";
        expect(21, [&] { session.connected(); });
        assert(output.size() == 2 && state.durable().message_index == 2);
        venue.trades.at(100).qty = "0.10000000";
        venue.trades.at(102).ts = 120040;
        expect(23, [&] { session.ingest(venue.tick(102)); });
        passed("fence_reconnect_overlap_change_21_and_time_regression_23");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        venue.trades = {{99, {99, 119999, "10.10000000", "0.10000000"}}, {100, {100, 180001, "12.00000000", "0.50000000"}}};
        venue.candles.at(120000).bar = {120000, "10.10000000", "10.10000000", "10.10000000", "10.10000000", "0"};
        auto options = config(temporary, "ticks");
        {
            State state(options);
            std::vector<std::string> output;
            FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
            session.ingest(venue.close(120000));
            assert(output.empty());
            session.ingest(venue.tick(100));
            assert(kinds(output) == std::vector<std::string>({"time:180000", "100"}));
        }
        options.resume = true;
        options.start = -1;
        State resumed(options);
        assert(resumed.cursor().message_index == 2 && resumed.cursor().seq == 100 && resumed.cursor().cut == 180000);
        passed("fence_quiet_first_minute_closes_on_the_anchored_predecessor_and_resumes");
    }
    {
        // A resumed fence session rebuilds the open minute from the journal before it reconciles.
        Temporary temporary;
        FenceVenue venue;
        auto options = config(temporary, "ticks");
        {
            State state(options);
            FenceSession session(state, venue, [](const auto&) {});
            session.ingest(venue.tick(100));
            session.ingest(venue.tick(101));
        }
        options.resume = true;
        options.start = -1;
        options.output_from = 2;
        State state(options);
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        session.connected();
        session.ingest(venue.tick(102));
        session.ingest(venue.tick(103));
        session.ingest(venue.close(120000));
        assert(kinds(output) == std::vector<std::string>({"102", "time:180000", "103"}));
        passed("fence_resume_rebuilds_the_open_minute_from_the_journal");
    }
    {
        Temporary temporary;
        auto options = config(temporary, "ticks");
        options.qty_multiplier = "0.01";
        { State state(options); }
        options.resume = true;
        options.start = -1;
        options.qty_multiplier = "1";
        expect(21, [&] { State changed(options); });
        options.qty_multiplier = "0.01";
        State same(options);
        assert(same.cursor().message_index == 0);
        passed("resume_refuses_a_changed_contract_multiplier");
    }
    {
        Temporary temporary;
        FenceVenue venue;
        State state(config(temporary, "ticks"));
        FenceSession session(state, venue, [](const auto&) {});
        session.ingest(venue.tick(100));
        const Trade earlier{98, 120002, "10.10000000", "0.10000000"}, changed{99, 120003, "10.10000000", "0.10000000"};
        expect(23, [&] { session.ingest({VenueEvent::Kind::Trade, earlier, {}}); });
        expect(21, [&] { session.ingest({VenueEvent::Kind::Trade, changed, {}}); });
        assert(state.durable().seq == 100);
        passed("fence_print_at_or_before_the_start_predecessor_stops");
    }
    {
        // Aggregate prints: an aggregate dated in one minute can hold a fill the next minute's candle
        // counts, so the candle is not their sum and the fence alone closes the minute.
        Temporary temporary;
        FenceVenue venue;
        venue.print_sum = false;
        venue.candles.at(120000).bar.volume = "0.60300000";
        venue.candles.at(180000).bar = {180000, "12.00000000", "12.00000000", "10.90000000", "11.00000000", "0.49700000"};
        State state(config(temporary, "agg-ticks"));
        std::vector<std::string> output;
        FenceSession session(state, venue, [&](const auto& line) { output.push_back(line); });
        for (const std::uint64_t id : {100, 101, 102, 103, 104}) session.ingest(venue.tick(id));
        session.ingest(venue.close(120000));
        session.ingest(venue.close(180000));
        session.ingest(venue.tick(105));
        assert(kinds(output) == std::vector<std::string>({"100", "101", "102", "time:180000", "103", "104", "time:240000", "105"}));
        venue.print_sum = true;
        Temporary second;
        State strict(config(second, "ticks"));
        FenceSession raw(strict, venue, [](const auto&) {});
        for (const std::uint64_t id : {100, 101, 102, 103}) raw.ingest(venue.tick(id));
        expect(21, [&] { raw.ingest(venue.close(120000)); });
        passed("aggregate_prints_close_on_the_fence_without_candle_equality");
    }
    {
        Temporary temporary;
        SyntheticVenue venue;
        State state(config(temporary, "agg-ticks"));
        expect(23, [&] { make_session(state, venue, [](const auto&) {}); });
        passed("aggregate_mode_needs_a_next_print_fence_venue");
    }
    assert(retry_after_seconds("120", 0) == 120);
    assert(retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", 1445412475) == 5);
    assert(retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", 1445412490) == 0);
    for (const auto* invalid : {"", "soon", "-1", "1.5", "Wed, 21 Oct 2015 07:28:00 UTC", "21 Oct 2015 07:28:00 GMT"})
        expect(23, [&] { retry_after_seconds(invalid, 0); });
    passed("retry_after_delta_seconds_and_http_date");
    assert(timestamp("1970-01-01T00:02:00Z") == 120000);
    expect(23, [] { timestamp("2026-02-30T00:00:00Z"); });
    passed("strict_utc_start_and_exclusive_cut");
    std::cout << "PASS feed unit suite\n";
}
