# Tabular schema mapping

[Provider catalog](../providers.md) · [CSV API](csv.md) ·
[SQLite API](sqlite.md) · [SQLAlchemy API](sqlalchemy.md)

CSV, SQLite, and SQLAlchemy providers normalize user-owned OHLCV without
requiring a PineForge DDL. They share one runtime discovery and mapping API:

```text
file / table / view
        ↓ inspect header or reflected metadata
timestamp, open, high, low, close, volume, [symbol], [timeframe]
        ↓ validate and normalize
PineForge Bar records
```

## Inspect a source

Every tabular provider exposes `inspect_schema()` before rows are normalized:

```python
from pineforge_data import CsvBarProvider


async def inspect_file():
    provider = CsvBarProvider("./vendor-export.csv", venue="research")
    schema = await provider.inspect_schema()
    for column in schema.columns:
        print(column.name, column.data_type, column.nullable)
```

`TabularSchema` contains the source identity and ordered `SourceColumn`
records. CSV types are reported as text; database providers report reflected
types and nullability.

## Infer common columns

`TabularSchema.infer_bar_mapping()` recognizes common names such as
`timestamp`, `datetime`, `open_time`, `PX_OPEN`, `qty`, `ticker`, and
`interval`. Supply only unusual fields:

```python
mapping = schema.infer_bar_mapping(
    {
        "timestamp": "Bucket Start",
        "volume": "Total Traded Qty",
    }
)
```

Inference never chooses between multiple plausible columns. Missing or
ambiguous fields raise `SchemaMappingError` containing the available columns
and the overrides needed to continue.

Provider constructors also accept a plain mapping. A plain mapping is a set of
partial overrides; omitted fields are inferred when the source is inspected.

## Define an explicit mapping

For a fully custom schema, map all six required OHLCV fields:

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

One source column cannot provide multiple canonical fields. Every selected
column must exist exactly in the inspected source.

## Timestamp values

Normalized timestamps are Unix milliseconds. Set `timestamp_unit` for numeric
source values:

| Value | Meaning |
|---|---|
| `seconds` or `s` | Unix seconds, including exact fractional milliseconds |
| `milliseconds` or `ms` | Unix milliseconds; the default |
| `microseconds` or `us` | Unix microseconds |
| `nanoseconds` or `ns` | Unix nanoseconds |
| `iso8601` | ISO-8601 text or database date/datetime objects |

ISO-8601 values with an offset or `Z` are unambiguous. Naive text or database
datetimes use `timestamp_timezone`, which defaults to `UTC` and accepts an IANA
zone such as `America/New_York`. Values that cannot be represented exactly at
millisecond precision fail rather than being rounded silently.

## Symbol behavior

- When a `symbol` column is mapped, `list_markets()` uses its distinct values,
  `resolve_market()` requires an exact value, and bar fetches filter it.
- Without a `symbol` column, the source is treated as a single-instrument
  dataset. `resolve_market("AAPL")` binds that dataset to the requested name.
- Set the provider configuration `symbol` when a single-instrument dataset
  should be advertised by `list_markets()` before resolution.

Symbols are compared exactly. A provider never rewrites case, punctuation, or
contract suffixes.

## Timeframe behavior

- When a `timeframe` column is mapped, each fetch filters it exactly.
- For a single-timeframe source without that column, set configuration
  `timeframe` to reject requests at another resolution.
- When neither exists, the request timeframe is an assertion supplied by the
  caller; PineForge Data cannot verify the source resolution.

## Shared normalization guarantees

Tabular providers:

- include only bars in the half-open interval `[start_ms, end_ms)`;
- sort results in ascending timestamp order;
- apply `limit` after normalization;
- reject duplicate timestamps for the requested symbol and timeframe;
- reject missing or non-finite values, invalid OHLC relationships, negative
  volume, and lossy sub-millisecond timestamps;
- attach the requested instrument and provider source to every `Bar`.

Rows are assumed to be confirmed snapshots because local files and databases
usually do not expose a provider clock or candle-close capability. The user
owns that snapshot guarantee.

## Shared configuration keys

| Key | Default | Purpose |
|---|---|---|
| `columns` | inferred | partial canonical-to-source mapping |
| `timestamp_unit` | `milliseconds` | numeric unit or `iso8601` |
| `timestamp_timezone` | `UTC` | IANA zone for naive date/datetime values |
| `symbol` | unset | fixed identity when no symbol column exists |
| `timeframe` | unset | fixed resolution when no timeframe column exists |

Provider-specific path, table, URL, parser, and engine keys are documented on
the individual API pages.

## Errors

`SchemaMappingError` reports a discovery or mapping problem before bar
normalization. `TabularDataError` reports a malformed row, duplicate timestamp,
or unsafe conversion. Both include source-facing context while avoiding
database credentials.
