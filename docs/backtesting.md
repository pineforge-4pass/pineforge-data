# Backtesting with PineForge Data

The harness accepts raw PineScript. Data acquisition stays in Python while
transpilation, compilation, and deterministic execution stay in
`pineforge-release`.

## Execution modes

### Local release container

Without a server URL, the harness pulls and runs the pinned release image
locally. The container has networking disabled, a read-only root filesystem,
dropped capabilities, `no-new-privileges`, a read-only input mount, and an
executable `/tmp` tmpfs required for strategy compilation.

### FastAPI server

Set `--server-url` or `PINEFORGE_SERVER_URL` to send raw Pine and normalized
bars to the concurrent server. Provider credentials and provider API calls stay
on the harness host. Set `PINEFORGE_SERVER_API_KEY` for bearer authentication.

The server returns synchronously while accepting multiple requests. Capacity,
queueing, timeouts, authentication, deployment, and compiled-strategy caching
are documented in the [server guide](server.md).

## CLI reference

```bash
pineforge-backtest \
  --pine strategy.pine \
  --provider ccxt \
  --venue bitstamp \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z
```

| Group | Options |
|---|---|
| Strategy | `--pine`, `--strategy-params`, `--strategy-overrides` |
| Data source | `--provider`, `--venue`/`--exchange`, `--symbol`, `--timeframe`, `--start`, `--end`, `--limit`, `--warmup-bars`, `--provider-config` |
| Pine context | `--timezone`, `--session`, `--engine-timeframe`, `--script-timeframe` |
| Fill modeling | `--bar-magnifier`, `--magnifier-samples` |
| Local runtime | `--runtime-image`/`--image`, `--pull-policy`, `--execution-timeout` |
| Remote runtime | `--server-url`, `--server-api-key-env`, `--execution-timeout` |
| Output | `--output`, `--pretty` |

`--start` and `--end` accept Unix milliseconds or timezone-aware ISO-8601. The
end is exclusive. `--engine-timeframe` defaults to a Pine-compatible conversion
of the provider timeframe, and `--script-timeframe` defaults to the engine
timeframe.

### Indicator warmup

Use `--warmup-bars` to load source bars before `--start` without allowing the
strategy to place orders during that earlier interval:

```bash
pineforge-backtest \
  --pine strategy.pine \
  --provider ccxt \
  --venue bitstamp \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z \
  --warmup-bars 500
```

The warmup bars initialize indicators, higher-timeframe feeds, and persistent
Pine variables. PineForge suppresses order commands until `--start`, so broker
state and trade counters begin at the requested backtest boundary. The default
is `0` for backward compatibility.

Providers can return fewer warmup bars when history is unavailable or the
market has gaps. The report records `warmup_bars_requested`,
`warmup_bars_loaded`, the expanded `provider_start_ms`, and the effective
`trade_start_time_ms`. When `--limit` is set, warmup capacity is added to that
limit so it does not consume the requested-window allowance.

## Configuration files

Provider constructor configuration:

```json
{
  "enableRateLimit": true,
  "timeout": 30000
}
```

```bash
--provider-config provider.json
```

Pine input overrides:

```json
{
  "Fast Length": 8,
  "Slow Length": 21
}
```

```bash
--strategy-params inputs.json
```

`strategy()` header overrides use a separate file:

```json
{
  "default_qty_value": 5,
  "commission_value": 0.04
}
```

```bash
--strategy-overrides overrides.json
```

Input and override values sent to the FastAPI service must be scalar strings,
numbers, or booleans.

The same harness accepts `--provider csv`, `--provider sqlite`, or
`--provider sqlalchemy`. Their provider configuration maps arbitrary source
columns to normalized OHLCV, while raw Pine and normalized bars follow the
same local-container or FastAPI path. See
the [provider catalog](providers.md) for their individual API and configuration
guides.

## Runtime image policy

The package default is the exact release and OCI digest exported as
`pineforge_data.DEFAULT_RELEASE_IMAGE`. The server Dockerfile carries the same
pin. Release updates commit the verified hub manifest and dependency lock next
to the package, increment the independently versioned data package, and pass
the [release gates](releasing.md) before publication. The `missing` pull
policy downloads it only when absent; `never` supports offline runs; `always`
refreshes a tag before running.

The previous default, release 0.1.12 (engine 0.11.0, codegen 0.9.0), accepts the
same inputs. To reproduce results made with it, pin it by digest:

```bash
--runtime-image ghcr.io/pineforge-4pass/pineforge-release:0.1.12@sha256:312b9d908390b828484617472c749d5815feb75507da87eae2f6902cfe3d47b1
```

Results can differ between releases: the README quick start reports 89 trades
on 1.0.0 and 88 on 0.1.12. The first 88 are the same trades; 1.0.0 adds the
close of the position still open after the last bar, marked `open_at_end`.

Order sizes differ too when a script omits `initial_capital`,
`default_qty_type` or `default_qty_value`. Codegen 1.0 gives them TradingView's
Pine v6 defaults: 100000, `strategy.percent_of_equity` and 100. Release 0.1.12
used 1000000, `strategy.fixed` and 1. At 100% of equity, the default 100% margin
refuses an entry that fills above its signal price, so such a script can trade
less on 1.0.0. Declare all three settings, as the README strategy does, to size
orders the same way on both releases.

The rolling channel is explicit:

```bash
--runtime-image ghcr.io/pineforge-4pass/pineforge-release:latest \
--pull-policy always
```

Use the pinned default for reproducible research. A rolling tag may change
engine or codegen behavior without a `pineforge-data` version change.

## Report envelope

The harness combines provider provenance with the release report:

```json
{
  "schema_version": 1,
  "request_id": null,
  "provider": {
    "name": "ccxt:bitstamp",
    "adapter": "ccxt",
    "venue": "bitstamp",
    "source_timeframe": "15m",
    "market": {
      "symbol": "BTC/USD",
      "provider_id": "btcusd",
      "market_type": "spot",
      "contract": null
    }
  },
  "data": {
    "requested_start_ms": 1751328000000,
    "requested_end_ms": 1751932800000,
    "provider_start_ms": 1750878000000,
    "first_bar_ms": 1750878000000,
    "last_bar_ms": 1751931900000,
    "bars": 1172,
    "requested_bars": 672,
    "warmup_bars_requested": 500,
    "warmup_bars_loaded": 500,
    "trade_start_time_ms": 1751328000000
  },
  "strategy": {
    "pine": "/path/to/strategy.pine",
    "input_timeframe": "15",
    "script_timeframe": "15"
  },
  "runtime": {
    "mode": "local-container",
    "release_image": "ghcr.io/...@sha256:..."
  },
  "backtest": {
    "summary": {},
    "trades": [],
    "metrics": {},
    "equity_curve": [],
    "fingerprint": {}
  }
}
```

Remote responses include a request ID. Server runtime provenance also includes
the generated C++ digest, compile-cache key/hit, and engine/codegen/release
versions. Local runs record the resolved image digest and OCI component labels
when Docker exposes them.

## Reproducibility checklist

Retain:

- raw Pine source and strategy input/override files;
- provider adapter and venue;
- exact resolved symbol and provider market ID;
- requested interval and normalized OHLCV snapshot or checksum;
- release image reference and resolved digest;
- report fingerprint and request ID.

The pinned release currently does not expose trace collection. `--trace` fails
explicitly rather than producing an incomplete report.
