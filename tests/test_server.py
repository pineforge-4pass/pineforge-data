from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from pineforge_data import CompileCache
from pineforge_data.server import (
    BacktestApiRequest,
    BacktestService,
    BacktestServiceError,
    ExecutionResult,
    create_app,
)


def request_payload() -> dict[str, object]:
    return {
        "pine_source": "//@version=6\nstrategy('test')",
        "bars": [
            {
                "timestamp_ms": 1_000,
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 5,
            },
            {
                "timestamp_ms": 61_000,
                "open": 11,
                "high": 13,
                "low": 10,
                "close": 12,
                "volume": 6,
            },
        ],
        "instrument": {"symbol": "BTC/USD", "venue": "kraken"},
        "source": "ccxt:kraken",
        "options": {"input_timeframe": "1", "script_timeframe": "1"},
        "strategy_params": {"Length": 14},
    }


def canonical_report() -> dict[str, object]:
    return {
        "summary": {"total_trades": 1},
        "trades": [],
        "metrics": {},
        "diagnostics": {"input_bars_processed": 2},
    }


def test_api_authenticates_and_preserves_request_id(tmp_path) -> None:
    async def execute(_request: BacktestApiRequest) -> ExecutionResult:
        return ExecutionResult(canonical_report(), {"compile_cache": {"hit": True}})

    service = BacktestService(
        executor=execute,
        cache=CompileCache(tmp_path, max_entries=10, max_bytes=10_000),
    )
    client = TestClient(create_app(service, api_key="secret"))

    unauthorized = client.post("/v1/backtests", json=request_payload())
    response = client.post(
        "/v1/backtests",
        json=request_payload(),
        headers={"Authorization": "Bearer secret", "X-Request-ID": "job-123"},
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["request_id"] == "job-123"
    assert response.json()["runtime"]["compile_cache"]["hit"] is True


def test_service_enforces_global_concurrency(tmp_path) -> None:
    async def run() -> None:
        active = 0
        maximum = 0

        async def execute(_request: BacktestApiRequest) -> ExecutionResult:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ExecutionResult(canonical_report(), {})

        service = BacktestService(
            max_concurrency=2,
            max_queue=4,
            executor=execute,
            cache=CompileCache(tmp_path, max_entries=10, max_bytes=10_000),
        )
        request = BacktestApiRequest.model_validate(request_payload())
        results = await asyncio.gather(
            *(service.run(request, f"job-{index}") for index in range(6))
        )

        assert maximum == 2
        assert len(results) == 6

    asyncio.run(run())


def test_service_rejects_requests_beyond_queue_capacity(tmp_path) -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def execute(_request: BacktestApiRequest) -> ExecutionResult:
            started.set()
            await release.wait()
            return ExecutionResult(canonical_report(), {})

        service = BacktestService(
            max_concurrency=1,
            max_queue=0,
            executor=execute,
            cache=CompileCache(tmp_path, max_entries=10, max_bytes=10_000),
        )
        request = BacktestApiRequest.model_validate(request_payload())
        first = asyncio.create_task(service.run(request, "first"))
        await started.wait()
        with pytest.raises(BacktestServiceError, match="capacity is full"):
            await service.run(request, "second")
        release.set()
        await first

    asyncio.run(run())


def test_compile_cache_key_uses_generated_cpp_and_runtime_identity(tmp_path) -> None:
    async def execute(_request: BacktestApiRequest) -> ExecutionResult:
        return ExecutionResult(canonical_report(), {})

    service = BacktestService(
        release_image="release@sha256:one",
        executor=execute,
        cache=CompileCache(tmp_path, max_entries=10, max_bytes=10_000),
    )

    first, first_cpp = service._cache_key(b"generated C++ A")
    same, same_cpp = service._cache_key(b"generated C++ A")
    different, different_cpp = service._cache_key(b"generated C++ B")

    assert first == same
    assert first_cpp == same_cpp
    assert different != first
    assert different_cpp != first_cpp


def test_request_rejects_unsorted_bars() -> None:
    payload = request_payload()
    bars = payload["bars"]
    assert isinstance(bars, list)
    bars.reverse()

    with pytest.raises(ValueError, match="strictly increasing"):
        BacktestApiRequest.model_validate(payload)
