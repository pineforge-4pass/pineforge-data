"""Run normalized PineForge data directly through a compiled strategy library."""

from __future__ import annotations

import ctypes
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from itertools import pairwise
from math import isfinite
from pathlib import Path
from typing import TypeAlias

from .engine import PfBar, pack_bars
from .models import Bar, Instrument

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

EXPECTED_PF_ABI = 2


class EngineBacktestError(RuntimeError):
    """A PineForge historical backtest could not be completed."""


class MagnifierDistribution(IntEnum):
    """PineForge bar-magnifier path sampling distributions."""

    UNIFORM = 0
    COSINE = 1
    TRIANGLE = 2
    ENDPOINTS = 3
    FRONT_LOADED = 4
    BACK_LOADED = 5


@dataclass(frozen=True, slots=True)
class BacktestOptions:
    """Runtime options passed to ``run_backtest_full``."""

    input_timeframe: str = ""
    script_timeframe: str = ""
    bar_magnifier: bool = False
    magnifier_samples: int = 4
    magnifier_distribution: MagnifierDistribution = MagnifierDistribution.ENDPOINTS
    trace_enabled: bool = False
    chart_timezone: str | None = None

    def __post_init__(self) -> None:
        if self.magnifier_samples <= 0:
            raise ValueError("magnifier_samples must be positive")
        if self.input_timeframe and not self.input_timeframe.strip():
            raise ValueError("input_timeframe must not be whitespace")
        if self.script_timeframe and not self.script_timeframe.strip():
            raise ValueError("script_timeframe must not be whitespace")
        if self.chart_timezone is not None and not self.chart_timezone.strip():
            raise ValueError("chart_timezone must not be empty")


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """A detached, JSON-safe copy of ``pf_report_t``."""

    summary: Mapping[str, JsonValue]
    metrics: Mapping[str, Mapping[str, JsonValue]]
    trades: tuple[Mapping[str, JsonValue], ...]
    security_diagnostics: tuple[Mapping[str, JsonValue], ...]
    trace: tuple[Mapping[str, JsonValue], ...]
    equity_curve: tuple[Mapping[str, JsonValue], ...]

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a mutable JSON-shaped representation."""

        return {
            "summary": dict(self.summary),
            "metrics": {name: dict(values) for name, values in self.metrics.items()},
            "trades": [dict(trade) for trade in self.trades],
            "security_diagnostics": [dict(item) for item in self.security_diagnostics],
            "trace": [dict(item) for item in self.trace],
            "equity_curve": [dict(point) for point in self.equity_curve],
        }


class _PfTrade(ctypes.Structure):
    _fields_ = [
        ("entry_time", ctypes.c_int64),
        ("exit_time", ctypes.c_int64),
        ("entry_price", ctypes.c_double),
        ("exit_price", ctypes.c_double),
        ("pnl", ctypes.c_double),
        ("pnl_pct", ctypes.c_double),
        ("is_long", ctypes.c_int),
        ("max_runup", ctypes.c_double),
        ("max_drawdown", ctypes.c_double),
        ("qty", ctypes.c_double),
        ("commission", ctypes.c_double),
        ("entry_bar_index", ctypes.c_int32),
        ("exit_bar_index", ctypes.c_int32),
    ]


class _PfTradeStats(ctypes.Structure):
    _fields_ = [
        ("num_trades", ctypes.c_int32),
        ("num_wins", ctypes.c_int32),
        ("num_losses", ctypes.c_int32),
        ("num_even", ctypes.c_int32),
        ("percent_profitable", ctypes.c_double),
        ("net_profit", ctypes.c_double),
        ("net_profit_pct", ctypes.c_double),
        ("gross_profit", ctypes.c_double),
        ("gross_profit_pct", ctypes.c_double),
        ("gross_loss", ctypes.c_double),
        ("gross_loss_pct", ctypes.c_double),
        ("profit_factor", ctypes.c_double),
        ("avg_trade", ctypes.c_double),
        ("avg_trade_pct", ctypes.c_double),
        ("avg_win", ctypes.c_double),
        ("avg_win_pct", ctypes.c_double),
        ("avg_loss", ctypes.c_double),
        ("avg_loss_pct", ctypes.c_double),
        ("ratio_avg_win_avg_loss", ctypes.c_double),
        ("largest_win", ctypes.c_double),
        ("largest_win_pct", ctypes.c_double),
        ("largest_loss", ctypes.c_double),
        ("largest_loss_pct", ctypes.c_double),
        ("commission_paid", ctypes.c_double),
        ("expectancy", ctypes.c_double),
        ("max_consecutive_wins", ctypes.c_int32),
        ("max_consecutive_losses", ctypes.c_int32),
        ("avg_bars_in_trade", ctypes.c_double),
        ("avg_bars_in_wins", ctypes.c_double),
        ("avg_bars_in_losses", ctypes.c_double),
    ]


class _PfEquityStats(ctypes.Structure):
    _fields_ = [
        ("max_equity_drawdown", ctypes.c_double),
        ("max_equity_drawdown_pct", ctypes.c_double),
        ("max_equity_runup", ctypes.c_double),
        ("max_equity_runup_pct", ctypes.c_double),
        ("buy_hold_return", ctypes.c_double),
        ("buy_hold_return_pct", ctypes.c_double),
        ("sharpe_tv", ctypes.c_double),
        ("sortino_tv", ctypes.c_double),
        ("sharpe_bar", ctypes.c_double),
        ("sortino_bar", ctypes.c_double),
        ("cagr", ctypes.c_double),
        ("calmar", ctypes.c_double),
        ("recovery_factor", ctypes.c_double),
        ("time_in_market_pct", ctypes.c_double),
        ("open_pl", ctypes.c_double),
    ]


class _PfMetrics(ctypes.Structure):
    _fields_ = [
        ("all", _PfTradeStats),
        ("longs", _PfTradeStats),
        ("shorts", _PfTradeStats),
        ("equity", _PfEquityStats),
    ]


class _PfEquityPoint(ctypes.Structure):
    _fields_ = [
        ("time_ms", ctypes.c_int64),
        ("equity", ctypes.c_double),
        ("open_profit", ctypes.c_double),
    ]


class _PfSecurityDiagnostic(ctypes.Structure):
    _fields_ = [
        ("sec_id", ctypes.c_int),
        ("feed_count", ctypes.c_int64),
        ("complete_count", ctypes.c_int64),
        ("partial_count", ctypes.c_int64),
    ]


class _PfTraceEntry(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_int64),
        ("bar_index", ctypes.c_int32),
        ("name_id", ctypes.c_int32),
        ("value", ctypes.c_double),
    ]


class _PfReport(ctypes.Structure):
    _fields_ = [
        ("total_trades", ctypes.c_int),
        ("trades", ctypes.POINTER(_PfTrade)),
        ("trades_len", ctypes.c_int),
        ("net_profit", ctypes.c_double),
        ("input_bars_processed", ctypes.c_int64),
        ("script_bars_processed", ctypes.c_int64),
        ("security_feeds_total", ctypes.c_int64),
        ("security_complete_total", ctypes.c_int64),
        ("security_partial_total", ctypes.c_int64),
        ("magnifier_sub_bars_total", ctypes.c_int64),
        ("magnifier_sample_ticks_total", ctypes.c_int64),
        ("input_tf_seconds", ctypes.c_int),
        ("script_tf_seconds", ctypes.c_int),
        ("script_tf_ratio", ctypes.c_int),
        ("needs_aggregation", ctypes.c_int),
        ("bar_magnifier_enabled", ctypes.c_int),
        ("security_diag", ctypes.POINTER(_PfSecurityDiagnostic)),
        ("security_diag_len", ctypes.c_int),
        ("trace", ctypes.POINTER(_PfTraceEntry)),
        ("trace_len", ctypes.c_int),
        ("trace_names", ctypes.POINTER(ctypes.c_char_p)),
        ("trace_names_len", ctypes.c_int),
        ("metrics", _PfMetrics),
        ("equity_curve", ctypes.POINTER(_PfEquityPoint)),
        ("equity_curve_len", ctypes.c_int64),
    ]


_TRADE_STATS_FIELDS = (
    "num_trades",
    "num_wins",
    "num_losses",
    "num_even",
    "percent_profitable",
    "net_profit",
    "net_profit_pct",
    "gross_profit",
    "gross_profit_pct",
    "gross_loss",
    "gross_loss_pct",
    "profit_factor",
    "avg_trade",
    "avg_trade_pct",
    "avg_win",
    "avg_win_pct",
    "avg_loss",
    "avg_loss_pct",
    "ratio_avg_win_avg_loss",
    "largest_win",
    "largest_win_pct",
    "largest_loss",
    "largest_loss_pct",
    "commission_paid",
    "expectancy",
    "max_consecutive_wins",
    "max_consecutive_losses",
    "avg_bars_in_trade",
    "avg_bars_in_wins",
    "avg_bars_in_losses",
)
_EQUITY_STATS_FIELDS = (
    "max_equity_drawdown",
    "max_equity_drawdown_pct",
    "max_equity_runup",
    "max_equity_runup_pct",
    "buy_hold_return",
    "buy_hold_return_pct",
    "sharpe_tv",
    "sortino_tv",
    "sharpe_bar",
    "sortino_bar",
    "cagr",
    "calmar",
    "recovery_factor",
    "time_in_market_pct",
    "open_pl",
)


def _json_number(value: float) -> float | None:
    return value if isfinite(value) else None


def _numeric_structure(
    structure: ctypes.Structure, field_names: Sequence[str]
) -> dict[str, JsonValue]:
    values: dict[str, JsonValue] = {}
    for name in field_names:
        value = getattr(structure, name)
        if isinstance(value, float):
            values[name] = _json_number(value)
        elif isinstance(value, int):
            values[name] = value
        else:
            raise EngineBacktestError(f"unsupported report field type for {name}")
    return values


def _decode(value: bytes | None) -> str:
    return value.decode("utf-8", "replace") if value else ""


class PineForgeBacktestRunner:
    """Thin owner-safe wrapper around one compiled PineForge strategy library."""

    def __init__(self, library: ctypes.CDLL) -> None:
        self._library = library
        self._check_abi()
        self._configure_signatures()

    @classmethod
    def load(cls, strategy_library: str | Path) -> PineForgeBacktestRunner:
        """Load a compiled strategy shared library from disk."""

        path = Path(strategy_library).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"strategy library not found: {path}")
        return cls(ctypes.CDLL(str(path)))

    def _check_abi(self) -> None:
        try:
            self._library.pf_abi_version.argtypes = []
            self._library.pf_abi_version.restype = ctypes.c_int
            actual = int(self._library.pf_abi_version())
        except AttributeError as exc:
            raise EngineBacktestError(
                "strategy library predates pf_abi_version; rebuild it with the current engine"
            ) from exc
        if actual != EXPECTED_PF_ABI:
            raise EngineBacktestError(
                f"PineForge ABI mismatch: strategy reports {actual}, expected {EXPECTED_PF_ABI}"
            )

    def _configure_signatures(self) -> None:
        library = self._library
        library.strategy_create.argtypes = [ctypes.c_char_p]
        library.strategy_create.restype = ctypes.c_void_p
        library.strategy_free.argtypes = [ctypes.c_void_p]
        library.strategy_free.restype = None
        library.run_backtest_full.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PfBar),
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_PfReport),
        ]
        library.run_backtest_full.restype = None
        library.report_free.argtypes = [ctypes.POINTER(_PfReport)]
        library.report_free.restype = None
        if hasattr(library, "strategy_get_last_error"):
            library.strategy_get_last_error.argtypes = [ctypes.c_void_p]
            library.strategy_get_last_error.restype = ctypes.c_char_p
        if hasattr(library, "strategy_set_trace_enabled"):
            library.strategy_set_trace_enabled.argtypes = [ctypes.c_void_p, ctypes.c_int]
            library.strategy_set_trace_enabled.restype = None
        if hasattr(library, "strategy_set_input"):
            library.strategy_set_input.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
            ]
            library.strategy_set_input.restype = None
        if hasattr(library, "strategy_set_chart_timezone"):
            library.strategy_set_chart_timezone.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            library.strategy_set_chart_timezone.restype = None
        if hasattr(library, "strategy_set_syminfo_timezone"):
            library.strategy_set_syminfo_timezone.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            library.strategy_set_syminfo_timezone.restype = None
        if hasattr(library, "strategy_set_syminfo_session"):
            library.strategy_set_syminfo_session.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            library.strategy_set_syminfo_session.restype = None

    def _apply_context(
        self, state: int | ctypes.c_void_p, instrument: Instrument, options: BacktestOptions
    ) -> None:
        library = self._library
        if options.trace_enabled and hasattr(library, "strategy_set_trace_enabled"):
            library.strategy_set_trace_enabled(state, 1)
        chart_timezone = options.chart_timezone or instrument.timezone
        if chart_timezone and hasattr(library, "strategy_set_chart_timezone"):
            library.strategy_set_chart_timezone(state, chart_timezone.encode())
        if instrument.timezone and hasattr(library, "strategy_set_syminfo_timezone"):
            library.strategy_set_syminfo_timezone(state, instrument.timezone.encode())
        if instrument.session and hasattr(library, "strategy_set_syminfo_session"):
            library.strategy_set_syminfo_session(state, instrument.session.encode())

    def _last_error(self, state: int | ctypes.c_void_p) -> str:
        if not hasattr(self._library, "strategy_get_last_error"):
            return ""
        return _decode(self._library.strategy_get_last_error(state))

    def run(
        self,
        bars: Sequence[Bar],
        *,
        instrument: Instrument,
        options: BacktestOptions | None = None,
        strategy_params: Mapping[str, JsonValue] | None = None,
    ) -> BacktestReport:
        """Run confirmed normalized bars and return a detached report."""

        if not bars:
            raise ValueError("bars must not be empty")
        if len(bars) > 2**31 - 1:
            raise ValueError("bar count exceeds the PineForge C ABI limit")
        if any(left.timestamp_ms >= right.timestamp_ms for left, right in pairwise(bars)):
            raise ValueError("bar timestamps must be strictly increasing")

        runtime = options or BacktestOptions()
        packed = pack_bars(bars, instrument=instrument)
        params_json = json.dumps(
            dict(strategy_params or {}), separators=(",", ":"), allow_nan=False
        ).encode()
        state = self._library.strategy_create(params_json)
        if not state:
            raise EngineBacktestError("strategy_create returned a null handle")

        native_report = _PfReport()
        try:
            self._apply_context(state, instrument, runtime)
            if hasattr(self._library, "strategy_set_input"):
                for name, value in (strategy_params or {}).items():
                    self._library.strategy_set_input(
                        state,
                        name.encode(),
                        str(value).encode(),
                    )
            self._library.run_backtest_full(
                state,
                packed,
                len(packed),
                runtime.input_timeframe.encode(),
                runtime.script_timeframe.encode(),
                int(runtime.bar_magnifier),
                runtime.magnifier_samples,
                int(runtime.magnifier_distribution),
                ctypes.byref(native_report),
            )
            error = self._last_error(state)
            if error:
                raise EngineBacktestError(error)
            return self._copy_report(native_report)
        finally:
            self._library.report_free(ctypes.byref(native_report))
            self._library.strategy_free(state)

    def _copy_report(self, report: _PfReport) -> BacktestReport:
        summary: dict[str, JsonValue] = {
            "total_trades": report.total_trades,
            "net_profit": _json_number(report.net_profit),
            "input_bars_processed": report.input_bars_processed,
            "script_bars_processed": report.script_bars_processed,
            "security_feeds_total": report.security_feeds_total,
            "security_complete_total": report.security_complete_total,
            "security_partial_total": report.security_partial_total,
            "magnifier_sub_bars_total": report.magnifier_sub_bars_total,
            "magnifier_sample_ticks_total": report.magnifier_sample_ticks_total,
            "input_tf_seconds": report.input_tf_seconds,
            "script_tf_seconds": report.script_tf_seconds,
            "script_tf_ratio": report.script_tf_ratio,
            "needs_aggregation": bool(report.needs_aggregation),
            "bar_magnifier_enabled": bool(report.bar_magnifier_enabled),
        }
        metrics = {
            "all": _numeric_structure(report.metrics.all, _TRADE_STATS_FIELDS),
            "longs": _numeric_structure(report.metrics.longs, _TRADE_STATS_FIELDS),
            "shorts": _numeric_structure(report.metrics.shorts, _TRADE_STATS_FIELDS),
            "equity": _numeric_structure(report.metrics.equity, _EQUITY_STATS_FIELDS),
        }
        trades: list[Mapping[str, JsonValue]] = []
        for index in range(report.trades_len):
            trade = report.trades[index]
            trades.append(
                {
                    "entry_time": trade.entry_time,
                    "exit_time": trade.exit_time,
                    "entry_price": _json_number(trade.entry_price),
                    "exit_price": _json_number(trade.exit_price),
                    "pnl": _json_number(trade.pnl),
                    "pnl_pct": _json_number(trade.pnl_pct),
                    "is_long": bool(trade.is_long),
                    "max_runup": _json_number(trade.max_runup),
                    "max_drawdown": _json_number(trade.max_drawdown),
                    "qty": _json_number(trade.qty),
                    "commission": _json_number(trade.commission),
                    "entry_bar_index": trade.entry_bar_index,
                    "exit_bar_index": trade.exit_bar_index,
                }
            )

        diagnostics: list[Mapping[str, JsonValue]] = []
        for index in range(report.security_diag_len):
            diagnostic = report.security_diag[index]
            diagnostics.append(
                {
                    "sec_id": diagnostic.sec_id,
                    "feed_count": diagnostic.feed_count,
                    "complete_count": diagnostic.complete_count,
                    "partial_count": diagnostic.partial_count,
                }
            )

        trace_names = [
            _decode(report.trace_names[index]) for index in range(report.trace_names_len)
        ]
        trace: list[Mapping[str, JsonValue]] = []
        for index in range(report.trace_len):
            entry = report.trace[index]
            name = trace_names[entry.name_id] if 0 <= entry.name_id < len(trace_names) else None
            trace.append(
                {
                    "timestamp": entry.timestamp,
                    "bar_index": entry.bar_index,
                    "name_id": entry.name_id,
                    "name": name,
                    "value": _json_number(entry.value),
                }
            )

        equity_curve: list[Mapping[str, JsonValue]] = []
        for index in range(report.equity_curve_len):
            point = report.equity_curve[index]
            equity_curve.append(
                {
                    "time_ms": point.time_ms,
                    "equity": _json_number(point.equity),
                    "open_profit": _json_number(point.open_profit),
                }
            )
        return BacktestReport(
            summary,
            metrics,
            tuple(trades),
            tuple(diagnostics),
            tuple(trace),
            tuple(equity_curve),
        )


if ctypes.sizeof(_PfReport) != 944 or _PfReport.metrics.offset != 160:
    raise RuntimeError("unsupported platform ABI layout for PineForge reports")
