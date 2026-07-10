from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from pineforge_data import (
    BarRequest,
    CcxtCapabilityError,
    CcxtProvider,
    HistoricalBarProvider,
    Instrument,
    LiveTradeProvider,
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

    def milliseconds(self) -> int:
        return 180_000

    def parse_timeframe(self, timeframe: str) -> float:
        assert timeframe == "1m"
        return 60.0

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
        await provider.close()

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
