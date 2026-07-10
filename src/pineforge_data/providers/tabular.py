"""Schema discovery and normalization shared by user-owned tabular sources."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import Bar, Instrument, MarketListing
from ..requests import BarRequest, MarketQuery
from .base import MarketNotFoundError

TabularRow: TypeAlias = Mapping[str, object]


class TabularDataError(RuntimeError):
    """A user-owned tabular source cannot be normalized safely."""


class SchemaMappingError(TabularDataError):
    """Source columns cannot be mapped unambiguously to OHLCV fields."""


class TimestampUnit(StrEnum):
    """Storage unit used by numeric timestamps."""

    SECONDS = "seconds"
    MILLISECONDS = "milliseconds"
    MICROSECONDS = "microseconds"
    NANOSECONDS = "nanoseconds"
    ISO8601 = "iso8601"

    @classmethod
    def parse(cls, value: TimestampUnit | str) -> TimestampUnit:
        if isinstance(value, cls):
            return value
        aliases = {
            "s": cls.SECONDS,
            "sec": cls.SECONDS,
            "second": cls.SECONDS,
            "seconds": cls.SECONDS,
            "ms": cls.MILLISECONDS,
            "millisecond": cls.MILLISECONDS,
            "milliseconds": cls.MILLISECONDS,
            "us": cls.MICROSECONDS,
            "microsecond": cls.MICROSECONDS,
            "microseconds": cls.MICROSECONDS,
            "ns": cls.NANOSECONDS,
            "nanosecond": cls.NANOSECONDS,
            "nanoseconds": cls.NANOSECONDS,
            "iso": cls.ISO8601,
            "iso8601": cls.ISO8601,
            "datetime": cls.ISO8601,
        }
        normalized = value.strip().casefold()
        try:
            return aliases[normalized]
        except KeyError as exc:
            choices = ", ".join(unit.value for unit in cls)
            raise ValueError(
                f"unknown timestamp unit {value!r}; expected one of: {choices}"
            ) from exc


@dataclass(frozen=True, slots=True)
class SourceColumn:
    """One column discovered from a file header or database reflection."""

    name: str
    data_type: str = ""
    nullable: bool | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("source column name must not be empty")


_REQUIRED_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
_OPTIONAL_FIELDS = ("symbol", "timeframe")
_MAPPING_FIELDS = _REQUIRED_FIELDS + _OPTIONAL_FIELDS
_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "timestamp": (
            "timestamp",
            "timestamp_ms",
            "time",
            "datetime",
            "date",
            "ts",
            "open_time",
            "bar_time",
        ),
        "open": ("open", "open_price", "price_open", "px_open", "o"),
        "high": ("high", "high_price", "price_high", "px_high", "h"),
        "low": ("low", "low_price", "price_low", "px_low", "l"),
        "close": ("close", "close_price", "price_close", "px_close", "c"),
        "volume": ("volume", "vol", "base_volume", "quantity", "qty", "v"),
        "symbol": ("symbol", "ticker", "instrument", "market", "security"),
        "timeframe": ("timeframe", "interval", "resolution", "bar_size"),
    }
)


def _normalized_column_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _validate_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    normalized = dict(overrides or {})
    unknown = sorted(normalized.keys() - set(_MAPPING_FIELDS))
    if unknown:
        raise SchemaMappingError(
            f"unknown canonical column field(s): {', '.join(unknown)}; "
            f"expected: {', '.join(_MAPPING_FIELDS)}"
        )
    for field, column in normalized.items():
        if not isinstance(column, str) or not column:
            raise SchemaMappingError(f"column override for {field!r} must be a non-empty string")
    return normalized


@dataclass(frozen=True, slots=True)
class BarColumnMapping:
    """Map canonical PineForge bar fields to arbitrary source column names."""

    timestamp: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    symbol: str | None = None
    timeframe: str | None = None

    def __post_init__(self) -> None:
        for field in _REQUIRED_FIELDS:
            column = getattr(self, field)
            if not isinstance(column, str) or not column:
                raise ValueError(f"{field} column must be a non-empty string")
        for field in _OPTIONAL_FIELDS:
            column = getattr(self, field)
            if column is not None and not column:
                raise ValueError(f"{field} column must be None or a non-empty string")

        selected = [
            column for field in _MAPPING_FIELDS if (column := getattr(self, field)) is not None
        ]
        duplicates = sorted({column for column in selected if selected.count(column) > 1})
        if duplicates:
            raise ValueError(
                "one source column cannot map to multiple fields: " + ", ".join(duplicates)
            )

    @property
    def columns(self) -> tuple[str, ...]:
        """Return mapped source columns in canonical order without duplicates."""

        return tuple(
            column for field in _MAPPING_FIELDS if (column := getattr(self, field)) is not None
        )

    @classmethod
    def infer(
        cls,
        columns: Sequence[str],
        overrides: Mapping[str, str] | None = None,
    ) -> BarColumnMapping:
        """Infer common OHLCV names while requiring explicit ambiguous choices."""

        available = tuple(columns)
        if not available:
            raise SchemaMappingError("source exposes no columns")
        if len(set(available)) != len(available):
            duplicates = sorted({name for name in available if available.count(name) > 1})
            raise SchemaMappingError(f"source has duplicate column names: {', '.join(duplicates)}")

        selected = _validate_overrides(overrides)
        missing_overrides = sorted(set(selected.values()) - set(available))
        if missing_overrides:
            raise SchemaMappingError(
                "mapped column(s) not found: "
                f"{', '.join(missing_overrides)}; available columns: {', '.join(available)}"
            )

        used = set(selected.values())
        ambiguous: dict[str, list[str]] = {}
        for field in _MAPPING_FIELDS:
            if field in selected:
                continue
            aliases = {_normalized_column_name(alias) for alias in _ALIASES[field]}
            matches = [
                column
                for column in available
                if column not in used and _normalized_column_name(column) in aliases
            ]
            if len(matches) == 1:
                selected[field] = matches[0]
                used.add(matches[0])
            elif len(matches) > 1:
                ambiguous[field] = matches

        missing = [field for field in _REQUIRED_FIELDS if field not in selected]
        if missing or ambiguous:
            details: list[str] = []
            if missing:
                details.append("missing mappings for " + ", ".join(missing))
            if ambiguous:
                choices = "; ".join(
                    f"{field}: {', '.join(matches)}" for field, matches in sorted(ambiguous.items())
                )
                details.append("ambiguous mappings for " + choices)
            details.append("available columns: " + ", ".join(available))
            details.append(
                "pass mapping={'timestamp': 'your_time', ...} to override only unusual names"
            )
            raise SchemaMappingError("; ".join(details))

        return cls(
            timestamp=selected["timestamp"],
            open=selected["open"],
            high=selected["high"],
            low=selected["low"],
            close=selected["close"],
            volume=selected["volume"],
            symbol=selected.get("symbol"),
            timeframe=selected.get("timeframe"),
        )

    def validate(self, columns: Sequence[str]) -> None:
        """Require every selected name to exist in the reflected source schema."""

        missing = sorted(set(self.columns) - set(columns))
        if missing:
            raise SchemaMappingError(
                f"mapped column(s) not found: {', '.join(missing)}; "
                f"available columns: {', '.join(columns)}"
            )


ColumnMappingInput: TypeAlias = BarColumnMapping | Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class TabularSchema:
    """Runtime description of a user-owned file, table, or view."""

    source: str
    columns: tuple[SourceColumn, ...]

    def __post_init__(self) -> None:
        names = self.column_names
        if not self.source:
            raise ValueError("schema source must not be empty")
        if len(set(names)) != len(names):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise SchemaMappingError(f"source has duplicate column names: {', '.join(duplicates)}")

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def infer_bar_mapping(self, overrides: Mapping[str, str] | None = None) -> BarColumnMapping:
        """Build a validated mapping from reflected columns and optional overrides."""

        return BarColumnMapping.infer(self.column_names, overrides)


def _numeric_timestamp_ms(value: object, unit: TimestampUnit) -> int:
    try:
        numeric = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"invalid numeric timestamp: {value!r}") from exc
    if not numeric.is_finite():
        raise ValueError("timestamp must be finite")
    scale = {
        TimestampUnit.SECONDS: Decimal(1_000),
        TimestampUnit.MILLISECONDS: Decimal(1),
        TimestampUnit.MICROSECONDS: Decimal("0.001"),
        TimestampUnit.NANOSECONDS: Decimal("0.000001"),
    }[unit]
    milliseconds = numeric * scale
    if milliseconds != milliseconds.to_integral_value():
        raise ValueError("timestamp cannot be represented exactly at millisecond precision")
    normalized = int(milliseconds)
    if normalized < 0 or normalized > 2**63 - 1:
        raise ValueError("timestamp must fit a non-negative signed 64-bit millisecond value")
    return normalized


def _timestamp_ms(value: object, unit: TimestampUnit, timezone: ZoneInfo) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("timestamp must be numeric, ISO-8601 text, or a datetime")

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        if unit is not TimestampUnit.ISO8601:
            try:
                Decimal(text)
            except InvalidOperation:
                pass
            else:
                return _numeric_timestamp_ms(text, unit)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    else:
        if unit is TimestampUnit.ISO8601:
            raise ValueError("timestamp_unit=iso8601 requires text or datetime values")
        return _numeric_timestamp_ms(value, unit)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    normalized = int(parsed.timestamp() * 1_000)
    if normalized < 0 or normalized > 2**63 - 1:
        raise ValueError("timestamp must fit a non-negative signed 64-bit millisecond value")
    return normalized


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be numeric")
    try:
        normalized = float(value if isinstance(value, (int, float, str)) else str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(normalized):
        raise ValueError(f"{field} must be finite")
    return normalized


def numeric_source_bounds(
    start_ms: int, end_ms: int, unit: TimestampUnit
) -> tuple[int | float, int | float] | None:
    """Translate millisecond request bounds for safe numeric SQL pushdown."""

    if unit is TimestampUnit.ISO8601:
        return None
    if unit is TimestampUnit.SECONDS:
        return start_ms / 1_000, end_ms / 1_000
    if unit is TimestampUnit.MILLISECONDS:
        return start_ms, end_ms
    if unit is TimestampUnit.MICROSECONDS:
        return start_ms * 1_000, end_ms * 1_000
    return start_ms * 1_000_000, end_ms * 1_000_000


class TabularBarProvider(ABC):
    """Common catalog and OHLCV behavior for schema-mapped tabular sources."""

    name: str
    venue: str

    def __init__(
        self,
        *,
        venue: str,
        mapping: ColumnMappingInput = None,
        timestamp_unit: TimestampUnit | str = TimestampUnit.MILLISECONDS,
        timestamp_timezone: str = "UTC",
        instrument: Instrument | None = None,
        timeframe: str | None = None,
    ) -> None:
        if not venue.strip():
            raise ValueError("venue must not be empty")
        if timeframe is not None and not timeframe.strip():
            raise ValueError("timeframe must not be empty")
        if instrument is not None and instrument.venue and instrument.venue != venue:
            raise ValueError(
                f"instrument venue {instrument.venue!r} does not match provider venue {venue!r}"
            )
        try:
            self._timestamp_timezone = ZoneInfo(timestamp_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timestamp timezone: {timestamp_timezone}") from exc

        self.venue = venue
        self.timestamp_unit = TimestampUnit.parse(timestamp_unit)
        self.timestamp_timezone = timestamp_timezone
        self._instrument_template = instrument
        self._timeframe = timeframe
        self._explicit_mapping: BarColumnMapping | None = None
        self._mapping_overrides: dict[str, str] = {}
        if isinstance(mapping, BarColumnMapping):
            self._explicit_mapping = mapping
        elif mapping is not None:
            self._mapping_overrides = _validate_overrides(mapping)

    async def inspect_schema(self) -> TabularSchema:
        """Reflect columns without reading or normalizing the full dataset."""

        return await asyncio.to_thread(self._inspect_schema_sync)

    def _resolved_mapping_sync(self) -> BarColumnMapping:
        schema = self._inspect_schema_sync()
        if self._explicit_mapping is not None:
            self._explicit_mapping.validate(schema.column_names)
            return self._explicit_mapping
        return schema.infer_bar_mapping(self._mapping_overrides)

    def _instrument(self, symbol: str) -> Instrument:
        template = self._instrument_template
        if template is not None:
            if template.symbol != symbol:
                raise MarketNotFoundError(
                    f"{self.name} is bound to {template.symbol!r}, not {symbol!r}"
                )
            return replace(
                template,
                venue=template.venue or self.venue,
                provider_id=template.provider_id or symbol,
            )
        return Instrument(symbol=symbol, venue=self.venue, provider_id=symbol)

    def _symbols_sync(self, mapping: BarColumnMapping) -> tuple[str, ...]:
        if mapping.symbol is None:
            if self._instrument_template is None:
                return ()
            return (self._instrument_template.symbol,)
        values = self._distinct_values_sync(mapping.symbol)
        symbols = {
            str(value).strip() for value in values if value is not None and str(value).strip()
        }
        return tuple(sorted(symbols))

    async def list_markets(self, query: MarketQuery | None = None) -> Sequence[MarketListing]:
        mapping = await asyncio.to_thread(self._resolved_mapping_sync)
        symbols = await asyncio.to_thread(self._symbols_sync, mapping)
        listings = [MarketListing(self._instrument(symbol)) for symbol in symbols]
        return listings if query is None else [item for item in listings if query.matches(item)]

    async def resolve_market(self, symbol: str) -> MarketListing:
        if not symbol.strip():
            raise ValueError("symbol must not be empty")
        mapping = await asyncio.to_thread(self._resolved_mapping_sync)
        if mapping.symbol is not None:
            symbols = await asyncio.to_thread(self._symbols_sync, mapping)
            if symbol not in symbols:
                raise MarketNotFoundError(f"{self.name} has no exact symbol {symbol!r}")
        return MarketListing(self._instrument(symbol))

    def _fetch_bars_sync(self, request: BarRequest) -> Sequence[Bar]:
        if request.instrument.venue and request.instrument.venue != self.venue:
            raise ValueError(
                f"instrument venue {request.instrument.venue!r} does not match provider "
                f"venue {self.venue!r}"
            )
        if self._instrument_template is not None:
            self._instrument(request.instrument.symbol)
        if self._timeframe is not None and request.timeframe != self._timeframe:
            raise ValueError(
                f"source timeframe {self._timeframe!r} does not match request {request.timeframe!r}"
            )

        mapping = self._resolved_mapping_sync()
        rows = self._read_rows_sync(mapping, request)
        by_timestamp: dict[int, Bar] = {}
        for index, row in enumerate(rows, start=1):
            try:
                if mapping.symbol is not None:
                    row_symbol = str(row[mapping.symbol]).strip()
                    if row_symbol != request.instrument.symbol:
                        continue
                if mapping.timeframe is not None:
                    row_timeframe = str(row[mapping.timeframe]).strip()
                    if row_timeframe != request.timeframe:
                        continue
                timestamp_ms = _timestamp_ms(
                    row[mapping.timestamp], self.timestamp_unit, self._timestamp_timezone
                )
                if not request.start_ms <= timestamp_ms < request.end_ms:
                    continue
                if timestamp_ms in by_timestamp:
                    raise TabularDataError(
                        f"duplicate bar timestamp {timestamp_ms} for "
                        f"{request.instrument.symbol!r} and {request.timeframe!r}"
                    )
                by_timestamp[timestamp_ms] = Bar(
                    instrument=request.instrument,
                    timestamp_ms=timestamp_ms,
                    open=_number(row[mapping.open], "open"),
                    high=_number(row[mapping.high], "high"),
                    low=_number(row[mapping.low], "low"),
                    close=_number(row[mapping.close], "close"),
                    volume=_number(row[mapping.volume], "volume"),
                    source=self.name,
                )
            except TabularDataError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise TabularDataError(f"cannot normalize source row {index}: {exc}") from exc

        bars = [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]
        return bars if request.limit is None else bars[: request.limit]

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]:
        """Read, validate, sort, and normalize bars for one exact request."""

        return await asyncio.to_thread(self._fetch_bars_sync, request)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    @abstractmethod
    def _inspect_schema_sync(self) -> TabularSchema: ...

    @abstractmethod
    def _read_rows_sync(
        self, mapping: BarColumnMapping, request: BarRequest
    ) -> Sequence[TabularRow]: ...

    @abstractmethod
    def _distinct_values_sync(self, column: str) -> Sequence[object]: ...

    def _close_sync(self) -> None:
        return None
