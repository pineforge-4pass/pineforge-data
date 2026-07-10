"""Provider-neutral records shared by adapters and engine sinks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


def _non_empty(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


def _non_negative(value: int, field: str) -> None:
    if value < 0:
        raise ValueError(f"{field} must be non-negative")


def _unix_ms(value: int, field: str) -> None:
    _non_negative(value, field)
    if value > 2**63 - 1:
        raise ValueError(f"{field} must fit a signed 64-bit integer")


def _optional_text(value: str, field: str) -> None:
    if value and not value.strip():
        raise ValueError(f"{field} must not contain only whitespace")


class AssetClass(StrEnum):
    """Broad economic asset class, independent of execution venue."""

    UNKNOWN = "unknown"
    CRYPTO = "crypto"
    EQUITY = "equity"
    FOREX = "forex"
    COMMODITY = "commodity"
    INDEX = "index"
    FUND = "fund"
    BOND = "bond"


class MarketType(StrEnum):
    """Trading-market structure for an instrument listing."""

    UNKNOWN = "unknown"
    SPOT = "spot"
    CASH = "cash"
    SWAP = "swap"
    FUTURE = "future"
    OPTION = "option"
    CFD = "cfd"


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Normalized derivative terms supplied by a market catalog."""

    contract_size: float | None = None
    linear: bool | None = None
    inverse: bool | None = None
    expiry_ms: int | None = None
    strike: float | None = None
    option_type: OptionType | None = None

    def __post_init__(self) -> None:
        if self.contract_size is not None and (
            not isfinite(self.contract_size) or self.contract_size <= 0
        ):
            raise ValueError("contract_size must be finite and positive")
        if self.linear is True and self.inverse is True:
            raise ValueError("a contract cannot be both linear and inverse")
        if self.expiry_ms is not None:
            _unix_ms(self.expiry_ms, "expiry_ms")
        if self.strike is not None and (not isfinite(self.strike) or self.strike <= 0):
            raise ValueError("strike must be finite and positive")


@dataclass(frozen=True, slots=True)
class Instrument:
    """A normalized market instrument with provider identity kept explicit."""

    symbol: str
    venue: str = ""
    timezone: str = "UTC"
    session: str = "24x7"
    volume_unit: str = "base"
    asset_class: AssetClass = AssetClass.UNKNOWN
    market_type: MarketType = MarketType.UNKNOWN
    base: str = ""
    quote: str = ""
    settle: str = ""
    provider_id: str = ""
    contract: ContractSpec | None = None

    def __post_init__(self) -> None:
        _non_empty(self.symbol, "symbol")
        _non_empty(self.timezone, "timezone")
        _non_empty(self.session, "session")
        _non_empty(self.volume_unit, "volume_unit")
        _optional_text(self.venue, "venue")
        _optional_text(self.base, "base")
        _optional_text(self.quote, "quote")
        _optional_text(self.settle, "settle")
        _optional_text(self.provider_id, "provider_id")


@dataclass(frozen=True, slots=True)
class MarketListing:
    """One instrument as listed by one provider-bound venue."""

    instrument: Instrument
    active: bool | None = None
    margin_supported: bool | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    """One confirmed, provider-normalized OHLCV bar."""

    instrument: Instrument
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str

    def __post_init__(self) -> None:
        _unix_ms(self.timestamp_ms, "timestamp_ms")
        _non_empty(self.source, "source")
        prices = (self.open, self.high, self.low, self.close)
        if not all(isfinite(value) and value > 0 for value in prices):
            raise ValueError("OHLC prices must be finite and positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be greater than or equal to OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be less than or equal to OHLC values")
        if not isfinite(self.volume) or self.volume < 0:
            raise ValueError("volume must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class TradeTick:
    """One normalized executed trade ready for the PineForge stream ABI."""

    instrument: Instrument
    timestamp_ms: int
    sequence: int
    price: float
    quantity: float
    source: str

    def __post_init__(self) -> None:
        _unix_ms(self.timestamp_ms, "timestamp_ms")
        _non_negative(self.sequence, "sequence")
        if self.sequence > 2**64 - 1:
            raise ValueError("sequence must fit an unsigned 64-bit integer")
        _non_empty(self.source, "source")
        if not isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be finite and positive")
        if not isfinite(self.quantity) or self.quantity < 0:
            raise ValueError("quantity must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class MacroObservation:
    """A vintage-aware macro value safe to align without revised-data lookahead."""

    key: str
    currency: str
    period_end_ms: int
    released_at_ms: int
    vintage_at_ms: int
    value: float
    unit: str
    source: str

    def __post_init__(self) -> None:
        _non_empty(self.key, "key")
        _non_empty(self.currency, "currency")
        _non_empty(self.unit, "unit")
        _non_empty(self.source, "source")
        _unix_ms(self.period_end_ms, "period_end_ms")
        _unix_ms(self.released_at_ms, "released_at_ms")
        _unix_ms(self.vintage_at_ms, "vintage_at_ms")
        if self.released_at_ms < self.period_end_ms:
            raise ValueError("released_at_ms must not precede period_end_ms")
        if self.vintage_at_ms < self.released_at_ms:
            raise ValueError("vintage_at_ms must not precede released_at_ms")
        if not isfinite(self.value):
            raise ValueError("value must be finite")
