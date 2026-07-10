# Getting started

## Requirements

- Python 3.11 or newer;
- Docker only when running raw-Pine backtests locally or building the FastAPI
  server;
- network access to the selected data provider;
- provider credentials only when the requested endpoint requires them.

No Git submodules, local C++ compiler, or engine checkout is required.

## Install

Install CCXT support for crypto exchanges:

```bash
python3 -m venv .venv
.venv/bin/pip install 'pineforge-data[ccxt]'
```

Available extras:

| Extra | Purpose |
|---|---|
| `ccxt` | CCXT async exchange adapter |
| `database` | SQLAlchemy 2.x database reflection and queries |
| `server` | FastAPI and Uvicorn server dependencies |
| `dev` | tests, type checking, formatting, and package builds |

## Resolve a market and fetch confirmed bars

Use the provider's normalized symbol exactly as returned by its market catalog.
For CCXT, spot `BTC/USDT` and linear swap `BTC/USDT:USDT` are different
instruments.

```python
import asyncio

from pineforge_data import BarRequest, CcxtProvider


async def main() -> None:
    async with CcxtProvider("kraken") as provider:
        listing = await provider.resolve_market("BTC/USD")
        instrument = listing.instrument
        bars = await provider.fetch_bars(
            BarRequest(
                instrument=instrument,
                timeframe="15m",
                start_ms=1_751_328_000_000,
                end_ms=1_751_414_400_000,
            )
        )
        print(instrument.market_type, instrument.provider_id, len(bars))


asyncio.run(main())
```

The CCXT adapter paginates, deduplicates timestamps, sorts the result, and
excludes a candle that was not closed at the request's observation time.

## Run a raw-Pine backtest locally

```bash
.venv/bin/pineforge-backtest \
  --pine strategy.pine \
  --provider ccxt \
  --venue kraken \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z \
  --output report.json \
  --pretty
```

The first run pulls the package's digest-pinned `pineforge-release` image. The
provider runs on the host; only raw PineScript, normalized OHLCV, syminfo, and
runtime options enter the isolated container.

## Use the FastAPI server

If a backtest server is already running:

```bash
export PINEFORGE_SERVER_URL=http://127.0.0.1:8000
export PINEFORGE_SERVER_API_KEY=change-me
.venv/bin/pineforge-backtest \
  --pine strategy.pine \
  --venue kraken \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z
```

The data fetch still happens locally. The harness sends normalized bars to the
server, which provides bounded concurrency and a generated-C++ keyed compile
cache.

## Next steps

- [Learn the normalized data model](data-model.md).
- [Choose a provider and open its API guide](providers.md).
- [Configure parameters, runtime channels, and reports](backtesting.md).
- [Deploy the concurrent server](server.md).
