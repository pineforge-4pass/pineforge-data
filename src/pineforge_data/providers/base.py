"""Structural protocols implemented by community provider adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from ..models import Bar, MacroObservation, TradeTick
from ..requests import BarRequest, MacroRequest, TradeSubscription


@runtime_checkable
class HistoricalBarProvider(Protocol):
    name: str

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]: ...


@runtime_checkable
class LiveTradeProvider(Protocol):
    name: str

    def stream_trades(self, subscription: TradeSubscription) -> AsyncIterator[TradeTick]: ...


@runtime_checkable
class MacroDataProvider(Protocol):
    name: str

    async def fetch_observations(self, request: MacroRequest) -> Sequence[MacroObservation]: ...
