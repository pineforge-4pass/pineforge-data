"""CCXT adapter for exchange-neutral crypto bars and public trades."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from importlib import import_module
from math import isfinite
from typing import Protocol, cast

from ..models import Bar, TradeTick
from ..requests import BarRequest, TradeSubscription


class CcxtError(RuntimeError):
    """Base error raised by the CCXT adapter."""


class CcxtDependencyError(CcxtError):
    """The optional CCXT dependency is unavailable."""


class CcxtCapabilityError(CcxtError):
    """The configured exchange lacks a required unified method."""


class CcxtDataError(CcxtError):
    """CCXT returned a record that cannot be normalized safely."""


class _AsyncCcxtExchange(Protocol):
    id: str
    has: Mapping[str, object]

    def milliseconds(self) -> int: ...

    def parse_timeframe(self, timeframe: str) -> float: ...

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None,
        limit: int | None,
        params: Mapping[str, object],
    ) -> Sequence[Sequence[object]]: ...

    async def fetch_trades(
        self,
        symbol: str,
        since: int | None,
        limit: int | None,
        params: Mapping[str, object],
    ) -> Sequence[Mapping[str, object]]: ...

    async def close(self) -> None: ...


def _load_exchange(exchange_id: str, config: Mapping[str, object]) -> _AsyncCcxtExchange:
    try:
        module = import_module("ccxt.async_support")
    except ModuleNotFoundError as exc:
        if exc.name == "ccxt" or (exc.name and exc.name.startswith("ccxt.")):
            raise CcxtDependencyError(
                "CCXT is not installed; install pineforge-data[ccxt]"
            ) from exc
        raise

    factory = getattr(module, exchange_id, None)
    if factory is None or not callable(factory):
        raise ValueError(f"unknown CCXT exchange: {exchange_id}")
    return cast(_AsyncCcxtExchange, factory(dict(config)))


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CcxtDataError(f"{field} must be numeric")
    normalized = float(value)
    if not isfinite(normalized):
        raise CcxtDataError(f"{field} must be finite")
    return normalized


def _timestamp(value: object, field: str = "timestamp") -> int:
    normalized = _number(value, field)
    if normalized < 0 or not normalized.is_integer():
        raise CcxtDataError(f"{field} must be a non-negative integer")
    return int(normalized)


class CcxtProvider:
    """Normalize one CCXT exchange into PineForge bars and trade ticks.

    ``Instrument.symbol`` must use CCXT's unified symbol spelling, for example
    ``BTC/USDT``. The adapter owns exchanges it constructs and leaves injected
    exchanges open, which keeps tests and shared CCXT clients composable.
    """

    def __init__(
        self,
        exchange_id: str,
        *,
        config: Mapping[str, object] | None = None,
        exchange: _AsyncCcxtExchange | None = None,
        page_limit: int = 1_000,
        poll_interval_ms: int = 1_000,
        dedup_window: int = 10_000,
        ohlcv_params: Mapping[str, object] | None = None,
        trade_params: Mapping[str, object] | None = None,
    ) -> None:
        if not exchange_id.strip():
            raise ValueError("exchange_id must not be empty")
        if page_limit <= 0:
            raise ValueError("page_limit must be positive")
        if poll_interval_ms < 0:
            raise ValueError("poll_interval_ms must be non-negative")
        if dedup_window <= 0:
            raise ValueError("dedup_window must be positive")

        self._owns_exchange = exchange is None
        self._exchange = exchange or _load_exchange(exchange_id, config or {})
        if self._exchange.id != exchange_id:
            raise ValueError(
                f"injected CCXT exchange id {self._exchange.id!r} does not match {exchange_id!r}"
            )
        self.exchange_id = exchange_id
        self.name = f"ccxt:{exchange_id}"
        self.page_limit = page_limit
        self.poll_interval_ms = poll_interval_ms
        self.dedup_window = dedup_window
        self.ohlcv_params = dict(ohlcv_params or {})
        self.trade_params = dict(trade_params or {})

    def _require_capability(self, name: str) -> None:
        if not self._exchange.has.get(name):
            raise CcxtCapabilityError(f"{self.exchange_id} does not support {name}")

    def _normalize_bar(self, raw: Sequence[object], request: BarRequest) -> Bar:
        if len(raw) < 6:
            raise CcxtDataError("OHLCV record must contain at least six fields")
        return Bar(
            instrument=request.instrument,
            timestamp_ms=_timestamp(raw[0]),
            open=_number(raw[1], "open"),
            high=_number(raw[2], "high"),
            low=_number(raw[3], "low"),
            close=_number(raw[4], "close"),
            volume=_number(raw[5], "volume"),
            source=self.name,
        )

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]:
        """Fetch paginated, deduplicated, confirmed OHLCV bars."""

        self._require_capability("fetchOHLCV")
        timeframe_seconds = self._exchange.parse_timeframe(request.timeframe)
        timeframe_ms = int(_number(timeframe_seconds, "timeframe seconds") * 1_000)
        if timeframe_ms <= 0:
            raise CcxtDataError("timeframe duration must be positive")

        observed_at_ms = min(request.end_ms, self._exchange.milliseconds())
        cursor = request.start_ms
        by_timestamp: dict[int, Bar] = {}
        while cursor < request.end_ms:
            remaining = None if request.limit is None else request.limit - len(by_timestamp)
            if remaining is not None and remaining <= 0:
                break
            batch_limit = self.page_limit if remaining is None else min(self.page_limit, remaining)
            raw_bars = await self._exchange.fetch_ohlcv(
                request.instrument.symbol,
                request.timeframe,
                cursor,
                batch_limit,
                self.ohlcv_params,
            )
            if not raw_bars:
                break

            max_timestamp = cursor - 1
            for raw in raw_bars:
                bar = self._normalize_bar(raw, request)
                max_timestamp = max(max_timestamp, bar.timestamp_ms)
                if (
                    request.start_ms <= bar.timestamp_ms < request.end_ms
                    and bar.timestamp_ms + timeframe_ms <= observed_at_ms
                ):
                    by_timestamp[bar.timestamp_ms] = bar

            next_cursor = max_timestamp + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        bars = sorted(by_timestamp.values(), key=lambda bar: bar.timestamp_ms)
        return bars if request.limit is None else bars[: request.limit]

    def _trade_key(self, raw: Mapping[str, object], tick: TradeTick) -> tuple[object, ...]:
        trade_id = raw.get("id")
        if trade_id is not None and str(trade_id):
            return ("id", str(trade_id))
        return ("values", tick.timestamp_ms, tick.price, tick.quantity, raw.get("side"))

    def _normalize_trade(
        self,
        raw: Mapping[str, object],
        subscription: TradeSubscription,
        sequence: int,
    ) -> TradeTick:
        return TradeTick(
            instrument=subscription.instrument,
            timestamp_ms=_timestamp(raw.get("timestamp")),
            sequence=sequence,
            price=_number(raw.get("price"), "price"),
            quantity=_number(raw.get("amount"), "amount"),
            source=self.name,
        )

    async def stream_trades(
        self, subscription: TradeSubscription
    ) -> AsyncIterator[TradeTick]:
        """Poll CCXT public trades and emit a strictly ordered local sequence."""

        self._require_capability("fetchTrades")
        since = (
            subscription.start_ms
            if subscription.start_ms is not None
            else self._exchange.milliseconds()
        )
        sequence = subscription.start_sequence
        seen_order: deque[tuple[object, ...]] = deque()
        seen: set[tuple[object, ...]] = set()

        while True:
            poll_since = since
            raw_trades = await self._exchange.fetch_trades(
                subscription.instrument.symbol,
                since,
                self.page_limit,
                self.trade_params,
            )
            ordered = sorted(raw_trades, key=lambda raw: _timestamp(raw.get("timestamp")))
            for raw in ordered:
                candidate = self._normalize_trade(raw, subscription, sequence + 1)
                if candidate.timestamp_ms < poll_since:
                    continue
                since = max(since, candidate.timestamp_ms)
                key = self._trade_key(raw, candidate)
                if key in seen:
                    continue
                sequence += 1
                tick = TradeTick(
                    candidate.instrument,
                    candidate.timestamp_ms,
                    sequence,
                    candidate.price,
                    candidate.quantity,
                    candidate.source,
                )
                seen.add(key)
                seen_order.append(key)
                if len(seen_order) > self.dedup_window:
                    seen.remove(seen_order.popleft())
                yield tick

            await asyncio.sleep(self.poll_interval_ms / 1_000)

    async def close(self) -> None:
        """Close an exchange constructed by this provider."""

        if self._owns_exchange:
            await self._exchange.close()

    async def __aenter__(self) -> CcxtProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
