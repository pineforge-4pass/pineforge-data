"""Shared input and output contract for the published PineForge release image."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path

from .backtest import BacktestOptions, JsonValue
from .models import Bar, Instrument

DEFAULT_RELEASE_IMAGE = (
    "ghcr.io/pineforge-4pass/pineforge-release:0.1.12@"
    "sha256:312b9d908390b828484617472c749d5815feb75507da87eae2f6902cfe3d47b1"
)
RELEASE_ENTRYPOINT = "/opt/pineforge/bin/entrypoint.sh"
RESPONSE_SCHEMA_VERSION = 1


class ReleaseContractError(ValueError):
    """A request or release-image response violates the integration contract."""


def _validate_request(pine_source: str, bars: Sequence[Bar]) -> None:
    if not pine_source.strip():
        raise ReleaseContractError("PineScript source must not be empty")
    if not bars:
        raise ReleaseContractError("bars must not be empty")
    if any(left.timestamp_ms >= right.timestamp_ms for left, right in pairwise(bars)):
        raise ReleaseContractError("bar timestamps must be strictly increasing")


def write_release_inputs(
    workspace: Path,
    pine_source: str,
    bars: Sequence[Bar],
    instrument: Instrument,
) -> None:
    """Write the immutable `/in` files consumed by pineforge-release."""

    _validate_request(pine_source, bars)
    workspace.mkdir(parents=True, exist_ok=True)
    pine_path = workspace / "strategy.pine"
    ohlcv_path = workspace / "ohlcv.csv"
    syminfo_path = workspace / "syminfo.json"
    pine_path.write_text(pine_source, encoding="utf-8")
    with ohlcv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "open", "high", "low", "close", "volume"))
        for bar in bars:
            writer.writerow(
                (
                    bar.timestamp_ms,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                )
            )
    syminfo_path.write_text(
        json.dumps(
            {
                "syminfo": {
                    "ticker": instrument.symbol,
                    "timezone": instrument.timezone,
                    "session": instrument.session,
                }
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    for path in (pine_path, ohlcv_path, syminfo_path):
        path.chmod(0o644)
    workspace.chmod(0o755)


def release_environment(
    input_directory: str,
    instrument: Instrument,
    options: BacktestOptions,
    strategy_params: Mapping[str, JsonValue] | None = None,
    strategy_overrides: Mapping[str, JsonValue] | None = None,
) -> dict[str, str]:
    """Translate PineForge options into the release entrypoint environment."""

    if options.trace_enabled:
        raise ReleaseContractError("pineforge-release 0.1.12 does not expose trace collection")
    if options.bar_magnifier and options.magnifier_samples < 2:
        raise ReleaseContractError("bar magnifier requires at least two samples")
    environment = {
        "PINEFORGE_IN_DIR": input_directory,
        "PINEFORGE_INPUTS": json.dumps(
            dict(strategy_params or {}), separators=(",", ":"), allow_nan=False
        ),
        "PINEFORGE_OVERRIDES": json.dumps(
            dict(strategy_overrides or {}), separators=(",", ":"), allow_nan=False
        ),
        "PINEFORGE_INPUT_TF": options.input_timeframe,
        "PINEFORGE_SCRIPT_TF": options.script_timeframe,
        "PINEFORGE_BAR_MAGNIFIER": "true" if options.bar_magnifier else "false",
        "PINEFORGE_MAGNIFIER_SAMPLES": str(options.magnifier_samples),
        "PINEFORGE_MAGNIFIER_DIST": options.magnifier_distribution.name.lower(),
        "PINEFORGE_CHART_TZ": options.chart_timezone or "",
        "PINEFORGE_SYMINFO": f"{input_directory.rstrip('/')}/syminfo.json",
        "PINEFORGE_DATA_SYMBOL": instrument.symbol,
        "PINEFORGE_DATA_VENUE": instrument.venue,
    }
    if options.trade_start_time_ms is not None:
        environment["PINEFORGE_TRADE_START_MS"] = str(options.trade_start_time_ms)
    return environment


def parse_release_report(stdout: str) -> dict[str, object]:
    """Parse and minimally validate the canonical release-image report."""

    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseContractError("pineforge-release returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseContractError("pineforge-release report must be a JSON object")
    required = ("summary", "trades", "metrics", "diagnostics")
    missing = [field for field in required if field not in value]
    if missing:
        raise ReleaseContractError(f"pineforge-release report is missing: {', '.join(missing)}")
    return value


def release_response(
    report: Mapping[str, object],
    *,
    release_image: str,
    mode: str,
    request_id: str | None,
    runtime_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Wrap the release report in the stable pineforge-data transport envelope."""

    runtime: dict[str, object] = {
        "mode": mode,
        "release_image": release_image,
    }
    runtime.update(runtime_metadata or {})
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "request_id": request_id,
        "runtime": runtime,
        "backtest": dict(report),
    }
