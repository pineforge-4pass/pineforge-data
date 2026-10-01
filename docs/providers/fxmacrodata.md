# FXMacroData macro provider API

[Provider catalog](../providers.md) · [Normalized data model](../data-model.md)

`FxMacroDataProvider` implements `MacroDataProvider`. It reads economic
announcements (CPI, policy rates, payrolls, GDP and so on) from the
[FXMacroData API](https://fxmacrodata.com/documentation/reference)
and returns `MacroObservation` records with release and vintage timestamps. It
has no market catalog and no bars, so it is not a `MarketDataProvider` and is
not registered for `pineforge-backtest --provider`.

FXMacroData is a commercial API. Without a key only USD is available, limited
to the most recent 90 days and delayed by 15 minutes. Other currencies and full
history need an API key.

## Install

```bash
pip install pineforge-data
```

No extra is needed: the default transport uses the standard library.

## Construct the provider

```python
import os

from pineforge_data import FxMacroDataProvider, MacroRequest


async def us_payrolls():
    async with FxMacroDataProvider(api_key=os.environ.get("FXMACRODATA_API_KEY")) as provider:
        return await provider.fetch_observations(
            MacroRequest(
                key="non_farm_payrolls",
                currency="USD",
                start_ms=1_775_001_600_000,  # 2026-04-01
                end_ms=1_790_812_800_000,  # 2026-10-01
            )
        )
```

`key` is an FXMacroData indicator slug, such as `inflation`, `policy_rate`,
`non_farm_payrolls` or `gdp`. The per-currency list is served by
`GET /v1/data_catalogue/{currency}`. `currency` is a currency code (`USD`,
`EUR`, `JPY` and so on).

The key is sent only in the `X-API-Key` header. It is not placed in URLs,
exception messages or the provider's `repr`.

## Timestamps and vintages

| Field | Taken from |
|---|---|
| `period_end_ms` | UTC midnight of the record's `date` |
| `released_at_ms` | the record's `announcement_datetime` |
| `vintage_at_ms` | when that value became available (below) |
| `source` | `fxmacrodata:<publisher>`, for example `fxmacrodata:BLS` |

`date` is the end of the reference period for periodic series: the last day
of the month for monthly data, the week-ending date for weekly data. For
event series such as `policy_rate` it is the decision date, and for daily
series such as government bond yields it is the observation date.

The provider requests `revisions=all` and emits one observation each time a
period's value changed:

- a snapshot (`vintage_status: captured_snapshot` or `legacy_snapshot`) is
  dated by `observed_at_ns`, the time FXMacroData read it from the publisher,
  and skipped when that is absent. Its `epoch` repeats the original release
  time, so using it would date a later revision back to the first print;
- any other revision is dated by `publication_at_ns`, else `epoch`. On
  revisions without a `vintage_status`, `epoch` is either the publication time
  of that vintage or the time FXMacroData collected it; either way it is not
  earlier than the value was available;
- a record whose `revisions` is null or empty is treated as a single vintage
  of its own `val`;
- a vintage is never earlier than `released_at_ms`, and a vintage that repeats
  the previous value is dropped.

Some history was collected after publication and has only a captured snapshot.
Those values are dated by when they were collected, so a backtest set before
that date does not see them. This is deliberate: the alternative is
lookahead.

## Records that are kept or skipped

Records with `publication_time_status: unverified` are kept. Their
`announcement_datetime` is a real timestamp whose origin the row does not
evidence; only `confirmed` is evidence of when a value became public. Where a
release time came from a release calendar, the planned time can precede the
actual release, so treat unverified times as approximate.

`MacroObservation` requires a release time, and the provider does not invent
one. These records are left out and counted in `provider.last_skipped` for the
most recent fetch:

| Reason | Meaning |
|---|---|
| `unknown_release_time` | `announcement_datetime` is null (`publication_time_status: unknown`) |
| `assumed_release_time` | the time was derived by FXMacroData from the series' usual publication lag, not captured (`release_time_assumed` or `publication_time_status: assumed_historical`). Pass `include_assumed_release_times=True` to keep them |
| `released_before_period_end` | the release precedes the period date, for example a flash estimate, which `MacroObservation` cannot represent |
| `revision_without_vintage_time` | a revision has no usable availability time |
| `missing_value` | a record or revision has a null value |

## Constructor reference

| Argument | Default | Meaning |
|---|---|---|
| `api_key` | `None` | FXMacroData API key; `None` uses the keyless USD tier |
| `api_url` | `https://api.fxmacrodata.com/v1` | API root |
| `timeout_seconds` | `20.0` | per-request timeout of the default transport |
| `max_retries` | `2` | retries after HTTP 429, 500, 502, 503 or 504 |
| `retry_backoff_seconds` | `1.0` | first retry delay, doubled each attempt |
| `max_retry_after_seconds` | `60.0` | a server `Retry-After` up to this is honored; above it the backoff schedule is used |
| `max_pages` | `1000` | pages followed per fetch before `FxMacroDataDataError` is raised |
| `include_assumed_release_times` | `False` | keep records whose release time was derived rather than captured |
| `transport` | `None` | an `FxMacroDataTransport`; inject one for offline tests or a custom HTTP client |

Pages hold at most 100 records; the provider follows `pagination.has_more` and
`next_offset` until the request is complete, up to `max_pages` pages.

`key` must be an indicator slug (lowercase letters, digits and underscores) and
`currency` a three-letter code; anything else raises `ValueError` before a
request is made. The default transport does not follow redirects, so the API
key is only sent to `api_url`; a 3xx response raises `FxMacroDataHTTPError`.

## Errors and limitations

| Error | Meaning |
|---|---|
| `FxMacroDataHTTPError` | a non-success status after retries, including a refused redirect; `.status` holds the code (403 for a currency or history the key does not cover). The key is redacted from the server's message |
| `FxMacroDataDataError` | a response or record cannot be normalized safely |
| `FxMacroDataError` | base class; also raised for network failures |
| `FxMacroDataAccessWarning` | a warning, not an error: the keyless tier cut the requested history short or withheld a release from the last 15 minutes |

- Units come from the response's `value_metadata.source_unit`; `unknown` is
  used when it is absent.
- Responses are not cached. Snapshot them at your job boundary when exact
  replay matters.
- Only the announcements endpoint is used. Release calendars, consensus
  forecasts and FX prices are out of scope for this provider.
