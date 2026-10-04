// SPDX-License-Identifier: Apache-2.0
#include "export.hpp"
#include "venue.hpp"
#include <openssl/evp.h>
#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <chrono>
#include <exception>
#include <filesystem>
#include <fstream>
#include <limits>
#include <vector>

namespace pineforge::feed {
namespace {
constexpr std::size_t csv_limit = 64 * 1024 * 1024;
constexpr std::string_view archive_header = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker";
constexpr const char* bar_rule = "a print belongs to minute floor(ts/60000)*60000; open=first price, high=max, low=min, close=last, "
    "volume=exact sum of quantities; a minute without a print has open=high=low=close=the previous close (the "
    "predecessor's price before the first print) and volume 0";
Json number(std::uint64_t value) { return Json::number(std::to_string(value)); }
Json signed_number(std::int64_t value) { return Json::number(std::to_string(value)); }
std::string base_name(const std::string& path) { return std::filesystem::path(path).filename().string(); }
std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
}
template<class Integer> Integer digits(std::string_view text) {
    Integer result{};
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    if (text.empty() || text.find_first_not_of("0123456789") != std::string_view::npos || parsed.ec != std::errc{} ||
        parsed.ptr != text.data() + text.size())
        throw Error(23, "archive row has an invalid unsigned integer");
    return result;
}
std::uint32_t little(const std::string& bytes, std::size_t offset, std::size_t width) {
    if (offset + width > bytes.size()) throw Error(21, "zip structure is truncated");
    std::uint32_t value = 0;
    for (std::size_t index = width; index > 0; --index) value = (value << 8) | static_cast<unsigned char>(bytes[offset + index - 1]);
    return value;
}
void read_at(std::ifstream& file, std::uint64_t offset, std::string& bytes) {
    file.clear();
    file.seekg(static_cast<std::streamoff>(offset));
    file.read(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    if (static_cast<std::size_t>(file.gcount()) != bytes.size()) throw Error(22, "cannot read the archive");
}

// Huffman lookup by the next 15 input bits (deflate sends codes least-significant bit first): symbol << 4 | length,
// 0 for a code the table does not assign.
using Table = std::vector<std::uint32_t>;
void build(Table& table, const unsigned char* lengths, std::size_t count) {
    std::array<unsigned int, 16> counts{}, next{};
    for (std::size_t symbol = 0; symbol < count; ++symbol) ++counts[lengths[symbol]];
    counts[0] = 0;
    int left = 1;
    for (unsigned int length = 1; length < 16; ++length) {
        left = (left << 1) - static_cast<int>(counts[length]);
        if (left < 0) throw Error(21, "deflate Huffman code is over-subscribed");
    }
    for (unsigned int length = 1, code = 0; length < 16; ++length) {
        code = (code + counts[length - 1]) << 1;
        next[length] = code;
    }
    table.assign(1U << 15, 0);
    for (std::size_t symbol = 0; symbol < count; ++symbol) {
        const unsigned int length = lengths[symbol];
        if (!length) continue;
        unsigned int code = next[length]++, reversed = 0;
        for (unsigned int bit = 0; bit < length; ++bit, code >>= 1) reversed = (reversed << 1) | (code & 1);
        for (auto index = reversed; index < table.size(); index += 1U << length)
            table[index] = (static_cast<std::uint32_t>(symbol) << 4) | length;
    }
}

class Inflater {
    const std::function<std::size_t(char*, std::size_t)>& source_;
    const std::function<void(std::string_view)>& sink_;
    std::vector<char> input_ = std::vector<char>(1 << 16);
    std::size_t position_ = 0, available_ = 0;
    bool exhausted_ = false;
    std::uint64_t bits_ = 0;
    unsigned int count_ = 0, padding_ = 0;  // padding_: zero bytes appended beyond the end of the input
    std::string window_;  // output not yet handed on, plus the last 32 KiB for back-references
    static constexpr std::size_t flush_at = 1 << 20;

    bool byte(unsigned char& value) {
        if (position_ == available_) {
            if (exhausted_) return false;
            available_ = source_(input_.data(), input_.size());
            position_ = 0;
            if (!available_) { exhausted_ = true; return false; }
        }
        value = static_cast<unsigned char>(input_[position_++]);
        return true;
    }
    void need(unsigned int wanted) {
        while (count_ < wanted) {
            unsigned char value = 0;
            if (!byte(value)) ++padding_;
            bits_ |= static_cast<std::uint64_t>(value) << count_;
            count_ += 8;
        }
    }
    void drop(unsigned int used) {
        bits_ >>= used;
        count_ -= used;
        if (count_ < 8 * padding_) throw Error(21, "deflate stream is truncated");
    }
    std::uint32_t bits(unsigned int wanted) {
        if (!wanted) return 0;
        need(wanted);
        const auto value = static_cast<std::uint32_t>(bits_ & ((1ULL << wanted) - 1));
        drop(wanted);
        return value;
    }
    unsigned int decode(const Table& table) {
        need(15);
        const auto entry = table[bits_ & 0x7FFF];
        if (!(entry & 15)) throw Error(21, "deflate stream has an unassigned Huffman code");
        drop(entry & 15);
        return entry >> 4;
    }
    void emit(std::size_t keep) {
        keep = std::min(keep, window_.size());
        if (window_.size() > keep) sink_(std::string_view(window_.data(), window_.size() - keep));
        window_.erase(0, window_.size() - keep);
    }
    void put(char value) {
        window_.push_back(value);
        if (window_.size() >= flush_at) emit(32768);
    }
    void stored() {
        drop(count_ % 8);
        const auto length = bits(16), complement = bits(16);
        if (length != (~complement & 0xFFFF)) throw Error(21, "deflate stored block length is corrupt");
        for (std::uint32_t index = 0; index < length; ++index) put(static_cast<char>(bits(8)));
    }
    void codes(const Table& literal, const Table& distance) {
        static constexpr std::uint16_t base[29] = {3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59,
                                                   67, 83, 99, 115, 131, 163, 195, 227, 258};
        static constexpr std::uint8_t extra[29] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0};
        static constexpr std::uint16_t offset[30] = {1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513,
                                                     769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577};
        static constexpr std::uint8_t spread[30] = {0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10,
                                                    11, 11, 12, 12, 13, 13};
        for (;;) {
            const auto symbol = decode(literal);
            if (symbol < 256) { put(static_cast<char>(symbol)); continue; }
            if (symbol == 256) return;
            if (symbol > 285) throw Error(21, "deflate stream has an invalid length code");
            const auto length = base[symbol - 257] + bits(extra[symbol - 257]);
            const auto code = decode(distance);
            if (code > 29) throw Error(21, "deflate stream has an invalid distance code");
            const auto back = offset[code] + bits(spread[code]);
            if (back > window_.size()) throw Error(21, "deflate distance reaches before the start of the stream");
            for (std::uint32_t copied = 0; copied < length; ++copied) window_.push_back(window_[window_.size() - back]);
            if (window_.size() >= flush_at) emit(32768);
        }
    }
    void dynamic(Table& literal, Table& distance, Table& lengths_code) {
        static constexpr unsigned char order[19] = {16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15};
        const auto literals = bits(5) + 257, distances = bits(5) + 1, header = bits(4) + 4;
        if (literals > 286 || distances > 30) throw Error(21, "deflate dynamic block header is corrupt");
        unsigned char lengths[320] = {};
        for (unsigned int index = 0; index < header; ++index) lengths[order[index]] = static_cast<unsigned char>(bits(3));
        build(lengths_code, lengths, 19);
        std::fill(std::begin(lengths), std::end(lengths), 0);
        for (unsigned int index = 0; index < literals + distances;) {
            const auto symbol = decode(lengths_code);
            if (symbol < 16) { lengths[index++] = static_cast<unsigned char>(symbol); continue; }
            unsigned char value = 0;
            unsigned int repeat = 0;
            if (symbol == 16) {
                if (!index) throw Error(21, "deflate code lengths repeat before the first length");
                value = lengths[index - 1];
                repeat = 3 + bits(2);
            } else repeat = symbol == 17 ? 3 + bits(3) : 11 + bits(7);
            if (index + repeat > literals + distances) throw Error(21, "deflate code lengths overrun the header counts");
            while (repeat--) lengths[index++] = value;
        }
        if (!lengths[256]) throw Error(21, "deflate dynamic block has no end-of-block code");
        build(literal, lengths, literals);
        build(distance, lengths + literals, distances);
    }
public:
    Inflater(const std::function<std::size_t(char*, std::size_t)>& source, const std::function<void(std::string_view)>& sink)
        : source_(source), sink_(sink) { window_.reserve(flush_at + 512); }
    void run() {
        static const auto fixed = [] {
            unsigned char lengths[288 + 30];
            std::fill(lengths, lengths + 144, 8);
            std::fill(lengths + 144, lengths + 256, 9);
            std::fill(lengths + 256, lengths + 280, 7);
            std::fill(lengths + 280, lengths + 288, 8);
            std::fill(lengths + 288, lengths + 318, 5);
            std::pair<Table, Table> tables;
            build(tables.first, lengths, 288);
            build(tables.second, lengths + 288, 30);
            return tables;
        }();
        Table literal, distance, lengths_code;
        for (bool last = false; !last;) {
            last = bits(1) != 0;
            const auto type = bits(2);
            if (type == 0) stored();
            else if (type == 1) codes(fixed.first, fixed.second);
            else if (type == 2) {
                dynamic(literal, distance, lengths_code);
                codes(literal, distance);
            } else throw Error(21, "deflate stream has an invalid block type");
        }
        unsigned char spare = 0;
        if (count_ - 8 * padding_ >= 8 || byte(spare)) throw Error(21, "bytes follow the final deflate block");
        emit(0);
    }
};
}

ChainBars::ChainBars(std::int64_t start, std::int64_t end) : start_(start), end_(end), next_(start),
    csv_("timestamp,open,high,low,close,volume\n") {
    if (start < 0 || end <= start || start % 60000 || end % 60000 || end > std::numeric_limits<std::int64_t>::max() - 60000)
        throw Error(23, "export requires a nonempty minute-aligned exclusive range");
    if ((end - start) / 60000 > 100000) throw Error(22, "export range exceeds 100000 minutes");
}
void ChainBars::anchor(const Trade& predecessor) {
    predecessor.validate();
    if (predecessor_) throw Error(23, "the print chain is already anchored");
    if (predecessor.ts >= start_) throw Error(20, "the predecessor print is not strictly before the start");
    predecessor_ = predecessor;
    last_id_ = predecessor.id;
    last_ts_ = predecessor.ts;
    close_ = predecessor.price;
}
bool ChainBars::add(const Trade& trade) {
    if (!predecessor_ || fence_) throw Error(23, "a print chain takes its predecessor first and nothing after its fence");
    trade.validate();
    if (last_id_ == std::numeric_limits<std::uint64_t>::max() || trade.id != last_id_ + 1)
        throw Error(20, "the print chain has a hole after ID " + std::to_string(last_id_) + " (next print is " + std::to_string(trade.id) + ")");
    if (trade.ts < last_ts_) throw Error(23, "print time goes backwards along the ID chain at ID " + std::to_string(trade.id));
    if (trade.ts < start_) throw Error(20, "a print before the start follows the predecessor: the predecessor is not the last one");
    last_id_ = trade.id;
    last_ts_ = trade.ts;
    if (trade.ts >= end_) {
        flush();
        carry(end_);
        fence_ = trade;
        return true;
    }
    const auto minute = trade.ts - trade.ts % 60000;
    if (minute != minute_) {
        flush();
        carry(minute);
        minute_ = minute;
    }
    const Decimal price(trade.price);
    if (!count_) {
        open_ = high_ = low_ = trade.price;
        high_value_ = low_value_ = price;
        volume_ = Decimal("0");
    } else {
        if (price.compare(high_value_) > 0) { high_ = trade.price; high_value_ = price; }
        if (price.compare(low_value_) < 0) { low_ = trade.price; low_value_ = price; }
    }
    close_ = trade.price;
    volume_ = volume_.add(Decimal(trade.qty));
    ++count_;
    ++prints_;
    return false;
}
void ChainBars::flush() {
    if (!count_) return;
    row(minute_, open_, high_, low_, close_, volume_.str());
    count_ = 0;
    next_ = minute_ + 60000;
}
void ChainBars::carry(std::int64_t until) {
    for (; next_ < until; next_ += 60000, ++quiet_) row(next_, close_, close_, close_, close_, "0");
}
void ChainBars::row(std::int64_t minute, const std::string& open, const std::string& high, const std::string& low,
                    const std::string& close, const std::string& volume) {
    csv_ += std::to_string(minute) + ',' + open + ',' + high + ',' + low + ',' + close + ',' + volume + '\n';
    ++bars_;
    if (csv_.size() > csv_limit) throw Error(22, "export output exceeds 64 MiB");
}
const std::string& ChainBars::csv() const {
    if (!fence_) throw Error(20, "the window's fence (the first print at or after its end) is not proven");
    return csv_;
}
const Trade& ChainBars::predecessor() const {
    if (!predecessor_) throw Error(20, "the window's predecessor print is not proven");
    return *predecessor_;
}
const Trade& ChainBars::fence() const {
    if (!fence_) throw Error(20, "the window's fence (the first print at or after its end) is not proven");
    return *fence_;
}

std::optional<Trade> archive_row(std::string_view line, bool first_line) {
    if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
    if (first_line && !line.empty() && (line.front() < '0' || line.front() > '9')) {
        if (line != archive_header) throw Error(23, "unexpected archive header; expected " + std::string(archive_header));
        return std::nullopt;
    }
    std::array<std::string_view, 7> fields;
    std::size_t count = 0;
    for (std::size_t begin = 0;;) {
        const auto comma = line.find(',', begin);
        if (count == fields.size()) throw Error(23, "archive row must have seven fields");
        fields[count++] = line.substr(begin, comma == std::string_view::npos ? std::string_view::npos : comma - begin);
        if (comma == std::string_view::npos) break;
        begin = comma + 1;
    }
    if (count != fields.size()) throw Error(23, "archive row must have seven fields");
    const auto id = digits<std::uint64_t>(fields[0]), first = digits<std::uint64_t>(fields[3]), last = digits<std::uint64_t>(fields[4]);
    auto time = digits<std::int64_t>(fields[5]);
    if (!first || last < first) throw Error(23, "invalid aggregate raw-trade range");
    // Binance moved its archives' transact_time to microseconds; no millisecond time reaches 10^14 before 5138.
    if (time > 100000000000000LL) time /= 1000;
    if (fields[6] != "true" && fields[6] != "false" && fields[6] != "True" && fields[6] != "False")
        throw Error(23, "archive is_buyer_maker must be true or false");
    Trade trade{id, time, std::string(fields[1]), std::string(fields[2])};
    trade.validate();
    return trade;
}

std::uint32_t crc32(std::uint32_t crc, std::string_view bytes) {
    static const auto table = [] {
        std::array<std::uint32_t, 256> values{};
        for (std::uint32_t index = 0; index < 256; ++index) {
            auto value = index;
            for (int bit = 0; bit < 8; ++bit) value = (value & 1) ? 0xEDB88320U ^ (value >> 1) : value >> 1;
            values[index] = value;
        }
        return values;
    }();
    crc = ~crc;
    for (const auto letter : bytes) crc = table[(crc ^ static_cast<unsigned char>(letter)) & 0xFF] ^ (crc >> 8);
    return ~crc;
}

void inflate(const std::function<std::size_t(char*, std::size_t)>& source, const std::function<void(std::string_view)>& sink) {
    Inflater(source, sink).run();
}

std::string unzip(const std::string& path, const std::function<void(std::string_view)>& sink) {
    std::ifstream file(path, std::ios::binary);
    if (!file) throw Error(23, "cannot open the archive " + path);
    file.seekg(0, std::ios::end);
    const auto end = file.tellg();
    if (end < 0) throw Error(22, "cannot size the archive");
    const auto size = static_cast<std::uint64_t>(end);
    // The end-of-central-directory record: 22 bytes, then a comment of at most 65535 bytes.
    std::string tail(static_cast<std::size_t>(std::min<std::uint64_t>(size, 22 + 65535)), '\0');
    read_at(file, size - tail.size(), tail);
    std::size_t record = std::string::npos;
    for (auto index = tail.size() >= 22 ? tail.size() - 21 : 0; index-- > 0;)
        if (little(tail, index, 4) == 0x06054b50 && index + 22 + little(tail, index + 20, 2) == tail.size()) { record = index; break; }
    if (record == std::string::npos) throw Error(23, "the archive is not a zip file (no end-of-central-directory record)");
    if (little(tail, record + 4, 2) || little(tail, record + 6, 2) || little(tail, record + 8, 2) != 1 || little(tail, record + 10, 2) != 1)
        throw Error(23, "the zip archive must be a single-disk archive holding exactly one entry");
    const std::uint64_t directory_size = little(tail, record + 12, 4), directory_offset = little(tail, record + 16, 4);
    if (directory_size == 0xFFFFFFFF || directory_offset == 0xFFFFFFFF)
        throw Error(23, "ZIP64 archives are not supported; pass the extracted .csv");
    if (directory_offset + directory_size > size - tail.size() + record) throw Error(21, "zip central directory is out of bounds");
    std::string directory(static_cast<std::size_t>(directory_size), '\0');
    read_at(file, directory_offset, directory);
    if (little(directory, 0, 4) != 0x02014b50) throw Error(21, "zip central directory entry is corrupt");
    const auto flags = little(directory, 8, 2), method = little(directory, 10, 2), crc = little(directory, 16, 4);
    const std::uint64_t compressed = little(directory, 20, 4), uncompressed = little(directory, 24, 4), local = little(directory, 42, 4);
    const auto name_size = little(directory, 28, 2);
    if (46 + name_size + little(directory, 30, 2) + little(directory, 32, 2) != directory.size())
        throw Error(21, "zip central directory entry does not fill the directory");
    const auto name = directory.substr(46, name_size);
    if (flags & 1) throw Error(23, "encrypted zip entries are not supported");
    if (method != 0 && method != 8) throw Error(23, "the zip entry must be stored or deflated");
    if (compressed == 0xFFFFFFFF || uncompressed == 0xFFFFFFFF || local == 0xFFFFFFFF)
        throw Error(23, "ZIP64 archives are not supported; pass the extracted .csv");
    if (local + 30 > directory_offset) throw Error(21, "zip local header is out of bounds");
    std::string header(30, '\0');
    read_at(file, local, header);
    if (little(header, 0, 4) != 0x04034b50 || little(header, 8, 2) != method) throw Error(21, "zip local header is corrupt");
    const auto data = local + 30 + little(header, 26, 2) + little(header, 28, 2);
    if (data + compressed > directory_offset) throw Error(21, "zip entry data is out of bounds");
    if (method == 0 && compressed != uncompressed) throw Error(21, "stored zip entry sizes disagree");
    file.clear();
    file.seekg(static_cast<std::streamoff>(data));
    std::uint64_t remaining = compressed, produced = 0;
    std::uint32_t check = 0;
    const std::function<std::size_t(char*, std::size_t)> source = [&](char* buffer, std::size_t limit) -> std::size_t {
        const auto wanted = static_cast<std::size_t>(std::min<std::uint64_t>(remaining, limit));
        if (!wanted) return 0;
        file.read(buffer, static_cast<std::streamsize>(wanted));
        if (static_cast<std::size_t>(file.gcount()) != wanted) throw Error(22, "cannot read the zip entry");
        remaining -= wanted;
        return wanted;
    };
    const std::function<void(std::string_view)> output = [&](std::string_view bytes) {
        produced += bytes.size();
        if (produced > uncompressed) throw Error(21, "zip entry inflates beyond its recorded size");
        check = crc32(check, bytes);
        sink(bytes);
    };
    if (method == 8) inflate(source, output);
    else {
        std::vector<char> buffer(1 << 20);
        while (const auto got = source(buffer.data(), buffer.size())) output(std::string_view(buffer.data(), got));
    }
    if (produced != uncompressed || check != crc) throw Error(21, "zip entry CRC-32 or size mismatch: the archive is corrupt");
    return name;
}

std::string file_sha256(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) throw Error(23, "cannot open " + path);
    struct Digest {
        EVP_MD_CTX* context = EVP_MD_CTX_new();
        ~Digest() { EVP_MD_CTX_free(context); }
    } digest;
    if (!digest.context || EVP_DigestInit_ex(digest.context, EVP_sha256(), nullptr) != 1) throw Error(23, "SHA-256 failed");
    std::vector<char> buffer(1 << 20);
    while (file) {
        file.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto got = static_cast<std::size_t>(file.gcount());
        if (got && EVP_DigestUpdate(digest.context, buffer.data(), got) != 1) throw Error(23, "SHA-256 failed");
    }
    if (file.bad()) throw Error(22, "cannot read " + path);
    unsigned char value[EVP_MAX_MD_SIZE];
    unsigned int size = 0;
    if (EVP_DigestFinal_ex(digest.context, value, &size) != 1 || size != 32) throw Error(23, "SHA-256 failed");
    constexpr char hex[] = "0123456789abcdef";
    std::string result;
    for (unsigned int index = 0; index < size; ++index) {
        result += hex[value[index] >> 4];
        result += hex[value[index] & 15];
    }
    return result;
}

void verify_checksum(const std::string& checksum, const std::string& archive, const std::string& digest) {
    std::ifstream file(checksum, std::ios::binary);
    if (!file) throw Error(23, "cannot open the checksum file " + checksum);
    std::string text(4097, '\0');
    file.read(text.data(), static_cast<std::streamsize>(text.size()));
    text.resize(static_cast<std::size_t>(file.gcount()));
    if (text.size() > 4096) throw Error(23, "the checksum file exceeds 4096 bytes");
    while (!text.empty() && (text.back() == '\n' || text.back() == '\r')) text.pop_back();
    if (text.size() < 66 || text.find('\n') != std::string::npos || text[64] != ' ' ||
        text.find_first_not_of("0123456789abcdefABCDEF") != 64)
        throw Error(23, "the checksum file must hold one line `<sha256-hex>  <file name>`");
    auto hash = text.substr(0, 64);
    std::transform(hash.begin(), hash.end(), hash.begin(), [](unsigned char letter) { return static_cast<char>(std::tolower(letter)); });
    const auto name = text.substr(text[65] == ' ' || text[65] == '*' ? 66 : 65);
    if (name != base_name(archive)) throw Error(21, "the checksum file names " + name + ", not the archive " + base_name(archive));
    if (hash != digest) throw Error(21, "the archive's SHA-256 does not match its checksum file");
}

ChainBars archive_bars(const std::string& archive, std::int64_t start, std::int64_t end) {
    ChainBars chain(start, end);
    std::optional<Trade> candidate;
    std::exception_ptr failure;
    std::string pending;
    bool first = true;
    const auto line = [&](std::string_view text) {
        const auto row = archive_row(text, first);
        first = false;
        if (!row) return;
        if (!chain.anchored()) {
            if (row->ts < start) { candidate = *row; return; }
            if (!candidate) throw Error(20, "the archive holds no print before the start: the window's predecessor is not inside it");
            chain.anchor(*candidate);
        }
        chain.add(*row);
    };
    // Rows stop counting at the fence or the first failure; a zip still inflates to its end so that its CRC-32 is
    // checked first: a corrupt archive stops 21, not with whatever its damaged rows would have caused.
    const std::function<void(std::string_view)> bytes = [&](std::string_view chunk) {
        if (failure || chain.fenced()) return;
        try {
            while (!chunk.empty() && !chain.fenced()) {
                const auto newline = chunk.find('\n');
                if (newline == std::string_view::npos) {
                    if (pending.size() + chunk.size() > 4096) throw Error(23, "archive line exceeds 4096 bytes");
                    pending.append(chunk);
                    return;
                }
                if (pending.empty()) line(chunk.substr(0, newline));
                else {
                    pending.append(chunk.substr(0, newline));
                    line(pending);
                    pending.clear();
                }
                chunk.remove_prefix(newline + 1);
            }
        } catch (const Error&) { failure = std::current_exception(); }
    };
    const auto name = base_name(archive);
    const bool zipped = name.size() > 4 && (name.compare(name.size() - 4, 4, ".zip") == 0 || name.compare(name.size() - 4, 4, ".ZIP") == 0);
    if (zipped) {
        const auto entry = unzip(archive, bytes);
        log("info", "export_archive_entry_verified", Json::object({{"entry", Json::string(entry)}}));
    } else {
        std::ifstream file(archive, std::ios::binary);
        if (!file) throw Error(23, "cannot open the archive " + archive);
        std::vector<char> buffer(1 << 20);
        while (file && !failure && !chain.fenced()) {
            file.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
            bytes(std::string_view(buffer.data(), static_cast<std::size_t>(file.gcount())));
        }
        if (file.bad()) throw Error(22, "cannot read the archive " + archive);
    }
    if (!failure && !chain.fenced() && !pending.empty()) {
        try { line(pending); } catch (const Error&) { failure = std::current_exception(); }
    }
    if (failure) std::rethrow_exception(failure);
    if (!chain.anchored()) throw Error(20, "the archive holds no print at or after the start: the window is not inside it");
    if (!chain.fenced()) throw Error(20, "the archive ends before the window's fence (the first print at or after the end)");
    return chain;
}

namespace {
ChainBars rest_bars(const Config& config, std::string& multiplier) {
    ChainBars chain(config.start, config.end);
    const auto now = now_ms();
    if (config.end > now) throw Error(20, "the window has not closed yet: no print at or after its end can exist");
    const auto venue = make_venue(config);
    venue->horizon(now);
    multiplier = venue->qty_multiplier();
    const auto window = venue->retention_ms();
    if (window && now - config.start > window)
        throw Error(20, "the export start is beyond the venue's REST print history window (" + std::to_string(window / 3600000) +
                        " h); use --archive for older windows");
    chain.anchor(venue->predecessor(config.start));
    log("info", "export_anchored", Json::object({{"predecessor_id", number(chain.predecessor().id)},
        {"predecessor_ts", signed_number(chain.predecessor().ts)}}));
    // OKX history-trades pages hold 100 prints; Binance pages hold 1000.
    const std::size_t limit = config.venue == "okx" ? 100 : 1000;
    for (std::uint64_t pages = 1; !chain.fenced(); ++pages) {
        const auto page = venue->history(chain.next_id(), limit);
        if (page.empty()) throw Error(20, "no print at or after the window end is available yet: its fence cannot be proven");
        for (const auto& trade : page)
            if (chain.add(trade)) break;
        if (pages % 20 == 0 || chain.fenced())
            log("info", "export_progress", Json::object({{"pages", number(pages)}, {"prints", number(chain.prints())},
                {"next_id", number(chain.next_id())}, {"ts", signed_number(chain.last_ts())}}));
    }
    return chain;
}
}

void export_bars(const Config& config, const ExportOptions& options) {
    if (!tick_mode(config.mode)) throw Error(23, "export builds bars from prints: use --mode ticks or agg-ticks");
    const auto manifest_path = options.output + ".manifest.json";
    const auto refuse_existing = [&] {
        if (options.output.empty() || std::filesystem::exists(options.output) || std::filesystem::exists(manifest_path))
            throw Error(23, "export output already exists (or is empty)");
    };
    refuse_existing();
    (void)ChainBars(config.start, config.end);  // the range and its budget are checked before any network or file access
    std::string multiplier = "1";
    Json source;
    std::optional<ChainBars> chain;
    if (!options.archive.empty()) {
        if (config.venue != "binance" || config.market != "usdm" || config.mode != "agg-ticks")
            throw Error(23, "--archive reads the Binance USD-M daily aggTrades archive: use --venue binance --market usdm --mode agg-ticks");
        const auto digest = file_sha256(options.archive);
        const bool verified = !options.checksum.empty();
        const auto file = Json::object({{"file", Json::string(base_name(options.archive))}, {"sha256", Json::string(digest)}});
        if (verified) verify_checksum(options.checksum, options.archive, digest);
        else log("warn", "archive_not_checksum_verified", file);
        if (base_name(options.archive).rfind(config.symbol + "-aggTrades-", 0) != 0) log("warn", "archive_name_not_symbol", file);
        chain = archive_bars(options.archive, config.start, config.end);
        source = Json::object({{"kind", Json::string("archive")}, {"file", Json::string(base_name(options.archive))},
            {"sha256", Json::string(digest)}, {"checksum_verified", Json::boolean(verified)}});
    } else {
        chain = rest_bars(config, multiplier);
        source = Json::object({{"kind", Json::string("rest")}, {"origin", Json::string(config.rest_url)}});
    }
    const auto& csv = chain->csv();
    const auto& predecessor = chain->predecessor();
    const auto& fence = chain->fence();
    const bool any = chain->prints() != 0;
    const auto manifest = Json::object({{"schema", Json::string("pineforge-feed-export/v1")},
        {"venue", Json::string(config.venue)}, {"market", Json::string(config.market)}, {"symbol", Json::string(config.symbol)},
        {"mode", Json::string(config.mode)}, {"source", source}, {"interval", Json::string("1m")},
        {"qty_multiplier", Json::string(multiplier)},
        {"start", signed_number(config.start)}, {"end_exclusive", signed_number(config.end)},
        {"predecessor", Json::object({{"id", number(predecessor.id)}, {"ts", signed_number(predecessor.ts)},
            {"price", Json::string(predecessor.price)}})},
        {"fence", Json::object({{"id", number(fence.id)}, {"ts", signed_number(fence.ts)}})},
        {"first_id", any ? number(predecessor.id + 1) : Json{}}, {"last_id", any ? number(fence.id - 1) : Json{}},
        {"prints", number(chain->prints())}, {"bars", number(chain->bars())}, {"quiet_minutes", number(chain->quiet_minutes())},
        {"rule", Json::string(bar_rule)}, {"sha256", Json::string(sha256(csv))}});
    refuse_existing();
    atomic_file(options.output, csv);
    atomic_file(manifest_path, manifest.dump() + '\n');
    log("info", "export_written", manifest);
}
}
