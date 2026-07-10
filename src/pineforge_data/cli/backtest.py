"""Fetch provider OHLCV and run a PineForge strategy without intermediate files."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

from ..backtest import BacktestOptions, JsonValue, PineForgeBacktestRunner
from ..models import Instrument
from ..providers import CcxtProvider
from ..requests import BarRequest


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


def ccxt_timeframe_to_pine(timeframe: str) -> str:
    """Translate common CCXT timeframe spelling into Pine timeframe spelling."""

    match = re.fullmatch(r"([1-9][0-9]*)([smhdwM])", timeframe)
    if match is None:
        raise ValueError(f"cannot translate CCXT timeframe to PineForge: {timeframe}")
    count = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return f"{count}S"
    if unit == "m":
        return str(count)
    if unit == "h":
        return str(count * 60)
    return f"{count}{unit.upper() if unit != 'M' else unit}"


def _load_json_object(path: Path | None) -> dict[str, JsonValue]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"JSON file must contain an object: {path}")
    return cast(dict[str, JsonValue], value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pineforge-backtest",
        description="Feed provider OHLCV directly into a compiled PineForge strategy",
    )
    parser.add_argument("--strategy", type=Path, required=True, help="compiled strategy library")
    parser.add_argument("--provider", choices=("ccxt",), default="ccxt")
    parser.add_argument("--exchange", required=True, help="CCXT exchange id, such as kraken")
    parser.add_argument("--symbol", required=True, help="CCXT unified symbol, such as BTC/USD")
    parser.add_argument("--timeframe", required=True, help="CCXT timeframe, such as 15m or 1h")
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
    parser.add_argument("--provider-config", type=Path, help="CCXT constructor options JSON file")
    parser.add_argument("--strategy-params", type=Path, help="strategy parameters JSON file")
    parser.add_argument("--bar-magnifier", action="store_true")
    parser.add_argument("--magnifier-samples", type=int, default=4)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--output", type=Path, help="report path; defaults to stdout")
    parser.add_argument("--pretty", action="store_true", help="pretty-print report JSON")
    return parser


async def run_harness(args: argparse.Namespace) -> dict[str, JsonValue]:
    """Execute the provider-to-engine pipeline for parsed CLI arguments."""

    provider_config = _load_json_object(args.provider_config)
    strategy_params = _load_json_object(args.strategy_params)
    instrument = Instrument(
        args.symbol,
        venue=args.exchange,
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
    async with CcxtProvider(args.exchange, config=provider_config) as provider:
        bars = await provider.fetch_bars(request)
        provider_name = provider.name
    if not bars:
        raise RuntimeError("provider returned no confirmed bars for the requested interval")

    engine_timeframe = args.engine_timeframe or ccxt_timeframe_to_pine(args.timeframe)
    options = BacktestOptions(
        input_timeframe=engine_timeframe,
        script_timeframe=args.script_timeframe or engine_timeframe,
        bar_magnifier=args.bar_magnifier,
        magnifier_samples=args.magnifier_samples,
        trace_enabled=args.trace,
        chart_timezone=args.timezone,
    )
    runner = PineForgeBacktestRunner.load(args.strategy)
    report = runner.run(
        bars,
        instrument=instrument,
        options=options,
        strategy_params=strategy_params,
    )
    return {
        "provider": {
            "name": provider_name,
            "exchange": args.exchange,
            "symbol": args.symbol,
            "source_timeframe": args.timeframe,
        },
        "data": {
            "requested_start_ms": args.start,
            "requested_end_ms": args.end,
            "first_bar_ms": bars[0].timestamp_ms,
            "last_bar_ms": bars[-1].timestamp_ms,
            "bars": len(bars),
        },
        "strategy": {
            "library": str(args.strategy.expanduser().resolve()),
            "input_timeframe": engine_timeframe,
            "script_timeframe": options.script_timeframe,
        },
        "backtest": report.to_dict(),
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
