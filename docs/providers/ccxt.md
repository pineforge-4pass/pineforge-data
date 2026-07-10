# CCXT provider API

[Provider catalog](../providers.md) · [Normalized data model](../data-model.md)

The `ccxt` provider adapts CCXT's unified async API into PineForge market
listings, confirmed historical OHLCV, and ordered public trade ticks.

## Install

```bash
pip install 'pineforge-data[ccxt]'
```

The registry name is `ccxt`. The venue is an exact CCXT exchange ID such as
`kraken`, `okx`, or `binance`.

## Construct the provider

```python
from pineforge_data import CcxtProvider

provider = CcxtProvider(
    "kraken",
    config={"enableRateLimit": True},
    page_limit=500,
)
```

`CcxtProvider` is an async context manager and closes exchanges it constructs:

```python
async with CcxtProvider("kraken") as provider:
    listing = await provider.resolve_market("BTC/USD")
```

An injected exchange remains caller-owned, which supports tests and shared CCXT
clients.

## Discover markets

```python
from pineforge_data import CcxtProvider, MarketQuery, MarketType


async def list_linear_swaps():
    async with CcxtProvider("okx") as provider:
        return await provider.list_markets(
            MarketQuery(
                market_types=frozenset({MarketType.SWAP}),
                quote="USDT",
                settle="USDT",
                active=True,
                linear=True,
            )
        )
```

`Instrument.symbol` uses CCXT's exact unified spelling. `BTC/USDT` and
`BTC/USDT:USDT` are distinct spot and swap listings. `resolve_market()` looks
up the exact unified symbol in the loaded catalog and never infers a market by
parsing text.

Normalized catalog metadata includes:

- CCXT market ID as `provider_id`;
- base, quote, and settlement assets;
- spot, swap, future, or option market type;
- active and margin-support flags;
- contract size, linear/inverse flags, expiry, strike, and option side when
  supplied by the exchange.

## Fetch confirmed historical bars

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

Historical behavior:

- requires the exchange's unified `fetchOHLCV` capability;
- translates the requested timeframe with the exchange parser;
- paginates by timestamp up to the requested limit;
- deduplicates repeated candle timestamps;
- sorts bars in ascending order;
- includes only timestamps in `[start_ms, end_ms)`;
- excludes a candle whose close time is later than the observation time.

Exchange gaps remain gaps. PineForge Data does not synthesize missing candles.

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

The bootstrap adapter polls the unified public-trades endpoint. It orders each
batch by timestamp, deduplicates by trade ID or normalized values, and assigns
a strictly increasing local sequence. `start_sequence` is the last accepted
sequence, so the next emitted value is `start_sequence + 1`.

## Constructor reference

| Argument | Default | Purpose |
|---|---|---|
| `exchange_id` | required | exact CCXT exchange ID and PineForge venue |
| `config` | `{}` | CCXT exchange constructor options and credentials |
| `exchange` | `None` | injected async CCXT-compatible exchange |
| `page_limit` | `1000` | maximum bars or trades requested per REST page |
| `poll_interval_ms` | `1000` | delay between public-trade polls |
| `dedup_window` | `10000` | retained trade identities |
| `reload_markets` | `False` | force CCXT catalog reloads |
| `market_params` | `{}` | exchange-specific `load_markets` parameters |
| `ohlcv_params` | `{}` | exchange-specific OHLCV parameters |
| `trade_params` | `{}` | exchange-specific public-trade parameters |

Keep credentials out of source control. Constructor configuration may include
API keys for private endpoints, although the built-in bar and trade operations
use public market data.

## Harness configuration

The registry factory forwards the JSON object to CCXT's exchange constructor:

```json
{
  "enableRateLimit": true,
  "timeout": 30000
}
```

```bash
pineforge-backtest \
  --pine strategy.pine \
  --provider ccxt \
  --provider-config ccxt.json \
  --venue kraken \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z
```

The CLI registry path configures constructor options only. Use the concrete
Python class when endpoint-specific parameters or polling controls are needed.

## Errors and limitations

| Error | Meaning |
|---|---|
| `CcxtDependencyError` | the `ccxt` extra is not installed |
| `CcxtCapabilityError` | the exchange lacks `fetchOHLCV` or `fetchTrades` |
| `CcxtDataError` | CCXT returned a record that cannot be normalized safely |
| `MarketNotFoundError` | the exact unified symbol is absent |

Public trades currently use REST polling, not CCXT Pro WebSockets. Availability,
history depth, pagination semantics, and rate limits still depend on the
selected exchange.
