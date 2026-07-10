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

If profiling later identifies a normalization hot path, it can gain an optional
native extension without changing the public provider contracts.

## Initial contracts

- `Instrument` — normalized symbol, venue, timezone, session, and volume units.
- `Bar` — confirmed OHLCV with source provenance.
- `TradeTick` — the provider-neutral four-field engine payload plus provenance.
- `MacroObservation` — observation period, first release, and vintage timestamps
  to prevent revised-data lookahead.
- `HistoricalBarProvider`, `LiveTradeProvider`, and `MacroDataProvider` — small
  structural protocols that community adapters implement.
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
    confirmed_bars = await provider.fetch_bars(request)
```

`Instrument.symbol` uses CCXT's unified spelling. Exchange credentials and
exchange-specific options can be passed through `config`, while endpoint
options remain isolated in `ohlcv_params` and `trade_params`. Realtime public
trades use REST polling in this bootstrap; a WebSocket transport can implement
the same `LiveTradeProvider` contract later.

`TradeSubscription.start_ms` can pin the live handoff to the next timestamp
after an engine warmup. `start_sequence` is the last accepted sequence, so the
adapter emits `start_sequence + 1` next.

## Direct backtest harness

`pineforge-backtest` fetches confirmed OHLCV through a data provider, packs the
normalized bars into the PineForge C ABI, and calls a compiled strategy library
directly. It does not create an intermediate CSV.

```bash
pineforge-backtest \
  --strategy /path/to/strategy.so \
  --exchange kraken \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2026-07-01T00:00:00Z \
  --end 2026-07-08T00:00:00Z \
  --output report.json \
  --pretty
```

The JSON report contains data provenance, processed-bar counts, every closed
trade, all/long/short trade statistics, equity statistics, security-feed
diagnostics, optional trace values, and the complete equity curve. Unix
millisecond timestamps can be used instead of ISO-8601 values.

Use `--provider-config config.json` for CCXT constructor options and
`--strategy-params inputs.json` for Pine input overrides. The provider config
file may contain credentials, so keep it outside version control.

Provider implementations are organized by their strongest supported runtime.
The current Python bucket contains CCXT and the harness; native low-latency
providers will live in the C++ bucket. Both buckets must emit the same
normalized records, but an individual provider does not need implementations
in both languages.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,ccxt]'
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

## Provider boundary

A provider adapter should:

1. fetch or subscribe to its external service;
2. retain source and instrument provenance;
3. normalize timestamps to Unix milliseconds and records to the public models;
4. emit stable ordering sequences when the provider supplies them;
5. batch records before crossing the engine ABI when practical.

It should not add provider-specific fields to `pineforge-engine`. Data that the
engine does not consume remains in provider-owned metadata or higher-level
models in this repository.

See [CONTRIBUTING.md](CONTRIBUTING.md) before adding a provider.
