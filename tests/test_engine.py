from __future__ import annotations

import ctypes
from typing import cast

import pytest

from pineforge_data import (
    Bar,
    EngineStreamError,
    EngineStreamSink,
    Instrument,
    PfBar,
    PfTradeTick,
    TradeTick,
    pack_bars,
    pack_trade_ticks,
)


class FakeFunction:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        return self.result


class FakeLibrary:
    def __init__(self) -> None:
        self.strategy_get_last_error = FakeFunction(b"bad sequence")
        self.strategy_stream_begin = FakeFunction(0)
        self.strategy_stream_push_tick = FakeFunction(0)
        self.strategy_stream_push_ticks = FakeFunction(0)
        self.strategy_stream_advance_time = FakeFunction(0)
        self.strategy_stream_end = FakeFunction(0)


def instrument() -> Instrument:
    return Instrument("BTCUSD", venue="example")


def bar(timestamp_ms: int = 1_000) -> Bar:
    return Bar(instrument(), timestamp_ms, 10.0, 12.0, 9.0, 11.0, 5.0, "example")


def tick(timestamp_ms: int = 2_000, sequence: int = 1) -> TradeTick:
    return TradeTick(instrument(), timestamp_ms, sequence, 11.5, 0.25, "example")


def test_structures_match_engine_abi_layout() -> None:
    assert ctypes.sizeof(PfBar) == 48
    assert PfBar.timestamp.offset == 40
    assert ctypes.sizeof(PfTradeTick) == 32
    assert PfTradeTick.sequence.offset == 8


def test_pack_records_into_contiguous_arrays() -> None:
    bars = pack_bars([bar()])
    ticks = pack_trade_ticks([tick()])

    assert bars[0].timestamp == 1_000
    assert bars[0].close == 11.0
    assert ticks[0].sequence == 1
    assert ticks[0].quantity == 0.25


def test_pack_rejects_mixed_instruments() -> None:
    other = TradeTick(Instrument("ETHUSD"), 2_001, 2, 20.0, 1.0, "example")

    with pytest.raises(ValueError, match="expected instrument"):
        pack_trade_ticks([tick(), other])


def test_sink_invokes_complete_stream_lifecycle() -> None:
    fake = FakeLibrary()
    sink = EngineStreamSink(cast(ctypes.CDLL, fake), 123, instrument())

    sink.begin([bar()], input_timeframe="1", script_timeframe="5")
    sink.push_tick(tick())
    sink.push_ticks([tick(2_001, 2), tick(2_002, 3)])
    sink.push_ticks([])
    sink.advance_time(3_000)
    sink.end(finalize_partial_input_bar=True)

    assert len(fake.strategy_stream_begin.calls) == 1
    assert len(fake.strategy_stream_push_tick.calls) == 1
    assert len(fake.strategy_stream_push_ticks.calls) == 1
    assert len(fake.strategy_stream_advance_time.calls) == 1
    assert fake.strategy_stream_end.calls == [(123, 1)]


def test_sink_surfaces_engine_error() -> None:
    fake = FakeLibrary()
    fake.strategy_stream_push_ticks.result = -1
    sink = EngineStreamSink(cast(ctypes.CDLL, fake), 123, instrument())

    with pytest.raises(EngineStreamError, match="stream ticks: bad sequence"):
        sink.push_ticks([tick()])


def test_sink_rejects_null_state() -> None:
    with pytest.raises(ValueError, match="non-null"):
        EngineStreamSink(cast(ctypes.CDLL, FakeLibrary()), 0, instrument())
