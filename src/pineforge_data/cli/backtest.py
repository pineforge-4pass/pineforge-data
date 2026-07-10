"""Fetch provider OHLCV and run a PineForge strategy without intermediate files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import cast

from ..backtest import BacktestOptions, JsonValue
from ..docker_runtime import DockerBacktestRuntime
from ..models import MarketListing
from ..providers import create_provider
from ..release_contract import DEFAULT_RELEASE_IMAGE
from ..requests import BarRequest
from ..server_client import FastApiBacktestClient


def parse_timestamp(value: str) -> int:
    """Parse Unix milliseconds or a timezone-aware ISO-8601 timestamp."""

    try:
        timestamp = int(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid timestamp: {value}") from exc
        if parsed.tzinfo is None:
            raise argparse.ArgumentTypeError("ISO timestamps must include a timezone") from None
        timestamp = int(parsed.timestamp() * 1_000)
    if timestamp < 0:
        raise argparse.ArgumentTypeError("timestamps must be non-negative")
    return timestamp


def source_timeframe_to_pine(timeframe: str) -> str:
    """Translate common compact timeframe spelling into Pine timeframe spelling."""

    match = re.fullmatch(r"([1-9][0-9]*)([smhdwM])", timeframe)
    if match is None:
        raise ValueError(f"cannot translate source timeframe to PineForge: {timeframe}")
    count = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return f"{count}S"
    if unit == "m":
        return str(count)
    if unit == "h":
        return str(count * 60)
    return f"{count}{unit.upper() if unit != 'M' else unit}"


# Backward-compatible import for callers of the CCXT-only bootstrap API.
ccxt_timeframe_to_pine = source_timeframe_to_pine


def _load_json_object(path: Path | None) -> dict[str, JsonValue]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"JSON file must contain an object: {path}")
    return cast(dict[str, JsonValue], value)


def _market_payload(listing: MarketListing) -> dict[str, JsonValue]:
    instrument = listing.instrument
    contract = instrument.contract
    contract_payload: JsonValue = None
    if contract is not None:
        contract_payload = {
            "contract_size": contract.contract_size,
            "linear": contract.linear,
            "inverse": contract.inverse,
            "expiry_ms": contract.expiry_ms,
            "strike": contract.strike,
            "option_type": contract.option_type.value if contract.option_type else None,
        }
    return {
        "symbol": instrument.symbol,
        "provider_id": instrument.provider_id,
        "asset_class": instrument.asset_class.value,
        "market_type": instrument.market_type.value,
        "base": instrument.base,
        "quote": instrument.quote,
        "settle": instrument.settle,
        "volume_unit": instrument.volume_unit,
        "active": listing.active,
        "margin_supported": listing.margin_supported,
        "contract": contract_payload,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pineforge-backtest",
        description="Transpile raw PineScript and backtest provider OHLCV in Docker",
    )
    parser.add_argument("--pine", type=Path, required=True, help="raw PineScript v6 strategy")
    parser.add_argument(
        "--provider", default="ccxt", help="provider adapter name; defaults to ccxt"
    )
    parser.add_argument(
        "--venue",
        "--exchange",
        dest="venue",
        required=True,
        help="exchange, broker, or provider environment; for example kraken",
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="exact provider-normalized symbol, such as BTC/USD or BTC/USDT:USDT",
    )
    parser.add_argument("--timeframe", required=True, help="source timeframe, such as 15m or 1h")
    parser.add_argument("--start", type=parse_timestamp, required=True)
    parser.add_argument("--end", type=parse_timestamp, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timezone", default="UTC", help="IANA chart and exchange timezone")
    parser.add_argument("--session", default="24x7", help="PineForge syminfo session")
    parser.add_argument(
        "--engine-timeframe",
        help="PineForge input timeframe; defaults to a translation of --timeframe",
    )
    parser.add_argument("--script-timeframe", default="")
    parser.add_argument("--provider-config", type=Path, help="provider options JSON file")
    parser.add_argument("--strategy-params", type=Path, help="strategy parameters JSON file")
    parser.add_argument(
        "--strategy-overrides", type=Path, help="strategy() header overrides JSON file"
    )
    parser.add_argument("--bar-magnifier", action="store_true")
    parser.add_argument("--magnifier-samples", type=int, default=4)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument(
        "--runtime-image",
        "--image",
        dest="runtime_image",
        default=DEFAULT_RELEASE_IMAGE,
        help="pineforge-release image; defaults to the package's pinned digest",
    )
    parser.add_argument(
        "--pull-policy",
        choices=("always", "missing", "never"),
        default="missing",
        help="local release-image pull policy",
    )
    parser.add_argument(
        "--execution-timeout",
        type=float,
        default=300.0,
        help="local or server request timeout in seconds",
    )
    parser.add_argument(
        "--server-url",
        help="FastAPI base URL; also read from PINEFORGE_SERVER_URL",
    )
    parser.add_argument(
        "--server-api-key-env",
        default="PINEFORGE_SERVER_API_KEY",
        help="environment variable containing the server bearer token",
    )
    parser.add_argument("--output", type=Path, help="report path; defaults to stdout")
    parser.add_argument("--pretty", action="store_true", help="pretty-print report JSON")
    return parser


async def run_harness(args: argparse.Namespace) -> dict[str, JsonValue]:
    """Execute the provider-to-engine pipeline for parsed CLI arguments."""

    provider_config = _load_json_object(args.provider_config)
    strategy_params = _load_json_object(args.strategy_params)
    strategy_overrides = _load_json_object(args.strategy_overrides)
    provider = create_provider(args.provider, args.venue, config=provider_config)
    try:
        listing = await provider.resolve_market(args.symbol)
        instrument = replace(
            listing.instrument,
            timezone=args.timezone,
            session=args.session,
        )
        request = BarRequest(
            instrument,
            args.timeframe,
            args.start,
            args.end,
            limit=args.limit,
        )
        bars = await provider.fetch_bars(request)
        provider_name = provider.name
    finally:
        await provider.close()
    if not bars:
        raise RuntimeError("provider returned no confirmed bars for the requested interval")

    engine_timeframe = args.engine_timeframe or source_timeframe_to_pine(args.timeframe)
    options = BacktestOptions(
        input_timeframe=engine_timeframe,
        script_timeframe=args.script_timeframe or engine_timeframe,
        bar_magnifier=args.bar_magnifier,
        magnifier_samples=args.magnifier_samples,
        trace_enabled=args.trace,
        chart_timezone=args.timezone,
    )
    pine_path = args.pine.expanduser().resolve()
    if not pine_path.is_file():
        raise FileNotFoundError(f"PineScript file not found: {pine_path}")
    pine_source = pine_path.read_text(encoding="utf-8")
    server_url = args.server_url or os.environ.get("PINEFORGE_SERVER_URL")
    if server_url:
        api_key = os.environ.get(args.server_api_key_env) if args.server_api_key_env else None
        client = FastApiBacktestClient(
            server_url,
            timeout_seconds=args.execution_timeout + 30,
            api_key=api_key,
        )
        container = await asyncio.to_thread(
            client.run,
            pine_source,
            bars,
            instrument=instrument,
            source=provider_name,
            options=options,
            strategy_params=strategy_params,
            strategy_overrides=strategy_overrides,
        )
    else:
        runtime = DockerBacktestRuntime(
            image=args.runtime_image,
            pull_policy=args.pull_policy,
            timeout_seconds=args.execution_timeout,
        )
        container = await asyncio.to_thread(
            runtime.run,
            pine_source,
            bars,
            instrument=instrument,
            source=provider_name,
            options=options,
            strategy_params=strategy_params,
            strategy_overrides=strategy_overrides,
        )
    required_sections = ("runtime", "backtest")
    if any(section not in container for section in required_sections):
        raise RuntimeError("Docker report is missing a required section")
    return {
        "schema_version": 1,
        "request_id": cast(JsonValue, container.get("request_id")),
        "provider": {
            "name": provider_name,
            "adapter": args.provider,
            "venue": args.venue,
            "source_timeframe": args.timeframe,
            "market": _market_payload(listing),
        },
        "data": {
            "requested_start_ms": args.start,
            "requested_end_ms": args.end,
            "first_bar_ms": bars[0].timestamp_ms,
            "last_bar_ms": bars[-1].timestamp_ms,
            "bars": len(bars),
        },
        "strategy": {
            "pine": str(pine_path),
            "input_timeframe": engine_timeframe,
            "script_timeframe": options.script_timeframe,
        },
        "runtime": cast(JsonValue, container["runtime"]),
        "backtest": cast(JsonValue, container["backtest"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = asyncio.run(run_harness(args))
        rendered = json.dumps(
            payload,
            indent=2 if args.pretty else None,
            sort_keys=args.pretty,
            allow_nan=False,
        )
        if args.output is None:
            print(rendered)
        else:
            args.output.write_text(f"{rendered}\n", encoding="utf-8")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
