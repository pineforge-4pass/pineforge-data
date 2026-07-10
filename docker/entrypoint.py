#!/usr/bin/env python3
"""Container entrypoint: raw Pine + normalized OHLCV -> JSON backtest report."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

from pineforge_codegen import transpile
from pineforge_codegen.errors import CompileError

from pineforge_data import (
    BacktestOptions,
    Bar,
    Instrument,
    MagnifierDistribution,
    PineForgeBacktestRunner,
)
from pineforge_data.backtest import JsonValue

WORK = Path("/work")
PREFIX = Path(os.environ.get("PINEFORGE_PREFIX", "/opt/pineforge"))


def _load_request() -> dict[str, object]:
    value = json.loads((WORK / "request.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request.json must contain an object")
    return cast(dict[str, object], value)


def _load_bars(path: Path, instrument: Instrument, source: str) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bars.append(
                Bar(
                    instrument,
                    int(row["timestamp"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                    source,
                )
            )
    return bars


def _read_commit(name: str) -> str:
    return (PREFIX / name).read_text(encoding="utf-8").strip()


def _compile(generated: Path, strategy_library: Path) -> float:
    command = [
        "g++",
        "-std=c++17",
        "-O2",
        "-ffp-contract=off",
        "-fPIC",
        "-shared",
        f"-I{PREFIX / 'include'}",
        "-I/usr/include/eigen3",
        str(generated),
        "-Wl,--whole-archive",
        str(PREFIX / "lib/libpineforge.a"),
        "-Wl,--no-whole-archive",
        "-o",
        str(strategy_library),
    ]
    started = time.perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown compiler error"
        raise RuntimeError(f"strategy compilation failed:\n{detail}")
    return elapsed


def main() -> int:
    pine_path = WORK / "strategy.pine"
    ohlcv_path = WORK / "ohlcv.csv"
    if not pine_path.is_file() or not ohlcv_path.is_file():
        print(
            "error: /work must contain strategy.pine, ohlcv.csv, and request.json",
            file=sys.stderr,
        )
        return 2

    try:
        request = _load_request()
        instrument_data = cast(dict[str, object], request["instrument"])
        options_data = cast(dict[str, object], request["options"])
        instrument = Instrument(
            symbol=str(instrument_data["symbol"]),
            venue=str(instrument_data.get("venue", "")),
            timezone=str(instrument_data.get("timezone", "UTC")),
            session=str(instrument_data.get("session", "24x7")),
            volume_unit=str(instrument_data.get("volume_unit", "base")),
        )
        bars = _load_bars(ohlcv_path, instrument, str(request.get("source", "provider")))
        options = BacktestOptions(
            input_timeframe=str(options_data.get("input_timeframe", "")),
            script_timeframe=str(options_data.get("script_timeframe", "")),
            bar_magnifier=bool(options_data.get("bar_magnifier", False)),
            magnifier_samples=int(options_data.get("magnifier_samples", 4)),
            magnifier_distribution=MagnifierDistribution(
                int(options_data.get("magnifier_distribution", 3))
            ),
            trace_enabled=bool(options_data.get("trace_enabled", False)),
            chart_timezone=cast(str | None, options_data.get("chart_timezone")),
        )
        strategy_params = cast(dict[str, JsonValue], request.get("strategy_params", {}))
        pine_source = pine_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory(prefix="pineforge-") as temporary:
            temp = Path(temporary)
            generated = temp / "strategy.cpp"
            strategy_library = temp / "strategy.so"
            transpile_started = time.perf_counter()
            generated_source = transpile(pine_source, filename=pine_path.name)
            transpile_seconds = time.perf_counter() - transpile_started
            generated.write_text(generated_source, encoding="utf-8")
            compile_seconds = _compile(generated, strategy_library)
            report = PineForgeBacktestRunner.load(strategy_library).run(
                bars,
                instrument=instrument,
                options=options,
                strategy_params=strategy_params,
            )

        output: dict[str, object] = {
            "runtime": {
                "data_source_digest": _read_commit("DATA_SOURCE_DIGEST"),
                "engine_commit": _read_commit("ENGINE_COMMIT"),
                "codegen_commit": _read_commit("CODEGEN_COMMIT"),
                "codegen_version": (
                    Path("/opt/pineforge-codegen/VERSION").read_text(encoding="utf-8").strip()
                ),
            },
            "transpile": {
                "seconds": round(transpile_seconds, 6),
                "pine_sha256": hashlib.sha256(pine_source.encode()).hexdigest(),
                "generated_cpp_sha256": hashlib.sha256(generated_source.encode()).hexdigest(),
                "generated_cpp_bytes": len(generated_source.encode()),
            },
            "compile": {"seconds": round(compile_seconds, 6)},
            "backtest": report.to_dict(),
        }
        print(json.dumps(output, separators=(",", ":"), allow_nan=False))
        return 0
    except CompileError as exc:
        print(f"error: PineScript transpilation failed: {exc}", file=sys.stderr)
        return 5
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
