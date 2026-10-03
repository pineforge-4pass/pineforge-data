// SPDX-License-Identifier: Apache-2.0
#include "binance.hpp"
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
    std::cout << "pineforge-feed warmup --venue binance --market spot --symbol SYMBOL --start UTC|MS --end UTC|MS --output FILE\n"
                 "pineforge-feed run --venue binance --market spot --symbol SYMBOL --mode bars|ticks --state-dir DIR [--start UTC|MS | --resume] [--output-from INDEX]\n"
                 "Limits: --max-messages N --max-log-bytes N --max-replay-seconds N --max-queue-bytes N --reconnect-seconds N\n"
                 "Testing/public origins: --rest-url ORIGIN --ws-url ORIGIN [--allow-insecure-http (loopback only)]\n";
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
        if (command != "run" && command != "warmup") throw Error(23, "only warmup and run are implemented");
        Config config;
        std::string output;
        std::set<std::string> seen;
        for (int index = 2; index < argc; ++index) {
            const std::string key = argv[index];
            if (!seen.insert(key).second) throw Error(23, "duplicate CLI option: " + key);
            if (key == "--resume") { config.resume = true; continue; }
            if (key == "--allow-insecure-http") { config.allow_insecure = true; continue; }
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
            else if (key == "--output-from") config.output_from = unsigned_value(value);
            else if (key == "--max-messages") config.max_messages = unsigned_value(value);
            else if (key == "--max-log-bytes") config.max_log_bytes = unsigned_value(value);
            else if (key == "--max-replay-seconds") config.max_replay_seconds = unsigned_value(value);
            else if (key == "--max-queue-bytes") config.max_queue_bytes = unsigned_value(value);
            else if (key == "--reconnect-seconds") config.reconnect_seconds = unsigned_value(value);
            else if (key == "--rest-url") config.rest_url = value;
            else if (key == "--ws-url") config.ws_url = value;
            else throw Error(23, "unknown CLI option: " + key);
        }
        if (config.venue != "binance" || config.market != "spot") throw Error(23, "this release supports public Binance spot only");
        if (config.symbol.empty() || config.symbol.size() > 32 || config.symbol.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") != std::string::npos)
            throw Error(23, "use an uppercase ASCII Binance symbol");
        if (config.mode != "bars" && config.mode != "ticks") throw Error(23, "mode must be bars or ticks");
        if (!config.max_log_bytes || !config.max_queue_bytes || !config.max_replay_seconds || config.max_replay_seconds > 3600 ||
            !config.reconnect_seconds || config.reconnect_seconds > 86100)
            throw Error(23, "budgets must be positive; reconnect must precede the 24-hour connection limit");
        check_runtime_curl();
        validate_origin(config.rest_url, false, config.allow_insecure);
        validate_origin(config.ws_url, true, config.allow_insecure);
        if (command == "warmup") {
            if (output.empty() || config.resume || !config.state_dir.empty() || seen.count("--output-from") || seen.count("--mode"))
                throw Error(23, "warmup requires --output and does not accept run-state options");
            warmup(config, output);
        } else {
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
