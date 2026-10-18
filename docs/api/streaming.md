# Streaming and C ABI

The dependency-free `ctypes` bridge packs normalized records into the arrays
accepted by Tunaxis strategy libraries. Providers remain responsible for
ordering and provenance before the handoff.

::: tunaxis_data.engine.PfBar

::: tunaxis_data.engine.PfTradeTick

::: tunaxis_data.engine.pack_bars

::: tunaxis_data.engine.pack_trade_ticks

::: tunaxis_data.engine.EngineStreamSink

::: tunaxis_data.errors.EngineStreamError
