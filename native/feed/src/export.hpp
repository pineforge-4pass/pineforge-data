// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "core.hpp"
#include <functional>
#include <optional>

namespace pineforge::feed {
struct ExportOptions {
    std::string output;    // the warmup-format CSV to write; refused if it exists
    std::string archive;   // optional: a venue daily aggregate-trade archive (.zip or extracted .csv)
    std::string checksum;  // optional: the archive's published .CHECKSUM file
    std::string warmup;    // optional: the runner's warmup CSV, ending at the minute before the start
};
// Prints-built one-minute bars over [config.start, config.end), by the runner's tick-built bar rule, from venue
// REST within its retention or from a local daily archive.
void export_bars(const Config& config, const ExportOptions& options);

// The runner's tick-built bar rule over [start, end) on a proven print chain: anchor() takes the predecessor (the
// last print strictly before start), add() every later print in ID order until it returns true at the fence (the
// first print at or after end). A minute is first/max/min/last price and the exact volume sum; a minute without a
// print repeats the previous close with volume 0. Before the window's first print that close is the runner's last
// warmup close (seed()), as the runner carries it; without a seed a quiet first minute stops 20. A hole stops 20,
// a time regression along the chain 23.
class ChainBars {
    std::int64_t start_, end_, minute_ = -1, next_, last_ts_ = -1;
    std::uint64_t last_id_ = 0, count_ = 0, prints_ = 0, bars_ = 0, quiet_ = 0;
    std::optional<Trade> predecessor_, fence_;
    std::string open_, high_, low_, close_, csv_, seed_;
    Decimal high_value_{"0"}, low_value_{"0"}, volume_{"0"};
    void flush();
    void carry(std::int64_t until);
    void row(std::int64_t minute, const std::string& open, const std::string& high, const std::string& low,
             const std::string& close, const std::string& volume);
public:
    ChainBars(std::int64_t start, std::int64_t end);
    // The close a quiet first minute carries: the last warmup bar's close (before anchor()).
    void seed(const std::string& close);
    void anchor(const Trade& predecessor);
    bool add(const Trade& trade);
    bool anchored() const { return predecessor_.has_value(); }
    bool fenced() const { return fence_.has_value(); }
    std::uint64_t next_id() const { return last_id_ + 1; }
    std::int64_t last_ts() const { return last_ts_; }
    // The warmup CSV (header plus one row per minute); 20 until the fence is in hand.
    const std::string& csv() const;
    const Trade& predecessor() const;
    const Trade& fence() const;
    std::uint64_t prints() const { return prints_; }
    std::uint64_t bars() const { return bars_; }
    std::uint64_t quiet_minutes() const { return quiet_; }
    const std::string& seeded_close() const { return seed_; }
};
// One daily-archive line, columns agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,
// is_buyer_maker: nullopt for the header, which only the first line may be. A transact_time above 10^14 is
// microseconds and becomes floor(us / 1000) milliseconds.
std::optional<Trade> archive_row(std::string_view line, bool first_line);
// CRC-32 (ISO-HDLC, as zip uses) of `bytes` continued from `crc`; start from 0.
std::uint32_t crc32(std::uint32_t crc, std::string_view bytes);
// RFC 1951 raw deflate: pulls input from `source` (0 at its end), pushes output to `sink`. Corrupt or truncated
// input, or bytes after the final block, stop 21.
void inflate(const std::function<std::size_t(char*, std::size_t)>& source, const std::function<void(std::string_view)>& sink);
// Streams the only entry of a stored or deflated zip to `sink`, then checks its CRC-32 and size (21). Returns
// the entry name.
std::string unzip(const std::string& path, const std::function<void(std::string_view)>& sink);
std::string file_sha256(const std::string& path);
// A published `<sha256-hex>  <file name>` checksum must name the archive's base name and carry its SHA-256 (21).
void verify_checksum(const std::string& checksum, const std::string& archive, const std::string& digest);
// The window's chain from a daily aggregate-trade archive (.zip, else an extracted .csv), streamed: only the
// predecessor candidate, the window's bars and the fence are kept.
ChainBars archive_bars(const std::string& archive, std::int64_t start, std::int64_t end, const std::string& seed = {});
// The close of the warmup CSV's last row, which must be the minute before start (20 otherwise; 23 if malformed).
std::string warmup_close(const std::string& warmup, std::int64_t start);
}
