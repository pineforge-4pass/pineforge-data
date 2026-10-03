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


SCRIPT_TF_MS = 60000  # the runner and batch both run --script-tf 1


def action_key(record, mode):
    """Comparable action fields. Ticks use the predeclared R-B2 mapping: an action timestamp
    becomes floor(ts / script_tf) * script_tf on both sides, because a tick fill carries its
    print's time while the OHLC batch carries the modeled segment's clock. Every other field
    stays exact."""
    order = record["order"]
    timestamp = record["timestamp"]
    if mode == "ticks":
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
            mode,
            "--script-tf",
            "1",
            "--ledger",
            str(ledger),
            "--symbol",
            "BINANCE:BTCUSDT",
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
            "binance",
            "--market",
            "spot",
            "--symbol",
            "BTCUSDT",
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
            "binance",
            "--market",
            "spot",
            "--symbol",
            "BTCUSDT",
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

    def fetch(self, path, query, weight=2):
        time.sleep(weight / 10)
        url = "https://api.binance.com" + path + "?" + urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read(), parse_float=Decimal)

    def candles(self, start, end):
        result = []
        while start < end:
            page = self.fetch(
                "/api/v3/klines",
                {
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": start,
                    "endTime": end - 1,
                    "limit": 1000,
                },
            )
            assert page and page[0][0] == start, "REST minute missing"
            result += page
            start = page[-1][0] + 60000
        assert len(result) * 60000 == end - result[0][0]
        return result

    def validate(self, mode, duration, cursor, minutes):
        getcontext().prec = 160
        tape = rows(self.directory / (mode + "-feed.jsonl"), True)
        with open(self.directory / (mode + "-feed.jsonl")) as source:
            tokens = [
                json.loads(line, parse_float=str, parse_int=str) for line in source if line.strip()
            ]
        assert len(tape) == cursor, "capture/runner cursor mismatch"
        durable = rows(self.directory / (mode + "-state/events.jsonl"), True)
        assert tape == durable, "restart changed normalized prefix or message boundaries"
        self.receipt(f"PASS {mode} restart without gap or duplicate messages={cursor}")
        if mode == "bars":
            normalized = [event["bar"] for event in tape]
            assert len(normalized) >= minutes - 1 and duration >= (minutes - 1) * 60
            expected = self.candles(self.cut, normalized[-1]["ts_open"] + 60000)
            for index, (actual, candle) in enumerate(zip(normalized, expected, strict=True)):
                assert [actual["ts_open"], *(actual[key] for key in ("o", "h", "l", "c", "v"))] == [
                    candle[0],
                    *(Decimal(candle[index]) for index in range(1, 6)),
                ], "bar REST mismatch"
                assert tape[index]["type"] == "bar"
                assert [tokens[index]["bar"][key] for key in ("o", "h", "l", "c", "v")] == candle[
                    1:6
                ], "bar decimal tokens changed"
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
            next_id = trades[0]["seq"]
            trade_offset = 0
            while trade_offset < len(trades):
                page = self.fetch(
                    "/api/v3/historicalTrades",
                    {"symbol": "BTCUSDT", "fromId": next_id, "limit": 1000},
                    25,
                )
                assert page and page[0]["id"] == next_id
                for raw in page:
                    if trade_offset == len(trades):
                        break
                    tick = trades[trade_offset]
                    assert [tick["seq"], tick["ts"], tick["price"], tick["qty"]] == [
                        raw["id"],
                        raw["time"],
                        Decimal(raw["price"]),
                        Decimal(raw["qty"]),
                    ], "raw REST mismatch"
                    assert [trade_tokens[trade_offset][key] for key in ("price", "qty")] == [
                        raw["price"],
                        raw["qty"],
                    ], "raw decimal tokens changed"
                    trade_offset += 1
                    next_id += 1
            expected = self.candles(self.cut, times[-1])
            normalized = []
            minute_trades = []
            minute = self.cut
            for event in tape:
                if event["type"] == "tick":
                    assert minute <= event["ts"] < minute + 60000, "time/print ordering violation"
                    minute_trades.append(event)
                else:
                    assert event["type"] == "time" and event["ts"] == minute + 60000
                    raw = expected[len(normalized)]
                    prices = [tick["price"] for tick in minute_trades]
                    assert prices and len(prices) == raw[8]
                    values = [
                        prices[0],
                        max(prices),
                        min(prices),
                        prices[-1],
                        sum(tick["qty"] for tick in minute_trades),
                    ]
                    assert values == [Decimal(raw[index]) for index in range(1, 6)], (
                        "time OHLCV mismatch"
                    )
                    normalized.append(
                        dict(
                            zip(
                                ("ts_open", "o", "h", "l", "c", "v"), [minute, *values], strict=True
                            )
                        )
                    )
                    minute += 60000
                    minute_trades = []
            self.receipt(
                f"PASS ticks contiguous and time events equal REST ticks={len(trades)} "
                f"minutes={len(times)} duration_seconds={duration:.3f}"
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
        actual_actions = [
            action_key(record, mode) for record in self.receiver.payloads.get(mode, [])
        ]
        event_ids = [record["event_id"] for record in self.receiver.payloads.get(mode, [])]
        assert len(event_ids) == len(set(event_ids)), "duplicate delivered action after restart"
        equal = expected_actions == actual_actions
        (self.directory / (mode + "-action-comparison.json")).write_text(
            json.dumps(
                {"equal": equal, "batch": expected_actions, "runner": actual_actions}, indent=2
            )
            + "\n"
        )
        mapping = " (R-B2 timestamps)" if mode == "ticks" else ""
        verdict = "PASS" if equal else "FAIL"
        self.receipt(
            f"{verdict} {mode} actions equal batch via run_backtest_full{mapping} "
            f"batch={len(expected_actions)} runner={len(actual_actions)}"
        )
        if mode == "ticks":
            name = "ticks-replay"
            completed = subprocess.run(
                self.runner_command(
                    mode,
                    name,
                    self.directory / "ticks-replay.sqlite3",
                    str(self.directory / "ticks-feed.jsonl"),
                ),
                capture_output=True,
                text=True,
                timeout=600,
            )
            (self.directory / "ticks-replay.stdout").write_text(completed.stdout)
            (self.directory / "ticks-replay.stderr").write_text(completed.stderr)
            assert completed.returncode == 0, "tick replay runner failed"
            raw = [action_key(record, "bars") for record in self.receiver.payloads.get(mode, [])]
            assert raw == [
                action_key(record, "bars") for record in self.receiver.payloads.get(name, [])
            ]
            self.receipt(
                f"PASS ticks same-print runner replay actions equal actions={len(actual_actions)}"
            )
        return {
            "duration_seconds": duration,
            "messages": cursor,
            "minutes": len(normalized),
            "actions": len(actual_actions),
            "batch_actions_equal": equal,
        }

    def live(self, mode, minutes):
        processes = None
        try:
            started = time.monotonic()
            processes = self.generation(mode, 0, 0)
            self.receipt(f"START {mode} public BTCUSDT cut={self.cut} minimum_minutes={minutes}")
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
            threading.Thread(target=self.live, args=("bars", self.options.bar_minutes)),
            threading.Thread(target=self.live, args=("ticks", self.options.tick_minutes)),
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
    parser.add_argument("--bar-minutes", type=int, default=46)
    parser.add_argument("--tick-minutes", type=int, default=21)
    parser.add_argument("--restart-seconds", type=int, default=180)
    raise SystemExit(Soak(parser.parse_args()).run())
