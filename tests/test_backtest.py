from __future__ import annotations

import ctypes
import json
from math import nan
from typing import cast

import pytest

from pineforge_data import (
    BacktestOptions,
    Bar,
    EngineBacktestError,
    Instrument,
    PineForgeBacktestRunner,
)
from pineforge_data.backtest import _PfEquityPoint, _PfReport, _PfTrade
from pineforge_data.cli.backtest import ccxt_timeframe_to_pine, parse_timestamp


class FakeFunction:
    def __init__(self, result: object = None, callback: object = None) -> None:
        self.result = result
        self.callback = callback
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        if callable(self.callback):
            return self.callback(*args)
        return self.result


class FakeBacktestLibrary:
    def __init__(self, *, abi: int = 2, error: bytes = b"") -> None:
        self.pf_abi_version = FakeFunction(abi)
        self.strategy_create = FakeFunction(123)
        self.strategy_free = FakeFunction()
        self.report_free = FakeFunction()
        self.strategy_get_last_error = FakeFunction(error)
        self.strategy_set_input = FakeFunction()
        self.strategy_set_trace_enabled = FakeFunction()
        self.strategy_set_chart_timezone = FakeFunction()
        self.strategy_set_syminfo_timezone = FakeFunction()
        self.strategy_set_syminfo_session = FakeFunction()
        self.run_backtest_full = FakeFunction(callback=self._fill_report)
        self._trades = (_PfTrade * 1)(
            _PfTrade(1_000, 2_000, 10.0, 12.0, 20.0, 20.0, 1, 2.5, 0.5, 10.0, 0.0, 0, 1)
        )
        self._equity = (_PfEquityPoint * 2)(
            _PfEquityPoint(1_000, 10_000.0, 0.0),
            _PfEquityPoint(2_000, 10_020.0, 0.0),
        )

    def _fill_report(self, *args: object) -> None:
        native = ctypes.cast(args[-1], ctypes.POINTER(_PfReport)).contents
        native.total_trades = 1
        native.trades = self._trades
        native.trades_len = 1
        native.net_profit = 20.0
        native.input_bars_processed = 2
        native.script_bars_processed = 2
        native.input_tf_seconds = 60
        native.script_tf_seconds = 60
        native.script_tf_ratio = 1
        native.metrics.all.num_trades = 1
        native.metrics.all.num_wins = 1
        native.metrics.all.net_profit = 20.0
        native.metrics.all.profit_factor = nan
        native.metrics.equity.open_pl = 0.0
        native.equity_curve = self._equity
        native.equity_curve_len = 2


def bars() -> list[Bar]:
    instrument = Instrument("BTC/USD", venue="fake")
    return [
        Bar(instrument, 1_000, 10.0, 12.0, 9.0, 11.0, 5.0, "fake"),
        Bar(instrument, 2_000, 11.0, 13.0, 10.0, 12.0, 6.0, "fake"),
    ]


def test_runner_returns_detached_json_safe_report() -> None:
    fake = FakeBacktestLibrary()
    runner = PineForgeBacktestRunner(cast(ctypes.CDLL, fake))
    instrument = bars()[0].instrument

    report = runner.run(
        bars(),
        instrument=instrument,
        options=BacktestOptions(
            input_timeframe="1",
            script_timeframe="1",
            trace_enabled=True,
            chart_timezone="UTC",
        ),
        strategy_params={"length": 14},
    )
    payload = report.to_dict()

    assert payload["summary"]["net_profit"] == 20.0  # type: ignore[index]
    assert payload["metrics"]["all"]["profit_factor"] is None  # type: ignore[index]
    assert payload["trades"][0]["is_long"] is True  # type: ignore[index]
    assert len(payload["equity_curve"]) == 2  # type: ignore[arg-type]
    json.dumps(payload, allow_nan=False)
    assert len(fake.report_free.calls) == 1
    assert len(fake.strategy_free.calls) == 1
    assert fake.strategy_set_trace_enabled.calls == [(123, 1)]
    assert fake.strategy_set_input.calls == [(123, b"length", b"14")]
    assert fake.strategy_set_syminfo_session.calls == [(123, b"24x7")]


def test_runner_surfaces_engine_error_and_releases_owners() -> None:
    fake = FakeBacktestLibrary(error=b"strategy failed")
    runner = PineForgeBacktestRunner(cast(ctypes.CDLL, fake))

    with pytest.raises(EngineBacktestError, match="strategy failed"):
        runner.run(bars(), instrument=bars()[0].instrument)

    assert len(fake.report_free.calls) == 1
    assert len(fake.strategy_free.calls) == 1


def test_runner_rejects_abi_mismatch_and_unsorted_bars() -> None:
    with pytest.raises(EngineBacktestError, match="ABI mismatch"):
        PineForgeBacktestRunner(cast(ctypes.CDLL, FakeBacktestLibrary(abi=99)))

    runner = PineForgeBacktestRunner(cast(ctypes.CDLL, FakeBacktestLibrary()))
    with pytest.raises(ValueError, match="strictly increasing"):
        runner.run(list(reversed(bars())), instrument=bars()[0].instrument)


@pytest.mark.parametrize(
    ("ccxt", "pine"),
    [("15m", "15"), ("2h", "120"), ("1d", "1D"), ("1w", "1W"), ("1M", "1M")],
)
def test_ccxt_timeframe_conversion(ccxt: str, pine: str) -> None:
    assert ccxt_timeframe_to_pine(ccxt) == pine


def test_timestamp_parser_accepts_unix_ms_and_iso_8601() -> None:
    assert parse_timestamp("1000") == 1_000
    assert parse_timestamp("1970-01-01T00:00:01Z") == 1_000
