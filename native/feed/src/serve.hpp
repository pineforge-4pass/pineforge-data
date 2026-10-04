// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "core.hpp"
#include <optional>

namespace pineforge::feed {
// Reads committed journal lines by message index straight from the segment files, independently of the
// producer's State: a client catching up, and the snapshot, read here while the producer appends.
class JournalReader {
    std::string directory_;
    int descriptor_ = -1;
    std::string buffer_;
    std::size_t offset_ = 0;
    std::uint64_t index_ = 0;
    bool open(std::uint64_t base);
public:
    explicit JournalReader(std::string directory) : directory_(std::move(directory)) {}
    ~JournalReader();
    JournalReader(const JournalReader&) = delete;
    JournalReader& operator=(const JournalReader&) = delete;
    // Positions before message `index`; false when no retained segment holds it (expired).
    bool seek(std::uint64_t index);
    // The next complete line, or nullopt while it is not on disk yet.
    std::optional<std::string> next();
    std::uint64_t index() const { return index_; }
};

// The committed prefix [0, published) as JSONL, or nullopt when it exceeds `limit` bytes; throws 22 when index 0
// has expired or a segment disappears while it is read.
std::optional<std::string> snapshot_prefix(const std::string& journal, std::uint64_t published, std::size_t limit);
// The listening address: loopback unless the operator explicitly allows another interface.
std::string listen_address(const std::string& listen, bool allow_remote);
void serve_feed(Config config);
}
