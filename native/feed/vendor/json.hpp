// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <charconv>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace pineforge::live {

// Bounded JSON for the native runner's configuration and JSONL wire format.
// Number tokens remain decimal strings so event identities never round uint64.
struct Json {
    enum class Kind { Null, Bool, Number, String, Array, Object };
    Kind kind = Kind::Null;
    std::string value;
    std::vector<Json> items;
    std::map<std::string, Json> members;

    static Json string(std::string v) {
        Json j;
        j.kind = Kind::String;
        j.value = std::move(v);
        return j;
    }
    static Json number(std::string v) {
        Json j;
        j.kind = Kind::Number;
        j.value = std::move(v);
        return j;
    }
    static Json boolean(bool v) {
        Json j;
        j.kind = Kind::Bool;
        j.value = v ? "true" : "false";
        return j;
    }
    static Json object(std::map<std::string, Json> v) {
        Json j;
        j.kind = Kind::Object;
        j.members = std::move(v);
        return j;
    }
    const Json &at(const std::string &key) const {
        if (kind != Kind::Object || members.count(key) != 1)
            throw std::runtime_error("missing field: " + key);
        return members.at(key);
    }
    const Json *find(const std::string &key) const {
        if (kind != Kind::Object)
            throw std::runtime_error("expected object");
        auto i = members.find(key);
        return i == members.end() ? nullptr : &i->second;
    }
    std::string text() const {
        if (kind != Kind::String)
            throw std::runtime_error("expected string");
        return value;
    }
    double real() const {
        if (kind != Kind::Number)
            throw std::runtime_error("expected number");
        std::size_t n = 0;
        double d = std::stod(value, &n);
        if (n != value.size() || !std::isfinite(d))
            throw std::runtime_error("nonfinite number");
        return d;
    }
    template <class T> T integer() const {
        if (kind != Kind::Number)
            throw std::runtime_error("expected integer");
        T result{};
        auto r = std::from_chars(value.data(), value.data() + value.size(), result);
        if (r.ec != std::errc{} || r.ptr != value.data() + value.size())
            throw std::runtime_error("invalid or overflowing integer");
        return result;
    }
    static std::string quote(std::string_view s) {
        static constexpr char hex[] = "0123456789abcdef";
        std::string out = "\"";
        for (unsigned char c : s) {
            if (c == '"' || c == '\\') {
                out += '\\';
                out += static_cast<char>(c);
            } else if (c < 32) {
                out += "\\u00";
                out += hex[c >> 4];
                out += hex[c & 15];
            } else
                out += static_cast<char>(c);
        }
        return out + '"';
    }
    std::string dump() const {
        if (kind == Kind::Null)
            return "null";
        if (kind == Kind::String)
            return quote(value);
        if (kind == Kind::Bool || kind == Kind::Number)
            return value;
        std::string out = kind == Kind::Array ? "[" : "{";
        bool first = true;
        if (kind == Kind::Array)
            for (const auto &i : items) {
                if (!first)
                    out += ',';
                first = false;
                out += i.dump();
            }
        else
            for (const auto &[key, i] : members) {
                if (!first)
                    out += ',';
                first = false;
                out += quote(key) + ':' + i.dump();
            }
        return out + (kind == Kind::Array ? ']' : '}');
    }
};

class JsonParser {
    std::string_view s_;
    std::size_t p_ = 0;
    [[noreturn]] void fail() const {
        throw std::runtime_error("invalid JSON at byte " + std::to_string(p_));
    }
    void ws() {
        while (p_ < s_.size() &&
               (s_[p_] == ' ' || s_[p_] == '\t' || s_[p_] == '\r' || s_[p_] == '\n'))
            ++p_;
    }
    bool take(char c) {
        if (p_ < s_.size() && s_[p_] == c) {
            ++p_;
            return true;
        }
        return false;
    }
    unsigned hex4() {
        unsigned value = 0;
        for (int i = 0; i < 4; ++i) {
            if (p_ == s_.size())
                fail();
            char c = s_[p_++];
            value <<= 4;
            if (c >= '0' && c <= '9')
                value += c - '0';
            else if (c >= 'a' && c <= 'f')
                value += c - 'a' + 10;
            else if (c >= 'A' && c <= 'F')
                value += c - 'A' + 10;
            else
                fail();
        }
        return value;
    }
    static void utf8(std::string &out, unsigned c) {
        if (c < 0x80)
            out += static_cast<char>(c);
        else if (c < 0x800) {
            out += static_cast<char>(0xc0 | (c >> 6));
            out += static_cast<char>(0x80 | (c & 63));
        } else if (c < 0x10000) {
            out += static_cast<char>(0xe0 | (c >> 12));
            out += static_cast<char>(0x80 | ((c >> 6) & 63));
            out += static_cast<char>(0x80 | (c & 63));
        } else {
            out += static_cast<char>(0xf0 | (c >> 18));
            out += static_cast<char>(0x80 | ((c >> 12) & 63));
            out += static_cast<char>(0x80 | ((c >> 6) & 63));
            out += static_cast<char>(0x80 | (c & 63));
        }
    }
    std::string str() {
        if (!take('"'))
            fail();
        std::string out;
        while (p_ < s_.size()) {
            unsigned char c = s_[p_++];
            if (c == '"')
                return out;
            if (c < 32)
                fail();
            if (c != '\\') {
                if (c < 128) {
                    out += static_cast<char>(c);
                    continue;
                }
                unsigned n = c >= 0xf0 ? 3 : c >= 0xe0 ? 2 : c >= 0xc2 ? 1 : 0;
                if (!n || c > 0xf4)
                    fail();
                unsigned point = c & ((1u << (6 - n)) - 1);
                for (unsigned i = 0; i < n; ++i) {
                    if (p_ == s_.size())
                        fail();
                    unsigned char tail = s_[p_++];
                    if ((tail & 0xc0) != 0x80)
                        fail();
                    point = (point << 6) | (tail & 63);
                }
                if (point < (n == 1   ? 0x80u
                             : n == 2 ? 0x800u
                                      : 0x10000u) ||
                    point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff))
                    fail();
                utf8(out, point);
                continue;
            }
            if (p_ == s_.size())
                fail();
            char e = s_[p_++];
            switch (e) {
            case '"':
            case '\\':
            case '/':
                out += e;
                break;
            case 'b':
                out += '\b';
                break;
            case 'f':
                out += '\f';
                break;
            case 'n':
                out += '\n';
                break;
            case 'r':
                out += '\r';
                break;
            case 't':
                out += '\t';
                break;
            case 'u': {
                unsigned point = hex4();
                if (point >= 0xd800 && point <= 0xdbff) {
                    if (!take('\\') || !take('u'))
                        fail();
                    unsigned low = hex4();
                    if (low < 0xdc00 || low > 0xdfff)
                        fail();
                    point = 0x10000 + ((point - 0xd800) << 10) + (low - 0xdc00);
                } else if (point >= 0xdc00 && point <= 0xdfff)
                    fail();
                utf8(out, point);
                break;
            }
            default:
                fail();
            }
        }
        fail();
    }
    Json parse(unsigned depth) {
        if (depth >= 32)
            fail();
        ws();
        if (p_ == s_.size())
            fail();
        if (s_[p_] == '"')
            return Json::string(str());
        if (take('{')) {
            Json j;
            j.kind = Json::Kind::Object;
            ws();
            if (take('}'))
                return j;
            do {
                ws();
                auto key = str();
                ws();
                if (!take(':'))
                    fail();
                auto v = parse(depth + 1);
                if (!j.members.emplace(std::move(key), std::move(v)).second)
                    fail();
                ws();
                if (take('}'))
                    return j;
            } while (take(','));
            fail();
        }
        if (take('[')) {
            Json j;
            j.kind = Json::Kind::Array;
            ws();
            if (take(']'))
                return j;
            do {
                j.items.push_back(parse(depth + 1));
                ws();
                if (take(']'))
                    return j;
            } while (take(','));
            fail();
        }
        for (auto lit : {"null", "true", "false"}) {
            std::string_view v = lit;
            if (s_.substr(p_, v.size()) == v) {
                p_ += v.size();
                return v == "null" ? Json{} : Json::boolean(v == "true");
            }
        }
        std::size_t begin = p_;
        take('-');
        if (take('0')) {
        } else {
            if (p_ == s_.size() || s_[p_] < '1' || s_[p_] > '9')
                fail();
            while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9')
                ++p_;
        }
        if (take('.')) {
            auto old = p_;
            while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9')
                ++p_;
            if (p_ == old)
                fail();
        }
        if (take('e') || take('E')) {
            if (!take('+'))
                take('-');
            auto old = p_;
            while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9')
                ++p_;
            if (p_ == old)
                fail();
        }
        return Json::number(std::string(s_.substr(begin, p_ - begin)));
    }

  public:
    explicit JsonParser(std::string_view s) : s_(s) {
        if (s.size() > 1024 * 1024)
            throw std::runtime_error("JSON frame exceeds 1 MiB");
    }
    Json run() {
        auto j = parse(0);
        ws();
        if (p_ != s_.size())
            fail();
        return j;
    }
};
inline Json parse_json(std::string_view s) { return JsonParser(s).run(); }
inline void only_fields(const Json &j, std::initializer_list<std::string> allowed) {
    if (j.kind != Json::Kind::Object)
        throw std::runtime_error("expected object");
    for (const auto &[key, value] : j.members) {
        bool found = false;
        for (const auto &a : allowed)
            found = found || a == key;
        if (!found)
            throw std::runtime_error("unknown field: " + key);
    }
}
} // namespace pineforge::live
