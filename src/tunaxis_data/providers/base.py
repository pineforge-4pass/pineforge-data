"""Structural protocols implemented by community provider adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from ..models import Bar, MacroObservation, MarketListing, TradeTick
from ..requests import BarRequest, MacroRequest, MarketQuery, TradeSubscription


class MarketNotFoundError(LookupError):
    """A provider catalog has no exact match for a normalized symbol."""


@runtime_checkable
class HistoricalBarProvider(Protocol):
    name: str

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]: ...


@runtime_checkable
class MarketCatalogProvider(Protocol):
    name: str
    venue: str

    async def list_markets(self, query: MarketQuery | None = None) -> Sequence[MarketListing]: ...

    async def resolve_market(self, symbol: str) -> MarketListing: ...


@runtime_checkable
class LiveTradeProvider(Protocol):
    name: str

    def stream_trades(self, subscription: TradeSubscription) -> AsyncIterator[TradeTick]: ...


@runtime_checkable
class MacroDataProvider(Protocol):
    name: str

    async def fetch_observations(self, request: MacroRequest) -> Sequence[MacroObservation]: ...


@runtime_checkable
class MarketDataProvider(HistoricalBarProvider, MarketCatalogProvider, Protocol):
    """Catalog plus historical bars required by the backtest harness."""

    async def close(self) -> None: ...
