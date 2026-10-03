// SPDX-License-Identifier: Apache-2.0
#include "state.hpp"
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <fcntl.h>
#include <limits>
#include <utility>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

namespace pineforge::feed {
Descriptor::~Descriptor() { if (value_ >= 0) ::close(value_); }
void Descriptor::reset(int value) {
    if (value_ >= 0) ::close(value_);
    value_ = value;
}
namespace {
Json number(std::uint64_t value) { return Json::number(std::to_string(value)); }
Json signed_number(std::int64_t value) { return Json::number(std::to_string(value)); }
bool digest_token(const std::string& token) {
    return token.size() == 64 && token.find_first_not_of("0123456789abcdef") == std::string::npos;
}
void advance(Cursor& cursor, const Json& event, const std::string& mode) {
    const auto type = event.at("type").text();
    if (type == "tick" && tick_mode(mode)) {
        const auto trade = normalized_trade(event);
        const auto previous = cursor.seq ? cursor.seq : cursor.predecessor ? cursor.predecessor->id : 0;
        if (!previous || previous == std::numeric_limits<std::uint64_t>::max() || trade.id != previous + 1 ||
            trade.ts < cursor.cut || trade.ts >= cursor.cut + 60000 || trade.ts < cursor.last_tick_ts)
            throw Error(20, "tick does not extend the verified chronological minute prefix");
        cursor.seq = trade.id;
        cursor.last_tick_ts = trade.ts;
    } else if (type == "bar" && mode == "bars") {
        const auto bar = normalized_bar(event);
        if (bar.ts != cursor.cut) throw Error(20, "bar does not adjoin the verified cut");
        cursor.last_bar = bar.ts;
        cursor.cut += 60000;
    } else if (type == "time" && tick_mode(mode)) {
        const auto cut = event.at("ts").integer<std::int64_t>();
        // A fenced quiet minute may close before any print: the anchored predecessor bounds it.
        if ((!cursor.seq && !cursor.predecessor) || cut != cursor.cut + 60000 || cut <= cursor.last_tick_ts)
            throw Error(20, "time does not advance exactly one proven closed minute");
        cursor.cut = cut;
    } else throw Error(23, "journal contains an unsupported normalized message");
}
}

std::string State::log_path() const { return config_.state_dir + "/events.jsonl"; }

Json State::serialize(const Cursor& cursor) const {
    Json proofs;
    proofs.kind = Json::Kind::Array;
    for (const auto& bar : cursor.proofs) proofs.items.push_back(parse_json(bar.wire()));
    return Json::object({
        {"schema", Json::string("pineforge-feed-cursor/v1")}, {"epoch", Json::string(cursor.epoch)},
        {"venue", Json::string(config_.venue)}, {"market", Json::string(config_.market)},
        {"symbol", Json::string(config_.symbol)}, {"mode", Json::string(config_.mode)},
        {"rest_origin", Json::string(config_.rest_url)}, {"ws_origin", Json::string(config_.ws_url)},
        {"units", Json::object({{"price", Json::string("quote/base")}, {"qty", Json::string("base")}, {"ts", Json::string("unix-ms")}})},
        {"qty_multiplier", Json::string(config_.qty_multiplier)},
        {"start", signed_number(cursor.start)}, {"verified_cut", signed_number(cursor.cut)},
        {"venue_id", tick_mode(config_.mode) ? number(cursor.seq) : signed_number(cursor.last_bar)},
        {"emitted_seq", number(cursor.seq)}, {"last_tick_ts", signed_number(cursor.last_tick_ts)},
        {"last_bar", signed_number(cursor.last_bar)}, {"message_index", number(cursor.message_index)},
        {"log_bytes", number(cursor.log_bytes)}, {"prefix_hash", Json::string(cursor.prefix_hash)},
        {"overlap_hash", Json::string(cursor.overlap_hash)},
        {"predecessor", cursor.predecessor ? parse_json(cursor.predecessor->wire()) : Json{}},
        {"closed_proofs", std::move(proofs)}, {"partition", Json::string("one-event-per-message")}
    });
}

void State::persist(const Cursor& cursor) const {
    const auto document = serialize(cursor);
    atomic_file(config_.state_dir + "/cursor.json", Json::object({{"cursor", document},
        {"sha256", Json::string(sha256(document.dump()))}}).dump() + '\n');
}

State::State(const Config& config) : config_(config) {
    std::error_code failure;
    std::filesystem::create_directories(config_.state_dir, failure);
    if (failure) throw Error(22, "cannot create state directory");
    lock_.reset(::open((config_.state_dir + "/producer.lock").c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0600));
    if (lock_.get() < 0 || ::flock(lock_.get(), LOCK_EX | LOCK_NB) != 0)
        throw Error(22, "state directory has another producer or cannot be locked");
    if (config_.resume) {
        recover();
        journal_.reset(::open(log_path().c_str(), O_WRONLY | O_APPEND | O_CLOEXEC));
        if (journal_.get() < 0 || ::fsync(journal_.get()) != 0) throw Error(22, "cannot sync rolled-back journal tail");
    } else {
        if (config_.start < 0 || config_.start % 60000) throw Error(23, "new streams require a minute-aligned --start");
        // An empty journal without a cursor is an initialization that crashed before its first cursor
        // write: nothing was committed or published, so a fresh start may take the directory over.
        if (std::filesystem::exists(config_.state_dir + "/cursor.json") ||
            (std::filesystem::exists(log_path()) && std::filesystem::file_size(log_path()) != 0))
            throw Error(23, "state already exists; use --resume or an explicitly new state directory");
        cursor_.epoch = random_epoch();
        cursor_.start = cursor_.cut = config_.start;
        cursor_.prefix_hash = sha256("");
        cursor_.overlap_hash = sha256("");
        journal_.reset(::open(log_path().c_str(), O_WRONLY | O_APPEND | O_CREAT | O_TRUNC | O_CLOEXEC, 0600));
        if (journal_.get() < 0 || ::fsync(journal_.get()) != 0) throw Error(22, "cannot initialize durable journal");
        persist(cursor_);
        durable_ = cursor_;
    }
    if (journal_.get() < 0) throw Error(22, "cannot open durable journal");
    if (config_.output_from > cursor_.message_index) throw Error(22, "output cursor exceeds the retained verified prefix");
}

void State::recover() {
    try {
        const auto path = config_.state_dir + "/cursor.json";
        if (!std::filesystem::exists(path) || std::filesystem::file_size(path) > 65536)
            throw Error(22, "missing or oversized durable cursor");
        std::ifstream input(path);
        const std::string bytes((std::istreambuf_iterator<char>(input)), {});
        const auto envelope = parse_json(bytes);
        const auto& document = envelope.at("cursor");
        if (envelope.at("sha256").text() != sha256(document.dump())) throw Error(21, "cursor digest mismatch");
        if (document.at("schema").text() != "pineforge-feed-cursor/v1" || document.at("partition").text() != "one-event-per-message")
            throw Error(23, "unsupported cursor schema or message partition");
        for (const auto& item : {std::make_pair("venue", config_.venue), {"market", config_.market}, {"symbol", config_.symbol},
                                {"mode", config_.mode}, {"rest_origin", config_.rest_url}, {"ws_origin", config_.ws_url}})
            if (document.at(item.first).text() != item.second) throw Error(21, "resume source identity changed");
        const auto& units = document.at("units");
        if (units.at("price").text() != "quote/base" || units.at("qty").text() != "base" || units.at("ts").text() != "unix-ms")
            throw Error(21, "resume units changed");
        // Contract quantities were converted with this instrument multiplier; a change would mix units.
        const auto* multiplier = document.find("qty_multiplier");
        if ((multiplier ? multiplier->text() : std::string("1")) != config_.qty_multiplier)
            throw Error(21, "resume contract quantity multiplier changed");
        cursor_.epoch = document.at("epoch").text();
        cursor_.prefix_hash = document.at("prefix_hash").text();
        cursor_.overlap_hash = document.at("overlap_hash").text();
        if (!digest_token(cursor_.epoch) || !digest_token(cursor_.prefix_hash) || !digest_token(cursor_.overlap_hash))
            throw Error(21, "invalid cursor hash or epoch");
        cursor_.start = document.at("start").integer<std::int64_t>();
        cursor_.cut = document.at("verified_cut").integer<std::int64_t>();
        cursor_.seq = document.at("emitted_seq").integer<std::uint64_t>();
        cursor_.last_tick_ts = document.at("last_tick_ts").integer<std::int64_t>();
        cursor_.last_bar = document.at("last_bar").integer<std::int64_t>();
        if ((tick_mode(config_.mode) && document.at("venue_id").integer<std::uint64_t>() != cursor_.seq) ||
            (config_.mode == "bars" && document.at("venue_id").integer<std::int64_t>() != cursor_.last_bar))
            throw Error(21, "venue ID does not match the emitted cursor");
        cursor_.message_index = document.at("message_index").integer<std::uint64_t>();
        cursor_.log_bytes = document.at("log_bytes").integer<std::uint64_t>();
        if (cursor_.start < 0 || cursor_.start % 60000 || cursor_.cut < cursor_.start || cursor_.cut % 60000 ||
            cursor_.log_bytes > config_.max_log_bytes || (config_.start >= 0 && config_.start != cursor_.start))
            throw Error(21, "invalid or changed resume cut");
        if (document.at("predecessor").kind != Json::Kind::Null) cursor_.predecessor = normalized_trade(document.at("predecessor"));
        const auto& proofs = document.at("closed_proofs");
        if (proofs.kind != Json::Kind::Array || proofs.items.size() > 32) throw Error(21, "invalid closed-minute proof window");
        for (const auto& proof : proofs.items) cursor_.proofs.push_back(normalized_bar(proof));
        if (!std::filesystem::exists(log_path()) || std::filesystem::file_size(log_path()) < cursor_.log_bytes)
            throw Error(21, "durable journal is shorter than its verified cursor");
        durable_ = cursor_;
        Cursor reconstructed;
        reconstructed.start = reconstructed.cut = cursor_.start;
        reconstructed.predecessor = cursor_.predecessor;
        reconstructed.prefix_hash = reconstructed.overlap_hash = sha256("");
        visit(0, [&](const std::string& line) {
            const auto event = parse_json(line);
            advance(reconstructed, event, config_.mode);
            reconstructed.prefix_hash = sha256(reconstructed.prefix_hash + line + '\n');
            reconstructed.overlap_hash = sha256(line + '\n');
            reconstructed.log_bytes += line.size() + 1;
            ++reconstructed.message_index;
            remember(event);
        });
        if (reconstructed.prefix_hash != cursor_.prefix_hash || reconstructed.overlap_hash != cursor_.overlap_hash ||
            reconstructed.log_bytes != cursor_.log_bytes || reconstructed.message_index != cursor_.message_index ||
            reconstructed.seq != cursor_.seq || reconstructed.cut != cursor_.cut || reconstructed.last_bar != cursor_.last_bar ||
            reconstructed.last_tick_ts != cursor_.last_tick_ts)
            throw Error(21, "durable journal prefix does not match its cursor");
        if (::truncate(log_path().c_str(), static_cast<off_t>(cursor_.log_bytes)) != 0)
            throw Error(22, "cannot roll back an uncommitted journal tail");
        config_.start = cursor_.start;
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(21, "invalid durable cursor or journal"); }
}

void State::anchor(const Trade& predecessor) {
    predecessor.validate();
    if (cursor_.predecessor || cursor_.message_index || !staged_.empty() || predecessor.ts >= cursor_.start)
        throw Error(20, "initial raw-trade predecessor does not prove the start fence");
    auto next = cursor_;
    next.predecessor = predecessor;
    persist(next);
    durable_ = cursor_ = std::move(next);
}

void State::remember(const Json& event) {
    if (event.at("type").text() == "tick") {
        recent_trades_.push_back(normalized_trade(event));
        if (recent_trades_.size() > 1000) recent_trades_.pop_front();
    } else if (event.at("type").text() == "bar") {
        recent_bars_.push_back(normalized_bar(event));
        if (recent_bars_.size() > 32) recent_bars_.pop_front();
    }
}

void State::stage(const std::string& line, const std::optional<Bar>& proof) {
    if (line.size() > 4095 || line.find('\n') != std::string::npos) throw Error(23, "normalized message exceeds the atomic output bound");
    auto next = cursor_;
    const auto event = parse_json(line);
    advance(next, event, config_.mode);
    if (proof) {
        proof->validate();
        if (proof->ts + 60000 != next.cut) throw Error(23, "closed-minute proof does not match the committed cut");
        next.proofs.push_back(*proof);
        if (next.proofs.size() > 32) next.proofs.pop_front();
    }
    if (next.message_index == std::numeric_limits<std::uint64_t>::max() ||
        line.size() + 1 > config_.max_log_bytes - std::min(cursor_.log_bytes, config_.max_log_bytes))
        throw Error(22, "retained replay budget exhausted");
    next.log_bytes += line.size() + 1;
    ++next.message_index;
    next.prefix_hash = sha256(cursor_.prefix_hash + line + '\n');
    next.overlap_hash = sha256(line + '\n');
    staged_bytes_ += line;
    staged_bytes_ += '\n';
    staged_.push_back(line);
    cursor_ = std::move(next);
    remember(event);
}

std::vector<std::string> State::flush() {
    if (staged_.empty()) return {};
    // A failed append or cursor write leaves an uncommitted tail that only resume may truncate.
    if (unusable_) throw Error(22, "durable journal is unusable after a failed commit");
    unusable_ = true;
    write_all(journal_.get(), staged_bytes_);
    if (::fsync(journal_.get()) != 0) throw Error(22, "cannot sync normalized messages");
    persist(cursor_);
    unusable_ = false;
    durable_ = cursor_;
    staged_bytes_.clear();
    return std::exchange(staged_, {});
}

void State::visit(std::uint64_t from, const std::function<void(const std::string&)>& visitor) const {
    if (from > cursor_.message_index) throw Error(22, "replay cursor is unavailable");
    std::ifstream input(log_path(), std::ios::binary);
    if (!input) throw Error(22, "cannot read retained normalized log");
    std::uint64_t index = 0, bytes = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(config_.max_replay_seconds);
    std::string line;
    while (bytes < durable_.log_bytes) {
        if (stopping) throw Stopped{};
        if (std::chrono::steady_clock::now() >= deadline) throw Error(22, "retained prefix replay time budget exhausted");
        if (!std::getline(input, line) || line.size() > 4095 || bytes + line.size() + 1 > durable_.log_bytes)
            throw Error(21, "truncated or oversized verified message");
        bytes += line.size() + 1;
        if (index >= from) visitor(line);
        ++index;
    }
    if (bytes != durable_.log_bytes || index != durable_.message_index) throw Error(21, "verified message partition changed");
    for (const auto& staged : staged_) {
        if (index >= from) visitor(staged);
        ++index;
    }
}

std::optional<Trade> State::trade(std::uint64_t id) const {
    for (const auto& trade : recent_trades_) if (trade.id == id) return trade;
    std::optional<Trade> found;
    visit(0, [&](const std::string& line) {
        const auto event = parse_json(line);
        if (event.at("type").text() == "tick" && event.at("seq").integer<std::uint64_t>() == id) found = normalized_trade(event);
    });
    return found;
}
std::optional<Bar> State::bar(std::int64_t ts) const {
    for (const auto& bar : recent_bars_) if (bar.ts == ts) return bar;
    for (const auto& bar : cursor_.proofs) if (bar.ts == ts) return bar;
    std::optional<Bar> found;
    visit(0, [&](const std::string& line) {
        const auto event = parse_json(line);
        if (event.at("type").text() == "bar" && event.at("bar").at("ts_open").integer<std::int64_t>() == ts) found = normalized_bar(event);
    });
    return found;
}
Json State::status() const {
    return Json::object({{"epoch", Json::string(durable_.epoch)}, {"message_index", number(durable_.message_index)},
        {"emitted_seq", number(durable_.seq)}, {"verified_cut", signed_number(durable_.cut)},
        {"prefix_hash", Json::string(durable_.prefix_hash)}, {"cursor", Json::string(config_.state_dir + "/cursor.json")}});
}
}
