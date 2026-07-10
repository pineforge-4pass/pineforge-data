from __future__ import annotations

import json
from typing import cast
from urllib.request import Request

import pytest

from pineforge_data import BacktestOptions, Bar, FastApiBacktestClient, Instrument


class FakeResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.body = json.dumps(value).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


def test_client_posts_normalized_request(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        assert timeout == 30
        observed.append(request)
        return FakeResponse(
            {
                "schema_version": 1,
                "request_id": "one",
                "runtime": {"mode": "fastapi-server"},
                "backtest": {"summary": {}},
            }
        )

    monkeypatch.setattr("pineforge_data.server_client.urlopen", fake_urlopen)
    instrument = Instrument("BTC/USD", venue="kraken")
    bars = [Bar(instrument, 1_000, 10, 12, 9, 11, 5, "ccxt:kraken")]
    client = FastApiBacktestClient("http://localhost:8000", timeout_seconds=30, api_key="secret")

    result = client.run(
        "strategy('test')",
        bars,
        instrument=instrument,
        source="ccxt:kraken",
        options=BacktestOptions(input_timeframe="1", trade_start_time_ms=1_000),
        strategy_params={"Length": 14},
    )

    assert result["schema_version"] == 1
    assert len(observed) == 1
    assert observed[0].get_header("Authorization") == "Bearer secret"
    body = json.loads(cast(bytes, observed[0].data))
    assert body["bars"][0]["timestamp_ms"] == 1_000
    assert body["strategy_params"] == {"Length": 14}
    assert body["options"]["trade_start_time_ms"] == 1_000


def test_client_rejects_non_scalar_strategy_values() -> None:
    client = FastApiBacktestClient("http://localhost:8000")
    instrument = Instrument("BTC/USD")
    bars = [Bar(instrument, 1_000, 10, 12, 9, 11, 5, "fixture")]

    with pytest.raises(ValueError, match="must be a scalar"):
        client.run(
            "strategy('test')",
            bars,
            instrument=instrument,
            source="fixture",
            options=BacktestOptions(),
            strategy_params={"nested": {"bad": True}},
        )
