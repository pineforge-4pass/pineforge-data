# Tunaxis Data

[![PyPI](https://img.shields.io/pypi/v/tunaxis-data.svg)](https://pypi.org/project/tunaxis-data/)
[![Python](https://img.shields.io/pypi/pyversions/tunaxis-data.svg)](https://pypi.org/project/tunaxis-data/)
[![CI](https://github.com/pineforge-4pass/pineforge-data/actions/workflows/ci.yml/badge.svg)](https://github.com/pineforge-4pass/pineforge-data/actions/workflows/ci.yml)
[![Documentation](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://pineforge-4pass.github.io/pineforge-data/)

Fetch normalized market data and run raw PineScript strategies through
Tunaxis.

Tunaxis Data provides Python adapters for exchanges, CSV files, SQLite, and
SQLAlchemy-compatible databases. Its backtest harness sends normalized OHLCV
to the `pineforge-release` container, which transpiles the PineScript, compiles
it, and runs `pineforge-engine`.

```text
exchange / CSV / database
          ↓
    tunaxis-data
          ↓
 pineforge-release container
          ↓
     backtest report
```

The provider layer is Python-only. You do not need a C++ compiler, Git
submodules, or local engine and codegen checkouts.

## Formerly PineForge

Tunaxis Data was formerly published as `pineforge-data`. The old distribution
now forwards to `tunaxis-data`; `import pineforge_data` and the
`pineforge-backtest` and `pineforge-backtest-server` commands keep working with
a `DeprecationWarning` for the old import.

## Quick start

You need Python 3.11 or newer. Docker is required only for backtests; fetching
and normalizing data does not use Docker.

### 1. Install an exchange provider

```bash
python -m pip install 'tunaxis-data[ccxt]'
```

### 2. Fetch confirmed OHLCV

```python
import asyncio

from tunaxis_data import BarRequest, CcxtProvider


async def main() -> None:
    async with CcxtProvider("bitstamp") as provider:
        market = await provider.resolve_market("BTC/USD")
        bars = await provider.fetch_bars(
            BarRequest(
                instrument=market.instrument,
                timeframe="15m",
                start_ms=1_751_328_000_000,
                end_ms=1_751_414_400_000,
            )
        )
        print(bars[-1])


asyncio.run(main())
```

Symbols are resolved through the provider catalog. For example, CCXT spot
`BTC/USDT` and linear swap `BTC/USDT:USDT` are distinct instruments with
different contract metadata.

### 3. Backtest raw PineScript

Save a strategy as `strategy.pine`:

```pinescript
//@version=6
strategy("SMA cross", initial_capital=10000, default_qty_type=strategy.percent_of_equity, default_qty_value=10)

fast = ta.sma(close, 2)
slow = ta.sma(close, 4)

if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)

if ta.crossunder(fast, slow)
    strategy.close("Long")
```

Then run:

```bash
tunaxis-backtest \
  --pine strategy.pine \
  --provider ccxt \
  --venue bitstamp \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z \
  --warmup-bars 100 \
  --output report.json \
  --pretty
```

The first run pulls the exact release and digest in `DEFAULT_RELEASE_IMAGE`.
The runtime and server defaults advance together through a tested release PR;
the [release guide](docs/releasing.md) describes the automated gates.
Results can differ between releases, notably for scripts
that omit their capital or order size; the
[runtime image policy](https://pineforge-4pass.github.io/pineforge-data/backtesting/#runtime-image-policy)
explains why and how to pin another release with `--runtime-image`.
The JSON report includes trades, performance
statistics, the equity curve, data provenance, and exact runtime versions. The
strategy is compiled inside an isolated Docker container; the provider and its
credentials remain on the host.

## Bring your own data

Tunaxis does not require a fixed table definition. Local providers inspect
headers or reflected database columns at runtime, infer common OHLCV names,
and accept an explicit mapping when your schema uses different names.

| Source | Install | Guide |
|---|---|---|
| CSV | `pip install tunaxis-data` | [CSV API](https://pineforge-4pass.github.io/pineforge-data/providers/csv/) |
| SQLite | `pip install tunaxis-data` | [SQLite API](https://pineforge-4pass.github.io/pineforge-data/providers/sqlite/) |
| SQLAlchemy database | `pip install 'tunaxis-data[database]'` plus a database driver | [SQLAlchemy API](https://pineforge-4pass.github.io/pineforge-data/providers/sqlalchemy/) |
| CCXT exchange | `pip install 'tunaxis-data[ccxt]'` | [CCXT API](https://pineforge-4pass.github.io/pineforge-data/providers/ccxt/) |

The same `tunaxis-backtest` command supports `--provider csv`, `sqlite`, and
`sqlalchemy`. See the [backtesting guide](https://pineforge-4pass.github.io/pineforge-data/backtesting/)
for provider configuration, warmup behavior, strategy inputs, reports, and
local versus remote execution.

## Documentation

| I want to… | Read… |
|---|---|
| Install and run the first request | [Getting started](https://pineforge-4pass.github.io/pineforge-data/getting-started/) |
| Look up Python classes and signatures | [API reference](https://pineforge-4pass.github.io/pineforge-data/api/) |
| Choose or configure a provider | [Provider catalog](https://pineforge-4pass.github.io/pineforge-data/providers/) |
| Understand instruments, contracts, and bars | [Data model](https://pineforge-4pass.github.io/pineforge-data/data-model/) |
| Run and reproduce backtests | [Backtesting](https://pineforge-4pass.github.io/pineforge-data/backtesting/) |
| Deploy the concurrent compile/backtest service | [FastAPI server](https://pineforge-4pass.github.io/pineforge-data/server/) |
| Implement a broker or exchange adapter | [Provider contract](https://pineforge-4pass.github.io/pineforge-data/provider-contract/) |

## Contributing

Community providers are welcome. Keep vendor SDKs and schemas inside the
adapter, resolve instruments through the upstream market catalog, normalize
timestamps to Unix milliseconds, and add deterministic offline tests.

```bash
git clone https://github.com/pineforge-4pass/pineforge-data.git
cd pineforge-data
python -m pip install -e '.[dev,ccxt,database-e2e,server,docs,release]'
ruff check .
mypy src
pytest
mkdocs build --strict
```

Run `./scripts/run_database_e2e.sh` to seed corpus OHLCV into disposable
SQLite, MySQL, and PostgreSQL databases and verify the public provider API
against all three.

Read [CONTRIBUTING.md](https://github.com/pineforge-4pass/pineforge-data/blob/main/CONTRIBUTING.md)
for the provider contract, entry points, security rules, and full validation
checklist.

Apache-2.0 licensed.
