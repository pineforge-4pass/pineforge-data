# SQLAlchemy provider API

[Provider catalog](../providers.md) ·
[Tabular schema mapping](tabular-schema.md)

The `sqlalchemy` provider reflects one table or view through a synchronous
SQLAlchemy 2.x engine. It supports SQLAlchemy built-in and third-party dialects
without requiring ORM models or a PineForge-owned DDL.

## Install

Install PineForge's database extra and the driver for the target dialect:

```bash
pip install 'pineforge-data[database]' 'psycopg[binary]'  # PostgreSQL
pip install 'pineforge-data[database]' pymysql             # MySQL
```

The `database` extra installs SQLAlchemy, not every database driver. For
example, PostgreSQL may use `psycopg`, MySQL may use `pymysql`, and an
organization-specific dialect may have its own package.

Use an explicit synchronous driver in the URL:

```text
postgresql+psycopg://user:password@host/database
mysql+pymysql://user:password@host/database
```

## Construct the provider

```python
import os

from pineforge_data import SqlAlchemyBarProvider

provider = SqlAlchemyBarProvider(
    os.environ["PINEFORGE_DATABASE_URL"],
    table="daily_prices",
    schema="market_data",
    venue="warehouse",
    mapping={
        "timestamp": "trading_day",
        "symbol": "security_id",
    },
    timestamp_unit="iso8601",
)
```

The URL must select a synchronous SQLAlchemy driver. Provider calls run in a
worker thread so they do not block the async PineForge harness.

Use a database role with `SELECT` access only. PineForge Data issues reflection
and select operations, but database permissions remain the strongest safety
boundary.

## Inspect reflected metadata

```python
schema = await provider.inspect_schema()
for column in schema.columns:
    print(column.name, column.data_type, column.nullable)
```

Reflection loads the exact table/view and optional schema. The returned source
identity includes the PineForge venue and qualified table, not the connection
URL or credentials.

## Resolve and fetch bars

```python
from pineforge_data import BarRequest


async def load_database():
    listing = await provider.resolve_market("AAPL")
    return await provider.fetch_bars(
        BarRequest(
            listing.instrument,
            timeframe="1d",
            start_ms=1_735_689_600_000,
            end_ms=1_751_414_400_000,
        )
    )
```

Mapped symbol and timeframe columns use SQLAlchemy bound expressions. Numeric
timestamp ranges are converted into the configured source unit and pushed into
the query. Every row is checked again by the shared normalization layer.

For joins, expressions, timezone conversion, or vendor-specific cleanup,
create a database view and map the view. The provider intentionally does not
accept arbitrary SQL strings in harness configuration.

## Constructor reference

| Argument | Default | Purpose |
|---|---|---|
| `url` | required | synchronous SQLAlchemy database URL |
| `table` | required | exact reflected table or view |
| `venue` | `database` | source identity attached to instruments and provenance |
| `schema` | `None` | optional database schema |
| `mapping` | inferred | `BarColumnMapping` or partial overrides |
| `timestamp_unit` | `milliseconds` | numeric timestamp unit or `iso8601` |
| `timestamp_timezone` | `UTC` | IANA zone for naive date/datetime values |
| `instrument` | `None` | optional fixed instrument template |
| `timeframe` | `None` | optional fixed timeframe assertion |
| `engine_options` | `{}` | keyword options forwarded to `create_engine()` |

`hide_parameters=True` is enabled unless explicitly overridden in
`engine_options` so SQL logs do not include bound symbol or time values.

Call `await provider.close()` to dispose the engine and its connection pool.

## Harness configuration

Keep credentials out of JSON by naming an environment variable:

```json
{
  "url_env": "PINEFORGE_DATABASE_URL",
  "schema": "market_data",
  "table": "daily_prices",
  "timestamp_unit": "iso8601",
  "columns": {
    "timestamp": "trading_day",
    "symbol": "security_id"
  },
  "engine_options": {
    "pool_pre_ping": true,
    "pool_recycle": 1800
  }
}
```

```bash
export PINEFORGE_DATABASE_URL='postgresql+psycopg://user:password@db/market'
pineforge-backtest \
  --pine strategy.pine \
  --provider sqlalchemy \
  --provider-config database.json \
  --venue warehouse \
  --symbol AAPL \
  --timeframe 1d \
  --start 2025-01-01T00:00:00Z \
  --end 2025-07-02T00:00:00Z
```

A literal `url` is also accepted for local or non-sensitive configurations.
Configure only one of `url` and `url_env`. Registry configuration also accepts
`table`, `schema`, `engine_options`, and the shared
[tabular configuration keys](tabular-schema.md#shared-configuration-keys).

## Errors and limitations

| Error | Meaning |
|---|---|
| `SqlAlchemyDependencyError` | the `database` extra is not installed |
| `SchemaMappingError` | reflected columns cannot satisfy the OHLCV mapping |
| `TabularDataError` | a selected row cannot be normalized safely |

Driver import, authentication, reflection, connectivity, and database timeout
errors remain SQLAlchemy/dialect exceptions so operational tooling retains the
original cause.

Only synchronous engines are supported initially. Each provider owns one
engine and reflected table cache. Query pagination is delegated to the database
filter and ordering plan; PineForge Data currently materializes the selected
rows before final normalization and `limit` application.

## Corpus-backed compatibility test

Contributors can verify reflection, arbitrary column mapping, bound
symbol/timeframe filters, timestamp pushdown, and normalization against SQLite,
MySQL, and PostgreSQL with one command:

```bash
python -m pip install -e '.[dev,database-e2e]'
./scripts/run_database_e2e.sh
```

The three databases receive the same OHLCV slice from the PineForge validation
corpus. The test constructs providers through the public registry API and
requires every backend to return identical normalized bars. The containers and
their volumes are removed when the command exits.
