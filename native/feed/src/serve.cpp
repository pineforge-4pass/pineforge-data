// SPDX-License-Identifier: Apache-2.0
#include "serve.hpp"
#include "session.hpp"
#include "state.hpp"
#include "transport.hpp"
#include "civetweb.h"
#include <arpa/inet.h>
#include <algorithm>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <fcntl.h>
#include <memory>
#include <mutex>
#include <set>
#include <thread>
#include <sys/resource.h>
#include <unistd.h>

namespace pineforge::feed {
namespace {
constexpr std::size_t snapshot_limit = 4 * 1024 * 1024;  // the runner's HTTP snapshot bound
constexpr auto keepalive = std::chrono::seconds(5);       // the runner's WebSocket idle deadline is 15 s

Json number(std::uint64_t value) { return Json::number(std::to_string(value)); }

struct Client {
    std::uint64_t next = 0;  // the next message index this client receives
    std::deque<std::shared_ptr<const std::string>> queue;
    std::size_t queue_bytes = 0;
    bool live = false, slow = false;
};

// Producer-side fan-out state. The producer appends released lines; each client's worker thread reads the
// journal from its cursor until it reaches the released head, then drains its own bounded queue.
struct Hub {
    Config config;
    std::string epoch, journal, source;
    std::mutex mutex;
    std::condition_variable changed;
    std::uint64_t published = 0, first_retained = 0;
    std::string status;  // the durable cursor's public fields
    std::set<Client*> clients;
    std::size_t connected = 0, serving = 0;
    bool stopping = false;
    std::atomic<bool> halting{false};  // stopping, readable without the mutex by clients catching up

    void publish(const std::string& line) {
        const auto shared = std::make_shared<const std::string>(line);
        std::lock_guard<std::mutex> guard(mutex);
        ++published;
        for (auto* client : clients) {
            if (!client->live || client->slow) continue;
            // A client that cannot keep up is cut off; it never slows the producer or another client.
            if (shared->size() > config.client_queue_bytes - std::min(client->queue_bytes, config.client_queue_bytes)) {
                client->slow = true;
                log("warn", "client_slow", Json::object({{"next", number(client->next)}, {"queued_bytes", number(client->queue_bytes)}}));
                continue;
            }
            client->queue_bytes += shared->size();
            client->queue.push_back(shared);
        }
        changed.notify_all();
    }
    void commit(const State& state) {
        const auto& durable = state.durable();
        const auto document = Json::object({
            {"schema", Json::string("pineforge-feed-status/v1")}, {"epoch", Json::string(durable.epoch)},
            {"source", parse_json(source)},
            {"retained", Json::object({{"first_index", number(state.first_retained())}, {"next_index", number(durable.message_index)}})},
            {"last", Json::object({{"seq", number(durable.seq)}, {"verified_cut", Json::number(std::to_string(durable.cut))},
                                   {"last_tick_ts", Json::number(std::to_string(durable.last_tick_ts))},
                                   {"last_bar", Json::number(std::to_string(durable.last_bar))}})}});
        std::lock_guard<std::mutex> guard(mutex);
        first_retained = state.first_retained();
        status = document.dump();
    }
    // Clients get their close frame (1001) before the server stops its connections.
    void stop() {
        std::unique_lock<std::mutex> guard(mutex);
        stopping = true;
        halting = true;
        changed.notify_all();
        changed.wait_for(guard, std::chrono::seconds(3), [this] { return serving == 0; });
    }
};

void respond(mg_connection* connection, int status, const char* type, const std::string& body) {
    mg_printf(connection, "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nCache-Control: no-store\r\nContent-Length: %zu\r\n"
              "Connection: close\r\n\r\n", status, mg_get_response_code_text(connection, status), type, body.size());
    if (!body.empty()) mg_write(connection, body.data(), body.size());
}
// Cursor negotiation fails in HTTP, before any WebSocket message, with the reason in the body.
int refuse(mg_connection* connection, int status, const std::string& reason) {
    log("warn", "client_refused", Json::object({{"status", Json::number(std::to_string(status))}, {"reason", Json::string(reason)}}));
    const auto body = Json::object({{"error", Json::string(reason)}}).dump() + '\n';
    respond(connection, status, "application/json", body);
    return status;
}
bool get_only(mg_connection* connection) {
    const auto* request = mg_get_request_info(connection);
    if (std::string(request->request_method) == "GET") return true;
    refuse(connection, 405, "only GET is served");
    return false;
}

int status_page(mg_connection* connection, void* data) {
    if (!get_only(connection)) return 405;
    auto& hub = *static_cast<Hub*>(data);
    std::string body;
    {
        std::lock_guard<std::mutex> guard(hub.mutex);
        auto document = parse_json(hub.status);
        document.members["clients"] = number(hub.connected);
        document.members["limits"] = Json::object({{"replay_bytes", number(hub.config.replay_bytes)},
            {"replay_age_seconds", number(static_cast<std::uint64_t>(hub.config.replay_age_ms / 1000))},
            {"client_queue_bytes", number(hub.config.client_queue_bytes)}, {"max_clients", number(hub.config.max_clients)}});
        body = document.dump() + '\n';
    }
    respond(connection, 200, "application/json", body);
    return 200;
}

// The complete committed prefix from index 0, the only HTTP shape the current runner accepts.
int snapshot_page(mg_connection* connection, void* data) {
    if (!get_only(connection)) return 405;
    auto& hub = *static_cast<Hub*>(data);
    std::uint64_t published = 0, first = 0;
    {
        std::lock_guard<std::mutex> guard(hub.mutex);
        published = hub.published;
        first = hub.first_retained;
    }
    const std::string expired = "the snapshot prefix from index 0 has expired from the retained journal; use /v1/stream";
    if (first) return refuse(connection, 410, expired);
    try {
        const auto body = snapshot_prefix(hub.journal, published, snapshot_limit);
        if (!body) return refuse(connection, 413, "the committed prefix exceeds the runner's 4 MiB HTTP snapshot bound; use /v1/stream");
        respond(connection, 200, "application/x-ndjson", *body);
        return 200;
    } catch (const Error& failure) { return failure.code == 20 ? refuse(connection, 410, expired) : refuse(connection, 500, failure.what()); }
}

bool query_value(const std::string& query, const char* name, std::string& value) {
    char buffer[128];
    const auto length = mg_get_var(query.c_str(), query.size(), name, buffer, sizeof(buffer));
    if (length < 0) return false;
    value.assign(buffer, static_cast<std::size_t>(length));
    return true;
}

int admit(const mg_connection* constant, void* data) {
    auto* connection = const_cast<mg_connection*>(constant);
    auto& hub = *static_cast<Hub*>(data);
    const auto* request = mg_get_request_info(connection);
    const std::string query = request->query_string ? request->query_string : "";
    std::string epoch, from;
    std::uint64_t index = 0;
    if (!query_value(query, "epoch", epoch) || !query_value(query, "from", from) || from.empty() ||
        from.find_first_not_of("0123456789") != std::string::npos ||
        std::from_chars(from.data(), from.data() + from.size(), index).ec != std::errc{})
        return refuse(connection, 400, "use /v1/stream?epoch=E&from=I with the stream epoch and a message index") != 0;
    int status = 0;
    std::string reason;
    {
        std::lock_guard<std::mutex> guard(hub.mutex);
        if (hub.stopping) status = 503, reason = "the producer is stopping";
        else if (epoch != hub.epoch) status = 409, reason = "wrong epoch: this stream's epoch is " + hub.epoch;
        else if (index > hub.published)
            status = 416, reason = "unknown cursor: index " + from + " is beyond the committed log (next index " +
                std::to_string(hub.published) + ")";
        else if (index < hub.first_retained)
            status = 410, reason = "cursor expired: index " + from + " is older than the retained journal (first retained index " +
                std::to_string(hub.first_retained) + "); rebuild the consumer from a warmup inside the retained window";
        else if (hub.connected >= hub.config.max_clients) status = 503, reason = "client limit reached (--max-clients)";
        else {
            auto* client = new Client;
            client->next = index;
            hub.clients.insert(client);
            ++hub.connected;
            mg_set_user_connection_data(constant, client);
        }
    }
    if (status) return refuse(connection, status, reason) != 0;
    log("info", "client_connected", Json::object({{"from", number(index)}}));
    return 0;
}

// No exception may unwind into CivetWeb's C code: a failing request answers 500, a failing upgrade is refused.
int on_status(mg_connection* connection, void* data) {
    try { return status_page(connection, data); }
    catch (...) { return refuse(connection, 500, "internal error"); }
}
int on_snapshot(mg_connection* connection, void* data) {
    try { return snapshot_page(connection, data); }
    catch (...) { return refuse(connection, 500, "internal error"); }
}
int on_connect(const mg_connection* connection, void* data) {
    try { return admit(connection, data); }
    catch (...) { return 1; }
}

bool send_close(mg_connection* connection, unsigned short code, const std::string& reason) {
    std::string payload;
    payload += static_cast<char>(code >> 8);
    payload += static_cast<char>(code & 0xff);
    payload += reason.substr(0, 120);
    log(code == 1000 || code == 1001 ? "info" : "warn", "client_closed",
        Json::object({{"code", Json::number(std::to_string(code))}, {"reason", Json::string(reason)}}));
    return mg_websocket_write(connection, MG_WEBSOCKET_OPCODE_CONNECTION_CLOSE, payload.data(), payload.size()) > 0;
}

void serve_client(mg_connection* connection, Hub& hub, Client* client) {
    auto last_send = std::chrono::steady_clock::now();
    // A write that cannot complete within the request timeout leaves a partial frame: the connection is dead.
    const auto send = [&](const std::string& bytes, int opcode) {
        const auto written = mg_websocket_write(connection, opcode, bytes.data(), bytes.size());
        if (written <= 0 || (!bytes.empty() && static_cast<std::size_t>(written) != bytes.size())) {
            log("warn", "client_write_failed", Json::object({{"next", number(client->next)}}));
            return false;
        }
        last_send = std::chrono::steady_clock::now();
        return true;
    };
    const auto expired = [&] {
        std::lock_guard<std::mutex> guard(hub.mutex);
        return client->next < hub.first_retained;
    };
    // The journal is read only while catching up: a client at the head holds no segment open.
    std::optional<JournalReader> reader;
    for (;;) {
        std::uint64_t head = 0;
        bool stopping = false;
        {
            std::lock_guard<std::mutex> guard(hub.mutex);
            stopping = hub.stopping;
            head = hub.published;
            if (!stopping && client->next == head) client->live = true;
        }
        if (stopping) { send_close(connection, 1001, "producer stopping"); return; }
        if (client->live) break;
        if (!reader) {
            reader.emplace(hub.journal);
            if (!reader->seek(client->next)) {
                send_close(connection, 4410, "cursor expired: older than the retained journal");
                return;
            }
        }
        while (client->next < head) {
            if (hub.halting) { send_close(connection, 1001, "producer stopping"); return; }
            auto line = reader->next();
            if (!line) {
                // A released line is on disk: missing means its segment expired (or the reader cannot open it).
                const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
                while (!line && std::chrono::steady_clock::now() < deadline && !hub.halting) {
                    if (expired()) {
                        send_close(connection, 4410, "cursor expired while catching up: the journal moved past it");
                        return;
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(2));
                    line = reader->next();
                }
                if (!line) {
                    if (hub.halting) continue;
                    send_close(connection, 1011, "journal segment unavailable");
                    return;
                }
            }
            if (!send(*line, MG_WEBSOCKET_OPCODE_TEXT)) return;
            ++client->next;
        }
    }
    reader.reset();
    for (;;) {
        std::shared_ptr<const std::string> line;
        {
            std::unique_lock<std::mutex> guard(hub.mutex);
            hub.changed.wait_until(guard, last_send + keepalive, [&] { return hub.stopping || client->slow || !client->queue.empty(); });
            // A stopping producer still delivers what it already released, then says goodbye.
            if (hub.stopping && client->queue.empty() && !client->slow) {
                guard.unlock();
                send_close(connection, 1001, "producer stopping");
                return;
            }
            if (client->slow) {
                guard.unlock();
                send_close(connection, 1008, "slow consumer: queued messages exceeded --client-queue-bytes " +
                                                 std::to_string(hub.config.client_queue_bytes));
                return;
            }
            if (!client->queue.empty()) {
                line = std::move(client->queue.front());
                client->queue.pop_front();
                client->queue_bytes -= line->size();
            }
        }
        if (line) {
            if (!send(*line, MG_WEBSOCKET_OPCODE_TEXT)) return;
            ++client->next;
        } else if (std::chrono::steady_clock::now() - last_send >= keepalive && !send("", MG_WEBSOCKET_OPCODE_PONG)) return;
    }
}

// One client, on its CivetWeb worker thread: the retained journal from its cursor, then live messages from
// its bounded queue, one WebSocket text message per normalized message, and an unsolicited PONG after 5 s
// without a message so a quiet stream never trips the runner's idle deadline. Nothing may unwind into
// CivetWeb's C code.
void on_ready(mg_connection* connection, void* data) {
    auto& hub = *static_cast<Hub*>(data);
    auto* client = static_cast<Client*>(mg_get_user_connection_data(connection));
    if (!client) return;
    {
        std::lock_guard<std::mutex> guard(hub.mutex);
        ++hub.serving;
    }
    struct Done {
        Hub& hub;
        ~Done() {
            std::lock_guard<std::mutex> guard(hub.mutex);
            --hub.serving;
            hub.changed.notify_all();
        }
    } done{hub};
    try { serve_client(connection, hub, client); }
    catch (...) {
        try { send_close(connection, 1011, "internal error"); } catch (...) {}
    }
}
// The protocol has no client messages: any data frame (CivetWeb reads frames only after the ready handler has
// returned, i.e. after serve's close) ends the connection instead of being read on.
int on_data(mg_connection*, int, char*, std::size_t, void*) { return 0; }
void on_close(const mg_connection*, void*) {}

// Every connection end, handshake failures included, releases its client.
void on_connection_close(const mg_connection* connection) {
    auto* hub = static_cast<Hub*>(mg_get_user_data(mg_get_context(connection)));
    auto* client = static_cast<Client*>(mg_get_user_connection_data(connection));
    if (!hub || !client) return;
    {
        std::lock_guard<std::mutex> guard(hub->mutex);
        hub->clients.erase(client);
        --hub->connected;
    }
    const auto next = client->next;
    delete client;
    mg_set_user_connection_data(connection, nullptr);
    try { log("info", "client_disconnected", Json::object({{"next", number(next)}})); } catch (...) {}
}
int on_log(const mg_connection*, const char* message) {
    log("warn", "http_server", Json::object({{"message", Json::string(std::string(message).substr(0, 512))}}));
    return 1;
}

class Server {
    mg_context* context_ = nullptr;
public:
    Server(Hub& hub, const std::string& address) {
        mg_init_library(0);
        // CivetWeb is thread-per-connection and every streaming client holds its worker. 16 spare workers serve
        // status, snapshots and new upgrades; a request that is not complete within 10 s (a half-open
        // connection) is dropped, which also bounds a blocked write to a dead client.
        const auto threads = std::to_string(hub.config.max_clients + 16);
        const char* options[] = {"listening_ports", address.c_str(), "num_threads", threads.c_str(),
                                 "request_timeout_ms", "10000", "websocket_timeout_ms", "10000",
                                 "enable_websocket_ping_pong", "yes", "enable_keep_alive", "no", nullptr};
        mg_callbacks callbacks{};
        callbacks.log_message = &on_log;
        callbacks.connection_close = &on_connection_close;
        context_ = mg_start(&callbacks, &hub, options);
        if (!context_) {
            mg_exit_library();
            throw Error(23, "cannot listen on " + address);
        }
        mg_set_request_handler(context_, "/v1/status$", &on_status, &hub);
        mg_set_request_handler(context_, "/v1/snapshot$", &on_snapshot, &hub);
        mg_set_websocket_handler(context_, "/v1/stream$", &on_connect, &on_ready, &on_data, &on_close, &hub);
        mg_server_port ports[4]{};
        const auto count = mg_get_server_ports(context_, 4, ports);
        log("info", "serve_listening", Json::object({{"address", Json::string(address)},
            {"port", Json::number(std::to_string(count > 0 ? ports[0].port : 0))}}));
    }
    ~Server() {
        mg_stop(context_);
        mg_exit_library();
    }
    Server(const Server&) = delete;
    Server& operator=(const Server&) = delete;
};
}

JournalReader::~JournalReader() { if (descriptor_ >= 0) ::close(descriptor_); }

// False only when the segment does not exist (expired, or not yet sealed into existence); any other failure,
// such as running out of descriptors, is an I/O error and never passes for an expired cursor.
bool JournalReader::open(std::uint64_t base) {
    const auto path = directory_ + "/" + segment_name(base) + ".jsonl";
    const int descriptor = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (descriptor < 0 && errno == ENOENT) return false;
    if (descriptor < 0) throw Error(22, "cannot open a journal segment for a client");
    if (descriptor_ >= 0) ::close(descriptor_);
    descriptor_ = descriptor;
    buffer_.clear();
    offset_ = 0;
    index_ = base;
    return true;
}

bool JournalReader::seek(std::uint64_t index) {
    // Retention may expire the chosen segment between the listing and the open: list once more.
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::uint64_t chosen = 0;
        bool found = false;
        std::error_code failure;
        std::filesystem::directory_iterator entries(directory_, failure), end;
        for (; !failure && entries != end; entries.increment(failure)) {
            const auto name = entries->path().filename().string();
            if (name.size() != 26 || name.compare(20, 6, ".jsonl") != 0) continue;
            std::uint64_t base = 0;
            if (std::from_chars(name.data(), name.data() + 20, base).ec != std::errc{}) continue;
            if (base <= index && (!found || base > chosen)) { chosen = base; found = true; }
        }
        if (failure) throw Error(22, "cannot list the journal for a client");
        if (!found) return false;
        if (!open(chosen)) continue;
        while (index_ < index) {
            auto skipped = next();
            if (!skipped) return false;
        }
        return true;
    }
    return false;
}

std::optional<std::string> JournalReader::next() {
    for (;;) {
        const auto newline = buffer_.find('\n', offset_);
        if (newline != std::string::npos) {
            std::string line = buffer_.substr(offset_, newline - offset_);
            offset_ = newline + 1;
            if (offset_ > 65536) { buffer_.erase(0, offset_); offset_ = 0; }
            ++index_;
            return line;
        }
        char chunk[65536];
        const auto count = ::read(descriptor_, chunk, sizeof(chunk));
        if (count < 0 && errno == EINTR) continue;
        if (count > 0) { buffer_.append(chunk, static_cast<std::size_t>(count)); continue; }
        // End of this segment: a sealed segment ends on a line boundary, and its successor starts here.
        if (offset_ == buffer_.size() && open(index_)) continue;
        return std::nullopt;
    }
}

std::optional<std::string> snapshot_prefix(const std::string& journal, std::uint64_t published, std::size_t limit) {
    JournalReader reader(journal);
    if (!reader.seek(0)) throw Error(20, "the snapshot prefix has expired");
    std::string body;
    while (reader.index() < published) {
        // Every published line is on disk before it is published: a missing one means its segment expired.
        auto line = reader.next();
        if (!line) throw Error(20, "a snapshot segment expired while it was read");
        if (line->size() + 1 > limit - body.size()) return std::nullopt;
        body += *line;
        body += '\n';
    }
    return body;
}

std::string listen_address(const std::string& listen, bool allow_remote) {
    const auto colon = listen.rfind(':');
    if (colon == std::string::npos || colon + 1 == listen.size()) throw Error(23, "--listen takes HOST:PORT or [IPV6]:PORT");
    auto host = listen.substr(0, colon);
    const auto port = listen.substr(colon + 1);
    unsigned int value = 0;
    if (port.find_first_not_of("0123456789") != std::string::npos ||
        std::from_chars(port.data(), port.data() + port.size(), value).ec != std::errc{} || value > 65535)
        throw Error(23, "--listen port must be 0..65535");
    bool loopback = false;
    if (host.size() > 2 && host.front() == '[' && host.back() == ']') {
        in6_addr parsed{};
        if (::inet_pton(AF_INET6, host.substr(1, host.size() - 2).c_str(), &parsed) != 1)
            throw Error(23, "--listen host must be an IPv4 address, [IPV6] or localhost");
        loopback = IN6_IS_ADDR_LOOPBACK(&parsed);
    } else {
        if (host == "localhost") host = "127.0.0.1";
        in_addr parsed{};
        if (::inet_pton(AF_INET, host.c_str(), &parsed) != 1)
            throw Error(23, "--listen host must be an IPv4 address, [IPV6] or localhost");
        loopback = (ntohl(parsed.s_addr) >> 24) == 127;
    }
    if (!loopback && !allow_remote)
        throw Error(23, "--listen on a non-loopback address needs --allow-remote-listen; serve has no TLS or authentication");
    return host + ":" + std::to_string(value);
}

void serve_feed(Config config) {
    const auto address = listen_address(config.listen, config.allow_remote_listen);
    // Each client holds a socket (and a journal segment while it catches up); the producer needs its own.
    rlimit files{};
    if (::getrlimit(RLIMIT_NOFILE, &files) == 0 && files.rlim_cur < files.rlim_max) {
        files.rlim_cur = files.rlim_max;
        ::setrlimit(RLIMIT_NOFILE, &files);
        ::getrlimit(RLIMIT_NOFILE, &files);
    }
    const auto needed = static_cast<rlim_t>(2 * config.max_clients + 64);
    if (files.rlim_cur != RLIM_INFINITY && files.rlim_cur < needed)
        throw Error(23, "the open-file limit (" + std::to_string(files.rlim_cur) + ") is below " + std::to_string(needed) +
                        " for --max-clients " + std::to_string(config.max_clients) + "; raise it or lower --max-clients");
    // Instrument metadata fixes the quantity units before the cursor binds them.
    const auto venue = make_venue(config);
    config.qty_multiplier = venue->qty_multiplier();
    config.serve = true;
    State state(config);
    Hub hub;
    hub.config = config;
    hub.epoch = state.durable().epoch;
    hub.journal = config.state_dir + "/journal";
    hub.published = state.durable().message_index;
    hub.source = Json::object({{"venue", Json::string(config.venue)}, {"market", Json::string(config.market)},
        {"symbol", Json::string(config.symbol)}, {"mode", Json::string(config.mode)}, {"rest_origin", Json::string(config.rest_url)},
        {"ws_origin", Json::string(config.ws_url)}, {"start", Json::number(std::to_string(state.durable().start))},
        {"qty_multiplier", Json::string(config.qty_multiplier)}}).dump();
    hub.commit(state);
    Server server(hub, address);
    try {
        const auto session = make_session(state, *venue, [&](const std::string& line) { hub.publish(line); });
        stream(config, *venue, *session, [&] { hub.commit(state); });
    } catch (const Stopped&) {
        hub.stop();
        log("info", "stopped", state.status());
        return;
    } catch (...) {
        hub.stop();
        log("error", "verified_cursor_retained", state.status());
        throw;
    }
}
}
