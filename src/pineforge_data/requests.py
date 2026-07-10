"""Provider-neutral request and subscription objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import AssetClass, Instrument, MarketListing, MarketType


@dataclass(frozen=True, slots=True)
class MarketQuery:
    """Provider-neutral filters for catalog discovery."""

    asset_class: AssetClass | None = None
    market_types: frozenset[MarketType] = field(default_factory=frozenset)
    base: str = ""
    quote: str = ""
    settle: str = ""
    active: bool | None = None
    margin_supported: bool | None = None
    linear: bool | None = None
    inverse: bool | None = None

    def matches(self, listing: MarketListing) -> bool:
        """Return whether a normalized listing satisfies every supplied filter."""

        instrument = listing.instrument
        contract = instrument.contract
        return (
            (self.asset_class is None or instrument.asset_class is self.asset_class)
            and (not self.market_types or instrument.market_type in self.market_types)
            and (not self.base or instrument.base.casefold() == self.base.casefold())
            and (not self.quote or instrument.quote.casefold() == self.quote.casefold())
            and (not self.settle or instrument.settle.casefold() == self.settle.casefold())
            and (self.active is None or listing.active is self.active)
            and (self.margin_supported is None or listing.margin_supported is self.margin_supported)
            and (self.linear is None or (contract is not None and contract.linear is self.linear))
            and (
                self.inverse is None or (contract is not None and contract.inverse is self.inverse)
            )
        )


@dataclass(frozen=True, slots=True)
class BarRequest:
    instrument: Instrument
    timeframe: str
    start_ms: int
    end_ms: int
    limit: int | None = None

    def __post_init__(self) -> None:
        if not self.timeframe.strip():
            raise ValueError("timeframe must not be empty")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("bar request requires 0 <= start_ms < end_ms")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit must be positive")


@dataclass(frozen=True, slots=True)
class TradeSubscription:
    """A live stream beginning at ``start_ms`` and after ``start_sequence``."""

    instrument: Instrument
    start_ms: int | None = None
    start_sequence: int = 0

    def __post_init__(self) -> None:
        if self.start_ms is not None and self.start_ms < 0:
            raise ValueError("start_ms must be non-negative")
        if self.start_sequence < 0:
            raise ValueError("start_sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class MacroRequest:
    key: str
    currency: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.currency.strip():
            raise ValueError("key and currency must not be empty")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("macro request requires 0 <= start_ms < end_ms")
