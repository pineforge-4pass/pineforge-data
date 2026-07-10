from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from pineforge_data import (
    Bar,
    BarRequest,
    HistoricalBarProvider,
    Instrument,
    MarketDataProvider,
    MarketListing,
    MarketNotFoundError,
    MarketQuery,
    ProviderNotFoundError,
    ProviderRegistry,
)


class ExampleBarProvider:
    name = "example"

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]:
        return [
            Bar(
                request.instrument,
                request.start_ms,
                10.0,
                12.0,
                9.0,
                11.0,
                5.0,
                self.name,
            )
        ]


def test_structural_provider_protocol_needs_no_base_class() -> None:
    provider = ExampleBarProvider()
    request = BarRequest(Instrument("BTCUSD"), "1m", 1_000, 61_000)

    assert isinstance(provider, HistoricalBarProvider)
    assert asyncio.run(provider.fetch_bars(request))[0].source == "example"


class ExampleMarketProvider:
    name = "example:paper"
    venue = "paper"

    async def list_markets(self, query: MarketQuery | None = None) -> Sequence[MarketListing]:
        listing = MarketListing(Instrument("TEST/USD", venue=self.venue))
        return [listing] if query is None or query.matches(listing) else []

    async def resolve_market(self, symbol: str) -> MarketListing:
        if symbol != "TEST/USD":
            raise MarketNotFoundError(symbol)
        return MarketListing(Instrument(symbol, venue=self.venue))

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]:
        return []

    async def close(self) -> None:
        return None


def example_factory(venue: str, config: Mapping[str, object]) -> MarketDataProvider:
    assert venue == "paper"
    assert config == {"environment": "test"}
    return ExampleMarketProvider()


def test_provider_registry_accepts_broker_plugins_without_cli_branching() -> None:
    registry = ProviderRegistry(include_builtin=False)
    registry.register("example", example_factory)

    provider = registry.create("EXAMPLE", "paper", config={"environment": "test"})

    assert isinstance(provider, MarketDataProvider)
    assert asyncio.run(provider.resolve_market("TEST/USD")).instrument.venue == "paper"


def test_provider_registry_reports_unknown_adapters() -> None:
    registry = ProviderRegistry(include_builtin=False)

    with pytest.raises(ProviderNotFoundError, match="unknown provider"):
        registry.create("missing", "paper")
