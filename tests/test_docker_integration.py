from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from urllib.request import urlopen

import pytest

from pineforge_data import (
    BacktestOptions,
    Bar,
    DockerBacktestRuntime,
    FastApiBacktestClient,
    Instrument,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PINEFORGE_DOCKER_TEST") != "1",
    reason="set PINEFORGE_DOCKER_TEST=1 to exercise pineforge-release",
)

ROOT = Path(__file__).resolve().parents[1]


def fixture_values() -> tuple[str, Instrument, list[Bar]]:
    pine = (ROOT / "tests/fixtures/sma_cross.pine").read_text(encoding="utf-8")
    instrument = Instrument("TEST/USD", venue="fixture")
    closes = [10, 11, 12, 13, 12, 11, 10, 9] * 3
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
    assert "pineforge-release:0.1.12@sha256:" in cast(str, runtime["release_image"])
    assert summary["bars_processed"] == len(bars)


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
