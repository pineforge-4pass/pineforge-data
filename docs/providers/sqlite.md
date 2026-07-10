# SQLite provider API

[Provider catalog](../providers.md) ·
[Tabular schema mapping](tabular-schema.md)

The `sqlite` provider reflects one local SQLite table or view and queries
historical OHLCV through Python's built-in SQLite driver. It requires no
optional package.

## Construct the provider

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

Table and column names may contain spaces or reserved words. The provider first
checks the table or view and mapping against `sqlite_schema` and `PRAGMA
table_info`, then quotes the reflected identifiers.

## Inspect the table or view

```python
schema = await provider.inspect_schema()
for column in schema.columns:
    print(column.name, column.data_type, column.nullable)
```

The provider opens a fresh read-only SQLite connection for each operation. It
never creates tables, runs migrations, or changes source data.

## Resolve and fetch bars

```python
from pineforge_data import BarRequest


async def load_sqlite():
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

Mapped symbol and timeframe values are bound parameters. Numeric timestamp
bounds are translated into the configured source unit and pushed into the SQL
query. The common normalization layer checks the interval again after reading
each row.

ISO-8601 timestamps are filtered after conversion because arbitrary stored text
formats do not always preserve chronological SQL ordering. For large text-time
tables, expose a view with a consistently sortable numeric epoch column.

## Constructor reference

| Argument | Default | Purpose |
|---|---|---|
| `path` | required | local SQLite database path |
| `table` | required | exact table or view name |
| `venue` | `local` | source identity attached to instruments and provenance |
| `mapping` | inferred | `BarColumnMapping` or partial overrides |
| `timestamp_unit` | `milliseconds` | numeric timestamp unit or `iso8601` |
| `timestamp_timezone` | `UTC` | IANA zone for naive date/time values |
| `instrument` | `None` | optional fixed instrument template |
| `timeframe` | `None` | optional fixed timeframe assertion |

## Harness configuration

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

```bash
pineforge-backtest \
  --pine strategy.pine \
  --provider sqlite \
  --provider-config sqlite.json \
  --venue warehouse \
  --symbol AAPL \
  --timeframe 1m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-02T00:00:00Z
```

Registry configuration accepts `path`, `table`, and the shared
[tabular configuration keys](tabular-schema.md#shared-configuration-keys).
Unknown keys fail early.

## Errors and limitations

- A missing database path raises `FileNotFoundError`.
- A missing table/view or a source with no columns raises `TabularDataError`.
- Missing or ambiguous mapped fields raise `SchemaMappingError`.
- SQLite connections use URI `mode=ro`; the filesystem must permit reads.
- Complex joins, expressions, or cleanup should be exposed as a SQLite view.
- The provider uses synchronous SQLite calls moved off the asyncio event loop.
