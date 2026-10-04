// SPDX-License-Identifier: Apache-2.0
#include "transport.hpp"
#include <curl/curl.h>
#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <cstring>
#include <ctime>
#include <iomanip>
#include <locale>
#include <memory>
#include <poll.h>
#include <sstream>

#if LIBCURL_VERSION_NUM < 0x080e01
#error pineforge-feed requires libcurl 8.14.1 or newer
#endif

namespace pineforge::feed {
namespace {
using Handle = std::unique_ptr<CURL, decltype(&curl_easy_cleanup)>;
struct GlobalCurl {
    GlobalCurl() {
        if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) throw Error(23, "curl initialization failed");
    }
    ~GlobalCurl() { curl_global_cleanup(); }
};
template<class Value> void option(CURL* handle, CURLoption name, Value value) {
    if (curl_easy_setopt(handle, name, value) != CURLE_OK) throw Error(23, "curl option rejected");
}
struct Progress { const std::atomic<bool>* stopped = nullptr; };
int progress(void* context, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
    const auto* state = static_cast<Progress*>(context);
    return stopping || (state->stopped && state->stopped->load()) ? 1 : 0;
}
Handle handle_for(const std::string& url, bool websocket, bool insecure, Progress* state) {
    check_runtime_curl();
    Handle handle(curl_easy_init(), &curl_easy_cleanup);
    if (!handle) throw Error(23, "curl handle allocation failed");
    option(handle.get(), CURLOPT_URL, url.c_str());
    option(handle.get(), CURLOPT_NOSIGNAL, 1L);
    option(handle.get(), CURLOPT_NETRC, static_cast<long>(CURL_NETRC_IGNORED));
    option(handle.get(), CURLOPT_FOLLOWLOCATION, 0L);
    option(handle.get(), CURLOPT_MAXREDIRS, 0L);
    option(handle.get(), CURLOPT_SSL_VERIFYPEER, 1L);
    option(handle.get(), CURLOPT_SSL_VERIFYHOST, 2L);
    option(handle.get(), CURLOPT_CONNECTTIMEOUT_MS, 10000L);
    option(handle.get(), CURLOPT_TIMEOUT_MS, 20000L);
    option(handle.get(), CURLOPT_USERAGENT, "pineforge-feed/0.1");
    option(handle.get(), CURLOPT_PROTOCOLS_STR, websocket ? (insecure ? "ws,wss" : "wss") : (insecure ? "http,https" : "https"));
    option(handle.get(), CURLOPT_NOPROGRESS, 0L);
    option(handle.get(), CURLOPT_XFERINFOFUNCTION, &progress);
    option(handle.get(), CURLOPT_XFERINFODATA, state);
    return handle;
}
void pause_until(std::chrono::steady_clock::time_point deadline) {
    while (std::chrono::steady_clock::now() < deadline) {
        if (stopping) throw Stopped{};
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
}
struct Response {
    std::string body, retry_after;
    std::size_t limit = 1024 * 1024;
    std::uint64_t used_weight = 0;
    bool overflow = false;
};
std::size_t body_callback(char* data, std::size_t size, std::size_t count, void* context) noexcept {
    auto* response = static_cast<Response*>(context);
    const auto bytes = size * count;
    // The JSON parser refuses documents above 1 MiB, so a larger body is refused here unless the caller
    // extracts one bounded member from it.
    if (bytes > response->limit - response->body.size()) { response->overflow = true; return 0; }
    try { response->body.append(data, bytes); return bytes; }
    catch (...) { response->overflow = true; return 0; }
}
std::size_t header_callback(char* data, std::size_t size, std::size_t count, void* context) noexcept {
    const auto bytes = size * count;
    try {
        auto* response = static_cast<Response*>(context);
        const std::string line(data, bytes);
        const auto colon = line.find(':');
        if (colon == std::string::npos) return bytes;
        auto name = line.substr(0, colon);
        std::transform(name.begin(), name.end(), name.begin(), [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
        const auto first = line.find_first_not_of(" \t", colon + 1);
        const auto last = line.find_last_not_of(" \t\r\n");
        const auto value = first == std::string::npos || last < first ? std::string() : line.substr(first, last - first + 1);
        if (name == "retry-after") response->retry_after = value;
        if (name == "x-mbx-used-weight-1m") {
            std::uint64_t used = 0;
            const auto result = std::from_chars(value.data(), value.data() + value.size(), used);
            if (result.ec == std::errc{} && result.ptr == value.data() + value.size()) response->used_weight = used;
        }
    } catch (...) {}
    return bytes;
}
// A venue frame on a CONNECT_ONLY handle: control-size text, retried while the socket would block.
bool send_text(CURL* handle, curl_socket_t socket, const std::string& text) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    for (;;) {
        std::size_t sent = 0;
        const auto result = curl_ws_send(handle, text.data(), text.size(), &sent, 0, CURLWS_TEXT);
        if (result == CURLE_OK && sent == text.size()) return true;
        if (result != CURLE_AGAIN || sent || std::chrono::steady_clock::now() >= deadline) return false;
        pollfd descriptor{socket, POLLOUT, 0};
        ::poll(&descriptor, 1, 100);
    }
}
}

void pause_for(std::chrono::milliseconds duration) { pause_until(std::chrono::steady_clock::now() + duration); }

// Retry-After is either delta-seconds or an IMF-fixdate HTTP-date (RFC 9110, section 10.2.3).
std::uint64_t retry_after_seconds(const std::string& value, std::int64_t now) {
    std::uint64_t seconds = 0;
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), seconds);
    if (!value.empty() && parsed.ec == std::errc{} && parsed.ptr == value.data() + value.size()) return seconds;
    std::tm broken{};
    std::istringstream input(value);
    input.imbue(std::locale::classic());
    input >> std::get_time(&broken, "%a, %d %b %Y %H:%M:%S GMT");
    if (value.size() != 29 || input.fail()) throw Error(23, "invalid Retry-After on a public rate-limit response");
    const auto when = static_cast<std::int64_t>(timegm(&broken));
    return when > now ? static_cast<std::uint64_t>(when - now) : 0;
}

std::uint64_t quota_wait_ms(std::uint64_t used, std::uint64_t used_window, unsigned int weight, std::uint64_t limit, std::int64_t now_ms) {
    if (!limit || now_ms < 0) return 0;
    const auto window = static_cast<std::uint64_t>(now_ms) / 60000;
    const auto spent = window == used_window ? used : 0;
    if (spent + weight <= limit * 9 / 10) return 0;
    return (window + 1) * 60000 + 500 - static_cast<std::uint64_t>(now_ms);
}

void check_runtime_curl() {
    static GlobalCurl initialization;
    (void)initialization;
    const auto* info = curl_version_info(CURLVERSION_NOW);
    bool ws = false, wss = false;
    for (const char* const* protocol = info ? info->protocols : nullptr; protocol && *protocol; ++protocol) {
        ws = ws || std::strcmp(*protocol, "ws") == 0;
        wss = wss || std::strcmp(*protocol, "wss") == 0;
    }
    if (!info || info->version_num < 0x080e01 || !ws || !wss)
        throw Error(23, "libcurl >= 8.14.1 with both ws and wss is required at runtime");
}

void validate_origin(const std::string& origin, bool websocket, bool allow_insecure) {
    if (origin.empty() || origin.size() > 4096 || origin.find_first_of("\r\n") != std::string::npos || origin.find('\0') != std::string::npos)
        throw Error(23, "invalid public endpoint origin");
    std::unique_ptr<CURLU, decltype(&curl_url_cleanup)> parsed(curl_url(), &curl_url_cleanup);
    if (!parsed || curl_url_set(parsed.get(), CURLUPART_URL, origin.c_str(), CURLU_NON_SUPPORT_SCHEME) != CURLUE_OK)
        throw Error(23, "invalid public endpoint URL");
    auto field = [&](CURLUPart part) {
        char* text = nullptr;
        const auto status = curl_url_get(parsed.get(), part, &text, 0);
        std::string value;
        if (status == CURLUE_OK) { value = text; curl_free(text); }
        return value;
    };
    const auto scheme = field(CURLUPART_SCHEME);
    const auto host = field(CURLUPART_HOST);
    const auto secure = websocket ? "wss" : "https";
    const auto insecure = websocket ? "ws" : "http";
    if (scheme != secure && !(allow_insecure && scheme == insecure && (host == "127.0.0.1" || host == "localhost" || host == "[::1]")))
        throw Error(23, "public endpoints require TLS; insecure overrides are loopback-only");
    if (!field(CURLUPART_USER).empty() || !field(CURLUPART_PASSWORD).empty() || !field(CURLUPART_FRAGMENT).empty() ||
        !field(CURLUPART_QUERY).empty() || field(CURLUPART_PATH) != "/") throw Error(23, "endpoint must be a credential-free origin without a path or query");
}

RestPolicy binance_spot_rest(const std::string& symbol) {
    RestPolicy policy;
    policy.endpoints = {"/api/v3/exchangeInfo", "/api/v3/historicalTrades", "/api/v3/aggTrades", "/api/v3/klines"};
    policy.limits_path = "/api/v3/exchangeInfo?symbol=" + symbol;
    policy.limits_weight = 20;
    return policy;
}

HttpClient::HttpClient(const Config& config, RestPolicy policy) : config_(config), policy_(std::move(policy)) {
    check_runtime_curl();
    validate_origin(config_.rest_url, false, config_.allow_insecure);
    milliseconds_per_request_ = policy_.milliseconds_per_request;
    if (policy_.endpoints.empty()) throw Error(23, "public REST allowlist is empty");
    if (policy_.limits_path.empty()) return;
    try {
        // The USD-M document exceeds the parser bound: only its top-level rateLimits member is parsed.
        const auto document = body(policy_.limits_path, policy_.limits_weight, policy_.limits_bytes);
        const auto limits = parse_json(json_member(document, "rateLimits"));
        if (limits.kind != Json::Kind::Array || limits.items.size() > 64) throw Error(23, "invalid public rate-limit metadata");
        bool weighted = false;
        for (const auto& limit : limits.items) {
            const auto type = limit.at("rateLimitType").text();
            if (type != "REQUEST_WEIGHT" && type != "RAW_REQUESTS") continue;
            const auto interval = limit.at("interval").text();
            const auto multiple = limit.at("intervalNum").integer<std::uint64_t>();
            const auto ceiling = limit.at("limit").integer<std::uint64_t>();
            const auto unit = interval == "SECOND" ? 1000ULL : interval == "MINUTE" ? 60000ULL :
                interval == "HOUR" ? 3600000ULL : interval == "DAY" ? 86400000ULL : 0ULL;
            if (!unit || !multiple || multiple > 10000 || !ceiling || ceiling > 1000000000)
                throw Error(23, "unsupported public rate-limit interval or ceiling");
            // The one-minute weight ceiling is spent by the venue's own count (X-MBX-USED-WEIGHT-1M, shared by
            // every client on this IP); any other ceiling is paced evenly across its interval.
            const auto pace = (unit * multiple + ceiling - 1) / ceiling;
            if (type == "REQUEST_WEIGHT") {
                weighted = true;
                if (interval == "MINUTE" && multiple == 1) weight_limit_ = ceiling;
                else milliseconds_per_weight_ = std::max<std::uint64_t>(milliseconds_per_weight_, pace);
            } else milliseconds_per_request_ = std::max<std::uint64_t>(milliseconds_per_request_, pace);
        }
        if (!weighted) throw Error(23, "public request-weight ceiling is unavailable");
        log("info", "rest_limits_verified", Json::object({{"weight_limit_1m", Json::number(std::to_string(weight_limit_))},
            {"milliseconds_per_weight", Json::number(std::to_string(milliseconds_per_weight_))},
            {"milliseconds_per_request", Json::number(std::to_string(milliseconds_per_request_))}}));
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid public exchange rate-limit metadata"); }
}
Json HttpClient::get(const std::string& path, unsigned int weight) {
    const auto text = body(path, weight);
    try { return parse_json(text); }
    catch (const std::exception&) { throw Error(23, "public market-data response is invalid JSON"); }
}
std::string HttpClient::body(const std::string& path, unsigned int weight, std::size_t limit) {
    const auto endpoint = path.substr(0, path.find('?'));
    if (std::find(policy_.endpoints.begin(), policy_.endpoints.end(), endpoint) == policy_.endpoints.end())
        throw Error(23, "REST request is outside the public market-data allowlist");
    if (!limit || limit > 4 * 1024 * 1024) throw Error(22, "REST body bound exceeds 4 MiB");
    for (unsigned int attempt = 0; attempt < 4; ++attempt) {
        pause_until(next_request_);
        const auto wait = quota_wait_ms(used_weight_, used_window_, weight, weight_limit_, std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count());
        if (wait && !config_.allow_insecure) {
            log("warn", "rest_quota_wait", Json::object({{"used_weight_1m", Json::number(std::to_string(used_weight_))},
                {"weight_limit_1m", Json::number(std::to_string(weight_limit_))}, {"wait_ms", Json::number(std::to_string(wait))}}));
            pause_for(std::chrono::milliseconds(wait));
        }
        Progress state;
        auto handle = handle_for(config_.rest_url + path, false, config_.allow_insecure, &state);
        Response response;
        response.limit = limit;
        option(handle.get(), CURLOPT_WRITEFUNCTION, &body_callback);
        option(handle.get(), CURLOPT_WRITEDATA, &response);
        option(handle.get(), CURLOPT_HEADERFUNCTION, &header_callback);
        option(handle.get(), CURLOPT_HEADERDATA, &response);
        const auto code = curl_easy_perform(handle.get());
        long status = 0;
        curl_easy_getinfo(handle.get(), CURLINFO_RESPONSE_CODE, &status);
        const auto pacing = std::max<std::uint64_t>(milliseconds_per_request_, weight * milliseconds_per_weight_);
        if (pacing > 86400000) throw Error(22, "public quota wait exceeds the supported resource bound");
        next_request_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(config_.allow_insecure ? 0 : pacing);
        if (stopping || code == CURLE_ABORTED_BY_CALLBACK) throw Stopped{};
        if (response.overflow) throw Error(22, "REST response exceeds the bounded buffer");
        // A venue's own rejection meaning comes first (Bybit's 403 is a rate ban, USD-M's 400 codes).
        if (code == CURLE_OK && status != 200 && status != 429 && policy_.rejected) policy_.rejected(status, response.body);
        if (status == 401 || status == 403 || status == 418 || status == 451 || (status >= 300 && status < 400))
            throw Error(23, "public market-data access denied or redirected (HTTP " + std::to_string(status) + ")");
        if (status == 429 || (status == 200 && policy_.throttled && policy_.throttled(response.body))) {
            const auto wait = response.retry_after.empty() ? 1 : retry_after_seconds(response.retry_after,
                std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count());
            log("warn", "rest_rate_limited", Json::object({{"retry_after", Json::number(std::to_string(wait))}}));
            if (wait > 86400) throw Error(22, "public Retry-After exceeds the bounded retry window");
            next_request_ = std::chrono::steady_clock::now() + std::chrono::seconds(wait);
            continue;
        }
        if (code == CURLE_OK && policy_.transient && policy_.transient(status, response.body)) {
            log("warn", "rest_transient_retry", Json::object({{"status", Json::number(std::to_string(status))}}));
            next_request_ = std::chrono::steady_clock::now() + std::chrono::seconds(1U << attempt);
            continue;
        }
        if (status == 404) throw Error(20, "public history is unavailable");
        if (code != CURLE_OK || status >= 500) {
            next_request_ = std::chrono::steady_clock::now() + std::chrono::seconds(1U << attempt);
            continue;
        }
        if (status != 200) throw Error(23, "public market-data request rejected (HTTP " + std::to_string(status) + ")");
        if (response.used_weight) {
            used_weight_ = response.used_weight;
            used_window_ = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count()) / 60000;
            log("info", "rest_weight", Json::object({{"used_weight_1m", Json::number(std::to_string(response.used_weight))}}));
        }
        return std::move(response.body);
    }
    throw Error(20, "public history retries exhausted; verified prefix retained");
}

bool WebSocketPump::stopped() const { return stopped_.load() || stopping.load(); }
WebSocketPump::WebSocketPump(const Config& config, Connection connection)
    : config_(config), connection_(std::move(connection)), url_(config.ws_url + connection_.path) {
    check_runtime_curl();
    validate_origin(config_.ws_url, true, config_.allow_insecure);
    if (connection_.path.empty() || connection_.path.front() != '/' || !connection_.classify)
        throw Error(23, "invalid venue WebSocket connection");
    worker_ = std::thread([this] { run(); });
}
WebSocketPump::~WebSocketPump() {
    stopped_.store(true);
    ready_.notify_all();
    if (worker_.joinable()) worker_.join();
}
// False when the message does not fit the queue now; one message larger than the whole budget stops 22.
bool WebSocketPump::push(SourceMessage message) {
    std::lock_guard<std::mutex> guard(mutex_);
    const auto bytes = message.text.size() + 32;
    if (bytes > config_.max_queue_bytes) throw Error(22, "one source message exceeds --max-queue-bytes");
    if (bytes > config_.max_queue_bytes - std::min(queue_bytes_, config_.max_queue_bytes)) return false;
    queue_bytes_ += bytes;
    queue_.push_back(std::move(message));
    ready_.notify_one();
    return true;
}
void WebSocketPump::wait_for_room(std::size_t bytes) {
    for (;;) {
        {
            std::lock_guard<std::mutex> guard(mutex_);
            if (queue_bytes_ + bytes <= config_.max_queue_bytes) return;
        }
        if (stopped()) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
}
bool WebSocketPump::try_take(SourceMessage& message) {
    std::lock_guard<std::mutex> guard(mutex_);
    if (failure_) std::rethrow_exception(failure_);
    if (queue_.empty()) return false;
    message = std::move(queue_.front());
    queue_.pop_front();
    queue_bytes_ -= message.text.size() + 32;
    return true;
}
SourceMessage WebSocketPump::take() {
    std::unique_lock<std::mutex> guard(mutex_);
    while (queue_.empty() && !failure_) {
        if (stopped()) throw Stopped{};
        ready_.wait_for(guard, std::chrono::milliseconds(100));
    }
    if (failure_) std::rethrow_exception(failure_);
    auto message = std::move(queue_.front());
    queue_.pop_front();
    queue_bytes_ -= message.text.size() + 32;
    return message;
}

void WebSocketPump::run() {
    try {
        unsigned int failures = 0;
        // Reconnect backoff doubles from 2 units up to 30; loopback test origins use 50 ms units.
        const auto unit = std::chrono::milliseconds(config_.allow_insecure ? 50 : 1000);
        auto next_attempt = std::chrono::steady_clock::now();
        while (!stopped()) {
            while (!stopped() && std::chrono::steady_clock::now() < next_attempt)
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            if (stopped()) return;
            next_attempt = std::chrono::steady_clock::now() + std::chrono::milliseconds(config_.allow_insecure ? 250 : 5000);
            Progress state{&stopped_};
            auto handle = handle_for(url_, true, config_.allow_insecure, &state);
            option(handle.get(), CURLOPT_CONNECT_ONLY, 2L);
            option(handle.get(), CURLOPT_HTTP_VERSION, static_cast<long>(CURL_HTTP_VERSION_1_1));
            const auto handshake = curl_easy_perform(handle.get());
            long status = 0;
            curl_easy_getinfo(handle.get(), CURLINFO_RESPONSE_CODE, &status);
            if (stopped()) return;
            if (status == 401 || status == 403 || status == 418 || status == 451 || (status >= 300 && status < 400))
                throw Error(23, "public WebSocket access denied or redirected");
            if (handshake != CURLE_OK || status != 101) {
                if (++failures >= 8) throw Error(20, "public WebSocket reconnect attempts exhausted");
                const auto deadline = std::chrono::steady_clock::now() + unit * std::min(30U, 1U << failures);
                while (!stopped() && std::chrono::steady_clock::now() < deadline) std::this_thread::sleep_for(std::chrono::milliseconds(50));
                continue;
            }
            curl_socket_t socket = CURL_SOCKET_BAD;
            curl_easy_getinfo(handle.get(), CURLINFO_ACTIVESOCKET, &socket);
            // The marker precedes every message of this connection: the session verifies its REST
            // overlap while the subscribed data is buffered behind it.
            wait_for_room(32);
            if (stopped() || !push({true, {}})) return;
            bool subscribed = true;
            for (const auto& request : connection_.subscribe) subscribed = subscribed && send_text(handle.get(), socket, request);
            log(subscribed ? "info" : "warn", subscribed ? "websocket_connected" : "websocket_subscribe_failed");
            const auto birth = std::chrono::steady_clock::now();
            auto last_data = birth, message_birth = birth, last_ping = birth, probe_sent = birth;
            std::string message;
            bool assembling = false, heard = false, probing = false, backpressure = false;
            std::uint64_t frame_offset = 0;
            while (subscribed && !stopped()) {
                const auto now = std::chrono::steady_clock::now();
                if (now - birth >= std::chrono::seconds(config_.reconnect_seconds) || (assembling && now - message_birth > std::chrono::seconds(30))) break;
                // Keepalive replies are control messages: they cannot hide a silent data stream. A venue with a
                // probe proves the connection and its subscriptions instead of paying a reconnect; completeness
                // never rests on the connection (chain IDs, watermarks and the reconnect overlap prove it).
                if (now - last_data > std::chrono::seconds(config_.silence_seconds)) {
                    if (connection_.probe.empty()) break;
                    if (!probing) {
                        if (!send_text(handle.get(), socket, connection_.probe)) break;
                        probing = true;
                        probe_sent = now;
                    } else if (now - probe_sent > std::chrono::seconds(10)) {
                        log("warn", "source_probe_unanswered");
                        break;
                    }
                }
                if (!connection_.ping.empty() && now - last_ping >= std::chrono::seconds(config_.keepalive_seconds)) {
                    if (!send_text(handle.get(), socket, connection_.ping)) break;
                    last_ping = now;
                }
                std::array<char, 16384> buffer{};
                std::size_t received = 0;
                const curl_ws_frame* metadata = nullptr;
                const auto result = curl_ws_recv(handle.get(), buffer.data(), buffer.size(), &received, &metadata);
                if (result == CURLE_AGAIN) {
                    pollfd descriptor{socket, POLLIN, 0};
                    ::poll(&descriptor, 1, 100);
                    continue;
                }
                if (result != CURLE_OK || !metadata || (metadata->flags & CURLWS_CLOSE)) break;
                if (metadata->flags & (CURLWS_PING | CURLWS_PONG)) continue;
                if ((metadata->flags & CURLWS_BINARY) || !(metadata->flags & CURLWS_TEXT) || metadata->offset < 0 || metadata->bytesleft < 0 ||
                    static_cast<std::uint64_t>(metadata->offset) != frame_offset) throw Error(23, "public WebSocket requires ordered text frames");
                if (!assembling) { assembling = true; message_birth = now; }
                if (received > 1024 * 1024 - message.size() || static_cast<std::uint64_t>(metadata->bytesleft) > 1024 * 1024 - message.size() - received)
                    throw Error(22, "WebSocket message exceeds 1 MiB");
                message.append(buffer.data(), received);
                frame_offset += received;
                if (metadata->bytesleft) continue;
                frame_offset = 0;
                if (metadata->flags & CURLWS_CONT) continue;
                const auto kind = connection_.classify(message);
                assembling = false;
                // A connection counts as established only once the venue answers on it.
                if (!heard) { heard = true; failures = 0; }
                if (kind == Frame::Stale) { log("warn", "source_subscription_lost"); break; }
                if (kind == Frame::Control) {
                    // A probe reply restarts the silence clock: the connection and subscriptions are alive.
                    if (probing) { probing = false; last_data = now; log("info", "source_probe_answered"); }
                    message.clear();
                    continue;
                }
                last_data = now;
                probing = false;
                // A full queue (the session is busy healing a long outage) drops this connection instead of
                // stopping: everything from here on is healed from REST after the reconnect's overlap check.
                if (!push({false, std::move(message)})) { backpressure = true; break; }
                message.clear();
                if (kind == Frame::Retire) { log("info", "source_retiring_reconnect"); break; }
            }
            if (backpressure && !stopped()) {
                log("warn", "websocket_backpressure_reconnect", Json::object({{"max_queue_bytes", Json::number(std::to_string(config_.max_queue_bytes))}}));
                handle.reset();
                wait_for_room(config_.max_queue_bytes - config_.max_queue_bytes / 4);
                next_attempt = std::chrono::steady_clock::now();
                continue;
            }
            if (!stopped()) {
                log("warn", "websocket_reconnect");
                std::this_thread::sleep_for(std::chrono::milliseconds(250));
                // Accepted, then closed or refused before any frame: that spends the reconnect budget too.
                if (!heard) {
                    if (++failures >= 8) throw Error(20, "public WebSocket reconnect attempts exhausted");
                    const auto deadline = std::chrono::steady_clock::now() + unit * std::min(30U, 1U << failures);
                    while (!stopped() && std::chrono::steady_clock::now() < deadline) std::this_thread::sleep_for(std::chrono::milliseconds(50));
                }
            }
        }
    } catch (const Stopped&) {}
    catch (...) {
        std::lock_guard<std::mutex> guard(mutex_);
        failure_ = std::current_exception();
        ready_.notify_all();
    }
}
}
