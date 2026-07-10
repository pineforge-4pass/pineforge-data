"""Provider-neutral request and subscription objects."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Instrument


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
