# Backtesting

## Runtime options and reports

::: tunaxis_data.backtest.BacktestOptions

::: tunaxis_data.backtest.MagnifierDistribution

::: tunaxis_data.backtest.BacktestReport

`TunaxisBacktestRunner` runs a compiled strategy library in-process. It
accepts libraries that report Tunaxis C ABI 4, which engine 1.x builds. It
refuses any other library before any strategy call, with an
`EngineBacktestError` that says what to do. Libraries without
`pf_abi_version` (engine v0.10.1 or earlier), ABI 2 (engine v0.10.2 through
v0.12.3) and ABI 3 (engine v0.13.x) need a rebuild with engine 1.x;
tunaxis-data 0.2.0 still reads ABI 2. A newer ABI needs a newer
tunaxis-data. The release container and the FastAPI server do not use this
class.

A trade's `max_runup` and `max_drawdown` are whole-trade excursions in account
currency, net of entry fees, as engine 1.0's `pineforge.h` documents them.
Headers before 1.0 described them as price travel per unit of quantity, but the
values did not change: on the README quick start, engines 0.11.0 and 1.0.0
report identical excursions for the 88 trades both report. A trade whose
`open_at_end` is true closes a position still open after the final bar, at that
bar's close rounded to the tick size, without slippage. Engine 0.11.0 reports no
such row, so the README quick start has 88 trades on it and 89 on engine 1.0.0.

::: tunaxis_data.backtest.TunaxisBacktestRunner

::: tunaxis_data.backtest.EngineBacktestError

## Published release container

::: tunaxis_data.docker_runtime.DockerBacktestRuntime

::: tunaxis_data.docker_runtime.DockerPrerequisiteError

::: tunaxis_data.docker_runtime.DockerExecutionError

::: tunaxis_data.release_contract.DEFAULT_RELEASE_IMAGE

::: tunaxis_data.release_contract.ReleaseContractError

## Remote FastAPI client

::: tunaxis_data.server_client.FastApiBacktestClient

::: tunaxis_data.server_client.BacktestServerError
