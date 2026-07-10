# FastAPI backtest server

The server is a thin concurrent control plane layered on the published
`pineforge-release` image. Market data remains a host-side provider concern;
the harness sends normalized OHLCV, raw PineScript, syminfo, runtime options,
and strategy inputs to `POST /v1/backtests`.

## Endpoints

- `GET /healthz` — process liveness.
- `GET /readyz` — release tools, concurrency counters, and cache statistics.
- `POST /v1/backtests` — synchronous backtest request; concurrent HTTP requests
  execute independently up to the configured process limit.
- `GET /docs` — generated OpenAPI documentation.

Clients may supply `X-Request-ID`; otherwise the server creates one. When
`PINEFORGE_SERVER_API_KEY` is set, the backtest endpoint requires
`Authorization: Bearer <token>`. Health endpoints intentionally remain
unauthenticated for container orchestration.

## Concurrency and overload behavior

Run one Uvicorn worker per container. The service owns a process-wide semaphore;
additional Uvicorn workers would multiply the configured compiler limit.
Scale horizontally with multiple containers when more capacity is required.

| Environment variable | Default | Meaning |
|---|---:|---|
| `PINEFORGE_SERVER_CONCURRENCY` | `2` in the image | simultaneous compile/backtest processes |
| `PINEFORGE_SERVER_MAX_QUEUE` | `8` in the image | admitted requests waiting for a slot |
| `PINEFORGE_SERVER_QUEUE_TIMEOUT` | `30` | seconds before a queued request returns 503 |
| `PINEFORGE_SERVER_EXECUTION_TIMEOUT` | `300` | total transpile, compile, and backtest deadline |

When running and queued capacity is full, new requests receive HTTP 429. A
transpile, compile, or engine failure returns HTTP 422 with a structured phase,
code, message, and request ID. Execution timeouts return HTTP 504.

## Compile cache

Pine source is transpiled for each request. The server hashes the generated C++
and combines that digest with the release image, engine/codegen versions,
architecture, and parity-critical compiler flags. The resulting key identifies
the compiled `.so` artifact.

This deliberately does not key compiled artifacts by Pine source: codegen
changes can alter generated C++ for identical Pine, while different Pine inputs
can theoretically produce the same translation unit. Writes are atomic and
concurrent misses for one key are deduplicated.

| Environment variable | Default | Meaning |
|---|---:|---|
| `PINEFORGE_SERVER_CACHE_DIR` | `/cache` | compiled artifact directory |
| `PINEFORGE_SERVER_CACHE_MAX_ENTRIES` | `1024` | maximum retained `.so` files |
| `PINEFORGE_SERVER_CACHE_MAX_BYTES` | `2147483648` | maximum retained bytes |

Mount `/cache` as a named volume to preserve compiled strategies across server
restarts. The cache metadata returned with each result includes `key`, `hit`,
and `generated_cpp_sha256`.

## Runtime channel

The Dockerfile defaults to a semver-and-digest-pinned release image. Override it
at build time for a rolling development server:

```bash
docker build -f docker/server.Dockerfile \
  --build-arg PINEFORGE_RELEASE_IMAGE=ghcr.io/pineforge-4pass/pineforge-release:latest \
  -t pineforge-data-server:latest .
```

Do not use the rolling channel for reproducibility-sensitive production runs.
The cache key includes component/release identity, so a runtime upgrade cannot
reuse a compiled artifact from a different engine version.

## Deployment boundary

The server accepts CPU- and memory-intensive work. Bind it to a private network,
set an API key, enforce request-size and rate limits at the ingress, and do not
expose it directly to the public internet. The container runs as UID 10001;
deploy it with a read-only root filesystem, an executable `/tmp` tmpfs, dropped
capabilities, `no-new-privileges`, and a writable cache volume.
