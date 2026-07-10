# Server components

Most users run the packaged `pineforge-backtest-server` command or submit work
through `FastApiBacktestClient`. These objects support applications that embed
the service or manage its compiled-strategy cache directly.

::: pineforge_data.server.create_app

::: pineforge_data.server.BacktestService

::: pineforge_data.server.BacktestServiceError

::: pineforge_data.compile_cache.CompileCache

The running service also publishes an interactive OpenAPI schema at `/docs`.
See the [server guide](../server.md) for HTTP behavior, concurrency, security,
and deployment configuration.
