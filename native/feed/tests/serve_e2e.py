# SPDX-License-Identifier: Apache-2.0
"""Explicit public-data soak through `serve`: a producer, runners on --feed-url, cursor resumes."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import time
import traceback
import urllib.request
from decimal import Decimal

from public_e2e import Soak, action_key, journal_rows, proven_actions, rows


def ledger_inputs(path):
    with sqlite3.connect(path) as ledger:
        return [
            row[0]
            for row in ledger.execute("SELECT canonical_json FROM inputs ORDER BY input_index")
        ]


class ServeSoak(Soak):
    def __init__(self, options):
        options.modes = options.mode
        options.bar_minutes = options.tick_minutes = options.minutes
        super().__init__(options)
        self.mode = options.mode
        self.state = self.directory / (self.mode + "-state")
        self.producer = None
        self.port = None
        self.epoch = None
        self.generation = 0
        self.runners = {}

    def start_producer(self, resume):
        command = [
            self.options.feed,
            "serve",
            "--venue",
            self.venue.venue,
            "--market",
            self.venue.market,
            "--symbol",
            self.venue.symbol,
            "--mode",
            self.mode,
            "--state-dir",
            str(self.state),
            "--listen",
            "127.0.0.1:0",
        ]
        command += ["--resume"] if resume else ["--start", str(self.cut)]
        error = self.directory / f"producer-{self.generation}.stderr"
        with open(error, "w") as log:
            self.producer = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for line in error.read_text().splitlines():
                record = json.loads(line)
                if record["event"] == "serve_listening":
                    self.port = record["port"]
                    status = self.status()
                    assert self.epoch in (None, status["epoch"]), (
                        "the resumed producer changed the epoch"
                    )
                    self.epoch = status["epoch"]
                    self.generation += 1
                    return status
            assert self.producer.poll() is None, f"producer exited {self.producer.returncode}"
            time.sleep(0.2)
        raise AssertionError("producer never listened")

    def status(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/v1/status", timeout=10
        ) as response:
            return json.loads(response.read())

    def start_runner(self, name, cursor):
        state = self.runners.setdefault(name, {"generations": 0, "exits": []})
        generation = state["generations"]
        url = f"ws://127.0.0.1:{self.port}/v1/stream?epoch={self.epoch}&from={cursor}"
        command = self.runner_command(
            self.mode, name, self.directory / (name + ".sqlite3"), "-", cursor
        )
        index = command.index("--feed")
        command[index : index + 2] = ["--feed-url", url]
        prefix = self.directory / f"{name}-runner-{generation}"
        with open(f"{prefix}.stdout", "w") as output, open(f"{prefix}.stderr", "w") as error:
            state["process"] = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=output, stderr=error
            )
        state["generations"] += 1
        return url

    def wait_runner(self, name, timeout=60):
        state = self.runners[name]
        code = state["process"].wait(timeout=timeout)
        state["exits"].append(code)
        return code, len(ledger_inputs(self.directory / (name + ".sqlite3")))

    def run(self):
        try:
            self.warmup()
            started = time.monotonic()
            self.start_producer(False)
            names = [
                f"{self.mode}" if index == 0 else f"{self.mode}-r{index + 1}"
                for index in range(self.options.runners)
            ]
            for name in names:
                self.start_runner(name, 0)
            self.receipt(
                f"START serve {self.mode} public {self.venue.venue} {self.venue.market} "
                f"{self.venue.symbol} cut={self.cut} runners={len(names)} "
                f"minimum_minutes={self.options.minutes}"
            )
            events = []
            if len(names) > 1:
                events.append((self.options.runner_restart_seconds, "runner"))
            if self.options.producer_kill_seconds:
                events.append((self.options.producer_kill_seconds, "producer"))
            for moment, kind in sorted(events):
                while time.monotonic() - started < moment:
                    assert self.producer.poll() is None, "producer stopped early"
                    for name in names:
                        assert self.runners[name]["process"].poll() is None, (
                            f"runner {name} stopped early"
                        )
                    time.sleep(1)
                if kind == "runner":
                    name = names[-1]
                    self.runners[name]["process"].send_signal(signal.SIGTERM)
                    code, cursor = self.wait_runner(name)
                    assert code == 130, f"runner {name} exit={code}"
                    self.start_runner(name, cursor)
                    self.receipt(f"RESTART runner {name} --from-input={cursor} from={cursor}")
                else:
                    self.producer.send_signal(signal.SIGKILL)
                    assert self.producer.wait(timeout=20) == -signal.SIGKILL
                    cursors = {}
                    for name in names:
                        code, cursors[name] = self.wait_runner(name)
                        assert code == 1, f"runner {name} exit={code} after the producer was killed"
                    status = self.start_producer(True)
                    for name in names:
                        self.start_runner(name, cursors[name])
                    self.receipt(
                        "RESUME producer --resume epoch-unchanged next_index="
                        f"{status['retained']['next_index']} "
                        + " ".join(
                            f"{name}:--from-input={cursor}" for name, cursor in cursors.items()
                        )
                    )
            while time.monotonic() - started < self.options.minutes * 60:
                assert self.producer.poll() is None, "producer stopped early"
                for name in names:
                    assert self.runners[name]["process"].poll() is None, (
                        f"runner {name} stopped early"
                    )
                time.sleep(5)
            final = self.status()
            self.producer.send_signal(signal.SIGTERM)
            assert self.producer.wait(timeout=30) == 0, "producer did not stop cleanly"
            cursors = {name: self.wait_runner(name)[1] for name in names}
            duration = time.monotonic() - started
            journal = journal_rows(self.state, True)
            assert final["retained"]["first_index"] == 0
            assert len(journal) >= final["retained"]["next_index"]
            for name in names:
                ledger = [
                    json.loads(text, parse_float=Decimal)
                    for text in ledger_inputs(self.directory / (name + ".sqlite3"))
                ]
                assert ledger == journal[: len(ledger)], (
                    f"runner {name} ledger differs from the journal prefix"
                )
                assert len(ledger) == len(journal), (
                    f"runner {name} committed {len(ledger)} of {len(journal)}"
                )
                self.receipt(
                    f"PASS serve {self.mode} runner {name} ledger equals the journal "
                    f"messages={len(ledger)} generations={self.runners[name]['generations']} "
                    "(no loss, no duplicate)"
                )
            idle = [
                path.name
                for path in self.directory.glob("*-runner-*.stderr")
                if "idle or message timeout" in path.read_text()
            ]
            assert not idle, f"runner idle deadline tripped: {idle}"
            self.receipt(
                f"PASS serve {self.mode} quiet periods never tripped the runner idle deadline "
                f"runner_generations={sum(state['generations'] for state in self.runners.values())}"
            )
            # The journal is the tape: validate it like the stdout path (REST, batch, replay).
            tape = self.directory / (self.mode + "-feed.jsonl")
            with open(tape, "w") as output:
                for path in sorted((self.state / "journal").glob("*.jsonl")):
                    output.write(path.read_text())
            result = self.validate(self.mode, duration, len(journal), self.options.minutes)
            batch = [
                action_key(record, self.mode)
                for record in rows(self.directory / (self.mode + "-batch-actions.jsonl"))
                if record["origin_input_index"] >= 200
            ]
            window = 200 + result["minutes"]
            for name in names[1:]:
                actual, trailing = proven_actions(
                    self.receiver.payloads.get(name, []), self.mode, window
                )
                equal = actual == batch
                result.setdefault("runners", {})[name] = {
                    "batch_actions_equal": equal,
                    "actions": len(actual),
                }
                result["batch_actions_equal"] = result["batch_actions_equal"] and equal
                self.receipt(
                    f"{'PASS' if equal else 'FAIL'} serve {self.mode} runner {name} actions equal "
                    f"batch batch={len(batch)} runner={len(actual)} "
                    f"after_last_proven_minute={trailing}"
                )
            self.results[self.mode] = result
        except Exception:
            failure = traceback.format_exc()
            (self.directory / "serve-failure.txt").write_text(failure)
            self.receipt(f"FAIL serve {self.mode} qualification; see serve-failure.txt")
            self.results[self.mode] = {"error": failure}
        finally:
            if self.producer and self.producer.poll() is None:
                self.producer.kill()
            for state in self.runners.values():
                if state.get("process") and state["process"].poll() is None:
                    state["process"].kill()
            self.receiver.shutdown()
        (self.directory / "summary.json").write_text(
            json.dumps(self.results, indent=2, default=str) + "\n"
        )
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
    parser.add_argument("--mode", default="bars")
    parser.add_argument("--minutes", type=int, default=17)
    parser.add_argument("--runners", type=int, default=2)
    parser.add_argument("--runner-restart-seconds", type=int, default=240)
    parser.add_argument("--producer-kill-seconds", type=int, default=480)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(ServeSoak(parser.parse_args()).run())
