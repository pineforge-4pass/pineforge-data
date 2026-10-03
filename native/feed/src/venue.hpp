// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "core.hpp"
#include <vector>

namespace pineforge::feed {
struct VenueEvent {
    enum class Kind { Trade, Kline, Control } kind = Kind::Control;
    Trade trade;
    Kline kline;
};
class Venue {
public:
    virtual ~Venue() = default;
    virtual std::string streams() const = 0;
    virtual VenueEvent decode(const std::string& message) const = 0;
    virtual std::vector<Trade> history(std::uint64_t from, std::size_t limit = 1000) = 0;
    virtual std::uint64_t first_trade_id(std::int64_t minute) = 0;
    virtual std::vector<Kline> klines(std::int64_t start, std::int64_t end) = 0;
};
}
