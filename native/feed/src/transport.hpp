// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "core.hpp"
#include <chrono>
#include <condition_variable>
#include <deque>
#include <exception>
#include <mutex>
#include <thread>

namespace pineforge::feed {
void check_runtime_curl();
void validate_origin(const std::string& origin, bool websocket, bool allow_insecure);

class HttpClient {
    Config config_;
    std::chrono::steady_clock::time_point next_request_{};
    std::uint64_t weight_limit_ = 0, milliseconds_per_weight_ = 100, milliseconds_per_request_ = 0;
public:
    explicit HttpClient(const Config& config);
    Json get(const std::string& path, unsigned int weight);
};
struct SourceMessage {
    bool connected = false;
    std::string text;
};
class WebSocketPump {
    Config config_;
    std::string url_;
    std::atomic<bool> stopped_{false}, reconnect_{false};
    std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<SourceMessage> queue_;
    std::size_t queue_bytes_ = 0;
    std::exception_ptr failure_;
    std::thread worker_;
    bool stopped() const;
    void push(SourceMessage message);
    void run();
public:
    WebSocketPump(const Config& config, const std::string& streams);
    ~WebSocketPump();
    WebSocketPump(const WebSocketPump&) = delete;
    WebSocketPump& operator=(const WebSocketPump&) = delete;
    SourceMessage take();
    void reconnect() { reconnect_.store(true); }
};
}
