// SPDX-License-Identifier: Apache-2.0
#include "state.hpp"
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <fcntl.h>
#include <limits>
#include <map>
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
std::string segment_name(std::uint64_t base) {
    char name[32];
    std::snprintf(name, sizeof(name), "%020llu", static_cast<unsigned long long>(base));
    return name;
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
void sync_directory(const std::string& path) {
    const int directory = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (directory < 0) throw Error(22, "cannot open journal directory for sync");
    const auto status = ::fsync(directory);
    ::close(directory);
    if (status != 0) throw Error(22, "cannot sync journal directory");
}
std::string small_file(const std::string& path) {
    if (!std::filesystem::exists(path) || std::filesystem::file_size(path) > 65536) throw Error(22, "missing or oversized durable state file");
    std::ifstream input(path, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(input)), {});
}
// Every complete line of the first `limit` bytes of `path`; a line cut by `limit` or the end of the file
// is a broken message partition.
void read_lines(const std::string& path, std::uint64_t limit, std::chrono::steady_clock::time_point deadline,
                const std::function<void(const std::string&)>& visitor) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw Error(22, "cannot read retained normalized journal segment");
    std::uint64_t bytes = 0;
    std::string line;
    while (bytes < limit) {
        if (stopping) throw Stopped{};
        if (std::chrono::steady_clock::now() >= deadline) throw Error(22, "retained prefix replay time budget exhausted");
        if (!std::getline(input, line) || input.eof() || line.size() > 4095 || bytes + line.size() + 1 > limit)
            throw Error(21, "truncated or oversized verified message");
        bytes += line.size() + 1;
        visitor(line);
    }
}
Json checkpoint_document(const Cursor& cursor) {
    return Json::object({
        {"schema", Json::string("pineforge-feed-checkpoint/v1")}, {"epoch", Json::string(cursor.epoch)},
        {"message_index", number(cursor.message_index)}, {"log_bytes", number(cursor.log_bytes)},
        {"prefix_hash", Json::string(cursor.prefix_hash)}, {"overlap_hash", Json::string(cursor.overlap_hash)},
        {"start", signed_number(cursor.start)}, {"verified_cut", signed_number(cursor.cut)},
        {"emitted_seq", number(cursor.seq)}, {"last_tick_ts", signed_number(cursor.last_tick_ts)},
        {"last_bar", signed_number(cursor.last_bar)},
        {"predecessor", cursor.predecessor ? parse_json(cursor.predecessor->wire()) : Json{}}
    });
}
// The verified fields a segment boundary must reproduce exactly.
bool same_state(const Cursor& left, const Cursor& right) {
    return left.message_index == right.message_index && left.log_bytes == right.log_bytes &&
        left.prefix_hash == right.prefix_hash && left.overlap_hash == right.overlap_hash && left.cut == right.cut &&
        left.seq == right.seq && left.last_tick_ts == right.last_tick_ts && left.last_bar == right.last_bar;
}
bool parse_base(const std::string& digits, std::uint64_t& base) {
    if (digits.size() != 20 || digits.find_first_not_of("0123456789") != std::string::npos) return false;
    base = std::stoull(digits);
    return true;
}
}

std::string State::journal_dir() const { return config_.state_dir + "/journal"; }
std::string State::segment_path(std::uint64_t base) const { return journal_dir() + "/" + segment_name(base) + ".jsonl"; }
std::string State::checkpoint_path(std::uint64_t base) const { return journal_dir() + "/" + segment_name(base) + ".checkpoint.json"; }

Json State::serialize(const Cursor& cursor) const {
    Json proofs;
    proofs.kind = Json::Kind::Array;
    for (const auto& bar : cursor.proofs) proofs.items.push_back(parse_json(bar.wire()));
    return Json::object({
        {"schema", Json::string("pineforge-feed-cursor/v2")}, {"epoch", Json::string(cursor.epoch)},
        {"venue", Json::string(config_.venue)}, {"market", Json::string(config_.market)},
        {"symbol", Json::string(config_.symbol)}, {"mode", Json::string(config_.mode)},
        {"rest_origin", Json::string(config_.rest_url)}, {"ws_origin", Json::string(config_.ws_url)},
        {"units", Json::object({{"price", Json::string("quote/base")}, {"qty", Json::string("base")}, {"ts", Json::string("unix-ms")}})},
        {"qty_multiplier", Json::string(config_.qty_multiplier)},
        {"start", signed_number(cursor.start)}, {"verified_cut", signed_number(cursor.cut)},
        {"venue_id", tick_mode(config_.mode) ? number(cursor.seq) : signed_number(cursor.last_bar)},
        {"emitted_seq", number(cursor.seq)}, {"last_tick_ts", signed_number(cursor.last_tick_ts)},
        {"last_bar", signed_number(cursor.last_bar)}, {"message_index", number(cursor.message_index)},
        {"log_bytes", number(cursor.log_bytes)}, {"segment", number(cursor.segment)},
        {"prefix_hash", Json::string(cursor.prefix_hash)}, {"overlap_hash", Json::string(cursor.overlap_hash)},
        {"predecessor", cursor.predecessor ? parse_json(cursor.predecessor->wire()) : Json{}},
        {"closed_proofs", std::move(proofs)}, {"partition", Json::string("one-event-per-message")}
    });
}

void State::persist(const Cursor& cursor) const {
    const auto document = serialize(cursor);
    atomic_file(config_.state_dir + "/cursor.json", Json::object({{"cursor", document},
        {"sha256", Json::string(sha256(document.dump()))}}).dump() + '\n');
}

void State::write_checkpoint(const Cursor& cursor) const {
    const auto document = checkpoint_document(cursor);
    atomic_file(checkpoint_path(cursor.message_index), Json::object({{"checkpoint", document},
        {"sha256", Json::string(sha256(document.dump()))}}).dump() + '\n');
}

Cursor State::read_checkpoint(std::uint64_t base) const {
    try {
        const auto envelope = parse_json(small_file(checkpoint_path(base)));
        const auto& document = envelope.at("checkpoint");
        if (envelope.at("sha256").text() != sha256(document.dump())) throw Error(21, "journal checkpoint digest mismatch");
        if (document.at("schema").text() != "pineforge-feed-checkpoint/v1") throw Error(23, "unsupported journal checkpoint schema");
        Cursor checkpoint;
        checkpoint.epoch = document.at("epoch").text();
        checkpoint.message_index = document.at("message_index").integer<std::uint64_t>();
        checkpoint.log_bytes = document.at("log_bytes").integer<std::uint64_t>();
        checkpoint.prefix_hash = document.at("prefix_hash").text();
        checkpoint.overlap_hash = document.at("overlap_hash").text();
        checkpoint.start = document.at("start").integer<std::int64_t>();
        checkpoint.cut = document.at("verified_cut").integer<std::int64_t>();
        checkpoint.seq = document.at("emitted_seq").integer<std::uint64_t>();
        checkpoint.last_tick_ts = document.at("last_tick_ts").integer<std::int64_t>();
        checkpoint.last_bar = document.at("last_bar").integer<std::int64_t>();
        if (document.at("predecessor").kind != Json::Kind::Null) checkpoint.predecessor = normalized_trade(document.at("predecessor"));
        if (checkpoint.message_index != base || !digest_token(checkpoint.prefix_hash) || !digest_token(checkpoint.overlap_hash))
            throw Error(21, "journal checkpoint does not describe its segment");
        checkpoint.segment = base;
        return checkpoint;
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(21, "invalid journal checkpoint"); }
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
        journal_.reset(::open(segment_path(cursor_.segment).c_str(), O_WRONLY | O_APPEND | O_CLOEXEC));
        if (journal_.get() < 0 || ::fsync(journal_.get()) != 0) throw Error(22, "cannot sync rolled-back journal tail");
    } else initialize();
    if (journal_.get() < 0) throw Error(22, "cannot open durable journal");
    if (config_.output_from > cursor_.message_index) throw Error(22, "output cursor exceeds the retained verified prefix");
    if (config_.resume && !config_.serve && config_.output_from < first_retained())
        throw Error(22, "output cursor " + std::to_string(config_.output_from) + " has expired: messages before index " +
            std::to_string(first_retained()) + " left the retained journal (--replay-bytes, --replay-age-seconds). A consumer "
            "behind that index cannot resume from this stream: rebuild it from a warmup that ends inside the retained window, "
            "or keep a longer retention");
}

void State::initialize() {
    if (config_.start < 0 || config_.start % 60000) throw Error(23, "new streams require a minute-aligned --start");
    if (std::filesystem::exists(config_.state_dir + "/events.jsonl"))
        throw Error(23, "this state directory holds an unsegmented journal from an earlier release; start a new state directory");
    if (std::filesystem::exists(config_.state_dir + "/cursor.json"))
        throw Error(23, "state already exists; use --resume or an explicitly new state directory");
    std::error_code failure;
    std::filesystem::create_directories(journal_dir(), failure);
    if (failure) throw Error(22, "cannot create journal directory");
    // A journal without a cursor is an initialization that crashed before its first cursor write: nothing
    // was committed or published, so a fresh start may take the directory over if no segment holds a byte.
    std::vector<std::filesystem::path> leftovers;
    for (const auto& entry : std::filesystem::directory_iterator(journal_dir())) {
        if (entry.path().extension() == ".jsonl" && entry.file_size() != 0)
            throw Error(23, "state already exists; use --resume or an explicitly new state directory");
        leftovers.push_back(entry.path());
    }
    for (const auto& path : leftovers) std::filesystem::remove(path);
    cursor_.epoch = random_epoch();
    cursor_.start = cursor_.cut = config_.start;
    cursor_.prefix_hash = sha256("");
    cursor_.overlap_hash = sha256("");
    write_checkpoint(cursor_);
    journal_.reset(::open(segment_path(0).c_str(), O_WRONLY | O_APPEND | O_CREAT | O_EXCL | O_CLOEXEC, 0600));
    if (journal_.get() < 0 || ::fsync(journal_.get()) != 0) throw Error(22, "cannot initialize durable journal");
    sync_directory(journal_dir());
    persist(cursor_);
    durable_ = cursor_;
    segments_ = {{0, 0, 0, cursor_.start, -1}};
}

void State::recover() {
    try {
        const auto envelope = parse_json(small_file(config_.state_dir + "/cursor.json"));
        const auto& document = envelope.at("cursor");
        if (envelope.at("sha256").text() != sha256(document.dump())) throw Error(21, "cursor digest mismatch");
        if (document.at("schema").text() != "pineforge-feed-cursor/v2" || document.at("partition").text() != "one-event-per-message")
            throw Error(23, "unsupported cursor schema or message partition");
        for (const auto& item : {std::make_pair("venue", config_.venue), {"market", config_.market}, {"symbol", config_.symbol},
                                {"mode", config_.mode}, {"rest_origin", config_.rest_url}, {"ws_origin", config_.ws_url}})
            if (document.at(item.first).text() != item.second) throw Error(21, "resume source identity changed");
        const auto& units = document.at("units");
        if (units.at("price").text() != "quote/base" || units.at("qty").text() != "base" || units.at("ts").text() != "unix-ms")
            throw Error(21, "resume units changed");
        // Contract quantities were converted with this instrument multiplier; a change would mix units.
        if (document.at("qty_multiplier").text() != config_.qty_multiplier) throw Error(21, "resume contract quantity multiplier changed");
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
        cursor_.segment = document.at("segment").integer<std::uint64_t>();
        if (cursor_.start < 0 || cursor_.start % 60000 || cursor_.cut < cursor_.start || cursor_.cut % 60000 ||
            cursor_.segment > cursor_.message_index || (config_.start >= 0 && config_.start != cursor_.start))
            throw Error(21, "invalid or changed resume cut");
        if (document.at("predecessor").kind != Json::Kind::Null) cursor_.predecessor = normalized_trade(document.at("predecessor"));
        const auto& proofs = document.at("closed_proofs");
        if (proofs.kind != Json::Kind::Array || proofs.items.size() > 32) throw Error(21, "invalid closed-minute proof window");
        for (const auto& proof : proofs.items) cursor_.proofs.push_back(normalized_bar(proof));
        // Segments after the cursor's open one are an interrupted rotation: never committed, so removed. A
        // checkpoint without its segment is an interrupted expiry. A segment without its checkpoint never occurs.
        std::map<std::uint64_t, std::pair<bool, bool>> files;
        for (const auto& entry : std::filesystem::directory_iterator(journal_dir())) {
            const auto name = entry.path().filename().string();
            std::uint64_t base = 0;
            if (!name.empty() && name.front() == '.') continue;
            if (name.size() > 4 && name.compare(name.size() - 4, 4, ".tmp") == 0) { std::filesystem::remove(entry.path()); continue; }
            if (name.size() == 26 && name.compare(20, 6, ".jsonl") == 0 && parse_base(name.substr(0, 20), base)) files[base].first = true;
            else if (name.size() == 36 && name.compare(20, 16, ".checkpoint.json") == 0 && parse_base(name.substr(0, 20), base)) files[base].second = true;
            else throw Error(21, "unexpected file in the journal directory: " + name);
        }
        std::vector<std::uint64_t> retained;
        bool removed = false;
        std::uint64_t lowest = cursor_.segment;
        for (const auto& [base, kinds] : files)
            if (kinds.first) lowest = std::min(lowest, base);
        for (const auto& [base, kinds] : files) {
            if (base > cursor_.segment) {
                // Only the successor an interrupted seal creates: at the cursor's index, and still empty.
                if (base != cursor_.message_index || (kinds.first && std::filesystem::file_size(segment_path(base)) != 0))
                    throw Error(21, "a journal segment after the cursor's open segment holds data the cursor does not cover");
                std::filesystem::remove(segment_path(base));
                std::filesystem::remove(checkpoint_path(base));
                log("warn", "uncommitted_rotation_removed", Json::object({{"segment", number(base)}}));
                removed = true;
            } else if (!kinds.first) {
                // An interrupted expiry leaves the oldest checkpoint without its segment; anything else is a hole.
                if (base >= lowest) throw Error(21, "the retained journal has a hole: a checkpoint without its segment");
                std::filesystem::remove(checkpoint_path(base));
                removed = true;
            } else if (!kinds.second) throw Error(21, "a journal segment has no checkpoint");
            else retained.push_back(base);
        }
        if (removed) sync_directory(journal_dir());
        if (retained.empty() || retained.back() != cursor_.segment) throw Error(21, "the open journal segment is missing");
        Cursor reconstructed = read_checkpoint(retained.front());
        if (reconstructed.epoch != cursor_.epoch || reconstructed.start != cursor_.start)
            throw Error(21, "journal checkpoint belongs to another stream");
        // The start predecessor is anchored once, before the first message, and never changes.
        if (!reconstructed.predecessor) reconstructed.predecessor = cursor_.predecessor;
        else if (!cursor_.predecessor || !(*reconstructed.predecessor == *cursor_.predecessor))
            throw Error(21, "journal checkpoint predecessor does not match the cursor");
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(config_.max_replay_seconds);
        for (std::size_t index = 0; index < retained.size(); ++index) {
            const auto base = retained[index];
            if (index && !same_state(reconstructed, read_checkpoint(base)))
                throw Error(21, "durable journal segment boundary does not match its checkpoint");
            segments_.push_back({base, reconstructed.log_bytes, reconstructed.seq, reconstructed.cut, reconstructed.last_tick_ts});
            const auto path = segment_path(base);
            const auto size = std::filesystem::file_size(path);
            const bool open = index + 1 == retained.size();
            if (open && cursor_.log_bytes < reconstructed.log_bytes) throw Error(21, "cursor precedes its open journal segment");
            const auto limit = open ? cursor_.log_bytes - reconstructed.log_bytes : size;
            if (size < limit) throw Error(21, "durable journal is shorter than its verified cursor");
            read_lines(path, limit, deadline, [&](const std::string& line) {
                const auto event = parse_json(line);
                advance(reconstructed, event, config_.mode);
                reconstructed.prefix_hash = sha256(reconstructed.prefix_hash + line + '\n');
                reconstructed.overlap_hash = sha256(line + '\n');
                reconstructed.log_bytes += line.size() + 1;
                ++reconstructed.message_index;
                remember(event);
            });
        }
        if (!same_state(reconstructed, cursor_)) throw Error(21, "durable journal prefix does not match its cursor");
        // Only a verified journal is changed: the open segment's uncommitted tail goes last.
        if (::truncate(segment_path(cursor_.segment).c_str(), static_cast<off_t>(cursor_.log_bytes - segments_.back().bytes)) != 0)
            throw Error(22, "cannot roll back an uncommitted journal tail");
        config_.start = cursor_.start;
        durable_ = cursor_;
    } catch (const Error&) { throw; }
    catch (const Stopped&) { throw; }
    catch (const std::filesystem::filesystem_error&) { throw Error(22, "journal I/O failed during recovery"); }
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
    if (next.message_index == std::numeric_limits<std::uint64_t>::max()) throw Error(22, "message index exhausted");
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
    crash_point("commit-append");
    if (::fsync(journal_.get()) != 0) throw Error(22, "cannot sync normalized messages");
    persist(cursor_);
    crash_point("commit-cursor");
    unusable_ = false;
    durable_ = cursor_;
    staged_bytes_.clear();
    auto lines = std::exchange(staged_, {});
    // The lines are committed; a failed seal or expiry leaves files only resume may reconcile.
    try {
        if (durable_.log_bytes - segments_.back().bytes >= config_.segment_bytes) rotate();
        retain();
    } catch (...) {
        unusable_ = true;
        throw;
    }
    return lines;
}

// Seal the open segment: its successor's checkpoint first, then the empty successor, then the cursor that
// names it. A crash before the cursor leaves files recovery removes; the old segment stays open.
void State::rotate() {
    const auto base = durable_.message_index;
    if (!staged_.empty() || base == segments_.back().base) return;
    write_checkpoint(durable_);
    crash_point("rotate-checkpoint");
    Descriptor next(::open(segment_path(base).c_str(), O_WRONLY | O_APPEND | O_CREAT | O_EXCL | O_CLOEXEC, 0600));
    if (next.get() < 0 || ::fsync(next.get()) != 0) throw Error(22, "cannot create the next journal segment");
    sync_directory(journal_dir());
    crash_point("rotate-segment");
    auto rotated = durable_;
    rotated.segment = base;
    persist(rotated);
    crash_point("rotate-cursor");
    durable_.segment = cursor_.segment = base;
    journal_.reset(next.release());
    segments_.push_back({base, durable_.log_bytes, durable_.seq, durable_.cut, durable_.last_tick_ts});
    log("info", "journal_segment_sealed", Json::object({{"next_segment", number(base)}, {"segments", number(segments_.size())}}));
}

// Expire the oldest sealed segments over the byte or venue-time budget. The open segment and every segment
// that can hold a message of the protected window (the open minute and the closed-minute proofs) stay.
void State::retain() {
    const auto protected_start = durable_.proofs.empty() ? durable_.cut : std::min(durable_.proofs.front().ts, durable_.cut);
    while (segments_.size() > 1) {
        const auto oldest = segments_[0];
        const auto& next = segments_[1];
        if (next.cut >= protected_start) break;
        // The segment holding the newest print stays: a reconnect re-reads the recent prints from the journal.
        if (tick_mode(config_.mode) && next.seq == durable_.seq && oldest.seq < durable_.seq) break;
        const bool bytes = durable_.log_bytes - oldest.bytes > config_.replay_bytes;
        const bool age = config_.replay_age_ms && durable_.cut - next.cut > config_.replay_age_ms;
        if (!bytes && !age) break;
        if (::unlink(segment_path(oldest.base).c_str()) != 0) throw Error(22, "cannot expire a journal segment");
        segments_.erase(segments_.begin());
        sync_directory(journal_dir());
        crash_point("retain-segment");
        if (::unlink(checkpoint_path(oldest.base).c_str()) != 0 && errno != ENOENT) throw Error(22, "cannot expire a journal checkpoint");
        sync_directory(journal_dir());
        log("info", "journal_segment_expired", Json::object({{"segment", number(oldest.base)},
            {"first_retained", number(segments_.front().base)}, {"reason", Json::string(bytes ? "bytes" : "age")}}));
    }
    const bool over = durable_.log_bytes - segments_.front().bytes > config_.replay_bytes;
    if (over && !over_budget_)
        log("warn", "replay_budget_held_by_protected_window", Json::object({{"retained_bytes",
            number(durable_.log_bytes - segments_.front().bytes)}, {"replay_bytes", number(config_.replay_bytes)}}));
    over_budget_ = over;
}

std::size_t State::segment_for(std::uint64_t index) const {
    const auto found = std::upper_bound(segments_.begin(), segments_.end(), index,
                                        [](std::uint64_t value, const Segment& segment) { return value < segment.base; });
    return found == segments_.begin() ? 0 : static_cast<std::size_t>(found - segments_.begin() - 1);
}

// Ticks of the first retained segment's open minute can sit in the expired segment before it: that minute
// is expired only when its checkpoint shows a tick of it.
bool State::expired_minute(std::int64_t minute) const {
    const auto& first = segments_.front();
    return first_retained() &&
        (minute < first.cut || (tick_mode(config_.mode) && minute == first.cut && first.last_tick_ts >= first.cut));
}

std::uint64_t State::index_for_minute(std::int64_t minute) const {
    std::size_t chosen = 0;
    for (std::size_t index = 0; index < segments_.size(); ++index)
        if (segments_[index].cut < minute) chosen = index;
    return segments_[chosen].base;
}

void State::visit(std::uint64_t from, const std::function<void(const std::string&)>& visitor) const {
    scan(from, [&](const std::string& line) { visitor(line); return true; });
}

void State::scan(std::uint64_t from, const std::function<bool(const std::string&)>& visitor) const {
    if (from > cursor_.message_index) throw Error(22, "replay cursor is unavailable");
    if (from < first_retained()) throw Error(22, "replay cursor has expired from the retained journal");
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(config_.max_replay_seconds);
    struct Enough {};
    std::uint64_t index = durable_.message_index;
    try {
        for (auto position = segment_for(from); position < segments_.size() && from < durable_.message_index; ++position) {
            const auto& segment = segments_[position];
            const bool last = position + 1 == segments_.size();
            const auto end_bytes = last ? durable_.log_bytes : segments_[position + 1].bytes;
            const auto end_index = last ? durable_.message_index : segments_[position + 1].base;
            index = segment.base;
            read_lines(segment_path(segment.base), end_bytes - segment.bytes, deadline, [&](const std::string& line) {
                if (index >= from && !visitor(line)) throw Enough{};
                ++index;
            });
            if (index != end_index) throw Error(21, "verified message partition changed");
        }
        index = durable_.message_index;
        for (const auto& staged : staged_) {
            if (index >= from && !visitor(staged)) return;
            ++index;
        }
    } catch (const Enough&) {}
}

std::optional<Trade> State::recent_trade(std::uint64_t id) const {
    for (const auto& trade : recent_trades_) if (trade.id == id) return trade;
    return std::nullopt;
}
std::optional<Trade> State::trade(std::uint64_t id) const {
    if (const auto recent = recent_trade(id)) return recent;
    std::size_t chosen = 0;
    for (std::size_t index = 0; index < segments_.size(); ++index)
        if (segments_[index].seq < id) chosen = index;
    std::optional<Trade> found;
    scan(segments_[chosen].base, [&](const std::string& line) {
        const auto event = parse_json(line);
        if (event.at("type").text() == "tick" && event.at("seq").integer<std::uint64_t>() == id) found = normalized_trade(event);
        return !found;
    });
    return found;
}
std::optional<Bar> State::proof(std::int64_t ts) const {
    for (const auto& bar : cursor_.proofs) if (bar.ts == ts) return bar;
    return std::nullopt;
}
std::optional<Bar> State::bar(std::int64_t ts) const {
    for (const auto& bar : recent_bars_) if (bar.ts == ts) return bar;
    if (const auto proven = proof(ts)) return proven;
    std::optional<Bar> found;
    scan(index_for_minute(ts), [&](const std::string& line) {
        const auto event = parse_json(line);
        if (event.at("type").text() == "bar" && event.at("bar").at("ts_open").integer<std::int64_t>() == ts) found = normalized_bar(event);
        return !found;
    });
    return found;
}
Json State::status() const {
    return Json::object({{"epoch", Json::string(durable_.epoch)}, {"message_index", number(durable_.message_index)},
        {"first_retained", number(first_retained())}, {"segment", number(durable_.segment)},
        {"emitted_seq", number(durable_.seq)}, {"verified_cut", signed_number(durable_.cut)},
        {"prefix_hash", Json::string(durable_.prefix_hash)}, {"cursor", Json::string(config_.state_dir + "/cursor.json")}});
}
}
