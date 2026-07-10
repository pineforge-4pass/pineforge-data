from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from pineforge_data import (
    DEFAULT_RELEASE_IMAGE,
    BacktestOptions,
    Bar,
    Instrument,
    ReleaseContractError,
)
from pineforge_data.release_contract import (
    parse_release_report,
    release_environment,
    write_release_inputs,
)


def sample_bars(instrument: Instrument) -> list[Bar]:
    return [
        Bar(instrument, 1_000, 10, 12, 9, 11, 5, "fixture"),
        Bar(instrument, 61_000, 11, 13, 10, 12, 6, "fixture"),
    ]


def test_writes_release_mount_contract(tmp_path) -> None:
    instrument = Instrument("BTC/USD", venue="kraken", timezone="UTC", session="24x7")

    write_release_inputs(tmp_path, "strategy('x')", sample_bars(instrument), instrument)

    assert (tmp_path / "strategy.pine").read_text() == "strategy('x')"
    with (tmp_path / "ohlcv.csv").open(newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["timestamp", "open", "high", "low", "close", "volume"]
    assert rows[1][0] == "1000"
    syminfo = json.loads((tmp_path / "syminfo.json").read_text())
    assert syminfo["syminfo"]["ticker"] == "BTC/USD"


def test_release_environment_maps_runtime_options() -> None:
    instrument = Instrument("BTC/USD", venue="kraken")
    environment = release_environment(
        "/in",
        instrument,
        BacktestOptions(
            input_timeframe="15",
            script_timeframe="60",
            bar_magnifier=True,
            magnifier_samples=8,
        ),
        {"Length": 14},
        {"commission_value": 0.1},
    )

    assert environment["PINEFORGE_INPUTS"] == '{"Length":14}'
    assert environment["PINEFORGE_OVERRIDES"] == '{"commission_value":0.1}'
    assert environment["PINEFORGE_INPUT_TF"] == "15"
    assert environment["PINEFORGE_SCRIPT_TF"] == "60"
    assert environment["PINEFORGE_SYMINFO"] == "/in/syminfo.json"


def test_release_contract_rejects_unsupported_trace() -> None:
    with pytest.raises(ReleaseContractError, match="does not expose trace"):
        release_environment(
            "/in",
            Instrument("BTC/USD"),
            BacktestOptions(trace_enabled=True),
        )


def test_release_report_requires_canonical_sections() -> None:
    with pytest.raises(ReleaseContractError, match="missing"):
        parse_release_report('{"summary":{}}')


def test_server_image_and_python_runtime_share_the_release_pin() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "docker/server.Dockerfile").read_text(encoding="utf-8")

    assert f"PINEFORGE_RELEASE_IMAGE={DEFAULT_RELEASE_IMAGE}" in dockerfile
