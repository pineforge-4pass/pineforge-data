# Provider catalog

Providers are structural Python implementations selected independently from a
venue and symbol:

- **provider** chooses an adapter, such as `ccxt`, `csv`, `sqlite`, or
  `sqlalchemy`;
- **venue** identifies an exchange, broker environment, or user-defined data
  source, such as `kraken`, `warehouse`, or `research`;
- **symbol** is the exact normalized identity exposed by that provider.

This page documents the shared lifecycle and routes to the second-level API
guide for each built-in provider.

## Built-in providers

| Provider | Data source | Capabilities | Install extra | API guide |
|---|---|---|---|---|
| `ccxt` | Cryptocurrency exchanges | catalog, historical bars, public trades | `ccxt` | [CCXT](providers/ccxt.md) |
| `csv` | Local delimited file | inferred catalog, historical bars | none | [CSV](providers/csv.md) |
| `sqlite` | Local SQLite table or view | reflected catalog, historical bars | none | [SQLite](providers/sqlite.md) |
| `sqlalchemy` | SQLAlchemy-supported database table or view | reflected catalog, historical bars | `database` plus a dialect driver | [SQLAlchemy](providers/sqlalchemy.md) |

CSV, SQLite, and SQLAlchemy share runtime schema discovery and arbitrary column
mapping. Read the [tabular schema mapping](providers/tabular-schema.md) guide
before configuring one of them.

## Shared provider lifecycle

Create a provider by registry name when configuration comes from a CLI, service,
or other runtime boundary:

```python
from pineforge_data import create_provider


async def resolve_btc_usd():
    provider = create_provider(
        "ccxt",
        "kraken",
        config={"enableRateLimit": True},
    )
    try:
        return await provider.resolve_market("BTC/USD")
    finally:
        await provider.close()
```

`ProviderRegistry` contains built-in factories and discovers third-party
packages through the `pineforge_data.providers` entry-point group. Provider
names are case-insensitive; symbols remain exact and provider-defined.

Programmatic callers may instantiate a concrete provider directly when they
need constructor options that are not JSON-shaped:

```python
from pineforge_data import CcxtProvider


async def direct_provider():
    async with CcxtProvider("kraken", page_limit=500) as provider:
        return await provider.resolve_market("BTC/USD")
```

## Common market and bar API

Backtest-compatible providers implement `MarketDataProvider`:

```python
async def list_markets(query=None): ...
async def resolve_market(symbol): ...
async def fetch_bars(request): ...
async def close(): ...
```

Use exact resolution before fetching bars:

```python
from pineforge_data import BarRequest


async def fetch(provider, symbol):
    listing = await provider.resolve_market(symbol)
    return await provider.fetch_bars(
        BarRequest(
            instrument=listing.instrument,
            timeframe="1h",
            start_ms=1_751_328_000_000,
            end_ms=1_751_587_200_000,
            limit=500,
        )
    )
```

Every provider returns normalized `MarketListing` and `Bar` records. The end
timestamp is exclusive. Individual provider guides define symbol spelling,
confirmation behavior, pagination, filtering, and source-specific limitations.

Live trades are a separate `LiveTradeProvider` capability. A provider that can
fetch historical bars does not automatically promise a live stream. CCXT is
the first built-in provider implementing both.

## Use a provider in the backtest harness

The raw-Pine harness accepts every registered `MarketDataProvider`:

```bash
pineforge-backtest \
  --pine strategy.pine \
  --provider PROVIDER \
  --provider-config provider.json \
  --venue VENUE \
  --symbol SYMBOL \
  --timeframe 1h \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z
```

`--provider-config` must contain a JSON object. Its accepted keys belong to the
selected provider and are documented on that provider's API page. Unknown keys
for built-in local/database providers fail early; third-party providers own
their configuration validation.

## Shared errors

| Error | Meaning |
|---|---|
| `ProviderNotFoundError` | no built-in or installed provider has the requested registry name |
| `ProviderRegistryError` | a provider factory is duplicated, invalid, or does not implement `MarketDataProvider` |
| `MarketNotFoundError` | exact symbol resolution failed |
| `SchemaMappingError` | a tabular source cannot be mapped unambiguously to OHLCV |
| `TabularDataError` | a local/database row cannot be normalized safely |

Provider-specific dependency, capability, transport, and malformed-record
errors are listed in each second-level guide.

## Add another provider

Read the [provider contract](provider-contract.md). Backtest-compatible
providers implement catalog resolution, historical bars, and cleanup. Live
trades and macro data remain separate optional protocols.

A provider contribution should also add `docs/providers/<provider>.md` and a
row to the catalog above. Keep implementation-specific configuration and
behavior on that second-level page rather than expanding this index.
