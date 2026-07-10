# PineForge Data

Provider-neutral market and macro data adapters for PineForge.

`pineforge-data` is the boundary between third-party services and the
deterministic [`pineforge-engine`](https://github.com/pineforge-4pass/pineforge-engine)
runtime:

```text
provider APIs → pineforge-data normalization → PineForge C ABI
```

The engine does not import or link this package. Provider transport,
authentication, retries, caching, symbol mapping, and vendor schemas stay here;
the engine receives only normalized bars and ordered trades.

## Why Python first

Provider integrations are dominated by HTTP, WebSockets, JSON, credentials,
and asynchronous I/O. Python makes those integrations accessible to community
contributors. Engine throughput remains native: normalized records are packed
into contiguous C ABI arrays and submitted in one call.

## Initial contracts

- `Instrument` — normalized symbol, provider-native ID, venue, asset/market type,
  currencies, contract terms, timezone, session, and volume units.
- `MarketListing` and `MarketQuery` — catalog discovery without vendor schemas.
- `Bar` — confirmed OHLCV with source provenance.
- `TradeTick` — the provider-neutral four-field engine payload plus provenance.
- `MacroObservation` — observation period, first release, and vintage timestamps
  to prevent revised-data lookahead.
- `MarketCatalogProvider`, `HistoricalBarProvider`, `LiveTradeProvider`, and
  `MacroDataProvider` — small structural protocols that community adapters
  implement.
- `ProviderRegistry` — built-in and installed broker adapters selected by name.
- `PfBar`, `PfTradeTick`, and `EngineStreamSink` — dependency-free `ctypes`
  interoperability with PineForge strategy libraries.

## CCXT adapter

The first community adapter uses CCXT's unified async API for exchange-neutral
crypto data. It paginates OHLCV, removes duplicate timestamps, excludes the
currently forming candle, and polls public trades into a strictly increasing
per-stream sequence.

```bash
pip install -e '.[ccxt]'
```

```python
from pineforge_data import BarRequest, CcxtProvider, Instrument

instrument = Instrument("BTC/USDT", venue="kraken")
request = BarRequest(
    instrument,
    timeframe="1m",
    start_ms=1_767_225_600_000,
    end_ms=1_767_312_000_000,
)

async with CcxtProvider("kraken") as provider:
    listing = await provider.resolve_market("BTC/USDT")
    confirmed_bars = await provider.fetch_bars(request)
```

`Instrument.symbol` uses CCXT's exact unified spelling. Do not infer a market
by parsing it: `resolve_market()` uses CCXT's catalog fields to distinguish
spot, swap, future, and option listings and captures `base`, `quote`, `settle`,
raw exchange ID, contract size, linear/inverse settlement, expiry, strike, and
option type. For example, `BTC/USDT` and `BTC/USDT:USDT` are separate markets.

Exchange credentials and exchange-specific options can be passed through
`config`, while endpoint options remain isolated in `market_params`,
`ohlcv_params`, and `trade_params`. Realtime public trades use REST polling in
this bootstrap; a WebSocket transport can implement the same
`LiveTradeProvider` contract later.

`TradeSubscription.start_ms` can pin the live handoff to the next timestamp
after an engine warmup. `start_sequence` is the last accepted sequence, so the
adapter emits `start_sequence + 1` next.

## Direct backtest harness

The public backtest input is raw PineScript v6. `pineforge-backtest` fetches
confirmed OHLCV through a data provider and runs this pinned pipeline:

```text
raw .pine + provider OHLCV
        ↓ read-only mount
Docker: Python codegen → C++ strategy → pineforge-engine → JSON report
```

Docker is a prerequisite. A host C++ compiler and a precompiled strategy
library are not required.

Clone with both pinned runtime dependencies:

```bash
git clone --recurse-submodules https://github.com/pineforge-4pass/pineforge-data.git
cd pineforge-data
python3 -m venv .venv
.venv/bin/pip install -e '.[ccxt]'
```

For an existing checkout:

```bash
git submodule update --init
```

```bash
.venv/bin/pineforge-backtest \
  --pine strategy.pine \
  --provider ccxt \
  --venue kraken \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2026-07-01T00:00:00Z \
  --end 2026-07-08T00:00:00Z \
  --output report.json \
  --pretty
```

The first invocation builds a local image tagged from the container source and
the exact codegen and engine submodule commits. Later invocations reuse it.
Pass `--rebuild-image` to force a rebuild or `--no-image-build` to require a
prebuilt local image.
Compilation and execution run as a non-root user with networking disabled, all
Linux capabilities dropped, a read-only root filesystem, and only a read-only
temporary input mount.

The JSON report contains data provenance, processed-bar counts, every closed
trade, all/long/short trade statistics, equity statistics, security-feed
diagnostics, optional trace values, and the complete equity curve. Unix
millisecond timestamps can be used instead of ISO-8601 values.

Use `--provider-config config.json` for CCXT constructor options and
`--strategy-params inputs.json` for Pine input overrides. The provider config
file may contain credentials, so keep it outside version control.

The report records the Pine source hash, generated C++ hash, transpile and
compile timings, and the exact codegen and engine commits. The OSS codegen is
source-available under its own PolyForm Noncommercial license and supplemental
terms; review `vendor/pineforge-codegen-oss/LICENSE` before distribution or
commercial use. The engine remains Apache-2.0.

Provider implementations in this repository are Python-only. The compiled C++
strategy and engine stay behind the Docker/runtime boundary; broker SDKs and
provider-specific types do not cross into `pineforge-engine`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,ccxt]'
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
PINEFORGE_DOCKER_TEST=1 .venv/bin/pytest tests/test_docker_integration.py
```

## Provider boundary

A provider adapter should:

1. expose exact market discovery and symbol resolution;
2. fetch or subscribe to its external service;
3. retain source and instrument provenance;
4. normalize timestamps to Unix milliseconds and records to the public models;
5. emit stable ordering sequences when the provider supplies them;
6. batch records before crossing the engine ABI when practical.

It should not add provider-specific fields to `pineforge-engine`. Data that the
engine does not consume remains in provider-owned metadata or higher-level
models in this repository.

See [the provider contract](docs/provider-contract.md) and
[CONTRIBUTING.md](CONTRIBUTING.md) before adding a provider.
