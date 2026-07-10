from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from pineforge_data import (
    AssetClass,
    BarRequest,
    CcxtCapabilityError,
    CcxtProvider,
    HistoricalBarProvider,
    Instrument,
    LiveTradeProvider,
    MarketCatalogProvider,
    MarketNotFoundError,
    MarketQuery,
    MarketType,
    OptionType,
    TradeSubscription,
)


class FakeCcxtExchange:
    id = "fake"

    def __init__(self) -> None:
        self.has: Mapping[str, object] = {"fetchOHLCV": True, "fetchTrades": True}
        self.closed = False
        self.ohlcv = [
            [0, 10.0, 12.0, 9.0, 11.0, 5.0],
            [60_000, 11.0, 13.0, 10.0, 12.0, 6.0],
            [120_000, 12.0, 14.0, 11.0, 13.0, 7.0],
            [180_000, 13.0, 15.0, 12.0, 14.0, 8.0],
        ]
        self.trades: list[Mapping[str, object]] = [
            {"id": "b", "timestamp": 180_002, "price": 12.0, "amount": 0.2},
            {"id": "a", "timestamp": 180_001, "price": 11.0, "amount": 0.1},
        ]
        self.markets: dict[str, Mapping[str, object]] = {
            "BTC/USDT": {
                "id": "BTCUSDT",
                "symbol": "BTC/USDT",
                "base": "BTC",
                "quote": "USDT",
                "settle": None,
                "type": "spot",
                "spot": True,
                "margin": True,
                "contract": False,
                "active": True,
            },
            "BTC/USDT:USDT": {
                "id": "BTC-USDT-SWAP",
                "symbol": "BTC/USDT:USDT",
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "margin": False,
                "contract": True,
                "linear": True,
                "inverse": False,
                "contractSize": 0.001,
                "active": True,
            },
            "BTC/USD:BTC-260925": {
                "id": "PI_XBTUSD_260925",
                "symbol": "BTC/USD:BTC-260925",
                "base": "BTC",
                "quote": "USD",
                "settle": "BTC",
                "type": "future",
                "future": True,
                "margin": False,
                "contract": True,
                "linear": False,
                "inverse": True,
                "contractSize": 1,
                "expiry": 1_790_294_400_000,
                "active": True,
            },
            "BTC/USD:BTC-260925-100000-C": {
                "id": "BTC-260925-100000-C",
                "symbol": "BTC/USD:BTC-260925-100000-C",
                "base": "BTC",
                "quote": "USD",
                "settle": "BTC",
                "type": "option",
                "option": True,
                "margin": False,
                "contract": True,
                "linear": False,
                "inverse": True,
                "contractSize": 1,
                "expiry": 1_790_294_400_000,
                "strike": 100_000,
                "optionType": "call",
                "active": True,
            },
        }

    def milliseconds(self) -> int:
        return 180_000

    def parse_timeframe(self, timeframe: str) -> float:
        assert timeframe == "1m"
        return 60.0

    async def load_markets(
        self,
        reload: bool = False,
        params: Mapping[str, object] | None = None,
    ) -> Mapping[str, Mapping[str, object]]:
        assert not reload
        assert params == {}
        return self.markets

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None,
        limit: int | None,
        params: Mapping[str, object],
    ) -> Sequence[Sequence[object]]:
        assert symbol == "BTC/USDT"
        assert timeframe == "1m"
        start = since or 0
        selected = [row for row in self.ohlcv if int(row[0]) >= start]
        return selected if limit is None else selected[:limit]

    async def fetch_trades(
        self,
        symbol: str,
        since: int | None,
        limit: int | None,
        params: Mapping[str, object],
    ) -> Sequence[Mapping[str, object]]:
        assert symbol == "BTC/USDT"
        return self.trades

    async def close(self) -> None:
        self.closed = True


def test_fetch_bars_paginates_and_excludes_unconfirmed_candle() -> None:
    async def run() -> None:
        exchange = FakeCcxtExchange()
        provider = CcxtProvider("fake", exchange=exchange, page_limit=2)
        request = BarRequest(Instrument("BTC/USDT", venue="fake"), "1m", 0, 240_000)

        bars = await provider.fetch_bars(request)

        assert [bar.timestamp_ms for bar in bars] == [0, 60_000, 120_000]
        assert all(bar.source == "ccxt:fake" for bar in bars)
        await provider.close()
        assert not exchange.closed

    asyncio.run(run())


def test_stream_trades_orders_records_and_assigns_sequences() -> None:
    async def run() -> None:
        provider = CcxtProvider("fake", exchange=FakeCcxtExchange(), poll_interval_ms=0)
        stream = provider.stream_trades(
            TradeSubscription(
                Instrument("BTC/USDT", venue="fake"),
                start_ms=180_000,
                start_sequence=7,
            )
        )

        first = await anext(stream)
        second = await anext(stream)
        await stream.aclose()

        assert (first.timestamp_ms, first.sequence) == (180_001, 8)
        assert (second.timestamp_ms, second.sequence) == (180_002, 9)

    asyncio.run(run())


def test_constructs_installed_ccxt_exchange() -> None:
    pytest.importorskip("ccxt.async_support")

    async def run() -> None:
        provider = CcxtProvider("kraken")
        assert provider.name == "ccxt:kraken"
        assert isinstance(provider, HistoricalBarProvider)
        assert isinstance(provider, LiveTradeProvider)
        assert isinstance(provider, MarketCatalogProvider)
        await provider.close()

    asyncio.run(run())


def test_market_catalog_normalizes_spot_and_contract_metadata() -> None:
    async def run() -> None:
        provider = CcxtProvider("fake", exchange=FakeCcxtExchange())

        spot = await provider.resolve_market("BTC/USDT")
        swap = await provider.resolve_market("BTC/USDT:USDT")
        future = await provider.resolve_market("BTC/USD:BTC-260925")
        option = await provider.resolve_market("BTC/USD:BTC-260925-100000-C")

        assert spot.instrument.asset_class is AssetClass.CRYPTO
        assert spot.instrument.market_type is MarketType.SPOT
        assert spot.instrument.provider_id == "BTCUSDT"
        assert spot.instrument.contract is None
        assert spot.margin_supported is True

        assert swap.instrument.market_type is MarketType.SWAP
        assert swap.instrument.volume_unit == "contracts"
        assert swap.instrument.contract is not None
        assert swap.instrument.contract.contract_size == 0.001
        assert swap.instrument.contract.linear is True

        assert future.instrument.market_type is MarketType.FUTURE
        assert future.instrument.contract is not None
        assert future.instrument.contract.inverse is True
        assert future.instrument.contract.expiry_ms == 1_790_294_400_000

        assert option.instrument.market_type is MarketType.OPTION
        assert option.instrument.contract is not None
        assert option.instrument.contract.strike == 100_000
        assert option.instrument.contract.option_type is OptionType.CALL

    asyncio.run(run())


def test_market_query_filters_by_type_settlement_and_contract_shape() -> None:
    async def run() -> None:
        provider = CcxtProvider("fake", exchange=FakeCcxtExchange())
        query = MarketQuery(
            market_types=frozenset({MarketType.SWAP}),
            settle="usdt",
            active=True,
            linear=True,
        )

        listings = await provider.list_markets(query)

        assert [listing.instrument.symbol for listing in listings] == ["BTC/USDT:USDT"]

    asyncio.run(run())


def test_market_resolution_requires_an_exact_unified_symbol() -> None:
    async def run() -> None:
        provider = CcxtProvider("fake", exchange=FakeCcxtExchange())

        with pytest.raises(MarketNotFoundError, match="exact unified market symbol"):
            await provider.resolve_market("BTCUSDT")

    asyncio.run(run())


def test_rejects_an_instrument_from_another_venue() -> None:
    async def run() -> None:
        provider = CcxtProvider("fake", exchange=FakeCcxtExchange())
        request = BarRequest(Instrument("BTC/USDT", venue="other"), "1m", 0, 60_000)

        with pytest.raises(ValueError, match="does not match provider venue"):
            await provider.fetch_bars(request)

    asyncio.run(run())


def test_missing_exchange_capability_is_explicit() -> None:
    async def run() -> None:
        exchange = FakeCcxtExchange()
        exchange.has = {"fetchOHLCV": False, "fetchTrades": True}
        provider = CcxtProvider("fake", exchange=exchange)
        request = BarRequest(Instrument("BTC/USDT"), "1m", 0, 60_000)

        with pytest.raises(CcxtCapabilityError, match="fetchOHLCV"):
            await provider.fetch_bars(request)

    asyncio.run(run())
