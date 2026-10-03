# SPDX-License-Identifier: Apache-2.0
"""Synthetic public Binance shapes; standard-library HTTP/WS fault injection."""

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

    def __init__(self, scripts):
        super().__init__(("127.0.0.1", 0), Handler)
        self.scripts = scripts
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
            self.server.request_times.append((parsed.path, time.monotonic()))
            self.server.auth_headers.extend(
                name for name in ("Authorization", "X-MBX-APIKEY") if self.headers.get(name)
            )
        if parsed.path == "/stream":
            self.websocket(query)
            return
        with self.server.lock:
            if self.server.fail_status:
                self.respond(self.server.fail_status, {"code": -1, "msg": "synthetic refusal"})
                return
            if self.server.rate_once:
                self.server.rate_once = False
                self.respond(429, {}, retry=True)
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

    def websocket(self, query):
        assert query["streams"] in (["testusdt@trade/testusdt@kline_1m"], ["testusdt@kline_1m"])
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


class FeedMockTests(unittest.TestCase):
    def test_public_qualification_fails_on_any_action_difference(self):
        for ticks_equal, expected in ((False, 1), (True, 0)):
            soak = Soak.__new__(Soak)
            soak.options = SimpleNamespace(bar_minutes=21, tick_minutes=21)
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

    def server(self, scripts):
        server = MockServer(scripts)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def command(self, server, mode="bars", extra=(), resume=False):
        port = server.server_address[1]
        arguments = [
            BINARY,
            "run",
            "--venue",
            "binance",
            "--market",
            "spot",
            "--symbol",
            "TESTUSDT",
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
