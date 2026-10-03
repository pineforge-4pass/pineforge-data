// SPDX-License-Identifier: Apache-2.0
#include "transport.hpp"
#include <curl/curl.h>
#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <cstring>
#include <memory>
#include <poll.h>

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
    std::string body;
    std::uint64_t retry_after = 1;
    std::uint64_t used_weight = 0;
    bool overflow = false;
};
std::size_t body_callback(char* data, std::size_t size, std::size_t count, void* context) noexcept {
    auto* response = static_cast<Response*>(context);
    const auto bytes = size * count;
    if (bytes > 4 * 1024 * 1024 - response->body.size()) { response->overflow = true; return 0; }
    try { response->body.append(data, bytes); return bytes; }
    catch (...) { response->overflow = true; return 0; }
}
std::size_t header_callback(char* data, std::size_t size, std::size_t count, void* context) noexcept {
    const auto bytes = size * count;
    try {
        auto* response = static_cast<Response*>(context);
        std::string line(data, bytes);
        std::transform(line.begin(), line.end(), line.begin(), [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
        const auto colon = line.find(':');
        if (colon == std::string::npos) return bytes;
        const auto first = line.find_first_not_of(" \t", colon + 1);
        if (first == std::string::npos) return bytes;
        std::uint64_t value = 0;
        const auto result = std::from_chars(line.data() + first, line.data() + line.size(), value);
        if (result.ec != std::errc{}) return bytes;
        if (line.substr(0, colon) == "retry-after") response->retry_after = value;
        if (line.substr(0, colon) == "x-mbx-used-weight-1m") response->used_weight = value;
    } catch (...) {}
    return bytes;
}
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

HttpClient::HttpClient(const Config& config) : config_(config) {
    check_runtime_curl();
    validate_origin(config_.rest_url, false, config_.allow_insecure);
    try {
        const auto metadata = get("/api/v3/exchangeInfo?symbol=" + config_.symbol, 20);
        const auto& limits = metadata.at("rateLimits");
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
            const auto pace = (unit * multiple * 2 + ceiling - 1) / ceiling;
            if (type == "REQUEST_WEIGHT") {
                weighted = true;
                milliseconds_per_weight_ = std::max<std::uint64_t>(milliseconds_per_weight_, pace);
                if (interval == "MINUTE" && multiple == 1) weight_limit_ = ceiling;
            } else milliseconds_per_request_ = std::max<std::uint64_t>(milliseconds_per_request_, pace);
        }
        if (!weighted) throw Error(23, "public request-weight ceiling is unavailable");
        log("info", "rest_limits_verified", Json::object({{"milliseconds_per_weight", Json::number(std::to_string(milliseconds_per_weight_))},
            {"weight_limit_1m", Json::number(std::to_string(weight_limit_))}}));
    } catch (const Error&) { throw; }
    catch (const std::exception&) { throw Error(23, "invalid public exchange rate-limit metadata"); }
}
Json HttpClient::get(const std::string& path, unsigned int weight) {
    const auto endpoint = path.substr(0, path.find('?'));
    if (endpoint != "/api/v3/exchangeInfo" && endpoint != "/api/v3/historicalTrades" &&
        endpoint != "/api/v3/aggTrades" && endpoint != "/api/v3/klines")
        throw Error(23, "REST request is outside the public market-data allowlist");
    for (unsigned int attempt = 0; attempt < 4; ++attempt) {
        pause_until(next_request_);
        Progress state;
        auto handle = handle_for(config_.rest_url + path, false, config_.allow_insecure, &state);
        Response response;
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
        if (status == 401 || status == 403 || status == 418 || status == 451 || (status >= 300 && status < 400))
            throw Error(23, "public market-data access denied or redirected (HTTP " + std::to_string(status) + ")");
        if (status == 429) {
            log("warn", "rest_rate_limited", Json::object({{"retry_after", Json::number(std::to_string(response.retry_after))}}));
            if (response.retry_after > 86400) throw Error(22, "public Retry-After exceeds the bounded retry window");
            next_request_ = std::chrono::steady_clock::now() + std::chrono::seconds(response.retry_after);
            continue;
        }
        if (status == 404) throw Error(20, "public history is unavailable");
        if (code != CURLE_OK || status >= 500) {
            next_request_ = std::chrono::steady_clock::now() + std::chrono::seconds(1U << attempt);
            continue;
        }
        if (status != 200) throw Error(23, "public market-data request rejected (HTTP " + std::to_string(status) + ")");
        if (response.used_weight) log("info", "rest_weight", Json::object({{"used_weight_1m", Json::number(std::to_string(response.used_weight))}}));
        if (weight_limit_ && response.used_weight >= weight_limit_ - weight_limit_ / 10) {
            const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count() % 60000;
            next_request_ = std::max(next_request_, std::chrono::steady_clock::now() + std::chrono::milliseconds(60500 - elapsed));
            log("warn", "rest_shared_quota_pause");
        }
        try { return parse_json(response.body); }
        catch (const std::exception&) { throw Error(23, "public market-data response is invalid JSON"); }
    }
    throw Error(20, "public history retries exhausted; verified prefix retained");
}

bool WebSocketPump::stopped() const { return stopped_.load() || stopping.load(); }
WebSocketPump::WebSocketPump(const Config& config, const std::string& streams) : config_(config), url_(config.ws_url + "/stream?streams=" + streams) {
    check_runtime_curl();
    validate_origin(config_.ws_url, true, config_.allow_insecure);
    worker_ = std::thread([this] { run(); });
}
WebSocketPump::~WebSocketPump() {
    stopped_.store(true);
    ready_.notify_all();
    if (worker_.joinable()) worker_.join();
}
void WebSocketPump::push(SourceMessage message) {
    std::lock_guard<std::mutex> guard(mutex_);
    const auto bytes = message.text.size() + 32;
    if (bytes > config_.max_queue_bytes - std::min(queue_bytes_, config_.max_queue_bytes)) throw Error(22, "WebSocket healing buffer overrun");
    queue_bytes_ += bytes;
    queue_.push_back(std::move(message));
    ready_.notify_one();
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
                const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(std::min(30U, 1U << failures));
                while (!stopped() && std::chrono::steady_clock::now() < deadline) std::this_thread::sleep_for(std::chrono::milliseconds(50));
                continue;
            }
            failures = 0;
            reconnect_.store(false);
            push({true, {}});
            log("info", "websocket_connected");
            const auto birth = std::chrono::steady_clock::now();
            auto last_text = birth, message_birth = birth;
            std::string message;
            bool assembling = false;
            std::uint64_t frame_offset = 0;
            curl_socket_t socket = CURL_SOCKET_BAD;
            curl_easy_getinfo(handle.get(), CURLINFO_ACTIVESOCKET, &socket);
            while (!stopped() && !reconnect_) {
                const auto now = std::chrono::steady_clock::now();
                if (now - birth >= std::chrono::seconds(config_.reconnect_seconds) || now - last_text > std::chrono::seconds(75) ||
                    (assembling && now - message_birth > std::chrono::seconds(30))) break;
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
                last_text = now;
                if (metadata->bytesleft) continue;
                frame_offset = 0;
                if (metadata->flags & CURLWS_CONT) continue;
                bool retiring = false;
                try {
                    const auto envelope = parse_json(message);
                    const auto* wrapped = envelope.find("data");
                    const auto& data = wrapped ? *wrapped : envelope;
                    const auto* event = data.find("e");
                    retiring = event && event->kind == Json::Kind::String && event->text() == "serverShutdown";
                } catch (const std::exception&) { throw Error(23, "invalid public WebSocket JSON"); }
                push({false, std::move(message)});
                message.clear();
                assembling = false;
                if (retiring) { log("info", "source_retiring_reconnect"); break; }
            }
            if (!stopped()) {
                log("warn", "websocket_reconnect");
                std::this_thread::sleep_for(std::chrono::milliseconds(250));
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
