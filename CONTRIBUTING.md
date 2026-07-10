# Contributing

Community data providers are the reason this repository exists. Keep the core
contracts small and put vendor behavior behind Python provider modules. This
repository does not accept a separate C++ provider implementation surface.

User-facing architecture and API guides are indexed in
[docs/index.md](docs/index.md). Update the applicable guide when changing public
models, provider behavior, CLI options, report fields, runtime channels, or
server/cache semantics.

Docker is required for raw-Pine integration work. Engine and codegen are
consumed only through the pinned `pineforge-release` image; do not add source
submodules or duplicate their build logic here.

```bash
docker version
```

## Provider checklist

- Implement `MarketDataProvider` for backtest-compatible adapters; live-trade
  and macro support remain separate optional protocols.
- Resolve exact normalized symbols from the upstream market catalog. Do not
  infer base, quote, settlement currency, or contract terms by parsing symbols.
- Preserve both the normalized symbol and the provider-native `provider_id`.
- Represent spot/cash/CFD/swap/future/option separately and populate derivative
  contract size, linear/inverse flags, expiry, strike, and option type when the
  upstream supplies them.
- Normalize all timestamps to Unix milliseconds.
- Preserve the source name and normalized instrument on every record.
- Keep credentials out of logs and exception messages; prefer authorization
  headers when the upstream API supports them.
- Make retries, timeouts, and rate limits explicit.
- Add offline fixture tests. CI must not require network access or API keys.
- Declare provider-specific libraries as optional dependencies.
- Keep local/database adapters read-only, validate mapped identifiers against
  reflected metadata, and bind query values instead of accepting raw SQL.
- Keep provider-specific request parameters inside the adapter rather than
  extending the normalized records.
- Never make `pineforge-engine` depend on this package.

Adapters may be contributed in-tree or shipped by another Python package. An
external package registers a factory without changing this repository's CLI:

```toml
[project.entry-points."pineforge_data.providers"]
example = "example_pineforge:provider_factory"
```

The callable receives `(venue, config)` and returns a structural
`MarketDataProvider`. See [docs/provider-contract.md](docs/provider-contract.md)
for the normalized model and a complete skeleton.

Every provider must have a second-level API guide at
`docs/providers/<provider>.md` and a catalog row in
`docs/providers.md`. Keep provider-specific installation, constructor,
configuration, behavior, errors, and limitations out of the catalog page.

## Determinism and macro vintages

Historical results must be reproducible. Cache or snapshot mutable upstream
responses when exact replay matters. Macroeconomic values require both the
original release timestamp and the timestamp at which a particular vintage
became available; using today's revised value in an old backtest is lookahead.

## Checks

```bash
python -m pip install -e '.[dev,ccxt,database,server,docs,release]'
ruff check .
mypy src
pytest
mkdocs build --strict
python -m build
python -m twine check dist/*
PINEFORGE_DOCKER_TEST=1 pytest tests/test_docker_integration.py
```

Maintainers should follow the version, tag, artifact, and Trusted Publishing
steps in the [release guide](docs/releasing.md). Contributors cannot publish a
package from a pull request.
