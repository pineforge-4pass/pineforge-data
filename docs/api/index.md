# Python API reference

The reference is generated from the package's type annotations, signatures,
and docstrings. Import stable user-facing objects from `pineforge_data` unless
a guide explicitly documents a submodule path.

```python
from pineforge_data import Bar, BarRequest, CcxtProvider, Instrument
```

## Reference map

| Area | Use it for |
|---|---|
| [Models](models.md) | instruments, contracts, OHLCV bars, trades, and macro vintages |
| [Requests](requests.md) | market discovery, historical ranges, live handoffs, and macro queries |
| [Providers](providers.md) | provider protocols, registry extensions, CCXT, CSV, and databases |
| [Backtesting](backtesting.md) | raw-Pine release execution, compiled strategies, reports, and remote clients |
| [Streaming and C ABI](streaming.md) | packing normalized data into native arrays and feeding live trades |
| [Server components](server.md) | embedding the FastAPI service and managing its compile cache |

## Compatibility policy

Names exported by `pineforge_data.__all__` are the intended public Python API.
Modules, functions, and classes whose names begin with an underscore are
internal. The package is currently alpha, so incompatible public API changes
may occur before 1.0, but releases document and version those changes rather
than silently changing an installed version.

Provider implementations use structural protocols. A community package does
not need to inherit from a PineForge base class when it satisfies the required
typed methods. See the [provider contract](../provider-contract.md) for a
complete extension example.

## Optional dependencies

| Extra | Adds |
|---|---|
| `pineforge-data[ccxt]` | CCXT exchange provider |
| `pineforge-data[database]` | SQLAlchemy-compatible databases |
| `pineforge-data[server]` | FastAPI and Uvicorn server runtime |
| `pineforge-data[docs]` | local documentation build toolchain |
| `pineforge-data[release]` | distribution build and metadata checks |
