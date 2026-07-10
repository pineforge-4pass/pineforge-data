# PineForge Data documentation

PineForge Data connects external market and macro providers to deterministic
PineForge backtests. It owns provider transport, market discovery, normalized
records, data provenance, and the local or remote handoff to
`pineforge-release`.

```text
exchange / broker / macro API
             ↓
      Python provider adapter
             ↓
 normalized market + bars + trades
             ↓
 local pineforge-release or FastAPI server
             ↓
 versioned report with data/runtime provenance
```

## Choose a guide

| Goal | Guide |
|---|---|
| Install the package and run a first backtest | [Getting started](getting-started.md) |
| Understand instruments, contracts, bars, trades, and macro vintages | [Data model](data-model.md) |
| Choose a provider and open its API guide | [Provider catalog](providers.md) |
| Configure local and remote raw-Pine backtests | [Backtesting](backtesting.md) |
| Deploy and operate the concurrent FastAPI service | [FastAPI server](server.md) |
| Implement a new exchange or broker adapter | [Provider contract](provider-contract.md) |
| Prepare a contribution | [Contributing](../CONTRIBUTING.md) |

## Package boundaries

- `pineforge-data` is Python-only and owns external data integration.
- `pineforge-release` owns Pine transpilation, C++ compilation, and the engine
  runtime image.
- `pineforge-engine` receives normalized arrays and must not depend on provider
  SDKs or vendor-specific schemas.
- Provider credentials stay on the data-fetching host. The FastAPI backtest
  request contains normalized data, not provider credentials.

## Core guarantees

- timestamps use Unix milliseconds;
- normalized records retain their instrument and source provenance;
- historical bars exclude the currently forming candle when the provider can
  identify it;
- exact catalog resolution distinguishes spot, swaps, futures, and options;
- user-owned tabular schemas are reflected and mapped explicitly rather than
  requiring a PineForge-owned DDL;
- macro observations retain release and vintage timestamps to avoid revised-
  data lookahead;
- backtest reports identify both the resolved market and runtime versions.

The package does not promise that every provider exposes every protocol. A
provider may support historical bars without live trades, or macro observations
without a market catalog.
