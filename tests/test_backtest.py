from __future__ import annotations

import asyncio
import ctypes
import json
from math import nan
from pathlib import Path
from typing import cast

import pytest

from tunaxis_data import (
    BacktestOptions,
    Bar,
    EngineBacktestError,
    Instrument,
    TunaxisBacktestRunner,
)
from tunaxis_data.backtest import _PfEquityPoint, _PfEquityStats, _PfReport, _PfTrade
from tunaxis_data.cli.backtest import (
    build_parser,
    ccxt_timeframe_to_pine,
    parse_timestamp,
    run_harness,
    source_timeframe_to_pine,
    warmup_request_start_ms,
)
from tunaxis_data.models import MarketListing


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
    def __init__(self, *, abi: int = 4, error: bytes = b"") -> None:
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
        self.strategy_set_trade_start_time = FakeFunction()
        self.run_backtest_full = FakeFunction(callback=self._fill_report)
        self._trades = (_PfTrade * 1)(
            _PfTrade(1_000, 2_000, 10.0, 12.0, 20.0, 20.0, 1, 25.0, 5.0, 10.0, 0.0, 0, 1, 1)
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
    runner = TunaxisBacktestRunner(cast(ctypes.CDLL, fake))
    instrument = bars()[0].instrument

    report = runner.run(
        bars(),
        instrument=instrument,
        options=BacktestOptions(
            input_timeframe="1",
            script_timeframe="1",
            trace_enabled=True,
            chart_timezone="UTC",
            trade_start_time_ms=1_500,
        ),
        strategy_params={"length": 14},
    )
    payload = report.to_dict()

    assert payload["summary"]["net_profit"] == 20.0  # type: ignore[index]
    assert payload["metrics"]["all"]["profit_factor"] is None  # type: ignore[index]
    assert payload["trades"][0]["is_long"] is True  # type: ignore[index]
    assert payload["trades"][0]["open_at_end"] is True  # type: ignore[index]
    assert payload["trades"][0]["max_runup"] == 25.0  # type: ignore[index]
    assert payload["trades"][0]["max_drawdown"] == 5.0  # type: ignore[index]
    assert len(payload["equity_curve"]) == 2  # type: ignore[arg-type]
    json.dumps(payload, allow_nan=False)
    assert len(fake.report_free.calls) == 1
    assert len(fake.strategy_free.calls) == 1
    assert fake.strategy_set_trace_enabled.calls == [(123, 1)]
    assert fake.strategy_set_input.calls == [(123, b"length", b"14")]
    assert fake.strategy_set_syminfo_session.calls == [(123, b"24x7")]
    assert fake.strategy_set_trade_start_time.calls == [(123, 1_500)]


def test_runner_surfaces_engine_error_and_releases_owners() -> None:
    fake = FakeBacktestLibrary(error=b"strategy failed")
    runner = TunaxisBacktestRunner(cast(ctypes.CDLL, fake))

    with pytest.raises(EngineBacktestError, match="strategy failed"):
        runner.run(bars(), instrument=bars()[0].instrument)

    assert len(fake.report_free.calls) == 1
    assert len(fake.strategy_free.calls) == 1


def test_runner_rejects_abi_mismatch_and_unsorted_bars() -> None:
    with pytest.raises(EngineBacktestError, match=r"ABI mismatch.*upgrade tunaxis-data"):
        TunaxisBacktestRunner(cast(ctypes.CDLL, FakeBacktestLibrary(abi=99)))
    unversioned = FakeBacktestLibrary()
    del unversioned.pf_abi_version
    with pytest.raises(EngineBacktestError, match=r"predates pf_abi_version.*engine 1\.x"):
        TunaxisBacktestRunner(cast(ctypes.CDLL, unversioned))

    runner = TunaxisBacktestRunner(cast(ctypes.CDLL, FakeBacktestLibrary()))
    with pytest.raises(ValueError, match="strictly increasing"):
        runner.run(list(reversed(bars())), instrument=bars()[0].instrument)


@pytest.mark.parametrize(("abi", "engine"), [(2, "v0.10.2 to v0.12.3"), (3, "v0.13")])
def test_runner_refuses_pre_1_0_libraries_with_a_rebuild_hint(abi: int, engine: str) -> None:
    fake = FakeBacktestLibrary(abi=abi)

    with pytest.raises(EngineBacktestError, match=f"strategy reports {abi}, expected 4") as error:
        TunaxisBacktestRunner(cast(ctypes.CDLL, fake))

    assert engine in str(error.value)
    assert "rebuild" in str(error.value)
    assert fake.strategy_create.calls == []


def test_report_structures_match_pineforge_abi_4_layout() -> None:
    # Offsets and sizes measured by the C compiler against engine v1.0.0's
    # include/pineforge/pineforge.h (LP64).
    assert ctypes.sizeof(_PfTrade) == 104
    assert _PfTrade.exit_bar_index.offset == 92
    assert _PfTrade.open_at_end.offset == 96
    assert _PfEquityStats.sharpe_tv.offset == 48
    assert _PfEquityStats.sortino_tv.offset == 56
    assert ctypes.sizeof(_PfEquityStats) == 120
    assert ctypes.sizeof(_PfReport) == 960
    assert _PfReport.metrics.offset == 160
    assert _PfReport.equity_curve.offset == 928
    assert _PfReport.equity_curve_len.offset == 936
    assert _PfReport.broker_state_hash.offset == 944
    assert _PfReport.broker_state_hash_len.offset == 952


@pytest.mark.parametrize(
    ("ccxt", "pine"),
    [("15m", "15"), ("2h", "120"), ("1d", "1D"), ("1w", "1W"), ("1M", "1M")],
)
def test_ccxt_timeframe_conversion(ccxt: str, pine: str) -> None:
    assert ccxt_timeframe_to_pine(ccxt) == pine
    assert source_timeframe_to_pine(ccxt) == pine


def test_timestamp_parser_accepts_unix_ms_and_iso_8601() -> None:
    assert parse_timestamp("1000") == 1_000
    assert parse_timestamp("1970-01-01T00:00:01Z") == 1_000


def test_warmup_start_uses_source_bar_count_and_clamps_at_epoch() -> None:
    assert warmup_request_start_ms(10 * 60_000, "1m", 3) == 7 * 60_000
    assert warmup_request_start_ms(60_000, "1m", 3) == 0
    assert warmup_request_start_ms(60_000, "1m", 0) == 60_000


def test_backtest_options_reject_negative_trade_start() -> None:
    with pytest.raises(ValueError, match="trade_start_time_ms"):
        BacktestOptions(trade_start_time_ms=-1)


def test_cli_requires_raw_pine_instead_of_shared_library() -> None:
    args = build_parser().parse_args(
        [
            "--pine",
            "strategy.pine",
            "--exchange",
            "kraken",
            "--symbol",
            "BTC/USD",
            "--timeframe",
            "15m",
            "--start",
            "1000",
            "--end",
            "2000",
        ]
    )

    assert args.pine == Path("strategy.pine")
    assert args.venue == "kraken"
    assert not hasattr(args, "strategy")


def test_cli_accepts_generic_provider_and_venue_names() -> None:
    args = build_parser().parse_args(
        [
            "--pine",
            "strategy.pine",
            "--provider",
            "community-broker",
            "--venue",
            "paper",
            "--symbol",
            "ES/SEP26",
            "--timeframe",
            "1m",
            "--start",
            "1000",
            "--end",
            "2000",
        ]
    )

    assert (args.provider, args.venue) == ("community-broker", "paper")


def test_cli_can_route_harness_to_concurrent_server() -> None:
    args = build_parser().parse_args(
        [
            "--pine",
            "strategy.pine",
            "--venue",
            "kraken",
            "--symbol",
            "BTC/USD",
            "--timeframe",
            "15m",
            "--start",
            "1000",
            "--end",
            "2000",
            "--server-url",
            "http://127.0.0.1:8000",
            "--execution-timeout",
            "60",
            "--warmup-bars",
            "200",
        ]
    )

    assert args.server_url == "http://127.0.0.1:8000"
    assert args.execution_timeout == 60
    assert args.warmup_bars == 200


def test_harness_loads_warmup_bars_and_gates_trading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instrument = Instrument("BTC/USD", venue="fixture")

    class FakeProvider:
        name = "fixture"
        request: object = None

        async def resolve_market(self, _symbol: str) -> MarketListing:
            return MarketListing(instrument)

        async def fetch_bars(self, request: object) -> list[Bar]:
            self.request = request
            return [
                Bar(instrument, timestamp, 10, 11, 9, 10, 1, "fixture")
                for timestamp in (60_000, 120_000, 180_000, 240_000, 300_000)
            ]

        async def close(self) -> None:
            return None

    class FakeRuntime:
        def __init__(self) -> None:
            self.received_bars: list[Bar] = []
            self.received_options: BacktestOptions | None = None

        def run(
            self,
            _pine_source: str,
            runtime_bars: list[Bar],
            **kwargs: object,
        ) -> dict[str, object]:
            self.received_bars = runtime_bars
            options = kwargs["options"]
            assert isinstance(options, BacktestOptions)
            self.received_options = options
            return {"runtime": {}, "backtest": {}}

    provider = FakeProvider()
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "tunaxis_data.cli.backtest.create_provider", lambda *_args, **_kwargs: provider
    )
    monkeypatch.setattr(
        "tunaxis_data.cli.backtest.DockerBacktestRuntime", lambda **_kwargs: runtime
    )
    pine = tmp_path / "strategy.pine"
    pine.write_text("//@version=6\nstrategy('warmup')\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "--pine",
            str(pine),
            "--provider",
            "fixture",
            "--venue",
            "fixture",
            "--symbol",
            "BTC/USD",
            "--timeframe",
            "1m",
            "--start",
            "180000",
            "--end",
            "360000",
            "--limit",
            "10",
            "--warmup-bars",
            "2",
        ]
    )

    report = asyncio.run(run_harness(args))

    request = provider.request
    assert request is not None
    assert request.start_ms == 60_000  # type: ignore[attr-defined]
    assert request.limit == 12  # type: ignore[attr-defined]
    assert [bar.timestamp_ms for bar in runtime.received_bars] == [
        60_000,
        120_000,
        180_000,
        240_000,
        300_000,
    ]
    assert runtime.received_options is not None
    assert runtime.received_options.trade_start_time_ms == 180_000
    data = report["data"]
    assert isinstance(data, dict)
    assert data["warmup_bars_requested"] == 2
    assert data["warmup_bars_loaded"] == 2
