"""Compile a strategy inside pineforge-release and read it in-process.

This script runs inside the release image with the repository's ``src`` on
``PYTHONPATH``; ``tests/test_docker_integration.py`` starts it. It transpiles
``/in/strategy.pine`` with the image's entrypoint, compiles the library the way
the entrypoint does, and prints one JSON document on stdout:

- ``layout``: every structure ``tunaxis_data`` mirrors, as the C compiler lays
  it out from the image's ``pineforge.h`` (``c``) and as ``ctypes`` does: each
  member's name, offset, size and scalar kind;
- ``header_abi`` and ``library_abi``: ``PF_ABI_VERSION`` and the compiled
  library's ``pf_abi_version()``;
- ``release``: the image's own report (``run_json.py``) for that library;
- ``in_process``: ``TunaxisBacktestRunner``'s report for the same library,
  bars and options, or ``in_process_error`` when the runner refuses it.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tunaxis_data import (
    BacktestOptions,
    Bar,
    EngineBacktestError,
    Instrument,
    TunaxisBacktestRunner,
)
from tunaxis_data.backtest import (
    _PfEquityPoint,
    _PfEquityStats,
    _PfMetrics,
    _PfReport,
    _PfSecurityDiagnostic,
    _PfTraceEntry,
    _PfTrade,
    _PfTradeStats,
)
from tunaxis_data.engine import PfBar, PfTradeTick

PREFIX = Path(os.environ.get("PINEFORGE_PREFIX", "/opt/pineforge"))
IN_DIR = Path("/in")
MIRRORS: tuple[tuple[str, type[ctypes.Structure]], ...] = (
    ("pf_bar_t", PfBar),
    ("pf_trade_tick_t", PfTradeTick),
    ("pf_trade_t", _PfTrade),
    ("pf_trade_stats_t", _PfTradeStats),
    ("pf_equity_stats_t", _PfEquityStats),
    ("pf_metrics_t", _PfMetrics),
    ("pf_equity_point_t", _PfEquityPoint),
    ("pf_security_diag_t", _PfSecurityDiagnostic),
    ("pf_trace_entry_t", _PfTraceEntry),
    ("pf_report_t", _PfReport),
)
# pineforge.h 1.0 spells these two doubles sharpe_monthly / sortino_monthly; the
# report keys, and so the ctypes names, stay sharpe_tv / sortino_tv.
C_MEMBERS = {
    ("pf_equity_stats_t", "sharpe_tv"): "sharpe_monthly",
    ("pf_equity_stats_t", "sortino_tv"): "sortino_monthly",
}
# Scalar kinds, so that a same-size change of type (signedness, integer versus
# double, pointer versus integer) fails too. Pointers and nested structures
# are "other" on both sides; their sizes tell them apart.
CTYPES_KINDS: dict[object, str] = {
    ctypes.c_double: "f64",
    ctypes.c_float: "f32",
    ctypes.c_int8: "i8",
    ctypes.c_uint8: "u8",
    ctypes.c_int16: "i16",
    ctypes.c_uint16: "u16",
    ctypes.c_int32: "i32",
    ctypes.c_uint32: "u32",
    ctypes.c_int64: "i64",
    ctypes.c_uint64: "u64",
}
C_KIND = (
    '_Generic((x), double: "f64", float: "f32", signed char: "i8", unsigned char: "u8", '
    'short: "i16", unsigned short: "u16", int: "i32", unsigned int: "u32", '
    'long: "i64", unsigned long: "u64", long long: "i64", unsigned long long: "u64", '
    'default: "other")'
)

Layout = dict[str, dict[str, object]]


def _members(c_name: str, structure: type[ctypes.Structure]) -> list[tuple[str, str, object]]:
    return [
        (name, C_MEMBERS.get((c_name, name), name), field_type)
        for name, field_type, *_ in structure._fields_
    ]


def ctypes_layout() -> Layout:
    layout: Layout = {}
    for c_name, structure in MIRRORS:
        fields = []
        for name, member, field_type in _members(c_name, structure):
            descriptor = getattr(structure, name)
            kind = CTYPES_KINDS.get(field_type, "other")
            fields.append([member, descriptor.offset, descriptor.size, kind])
        layout[c_name] = {"size": ctypes.sizeof(structure), "fields": fields}
    return layout


def c_layout(work: Path) -> tuple[int, Layout]:
    lines = [
        "#include <stddef.h>",
        "#include <stdio.h>",
        "#include <pineforge/pineforge.h>",
        f"#define KIND(x) {C_KIND}",
        "int main(void) {",
        '    printf("abi %d\\n", PF_ABI_VERSION);',
    ]
    for c_name, structure in MIRRORS:
        lines.append(f'    printf("size {c_name} %zu\\n", sizeof({c_name}));')
        for _, member, _ in _members(c_name, structure):
            lines.append(
                f'    printf("field {c_name} {member} %zu %zu %s\\n", '
                f"offsetof({c_name}, {member}), sizeof((({c_name} *)0)->{member}), "
                f"KIND((({c_name} *)0)->{member}));"
            )
    lines += ["    return 0;", "}"]
    source = work / "layout.c"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    binary = work / "layout"
    subprocess.run(
        ["gcc", "-std=c11", f"-I{PREFIX / 'include'}", str(source), "-o", str(binary)],
        check=True,
    )
    output = subprocess.run([str(binary)], check=True, stdout=subprocess.PIPE, text=True).stdout
    header_abi = 0
    layout: Layout = {}
    for line in output.splitlines():
        kind, *values = line.split()
        if kind == "abi":
            header_abi = int(values[0])
        elif kind == "size":
            layout[values[0]] = {"size": int(values[1]), "fields": []}
        else:
            c_name, member, offset, size, kind = values
            fields = layout[c_name]["fields"]
            assert isinstance(fields, list)
            fields.append([member, int(offset), int(size), kind])
    return header_abi, layout


def build_library(work: Path) -> tuple[Path, Path]:
    environment = dict(os.environ, PINEFORGE_IN_DIR=str(IN_DIR), PINEFORGE_TRANSPILE_ONLY="1")
    generated = subprocess.run(
        [str(PREFIX / "bin/entrypoint.sh")],
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    source = work / "strategy.cpp"
    source.write_bytes(generated)
    library = work / "strategy.so"
    # The compile line of the image's entrypoint.sh.
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-O2",
            "-ffp-contract=off",
            "-fPIC",
            "-shared",
            f"-I{PREFIX / 'include'}",
            "-I/usr/include/eigen3",
            str(source),
            "-Wl,--whole-archive",
            str(PREFIX / "lib/libpineforge.a"),
            "-Wl,--no-whole-archive",
            "-o",
            str(library),
        ],
        check=True,
    )
    return source, library


def read_inputs() -> tuple[Instrument, list[Bar]]:
    syminfo = json.loads((IN_DIR / "syminfo.json").read_text(encoding="utf-8"))["syminfo"]
    instrument = Instrument(
        syminfo["ticker"], timezone=syminfo["timezone"], session=syminfo["session"]
    )
    with (IN_DIR / "ohlcv.csv").open(newline="", encoding="utf-8") as handle:
        bars = [
            Bar(
                instrument,
                int(row["timestamp"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
                "fixture",
            )
            for row in csv.DictReader(handle)
        ]
    return instrument, bars


def release_report(
    source: Path, library: Path, instrument: Instrument, options: BacktestOptions
) -> object:
    command = [
        "python3",
        str(PREFIX / "bin/run_json.py"),
        "--so",
        str(library),
        "--ohlcv",
        str(IN_DIR / "ohlcv.csv"),
        "--input-tf",
        options.input_timeframe,
        "--script-tf",
        options.script_timeframe,
        "--generated-cpp",
        str(source),
        "--transpiled",
        "true",
        "--syminfo",
        str(IN_DIR / "syminfo.json"),
        # TunaxisBacktestRunner sets the chart timezone to the instrument's.
        "--chart-tz",
        instrument.timezone,
    ]
    if options.trade_start_time_ms is not None:
        command += ["--trade-start-ms", str(options.trade_start_time_ms)]
    completed = subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True)
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-tf", required=True)
    parser.add_argument("--script-tf", required=True)
    parser.add_argument("--trade-start-ms", type=int)
    args = parser.parse_args()

    instrument, bars = read_inputs()
    options = BacktestOptions(
        input_timeframe=args.input_tf,
        script_timeframe=args.script_tf,
        trade_start_time_ms=args.trade_start_ms,
    )
    with tempfile.TemporaryDirectory(prefix="pineforge-data-in-process-") as temporary:
        work = Path(temporary)
        header_abi, c_structures = c_layout(work)
        source, library = build_library(work)
        library_abi = int(ctypes.CDLL(str(library)).pf_abi_version())
        release = release_report(source, library, instrument, options)
        in_process: object = None
        in_process_error = None
        try:
            runner = TunaxisBacktestRunner.load(library)
            in_process = runner.run(bars, instrument=instrument, options=options).to_dict()
        except EngineBacktestError as error:
            in_process_error = str(error)

    json.dump(
        {
            "header_abi": header_abi,
            "library_abi": library_abi,
            "layout": {"c": c_structures, "ctypes": ctypes_layout()},
            "release": release,
            "in_process": in_process,
            "in_process_error": in_process_error,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
