// SPDX-License-Identifier: Apache-2.0
#include "core.hpp"
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <algorithm>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <ctime>
#include <fcntl.h>
#include <filesystem>
#include <iomanip>
#include <limits>
#include <mutex>
#include <poll.h>
#include <sstream>
#include <unistd.h>

namespace pineforge::feed {
std::atomic<bool> stopping{false};
static_assert(std::atomic<bool>::is_always_lock_free);

void log(const std::string& level, const std::string& event, const Json& fields) {
    static std::mutex mutex;
    auto record = fields;
    record.members["level"] = Json::string(level);
    record.members["event"] = Json::string(event);
    record.members["ts"] = Json::number(std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count()));
    const auto bytes = record.dump() + '\n';
    std::lock_guard<std::mutex> guard(mutex);
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const auto written = ::write(STDERR_FILENO, bytes.data() + offset, bytes.size() - offset);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) break;
        offset += static_cast<std::size_t>(written);
    }
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

void Trade::validate() const {
    if (!id || ts < 0 || ts > std::numeric_limits<std::int64_t>::max() - 60000 || Decimal(price).zero() || Decimal(qty).zero())
        throw Error(23, "invalid positive venue trade");
}
std::string Trade::wire() const {
    validate();
    return "{\"type\":\"tick\",\"ts\":" + std::to_string(ts) + ",\"seq\":" + std::to_string(id) +
        ",\"price\":" + price + ",\"qty\":" + qty + "}";
}
bool Trade::operator==(const Trade& other) const {
    return id == other.id && ts == other.ts && price == other.price && qty == other.qty;
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
    return ts == other.ts && open == other.open && high == other.high && low == other.low && close == other.close && volume == other.volume;
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
void Aggregate::reconcile(const Kline& kline, bool require_ids) const {
    if (!kline.confirmed || !count || count != kline.count ||
        (require_ids && (kline.first <= 0 || kline.last < kline.first || first != static_cast<std::uint64_t>(kline.first) ||
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
Json string_field(const std::string& value) { return Json::string(value); }
}
