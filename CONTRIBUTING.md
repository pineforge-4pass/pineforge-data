# Contributing

Community data providers are the reason this repository exists. Keep the core
contracts small and put vendor behavior behind provider modules.

Docker and initialized codegen/engine submodules are required for raw-Pine
integration work:

```bash
git submodule update --init
docker version
```

## Provider checklist

- Implement one or more protocols from `pineforge_data.providers`.
- Normalize all timestamps to Unix milliseconds.
- Preserve the source name and normalized instrument on every record.
- Keep credentials out of logs and exception messages; prefer authorization
  headers when the upstream API supports them.
- Make retries, timeouts, and rate limits explicit.
- Add offline fixture tests. CI must not require network access or API keys.
- Declare provider-specific libraries as optional dependencies.
- Keep provider-specific request parameters inside the adapter rather than
  extending the normalized records.
- Never make `pineforge-engine` depend on this package.

## Determinism and macro vintages

Historical results must be reproducible. Cache or snapshot mutable upstream
responses when exact replay matters. Macroeconomic values require both the
original release timestamp and the timestamp at which a particular vintage
became available; using today's revised value in an old backtest is lookahead.

## Checks

```bash
ruff check .
mypy src
pytest
python -m build
PINEFORGE_DOCKER_TEST=1 pytest tests/test_docker_integration.py
```
