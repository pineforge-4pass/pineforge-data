"""Provider factories and third-party entry-point discovery."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Protocol, cast

from ..models import Instrument
from .base import MarketDataProvider
from .ccxt import CcxtProvider
from .local import CsvBarProvider, SqliteBarProvider
from .sqlalchemy import SqlAlchemyBarProvider

ENTRY_POINT_GROUP = "pineforge_data.providers"


class ProviderRegistryError(RuntimeError):
    """A provider factory cannot be registered or loaded."""


class ProviderNotFoundError(ProviderRegistryError):
    """No built-in or installed provider has the requested name."""


class ProviderFactory(Protocol):
    def __call__(self, venue: str, config: Mapping[str, object]) -> MarketDataProvider: ...


def _ccxt_factory(venue: str, config: Mapping[str, object]) -> MarketDataProvider:
    return CcxtProvider(venue, config=config)


def _unknown_config(config: Mapping[str, object], allowed: set[str]) -> None:
    unknown = sorted(config.keys() - allowed)
    if unknown:
        raise ValueError(f"unknown provider configuration key(s): {', '.join(unknown)}")


def _required_text(config: Mapping[str, object], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"provider configuration {key!r} must be a non-empty string")
    return value


def _optional_text(config: Mapping[str, object], key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"provider configuration {key!r} must be a non-empty string")
    return value


def _column_overrides(config: Mapping[str, object]) -> Mapping[str, str] | None:
    value = config.get("columns")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("provider configuration 'columns' must be an object")
    overrides: dict[str, str] = {}
    allowed = {"timestamp", "open", "high", "low", "close", "volume", "symbol", "timeframe"}
    for field, column in value.items():
        if not isinstance(field, str) or not isinstance(column, str):
            raise ValueError("provider column mappings must have string keys and values")
        if field not in allowed:
            raise ValueError(f"unknown canonical column field: {field}")
        if not column:
            raise ValueError(f"provider column mapping for {field!r} must not be empty")
        overrides[field] = column
    return overrides


def _instrument(venue: str, config: Mapping[str, object]) -> Instrument | None:
    symbol = _optional_text(config, "symbol")
    return None if symbol is None else Instrument(symbol, venue=venue, provider_id=symbol)


_COMMON_TABULAR_KEYS = {
    "columns",
    "timestamp_unit",
    "timestamp_timezone",
    "symbol",
    "timeframe",
}


def _csv_factory(venue: str, config: Mapping[str, object]) -> MarketDataProvider:
    _unknown_config(config, _COMMON_TABULAR_KEYS | {"path", "encoding", "delimiter"})
    return CsvBarProvider(
        _required_text(config, "path"),
        venue=venue,
        mapping=_column_overrides(config),
        timestamp_unit=_optional_text(config, "timestamp_unit") or "milliseconds",
        timestamp_timezone=_optional_text(config, "timestamp_timezone") or "UTC",
        instrument=_instrument(venue, config),
        timeframe=_optional_text(config, "timeframe"),
        encoding=_optional_text(config, "encoding") or "utf-8-sig",
        delimiter=_optional_text(config, "delimiter") or ",",
    )


def _sqlite_factory(venue: str, config: Mapping[str, object]) -> MarketDataProvider:
    _unknown_config(config, _COMMON_TABULAR_KEYS | {"path", "table"})
    return SqliteBarProvider(
        _required_text(config, "path"),
        _required_text(config, "table"),
        venue=venue,
        mapping=_column_overrides(config),
        timestamp_unit=_optional_text(config, "timestamp_unit") or "milliseconds",
        timestamp_timezone=_optional_text(config, "timestamp_timezone") or "UTC",
        instrument=_instrument(venue, config),
        timeframe=_optional_text(config, "timeframe"),
    )


def _sqlalchemy_factory(venue: str, config: Mapping[str, object]) -> MarketDataProvider:
    _unknown_config(
        config,
        _COMMON_TABULAR_KEYS | {"url", "url_env", "table", "schema", "engine_options"},
    )
    url = _optional_text(config, "url")
    url_env = _optional_text(config, "url_env")
    if url is not None and url_env is not None:
        raise ValueError("configure only one of 'url' and 'url_env'")
    if url_env is not None:
        url = os.environ.get(url_env)
        if not url:
            raise ValueError(f"database URL environment variable is unset: {url_env}")
    if url is None:
        raise ValueError("provider configuration requires 'url' or 'url_env'")
    engine_options = config.get("engine_options")
    normalized_engine_options: Mapping[str, object] | None = None
    if engine_options is not None:
        if not isinstance(engine_options, Mapping) or not all(
            isinstance(key, str) for key in engine_options
        ):
            raise ValueError("provider configuration 'engine_options' must be an object")
        normalized_engine_options = cast(Mapping[str, object], engine_options)
    return SqlAlchemyBarProvider(
        url,
        _required_text(config, "table"),
        venue=venue,
        schema=_optional_text(config, "schema"),
        mapping=_column_overrides(config),
        timestamp_unit=_optional_text(config, "timestamp_unit") or "milliseconds",
        timestamp_timezone=_optional_text(config, "timestamp_timezone") or "UTC",
        instrument=_instrument(venue, config),
        timeframe=_optional_text(config, "timeframe"),
        engine_options=normalized_engine_options,
    )


class ProviderRegistry:
    """Resolve built-ins and externally installed provider factories by name."""

    def __init__(self, *, include_builtin: bool = True) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        if include_builtin:
            self.register("ccxt", _ccxt_factory)
            self.register("csv", _csv_factory)
            self.register("sqlite", _sqlite_factory)
            self.register("sqlalchemy", _sqlalchemy_factory)

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip().casefold()
        if not normalized:
            raise ValueError("provider name must not be empty")
        return normalized

    def register(self, name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
        """Register an in-process provider factory."""

        normalized = self._normalize_name(name)
        if normalized in self._factories and not replace:
            raise ProviderRegistryError(f"provider already registered: {normalized}")
        self._factories[normalized] = factory

    def _matching_entry_point(self, name: str) -> EntryPoint | None:
        matches = [
            candidate
            for candidate in entry_points().select(group=ENTRY_POINT_GROUP)
            if candidate.name.casefold() == name
        ]
        if len(matches) > 1:
            packages = ", ".join(sorted(candidate.value for candidate in matches))
            raise ProviderRegistryError(
                f"multiple entry points registered for provider {name!r}: {packages}"
            )
        return matches[0] if matches else None

    def _load_external(self, name: str) -> ProviderFactory | None:
        entry_point = self._matching_entry_point(name)
        if entry_point is None:
            return None
        candidate = entry_point.load()
        if not callable(candidate):
            raise ProviderRegistryError(
                f"provider entry point {entry_point.value!r} is not callable"
            )
        factory = cast(ProviderFactory, candidate)
        self._factories[name] = factory
        return factory

    def create(
        self,
        name: str,
        venue: str,
        *,
        config: Mapping[str, object] | None = None,
    ) -> MarketDataProvider:
        """Create one provider bound to a venue or broker environment."""

        normalized = self._normalize_name(name)
        factory = self._factories.get(normalized) or self._load_external(normalized)
        if factory is None:
            available = ", ".join(self.names()) or "none"
            raise ProviderNotFoundError(
                f"unknown provider {name!r}; available providers: {available}"
            )
        provider = factory(venue, config or {})
        if not isinstance(provider, MarketDataProvider):
            raise ProviderRegistryError(
                f"provider {normalized!r} does not implement MarketDataProvider"
            )
        return provider

    def names(self) -> tuple[str, ...]:
        """List built-in, registered, and advertised provider names."""

        advertised = {
            candidate.name.casefold()
            for candidate in entry_points().select(group=ENTRY_POINT_GROUP)
        }
        return tuple(sorted(self._factories.keys() | advertised))


default_registry = ProviderRegistry()


def create_provider(
    name: str,
    venue: str,
    *,
    config: Mapping[str, object] | None = None,
) -> MarketDataProvider:
    """Create a provider from the process-wide registry."""

    return default_registry.create(name, venue, config=config)
