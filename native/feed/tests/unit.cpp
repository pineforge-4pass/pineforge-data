// SPDX-License-Identifier: Apache-2.0
#include "binance.hpp"
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
    std::string streams() const override { return "test@trade/test@kline_1m"; }
    VenueEvent decode(const std::string&) const override { throw Error(23, "not used by deterministic unit venue"); }
    std::vector<Trade> history(std::uint64_t from, std::size_t limit) override {
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
    std::vector<Kline> klines(std::int64_t start, std::int64_t end) override {
        std::vector<Kline> result;
        for (auto minute = start; minute < end; minute += 60000) {
            if (!bars.count(minute)) throw Error(20, "synthetic missing minute");
            result.push_back(bars.at(minute));
        }
        return result;
    }
    VenueEvent tick(std::uint64_t id) const { return {VenueEvent::Kind::Trade, trades.at(id), {}}; }
    VenueEvent close(std::int64_t minute) const { return {VenueEvent::Kind::Kline, {}, bars.at(minute)}; }
};
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
            FeedSession session(state, venue, [](const auto&) {});
            std::filesystem::create_directory(temporary.path + "/cursor.json.tmp");
            expect(22, [&] { session.ingest(venue.close(120000)); });
            assert(state.cursor().message_index == 0);
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
    assert(timestamp("1970-01-01T00:02:00Z") == 120000);
    expect(23, [] { timestamp("2026-02-30T00:00:00Z"); });
    passed("strict_utc_start_and_exclusive_cut");
    std::cout << "PASS feed unit suite\n";
}
