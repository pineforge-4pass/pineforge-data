"""CCXT adapter for exchange-neutral crypto bars and public trades."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from importlib import import_module
from math import isfinite
from typing import Protocol, cast

from ..models import (
    AssetClass,
    Bar,
    ContractSpec,
    Instrument,
    MarketListing,
    MarketType,
    OptionType,
    TradeTick,
)
from ..requests import BarRequest, MarketQuery, TradeSubscription
from .base import MarketNotFoundError


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

    async def load_markets(
        self,
        reload: bool = False,
        params: Mapping[str, object] | None = None,
    ) -> Mapping[str, Mapping[str, object]]: ...

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


def _optional_number(value: object, field: str) -> float | None:
    return None if value is None else _number(value, field)


def _optional_timestamp(value: object, field: str) -> int | None:
    return None if value is None else _timestamp(value, field)


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise CcxtDataError(f"{field} must be boolean")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CcxtDataError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object, field: str) -> str:
    if value is None:
        return ""
    return _text(value, field)


_CCXT_MARKET_TYPES = {
    "spot": MarketType.SPOT,
    "margin": MarketType.SPOT,
    "swap": MarketType.SWAP,
    "future": MarketType.FUTURE,
    "option": MarketType.OPTION,
}
_CONTRACT_MARKET_TYPES = {MarketType.SWAP, MarketType.FUTURE, MarketType.OPTION}


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
        reload_markets: bool = False,
        market_params: Mapping[str, object] | None = None,
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
        self.venue = exchange_id
        self.name = f"ccxt:{exchange_id}"
        self.page_limit = page_limit
        self.poll_interval_ms = poll_interval_ms
        self.dedup_window = dedup_window
        self.reload_markets = reload_markets
        self.market_params = dict(market_params or {})
        self.ohlcv_params = dict(ohlcv_params or {})
        self.trade_params = dict(trade_params or {})

    def _require_capability(self, name: str) -> None:
        if not self._exchange.has.get(name):
            raise CcxtCapabilityError(f"{self.exchange_id} does not support {name}")

    def _validate_instrument_venue(self, instrument: Instrument) -> None:
        if instrument.venue and instrument.venue != self.venue:
            raise ValueError(
                f"instrument venue {instrument.venue!r} does not match provider "
                f"venue {self.venue!r}"
            )

    def _normalize_market(self, raw: Mapping[str, object]) -> MarketListing:
        symbol = _text(raw.get("symbol"), "market.symbol")
        provider_id = _text(raw.get("id"), "market.id")
        raw_type = _optional_text(raw.get("type"), "market.type").casefold()
        market_type = _CCXT_MARKET_TYPES.get(raw_type, MarketType.UNKNOWN)
        declared_contract = _optional_bool(raw.get("contract"), "market.contract")
        is_contract = declared_contract is True or market_type in _CONTRACT_MARKET_TYPES

        option_type: OptionType | None = None
        raw_option_type = _optional_text(raw.get("optionType"), "market.optionType")
        if raw_option_type:
            try:
                option_type = OptionType(raw_option_type.casefold())
            except ValueError as exc:
                raise CcxtDataError(f"unsupported market.optionType: {raw_option_type!r}") from exc

        contract = None
        if is_contract:
            try:
                contract = ContractSpec(
                    contract_size=_optional_number(raw.get("contractSize"), "market.contractSize"),
                    linear=_optional_bool(raw.get("linear"), "market.linear"),
                    inverse=_optional_bool(raw.get("inverse"), "market.inverse"),
                    expiry_ms=_optional_timestamp(raw.get("expiry"), "market.expiry"),
                    strike=_optional_number(raw.get("strike"), "market.strike"),
                    option_type=option_type,
                )
            except ValueError as exc:
                raise CcxtDataError(f"invalid contract metadata for {symbol}: {exc}") from exc

        return MarketListing(
            instrument=Instrument(
                symbol=symbol,
                venue=self.venue,
                volume_unit="contracts" if is_contract else "base",
                asset_class=AssetClass.CRYPTO,
                market_type=market_type,
                base=_optional_text(raw.get("base"), "market.base"),
                quote=_optional_text(raw.get("quote"), "market.quote"),
                settle=_optional_text(raw.get("settle"), "market.settle"),
                provider_id=provider_id,
                contract=contract,
            ),
            active=_optional_bool(raw.get("active"), "market.active"),
            margin_supported=_optional_bool(raw.get("margin"), "market.margin"),
        )

    async def list_markets(self, query: MarketQuery | None = None) -> Sequence[MarketListing]:
        """Load and normalize every market advertised by this CCXT exchange."""

        raw_markets = await self._exchange.load_markets(self.reload_markets, self.market_params)
        listings = [self._normalize_market(raw) for raw in raw_markets.values()]
        if query is not None:
            listings = [listing for listing in listings if query.matches(listing)]
        return sorted(listings, key=lambda listing: listing.instrument.symbol)

    async def resolve_market(self, symbol: str) -> MarketListing:
        """Resolve one exact CCXT unified symbol into normalized market metadata."""

        if not symbol.strip():
            raise ValueError("symbol must not be empty")
        raw_markets = await self._exchange.load_markets(self.reload_markets, self.market_params)
        raw = raw_markets.get(symbol)
        if raw is None:
            raise MarketNotFoundError(f"{self.name} has no exact unified market symbol {symbol!r}")
        return self._normalize_market(raw)

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

        self._validate_instrument_venue(request.instrument)
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

    async def stream_trades(self, subscription: TradeSubscription) -> AsyncIterator[TradeTick]:
        """Poll CCXT public trades and emit a strictly ordered local sequence."""

        self._validate_instrument_venue(subscription.instrument)
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
