# SPDX-License-Identifier: Apache-2.0
"""Explicit public-data soak; captures stay in the operator-selected directory."""

import argparse
import hashlib
import http.server
import itertools
import json
import os
import pathlib
import signal
import struct
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
from decimal import Decimal, getcontext


def rows(path, exact=False):
    with open(path) as source:
        return [
            json.loads(line, parse_float=Decimal if exact else float)
            for line in source
            if line.strip()
        ]


def journal_rows(state, exact=False):
    """The feed's retained durable journal: segment files in message-index order."""
    result = []
    for path in sorted((pathlib.Path(state) / "journal").glob("*.jsonl")):
        result += rows(path, exact)
    return result


SCRIPT_TF_MS = 60000  # the runner and batch both run --script-tf 1


def action_key(record, mode):
    """Comparable action fields. Ticks use the predeclared R-B2 mapping: an action timestamp
    becomes floor(ts / script_tf) * script_tf on both sides, because a tick fill carries its
    print's time while the OHLC batch carries the modeled segment's clock. Every other field
    stays exact."""
    order = record["order"]
    timestamp = record["timestamp"]
    if mode != "bars":
        timestamp = timestamp // SCRIPT_TF_MS * SCRIPT_TF_MS
    return (
        timestamp,
        record["bar_index"],
        order["id"],
        order["action"],
        order["leg"],
        struct.pack("!d", float(order["contracts"])).hex(),
        struct.pack("!d", float(order["price"])).hex(),
        order["reduce_only"],
        order["entry_incarnation"],
    )


def proven_actions(records, mode, window):
    """Runner actions on the bars the batch replays, and how many fell after them. The batch
    sees only proven minutes; ticks of a minute without its `time` event can still drive
    actions in the runner, and those have no batch counterpart."""
    inside = [action_key(record, mode) for record in records if record["bar_index"] < window]
    return inside, len(records) - len(inside)


AGENT = {"User-Agent": "pineforge-feed-qualification/0.1"}


def canonical(value):
    """The feed's contract-to-base token: fixed point, no exponent, no trailing fractional zeros."""
    return format(value.normalize(), "f")


# (venue, market) -> public REST origin, the feed's tick mode (none for Bybit), runner label
PROFILES = {
    ("binance", "spot"): ("https://api.binance.com", "ticks", "BINANCE:{}"),
    ("binance", "usdm"): ("https://fapi.binance.com", "agg-ticks", "BINANCE:{}.P"),
    ("okx", "spot"): ("https://www.okx.com", "ticks", "OKX:{}"),
    ("okx", "swap"): ("https://www.okx.com", "ticks", "OKX:{}"),
    ("bybit", "spot"): ("https://api.bybit.com", None, "BYBIT:{}"),
    ("bybit", "linear"): ("https://api.bybit.com", None, "BYBIT:{}.P"),
}


class Venue:
    """Public REST truth for one venue and market, in the feed's normalized units."""

    def __init__(self, venue, market, symbol):
        self.venue, self.market, self.symbol = venue, market, symbol
        self.rest, self.tick_mode, label = PROFILES[(venue, market)]
        self.label = label.format(symbol)
        self.lock = threading.Lock()
        # USD-M aggregates can straddle a minute boundary: their kline is not their exact sum.
        self.candle_is_print_sum = (venue, market) != ("binance", "usdm")
        self.multiplier = Decimal(1)
        if venue == "okx" and market == "swap":
            row = self.fetch("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})[
                "data"
            ][0]
            assert row["ctType"] == "linear"
            self.multiplier = Decimal(row["ctVal"]) * Decimal(row["ctMult"])

    def fetch(self, path, query, weight=2):
        with self.lock:
            time.sleep(weight / 10)
            url = self.rest + path + "?" + urllib.parse.urlencode(query)
            request = urllib.request.Request(url, headers=AGENT)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read(), parse_float=Decimal)

    def quantity(self, token):
        return token if self.multiplier == 1 else canonical(Decimal(token) * self.multiplier)

    def candles(self, start, end):
        """Closed 1m candles in [start, end): ts, the five tokens and the raw-trade count when the
        feed's tick proof uses it (Binance spot only)."""
        result = []
        while start < end:
            if self.venue == "okx":
                page = self.fetch(
                    "/api/v5/market/history-candles",
                    {
                        "instId": self.symbol,
                        "bar": "1m",
                        "after": min(end, start + 100 * 60000),
                        "before": start - 1,
                        "limit": 100,
                    },
                )["data"][::-1]
                assert all(row[8] == "1" for row in page), "unconfirmed REST candle"
                page = [(int(row[0]), [*row[1:5], self.quantity(row[5])], None) for row in page]
            elif self.venue == "bybit":
                count = min(1000, (end - start) // 60000)
                page = self.fetch(
                    "/v5/market/kline",
                    {
                        "category": self.market,
                        "symbol": self.symbol,
                        "interval": 1,
                        "start": start,
                        "end": start + (count - 1) * 60000,
                        "limit": count,
                    },
                )["result"]["list"][::-1]
                page = [(int(row[0]), row[1:6], None) for row in page]
            else:
                path = "/fapi/v1/klines" if self.market == "usdm" else "/api/v3/klines"
                page = self.fetch(
                    path,
                    {
                        "symbol": self.symbol,
                        "interval": "1m",
                        "startTime": start,
                        "endTime": end - 1,
                        "limit": 1000,
                    },
                    5,
                )
                spot = self.market == "spot"
                page = [(row[0], row[1:6], row[8] if spot else None) for row in page]
            assert page and page[0][0] == start, "REST minute missing"
            result += page
            start = page[-1][0] + 60000
        assert [row[0] for row in result] == list(range(result[0][0], end, 60000))
        return result

    def prints(self, first):
        """One REST page of contiguous prints from `first`: id, ts and the price/qty tokens."""
        if self.venue == "okx":
            page = self.fetch(
                "/api/v5/market/history-trades",
                {
                    "instId": self.symbol,
                    "type": 1,
                    "after": first + 100,
                    "before": first - 1,
                    "limit": 100,
                },
            )["data"][::-1]
            page = [
                (int(row["tradeId"]), int(row["ts"]), row["px"], self.quantity(row["sz"]))
                for row in page
            ]
        elif self.market == "usdm":
            page = self.fetch(
                "/fapi/v1/aggTrades", {"symbol": self.symbol, "fromId": first, "limit": 1000}, 20
            )
            page = [(row["a"], row["T"], row["p"], row["q"]) for row in page]
        else:
            page = self.fetch(
                "/api/v3/historicalTrades",
                {"symbol": self.symbol, "fromId": first, "limit": 1000},
                25,
            )
            page = [(row["id"], row["time"], row["price"], row["qty"]) for row in page]
        assert page and page[0][0] == first, "REST print page missing"
        return page


class Receiver(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, directory):
        super().__init__(("127.0.0.1", 0), ReceiverHandler)
        self.directory = directory
        self.lock = threading.Lock()
        self.payloads = {}
        threading.Thread(target=self.serve_forever, daemon=True).start()


class ReceiverHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        name = self.path.removeprefix("/actions/")
        if not name.replace("-", "").isalnum():
            self.send_error(404)
            return
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.payloads.setdefault(name, []).append(payload)
            with open(self.server.directory / (name + "-actions.jsonl"), "a") as output:
                output.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_):
        pass


class Soak:
    def __init__(self, options):
        self.options = options
        self.venue = Venue(options.venue, options.market, options.symbol)
        self.directory = pathlib.Path(options.directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.receiver = Receiver(self.directory)
        self.cut = int(time.time() * 1000) // 60000 * 60000
        self.results = {}
        self.lock = threading.Lock()

    def receipt(self, message):
        with self.lock:
            print(message, flush=True)
            with open(self.directory / "receipts.log", "a") as output:
                output.write(message + "\n")

    def runner_command(self, mode, name, ledger, source, cursor=0):
        command = [
            self.options.runner,
            "run",
            "--strategy",
            self.options.strategy,
            "--warmup",
            str(self.directory / "warmup.csv"),
            "--feed",
            source,
            "--mode",
            "bars" if mode == "bars" else "ticks",
            "--script-tf",
            "1",
            "--ledger",
            str(ledger),
            "--symbol",
            self.venue.label,
            "--name",
            "public-rsi-qualification",
            "--webhook-url",
            f"http://127.0.0.1:{self.receiver.server_port}/actions/{name}",
            "--allow-insecure-http",
        ]
        if cursor:
            command += ["--from-input", str(cursor)]
        return command

    def warmup(self):
        command = [
            self.options.feed,
            "warmup",
            "--venue",
            self.venue.venue,
            "--market",
            self.venue.market,
            "--symbol",
            self.venue.symbol,
            "--start",
            str(self.cut - 200 * 60000),
            "--end",
            str(self.cut),
            "--output",
            str(self.directory / "warmup.csv"),
        ]
        with open(self.directory / "warmup.stderr", "w") as error:
            subprocess.run(command, stderr=error, check=True, timeout=150)
        manifest = json.loads((self.directory / "warmup.csv.manifest.json").read_text())
        assert manifest["end_exclusive"] == self.cut
        assert (
            manifest["sha256"]
            == hashlib.sha256((self.directory / "warmup.csv").read_bytes()).hexdigest()
        )
        self.receipt("PASS warmup exclusive cut and SHA-256")

    def generation(self, mode, generation, cursor):
        command = [
            self.options.feed,
            "run",
            "--venue",
            self.venue.venue,
            "--market",
            self.venue.market,
            "--symbol",
            self.venue.symbol,
            "--mode",
            mode,
            "--state-dir",
            str(self.directory / (mode + "-state")),
        ]
        command += (
            ["--resume", "--output-from", str(cursor)] if generation else ["--start", str(self.cut)]
        )
        files = [
            os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            for name in (
                f"{mode}-feed-{generation}.stderr",
                f"{mode}-runner-{generation}.stderr",
                f"{mode}-runner-{generation}.stdout",
            )
        ]
        feed_error, runner_error, runner_output = files
        runner = subprocess.Popen(
            self.runner_command(mode, mode, self.directory / (mode + ".sqlite3"), "-", cursor),
            stdin=subprocess.PIPE,
            stdout=runner_output,
            stderr=runner_error,
        )
        feed = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=feed_error)
        failures = []

        def pump():
            try:
                with open(self.directory / (mode + "-feed.jsonl"), "ab", buffering=0) as capture:
                    for line in feed.stdout:
                        if not line.endswith(b"\n"):
                            continue
                        runner.stdin.write(line)
                        runner.stdin.flush()
                        capture.write(line)
            except Exception as failure:
                failures.append(str(failure))
            finally:
                runner.stdin.close()

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return feed, runner, thread, failures, files

    def finish(self, mode, generation, processes, crash=False):
        feed, runner, thread, failures, files = processes
        if feed.poll() is None:
            feed.send_signal(signal.SIGKILL if crash else signal.SIGTERM)
        code = feed.wait(timeout=20)
        thread.join(timeout=30)
        assert not thread.is_alive(), "pipe drain exceeded deadline"
        runner_code = runner.wait(timeout=40)
        for descriptor in files:
            os.close(descriptor)
        assert code == (-signal.SIGKILL if crash else 0), f"feed exit={code}"
        assert runner_code == 0, f"runner exit={runner_code}"
        assert not failures, failures
        summary = json.loads((self.directory / f"{mode}-runner-{generation}.stdout").read_text())
        return summary["inputs_committed"]

    @staticmethod
    def settled(check, attempts=6, pause=5):
        """REST rows of the newest minutes and prints can trail the feed's WebSocket by seconds:
        a REST disagreement must persist across spaced re-reads (about 25 s) before it fails."""
        for attempt in range(attempts):
            try:
                return check()
            except AssertionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(pause)

    def validate(self, mode, duration, cursor, minutes):
        getcontext().prec = 160
        tape = rows(self.directory / (mode + "-feed.jsonl"), True)
        with open(self.directory / (mode + "-feed.jsonl")) as source:
            tokens = [
                json.loads(line, parse_float=str, parse_int=str) for line in source if line.strip()
            ]
        assert len(tape) == cursor, "capture/runner cursor mismatch"
        durable = journal_rows(self.directory / (mode + "-state"), True)
        assert tape == durable, "restart changed normalized prefix or message boundaries"
        self.receipt(f"PASS {mode} restart without gap or duplicate messages={cursor}")
        if mode == "bars":
            normalized = [event["bar"] for event in tape]
            assert len(normalized) >= minutes - 1 and duration >= (minutes - 1) * 60

            def compare_bars():
                expected = self.venue.candles(self.cut, normalized[-1]["ts_open"] + 60000)
                for index, (actual, candle) in enumerate(zip(normalized, expected, strict=True)):
                    keys = ("o", "h", "l", "c", "v")
                    assert [actual["ts_open"], *(actual[key] for key in keys)] == [
                        candle[0],
                        *(Decimal(token) for token in candle[1]),
                    ], "bar REST mismatch"
                    assert tape[index]["type"] == "bar"
                    # OKX renders one candle number differently on its WebSocket ("100.0") and
                    # REST ("100"); values are compared exactly above, lexemes where they agree.
                    if self.venue.venue != "okx":
                        assert [tokens[index]["bar"][key] for key in keys] == candle[1], (
                            "bar decimal tokens changed"
                        )

            self.settled(compare_bars)
            self.receipt(
                f"PASS bars equal REST minutes={len(normalized)} duration_seconds={duration:.3f}"
            )
        else:
            assert duration >= (minutes - 1) * 60
            trades = [event for event in tape if event["type"] == "tick"]
            trade_tokens = [event for event in tokens if event["type"] == "tick"]
            times = [event["ts"] for event in tape if event["type"] == "time"]
            assert len(times) >= minutes - 1 and trades
            for previous, current in itertools.pairwise(trades):
                assert current["seq"] == previous["seq"] + 1 and current["ts"] >= previous["ts"]

            def compare_prints():
                next_id = trades[0]["seq"]
                trade_offset = 0
                while trade_offset < len(trades):
                    for identifier, matched, price, quantity in self.venue.prints(next_id):
                        if trade_offset == len(trades):
                            break
                        tick = trades[trade_offset]
                        assert [tick["seq"], tick["ts"], tick["price"], tick["qty"]] == [
                            identifier,
                            matched,
                            Decimal(price),
                            Decimal(quantity),
                        ], "print REST mismatch"
                        assert [trade_tokens[trade_offset][key] for key in ("price", "qty")] == [
                            price,
                            quantity,
                        ], "print decimal tokens changed"
                        trade_offset += 1
                        next_id += 1

            def compare_minutes():
                expected = self.venue.candles(self.cut, times[-1])
                differing.clear()
                normalized = []
                minute_trades = []
                minute = self.cut
                for event in tape:
                    if event["type"] == "tick":
                        assert minute <= event["ts"] < minute + 60000, "time/print ordering"
                        minute_trades.append(event)
                    else:
                        assert event["type"] == "time" and event["ts"] == minute + 60000
                        candle = expected[len(normalized)]
                        prices = [tick["price"] for tick in minute_trades]
                        if not prices:
                            # A quiet minute closes on the next-print fence (OKX, USD-M) with a
                            # zero-volume candle; the batch then sees that flat candle.
                            assert candle[2] is None, "a proven minute without prints"
                            assert not self.venue.candle_is_print_sum or (
                                Decimal(candle[1][4]) == 0
                            ), "a quiet minute with candle volume"
                            values = [*(Decimal(token) for token in candle[1][:4]), Decimal(0)]
                        else:
                            assert candle[2] is None or len(prices) == candle[2], "count mismatch"
                            values = [
                                prices[0],
                                max(prices),
                                min(prices),
                                prices[-1],
                                sum(tick["qty"] for tick in minute_trades),
                            ]
                        if self.venue.candle_is_print_sum:
                            assert values == [Decimal(token) for token in candle[1]], (
                                "time OHLCV mismatch"
                            )
                        elif values != [Decimal(token) for token in candle[1]]:
                            differing.append(minute)
                        keys = ("ts_open", "o", "h", "l", "c", "v")
                        normalized.append(dict(zip(keys, [minute, *values], strict=True)))
                        minute += 60000
                        minute_trades = []
                return normalized

            differing = []
            self.settled(compare_prints)
            normalized = self.settled(compare_minutes)
            if self.venue.candle_is_print_sum:
                self.receipt(
                    f"PASS {mode} contiguous and time events equal REST ticks={len(trades)} "
                    f"minutes={len(times)} duration_seconds={duration:.3f}"
                )
            else:
                # Every print equals REST aggTrades and every time is fenced by the next print; the
                # kline is reported, not required, where a straddling aggregate moves a fill.
                self.receipt(
                    f"PASS {mode} contiguous fenced aggregates equal REST ticks={len(trades)} "
                    f"minutes={len(times)} kline_differs={len(differing)} "
                    f"duration_seconds={duration:.3f}"
                )
        combined = self.directory / (mode + "-combined.csv")
        with open(combined, "w") as output:
            output.write((self.directory / "warmup.csv").read_text())
            for bar in normalized:
                output.write(
                    ",".join(str(bar[key]) for key in ("ts_open", "o", "h", "l", "c", "v")) + "\n"
                )
        batch_actions = self.directory / (mode + "-batch-actions.jsonl")
        subprocess.run(
            [
                self.options.batch_probe,
                self.options.observed_strategy,
                str(combined),
                str(batch_actions),
            ],
            check=True,
        )
        expected_actions = [
            action_key(record, mode)
            for record in rows(batch_actions)
            if record["origin_input_index"] >= 200
        ]
        actual_actions, trailing = proven_actions(
            self.receiver.payloads.get(mode, []), mode, 200 + len(normalized)
        )
        event_ids = [record["event_id"] for record in self.receiver.payloads.get(mode, [])]
        assert len(event_ids) == len(set(event_ids)), "duplicate delivered action after restart"
        equal = expected_actions == actual_actions
        (self.directory / (mode + "-action-comparison.json")).write_text(
            json.dumps(
                {"equal": equal, "batch": expected_actions, "runner": actual_actions}, indent=2
            )
            + "\n"
        )
        mapping = " (R-B2 timestamps)" if mode != "bars" else ""
        verdict = "PASS" if equal else "FAIL"
        self.receipt(
            f"{verdict} {mode} actions equal batch via run_backtest_full{mapping} "
            f"batch={len(expected_actions)} runner={len(actual_actions)} "
            f"after_last_proven_minute={trailing}"
        )
        if mode != "bars" and not self.venue.candle_is_print_sum:
            self.kline_batch(mode, normalized, actual_actions, len(differing))
        exported = None
        if mode != "bars" and getattr(self.options, "export", False):
            exported = self.export_check(mode, normalized, actual_actions)
        if mode != "bars":
            name = mode + "-replay"
            completed = subprocess.run(
                self.runner_command(
                    mode,
                    name,
                    self.directory / (name + ".sqlite3"),
                    str(self.directory / (mode + "-feed.jsonl")),
                ),
                capture_output=True,
                text=True,
                timeout=600,
            )
            (self.directory / (name + ".stdout")).write_text(completed.stdout)
            (self.directory / (name + ".stderr")).write_text(completed.stderr)
            assert completed.returncode == 0, "tick replay runner failed"
            raw = [action_key(record, "bars") for record in self.receiver.payloads.get(mode, [])]
            assert raw == [
                action_key(record, "bars") for record in self.receiver.payloads.get(name, [])
            ]
            self.receipt(f"PASS {mode} same-print runner replay actions equal actions={len(raw)}")
        return {
            "export": exported,
            "duration_seconds": duration,
            "messages": cursor,
            "minutes": len(normalized),
            "actions": len(actual_actions),
            "actions_after_last_proven_minute": trailing,
            "batch_actions_equal": equal,
        }

    def export_check(self, mode, normalized, actual_actions):
        """`pineforge-feed export` over the proven window, from venue REST: its prints-built bars
        must equal the bars the runner built from the same prints (the tape's minutes), value for
        value, and the batch over the exported CSV must equal the runner's actions."""
        end = normalized[-1]["ts_open"] + 60000
        output = self.directory / (mode + "-export.csv")
        command = [
            self.options.feed,
            "export",
            "--venue",
            self.venue.venue,
            "--market",
            self.venue.market,
            "--symbol",
            self.venue.symbol,
            "--mode",
            mode,
            "--start",
            str(self.cut),
            "--end",
            str(end),
            "--output",
            str(output),
        ]
        with open(self.directory / (mode + "-export.stderr"), "w") as error:
            subprocess.run(command, stderr=error, check=True, timeout=600)
        with open(output) as source:
            lines = source.read().splitlines()
        assert lines[0] == "timestamp,open,high,low,close,volume"
        exported = [
            dict(
                zip(
                    ("ts_open", "o", "h", "l", "c", "v"),
                    [int(row[0]), *(Decimal(v) for v in row[1:])],
                    strict=True,
                )
            )
            for row in (line.split(",") for line in lines[1:])
        ]
        assert exported == normalized, "exported bars differ from the runner's prints-built bars"
        manifest = json.loads((self.directory / (mode + "-export.csv.manifest.json")).read_text())
        combined = self.directory / (mode + "-export-combined.csv")
        with open(combined, "w") as target:
            target.write((self.directory / "warmup.csv").read_text())
            target.write("\n".join(lines[1:]) + "\n")
        actions = self.directory / (mode + "-export-batch-actions.jsonl")
        subprocess.run(
            [self.options.batch_probe, self.options.observed_strategy, str(combined), str(actions)],
            check=True,
        )
        expected = [
            action_key(record, mode)
            for record in rows(actions)
            if record["origin_input_index"] >= 200
        ]
        equal = expected == actual_actions
        self.receipt(
            f"{'PASS' if equal else 'FAIL'} {mode} export prints-built bars equal the runner's "
            f"tick-built bars minutes={len(exported)} prints={manifest['prints']} "
            f"quiet_minutes={manifest['quiet_minutes']} "
            f"batch_over_export_actions={len(expected)} runner={len(actual_actions)}"
        )
        assert equal, "the batch over the exported bars differs from the runner"
        return {"minutes": len(exported), "prints": manifest["prints"], "actions": len(expected)}

    def kline_batch(self, mode, normalized, actual_actions, kline_differs):
        """Information, not a gate: the same strategy over Binance klines instead of the bars built
        from the prints. Aggregates can straddle a minute edge, so fills there can differ."""
        candles = self.venue.candles(self.cut, normalized[-1]["ts_open"] + 60000)
        combined = self.directory / (mode + "-kline-combined.csv")
        with open(combined, "w") as output:
            output.write((self.directory / "warmup.csv").read_text())
            for candle in candles:
                output.write(",".join([str(candle[0]), *candle[1]]) + "\n")
        actions = self.directory / (mode + "-kline-batch-actions.jsonl")
        subprocess.run(
            [self.options.batch_probe, self.options.observed_strategy, str(combined), str(actions)],
            check=True,
        )
        expected = [
            action_key(record, mode)
            for record in rows(actions)
            if record["origin_input_index"] >= 200
        ]
        differ = sum(
            1 for left, right in zip(expected, actual_actions, strict=False) if left != right
        )
        differ += abs(len(expected) - len(actual_actions))
        self.receipt(
            f"INFO {mode} batch over Binance klines: actions={len(expected)} "
            f"differ_from_runner={differ} kline_differs={kline_differs}"
        )

    def live(self, mode, minutes):
        processes = None
        try:
            started = time.monotonic()
            processes = self.generation(mode, 0, 0)
            self.receipt(
                f"START {mode} public {self.venue.venue} {self.venue.market} {self.venue.symbol} "
                f"cut={self.cut} minimum_minutes={minutes}"
            )
            time.sleep(self.options.restart_seconds)
            assert processes[0].poll() is None and processes[1].poll() is None, (
                "process stopped before restart"
            )
            cursor = self.finish(mode, 0, processes, crash=True)
            processes = self.generation(mode, 1, cursor)
            self.receipt(f"RESTART {mode} --output-from={cursor} --from-input={cursor}")
            while time.monotonic() - started < minutes * 60:
                assert processes[0].poll() is None and processes[1].poll() is None, (
                    "live process stopped early"
                )
                time.sleep(5)
            cursor = self.finish(mode, 1, processes)
            processes = None
            duration = time.monotonic() - started
            self.results[mode] = self.validate(mode, duration, cursor, minutes)
        except Exception:
            if processes:
                for process in processes[:2]:
                    if process.poll() is None:
                        process.terminate()
                for process in processes[:2]:
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            failure = traceback.format_exc()
            (self.directory / (mode + "-failure.txt")).write_text(failure)
            self.receipt(f"FAIL {mode} qualification; see {mode}-failure.txt")
            self.results[mode] = {"error": failure}

    def run(self):
        self.warmup()
        threads = [
            threading.Thread(
                target=self.live,
                args=(
                    mode,
                    self.options.bar_minutes if mode == "bars" else self.options.tick_minutes,
                ),
            )
            for mode in self.options.modes.split(",")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.receiver.shutdown()
        (self.directory / "summary.json").write_text(json.dumps(self.results, indent=2) + "\n")
        failed = any(
            "error" in result or not result.get("batch_actions_equal")
            for result in self.results.values()
        )
        return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for argument in ("feed", "runner", "strategy", "observed-strategy", "batch-probe", "directory"):
        parser.add_argument("--" + argument, required=True)
    parser.add_argument("--venue", default="binance")
    parser.add_argument("--market", default="spot")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument(
        "--modes", default="bars,ticks", help="bars, ticks or agg-ticks, comma-separated"
    )
    parser.add_argument("--bar-minutes", type=int, default=46)
    parser.add_argument("--tick-minutes", type=int, default=21)
    parser.add_argument("--restart-seconds", type=int, default=180)
    parser.add_argument(
        "--export", action="store_true", help="also check `pineforge-feed export` over tick windows"
    )
    raise SystemExit(Soak(parser.parse_args()).run())
