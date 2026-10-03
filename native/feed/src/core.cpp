// SPDX-License-Identifier: Apache-2.0
#include "core.hpp"
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <algorithm>
#include <cctype>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <climits>
#include <condition_variable>
#include <ctime>
#include <deque>
#include <fcntl.h>
#include <filesystem>
#include <iomanip>
#include <limits>
#include <mutex>
#include <poll.h>
#include <sstream>
#include <thread>
#include <unistd.h>

namespace pineforge::feed {
std::atomic<bool> stopping{false};
static_assert(std::atomic<bool>::is_always_lock_free);

namespace {
// Diagnostics go through one writer thread with ordinary blocking writes, so stderr's flags are never
// changed and no caller waits on a slow sink. A full queue drops whole records and counts them; a
// record is at most PIPE_BUF bytes, so a pipe write is atomic and never leaves half a line.
struct LogSink {
    std::mutex mutex;
    std::condition_variable ready, idle;
    std::deque<std::string> records;
    std::size_t bytes = 0;
    std::uint64_t dropped = 0;
    bool writing = false;
};
LogSink& sink() {
    static LogSink* const shared = [] {
        auto* created = new LogSink();  // never destroyed: the writer may still be blocked at exit
        std::thread([created] {
            std::unique_lock<std::mutex> guard(created->mutex);
            for (;;) {
                created->ready.wait(guard, [created] { return !created->records.empty(); });
                const auto record = std::move(created->records.front());
                created->records.pop_front();
                created->bytes -= record.size();
                created->writing = true;
                guard.unlock();
                std::size_t offset = 0;
                while (offset < record.size()) {
                    const auto written = ::write(STDERR_FILENO, record.data() + offset, record.size() - offset);
                    if (written < 0 && errno == EINTR) continue;
                    if (written <= 0) break;
                    offset += static_cast<std::size_t>(written);
                }
                guard.lock();
                created->writing = false;
                if (created->records.empty()) created->idle.notify_all();
            }
        }).detach();
        return created;
    }();
    return *shared;
}
}

void log(const std::string& level, const std::string& event, const Json& fields) {
    auto record = fields;
    record.members["level"] = Json::string(level);
    record.members["event"] = Json::string(event);
    record.members["ts"] = Json::number(std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count()));
    auto& shared = sink();
    std::lock_guard<std::mutex> guard(shared.mutex);
    if (shared.dropped) record.members["dropped_records"] = Json::number(std::to_string(shared.dropped));
    auto bytes = record.dump() + '\n';
    if (bytes.size() > PIPE_BUF)
        bytes = Json::object({{"level", record.members["level"]}, {"event", record.members["event"]},
            {"ts", record.members["ts"]}, {"truncated", Json::boolean(true)}}).dump() + '\n';
    if (bytes.size() > 1024 * 1024 - shared.bytes) { ++shared.dropped; return; }
    shared.dropped = 0;
    shared.bytes += bytes.size();
    shared.records.push_back(std::move(bytes));
    shared.ready.notify_one();
}

void flush_log(std::chrono::milliseconds limit) {
    auto& shared = sink();
    std::unique_lock<std::mutex> guard(shared.mutex);
    shared.idle.wait_for(guard, limit, [&] { return shared.records.empty() && !shared.writing; });
}

std::string sha256(std::string_view bytes) {
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int size = 0;
    if (EVP_Digest(bytes.data(), bytes.size(), digest, &size, EVP_sha256(), nullptr) != 1 || size != 32)
        throw Error(23, "SHA-256 failed");
    constexpr char digits[] = "0123456789abcdef";
    std::string result;
    for (unsigned int index = 0; index < size; ++index) {
        result += digits[digest[index] >> 4];
        result += digits[digest[index] & 15];
    }
    return result;
}

std::string random_epoch() {
    unsigned char bytes[32];
    if (RAND_bytes(bytes, sizeof(bytes)) != 1) throw Error(23, "stream epoch generation failed");
    return sha256(std::string_view(reinterpret_cast<const char*>(bytes), sizeof(bytes)));
}

std::int64_t timestamp(const std::string& token) {
    std::int64_t result = -1;
    const auto parsed = std::from_chars(token.data(), token.data() + token.size(), result);
    if (parsed.ec == std::errc{} && parsed.ptr == token.data() + token.size()) {
        if (result < 0 || result > std::numeric_limits<std::int64_t>::max() - 60000)
            throw Error(23, "timestamp outside supported range");
        return result;
    }
    if (token.size() != 20 || token.back() != 'Z') throw Error(23, "use UTC RFC3339 seconds or Unix milliseconds");
    std::tm broken{};
    std::istringstream input(token);
    input >> std::get_time(&broken, "%Y-%m-%dT%H:%M:%SZ");
    if (input.fail()) throw Error(23, "invalid UTC timestamp");
    const auto seconds = timegm(&broken);
    char canonical[32];
    std::tm checked{};
    gmtime_r(&seconds, &checked);
    std::strftime(canonical, sizeof(canonical), "%Y-%m-%dT%H:%M:%SZ", &checked);
    if (seconds < 0 || token != canonical) throw Error(23, "invalid UTC timestamp");
    return static_cast<std::int64_t>(seconds) * 1000;
}

void write_all(int descriptor, std::string_view bytes) {
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const auto written = ::write(descriptor, bytes.data() + offset, bytes.size() - offset);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) throw Error(22, "durable write failed");
        offset += static_cast<std::size_t>(written);
    }
}

void output_line(std::string_view line) {
    const auto bytes = std::string(line) + '\n';
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const auto written = ::write(STDOUT_FILENO, bytes.data() + offset, bytes.size() - offset);
        if (written < 0 && errno == EINTR) continue;
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (stopping && std::chrono::steady_clock::now() >= deadline) throw Stopped{};
            pollfd descriptor{STDOUT_FILENO, POLLOUT, 0};
            ::poll(&descriptor, 1, 100);
            continue;
        }
        if (written <= 0) throw Error(22, "stdout unavailable; replay from the consumer's committed message index");
        offset += static_cast<std::size_t>(written);
    }
}

void atomic_file(const std::string& path, std::string_view bytes) {
    const auto temporary = path + ".tmp";
    const int descriptor = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (descriptor < 0) throw Error(22, "cannot open atomic state file");
    try {
        write_all(descriptor, bytes);
        if (::fsync(descriptor) != 0) throw Error(22, "cannot sync atomic state file");
        if (::close(descriptor) != 0) throw Error(22, "cannot close atomic state file");
    } catch (...) {
        ::close(descriptor);
        throw;
    }
    if (::rename(temporary.c_str(), path.c_str()) != 0) throw Error(22, "cannot replace atomic state file");
    auto parent = std::filesystem::path(path).parent_path();
    if (parent.empty()) parent = ".";
    const int directory = ::open(parent.c_str(), O_RDONLY | O_CLOEXEC);
    if (directory < 0) throw Error(22, "cannot open state directory for sync");
    const auto status = ::fsync(directory);
    ::close(directory);
    if (status != 0) throw Error(22, "cannot sync state directory");
}

Decimal::Decimal(std::string coefficient, std::size_t scale)
    : coefficient_(std::move(coefficient)), scale_(scale) {
    const auto first = coefficient_.find_first_not_of('0');
    coefficient_ = first == std::string::npos ? "0" : coefficient_.substr(first);
    while (scale_ && coefficient_.size() > 1 && coefficient_.back() == '0') {
        coefficient_.pop_back();
        --scale_;
    }
    if (coefficient_ == "0") scale_ = 0;
    if (coefficient_.size() > 128 || scale_ > 32) throw Error(23, "decimal arithmetic exceeds bounded precision");
}

Decimal::Decimal(std::string_view token) : Decimal("0", 0) {
    if (token.empty() || token.size() > 96) throw Error(23, "invalid decimal token length");
    const auto dot = token.find('.');
    const auto integral = dot == std::string_view::npos ? token.size() : dot;
    if (!integral || (integral > 1 && token.front() == '0') || (dot != std::string_view::npos && dot + 1 == token.size()))
        throw Error(23, "invalid decimal token grammar");
    std::string coefficient;
    for (std::size_t index = 0; index < token.size(); ++index) {
        if (index == dot) continue;
        if (token[index] < '0' || token[index] > '9') throw Error(23, "decimal must be an unsigned fixed-point token");
        coefficient += token[index];
    }
    *this = Decimal(coefficient, dot == std::string_view::npos ? 0 : token.size() - dot - 1);
}

int Decimal::compare(const Decimal& other) const {
    if (zero() || other.zero()) return zero() == other.zero() ? 0 : zero() ? -1 : 1;
    const auto scale = std::max(scale_, other.scale_);
    const auto left = coefficient_ + std::string(scale - scale_, '0');
    const auto right = other.coefficient_ + std::string(scale - other.scale_, '0');
    if (left.size() != right.size()) return left.size() < right.size() ? -1 : 1;
    return left == right ? 0 : left < right ? -1 : 1;
}

Decimal Decimal::add(const Decimal& other) const {
    const auto scale = std::max(scale_, other.scale_);
    auto left = coefficient_ + std::string(scale - scale_, '0');
    auto right = other.coefficient_ + std::string(scale - other.scale_, '0');
    const auto width = std::max(left.size(), right.size());
    left.insert(0, width - left.size(), '0');
    right.insert(0, width - right.size(), '0');
    std::string sum(width, '0');
    int carry = 0;
    for (std::size_t index = width; index > 0; --index) {
        const int digit = left[index - 1] - '0' + right[index - 1] - '0' + carry;
        sum[index - 1] = static_cast<char>('0' + digit % 10);
        carry = digit / 10;
    }
    if (carry) sum.insert(sum.begin(), '1');
    return Decimal(sum, scale);
}

Decimal Decimal::multiply(const Decimal& other) const {
    if (zero() || other.zero()) return Decimal("0", 0);
    // The constructor bounds the product: at most 128 coefficient digits and 32 fractional digits.
    std::string product(coefficient_.size() + other.coefficient_.size(), '0');
    for (std::size_t left = coefficient_.size(); left > 0; --left) {
        int carry = 0;
        for (std::size_t right = other.coefficient_.size(); right > 0; --right) {
            auto& digit = product[left + right - 1];
            const int value = (digit - '0') + (coefficient_[left - 1] - '0') * (other.coefficient_[right - 1] - '0') + carry;
            digit = static_cast<char>('0' + value % 10);
            carry = value / 10;
        }
        product[left - 1] = static_cast<char>(product[left - 1] + carry);
    }
    return Decimal(product, scale_ + other.scale_);
}

std::string Decimal::str() const {
    if (!scale_) return coefficient_;
    auto digits = coefficient_;
    if (digits.size() <= scale_) digits.insert(0, scale_ - digits.size() + 1, '0');
    digits.insert(digits.size() - scale_, 1, '.');
    return digits;
}

std::string scaled_quantity(const std::string& token, const std::string& multiplier) {
    const Decimal amount(token);
    if (multiplier == "1") return token;
    return amount.multiply(Decimal(multiplier)).str();
}

std::string json_member(const std::string& document, const std::string& name) {
    std::size_t index = 0;
    const auto space = [&] { while (index < document.size() && std::isspace(static_cast<unsigned char>(document[index]))) ++index; };
    const auto string_end = [&](std::size_t start) {
        for (auto cursor = start + 1; cursor < document.size(); ++cursor) {
            if (document[cursor] == '\\') ++cursor;
            else if (document[cursor] == '"') return cursor + 1;
        }
        throw Error(23, "unterminated JSON string");
    };
    const auto value_end = [&](std::size_t start) {
        if (start >= document.size()) throw Error(23, "truncated JSON member");
        if (document[start] == '"') return string_end(start);
        std::size_t depth = 0, cursor = start;
        while (cursor < document.size()) {
            const char letter = document[cursor];
            if (letter == '"') { cursor = string_end(cursor); continue; }
            if (letter == '{' || letter == '[') ++depth;
            else if (letter == '}' || letter == ']') {
                if (!depth) return cursor;
                if (!--depth) return cursor + 1;
            } else if (letter == ',' && !depth) return cursor;
            ++cursor;
        }
        throw Error(23, "truncated JSON member");
    };
    space();
    if (index >= document.size() || document[index] != '{') throw Error(23, "expected a JSON object document");
    ++index;
    for (;;) {
        space();
        if (index >= document.size() || document[index] != '"') throw Error(23, "JSON member not found: " + name);
        const auto key_end = string_end(index);
        const auto key = document.substr(index + 1, key_end - index - 2);
        index = key_end;
        space();
        if (index >= document.size() || document[index] != ':') throw Error(23, "invalid JSON object member");
        ++index;
        space();
        const auto start = index;
        index = value_end(start);
        if (key == name) return document.substr(start, index - start);
        space();
        if (index >= document.size() || document[index] != ',') throw Error(23, "JSON member not found: " + name);
        ++index;
    }
}

void Trade::validate() const {
    if (!id || ts < 0 || ts > std::numeric_limits<std::int64_t>::max() - 60000 || Decimal(price).zero() || Decimal(qty).zero())
        throw Error(23, "invalid positive venue trade");
}
std::string Trade::wire() const {
    validate();
    return "{\"type\":\"tick\",\"ts\":" + std::to_string(ts) + ",\"seq\":" + std::to_string(id) +
        ",\"price\":" + price + ",\"qty\":" + qty + "}";
}
// Exact decimal values, not lexemes: a venue may render one number as "100.0" on its WebSocket and as
// "100" over REST. Emitted tokens stay verbatim; only the comparison is by value.
bool Trade::operator==(const Trade& other) const {
    return id == other.id && ts == other.ts && Decimal(price) == Decimal(other.price) && Decimal(qty) == Decimal(other.qty);
}
void Bar::validate() const {
    if (ts < 0 || ts % 60000 || ts > std::numeric_limits<std::int64_t>::max() - 60000)
        throw Error(23, "invalid minute timestamp");
    const Decimal opening(open), highest(high), lowest(low), closing(close), amount(volume);
    if (opening.zero() || highest.zero() || lowest.zero() || closing.zero() || highest.compare(lowest) < 0 ||
        highest.compare(opening) < 0 || highest.compare(closing) < 0 || lowest.compare(opening) > 0 || lowest.compare(closing) > 0)
        throw Error(23, "invalid OHLCV envelope");
    (void)amount;
}
std::string Bar::wire() const {
    validate();
    return "{\"type\":\"bar\",\"bar\":{\"ts_open\":" + std::to_string(ts) + ",\"o\":" + open +
        ",\"h\":" + high + ",\"l\":" + low + ",\"c\":" + close + ",\"v\":" + volume + "}}";
}
bool Bar::operator==(const Bar& other) const {
    return ts == other.ts && Decimal(open) == Decimal(other.open) && Decimal(high) == Decimal(other.high) &&
        Decimal(low) == Decimal(other.low) && Decimal(close) == Decimal(other.close) && Decimal(volume) == Decimal(other.volume);
}
void Aggregate::add(const Trade& trade) {
    trade.validate();
    if (count && (last == std::numeric_limits<std::uint64_t>::max() || trade.id != last + 1 || trade.ts < last_ts))
        throw Error(20, "noncontiguous minute trade prefix");
    if (!count) {
        first = trade.id;
        open = high = low = trade.price;
    }
    if (Decimal(trade.price).compare(Decimal(high)) > 0) high = trade.price;
    if (Decimal(trade.price).compare(Decimal(low)) < 0) low = trade.price;
    close = trade.price;
    volume = volume.add(Decimal(trade.qty));
    last = trade.id;
    last_ts = trade.ts;
    ++count;
}
// A WebSocket proof (require_ids) needs the kline's own x=true and venue f..L range. A REST row carries
// neither: its caller proves closure with a later x=true watermark and a next-minute fence print.
void Aggregate::reconcile(const Kline& kline, bool require_ids) const {
    if (!count || count != kline.count ||
        (require_ids && (!kline.confirmed || kline.first <= 0 || kline.last < kline.first ||
                        first != static_cast<std::uint64_t>(kline.first) ||
                        last != static_cast<std::uint64_t>(kline.last) || last - first + 1 != count)))
        throw Error(20, "closed-minute trade coverage is not proven");
    if (!(Decimal(open) == Decimal(kline.bar.open)) || !(Decimal(high) == Decimal(kline.bar.high)) ||
        !(Decimal(low) == Decimal(kline.bar.low)) || !(Decimal(close) == Decimal(kline.bar.close)) ||
        !(volume == Decimal(kline.bar.volume))) throw Error(21, "closed-minute exact OHLCV reconciliation failed");
}
Trade normalized_trade(const Json& event) {
    Trade trade{event.at("seq").integer<std::uint64_t>(), event.at("ts").integer<std::int64_t>(), event.at("price").value, event.at("qty").value};
    trade.validate();
    return trade;
}
Bar normalized_bar(const Json& event) {
    const auto& value = event.at("bar");
    Bar bar{value.at("ts_open").integer<std::int64_t>(), value.at("o").value, value.at("h").value,
            value.at("l").value, value.at("c").value, value.at("v").value};
    bar.validate();
    return bar;
}
}
