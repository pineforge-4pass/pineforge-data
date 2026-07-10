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

## Documentation

- [Documentation home](docs/index.md) — architecture, guarantees, and guide map.
- [Getting started](docs/getting-started.md) — installation, first provider
  request, and local or remote backtest.
- [Normalized data model](docs/data-model.md) — instruments, contracts, bars,
  live trades, macro vintages, and validation rules.
- [Provider catalog](docs/providers.md) — shared lifecycle and second-level API
  guides for CCXT, CSV, SQLite, and SQLAlchemy.
- [Backtesting](docs/backtesting.md) — CLI options, configuration files, runtime
  channels, report schema, and reproducibility.
- [FastAPI server](docs/server.md) — concurrency, authentication, timeouts,
  compile cache, and deployment.
- [Provider contract](docs/provider-contract.md) — implementing and testing a
  community exchange or broker adapter.

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
- `BarColumnMapping` and `TabularSchema` — runtime discovery and safe OHLCV
  mappings for user-owned files, tables, and views.
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

## Local files and databases

Built-in `csv`, `sqlite`, and `sqlalchemy` providers let users backtest their
own data without adopting a fixed PineForge DDL. They inspect file headers or
database reflection metadata at runtime, infer common OHLCV names, and accept
partial mappings for arbitrary names. Ambiguous schemas fail instead of being
guessed.

```python
from pineforge_data import SqliteBarProvider


provider = SqliteBarProvider(
    "warehouse.sqlite3",
    table="price candles",
    mapping={
        "timestamp": "epoch seconds",
        "open": "first px",
        "high": "top px",
        "low": "bottom px",
        "close": "last px",
        "volume": "traded qty",
    },
    timestamp_unit="seconds",
)
schema = await provider.inspect_schema()
```

SQL identifiers are validated against reflected metadata; filter values are
bound parameters. For complex transformations, expose a database view rather
than putting raw SQL in harness configuration. See the complete
[provider catalog](docs/providers.md), with dedicated API guides for
[CSV](docs/providers/csv.md), [SQLite](docs/providers/sqlite.md), and
[SQLAlchemy](docs/providers/sqlalchemy.md).

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

## Contributing

Community providers, market-model improvements, server/runtime work, tests, and
documentation are welcome. Provider integrations are Python-only; engine and
codegen changes belong in their upstream repositories and are consumed here
through `pineforge-release`.

### Choose the right contribution path

| Contribution | Primary location | Start here |
|---|---|---|
| Exchange or broker adapter | `src/pineforge_data/providers/` | [Provider contract](docs/provider-contract.md) |
| Market, contract, bar, or request model | `src/pineforge_data/models.py`, `src/pineforge_data/requests.py`, `src/pineforge_data/providers/base.py` | Existing public models and protocols |
| Backtest harness or HTTP client | `src/pineforge_data/cli/backtest.py`, `src/pineforge_data/server_client.py` | Harness unit tests |
| FastAPI concurrency or compile cache | `src/pineforge_data/server.py`, `src/pineforge_data/compile_cache.py` | [Server guide](docs/server.md) |
| Release-container integration | `src/pineforge_data/release_contract.py`, `src/pineforge_data/docker_runtime.py` | Pinned release contract and Docker tests |
| Documentation or examples | `README.md`, `docs/` | A focused documentation PR |

For a new provider, implement the smallest applicable structural protocols,
register its factory, keep its SDK in an optional dependency extra, and add
offline fixture tests. Resolve exact upstream markets through their catalog;
do not parse symbols to infer base, quote, settlement, or contract terms.
Provider-specific fields stay in this repository and must not leak into
`pineforge-engine`.

### Development setup

```bash
git clone https://github.com/pineforge-4pass/pineforge-data.git
cd pineforge-data
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,ccxt,server]'
```

No Git submodules are required. Docker is needed only for release-runtime and
end-to-end backtest work.

### Before opening a pull request

1. Keep the change focused and document any public API, report-schema, provider,
   runtime-image, or cache-key compatibility impact.
2. Add deterministic offline tests; CI must not require credentials or live
   provider access.
3. Keep credentials out of fixtures, logs, exception messages, and committed
   configuration.
4. Run the standard checks:

   ```bash
   .venv/bin/ruff format --check src tests
   .venv/bin/ruff check .
   .venv/bin/mypy src
   .venv/bin/pytest
   .venv/bin/python -m build
   ```

5. For Docker, FastAPI server, cache, or release-contract changes, also run:

   ```bash
   PINEFORGE_DOCKER_TEST=1 .venv/bin/pytest tests/test_docker_integration.py
   ```

Read the [documentation home](docs/index.md) and
[CONTRIBUTING.md](CONTRIBUTING.md) for provider requirements,
determinism rules, external provider entry points, and the complete checklist.
For broad changes to public models or the report contract, open an issue first
so providers and runtime consumers can agree on the shape before implementation.
