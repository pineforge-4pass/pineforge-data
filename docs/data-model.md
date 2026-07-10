# Normalized data model

The public records are frozen, slotted dataclasses. Invalid values fail at
construction time so provider errors do not silently reach a backtest.

## Instrument identity

An `Instrument` separates portable market meaning from provider identity.

| Field | Meaning |
|---|---|
| `symbol` | provider-normalized public symbol used in requests |
| `venue` | exchange, broker environment, or data-source instance |
| `provider_id` | raw provider/venue market ID retained for provenance |
| `asset_class` | crypto, equity, forex, commodity, index, fund, bond, or unknown |
| `market_type` | spot, cash, swap, future, option, CFD, or unknown |
| `base`, `quote`, `settle` | catalog-supplied assets/currencies |
| `contract` | derivative terms, or `None` for non-contract markets |
| `timezone` | IANA timezone used by Pine date/session behavior |
| `session` | Pine-compatible session description |
| `volume_unit` | meaning of bar/trade volume, such as base or contracts |

Do not parse `symbol` to derive the other fields. Providers must populate them
from their market catalog.

### Spot example

```python
from pineforge_data import AssetClass, Instrument, MarketType

spot = Instrument(
    symbol="BTC/USD",
    venue="kraken",
    provider_id="XXBTZUSD",
    asset_class=AssetClass.CRYPTO,
    market_type=MarketType.SPOT,
    base="BTC",
    quote="USD",
)
```

### Contract example

```python
from pineforge_data import ContractSpec, Instrument, MarketType

swap = Instrument(
    symbol="BTC/USDT:USDT",
    venue="exchange",
    market_type=MarketType.SWAP,
    base="BTC",
    quote="USDT",
    settle="USDT",
    volume_unit="contracts",
    contract=ContractSpec(
        contract_size=0.001,
        linear=True,
        inverse=False,
    ),
)
```

`ContractSpec` can also carry `expiry_ms`, `strike`, and `option_type`. A
contract cannot be both linear and inverse, and numeric contract terms must be
finite and positive.

## Market listings and discovery

`MarketListing` wraps an instrument with venue capabilities:

- `active`: whether the provider reports the listing as tradable;
- `margin_supported`: whether margin is available.

Margin is a capability rather than a market type: a spot listing may support
margin while remaining `MarketType.SPOT`.

`MarketQuery` filters listings by asset class, one or more market types,
base/quote/settlement asset, active state, margin support, and linear/inverse
contract form. Text asset filters are case-insensitive.

## Bars

`Bar` contains one normalized OHLCV candle:

- `timestamp_ms` is a non-negative signed-64-bit Unix timestamp;
- OHLC prices are finite and positive;
- `high` must be at least every other OHLC price;
- `low` must be no greater than every other OHLC price;
- volume is finite and non-negative;
- `instrument` and `source` preserve provenance.

Providers should emit bars in strictly increasing timestamp order. A
`BarRequest` defines an inclusive `start_ms`, exclusive `end_ms`, source
timeframe, and optional positive limit.

## Live trades

`TradeTick` carries timestamp, local sequence, price, quantity, source, and
instrument. Sequence values are unsigned-64-bit compatible and allow the engine
stream boundary to reject duplicates or out-of-order handoffs.

`TradeSubscription.start_ms` selects the initial provider timestamp. Its
`start_sequence` is the last sequence already accepted downstream, so the next
emitted record uses `start_sequence + 1`.

## Macro observations

`MacroObservation` records three different times:

| Timestamp | Meaning |
|---|---|
| `period_end_ms` | end of the measured economic period |
| `released_at_ms` | first time the value became public |
| `vintage_at_ms` | time this particular revision became available |

The enforced ordering is `period_end_ms <= released_at_ms <= vintage_at_ms`.
Backtests must align on availability/vintage time rather than inserting today's
revised value into historical periods.

`MacroDataProvider` and `MacroRequest` define the public contract; the bootstrap
package does not yet ship a built-in macro provider.

## Low-level engine streaming

Most raw-Pine users should use the local release container or FastAPI server.
For callers that already own a compiled strategy library and state handle, the
package also exposes dependency-free `ctypes` interoperability:

- `pack_bars()` creates a contiguous `pf_bar_t` array;
- `pack_trade_ticks()` creates a contiguous `pf_trade_tick_t` array;
- `EngineStreamSink.begin()` warms and starts the engine stream;
- `push_tick()` and `push_ticks()` deliver normalized executions;
- `advance_time()` closes elapsed bars when no trade arrives;
- `end()` finishes the stream and can optionally finalize a partial input bar.

All packed records must belong to the expected instrument. The caller owns the
native library and strategy state; `EngineStreamSink` does not create or free
them. Non-zero engine statuses raise `EngineStreamError` with the strategy's
last error message.
