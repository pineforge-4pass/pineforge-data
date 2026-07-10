# Connect local files and databases

PineForge Data can read historical OHLCV from a local CSV file, a SQLite table
or view, or any synchronous database supported by SQLAlchemy 2.x. The source
does not need to use PineForge column names or a prescribed DDL.

The safe boundary is a runtime column mapping:

```text
your file / table / view
        ↓ reflect header or database metadata
timestamp, open, high, low, close, volume, [symbol], [timeframe]
        ↓ validate and normalize
PineForge Bar records
```

PineForge Data never modifies a connected source. The native SQLite provider
opens the database in read-only mode. The SQLAlchemy provider performs
reflection and `SELECT` statements only; use a database account with read-only
permissions as defense in depth.

## Runtime schema discovery

Every tabular provider exposes `inspect_schema()` before data is loaded:

```python
from pineforge_data import CsvBarProvider


async def inspect_file():
    provider = CsvBarProvider("./vendor-export.csv", venue="research")
    schema = await provider.inspect_schema()
    for column in schema.columns:
        print(column.name, column.data_type, column.nullable)
```

`TabularSchema.infer_bar_mapping()` recognizes common names such as
`timestamp`, `datetime`, `open_time`, `PX_OPEN`, `qty`, `ticker`, and
`interval`. Supply only the unusual or ambiguous fields:

```python
mapping = schema.infer_bar_mapping(
    {
        "timestamp": "Bucket Start",
        "volume": "Total Traded Qty",
    }
)
```

Inference never chooses between multiple plausible columns. A missing or
ambiguous field raises `SchemaMappingError` with the available columns and the
overrides needed to continue. For a completely custom schema, define all six
required fields explicitly:

```python
from pineforge_data import BarColumnMapping

mapping = BarColumnMapping(
    timestamp="epoch seconds",
    open="first px",
    high="top px",
    low="bottom px",
    close="last px",
    volume="traded qty",
    symbol="security code",       # optional
    timeframe="bar interval",     # optional
)
```

Provider constructors also accept a plain mapping. Plain mappings are partial
overrides; any fields not supplied are inferred at runtime.

## Timestamp values

Normalized timestamps are always Unix milliseconds. Set `timestamp_unit` for
numeric source values:

| Value | Meaning |
|---|---|
| `seconds` or `s` | Unix seconds, including exact fractional milliseconds |
| `milliseconds` or `ms` | Unix milliseconds; the default |
| `microseconds` or `us` | Unix microseconds |
| `nanoseconds` or `ns` | Unix nanoseconds |
| `iso8601` | ISO-8601 text or database `datetime` objects |

ISO-8601 values with an offset or `Z` are unambiguous. Naive text or database
datetimes use `timestamp_timezone`, which defaults to `UTC` and accepts an IANA
zone such as `America/New_York`. Values that cannot be represented exactly at
millisecond precision are rejected rather than rounded silently.

## CSV

CSV requires no optional dependency:

```python
from pineforge_data import BarRequest, CsvBarProvider


async def load_csv():
    provider = CsvBarProvider(
        "./exports/equity-bars.csv",
        venue="research",
        mapping={
            "timestamp": "Bucket Start",
            "volume": "Total Traded Qty",
        },
        timestamp_unit="iso8601",
    )
    listing = await provider.resolve_market("AAPL")
    return await provider.fetch_bars(
        BarRequest(
            listing.instrument,
            timeframe="1m",
            start_ms=1_751_328_000_000,
            end_ms=1_751_414_400_000,
        )
    )
```

The default format is UTF-8 with a comma delimiter. Use `encoding` and
`delimiter` for other exports. CSV is scanned locally for each request; use a
database backend for large, repeatedly queried datasets.

Harness configuration:

```json
{
  "path": "/data/equity-bars.csv",
  "timestamp_unit": "iso8601",
  "columns": {
    "timestamp": "Bucket Start",
    "volume": "Total Traded Qty",
    "symbol": "Ticker",
    "timeframe": "Bar Size"
  }
}
```

```bash
pineforge-backtest \
  --pine strategy.pine \
  --provider csv \
  --provider-config csv.json \
  --venue research \
  --symbol AAPL \
  --timeframe 1m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-02T00:00:00Z
```

## SQLite

The native provider safely quotes table and column identifiers after verifying
them against SQLite schema metadata. Tables and views can contain spaces,
reserved words, or otherwise unconventional names.

```python
from pineforge_data import SqliteBarProvider

provider = SqliteBarProvider(
    "./warehouse.sqlite3",
    table="price candles",
    venue="warehouse",
    mapping={
        "timestamp": "epoch seconds",
        "open": "first px",
        "high": "top px",
        "low": "bottom px",
        "close": "last px",
        "volume": "traded qty",
        "symbol": "security code",
        "timeframe": "bar interval",
    },
    timestamp_unit="seconds",
)
```

Equivalent harness configuration:

```json
{
  "path": "/data/warehouse.sqlite3",
  "table": "price candles",
  "timestamp_unit": "seconds",
  "columns": {
    "timestamp": "epoch seconds",
    "open": "first px",
    "high": "top px",
    "low": "bottom px",
    "close": "last px",
    "volume": "traded qty",
    "symbol": "security code",
    "timeframe": "bar interval"
  }
}
```

Select it with `--provider sqlite`. Numeric timestamp, symbol, and timeframe
filters are pushed into the query; normalization still checks every returned
record.

## SQLAlchemy-compatible databases

Install SQLAlchemy support plus the driver for the target database:

```bash
pip install 'pineforge-data[database]' psycopg
```

The provider uses SQLAlchemy Core reflection, so it works with supported
PostgreSQL, MySQL, MariaDB, Oracle, Microsoft SQL Server, SQLite, and
third-party dialects. It intentionally accepts a table or view, not arbitrary
SQL text:

```python
import os

from pineforge_data import SqlAlchemyBarProvider

provider = SqlAlchemyBarProvider(
    os.environ["PINEFORGE_DATABASE_URL"],
    table="daily_prices",
    schema="market_data",
    venue="warehouse",
    mapping={"timestamp": "trading_day", "symbol": "security_id"},
    timestamp_unit="iso8601",
)
```

For joins, expressions, timezone conversion, or vendor-specific data cleanup,
create a database view and map that view. This preserves parameterized filters
and avoids placing raw SQL in a backtest configuration.

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
  }
}
```

Select it with `--provider sqlalchemy`. A literal `url` is also accepted for
local or non-sensitive configurations. `engine_options` passes SQLAlchemy
engine options such as pool settings; driver-specific packages remain the
user's dependency.

Only synchronous SQLAlchemy engines are supported initially. Provider calls
are moved off the asyncio event loop, so they remain compatible with the
async PineForge harness.

## Symbol and timeframe behavior

- When a `symbol` column is mapped, catalog listing and exact resolution use
  its distinct values, and every fetch filters it.
- Without a `symbol` column, the source is treated as a single-instrument
  dataset. `resolve_market("AAPL")` binds the dataset to that requested name.
  Set the provider configuration `symbol` when `list_markets()` must advertise
  it before resolution.
- When a `timeframe` column is mapped, every request filters it exactly.
- For a single-timeframe dataset without such a column, set configuration
  `timeframe` to reject accidental requests at another resolution.

All sources return rows in ascending time order, apply the half-open interval
`[start_ms, end_ms)`, and apply `limit` after normalization. Duplicate
timestamps for the same requested symbol and timeframe raise
`TabularDataError`; PineForge Data never silently selects one duplicate.

Local rows are assumed to be confirmed bars because a file or database usually
does not expose a provider clock or candle-close capability. The user owns that
snapshot guarantee. Invalid OHLC relationships, negative volume, missing
values, non-finite numbers, and sub-millisecond timestamp loss fail explicitly.

## Configuration reference

| Key | CSV | SQLite | SQLAlchemy | Purpose |
|---|---:|---:|---:|---|
| `path` | required | required | — | Local file/database path |
| `table` | — | required | required | Reflected table or view |
| `url` / `url_env` | — | — | one required | SQLAlchemy connection URL or its environment variable |
| `schema` | — | — | optional | Database schema name |
| `columns` | optional | optional | optional | Partial or complete canonical-to-source mapping |
| `timestamp_unit` | optional | optional | optional | Numeric unit or `iso8601` |
| `timestamp_timezone` | optional | optional | optional | IANA zone for naive datetime values |
| `symbol` | optional | optional | optional | Fixed identity when no symbol column exists |
| `timeframe` | optional | optional | optional | Fixed resolution when no timeframe column exists |
| `encoding`, `delimiter` | optional | — | — | CSV parser settings |
| `engine_options` | — | — | optional | SQLAlchemy `create_engine()` options |

Unknown configuration keys fail early to catch misspellings.
