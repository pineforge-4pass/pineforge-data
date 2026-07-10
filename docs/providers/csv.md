# CSV provider API

[Provider catalog](../providers.md) ·
[Tabular schema mapping](tabular-schema.md)

The `csv` provider reads historical OHLCV from a local delimited file. It has
no optional dependency and never modifies the source.

## Construct the provider

```python
from pineforge_data import CsvBarProvider

provider = CsvBarProvider(
    "./exports/equity-bars.csv",
    venue="research",
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

The keys are PineForge's canonical fields and the values are exact CSV header
names. For example, the mapping above accepts this header:

```csv
epoch seconds,first px,top px,bottom px,last px,traded qty,security code,bar interval
```

Plain mappings may be complete or partial. When a canonical field is omitted,
common names such as `open`, `px_open`, `ticker`, and `interval` are inferred
from the header. Use an explicit `BarColumnMapping` when a typed, reusable
complete mapping is preferable.

## Inspect the header

```python
schema = await provider.inspect_schema()
print(schema.column_names)
mapping = schema.infer_bar_mapping({"timestamp": "epoch seconds"})
```

The first record must be a non-empty, unique header. Files with duplicate or
empty column names fail before data is fetched.

## Resolve and fetch bars

```python
from pineforge_data import BarRequest


async def load_csv():
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

When a symbol column is mapped, resolution and fetches use its exact values.
Without one, the complete file is treated as the single symbol passed to
`resolve_market()`.

The file is scanned for each catalog or bar request. This keeps the provider
dependency-free and deterministic, but SQLite or SQLAlchemy is a better fit for
large datasets queried repeatedly.

## Constructor reference

| Argument | Default | Purpose |
|---|---|---|
| `path` | required | local CSV path; `~` is expanded and the path is resolved |
| `venue` | `local` | source identity attached to instruments and provenance |
| `mapping` | inferred | `BarColumnMapping` or partial override mapping |
| `timestamp_unit` | `milliseconds` | numeric timestamp unit or `iso8601` |
| `timestamp_timezone` | `UTC` | IANA zone for naive date/time text |
| `instrument` | `None` | optional fixed instrument template |
| `timeframe` | `None` | optional fixed timeframe assertion |
| `encoding` | `utf-8-sig` | Python text encoding |
| `delimiter` | `,` | exactly one delimiter character |

## Harness configuration

```json
{
  "path": "/data/equity-bars.csv",
  "encoding": "utf-8-sig",
  "delimiter": ",",
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
  --provider csv \
  --provider-config csv.json \
  --venue research \
  --symbol AAPL \
  --timeframe 1m \
  --start 2025-07-01T00:00:00Z \
  --end 2025-07-02T00:00:00Z
```

Registry configuration accepts `path`, `encoding`, `delimiter`, and the shared
[tabular configuration keys](tabular-schema.md#shared-configuration-keys).
Unknown keys fail early.

## Errors and limitations

- A missing path raises `FileNotFoundError`.
- Missing, duplicate, or ambiguous columns raise `SchemaMappingError`.
- Rows with more values than the header or invalid OHLCV raise
  `TabularDataError`.
- CSV parsing is synchronous work moved off the asyncio event loop.
- No dialect sniffing is performed; specify `delimiter` explicitly.
- The provider does not cache an index or file contents between requests.
