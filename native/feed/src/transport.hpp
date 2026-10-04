// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "core.hpp"
#include "venue.hpp"
#include <chrono>
#include <condition_variable>
#include <deque>
#include <exception>
#include <mutex>
#include <thread>
#include <vector>

namespace pineforge::feed {
void check_runtime_curl();
void validate_origin(const std::string& origin, bool websocket, bool allow_insecure);
void pause_for(std::chrono::milliseconds duration);
std::uint64_t retry_after_seconds(const std::string& value, std::int64_t now);
// Milliseconds to wait before a request of `weight`: none while the venue-reported use of the current
// one-minute window (`used`, reported in window `used_window`) leaves room under 90% of `limit`; otherwise
// until the next window opens (500 ms margin).
std::uint64_t quota_wait_ms(std::uint64_t used, std::uint64_t used_window, unsigned int weight, std::uint64_t limit, std::int64_t now_ms);

// A venue's public REST contract: allowlisted paths and how requests are paced.
struct RestPolicy {
    std::vector<std::string> endpoints;
    // Binance publishes its ceilings in a rateLimits document; other venues use fixed documented pacing.
    std::string limits_path;
    unsigned int limits_weight = 0;
    std::size_t limits_bytes = 1024 * 1024;
    std::uint64_t milliseconds_per_request = 0;
    // Maps a venue's rejection body to a specific stop before the generic HTTP status policy applies.
    std::function<void(long status, const std::string& body)> rejected;
    // A venue that signals rate limiting inside an HTTP 200 body.
    std::function<bool(const std::string& body)> throttled;
    // A venue code that means "try again" (timeout, busy): retried with backoff like HTTP 5xx.
    std::function<bool(long status, const std::string& body)> transient;
};
RestPolicy binance_spot_rest(const std::string& symbol);

class HttpClient {
    Config config_;
    RestPolicy policy_;
    std::chrono::steady_clock::time_point next_request_{};
    std::uint64_t weight_limit_ = 0, milliseconds_per_weight_ = 0, milliseconds_per_request_ = 0;
    std::uint64_t used_weight_ = 0, used_window_ = 0;  // the venue's X-MBX-USED-WEIGHT-1M and its minute
public:
    HttpClient(const Config& config, RestPolicy policy);
    std::string body(const std::string& path, unsigned int weight, std::size_t limit = 1024 * 1024);
    Json get(const std::string& path, unsigned int weight);
};
struct SourceMessage {
    bool connected = false;
    std::string text;
};
class WebSocketPump {
    Config config_;
    Connection connection_;
    std::string url_;
    std::atomic<bool> stopped_{false};
    std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<SourceMessage> queue_;
    std::size_t queue_bytes_ = 0;
    std::exception_ptr failure_;
    std::thread worker_;
    bool stopped() const;
    bool push(SourceMessage message);
    void wait_for_room(std::size_t bytes);
    void run();
public:
    WebSocketPump(const Config& config, Connection connection);
    ~WebSocketPump();
    WebSocketPump(const WebSocketPump&) = delete;
    WebSocketPump& operator=(const WebSocketPump&) = delete;
    SourceMessage take();
    bool try_take(SourceMessage& message);
};
}
