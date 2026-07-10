# Using providers

Providers are structural Python protocols rather than required base classes.
They are selected independently from venues:

- provider: adapter implementation, such as `ccxt`;
- venue: an exchange or broker environment, such as `kraken`;
- symbol: the exact normalized market symbol on that provider.

## Create a registered provider

```python
from pineforge_data import create_provider


async def resolve_btc_usd():
    provider = create_provider("ccxt", "kraken", config={"enableRateLimit": True})
    try:
        return await provider.resolve_market("BTC/USD")
    finally:
        await provider.close()
```

`ProviderRegistry` contains built-in factories and discovers third-party
packages through the `pineforge_data.providers` entry-point group. Unknown or
invalid adapters raise `ProviderNotFoundError` or `ProviderRegistryError`.

## Discover CCXT markets

```python
from pineforge_data import CcxtProvider, MarketQuery, MarketType


async def list_linear_swaps():
    async with CcxtProvider("okx") as provider:
        swaps = await provider.list_markets(
            MarketQuery(
                market_types=frozenset({MarketType.SWAP}),
                quote="USDT",
                settle="USDT",
                active=True,
                linear=True,
            )
        )
        for listing in swaps[:10]:
            print(
                listing.instrument.symbol,
                listing.instrument.provider_id,
                listing.instrument.contract,
            )
```

Use `resolve_market()` before fetching data. Resolution is exact: passing a raw
exchange ID where a unified symbol is expected raises `MarketNotFoundError`.
This prevents an ambiguous ticker from silently choosing spot instead of a
swap, future, or option.

## Fetch historical bars

```python
from pineforge_data import BarRequest, CcxtProvider


async def fetch_swap_bars():
    async with CcxtProvider("okx") as provider:
        listing = await provider.resolve_market("BTC/USDT:USDT")
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

CCXT behavior:

- requires the exchange's unified `fetchOHLCV` capability;
- paginates by timestamp up to the requested limit;
- deduplicates repeated candle timestamps;
- sorts results in ascending order;
- excludes bars outside `[start_ms, end_ms)`;
- excludes a candle whose close time is later than the observation time.

Unsupported methods raise `CcxtCapabilityError`; malformed records raise
`CcxtDataError`; missing optional CCXT installation raises
`CcxtDependencyError`.

## Stream public trades

```python
from pineforge_data import CcxtProvider, TradeSubscription


async def print_next_trade():
    async with CcxtProvider("kraken") as provider:
        listing = await provider.resolve_market("BTC/USD")
        subscription = TradeSubscription(
            instrument=listing.instrument,
            start_ms=1_751_328_000_000,
            start_sequence=42,
        )
        async for tick in provider.stream_trades(subscription):
            print(tick.sequence, tick.timestamp_ms, tick.price, tick.quantity)
            break
```

The bootstrap CCXT adapter polls the unified public-trades endpoint, orders each
batch by timestamp, deduplicates by provider trade ID (or normalized values when
no ID exists), and assigns a strictly increasing local sequence. Stop the async
iterator when handing control back to the caller and close the provider.

## Provider configuration

`CcxtProvider` accepts separate configuration layers:

| Argument | Purpose |
|---|---|
| `config` | CCXT exchange constructor options and credentials |
| `market_params` | exchange-specific `load_markets` parameters |
| `ohlcv_params` | exchange-specific OHLCV endpoint parameters |
| `trade_params` | exchange-specific public-trade parameters |
| `page_limit` | maximum records requested per REST page |
| `poll_interval_ms` | delay between public-trade polls |
| `dedup_window` | retained trade identity window |

Keep credentials outside source control. The CLI accepts constructor options
through `--provider-config`; programmatic callers can use all endpoint-specific
arguments.

## Implement another provider

Read the [provider contract](provider-contract.md). Backtest-compatible
providers implement `MarketDataProvider`: market listing, exact resolution,
historical bars, and cleanup. Live trades and macro data remain separate,
optional protocols.
