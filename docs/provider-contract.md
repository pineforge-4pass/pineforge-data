# Provider contract

PineForge Data treats a provider and a venue as separate identities:

- **provider** selects an adapter implementation, such as `ccxt` or a broker SDK;
- **venue** selects the exchange, broker environment, or data source instance;
- **symbol** is the provider's normalized public symbol;
- **provider_id** is the raw venue identifier used for provenance and debugging.

An adapter must resolve an exact symbol through its upstream catalog before it
fetches bars. Symbol text is not a portable schema: derivative symbols can
encode settlement currency, expiry, strike, or option side differently across
providers.

## Normalized market model

`Instrument` carries:

- `asset_class`: crypto, equity, forex, commodity, index, fund, bond, or unknown;
- `market_type`: spot, cash, swap, future, option, CFD, or unknown;
- `base`, `quote`, and `settle` currencies/assets;
- `contract`: optional `ContractSpec` with contract size, linear/inverse flags,
  expiry, strike, and option type;
- venue, normalized symbol, provider ID, volume unit, timezone, and session.

Margin availability is a capability on `MarketListing`, not a market type. A
spot listing can support margin while remaining a spot market.

Unknown and unavailable fields should remain explicit (`UNKNOWN`, empty text,
or `None`). An adapter must not manufacture metadata by parsing the symbol.

## In-tree adapter skeleton

```python
from collections.abc import Mapping, Sequence

from pineforge_data import (
    Bar,
    BarRequest,
    Instrument,
    MarketListing,
    MarketNotFoundError,
    MarketQuery,
)


class BrokerProvider:
    def __init__(self, venue: str, config: Mapping[str, object]) -> None:
        self.venue = venue
        self.name = f"broker:{venue}"

    async def list_markets(
        self, query: MarketQuery | None = None
    ) -> Sequence[MarketListing]:
        listings = []  # Normalize the broker's catalog here.
        return listings if query is None else [x for x in listings if query.matches(x)]

    async def resolve_market(self, symbol: str) -> MarketListing:
        for listing in await self.list_markets():
            if listing.instrument.symbol == symbol:
                return listing
        raise MarketNotFoundError(f"no exact market {symbol!r} on {self.venue}")

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]:
        ...

    async def close(self) -> None:
        ...


def provider_factory(
    venue: str, config: Mapping[str, object]
) -> BrokerProvider:
    return BrokerProvider(venue, config)
```

Add an in-tree factory to `providers/registry.py`. Out-of-tree packages use the
`pineforge_data.providers` Python entry-point group shown in
`CONTRIBUTING.md`. The CLI accepts any registered name through `--provider` and
passes `--venue` plus the JSON object from `--provider-config` to the factory.

## Tests required for a provider PR

- offline catalog fixtures for every supported market type;
- exact symbol resolution and a missing-symbol error;
- normalized provider ID, venue, base/quote/settle, and contract terms;
- catalog filtering through `MarketQuery`;
- confirmed OHLCV behavior, pagination, deduplication, and source provenance;
- close/cleanup behavior and explicit missing-capability errors;
- no network access or credentials in CI.
