"""CI only: run the installed distribution through a real release-container backtest.

Run with an isolated virtualenv's `python -I`, so the checkout cannot satisfy imports.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
from typing import Any

import tunaxis_data
from tunaxis_data import (
    DEFAULT_RELEASE_IMAGE,
    BacktestOptions,
    Bar,
    DockerBacktestRuntime,
    Instrument,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not Path(tunaxis_data.__file__).resolve().is_relative_to(args.source.resolve())
    assert importlib.metadata.version("tunaxis-data") == args.version
    assert args.image == DEFAULT_RELEASE_IMAGE
    instrument = Instrument("TEST/USD", venue="fixture")
    closes = (10, 11, 12, 13, 12, 11, 10, 9) * 3
    bars = [
        Bar(
            instrument,
            1_700_000_000_000 + index * 60_000,
            float(close),
            float(close + 1),
            float(close - 1),
            float(close),
            100,
            "fixture",
        )
        for index, close in enumerate(closes)
    ]
    result: dict[str, Any] = DockerBacktestRuntime().run(
        (args.fixtures / "sma_cross.pine").read_text(),
        bars,
        instrument=instrument,
        source="fixture",
        options=BacktestOptions(input_timeframe="1", script_timeframe="1"),
    )
    runtime = result["runtime"]
    version = args.image.split(":")[-2].split("@", 1)[0]
    assert runtime["release_image"] == args.image
    assert runtime["release_version"] == version
    assert runtime["engine_version"] == runtime["codegen_version"] == version
    assert result["backtest"]["summary"]["bars_processed"] == len(bars) > 0
    assert len(result["backtest"]["trades"]) > 0
    lock_path = Path(tunaxis_data.__file__).with_name("release_lock.json")
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else None
    if lock:
        event = lock["event"]
        assert runtime["hub_commit"] == event["hub_commit"]
        assert runtime["resolved_digest"] in {
            f"{event['image']['repository']}@{event['image'][key]}"
            for key in ("digest", "amd64_digest", "arm64_digest")
        }
    args.output.write_text(
        json.dumps(
            {"package_version": args.version, "runtime": runtime, "bars": len(bars)}, indent=2
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
