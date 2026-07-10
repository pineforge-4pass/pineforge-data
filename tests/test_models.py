from __future__ import annotations

import pytest

from pineforge_data import (
    Bar,
    ContractSpec,
    Instrument,
    MacroObservation,
    MarketListing,
    MarketQuery,
    MarketType,
    OptionType,
    TradeTick,
)


def test_normalized_records_accept_valid_values() -> None:
    instrument = Instrument("BTCUSD", venue="example")

    bar = Bar(instrument, 1_000, 10.0, 12.0, 9.0, 11.0, 5.0, "example")
    tick = TradeTick(instrument, 1_001, 42, 11.5, 0.25, "example")
    observation = MacroObservation(
        "policy_rate", "USD", 1_000, 2_000, 2_500, 5.25, "percent", "example"
    )

    assert bar.instrument == tick.instrument
    assert tick.sequence == 42
    assert observation.released_at_ms == 2_000


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"high": 10.5}, "high"),
        ({"low": 11.5}, "low"),
        ({"open": 0.0}, "OHLC"),
        ({"volume": -1.0}, "volume"),
    ],
)
def test_bar_rejects_invalid_ohlcv(kwargs: dict[str, float], message: str) -> None:
    values = {
        "instrument": Instrument("BTCUSD"),
        "timestamp_ms": 1_000,
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "volume": 5.0,
        "source": "example",
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        Bar(**values)  # type: ignore[arg-type]


def test_macro_observation_rejects_lookahead_unsafe_timestamps() -> None:
    with pytest.raises(ValueError, match="released_at_ms"):
        MacroObservation("cpi", "USD", 2_000, 1_999, 3_000, 2.5, "percent", "example")

    with pytest.raises(ValueError, match="vintage_at_ms"):
        MacroObservation("cpi", "USD", 1_000, 2_000, 1_999, 2.5, "percent", "example")


def test_trade_tick_rejects_negative_sequence() -> None:
    with pytest.raises(ValueError, match="sequence"):
        TradeTick(Instrument("BTCUSD"), 1_000, -1, 10.0, 1.0, "example")


def test_trade_tick_rejects_values_outside_c_abi_ranges() -> None:
    with pytest.raises(ValueError, match="signed 64-bit"):
        TradeTick(Instrument("BTCUSD"), 2**63, 1, 10.0, 1.0, "example")

    with pytest.raises(ValueError, match="unsigned 64-bit"):
        TradeTick(Instrument("BTCUSD"), 1_000, 2**64, 10.0, 1.0, "example")


def test_contract_spec_and_market_query_are_provider_neutral() -> None:
    contract = ContractSpec(
        contract_size=0.01,
        linear=True,
        inverse=False,
        expiry_ms=1_800_000_000_000,
        strike=100_000,
        option_type=OptionType.CALL,
    )
    listing = MarketListing(
        Instrument(
            "BTC/USD:USD-OPTION",
            venue="broker",
            market_type=MarketType.OPTION,
            base="BTC",
            quote="USD",
            settle="USD",
            provider_id="native-123",
            contract=contract,
        ),
        active=True,
        margin_supported=False,
    )

    assert MarketQuery(
        market_types=frozenset({MarketType.OPTION}),
        settle="usd",
        linear=True,
    ).matches(listing)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"contract_size": 0}, "contract_size"),
        ({"linear": True, "inverse": True}, "both linear and inverse"),
        ({"expiry_ms": -1}, "expiry_ms"),
        ({"strike": -1}, "strike"),
    ],
)
def test_contract_spec_rejects_invalid_terms(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ContractSpec(**kwargs)  # type: ignore[arg-type]
