"""Provider-neutral market and macro data contracts for PineForge."""

from .backtest import (
    BacktestOptions,
    BacktestReport,
    EngineBacktestError,
    MagnifierDistribution,
    PineForgeBacktestRunner,
)
from .engine import EngineStreamSink, PfBar, PfTradeTick, pack_bars, pack_trade_ticks
from .errors import EngineStreamError
from .models import Bar, Instrument, MacroObservation, TradeTick
from .providers import (
    CcxtCapabilityError,
    CcxtDataError,
    CcxtDependencyError,
    CcxtError,
    CcxtProvider,
    HistoricalBarProvider,
    LiveTradeProvider,
    MacroDataProvider,
)
from .requests import BarRequest, MacroRequest, TradeSubscription

__all__ = [
    "BacktestOptions",
    "BacktestReport",
    "Bar",
    "BarRequest",
    "CcxtCapabilityError",
    "CcxtDataError",
    "CcxtDependencyError",
    "CcxtError",
    "CcxtProvider",
    "EngineBacktestError",
    "EngineStreamError",
    "EngineStreamSink",
    "HistoricalBarProvider",
    "Instrument",
    "LiveTradeProvider",
    "MacroDataProvider",
    "MacroObservation",
    "MacroRequest",
    "MagnifierDistribution",
    "PfBar",
    "PfTradeTick",
    "PineForgeBacktestRunner",
    "TradeSubscription",
    "TradeTick",
    "pack_bars",
    "pack_trade_ticks",
]
