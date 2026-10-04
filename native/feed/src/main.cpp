// SPDX-License-Identifier: Apache-2.0
#include "export.hpp"
#include "session.hpp"
#include "transport.hpp"
#if PINEFORGE_FEED_SERVE
#include "serve.hpp"
#endif
#include <algorithm>
#include <charconv>
#include <chrono>
#include <csignal>
#include <fcntl.h>
#include <iostream>
#include <set>
#include <unistd.h>

namespace {
void on_signal(int) { pineforge::feed::stopping.store(true); }
std::uint64_t unsigned_value(const std::string& token) {
    std::uint64_t value = 0;
    const auto parsed = std::from_chars(token.data(), token.data() + token.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != token.data() + token.size()) throw pineforge::feed::Error(23, "invalid unsigned CLI value");
    return value;
}
// stdout's open file description is shared with whoever handed it to us: its flags are restored on exit.
struct StdoutFlags {
    int saved = -1;
    ~StdoutFlags() { if (saved >= 0) ::fcntl(STDOUT_FILENO, F_SETFL, saved); }
};
void help() {
    std::cout << "pineforge-feed warmup --venue VENUE --market MARKET --symbol SYMBOL --start UTC|MS --end UTC|MS --output FILE\n"
                 "pineforge-feed export --venue VENUE --market MARKET --symbol SYMBOL --mode ticks|agg-ticks --start UTC|MS --end UTC|MS --output FILE\n"
                 "                      [--warmup RUNNER-WARMUP.csv] [--qty-step RUNNER-QTY-STEP] [--archive DAILY.zip|DAILY.csv [--checksum DAILY.zip.CHECKSUM]]\n"
                 "                      (prints-built 1m bars; volume by the runner's rule for its qty_step, none by default)\n"
                 "pineforge-feed run --venue VENUE --market MARKET --symbol SYMBOL --mode MODE --state-dir DIR [--start UTC|MS | --resume] [--output-from INDEX]\n"
                 "pineforge-feed serve --venue VENUE --market MARKET --symbol SYMBOL --mode MODE --state-dir DIR [--start UTC|MS | --resume]\n"
                 "                     [--listen HOST:PORT|[IPV6]:PORT (default 127.0.0.1:8787)] [--allow-remote-listen] [--client-queue-bytes N] [--max-clients N]\n"
                 "  GET /v1/status, GET /v1/snapshot (complete prefix from index 0, at most 4 MiB), ws://HOST:PORT/v1/stream?epoch=E&from=I\n"
                 "Venues and modes:\n"
                 "  binance spot BTCUSDT        bars | ticks\n"
                 "  binance usdm BTCUSDT        bars | agg-ticks (aggregate prints, not raw trades; bars can\n"
                 "                              differ from klines at minute edges: use bars for kline-exact bars)\n"
                 "  okx spot BTC-USDT           bars | ticks\n"
                 "  okx swap BTC-USDT-SWAP      bars | ticks (linear swaps; base quantities)\n"
                 "  bybit spot|linear BTCUSDT   bars\n"
                 "Journal: --segment-bytes N --replay-bytes N --replay-age-seconds N\n"
                 "Limits: --max-messages N --max-replay-seconds N --max-queue-bytes N --reconnect-seconds N --keepalive-seconds N --silence-seconds N\n"
                 "Testing/public origins: --rest-url ORIGIN --ws-url ORIGIN [--allow-insecure-http (loopback only)]\n";
}
// Venue, market and mode gates are checked before any network access.
void gate(pineforge::feed::Config& config, bool warmup, bool rest_given, bool ws_given) {
    using pineforge::feed::Error;
    const auto& venue = config.venue;
    const auto& market = config.market;
    const auto& mode = config.mode;
    std::string rest, ws, grammar = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    if (venue == "coinbase")
        throw Error(23, "Coinbase is deferred: it has no confirmed one-minute candle stream, and its REST and WebSocket trade "
                        "timestamps disagree, so no lossless tick paging exists");
    if (venue == "binance" && market == "spot") {
        rest = "https://api.binance.com", ws = "wss://stream.binance.com:443";
        if (!warmup && mode != "bars" && mode != "ticks") throw Error(23, "Binance spot modes are bars and ticks");
    } else if (venue == "binance" && market == "usdm") {
        rest = "https://fapi.binance.com", ws = "wss://fstream.binance.com";
        if (!warmup && mode == "ticks")
            throw Error(23, "Binance USD-M raw trades have no public REST history; use --mode agg-ticks for aggregate prints");
        if (!warmup && mode != "bars" && mode != "agg-ticks") throw Error(23, "Binance USD-M modes are bars and agg-ticks");
    } else if (venue == "okx" && (market == "spot" || market == "swap")) {
        rest = "https://www.okx.com", ws = "wss://ws.okx.com:8443";
        grammar += '-';
        if (!warmup && mode != "bars" && mode != "ticks") throw Error(23, "OKX modes are bars and ticks (trades-all raw prints)");
    } else if (venue == "bybit" && (market == "spot" || market == "linear")) {
        rest = "https://api.bybit.com", ws = "wss://stream.bybit.com";
        if (!warmup && (mode == "ticks" || mode == "agg-ticks"))
            throw Error(23, "Bybit tick mode is refused: Bybit trade IDs are not a contiguous cursor and its REST cannot page back "
                            "through trades; use --mode bars (confirmed kline.1 candles)");
        if (!warmup && mode != "bars") throw Error(23, "Bybit supports bars only");
    } else throw Error(23, "unsupported venue/market: binance spot|usdm, okx spot|swap, bybit spot|linear");
    if (config.symbol.empty() || config.symbol.size() > 32 || config.symbol.find_first_not_of(grammar) != std::string::npos)
        throw Error(23, venue == "okx" ? "use an uppercase OKX instrument ID such as BTC-USDT or BTC-USDT-SWAP" : "use an uppercase ASCII symbol");
    if (venue == "okx") {
        const auto dashes = std::count(config.symbol.begin(), config.symbol.end(), '-');
        const bool swap = config.symbol.size() > 5 && config.symbol.compare(config.symbol.size() - 5, 5, "-SWAP") == 0;
        if (market == "spot" ? dashes != 1 || swap : dashes != 2 || !swap || config.symbol.front() == '-')
            throw Error(23, market == "spot" ? "OKX spot instruments look like BTC-USDT" : "OKX perpetual swaps look like BTC-USDT-SWAP");
    }
    if (!rest_given) config.rest_url = rest;
    if (!ws_given) config.ws_url = ws;
}
}
int run(int argc, char** argv) {
    using namespace pineforge::feed;
    StdoutFlags restore;
    std::signal(SIGTERM, &on_signal);
    std::signal(SIGINT, &on_signal);
    std::signal(SIGPIPE, SIG_IGN);
    try {
        if (argc == 2 && (std::string(argv[1]) == "--help" || std::string(argv[1]) == "-h")) { help(); return 0; }
        if (argc == 2 && std::string(argv[1]) == "--version") { check_runtime_curl(); std::cout << "pineforge-feed 0.1.0\n"; return 0; }
        if (argc < 2) throw Error(23, "choose warmup or run; see --help");
        const std::string command = argv[1];
        if (command != "run" && command != "warmup" && command != "serve" && command != "export")
            throw Error(23, "choose warmup, export, run or serve; see --help");
        Config config;
        std::string output;
        ExportOptions exported;
        std::set<std::string> seen;
        for (int index = 2; index < argc; ++index) {
            const std::string key = argv[index];
            if (!seen.insert(key).second) throw Error(23, "duplicate CLI option: " + key);
            if (key == "--resume") { config.resume = true; continue; }
            if (key == "--allow-insecure-http") { config.allow_insecure = true; continue; }
            if (key == "--allow-remote-listen") { config.allow_remote_listen = true; continue; }
            if (index + 1 >= argc) throw Error(23, "CLI option needs a value: " + key);
            const std::string value = argv[++index];
            if (key == "--venue") config.venue = value;
            else if (key == "--market") config.market = value;
            else if (key == "--symbol") config.symbol = value;
            else if (key == "--mode") config.mode = value;
            else if (key == "--state-dir") config.state_dir = value;
            else if (key == "--start") config.start = timestamp(value);
            else if (key == "--end") config.end = timestamp(value);
            else if (key == "--output") output = value;
            else if (key == "--archive") exported.archive = value;
            else if (key == "--checksum") exported.checksum = value;
            else if (key == "--warmup") exported.warmup = value;
            else if (key == "--qty-step") exported.qty_step = value;
            else if (key == "--output-from") config.output_from = unsigned_value(value);
            else if (key == "--max-messages") config.max_messages = unsigned_value(value);
            else if (key == "--segment-bytes") config.segment_bytes = unsigned_value(value);
            else if (key == "--replay-bytes") config.replay_bytes = unsigned_value(value);
            else if (key == "--replay-age-seconds") {
                const auto seconds = unsigned_value(value);
                if (seconds > 10ULL * 366 * 86400) throw Error(23, "--replay-age-seconds is at most ten years");
                config.replay_age_ms = static_cast<std::int64_t>(seconds) * 1000;
            }
            else if (key == "--listen") config.listen = value;
            else if (key == "--client-queue-bytes") config.client_queue_bytes = unsigned_value(value);
            else if (key == "--max-clients") config.max_clients = unsigned_value(value);
            else if (key == "--max-replay-seconds") config.max_replay_seconds = unsigned_value(value);
            else if (key == "--max-queue-bytes") config.max_queue_bytes = unsigned_value(value);
            else if (key == "--reconnect-seconds") config.reconnect_seconds = unsigned_value(value);
            else if (key == "--keepalive-seconds") config.keepalive_seconds = unsigned_value(value);
            else if (key == "--silence-seconds") config.silence_seconds = unsigned_value(value);
            else if (key == "--rest-url") config.rest_url = value;
            else if (key == "--ws-url") config.ws_url = value;
            else throw Error(23, "unknown CLI option: " + key);
        }
        gate(config, command == "warmup", seen.count("--rest-url") != 0, seen.count("--ws-url") != 0);
        if (command != "export" &&
            (!exported.archive.empty() || !exported.checksum.empty() || !exported.warmup.empty() || !exported.qty_step.empty()))
            throw Error(23, "--archive, --checksum, --warmup and --qty-step are export options");
        if (!config.max_queue_bytes || !config.max_replay_seconds || config.max_replay_seconds > 3600 ||
            !config.reconnect_seconds || config.reconnect_seconds > 86100)
            throw Error(23, "budgets must be positive; reconnect must precede the 24-hour connection limit");
        if (config.segment_bytes < 4096 || config.segment_bytes > (1ULL << 30) || config.replay_bytes / 2 < config.segment_bytes)
            throw Error(23, "--segment-bytes must be 4096..1 GiB and --replay-bytes at least twice --segment-bytes");
        // A client queue holds at least one atomic message (at most 4096 bytes).
        if (config.client_queue_bytes < 4096 || config.client_queue_bytes > (1ULL << 30) || !config.max_clients || config.max_clients > 1024)
            throw Error(23, "--client-queue-bytes must be 4096..1 GiB and --max-clients 1..1024");
        // OKX closes a connection after 30 s without traffic; Bybit recommends a ping every 20 s.
        if (!config.keepalive_seconds || config.keepalive_seconds > 25) throw Error(23, "--keepalive-seconds must be 1..25");
        if (!config.silence_seconds || config.silence_seconds > 75) throw Error(23, "--silence-seconds must be 1..75");
        check_runtime_curl();
        validate_origin(config.rest_url, false, config.allow_insecure);
        validate_origin(config.ws_url, true, config.allow_insecure);
        if (command == "export") {
            if (output.empty() || config.resume || !config.state_dir.empty() || seen.count("--output-from") || !tick_mode(config.mode) ||
                config.start < 0 || config.end < 0)
                throw Error(23, "export takes --mode ticks|agg-ticks, --start, --end and --output (plus --warmup, --archive [--checksum])");
            if (!exported.checksum.empty() && exported.archive.empty()) throw Error(23, "--checksum verifies an --archive");
            exported.output = output;
            export_bars(config, exported);
        } else if (command == "warmup") {
            if (output.empty() || config.resume || !config.state_dir.empty() || seen.count("--output-from") || seen.count("--mode"))
                throw Error(23, "warmup requires --output and does not accept run-state options");
            warmup(config, output);
        } else if (command == "serve") {
            if (config.state_dir.empty() || seen.count("--end") || !output.empty() || seen.count("--output-from"))
                throw Error(23, "serve requires --state-dir; clients choose their own cursor (from=I), so --output-from is refused");
#if PINEFORGE_FEED_SERVE
            serve_feed(config);
#else
            throw Error(23, "this build has no serve subcommand (configured with PINEFORGE_FEED_SERVE=OFF)");
#endif
        } else {
            if (seen.count("--listen") || config.allow_remote_listen || seen.count("--client-queue-bytes") || seen.count("--max-clients"))
                throw Error(23, "--listen, --allow-remote-listen, --client-queue-bytes and --max-clients are serve options");
            if (config.state_dir.empty() || seen.count("--end") || !output.empty() || (!config.resume && seen.count("--output-from")))
                throw Error(23, "run requires --state-dir; --output-from requires --resume");
            const auto flags = ::fcntl(STDOUT_FILENO, F_GETFL);
            if (flags < 0) throw Error(22, "cannot configure bounded stdout drain");
            restore.saved = flags;
            if (::fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) != 0) throw Error(22, "cannot configure bounded stdout drain");
            run_feed(config);
        }
        return 0;
    } catch (const Stopped&) { log("info", "stopped"); return 0; }
    catch (const std::bad_alloc&) { log("error", "allocation_exhausted"); return 22; }
    catch (const Error& failure) {
        log("error", "stop", Json::object({{"code", Json::number(std::to_string(failure.code))}, {"reason", Json::string(failure.what())}}));
        return failure.code;
    } catch (const std::exception& failure) {
        log("error", "stop", Json::object({{"code", Json::number("23")}, {"reason", Json::string(failure.what())}}));
        return 23;
    }
}

int main(int argc, char** argv) {
    const int code = run(argc, argv);
    pineforge::feed::flush_log(std::chrono::seconds(2));
    return code;
}
