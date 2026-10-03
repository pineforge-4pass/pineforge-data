# SPDX-License-Identifier: Apache-2.0
"""Synthetic public Binance, Binance USD-M, OKX and Bybit shapes; standard-library HTTP/WS fault
injection."""

import base64
import copy
import email.utils
import fcntl
import hashlib
import http.server
import json
import os
import pathlib
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from types import SimpleNamespace

from public_e2e import Soak, action_key, proven_actions

BINARY = str(pathlib.Path(sys.argv[1]).resolve())
sys.argv = [sys.argv[0]]
TRADES = {
    99: (119999, "10.10000000", "0.10000000"),
    100: (120001, "10.10000000", "0.10000000"),
    101: (120050, "11.20000000", "0.20000000"),
    102: (179999, "9.90000000", "0.30000000"),
    103: (180001, "12.00000000", "0.40000000"),
    104: (180050, "11.00000000", "0.10000000"),
    105: (240001, "12.00000000", "0.10000000"),
}
BARS = {
    120000: ("10.10000000", "11.20000000", "9.90000000", "9.90000000", "0.60000000", 100, 102, 3),
    180000: ("12.00000000", "12.00000000", "11.00000000", "11.00000000", "0.50000000", 103, 104, 2),
    240000: ("12.00000000", "12.00000000", "12.00000000", "12.00000000", "0.10000000", 105, 105, 1),
}


def trade(identifier, **changes):
    matched, price, quantity = TRADES[identifier]
    data = {
        "e": "trade",
        "E": matched + 999,
        "s": "TESTUSDT",
        "t": identifier,
        "p": price,
        "q": quantity,
        "T": matched,
        "m": False,
        "M": True,
    }
    data.update(changes)
    return {"stream": "testusdt@trade", "data": data}


def candle(minute, confirmed=True, **changes):
    opening, high, low, close, volume, first, last, count = BARS[minute]
    row = {
        "t": minute,
        "T": minute + 59999,
        "s": "TESTUSDT",
        "i": "1m",
        "f": first,
        "L": last,
        "o": opening,
        "h": high,
        "l": low,
        "c": close,
        "v": volume,
        "n": count,
        "x": confirmed,
        "q": "0.00000000",
        "V": "0.00000000",
        "Q": "0.00000000",
        "B": "0",
    }
    row.update(changes)
    return {
        "stream": "testusdt@kline_1m",
        "data": {"e": "kline", "E": minute + 60000, "s": "TESTUSDT", "k": row},
    }


def retirement():
    return {"stream": "!serverShutdown", "data": {"e": "serverShutdown", "E": 180000}}


# USD-M aggregate prints: id -> (matched time, price, q, nq, first raw id, last raw id). `q` counts
# every execution and reconciles with the kline volume; `nq` excludes RPI executions. Raw ranges are
# not contiguous across aggregates (1005 is absent) and are never expanded.
AGGREGATES = {
    499: (119999, "10.10000000", "0.10000000", "0.10000000", 998, 999),
    500: (120001, "10.10000000", "0.10000000", "0.05000000", 1000, 1004),
    501: (120050, "11.20000000", "0.20000000", "0.20000000", 1006, 1006),
    502: (179999, "9.90000000", "0.30000000", "0.30000000", 1007, 1009),
    503: (180001, "12.00000000", "0.40000000", "0.40000000", 1010, 1012),
    504: (180050, "11.00000000", "0.10000000", "0.10000000", 1013, 1013),
    505: (240001, "12.00000000", "0.10000000", "0.10000000", 1014, 1014),
}


def aggregate(identifier, **changes):
    matched, price, quantity, net, first, last = AGGREGATES[identifier]
    data = {
        "e": "aggTrade",
        "E": matched + 1,
        "a": identifier,
        "s": "TESTUSDT",
        "p": price,
        "q": quantity,
        "nq": net,
        "f": first,
        "l": last,
        "T": matched,
        "m": True,
        "st": 1,
    }
    data.update(changes)
    return {"stream": "testusdt@aggTrade", "data": data}


# OKX trades-all prints in contracts (ctVal 0.01): id -> (ts, px, sz). Candle volumes are contracts.
OKX_TRADES = {
    99: (119999, "10.1", "10"),
    100: (120001, "10.1", "10"),
    101: (120050, "11.2", "20"),
    102: (179999, "9.9", "30"),
    103: (180001, "12", "40"),
    104: (180050, "11", "10"),
    105: (240001, "12", "10"),
}
OKX_CANDLES = {
    120000: ("10.1", "11.2", "9.9", "9.9", "60"),
    180000: ("12", "12", "11", "11", "50"),
    240000: ("12", "12", "12", "12", "10"),
}
OKX_SWAP = "TEST-USDT-SWAP"


def okx_trades(*identifiers, channel="trades-all", **changes):
    rows = []
    for identifier in identifiers:
        matched, price, size = OKX_TRADES[identifier]
        row = {
            "instId": OKX_SWAP,
            "tradeId": str(identifier),
            "px": price,
            "sz": size,
            "side": "buy",
            "ts": str(matched),
            "source": "0",
        }
        row.update(changes)
        rows.append(row)
    return {"arg": {"channel": channel, "instId": OKX_SWAP}, "data": rows}


def okx_candle(minute, confirm="1", candles=None):
    opening, high, low, close, volume = (candles or OKX_CANDLES)[minute]
    row = [str(minute), opening, high, low, close, volume, "0", "0", confirm]
    return {"arg": {"channel": "candle1m", "instId": OKX_SWAP}, "data": [row]}


# Bybit linear bars: base quantities already.
BYBIT_BARS = {
    120000: ("10.1", "11.2", "9.9", "9.9", "0.6"),
    180000: ("12", "12", "11", "11", "0.5"),
    240000: ("12", "12", "12", "12", "0.1"),
    300000: ("12", "12.5", "12", "12.5", "0.2"),
}


def bybit_row(minute, confirm):
    opening, high, low, close, volume = BYBIT_BARS[minute]
    return {
        "start": minute,
        "end": minute + 59999,
        "interval": "1",
        "open": opening,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "turnover": "0",
        "confirm": confirm,
        "timestamp": minute + 30000,
    }


def bybit_push(*rows):
    return {"topic": "kline.1.TESTUSDT", "data": list(rows), "ts": 1, "type": "snapshot"}


def frame(connection, payload, opcode=1, final=True):
    if isinstance(payload, dict):
        payload = json.dumps(payload, separators=(",", ":")).encode()
    if isinstance(payload, str):
        payload = payload.encode()
    header = bytes([(128 if final else 0) | opcode])
    if len(payload) < 126:
        header += bytes([len(payload)])
    elif len(payload) < 65536:
        header += bytes([126]) + struct.pack("!H", len(payload))
    else:
        header += bytes([127]) + struct.pack("!Q", len(payload))
    connection.sendall(header + payload)


def receive(connection):
    def exact(size):
        result = bytearray()
        while len(result) < size:
            chunk = connection.recv(size - len(result))
            if not chunk:
                raise EOFError
            result.extend(chunk)
        return bytes(result)

    first, second = exact(2)
    size = second & 127
    if size == 126:
        size = struct.unpack("!H", exact(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", exact(8))[0]
    if not second & 128:
        raise AssertionError("client frames must be masked")
    mask = exact(4)
    payload = exact(size)
    return first & 15, bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


class MockServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, scripts, venue="binance"):
        super().__init__(("127.0.0.1", 0), Handler)
        self.venue = venue
        self.scripts = scripts
        self.aggregates = copy.deepcopy(AGGREGATES)
        self.okx_trades = copy.deepcopy(OKX_TRADES)
        self.okx_candles = copy.deepcopy(OKX_CANDLES)
        self.okx_unconfirmed = set()
        self.instrument = {
            "instId": OKX_SWAP,
            "instType": "SWAP",
            "state": "live",
            "ctType": "linear",
            "ctValCcy": "TEST",
            "ctVal": "0.01",
            "ctMult": "1",
        }
        self.bybit_bars = copy.deepcopy(BYBIT_BARS)
        self.bybit_ascending = False
        self.agg_window_error = False
        self.queries = []
        self.subscriptions = []
        self.ws_paths = []
        self.exchange_info_bytes = 0
        self.pings = []
        self.okx_time_lookup_skip = 0
        self.refuse_once = None
        self.usdm_unknown_symbol = False
        self.connections = 0
        self.trades = copy.deepcopy(TRADES)
        self.bars = copy.deepcopy(BARS)
        self.requests = []
        self.request_times = []
        self.agg_empty = 0
        self.pongs = []
        self.fail_status = 0
        self.rate_once = False
        self.retry_after = 0
        self.rate_limits = None
        self.auth_headers = []
        self.streams = []
        self.lock = threading.Lock()
        self.errors = []

    def handle_error(self, request, client_address):
        failure = sys.exc_info()[1]
        if not isinstance(failure, (BrokenPipeError, ConnectionResetError, EOFError)):
            self.errors.append(repr(failure))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *arguments):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        with self.server.lock:
            self.server.requests.append(parsed.path)
            self.server.queries.append((parsed.path, query))
            self.server.request_times.append((parsed.path, time.monotonic()))
            self.server.auth_headers.extend(
                name
                for name in ("Authorization", "X-MBX-APIKEY", "OK-ACCESS-KEY", "X-BAPI-API-KEY")
                if self.headers.get(name)
            )
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.websocket(parsed.path, query)
            return
        with self.server.lock:
            if self.server.fail_status:
                self.respond(self.server.fail_status, {"code": -1, "msg": "synthetic refusal"})
                return
            if self.server.rate_once:
                self.server.rate_once = False
                self.respond(429, {}, retry=True)
                return
            if self.server.refuse_once:
                status, value = self.server.refuse_once
                self.server.refuse_once = None
                self.respond(status, value)
                return
            if self.server.venue != "binance":
                status, rows = getattr(self, "rest_" + self.server.venue)(parsed.path, query)
                self.respond(status, rows)
                return
            if parsed.path == "/api/v3/exchangeInfo":
                rows = {
                    "rateLimits": [
                        {
                            "rateLimitType": "REQUEST_WEIGHT",
                            "interval": "MINUTE",
                            "intervalNum": 1,
                            "limit": 6000,
                        },
                        {
                            "rateLimitType": "RAW_REQUESTS",
                            "interval": "MINUTE",
                            "intervalNum": 5,
                            "limit": 61000,
                        },
                    ],
                    "symbols": [{"symbol": "TESTUSDT", "baseAsset": "TEST", "quoteAsset": "USDT"}],
                }
                if self.server.rate_limits is not None:
                    rows["rateLimits"] = self.server.rate_limits
            elif parsed.path == "/api/v3/historicalTrades":
                start = int(query["fromId"][0])
                limit = int(query["limit"][0])
                rows = []
                for identifier in sorted(self.server.trades):
                    if identifier >= start and len(rows) < limit:
                        matched, price, quantity = self.server.trades[identifier]
                        rows.append(
                            {
                                "id": identifier,
                                "price": price,
                                "qty": quantity,
                                "quoteQty": "0.00000000",
                                "time": matched,
                                "isBuyerMaker": False,
                                "isBestMatch": True,
                            }
                        )
            elif parsed.path == "/api/v3/aggTrades":
                start, end = int(query["startTime"][0]), int(query["endTime"][0])
                matching = [
                    identifier
                    for identifier in sorted(self.server.trades)
                    if start <= self.server.trades[identifier][0] <= end
                ]
                if self.server.agg_empty:
                    self.server.agg_empty -= 1
                    matching = []
                rows = (
                    []
                    if not matching
                    else [
                        {
                            "a": 42,
                            "f": matching[0],
                            "l": matching[0],
                            "T": self.server.trades[matching[0]][0],
                            "p": "10.10000000",
                            "q": "0.10000000",
                            "m": False,
                            "M": True,
                        }
                    ]
                )
            elif parsed.path == "/api/v3/klines":
                start, end = int(query["startTime"][0]), int(query["endTime"][0])
                rows = []
                for minute in sorted(self.server.bars):
                    if start <= minute <= end and len(rows) < 1000:
                        opening, high, low, close, volume, _first, _last, count = self.server.bars[
                            minute
                        ]
                        rows.append(
                            [
                                minute,
                                opening,
                                high,
                                low,
                                close,
                                volume,
                                minute + 59999,
                                "0.00000000",
                                count,
                                "0.00000000",
                                "0.00000000",
                                "0",
                            ]
                        )
            else:
                self.respond(404, {})
                return
        self.respond(200, rows)

    def rest_usdm(self, path, query):
        server = self.server
        if path == "/fapi/v1/exchangeInfo":
            # The real document lists every contract and exceeds the 1 MiB parser bound.
            document = {
                "timezone": "UTC",
                "assets": [
                    {"asset": "PAD" + str(index), "note": "x" * 96} for index in range(12000)
                ],
                "rateLimits": [
                    {
                        "rateLimitType": "REQUEST_WEIGHT",
                        "interval": "MINUTE",
                        "intervalNum": 1,
                        "limit": 2400,
                    }
                ],
                "symbols": [{"symbol": "TESTUSDT", "contractType": "PERPETUAL"}],
            }
            server.exchange_info_bytes = len(json.dumps(document, separators=(",", ":")))
            return 200, document
        if path == "/fapi/v1/aggTrades":
            if "fromId" in query:
                start = int(query["fromId"][0])
                if server.agg_window_error and start > 499:
                    return 400, {
                        "code": -4166,
                        "msg": "Search window is restricted to recent 2 days only.",
                    }
                identifiers = [key for key in sorted(server.aggregates) if key >= start]
            else:
                begin, end = int(query["startTime"][0]), int(query["endTime"][0])
                identifiers = [
                    key
                    for key in sorted(server.aggregates)
                    if begin <= server.aggregates[key][0] <= end
                ]
            rows = []
            for identifier in identifiers[: int(query["limit"][0])]:
                matched, price, quantity, net, first, last = server.aggregates[identifier]
                rows.append(
                    {
                        "a": identifier,
                        "p": price,
                        "q": quantity,
                        "nq": net,
                        "f": first,
                        "l": last,
                        "T": matched,
                        "m": True,
                    }
                )
            return 200, rows
        if path == "/fapi/v1/klines":
            if server.usdm_unknown_symbol:
                return 400, {"code": -1121, "msg": "Invalid symbol."}
            if "startTime" not in query:
                return 200, []
            start, end = int(query["startTime"][0]), int(query["endTime"][0])
            rows = []
            for minute in sorted(server.bars):
                if start <= minute <= end:
                    opening, high, low, close, volume, _first, _last, count = server.bars[minute]
                    rows.append(
                        [
                            minute,
                            opening,
                            high,
                            low,
                            close,
                            volume,
                            minute + 59999,
                            "0",
                            count,
                            "0",
                            "0",
                            "0",
                        ]
                    )
            return 200, rows[: int(query["limit"][0])]
        return 404, {}

    def rest_okx(self, path, query):
        server = self.server
        if path == "/api/v5/public/instruments":
            match = query["instId"][0] == server.instrument["instId"]
            return 200, {"code": "0", "msg": "", "data": [server.instrument] if match else []}
        if path == "/api/v5/market/history-trades":
            limit = int(query["limit"][0])
            if query.get("type") == ["2"]:
                before_time = int(query["after"][0])
                keys = [
                    key
                    for key in sorted(server.okx_trades)
                    if server.okx_trades[key][0] < before_time
                ]
                # The time lookup can answer an older print first (lag, or one millisecond's order).
                keys = keys[: len(keys) - server.okx_time_lookup_skip]
            else:
                upper = int(query["after"][0])
                lower = int(query["before"][0])
                keys = [key for key in sorted(server.okx_trades) if lower < key < upper]
            rows = []
            for identifier in sorted(keys, reverse=True)[:limit]:
                matched, price, size = server.okx_trades[identifier]
                rows.append(
                    {
                        "instId": OKX_SWAP,
                        "side": "buy",
                        "sz": size,
                        "px": price,
                        "source": "0",
                        "tradeId": str(identifier),
                        "ts": str(matched),
                    }
                )
            return 200, {"code": "0", "msg": "", "data": rows}
        if path == "/api/v5/market/history-candles":
            upper, lower = int(query["after"][0]), int(query["before"][0])
            rows = []
            for minute in sorted(server.okx_candles, reverse=True):
                if lower < minute < upper:
                    opening, high, low, close, volume = server.okx_candles[minute]
                    confirm = "0" if minute in server.okx_unconfirmed else "1"
                    rows.append([str(minute), opening, high, low, close, volume, "0", "0", confirm])
            return 200, {"code": "0", "msg": "", "data": rows[: int(query["limit"][0])]}
        return 404, {}

    def rest_bybit(self, path, query):
        server = self.server
        if path == "/v5/market/instruments-info":
            row = {"symbol": "TESTUSDT", "contractType": "LinearPerpetual", "status": "Trading"}
            return 200, {"retCode": 0, "retMsg": "OK", "result": {"list": [row]}}
        if path == "/v5/market/kline":
            start, end, limit = (int(query[key][0]) for key in ("start", "end", "limit"))
            # Rows newest first; a range wider than the limit keeps only its newest rows.
            minutes = [minute for minute in sorted(server.bybit_bars) if start <= minute <= end]
            minutes = sorted(minutes, reverse=True)[:limit]
            if server.bybit_ascending:
                minutes.reverse()
            rows = [[str(minute), *server.bybit_bars[minute]] for minute in minutes]
            rows = [[*row, "0"] for row in rows]
            return 200, {"retCode": 0, "retMsg": "OK", "result": {"list": rows}}
        return 404, {}

    def respond(self, status, value, retry=False):
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        if retry:
            value = self.server.retry_after
            self.send_header("Retry-After", value() if callable(value) else str(value))
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def websocket(self, path, query):
        with self.server.lock:
            self.server.ws_paths.append(self.path)
        if self.server.venue == "binance":
            assert path == "/stream"
            assert query["streams"] in (["testusdt@trade/testusdt@kline_1m"], ["testusdt@kline_1m"])
        elif self.server.venue == "usdm":
            # Only the routed /market endpoint serves aggTrade and kline; an unrouted connection
            # gets legacy raw prints only.
            assert path in ("/market/stream", "/stream")
            assert query["streams"] in (
                ["testusdt@aggTrade/testusdt@kline_1m"],
                ["testusdt@kline_1m"],
            )
        elif self.server.venue == "okx":
            assert path == "/ws/v5/business"
        else:
            assert path == "/v5/public/linear"
        if "streams" in query:
            with self.server.lock:
                self.server.streams.append(query["streams"][0])
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        with self.server.lock:
            connection_index = self.server.connections
            self.server.connections += 1
            script = self.server.scripts[min(connection_index, len(self.server.scripts) - 1)]
        if self.server.venue == "usdm" and path == "/stream":
            script = [trade(100)]
        if self.server.venue in ("okx", "bybit"):
            self.subscribed(script)
            return
        self.connection.settimeout(3)
        for action in script:
            if isinstance(action, dict):
                frame(self.connection, action)
            elif action[0] == "pause":
                time.sleep(action[1])
            elif action[0] == "ping":
                frame(self.connection, action[1], opcode=9)
                opcode, payload = receive(self.connection)
                assert opcode == 10 and payload == action[1]
                self.server.pongs.append(payload)
            elif action[0] == "fragment":
                payload = json.dumps(action[1], separators=(",", ":")).encode()
                middle = len(payload) // 2
                frame(self.connection, payload[:middle], final=False)
                frame(self.connection, payload[middle:], opcode=0)
            elif action[0] == "binary":
                frame(self.connection, action[1], opcode=2)
            elif action[0] == "raw":
                frame(self.connection, action[1])
            elif action[0] == "revise_trade":
                with self.server.lock:
                    row = list(self.server.trades[action[1]])
                    row[2] = "0.10000001"
                    self.server.trades[action[1]] = tuple(row)
            elif action[0] == "revise_rest":
                with self.server.lock:
                    row = list(self.server.bars[action[1]])
                    row[4] = "0.70000000"
                    self.server.bars[action[1]] = tuple(row)
            elif action[0] == "lag_rest":
                # The REST row trails the WebSocket close, then catches up.
                with self.server.lock:
                    original = self.server.bars[action[1]]
                    self.server.bars[action[1]] = (*original[:4], "0.70000000", *original[5:])

                def restore(minute=action[1], row=original):
                    with self.server.lock:
                        self.server.bars[minute] = row

                threading.Timer(action[2], restore).start()
            elif action[0] == "close":
                frame(self.connection, b"", opcode=8)
                self.close_connection = True
                return
        self.connection.settimeout(0.1)
        while True:
            try:
                receive(self.connection)
            except TimeoutError:
                continue
            except (EOFError, ConnectionResetError):
                break
        self.close_connection = True

    def subscribed(self, script):
        """OKX/Bybit: the client subscribes by message and keeps the socket alive with text pings,
        which a reader thread answers while the script runs."""
        lock = threading.Lock()
        okx = self.server.venue == "okx"

        def send(payload, opcode=1):
            with lock:
                frame(self.connection, payload, opcode)

        self.connection.settimeout(3)
        opcode, payload = receive(self.connection)
        request = json.loads(payload)
        assert opcode == 1 and request["op"] == "subscribe"
        with self.server.lock:
            self.server.subscriptions.append(request)
        for argument in request["args"]:
            if okx:
                send({"event": "subscribe", "arg": argument, "connId": "mock"})
            else:
                send(
                    {
                        "success": True,
                        "ret_msg": "",
                        "conn_id": "mock",
                        "req_id": "",
                        "op": "subscribe",
                    }
                )
        closed = threading.Event()

        def read():
            self.connection.settimeout(0.1)
            while not closed.is_set():
                try:
                    opcode, payload = receive(self.connection)
                except TimeoutError:
                    continue
                except (EOFError, ConnectionResetError, OSError):
                    break
                if opcode == 8:
                    break
                text = payload.decode()
                with self.server.lock:
                    self.server.pings.append(text)
                if okx and text == "ping":
                    send("pong")
                elif not okx and json.loads(text) == {"op": "ping"}:
                    send({"success": True, "ret_msg": "pong", "conn_id": "mock", "op": "ping"})
            closed.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        for action in script:
            if isinstance(action, dict):
                send(action)
            elif action[0] == "pause":
                time.sleep(action[1])
            elif action[0] == "raw":
                send(action[1])
            elif action[0] == "okx_unconfirm":
                # REST trails the WebSocket confirm of this minute for a moment.
                with self.server.lock:
                    self.server.okx_unconfirmed.add(action[1])
                threading.Timer(
                    action[2], self.server.okx_unconfirmed.discard, (action[1],)
                ).start()
            elif action[0] == "revise_okx_trade":
                with self.server.lock:
                    matched, price, _size = self.server.okx_trades[action[1]]
                    self.server.okx_trades[action[1]] = (matched, price, "11")
            elif action[0] == "close":
                send(b"", opcode=8)
                closed.set()
                break
        reader.join(timeout=15)
        closed.set()
        self.close_connection = True


VENUES = {
    "binance": ("binance", "spot", "TESTUSDT"),
    "usdm": ("binance", "usdm", "TESTUSDT"),
    "okx": ("okx", "swap", OKX_SWAP),
    "bybit": ("bybit", "linear", "TESTUSDT"),
}


def sequence(output):
    return [event["seq"] if event["type"] == "tick" else ("time", event["ts"]) for event in output]


class FeedMockTests(unittest.TestCase):
    def test_public_qualification_fails_on_any_action_difference(self):
        for ticks_equal, expected in ((False, 1), (True, 0)):
            soak = Soak.__new__(Soak)
            soak.options = SimpleNamespace(bar_minutes=21, tick_minutes=21, modes="bars,ticks")
            soak.directory = self.directory / str(ticks_equal)
            soak.directory.mkdir()
            soak.results = {}
            soak.receiver = SimpleNamespace(shutdown=lambda: None)
            soak.warmup = lambda: None
            soak.live = lambda mode, minutes, equal=ticks_equal, soak=soak: soak.results.update(
                {mode: {"batch_actions_equal": mode == "bars" or equal}}
            )
            self.assertEqual(soak.run(), expected)

    def test_r_b2_maps_only_tick_action_timestamps(self):
        record = {
            "timestamp": 1791037920021,
            "bar_index": 7,
            "order": {
                "id": "long",
                "action": "entry",
                "leg": "long",
                "contracts": 1.0,
                "price": 84816.0,
                "reduce_only": False,
                "entry_incarnation": 1,
            },
        }
        batch = dict(record, timestamp=1791037920000)
        self.assertEqual(action_key(record, "ticks"), action_key(batch, "ticks"))
        self.assertEqual(action_key(record, "agg-ticks"), action_key(batch, "agg-ticks"))
        self.assertNotEqual(action_key(record, "bars"), action_key(batch, "bars"))
        moved = dict(record, order=dict(record["order"], price=84816.5))
        self.assertNotEqual(action_key(moved, "ticks"), action_key(batch, "ticks"))

    def test_only_actions_on_proven_bars_are_compared(self):
        record = {
            "timestamp": 1791051480451,
            "bar_index": 221,
            "order": {
                "id": "S",
                "action": "sell",
                "leg": "entry",
                "contracts": 1.0,
                "price": 84980.0,
                "reduce_only": False,
                "entry_incarnation": 15,
            },
        }
        open_minute = dict(record, bar_index=222)
        inside, trailing = proven_actions([record, open_minute], "ticks", 222)
        self.assertEqual(inside, [action_key(record, "ticks")])
        self.assertEqual(trailing, 1)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pineforge-feed-mock-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = pathlib.Path(self.temporary.name)

    def server(self, scripts, venue="binance"):
        server = MockServer(scripts, venue)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def command(self, server, mode="bars", extra=(), resume=False):
        port = server.server_address[1]
        venue, market, symbol = VENUES[server.venue]
        arguments = [
            BINARY,
            "run",
            "--venue",
            venue,
            "--market",
            market,
            "--symbol",
            symbol,
            "--mode",
            mode,
            "--state-dir",
            str(self.directory / "state"),
            "--rest-url",
            f"http://127.0.0.1:{port}",
            "--ws-url",
            f"ws://127.0.0.1:{port}",
            "--allow-insecure-http",
        ]
        arguments += ["--resume"] if resume else ["--start", "120000"]
        return arguments + list(extra)

    def run_feed(self, server, mode="bars", extra=(), expected=0, resume=False):
        result = subprocess.run(
            self.command(server, mode, extra, resume), capture_output=True, text=True, timeout=12
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertFalse(server.errors, server.errors)
        for record in result.stderr.splitlines():
            self.assertIsInstance(json.loads(record), dict)
        output = [json.loads(line) for line in result.stdout.splitlines()]
        cursor = self.directory / "state" / "cursor.json"
        if cursor.exists():
            saved = json.loads(cursor.read_text())["cursor"]
            self.assertEqual(
                saved["message_index"],
                len((cursor.parent / "events.jsonl").read_text().splitlines()),
            )
        return output, result

    def test_bars_hole_is_healed_behind_closed_watermark(self):
        server = self.server([[candle(240000)]])
        output, _ = self.run_feed(server, extra=["--max-messages", "3"])
        self.assertEqual([event["bar"]["ts_open"] for event in output], [120000, 180000, 240000])
        self.assertEqual(server.streams, ["testusdt@kline_1m"])

    def test_unhealable_bar_hole_stops_20(self):
        server = self.server([[candle(240000)]])
        del server.bars[180000]
        output, _ = self.run_feed(server, expected=20)
        self.assertEqual([event["bar"]["ts_open"] for event in output], [120000])

    def test_raw_hole_and_reordering_are_healed_not_resequenced(self):
        server = self.server(
            [[trade(102), trade(100), trade(101), trade(103), candle(120000), candle(180000)]]
        )
        output, result = self.run_feed(server, "ticks", ["--max-messages", "7"])
        self.assertEqual(server.streams, ["testusdt@trade/testusdt@kline_1m"])
        self.assertEqual(
            [event["seq"] for event in output if event["type"] == "tick"], [100, 101, 102, 103, 104]
        )
        self.assertEqual(
            [event["ts"] for event in output if event["type"] == "time"], [180000, 240000]
        )
        self.assertIn('"price":10.10000000', result.stdout)
        self.assertEqual(output[0]["ts"], 120001)

    def test_unhealable_raw_hole_stops_20(self):
        server = self.server([[trade(102)]])
        del server.trades[101]
        output, _ = self.run_feed(server, "ticks", expected=20)
        self.assertTrue(all(event["type"] != "time" for event in output))

    def test_identical_duplicates_do_not_change_message_boundaries(self):
        server = self.server([[candle(120000), candle(120000), candle(180000)]])
        output, _ = self.run_feed(server, extra=["--max-messages", "2"])
        self.assertEqual(len(output), 2)

    def test_conflicting_raw_duplicate_stops_21(self):
        server = self.server([[trade(100), trade(100, q="0.20000000")]])
        output, _ = self.run_feed(server, "ticks", expected=21)
        self.assertEqual(len(output), 1)

    def test_revised_already_emitted_bar_stops_21(self):
        server = self.server([[candle(120000), candle(120000, v="0.70000000")]])
        output, _ = self.run_feed(server, expected=21)
        self.assertEqual(len(output), 1)

    def test_ohlcv_mismatch_emits_no_time(self):
        server = self.server([[candle(120000, v="0.70000000")]])
        output, _ = self.run_feed(server, "ticks", expected=21)
        self.assertEqual([event["type"] for event in output], ["tick", "tick", "tick"])

    def test_empty_ambiguous_tick_minute_stops_20(self):
        server = self.server([[candle(120000, n=0, f=-1, L=-1, v="0.00000000")]])
        output, _ = self.run_feed(server, "ticks", expected=20)
        self.assertEqual(output, [])

    def test_forming_candles_never_advance_time(self):
        server = self.server([[candle(120000, confirmed=False), trade(100), candle(120000)]])
        output, _ = self.run_feed(server, "ticks", ["--max-messages", "4"])
        self.assertEqual([event["type"] for event in output], ["tick", "tick", "tick", "time"])

    def test_ping_payload_is_echoed_and_fragmented_text_works(self):
        server = self.server([[("ping", b"synthetic-ping"), ("fragment", candle(120000))]])
        output, _ = self.run_feed(server, extra=["--max-messages", "1"])
        self.assertEqual(len(output), 1)
        self.assertEqual(server.pongs, [b"synthetic-ping"])

    def test_server_shutdown_reconnects_with_exact_overlap(self):
        server = self.server([[candle(120000), retirement()], [candle(120000), candle(180000)]])
        output, _ = self.run_feed(server, extra=["--max-messages", "2"])
        self.assertEqual(len(output), 2)
        self.assertGreaterEqual(server.connections, 2)

    def test_reconnect_changed_rest_overlap_stops_21(self):
        server = self.server(
            [[candle(120000), ("revise_rest", 120000), retirement()], [candle(180000)]]
        )
        output, _ = self.run_feed(server, expected=21)
        self.assertEqual(len(output), 1)

    def test_trailing_rest_row_is_reread_before_a_revision_stop(self):
        server = self.server(
            [[candle(120000), ("lag_rest", 120000, 0.45), retirement()], [candle(180000)]]
        )
        output, result = self.run_feed(server, extra=["--max-messages", "2"])
        self.assertEqual([event["bar"]["ts_open"] for event in output], [120000, 180000])
        self.assertIn("rest_overlap_trailing", result.stderr)

    def test_planned_24_hour_rotation_uses_overlap(self):
        server = self.server([[candle(120000)], [candle(180000)]])
        output, _ = self.run_feed(server, extra=["--max-messages", "2", "--reconnect-seconds", "1"])
        self.assertEqual(len(output), 2)
        self.assertGreaterEqual(server.connections, 2)

    def test_protocol_invalid_symbol_and_binary_stop_23(self):
        missing_count = candle(120000)
        del missing_count["data"]["k"]["n"]
        unknown_on_data_stream = trade(100, e="tradeV2")
        for payload in [
            trade(100, s="OTHERUSDT"),
            ("binary", b"not-a-text-event"),
            ("raw", "{"),
            trade(100, q="-0.10000000"),
            trade(100, p="1e3"),
            missing_count,
            unknown_on_data_stream,
            {"stream": "!notice", "data": {"E": 1}},
        ]:
            with self.subTest(payload=payload):
                server = self.server([[payload]])
                state = self.directory / "state"
                if state.exists():
                    import shutil

                    shutil.rmtree(state)
                self.run_feed(server, "ticks", expected=23)

    def test_rest_access_failure_stops_23_no_key_sent(self):
        for status in (451, 418, 403):
            with self.subTest(status=status):
                state = self.directory / "state"
                if state.exists():
                    import shutil

                    shutil.rmtree(state)
                server = self.server([[candle(240000)]])
                server.fail_status = status
                output, result = self.run_feed(server, expected=23)
                self.assertEqual(output, [])
                self.assertIn(f"HTTP {status}", result.stderr)
                self.assertEqual(server.requests.count("/api/v3/exchangeInfo"), 1)
                self.assertTrue(
                    all(
                        path in {"/stream", "/api/v3/klines", "/api/v3/exchangeInfo"}
                        for path in server.requests
                    )
                )
                self.assertEqual(server.auth_headers, [])

    def test_missing_rate_limit_metadata_is_refused(self):
        server = self.server([[candle(120000)]])
        server.rate_limits = []
        output, _ = self.run_feed(server, expected=23)
        self.assertEqual(output, [])

    def test_excessive_retry_after_stops_instead_of_retrying_early(self):
        server = self.server([[candle(120000)]])
        server.rate_once = True
        server.retry_after = 86401
        output, _ = self.run_feed(server, expected=22)
        self.assertEqual(output, [])

    def test_reconnect_changed_raw_overlap_stops_21(self):
        server = self.server(
            [[trade(100), trade(101), ("revise_trade", 100), retirement()], [trade(102)]]
        )
        output, result = self.run_feed(server, "ticks", expected=21)
        self.assertEqual([event["seq"] for event in output], [100, 101])
        self.assertIn("reconnect raw overlap changed", result.stderr)
        self.assertGreaterEqual(server.connections, 2)

    def test_multi_page_raw_hole_heals_contiguously(self):
        server = self.server([[]])
        server.trades = {99: TRADES[99]}
        for offset in range(2100):
            server.trades[100 + offset] = (120001 + offset * 20, "10.00000000", "0.00000001")
        server.bars = {
            120000: (
                "10.00000000",
                "10.00000000",
                "10.00000000",
                "10.00000000",
                "0.00002100",
                100,
                2199,
                2100,
            )
        }
        last = {
            "stream": "testusdt@trade",
            "data": {
                "e": "trade",
                "E": 162000,
                "s": "TESTUSDT",
                "t": 2199,
                "p": "10.00000000",
                "q": "0.00000001",
                "T": 161981,
                "m": False,
            },
        }
        closing = candle(
            120000,
            o="10.00000000",
            h="10.00000000",
            l="10.00000000",
            c="10.00000000",
            v="0.00002100",
            f=100,
            L=2199,
            n=2100,
        )
        server.scripts = [[last, closing]]
        output, _ = self.run_feed(server, "ticks", ["--max-messages", "2101"])
        self.assertEqual([event["seq"] for event in output[:-1]], list(range(100, 2200)))
        self.assertEqual(output[-1], {"type": "time", "ts": 180000})
        self.assertGreaterEqual(server.requests.count("/api/v3/historicalTrades"), 4)

    def test_matched_time_regression_stops_23(self):
        server = self.server([[trade(100), trade(101, T=120000)]])
        server.trades[101] = (120000, "11.20000000", "0.20000000")
        output, result = self.run_feed(server, "ticks", expected=23)
        self.assertEqual([event["seq"] for event in output], [100])
        self.assertIn("matched time regressed", result.stderr)

    def test_unknown_non_data_event_warns_and_continues(self):
        notice = {"stream": "!venueNotice", "data": {"e": "venueNotice", "E": 1}}
        server = self.server([[notice, candle(120000)]])
        output, result = self.run_feed(server, extra=["--max-messages", "1"])
        self.assertEqual(len(output), 1)
        warnings = [json.loads(record) for record in result.stderr.splitlines()]
        self.assertIn(
            {"stream": "!venueNotice", "type": "venueNotice"},
            [
                {key: record.get(key) for key in ("stream", "type")}
                for record in warnings
                if record["event"] == "unknown_stream_event" and record["level"] == "warn"
            ],
        )

    def test_initial_fence_retries_lagging_rest(self):
        server = self.server([[trade(100), trade(101), trade(102), candle(120000)]])
        server.agg_empty = 1
        output, _ = self.run_feed(server, "ticks", ["--max-messages", "4"])
        self.assertEqual([event["type"] for event in output], ["tick", "tick", "tick", "time"])
        self.assertEqual(server.requests.count("/api/v3/aggTrades"), 2)

    def test_stdio_flags_restored_and_full_stderr_never_cuts_a_record(self):
        server = self.server([[candle(120000)]])
        read_error, write_error = os.pipe()
        read_output, write_output = os.pipe()
        try:
            if hasattr(fcntl, "F_SETPIPE_SZ"):
                fcntl.fcntl(write_error, fcntl.F_SETPIPE_SZ, 4096)
            flags = fcntl.fcntl(write_error, fcntl.F_GETFL)
            fcntl.fcntl(write_error, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            filler = b'{"filler":true}\n'
            try:
                while True:
                    os.write(write_error, filler)
            except BlockingIOError:
                pass
            fcntl.fcntl(write_error, fcntl.F_SETFL, flags)
            process = subprocess.Popen(
                self.command(server, extra=["--max-messages", "1"]),
                stdout=write_output,
                stderr=write_error,
            )
            started = time.monotonic()
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertLess(time.monotonic() - started, 8)
            self.assertFalse(fcntl.fcntl(write_output, fcntl.F_GETFL) & os.O_NONBLOCK)
            self.assertFalse(fcntl.fcntl(write_error, fcntl.F_GETFL) & os.O_NONBLOCK)
            os.close(write_error)
            os.close(write_output)
            write_error = write_output = -1
            with os.fdopen(read_error, "rb") as errors, os.fdopen(read_output, "rb") as output:
                read_error = read_output = -1
                for line in errors.read().split(b"\n")[:-1]:
                    self.assertIsInstance(json.loads(line), dict)
                self.assertEqual(len(output.read().splitlines()), 1)
        finally:
            for descriptor in (read_error, write_error, read_output, write_output):
                if descriptor >= 0:
                    os.close(descriptor)

    def test_aggregate_trade_is_never_substituted_for_raw_prints(self):
        message = trade(100)
        message["data"]["e"] = "aggTrade"
        server = self.server([[message]])
        output, _ = self.run_feed(server, "ticks", expected=23)
        self.assertEqual(output, [])

    def test_sigterm_drain_is_bounded_with_unread_stdout(self):
        messages = []
        for offset in range(2000):
            message = candle(120000)
            minute = 120000 + offset * 60000
            message["data"]["k"]["t"] = minute
            message["data"]["k"]["T"] = minute + 59999
            messages.append(message)
        server = self.server([messages])
        process = subprocess.Popen(
            self.command(server), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            if hasattr(fcntl, "F_SETPIPE_SZ"):
                fcntl.fcntl(process.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            cursor = self.directory / "state" / "cursor.json"
            deadline = time.monotonic() + 20
            previous_index = 0
            stable_since = time.monotonic()
            while time.monotonic() < deadline:
                if cursor.exists():
                    current_index = json.loads(cursor.read_text())["cursor"]["message_index"]
                    if current_index != previous_index:
                        previous_index = current_index
                        stable_since = time.monotonic()
                    elif (
                        30 <= current_index < len(messages)
                        and time.monotonic() - stable_since >= 0.4
                    ):
                        break
                self.assertIsNone(process.poll())
                time.sleep(0.02)
            else:
                self.fail(
                    "unread stdout did not stall before source exhaustion; "
                    f"committed={previous_index}"
                )
            self.assertIsNone(process.poll())
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=7), 0)
            self.assertLess(time.monotonic() - started, 6)
            self.assertGreaterEqual(json.loads(cursor.read_text())["cursor"]["message_index"], 30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_retry_after_seconds_and_http_date_are_honoured(self):
        for retry_after in (2, lambda: email.utils.formatdate(time.time() + 4, usegmt=True)):
            with self.subTest(retry_after=retry_after):
                server = self.server([[candle(120000)]])
                server.rate_once = True
                server.retry_after = retry_after
                state = self.directory / "state"
                if state.exists():
                    import shutil

                    shutil.rmtree(state)
                output, result = self.run_feed(server, extra=["--max-messages", "1"])
                self.assertEqual(len(output), 1)
                self.assertIn("rest_rate_limited", result.stderr)
                limited, retried = [
                    moment
                    for path, moment in server.request_times
                    if path == "/api/v3/exchangeInfo"
                ]
                self.assertGreaterEqual(retried - limited, 1.9)

    def test_429_retry_after_preserves_prefix(self):
        server = self.server([[candle(240000)]])
        server.rate_once = True
        output, result = self.run_feed(server, extra=["--max-messages", "3"])
        self.assertEqual(len(output), 3)
        self.assertIn("rest_rate_limited", result.stderr)

    def test_replay_and_queue_budgets_stop_22(self):
        server = self.server([[candle(120000)]])
        output, _ = self.run_feed(server, extra=["--max-log-bytes", "4"], expected=22)
        self.assertEqual(output, [])
        import shutil

        shutil.rmtree(self.directory / "state")
        server = self.server([[trade(100)]])
        output, _ = self.run_feed(server, "ticks", ["--max-queue-bytes", "64"], expected=22)
        self.assertEqual(output, [])

    def test_sigterm_then_resume_tail_has_no_gap_or_duplicate(self):
        server = self.server([[candle(120000)], [candle(120000), candle(240000)]])
        process = subprocess.Popen(
            self.command(server), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        cursor = self.directory / "state" / "cursor.json"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if cursor.exists() and json.loads(cursor.read_text())["cursor"]["message_index"] == 1:
                break
            self.assertIsNone(process.poll())
            time.sleep(0.02)
        else:
            process.kill()
            self.fail("first atomic message did not commit")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(len(stdout.splitlines()), 1)
        output, _ = self.run_feed(
            server, extra=["--output-from", "1", "--max-messages", "2"], resume=True
        )
        self.assertEqual([event["bar"]["ts_open"] for event in output], [180000, 240000])

    def test_changed_committed_journal_is_refused(self):
        server = self.server([[candle(120000)], [candle(180000)]])
        self.run_feed(server, extra=["--max-messages", "1"])
        journal = self.directory / "state" / "events.jsonl"
        journal.write_text(journal.read_text().replace("10.10000000", "10.20000000"))
        output, _ = self.run_feed(server, resume=True, expected=21)
        self.assertEqual(output, [])

    def reset_state(self):
        import shutil

        shutil.rmtree(self.directory / "state", ignore_errors=True)

    def history_pages(self, server):
        return [
            query
            for path, query in server.queries
            if path == "/api/v5/market/history-trades" and query.get("type") == ["1"]
        ]

    def test_okx_swap_bars_confirm_flag_and_contract_units(self):
        server = self.server([[okx_candle(120000, "0"), okx_candle(180000)]], "okx")
        output, result = self.run_feed(server, extra=["--max-messages", "2"])
        self.assertEqual([event["bar"]["ts_open"] for event in output], [120000, 180000])
        # 60 and 50 contracts of ctVal 0.01: base volumes 0.6 and 0.5; prices stay verbatim.
        self.assertIn('{"ts_open":120000,"o":10.1,"h":11.2,"l":9.9,"c":9.9,"v":0.6}', result.stdout)
        self.assertIn('"v":0.5}', result.stdout)
        self.assertEqual(
            server.subscriptions,
            [{"op": "subscribe", "args": [{"channel": "candle1m", "instId": OKX_SWAP}]}],
        )
        self.assertEqual(server.ws_paths, ["/ws/v5/business"])
        saved = json.loads((self.directory / "state" / "cursor.json").read_text())["cursor"]
        self.assertEqual(saved["qty_multiplier"], "0.01")

    def test_okx_start_predecessor_is_proven_by_the_next_print(self):
        server = self.server([[okx_trades(100), okx_trades(101)]], "okx")
        server.okx_trades[98] = (119998, "10.1", "10")
        server.okx_time_lookup_skip = 1
        output, _ = self.run_feed(server, "ticks", ["--max-messages", "2"])
        self.assertEqual(sequence(output), [100, 101])
        saved = json.loads((self.directory / "state" / "cursor.json").read_text())["cursor"]
        self.assertEqual(saved["predecessor"]["seq"], 99)
        # No print at or after the start is in REST yet: nothing is anchored and the stop is 20.
        self.reset_state()
        server = self.server([[okx_trades(100)]], "okx")
        for identifier in range(100, 106):
            del server.okx_trades[identifier]
        output, result = self.run_feed(server, "ticks", expected=20)
        self.assertEqual(output, [])
        self.assertIn("not yet available", result.stderr)
        saved = json.loads((self.directory / "state" / "cursor.json").read_text())["cursor"]
        self.assertIsNone(saved["predecessor"])

    def test_okx_busy_and_rate_limit_codes_are_retried(self):
        for status, code, event in (
            (400, "50004", "rest_transient_retry"),
            (200, "50011", "rest_rate_limited"),
        ):
            with self.subTest(code=code):
                self.reset_state()
                server = self.server([[okx_candle(120000)]], "okx")
                server.refuse_once = (status, {"code": code, "msg": "synthetic", "data": []})
                output, result = self.run_feed(server, extra=["--max-messages", "1"])
                self.assertEqual(len(output), 1)
                self.assertIn(event, result.stderr)

    def test_bybit_rate_limit_and_server_error_codes_are_retried(self):
        for code, event in ((10006, "rest_rate_limited"), (10016, "rest_transient_retry")):
            with self.subTest(code=code):
                self.reset_state()
                server = self.server([[bybit_push(bybit_row(120000, True))]], "bybit")
                server.refuse_once = (200, {"retCode": code, "retMsg": "synthetic", "result": {}})
                output, result = self.run_feed(server, extra=["--max-messages", "1"])
                self.assertEqual(len(output), 1)
                self.assertIn(event, result.stderr)

    def test_usdm_unknown_symbol_is_refused_at_startup(self):
        server = self.server([[candle(120000)]], "usdm")
        server.usdm_unknown_symbol = True
        output, result = self.run_feed(server, expected=23)
        self.assertEqual(output, [])
        self.assertIn("unknown USD-M symbol", result.stderr)
        self.assertEqual(server.connections, 0)

    def test_usdm_quiet_start_minute_closes_on_the_fence(self):
        rows = {
            500: {"T": 180001, "p": "12.00000000", "q": "0.40000000", "f": 1010, "l": 1012},
            501: {"T": 180050, "p": "11.00000000", "q": "0.10000000", "f": 1013, "l": 1013},
            502: {"T": 240001, "p": "12.00000000", "q": "0.10000000", "f": 1014, "l": 1014},
        }
        server = self.server(
            [
                [
                    candle(120000, v="0.00000000", n=0, f=-1, L=-1),
                    aggregate(500, nq=rows[500]["q"], **rows[500]),
                    aggregate(501, nq=rows[501]["q"], **rows[501]),
                    candle(180000),
                    aggregate(502, nq=rows[502]["q"], **rows[502]),
                ]
            ],
            "usdm",
        )
        server.aggregates = {499: AGGREGATES[499]}
        for identifier, row in rows.items():
            server.aggregates[identifier] = (
                row["T"],
                row["p"],
                row["q"],
                row["q"],
                row["f"],
                row["l"],
            )
        output, _ = self.run_feed(server, "agg-ticks", ["--max-messages", "5"])
        self.assertEqual(sequence(output), [("time", 180000), 500, 501, ("time", 240000), 502])

    def test_websocket_closed_before_any_frame_spends_the_reconnect_budget(self):
        server = self.server([[("close",)]])
        result = subprocess.run(self.command(server), capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 20, result.stderr)
        self.assertIn("reconnect attempts exhausted", result.stderr)
        self.assertGreaterEqual(server.connections, 8)

    def test_okx_websocket_and_rest_lexemes_differ_but_overlap_values_match(self):
        notice = {"event": "notice", "code": "64008", "msg": "upgrade", "connId": "mock"}
        rendered = {120000: ("10.10", "11.20", "9.90", "9.9", "60.0")}
        server = self.server(
            [[okx_candle(120000, candles=rendered), notice], [okx_candle(180000)]], "okx"
        )
        output, result = self.run_feed(server, extra=["--max-messages", "2"])
        self.assertEqual([event["bar"]["ts_open"] for event in output], [120000, 180000])
        # The WebSocket lexemes are emitted verbatim; the REST overlap compares exact values.
        self.assertIn('"o":10.10,"h":11.20,"l":9.90,"c":9.9,"v":0.6}', result.stdout)
        self.assertGreaterEqual(server.connections, 2)

    def test_okx_warmup_rereads_a_rest_row_that_trails_the_websocket_confirm(self):
        server = self.server([[("okx_unconfirm", 180000, 0.35), okx_candle(180000)]], "okx")
        port = server.server_address[1]
        output = self.directory / "warmup.csv"
        command = [
            BINARY,
            "warmup",
            "--venue",
            "okx",
            "--market",
            "swap",
            "--symbol",
            OKX_SWAP,
            "--start",
            "120000",
            "--end",
            "240000",
            "--output",
            str(output),
            "--rest-url",
            f"http://127.0.0.1:{port}",
            "--ws-url",
            f"ws://127.0.0.1:{port}",
            "--allow-insecure-http",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rest_overlap_trailing", result.stderr)
        self.assertEqual(len(output.read_text().splitlines()), 3)

    def test_okx_unconfirmed_rest_candle_is_never_a_bar(self):
        server = self.server([[okx_candle(180000)]], "okx")
        server.okx_unconfirmed = {120000}
        output, _ = self.run_feed(server, expected=20)
        self.assertEqual(output, [])

    def test_okx_ticks_fence_closes_minute_on_exact_candle(self):
        server = self.server(
            [
                [
                    okx_trades(100, 101, 102),
                    okx_candle(120000),
                    okx_trades(103),
                    okx_candle(180000),
                    okx_trades(104),
                    okx_trades(105),
                ]
            ],
            "okx",
        )
        output, result = self.run_feed(server, "ticks", ["--max-messages", "8"])
        self.assertEqual(
            sequence(output), [100, 101, 102, ("time", 180000), 103, 104, ("time", 240000), 105]
        )
        self.assertIn('{"type":"tick","ts":120001,"seq":100,"price":10.1,"qty":0.1}', result.stdout)
        self.assertEqual(
            server.subscriptions[0]["args"][1], {"channel": "trades-all", "instId": OKX_SWAP}
        )

    def test_okx_count_aggregated_push_is_rejected_on_ticks(self):
        for payload in (okx_trades(100, count="3"), okx_trades(100, channel="trades")):
            with self.subTest(payload=payload["arg"]["channel"]):
                self.reset_state()
                server = self.server([[payload]], "okx")
                output, result = self.run_feed(server, "ticks", expected=23)
                self.assertEqual(output, [])
                self.assertRegex(result.stderr, "aggregated OKX trades")

    def test_okx_text_ping_is_answered_and_never_data(self):
        server = self.server([[("pause", 2.5), okx_candle(120000)]], "okx")
        output, _ = self.run_feed(server, extra=["--max-messages", "1", "--keepalive-seconds", "1"])
        self.assertEqual(len(output), 1)
        self.assertGreaterEqual(server.pings.count("ping"), 2)

    def test_okx_tick_gap_heals_by_trade_id_pages(self):
        server = self.server([[]], "okx")
        server.okx_trades = {99: OKX_TRADES[99]}
        for offset in range(250):
            server.okx_trades[100 + offset] = (120001 + offset * 100, "10", "1")
        server.okx_trades[350] = (180001, "10", "1")
        server.okx_candles = {120000: ("10", "10", "10", "10", "250")}

        def push(identifier):
            matched, price, size = server.okx_trades[identifier]
            row = {
                "instId": OKX_SWAP,
                "tradeId": str(identifier),
                "px": price,
                "sz": size,
                "side": "buy",
                "ts": str(matched),
                "source": "0",
            }
            return {"arg": {"channel": "trades-all", "instId": OKX_SWAP}, "data": [row]}

        server.scripts = [[push(349), okx_candle(120000, candles=server.okx_candles), push(350)]]
        output, _ = self.run_feed(server, "ticks", ["--max-messages", "251"])
        self.assertEqual([event["seq"] for event in output[:-1]], list(range(100, 350)))
        self.assertEqual(output[-1], {"type": "time", "ts": 180000})
        self.assertGreaterEqual(len(self.history_pages(server)), 3)
        self.assertTrue(all(page["limit"] == ["100"] for page in self.history_pages(server)))

    def test_okx_inverse_swap_is_refused_before_streaming(self):
        server = self.server([[okx_candle(120000)]], "okx")
        server.instrument = dict(server.instrument, ctType="inverse", ctValCcy="USD")
        output, result = self.run_feed(server, expected=23)
        self.assertEqual(output, [])
        self.assertIn("inverse OKX swaps are refused", result.stderr)
        self.assertEqual(server.connections, 0)

    def test_okx_notice_reconnects_and_changed_overlap_stops_21(self):
        notice = {"event": "notice", "code": "64008", "msg": "upgrade", "connId": "mock"}
        server = self.server(
            [[okx_trades(100, 101), ("revise_okx_trade", 100), notice], [okx_trades(102)]], "okx"
        )
        output, result = self.run_feed(server, "ticks", expected=21)
        self.assertEqual([event["seq"] for event in output], [100, 101])
        self.assertIn("reconnect print overlap changed", result.stderr)
        self.assertGreaterEqual(server.connections, 2)

    def test_okx_gap_beyond_three_month_window_stops_20(self):
        far = 120001 + 90 * 86400000
        server = self.server([[okx_trades(100), okx_trades(105, ts=str(far))]], "okx")
        server.okx_trades[105] = (far, "12", "10")
        output, result = self.run_feed(server, "ticks", expected=20)
        self.assertEqual([event["seq"] for event in output], [100])
        self.assertIn("beyond the venue's REST history window", result.stderr)
        # Only the start predecessor's proof read prints by ID; the gap from 101 was never read.
        self.assertEqual(
            [page for page in self.history_pages(server) if int(page["before"][0]) >= 100], []
        )

    def test_okx_warmup_converts_contract_volume(self):
        server = self.server([[okx_candle(240000)]], "okx")
        port = server.server_address[1]
        output = self.directory / "warmup.csv"
        command = [
            BINARY,
            "warmup",
            "--venue",
            "okx",
            "--market",
            "swap",
            "--symbol",
            OKX_SWAP,
            "--start",
            "120000",
            "--end",
            "240000",
            "--output",
            str(output),
            "--rest-url",
            f"http://127.0.0.1:{port}",
            "--ws-url",
            f"ws://127.0.0.1:{port}",
            "--allow-insecure-http",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            output.read_text().splitlines()[1:],
            ["120000,10.1,11.2,9.9,9.9,0.6", "180000,12,12,11,11,0.5"],
        )
        manifest = json.loads(pathlib.Path(str(output) + ".manifest.json").read_text())
        self.assertEqual(manifest["qty_multiplier"], "0.01")

    def test_bybit_bars_two_entry_push_and_confirm_flag(self):
        server = self.server(
            [
                [
                    bybit_push(bybit_row(120000, False)),
                    bybit_push(bybit_row(120000, True), bybit_row(180000, False)),
                    bybit_push(bybit_row(180000, True), bybit_row(240000, False)),
                ]
            ],
            "bybit",
        )
        output, _ = self.run_feed(server, extra=["--max-messages", "2"])
        self.assertEqual([event["bar"]["ts_open"] for event in output], [120000, 180000])
        self.assertEqual(server.subscriptions, [{"op": "subscribe", "args": ["kline.1.TESTUSDT"]}])
        self.assertEqual(server.ws_paths, ["/v5/public/linear"])

    def test_bybit_tick_modes_are_refused_at_startup(self):
        server = self.server([[]], "bybit")
        for mode in ("ticks", "agg-ticks"):
            with self.subTest(mode=mode):
                output, result = self.run_feed(server, mode, expected=23)
                self.assertEqual(output, [])
                self.assertIn("Bybit tick mode is refused", result.stderr)
                self.assertIn("not a contiguous cursor", result.stderr)
        self.assertEqual(server.requests, [])

    def test_bybit_reverse_order_rest_paging_heals_hole(self):
        server = self.server([[bybit_push(bybit_row(300000, True))]], "bybit")
        output, _ = self.run_feed(server, extra=["--max-messages", "4"])
        self.assertEqual(
            [event["bar"]["ts_open"] for event in output], [120000, 180000, 240000, 300000]
        )
        windows = [query for path, query in server.queries if path == "/v5/market/kline"]
        self.assertTrue(windows)
        for query in windows:
            start, end, limit = (int(query[key][0]) for key in ("start", "end", "limit"))
            self.assertEqual((end - start) // 60000 + 1, limit)
        self.reset_state()
        server = self.server([[bybit_push(bybit_row(300000, True))]], "bybit")
        server.bybit_ascending = True
        output, _ = self.run_feed(server, expected=20)
        self.assertEqual(output, [])

    def test_bybit_json_ping_keepalive(self):
        server = self.server([[("pause", 2.5), bybit_push(bybit_row(120000, True))]], "bybit")
        output, _ = self.run_feed(server, extra=["--max-messages", "1", "--keepalive-seconds", "1"])
        self.assertEqual(len(output), 1)
        self.assertGreaterEqual(server.pings.count('{"op":"ping"}'), 2)

    def test_usdm_bars_use_the_routed_market_stream(self):
        server = self.server([[candle(120000)]], "usdm")
        output, _ = self.run_feed(server, extra=["--max-messages", "1"])
        self.assertEqual(len(output), 1)
        self.assertEqual(server.ws_paths, ["/market/stream?streams=testusdt@kline_1m"])
        self.assertGreater(server.exchange_info_bytes, 1024 * 1024)

    def test_usdm_raw_trade_event_stops_23(self):
        server = self.server([[trade(100)]], "usdm")
        output, result = self.run_feed(server, "agg-ticks", expected=23)
        self.assertEqual(output, [])
        self.assertIn("raw USD-M trades are not a supported source", result.stderr)

    def test_usdm_agg_ticks_one_print_per_aggregate_with_q(self):
        server = self.server(
            [[aggregate(500), aggregate(501), aggregate(502), candle(120000), aggregate(503)]],
            "usdm",
        )
        output, result = self.run_feed(server, "agg-ticks", ["--max-messages", "5"])
        self.assertEqual(sequence(output), [500, 501, 502, ("time", 180000), 503])
        self.assertIn('"seq":500,"price":10.10000000,"qty":0.10000000}', result.stdout)
        self.assertEqual(
            server.ws_paths, ["/market/stream?streams=testusdt@aggTrade/testusdt@kline_1m"]
        )

    def test_usdm_agg_gap_heals_by_from_id(self):
        server = self.server([[aggregate(502), candle(120000), aggregate(503)]], "usdm")
        output, _ = self.run_feed(server, "agg-ticks", ["--max-messages", "5"])
        self.assertEqual(sequence(output), [500, 501, 502, ("time", 180000), 503])
        starts = [
            int(query["fromId"][0])
            for path, query in server.queries
            if path == "/fapi/v1/aggTrades" and "fromId" in query
        ]
        self.assertIn(500, starts)

    def test_usdm_agg_gap_beyond_48_hours_stops_20(self):
        far = 120001 + 49 * 3600000
        server = self.server([[aggregate(500), aggregate(505, T=far)]], "usdm")
        server.aggregates[505] = (far, "12.00000000", "0.10000000", "0.10000000", 1014, 1014)
        output, result = self.run_feed(server, "agg-ticks", expected=20)
        self.assertEqual([event["seq"] for event in output], [500])
        self.assertIn("beyond the venue's REST history window", result.stderr)
        starts = [
            int(query["fromId"][0])
            for path, query in server.queries
            if path == "/fapi/v1/aggTrades" and "fromId" in query
        ]
        self.assertNotIn(501, starts)

    def test_usdm_two_day_window_rejection_maps_to_20(self):
        server = self.server([[aggregate(500), aggregate(503)]], "usdm")
        server.agg_window_error = True
        output, result = self.run_feed(server, "agg-ticks", expected=20)
        self.assertEqual([event["seq"] for event in output], [500])
        self.assertIn("2-day aggTrades window", result.stderr)

    def test_usdm_raw_tick_mode_is_refused(self):
        server = self.server([[]], "usdm")
        output, result = self.run_feed(server, "ticks", expected=23)
        self.assertEqual(output, [])
        self.assertIn("use --mode agg-ticks", result.stderr)
        self.assertEqual(server.requests, [])

    def test_coinbase_is_deferred(self):
        result = subprocess.run(
            [
                BINARY,
                "run",
                "--venue",
                "coinbase",
                "--market",
                "spot",
                "--symbol",
                "BTCUSD",
                "--mode",
                "bars",
                "--state-dir",
                str(self.directory / "state"),
                "--start",
                "120000",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, "")
        self.assertIn("Coinbase is deferred", result.stderr)
        self.assertFalse((self.directory / "state").exists())

    def warmup(self, server):
        port = server.server_address[1]
        output = self.directory / "warmup.csv"
        command = [
            BINARY,
            "warmup",
            "--venue",
            "binance",
            "--market",
            "spot",
            "--symbol",
            "TESTUSDT",
            "--start",
            "120000",
            "--end",
            "240000",
            "--output",
            str(output),
            "--rest-url",
            f"http://127.0.0.1:{port}",
            "--ws-url",
            f"ws://127.0.0.1:{port}",
            "--allow-insecure-http",
        ]
        return output, subprocess.run(command, capture_output=True, text=True, timeout=8)

    def test_warmup_rest_rows_must_equal_confirmed_ws_bars(self):
        output, result = self.warmup(self.server([[candle(180000, v="0.70000000")]]))
        self.assertEqual(result.returncode, 21, result.stderr)
        self.assertFalse(output.exists())
        output, result = self.warmup(self.server([[candle(180000)]]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(pathlib.Path(str(output) + ".manifest.json").read_text())[
                "ws_cross_checked_bars"
            ],
            1,
        )

    def test_warmup_exclusive_cut_manifest_and_tokens(self):
        server = self.server([[candle(240000)]])
        port = server.server_address[1]
        output = self.directory / "warmup.csv"
        command = [
            BINARY,
            "warmup",
            "--venue",
            "binance",
            "--market",
            "spot",
            "--symbol",
            "TESTUSDT",
            "--start",
            "120000",
            "--end",
            "240000",
            "--output",
            str(output),
            "--rest-url",
            f"http://127.0.0.1:{port}",
            "--ws-url",
            f"ws://127.0.0.1:{port}",
            "--allow-insecure-http",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        manifest = json.loads(pathlib.Path(str(output) + ".manifest.json").read_text())
        self.assertEqual(manifest["cut"], 240000)
        self.assertEqual(manifest["bars"], 2)
        self.assertEqual(manifest["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertIn("10.10000000", output.read_text())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FeedMockTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print(f"PASS local HTTP+WS mock venue ({result.testsRun} scenarios)", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
