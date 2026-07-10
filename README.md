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
pip install 'pineforge-data[ccxt]'
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
        ↓ local read-only mount or FastAPI request
pineforge-release → generated C++ → cached/compiled strategy → JSON report
```

Docker is a prerequisite. A host C++ compiler and a precompiled strategy
library are not required. Install the package without cloning engine or codegen
repositories:

```bash
pip install 'pineforge-data[ccxt]'
```

```bash
pineforge-backtest \
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

The first local invocation pulls an immutable, multi-architecture
`pineforge-release` image pinned by both version and OCI digest. It never builds
engine or codegen locally. Use `--pull-policy never` for offline runs or opt in
to the rolling channel with:

```bash
pineforge-backtest ... \
  --runtime-image ghcr.io/pineforge-4pass/pineforge-release:latest \
  --pull-policy always
```

`latest` is convenient for development but not deterministic. The report
records the resolved image digest and component versions when Docker exposes
them.

Compilation and execution run as a non-root user with networking disabled, all
Linux capabilities dropped, a read-only root filesystem, and only a read-only
temporary input mount.

The JSON report contains provider and market provenance, the release runtime
identity and fingerprint, processed-bar counts, every closed trade,
all/long/short statistics, equity statistics, diagnostics, and the complete
equity curve. Unix millisecond timestamps can be used instead of ISO-8601
values. The pinned `pineforge-release` does not currently expose trace
collection; `--trace` fails explicitly rather than silently omitting it.

Use `--provider-config config.json` for CCXT constructor options and
`--strategy-params inputs.json` for Pine inputs. Use `--strategy-overrides` for
`strategy()` header overrides. The provider config file may contain
credentials, so keep it outside version control.

The generated C++ hash and exact engine/codegen versions are recorded in the
release fingerprint. The combined runtime and its component licensing are
owned by [`pineforge-release`](https://github.com/pineforge-4pass/pineforge-release),
not vendored into this repository.

Provider implementations in this repository are Python-only. The compiled C++
strategy and engine stay behind the Docker/runtime boundary; broker SDKs and
provider-specific types do not cross into `pineforge-engine`.

## Concurrent FastAPI server

The server image derives from the same pinned `pineforge-release` image. It
admits a bounded number of compiler/backtest processes, keeps a bounded queue,
isolates every request in its own temporary directory, and optionally requires
a bearer token.

```bash
docker build -f docker/server.Dockerfile -t pineforge-data-server .
docker volume create pineforge-compile-cache
docker run --rm -p 127.0.0.1:8000:8000 \
  --read-only \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount type=volume,src=pineforge-compile-cache,dst=/cache \
  -e PINEFORGE_SERVER_API_KEY=change-me \
  pineforge-data-server
```

Point the same harness at it without putting the token on the command line:

```bash
export PINEFORGE_SERVER_URL=http://127.0.0.1:8000
export PINEFORGE_SERVER_API_KEY=change-me
pineforge-backtest --pine strategy.pine --venue kraken --symbol BTC/USD \
  --timeframe 15m --start 2026-07-01T00:00:00Z --end 2026-07-08T00:00:00Z
```

The server always transpiles Pine deterministically and hashes the generated
C++. Its cache stores the compiled `.so` under a key containing that C++ hash
plus the release, engine, architecture, and compile flags. Concurrent misses
for the same key compile once; subsequent requests skip compilation. Cache
hit/key/hash are included in response provenance. See
[docs/server.md](docs/server.md) for endpoints, limits, deployment, and cache
settings.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,ccxt,server]'
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
