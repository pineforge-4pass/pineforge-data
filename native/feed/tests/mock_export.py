# SPDX-License-Identifier: Apache-2.0
"""Synthetic export scenarios: the real binary against the mock USD-M venue and daily archives."""

import hashlib
import importlib
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import UTC, datetime
from decimal import Decimal, getcontext

# mock_venue reads the binary from sys.argv[1] at import time.
sys.argv = [sys.argv[0], sys.argv[1]]
mock_venue = importlib.import_module("mock_venue")
BINARY = mock_venue.BINARY
getcontext().prec = 200
SYMBOL = "TESTUSDT"
HEADER = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"


def recent_minute(minutes_ago):
    return (int(time.time() * 1000) // 60000 - minutes_ago) * 60000


def canonical(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def expected_bars(prints, start, end, warmup_close=None):
    """The tick-built bar rule, independently: prints are (id, ts, price, qty) in ID order. A
    quiet first minute carries the runner's last warmup close."""
    close = warmup_close
    rows = ["timestamp,open,high,low,close,volume"]
    for minute in range(start, end, 60000):
        inside = [p for p in prints if minute <= p[1] < minute + 60000]
        if not inside:
            rows.append(f"{minute},{close},{close},{close},{close},0")
            continue
        high = max(inside, key=lambda p: Decimal(p[2]))[2]
        low = min(inside, key=lambda p: Decimal(p[2]))[2]
        volume = sum((Decimal(p[3]) for p in inside), Decimal(0))
        close = inside[-1][2]
        rows.append(f"{minute},{inside[0][2]},{high},{low},{close},{canonical(volume)}")
    return "\n".join(rows) + "\n"


def synthetic(first_id, start, minutes, per_minute, quiet=()):
    """Prints over `minutes` minutes from `start`, none in the `quiet` minute offsets."""
    prints = []
    identifier = first_id
    for offset in range(minutes):
        if offset in quiet:
            continue
        for index in range(per_minute):
            ts = start + offset * 60000 + index * (59999 // max(per_minute - 1, 1))
            price = f"{100 + (identifier * 37) % 50}.{(identifier * 13) % 100:02d}"
            quantity = f"0.{(identifier * 104729) % 99999999 + 1:08d}"
            prints.append((identifier, ts, price, quantity))
            identifier += 1
    return prints


class ExportMockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pineforge-feed-export-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = pathlib.Path(self.temporary.name)
        self.output = self.directory / "bars.csv"

    def server(self, prints):
        server = mock_venue.MockServer([[]], "usdm")
        server.aggregates = {
            identifier: (
                ts,
                price,
                quantity,
                quantity,
                1000 + 2 * identifier,
                1001 + 2 * identifier,
            )
            for identifier, ts, price, quantity in prints
        }
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def export(self, start, end, server=None, extra=(), expected=0, venue=("binance", "usdm")):
        port = server.server_address[1] if server else 9
        arguments = [
            BINARY,
            "export",
            "--venue",
            venue[0],
            "--market",
            venue[1],
            "--symbol",
            SYMBOL,
        ]
        arguments += ["--mode", "agg-ticks" if venue[1] == "usdm" else "ticks"]
        arguments += ["--start", str(start), "--end", str(end), "--output", str(self.output)]
        arguments += [
            "--rest-url",
            f"http://127.0.0.1:{port}",
            "--ws-url",
            f"ws://127.0.0.1:{port}",
        ]
        arguments += ["--allow-insecure-http", *extra]
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, expected, result.stderr)
        if expected:
            self.assertFalse(self.output.exists())
            self.assertFalse(pathlib.Path(str(self.output) + ".manifest.json").exists())
        return result

    def manifest(self):
        manifest = json.loads(pathlib.Path(str(self.output) + ".manifest.json").read_text())
        self.assertEqual(manifest["sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(manifest["schema"], "pineforge-feed-export/v1")
        self.assertIn("floor(ts/60000)", manifest["rule"])
        return manifest

    def test_rest_bars_follow_the_rule_with_quiet_minute_and_carry(self):
        start = recent_minute(30)
        prints = [
            (499, start - 1, "10.10000000", "0.10000000"),
            (500, start, "10.10000000", "0.10000000"),
            (501, start + 50, "11.20000000", "0.20000000"),
            (502, start + 59999, "9.90000000", "0.30000000"),
            (503, start + 120001, "12.00000000", "0.40000000"),
            (504, start + 120050, "11.00000000", "0.10000000"),
            (505, start + 180000, "11.50000000", "0.00100000"),
            (506, start + 240001, "12.00000000", "0.10000000"),
            (507, start + 240050, "12.10000000", "0.20000000"),
        ]
        server = self.server(prints)
        self.export(start, start + 240000, server)
        text = self.output.read_text()
        self.assertEqual(text, expected_bars(prints, start, start + 240000))
        self.assertIn(f"\n{start + 60000},9.90000000,9.90000000,9.90000000,9.90000000,0\n", text)
        self.assertIn(f"\n{start},10.10000000,11.20000000,9.90000000,9.90000000,0.6\n", text)
        manifest = self.manifest()
        self.assertEqual(
            {key: manifest[key] for key in ("venue", "market", "symbol", "mode")},
            {"venue": "binance", "market": "usdm", "symbol": SYMBOL, "mode": "agg-ticks"},
        )
        port = server.server_address[1]
        self.assertEqual(manifest["source"], {"kind": "rest", "origin": f"http://127.0.0.1:{port}"})
        self.assertEqual((manifest["start"], manifest["end_exclusive"]), (start, start + 240000))
        self.assertEqual(
            manifest["predecessor"], {"id": 499, "ts": start - 1, "price": "10.10000000"}
        )
        self.assertEqual(manifest["fence"], {"id": 506, "ts": start + 240001})
        self.assertEqual((manifest["first_id"], manifest["last_id"]), (500, 505))
        self.assertEqual(
            (manifest["prints"], manifest["bars"], manifest["quiet_minutes"]), (6, 4, 1)
        )

    def warmup(self, start, close):
        path = self.directory / "warmup.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n"
            f"{start - 120000},9,11,8,10,1\n{start - 60000},10,10.5,9.5,{close},2\n"
        )
        return path

    def test_rest_start_survives_a_quiet_first_hour(self):
        # The first aggregate after --start lies in the second hour window: the start lookup
        # searches forward up to the clock instead of stopping 20 after one window. The quiet
        # first minutes carry the warmup close (10.2), not the predecessor's 10.10000000, as the
        # runner carries its last warmup bar; the quiet later minute carries the last print.
        start = recent_minute(90)
        end = start + 73 * 60000
        prints = [(499, start - 1, "10.10000000", "0.10000000")]
        prints += synthetic(500, start + 70 * 60000, 4, 4, quiet=(1,))
        server = self.server(prints)
        warmup = self.warmup(start, "10.2")
        self.export(start, end, server, extra=["--warmup", str(warmup)])
        text = self.output.read_text()
        self.assertEqual(text, expected_bars(prints, start, end, warmup_close="10.2"))
        self.assertIn(f"\n{start},10.2,10.2,10.2,10.2,0\n", text)
        last = [p for p in prints if p[1] < start + 71 * 60000][-1][2]
        self.assertIn(f"\n{start + 71 * 60000},{last},{last},{last},{last},0\n", text)
        manifest = self.manifest()
        self.assertEqual((manifest["quiet_minutes"], manifest["warmup_close"]), (71, "10.2"))
        windows = [
            int(query["startTime"][0])
            for path, query in server.queries
            if path == "/fapi/v1/aggTrades" and "startTime" in query
        ]
        self.assertIn(start + 3600000, windows)

    def test_quiet_first_minute_without_the_warmup_close_stops(self):
        start = recent_minute(30)
        prints = [(499, start - 1, "10.10000000", "0.10000000")]
        prints += synthetic(500, start + 60000, 2, 3)
        server = self.server(prints)
        result = self.export(start, start + 120000, server, expected=20)
        self.assertIn("--warmup", result.stderr)
        warmup = self.warmup(start + 60000, "10.2")
        result = self.export(
            start, start + 120000, server, extra=["--warmup", str(warmup)], expected=20
        )
        self.assertIn("minute before the start", result.stderr)
        warmup.write_text("timestamp,open,high,low,close,volume\n" + f"{start - 60000},1,1,1,x,1\n")
        self.export(start, start + 120000, server, extra=["--warmup", str(warmup)], expected=23)
        # A first minute with prints never reads the warmup close.
        self.export(start + 60000, start + 120000, server)
        self.assertEqual(
            self.output.read_text(), expected_bars(prints, start + 60000, start + 120000)
        )
        self.assertIsNone(self.manifest()["warmup_close"])

    def test_rest_pages_by_from_id_and_sums_volume_exactly(self):
        start = recent_minute(40)
        prints = synthetic(1000, start - 60000, 6, 700, quiet=(2,))
        server = self.server(prints)
        self.export(start, start + 240000, server)
        self.assertEqual(self.output.read_text(), expected_bars(prints, start, start + 240000))
        pages = [q for path, q in server.queries if path == "/fapi/v1/aggTrades" and "fromId" in q]
        self.assertGreaterEqual(len([q for q in pages if q["limit"] == ["1000"]]), 3)
        manifest = self.manifest()
        self.assertEqual((manifest["prints"], manifest["quiet_minutes"]), (2100, 1))
        self.assertEqual(manifest["fence"]["id"], manifest["last_id"] + 1)

    def test_missing_fence_stops_20_and_writes_nothing(self):
        start = recent_minute(20)
        prints = [p for p in synthetic(1, start - 60000, 4, 5) if p[1] < start + 120000]
        server = self.server(prints)
        result = self.export(start, start + 180000, server, expected=20)
        self.assertIn("fence cannot be proven", result.stderr)

    def test_hole_stops_20_and_time_regression_stops_23(self):
        start = recent_minute(20)
        prints = synthetic(1, start - 60000, 5, 5)
        server = self.server([p for p in prints if p[0] != 9])
        result = self.export(start, start + 180000, server, expected=20)
        self.assertIn("nonunit aggregate ID", result.stderr)
        # ID 12 is dated before ID 11 (the first print of its minute).
        regressed = [(i, start + 50000 if i == 12 else ts, p, q) for i, ts, p, q in prints]
        server = self.server(regressed)
        result = self.export(start, start + 180000, server, expected=23)
        self.assertIn("backwards", result.stderr)

    def test_start_beyond_retention_or_open_window_stops_20(self):
        start = recent_minute(49 * 60)
        server = self.server(synthetic(1, start - 60000, 3, 3))
        result = self.export(start, start + 60000, server, expected=20)
        self.assertIn("REST print history window", result.stderr)
        self.assertNotIn("/fapi/v1/aggTrades", server.requests)
        result = self.export(recent_minute(1), recent_minute(-5), server, expected=20)
        self.assertIn("not closed yet", result.stderr)

    def test_existing_output_is_refused(self):
        self.output.write_text("keep\n")
        start = recent_minute(10)
        arguments = [BINARY, "export", "--venue", "binance", "--market", "usdm", "--symbol", SYMBOL]
        arguments += ["--mode", "agg-ticks", "--start", str(start), "--end", str(start + 60000)]
        arguments += ["--output", str(self.output), "--rest-url", "http://127.0.0.1:9"]
        arguments += ["--ws-url", "ws://127.0.0.1:9", "--allow-insecure-http"]
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.output.read_text(), "keep\n")

    def archive_prints(self):
        start = recent_minute(24 * 60)
        prints = synthetic(70000, start - 120000, 9, 400, quiet=(4,))
        return start, prints

    def write_archive(self, name, prints, microseconds=False, header=True):
        lines = [HEADER] if header else []
        for identifier, ts, price, quantity in prints:
            when = ts * 1000 + 456 if microseconds else ts
            raw = 2 * identifier
            maker = "true" if identifier % 3 else "false"
            lines.append(f"{identifier},{price},{quantity},{raw},{raw + 1},{when},{maker}")
        text = "\n".join(lines) + "\n"
        path = self.directory / name
        if name.endswith(".zip"):
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(name[:-4] + ".csv", text)
        else:
            path.write_text(text)
        return path

    def checksum(self, archive, digest=None, name=None):
        digest = digest or hashlib.sha256(archive.read_bytes()).hexdigest()
        path = self.directory / (archive.name + ".CHECKSUM")
        path.write_text(f"{digest}  {name or archive.name}\n")
        return path

    def test_archive_csv_and_zip_match_the_rule(self):
        start, prints = self.archive_prints()
        end = start + 300000
        expected = expected_bars(prints, start, end)
        csv_archive = self.write_archive(
            f"{SYMBOL}-aggTrades-day.csv", prints, microseconds=True, header=False
        )
        checksum = self.checksum(csv_archive)
        self.export(start, end, extra=["--archive", str(csv_archive), "--checksum", str(checksum)])
        self.assertEqual(self.output.read_text(), expected)
        manifest = self.manifest()
        self.assertEqual(manifest["source"]["checksum_verified"], True)
        self.output.unlink()
        pathlib.Path(str(self.output) + ".manifest.json").unlink()
        zipped = self.write_archive(f"{SYMBOL}-aggTrades-day.zip", prints)
        checksum = self.checksum(zipped)
        self.export(start, end, extra=["--archive", str(zipped), "--checksum", str(checksum)])
        self.assertEqual(self.output.read_text(), expected)
        manifest = self.manifest()
        digest = hashlib.sha256(zipped.read_bytes()).hexdigest()
        self.assertEqual(
            manifest["source"],
            {"kind": "archive", "file": zipped.name, "sha256": digest, "checksum_verified": True},
        )
        predecessor = [p for p in prints if p[1] < start][-1]
        fence = next(p for p in prints if p[1] >= end)
        self.assertEqual(
            manifest["predecessor"],
            {"id": predecessor[0], "ts": predecessor[1], "price": predecessor[2]},
        )
        self.assertEqual(manifest["fence"], {"id": fence[0], "ts": fence[1]})
        self.assertEqual(manifest["quiet_minutes"], 1)

    def test_archive_without_checksum_warns(self):
        start, prints = self.archive_prints()
        archive = self.write_archive(f"{SYMBOL}-aggTrades-day.zip", prints)
        result = self.export(start, start + 120000, extra=["--archive", str(archive)])
        self.assertIn("archive_not_checksum_verified", result.stderr)
        self.assertEqual(self.manifest()["source"]["checksum_verified"], False)

    def test_archive_checksum_mismatch_wrong_name_and_corruption_stop_21(self):
        start, prints = self.archive_prints()
        archive = self.write_archive(f"{SYMBOL}-aggTrades-day.zip", prints)
        tampered = self.checksum(archive, digest="0" * 64)
        result = self.export(
            start,
            start + 120000,
            extra=["--archive", str(archive), "--checksum", str(tampered)],
            expected=21,
        )
        self.assertIn("does not match", result.stderr)
        renamed = self.checksum(archive, name=f"{SYMBOL}-aggTrades-other.zip")
        result = self.export(
            start,
            start + 120000,
            extra=["--archive", str(archive), "--checksum", str(renamed)],
            expected=21,
        )
        self.assertIn("not the archive", result.stderr)
        csv_archive = self.write_archive(f"{SYMBOL}-aggTrades-day.csv", prints)
        tampered = self.checksum(csv_archive, digest="f" * 64)
        result = self.export(
            start,
            start + 120000,
            extra=["--archive", str(csv_archive), "--checksum", str(tampered)],
            expected=21,
        )
        self.assertIn("does not match", result.stderr)
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0x55
        archive.write_bytes(bytes(data))
        result = self.export(start, start + 120000, extra=["--archive", str(archive)], expected=21)
        self.assertRegex(result.stderr, "deflate|CRC-32|recorded size")
        self.assertNotIn("export_written", result.stderr)

    def test_archive_day_boundaries_name_the_missing_daily_file(self):
        # A window from 00:00 UTC has its predecessor in the previous day's file, and one to 24:00
        # has its fence in the next day's: export reads one daily file and says which one is needed.
        day = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)
        prints = synthetic(1000, day, 3, 4) + synthetic(1012, day + 86400000 - 180000, 3, 4)
        archive = self.write_archive(f"{SYMBOL}-aggTrades-2026-10-02.csv", prints)
        result = self.export(day, day + 120000, extra=["--archive", str(archive)], expected=20)
        self.assertIn(f"{SYMBOL}-aggTrades-2026-10-01.csv", result.stderr)
        result = self.export(
            day + 86400000 - 120000,
            day + 86400000,
            extra=["--archive", str(archive)],
            expected=20,
        )
        self.assertIn(f"{SYMBOL}-aggTrades-2026-10-03.csv", result.stderr)
        # A window reaching past the day, or starting before its first print, names the same files.
        result = self.export(
            day + 86400000 - 120000,
            day + 86400000 + 600000,
            extra=["--archive", str(archive)],
            expected=20,
        )
        self.assertIn(f"{SYMBOL}-aggTrades-2026-10-03.csv", result.stderr)
        late = self.write_archive(f"{SYMBOL}-aggTrades-2026-10-02.zip", prints[5:])
        result = self.export(day + 60000, day + 120000, extra=["--archive", str(late)], expected=20)
        self.assertIn(f"{SYMBOL}-aggTrades-2026-10-01.zip", result.stderr)

    def test_archive_outside_window_and_unsupported_venue(self):
        start, prints = self.archive_prints()
        archive = self.write_archive(f"{SYMBOL}-aggTrades-day.csv", prints)
        self.export(start - 600000, start, extra=["--archive", str(archive)], expected=20)
        self.export(start + 360000, start + 900000, extra=["--archive", str(archive)], expected=20)
        result = self.export(
            start,
            start + 60000,
            extra=["--archive", str(archive)],
            expected=23,
            venue=("binance", "spot"),
        )
        self.assertIn("--venue binance --market usdm --mode agg-ticks", result.stderr)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ExportMockTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print(f"PASS mock export ({result.testsRun} scenarios)", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
