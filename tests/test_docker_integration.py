from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from pineforge_data import (
    BacktestOptions,
    Bar,
    DockerBacktestRuntime,
    Instrument,
    discover_repository_root,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PINEFORGE_DOCKER_TEST") != "1",
    reason="set PINEFORGE_DOCKER_TEST=1 to build and exercise the pinned image",
)


def test_raw_pine_transpiles_compiles_and_backtests_in_docker() -> None:
    root = discover_repository_root(Path(__file__).resolve())
    pine = (root / "tests/fixtures/sma_cross.pine").read_text(encoding="utf-8")
    instrument = Instrument("TEST/USD", venue="fixture")
    closes = [
        10,
        11,
        12,
        13,
        12,
        11,
        10,
        9,
        10,
        11,
        12,
        13,
        12,
        11,
        10,
        9,
        10,
        11,
        12,
        13,
        12,
        11,
        10,
        9,
    ]
    bars = [
        Bar(
            instrument,
            1_700_000_000_000 + index * 60_000,
            float(close),
            float(close + 1),
            float(close - 1),
            float(close),
            100.0,
            "fixture",
        )
        for index, close in enumerate(closes)
    ]
    result = DockerBacktestRuntime(root).run(
        pine,
        bars,
        instrument=instrument,
        source="fixture",
        options=BacktestOptions(input_timeframe="1", script_timeframe="1"),
    )

    runtime = cast(dict[str, object], result["runtime"])
    backtest = cast(dict[str, object], result["backtest"])
    summary = cast(dict[str, object], backtest["summary"])
    transpile = cast(dict[str, object], result["transpile"])
    assert runtime["engine_commit"] == "9734d48ce32ed61c0a1d0285166276f110e9afaa"
    assert runtime["codegen_commit"] == "9aa99e4f0d4734cbfd4a01e4ceb058774580f1ee"
    assert len(cast(str, runtime["data_source_digest"])) == 64
    assert summary["input_bars_processed"] == len(bars)
    assert int(cast(int, transpile["generated_cpp_bytes"])) > 1_000
