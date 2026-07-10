"""Provider factories and third-party entry-point discovery."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Protocol, cast

from .base import MarketDataProvider
from .ccxt import CcxtProvider

ENTRY_POINT_GROUP = "pineforge_data.providers"


class ProviderRegistryError(RuntimeError):
    """A provider factory cannot be registered or loaded."""


class ProviderNotFoundError(ProviderRegistryError):
    """No built-in or installed provider has the requested name."""


class ProviderFactory(Protocol):
    def __call__(self, venue: str, config: Mapping[str, object]) -> MarketDataProvider: ...


def _ccxt_factory(venue: str, config: Mapping[str, object]) -> MarketDataProvider:
    return CcxtProvider(venue, config=config)


class ProviderRegistry:
    """Resolve built-ins and externally installed provider factories by name."""

    def __init__(self, *, include_builtin: bool = True) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        if include_builtin:
            self.register("ccxt", _ccxt_factory)

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
