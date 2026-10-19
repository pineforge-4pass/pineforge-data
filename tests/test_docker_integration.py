from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from urllib.request import urlopen

import pytest

from pineforge_data import (
    DEFAULT_RELEASE_IMAGE,
    BacktestOptions,
    Bar,
    DockerBacktestRuntime,
    FastApiBacktestClient,
    Instrument,
)
from pineforge_data.backtest import EXPECTED_PF_ABI
from pineforge_data.release_contract import write_release_inputs

pytestmark = pytest.mark.skipif(
    os.environ.get("PINEFORGE_DOCKER_TEST") != "1",
    reason="set PINEFORGE_DOCKER_TEST=1 to exercise pineforge-release",
)

ROOT = Path(__file__).resolve().parents[1]


def assert_release_identity(runtime: dict[str, Any], *, server: bool = False) -> None:
    """Compare observed runtime provenance, not just the configured image name."""
    assert runtime["release_image"] == DEFAULT_RELEASE_IMAGE
    version = DEFAULT_RELEASE_IMAGE.split(":")[-2].split("@", 1)[0]
    assert runtime["release_version"] == version
    assert runtime["engine_version"] == runtime["codegen_version"] == version
    lock_path = ROOT / "src/pineforge_data/release_lock.json"
    if lock_path.exists():
        event = json.loads(lock_path.read_text())["event"]
        assert runtime["hub_commit"] == event["hub_commit"]
        if not server:
            assert runtime["resolved_digest"] in {
                f"{event['image']['repository']}@{event['image'][key]}"
                for key in ("digest", "amd64_digest", "arm64_digest")
            }


def fixture_values(
    closes: Sequence[int] = (10, 11, 12, 13, 12, 11, 10, 9) * 3,
) -> tuple[str, Instrument, list[Bar]]:
    pine = (ROOT / "tests/fixtures/sma_cross.pine").read_text(encoding="utf-8")
    instrument = Instrument("TEST/USD", venue="fixture")
    bars = [
        Bar(
            instrument,
            1_700_000_000_000 + index * 60_000,
            float(close),
            float(close + 1),
            float(close - 1),
            float(close),
            100.0,
            "fixture",
        )
        for index, close in enumerate(closes)
    ]
    return pine, instrument, bars


def test_local_runtime_uses_published_release_image() -> None:
    pine, instrument, bars = fixture_values()

    result = DockerBacktestRuntime().run(
        pine,
        bars,
        instrument=instrument,
        source="fixture",
        options=BacktestOptions(input_timeframe="1", script_timeframe="1"),
    )

    runtime = cast(dict[str, object], result["runtime"])
    backtest = cast(dict[str, object], result["backtest"])
    summary = cast(dict[str, object], backtest["summary"])
    assert runtime["mode"] == "local-container"
    assert_release_identity(runtime)
    assert summary["bars_processed"] == len(bars)


@pytest.fixture(scope="module")
def in_process_check(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Compile the SMA fixture in the release image and read it both ways."""

    # A rising tail opens a long that is still open after the final bar, so the
    # report ends with a range-end (open_at_end) row.
    pine, instrument, bars = fixture_values((10, 11, 12, 13, 12, 11, 10, 9) * 3 + (10, 11, 12, 13))
    workspace = tmp_path_factory.mktemp("in-process")
    write_release_inputs(workspace / "in", pine, bars, instrument)
    image = DockerBacktestRuntime().ensure_image()
    cid_path = workspace / "container.cid"
    command = [
        "docker",
        "run",
        "--rm",
        "--cidfile",
        str(cid_path),
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=512m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--mount",
        f"type=bind,src={workspace / 'in'},dst=/in,readonly",
        "--mount",
        f"type=bind,src={ROOT / 'src'},dst=/opt/pineforge-data/src,readonly",
        "--mount",
        f"type=bind,src={ROOT / 'tests'},dst=/opt/pineforge-data/tests,readonly",
        "--env",
        "PYTHONPATH=/opt/pineforge-data/src:/opt/pineforge/pycodegen",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint",
        "python3",
        image,
        "/opt/pineforge-data/tests/in_process_release_check.py",
        "--input-tf",
        "1",
        "--script-tf",
        "1",
    ]
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=600
        )
    except subprocess.TimeoutExpired:
        DockerBacktestRuntime._remove_timed_out_container(cid_path)
        raise
    assert completed.returncode == 0, f"in-process check failed:\n{completed.stderr}"
    return cast(dict[str, Any], json.loads(completed.stdout))


def test_report_structures_match_the_release_header(in_process_check: dict[str, Any]) -> None:
    layout = in_process_check["layout"]

    for name, c_structure in layout["c"].items():
        assert layout["ctypes"][name] == c_structure, name
    assert in_process_check["header_abi"] == in_process_check["library_abi"] == EXPECTED_PF_ABI


def test_in_process_runner_matches_the_release_harness(in_process_check: dict[str, Any]) -> None:
    assert in_process_check["in_process_error"] is None
    release = in_process_check["release"]
    in_process = in_process_check["in_process"]
    expected_trades = [
        {
            "entry_time": trade["entry_time"],
            "exit_time": trade["exit_time"],
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "pnl": trade["pnl"],
            "pnl_pct": trade["pnl_pct"],
            "is_long": trade["side"] == "long",
            "max_runup": trade["max_runup"],
            "max_drawdown": trade["max_drawdown"],
            "qty": trade["qty"],
            "commission": trade["commission"],
            "entry_bar_index": trade["entry_bar_index"],
            "exit_bar_index": trade["exit_bar_index"],
            "open_at_end": trade["open_at_end"],
        }
        for trade in release["trades"]
    ]

    assert len(expected_trades) >= 2
    assert in_process["trades"] == expected_trades
    assert [trade["open_at_end"] for trade in in_process["trades"]][-2:] == [False, True]
    assert in_process["metrics"] == release["metrics"]
    assert in_process["equity_curve"] == release["equity_curve"]
    summary = in_process["summary"]
    assert summary["total_trades"] == release["summary"]["total_trades"] == len(expected_trades)
    assert summary["net_profit"] == release["summary"]["net_pnl"]
    for key in (
        "input_bars_processed",
        "script_bars_processed",
        "magnifier_sub_bars_total",
        "magnifier_sample_ticks_total",
        "bar_magnifier_enabled",
    ):
        assert summary[key] == release["diagnostics"][key], key
    for key in ("input_tf_seconds", "script_tf_seconds", "script_tf_ratio", "needs_aggregation"):
        assert summary[key] == release["applied_runtime"][key], key


def _wait_for_server(container_id: str) -> str:
    port_result = subprocess.run(
        ["docker", "port", container_id, "8000/tcp"],
        text=True,
        capture_output=True,
        check=True,
    )
    port = port_result.stdout.strip().rsplit(":", 1)[1]
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/readyz", timeout=1) as response:
                if response.status == 200:
                    return url
        except OSError:
            time.sleep(0.25)
    logs = subprocess.run(
        ["docker", "logs", container_id],
        text=True,
        capture_output=True,
        check=False,
    )
    raise AssertionError(f"server did not become ready:\n{logs.stdout}\n{logs.stderr}")


def test_server_handles_concurrent_requests_and_reuses_compile_cache() -> None:
    image = "pineforge-data-backtest-server:integration"
    build = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            str(ROOT / "docker/server.Dockerfile"),
            "--tag",
            image,
            str(ROOT),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert build.returncode == 0, f"server image build failed:\n{build.stdout}\n{build.stderr}"
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    labels = inspected["Config"]["Labels"]
    lock_path = ROOT / "src/pineforge_data/release_lock.json"
    if lock_path.exists():
        event = json.loads(lock_path.read_text())["event"]
        assert labels["org.opencontainers.image.revision"] == event["hub_commit"]
        # The server image inherits the hub image's labels, and the hub labels its image
        # with the release TAG (v-prefixed), exactly as release_chain/contract.py verifies.
        assert labels["org.opencontainers.image.version"] == f"v{event['release_version']}"
    container = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--publish",
            "127.0.0.1::8000",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=512m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            image,
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    try:
        url = _wait_for_server(container)
        pine, instrument, bars = fixture_values()
        client = FastApiBacktestClient(url)

        def submit() -> dict[str, object]:
            return client.run(
                pine,
                bars,
                instrument=instrument,
                source="fixture",
                options=BacktestOptions(input_timeframe="1", script_timeframe="1"),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.map(lambda _index: submit(), range(2))

        for result in (first, second):
            assert_release_identity(cast(dict[str, Any], result["runtime"]), server=True)
            assert cast(dict[str, Any], result["backtest"])["summary"]["bars_processed"] == len(
                bars
            )

        cache_results = [
            cast(dict[str, object], cast(dict[str, object], result["runtime"])["compile_cache"])
            for result in (first, second)
        ]
        assert sorted(cast(bool, result["hit"]) for result in cache_results) == [False, True]
        third = submit()
        third_cache = cast(
            dict[str, object], cast(dict[str, object], third["runtime"])["compile_cache"]
        )
        assert third_cache["hit"] is True
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
