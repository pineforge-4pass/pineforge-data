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
  --venue kraken \
  --symbol BTC/USD \
  --timeframe 15m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-08T00:00:00Z
```

| Group | Options |
|---|---|
| Strategy | `--pine`, `--strategy-params`, `--strategy-overrides` |
| Data source | `--provider`, `--venue`/`--exchange`, `--symbol`, `--timeframe`, `--start`, `--end`, `--limit`, `--provider-config` |
| Pine context | `--timezone`, `--session`, `--engine-timeframe`, `--script-timeframe` |
| Fill modeling | `--bar-magnifier`, `--magnifier-samples` |
| Local runtime | `--runtime-image`/`--image`, `--pull-policy`, `--execution-timeout` |
| Remote runtime | `--server-url`, `--server-api-key-env`, `--execution-timeout` |
| Output | `--output`, `--pretty` |

`--start` and `--end` accept Unix milliseconds or timezone-aware ISO-8601. The
end is exclusive. `--engine-timeframe` defaults to a Pine-compatible conversion
of the provider timeframe, and `--script-timeframe` defaults to the engine
timeframe.

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

## Runtime image policy

The package default is an exact `pineforge-release` version and OCI digest. The
`missing` pull policy downloads it only when absent; `never` supports offline
runs; `always` refreshes a tag before running.

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
    "name": "ccxt:kraken",
    "adapter": "ccxt",
    "venue": "kraken",
    "source_timeframe": "15m",
    "market": {
      "symbol": "BTC/USD",
      "provider_id": "XXBTZUSD",
      "market_type": "spot",
      "contract": null
    }
  },
  "data": {
    "requested_start_ms": 1751328000000,
    "requested_end_ms": 1751932800000,
    "first_bar_ms": 1751328000000,
    "last_bar_ms": 1751931900000,
    "bars": 672
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
