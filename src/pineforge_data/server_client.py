"""Standard-library client for the PineForge FastAPI backtest service."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .backtest import BacktestOptions, JsonValue
from .models import Bar, Instrument


class BacktestServerError(RuntimeError):
    """The configured FastAPI server rejected or failed a backtest."""


def _scalar_inputs(values: Mapping[str, JsonValue] | None, field: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in (values or {}).items():
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"{field}.{key} must be a scalar value")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class FastApiBacktestClient:
    """Submit normalized bars to a bounded remote PineForge runtime."""

    base_url: str
    timeout_seconds: float = 330.0
    api_key: str | None = None
    max_response_bytes: int = 128 * 1_024 * 1_024

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("server URL must be an absolute http:// or https:// URL")
        if self.timeout_seconds <= 0 or self.max_response_bytes <= 0:
            raise ValueError("client timeout and response limit must be positive")

    def run(
        self,
        pine_source: str,
        bars: Sequence[Bar],
        *,
        instrument: Instrument,
        source: str,
        options: BacktestOptions,
        strategy_params: Mapping[str, JsonValue] | None = None,
        strategy_overrides: Mapping[str, JsonValue] | None = None,
    ) -> dict[str, object]:
        payload = {
            "pine_source": pine_source,
            "bars": [
                {
                    "timestamp_ms": bar.timestamp_ms,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in bars
            ],
            "instrument": {
                "symbol": instrument.symbol,
                "venue": instrument.venue,
                "timezone": instrument.timezone,
                "session": instrument.session,
                "volume_unit": instrument.volume_unit,
            },
            "source": source,
            "options": {
                "input_timeframe": options.input_timeframe,
                "script_timeframe": options.script_timeframe,
                "bar_magnifier": options.bar_magnifier,
                "magnifier_samples": options.magnifier_samples,
                "magnifier_distribution": options.magnifier_distribution.name.lower(),
                "trace_enabled": options.trace_enabled,
                "chart_timezone": options.chart_timezone,
                "trade_start_time_ms": options.trade_start_time_ms,
            },
            "strategy_params": _scalar_inputs(strategy_params, "strategy_params"),
            "strategy_overrides": _scalar_inputs(strategy_overrides, "strategy_overrides"),
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Request-ID": uuid4().hex,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url.rstrip('/')}/v1/backtests",
            data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            body = exc.read(self.max_response_bytes + 1)
            detail = _error_message(body) or f"HTTP {exc.code}"
            raise BacktestServerError(detail) from exc
        except URLError as exc:
            raise BacktestServerError(f"backtest server is unavailable: {exc.reason}") from exc
        if len(body) > self.max_response_bytes:
            raise BacktestServerError("backtest server response exceeded the configured limit")
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BacktestServerError("backtest server returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise BacktestServerError("backtest server response must be a JSON object")
        if value.get("schema_version") != 1:
            raise BacktestServerError("unsupported backtest server response schema")
        if not isinstance(value.get("runtime"), dict) or not isinstance(
            value.get("backtest"), dict
        ):
            raise BacktestServerError("backtest server response is missing runtime data")
        return value


def _error_message(body: bytes) -> str:
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", "replace").strip()
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
        return str(value)
    error = value["error"]
    phase = error.get("phase", "server")
    message = error.get("message", "request failed")
    request_id = error.get("request_id")
    suffix = f" (request {request_id})" if request_id else ""
    return f"{phase}: {message}{suffix}"
