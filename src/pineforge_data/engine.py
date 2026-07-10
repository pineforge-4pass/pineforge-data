"""Dependency-free interoperability with PineForge's streaming C ABI."""

from __future__ import annotations

import ctypes
from collections.abc import Sequence

from .errors import EngineStreamError
from .models import Bar, Instrument, TradeTick


class PfBar(ctypes.Structure):
    """``ctypes`` mirror of PineForge's ``pf_bar_t``."""

    _fields_ = [
        ("open", ctypes.c_double),
        ("high", ctypes.c_double),
        ("low", ctypes.c_double),
        ("close", ctypes.c_double),
        ("volume", ctypes.c_double),
        ("timestamp", ctypes.c_int64),
    ]


class PfTradeTick(ctypes.Structure):
    """``ctypes`` mirror of PineForge's ``pf_trade_tick_t``."""

    _fields_ = [
        ("timestamp", ctypes.c_int64),
        ("sequence", ctypes.c_uint64),
        ("price", ctypes.c_double),
        ("quantity", ctypes.c_double),
    ]


def _require_instrument(
    records: Sequence[Bar] | Sequence[TradeTick], expected: Instrument | None
) -> None:
    if not records:
        return
    reference = expected or records[0].instrument
    if any(record.instrument != reference for record in records):
        raise ValueError("all records must belong to the expected instrument")


def pack_bars(bars: Sequence[Bar], *, instrument: Instrument | None = None) -> ctypes.Array[PfBar]:
    """Pack normalized bars into one contiguous ``pf_bar_t`` array."""

    _require_instrument(bars, instrument)
    array_type = PfBar * len(bars)
    return array_type(
        *(
            PfBar(bar.open, bar.high, bar.low, bar.close, bar.volume, bar.timestamp_ms)
            for bar in bars
        )
    )


def pack_trade_ticks(
    ticks: Sequence[TradeTick], *, instrument: Instrument | None = None
) -> ctypes.Array[PfTradeTick]:
    """Pack normalized trades into one contiguous ``pf_trade_tick_t`` array."""

    _require_instrument(ticks, instrument)
    array_type = PfTradeTick * len(ticks)
    return array_type(
        *(
            PfTradeTick(tick.timestamp_ms, tick.sequence, tick.price, tick.quantity)
            for tick in ticks
        )
    )


class EngineStreamSink:
    """Send normalized records to an existing PineForge strategy instance.

    The caller owns both ``library`` and ``state``. This adapter configures and
    invokes only the streaming functions; it never creates or frees a strategy.
    """

    def __init__(
        self,
        library: ctypes.CDLL,
        state: int | ctypes.c_void_p,
        instrument: Instrument,
    ) -> None:
        if not state:
            raise ValueError("state must be a non-null PineForge strategy handle")
        self._library = library
        self._state = state
        self.instrument = instrument
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._library.strategy_get_last_error.argtypes = [ctypes.c_void_p]
        self._library.strategy_get_last_error.restype = ctypes.c_char_p
        self._library.strategy_stream_begin.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PfBar),
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        self._library.strategy_stream_begin.restype = ctypes.c_int
        self._library.strategy_stream_push_tick.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PfTradeTick),
        ]
        self._library.strategy_stream_push_tick.restype = ctypes.c_int
        self._library.strategy_stream_push_ticks.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PfTradeTick),
            ctypes.c_int,
        ]
        self._library.strategy_stream_push_ticks.restype = ctypes.c_int
        self._library.strategy_stream_advance_time.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        self._library.strategy_stream_advance_time.restype = ctypes.c_int
        self._library.strategy_stream_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._library.strategy_stream_end.restype = ctypes.c_int

    def _check(self, status: int, operation: str) -> None:
        if status == 0:
            return
        raw = self._library.strategy_get_last_error(self._state)
        detail = raw.decode("utf-8", "replace") if raw else "unknown engine error"
        raise EngineStreamError(f"{operation}: {detail}")

    def begin(
        self,
        warmup_bars: Sequence[Bar],
        *,
        input_timeframe: str,
        script_timeframe: str,
    ) -> None:
        """Warm the strategy on confirmed bars and start realtime streaming."""

        if not warmup_bars:
            raise ValueError("warmup_bars must not be empty")
        if not input_timeframe or not script_timeframe:
            raise ValueError("input_timeframe and script_timeframe must not be empty")
        packed = pack_bars(warmup_bars, instrument=self.instrument)
        status = self._library.strategy_stream_begin(
            self._state,
            packed,
            len(packed),
            input_timeframe.encode(),
            script_timeframe.encode(),
        )
        self._check(status, "stream begin")

    def push_tick(self, tick: TradeTick) -> None:
        """Push one normalized executed trade."""

        if tick.instrument != self.instrument:
            raise ValueError("tick does not belong to the sink instrument")
        packed = PfTradeTick(tick.timestamp_ms, tick.sequence, tick.price, tick.quantity)
        status = self._library.strategy_stream_push_tick(self._state, ctypes.byref(packed))
        self._check(status, "stream tick")

    def push_ticks(self, ticks: Sequence[TradeTick]) -> None:
        """Push normalized executed trades in one contiguous ABI call."""

        if not ticks:
            return
        packed = pack_trade_ticks(ticks, instrument=self.instrument)
        status = self._library.strategy_stream_push_ticks(self._state, packed, len(packed))
        self._check(status, "stream ticks")

    def advance_time(self, timestamp_ms: int) -> None:
        """Advance the stream clock so elapsed input bars can close."""

        if timestamp_ms < 0:
            raise ValueError("timestamp_ms must be non-negative")
        status = self._library.strategy_stream_advance_time(self._state, timestamp_ms)
        self._check(status, "stream advance")

    def end(self, *, finalize_partial_input_bar: bool = False) -> None:
        """End the stream, optionally dispatching its partial input bar."""

        status = self._library.strategy_stream_end(self._state, int(finalize_partial_input_bar))
        self._check(status, "stream end")


if ctypes.sizeof(PfBar) != 48 or ctypes.sizeof(PfTradeTick) != 32:
    raise RuntimeError("unsupported platform ABI layout for PineForge streaming records")
