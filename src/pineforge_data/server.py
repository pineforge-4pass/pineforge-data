"""Bounded-concurrency FastAPI service backed by pineforge-release."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TypeAlias
from uuid import uuid4

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .backtest import BacktestOptions
from .compile_cache import CompileCache
from .models import Bar, Instrument
from .release_contract import (
    DEFAULT_RELEASE_IMAGE,
    RELEASE_ENTRYPOINT,
    ReleaseContractError,
    parse_release_report,
    release_environment,
    release_response,
    write_release_inputs,
)

InputValue: TypeAlias = str | int | float | bool
MagnifierName = Literal[
    "uniform",
    "cosine",
    "triangle",
    "endpoints",
    "front_loaded",
    "back_loaded",
]
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ApiInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=256)
    venue: str = Field(default="", max_length=128)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    session: str = Field(default="24x7", min_length=1, max_length=256)
    volume_unit: str = Field(default="base", min_length=1, max_length=64)


class ApiBar(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    timestamp_ms: int = Field(ge=0, le=2**63 - 1)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> ApiBar:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be greater than or equal to OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be less than or equal to OHLC values")
        return self


class ApiBacktestOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_timeframe: str = Field(default="", max_length=32)
    script_timeframe: str = Field(default="", max_length=32)
    bar_magnifier: bool = False
    magnifier_samples: int = Field(default=4, ge=1, le=10_000)
    magnifier_distribution: MagnifierName = "endpoints"
    trace_enabled: bool = False
    chart_timezone: str | None = Field(default=None, max_length=128)
    trade_start_time_ms: int | None = Field(default=None, ge=0, le=2**63 - 1)


class BacktestApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    pine_source: str = Field(min_length=1, max_length=2_000_000)
    bars: list[ApiBar] = Field(min_length=1, max_length=1_000_000)
    instrument: ApiInstrument
    source: str = Field(default="provider", min_length=1, max_length=256)
    options: ApiBacktestOptions = Field(default_factory=ApiBacktestOptions)
    strategy_params: dict[str, InputValue] = Field(default_factory=dict)
    strategy_overrides: dict[str, InputValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timestamps(self) -> BacktestApiRequest:
        if any(
            left.timestamp_ms >= right.timestamp_ms
            for left, right in zip(self.bars, self.bars[1:], strict=False)
        ):
            raise ValueError("bar timestamps must be strictly increasing")
        return self

    def domain_values(
        self,
    ) -> tuple[str, list[Bar], Instrument, BacktestOptions]:
        instrument = Instrument(
            self.instrument.symbol,
            venue=self.instrument.venue,
            timezone=self.instrument.timezone,
            session=self.instrument.session,
            volume_unit=self.instrument.volume_unit,
        )
        bars = [
            Bar(
                instrument,
                value.timestamp_ms,
                value.open,
                value.high,
                value.low,
                value.close,
                value.volume,
                self.source,
            )
            for value in self.bars
        ]
        from .backtest import MagnifierDistribution

        options = BacktestOptions(
            input_timeframe=self.options.input_timeframe,
            script_timeframe=self.options.script_timeframe,
            bar_magnifier=self.options.bar_magnifier,
            magnifier_samples=self.options.magnifier_samples,
            magnifier_distribution=MagnifierDistribution[
                self.options.magnifier_distribution.upper()
            ],
            trace_enabled=self.options.trace_enabled,
            chart_timezone=self.options.chart_timezone,
            trade_start_time_ms=self.options.trade_start_time_ms,
        )
        return self.pine_source, bars, instrument, options


class BacktestServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        phase: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.phase = phase


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    report: Mapping[str, object]
    runtime_metadata: Mapping[str, object]


Executor = Callable[[BacktestApiRequest], Awaitable[ExecutionResult]]


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _non_negative_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


class BacktestService:
    """Admit a bounded number of jobs and execute them in isolated directories."""

    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        max_queue: int | None = None,
        queue_timeout_seconds: float | None = None,
        execution_timeout_seconds: float | None = None,
        release_image: str | None = None,
        entrypoint: Path | None = None,
        run_json_path: Path | None = None,
        cache: CompileCache | None = None,
        executor: Executor | None = None,
    ) -> None:
        cpu_default = max(1, min(4, os.cpu_count() or 1))
        self.max_concurrency = (
            max_concurrency
            if max_concurrency is not None
            else _positive_env("PINEFORGE_SERVER_CONCURRENCY", cpu_default)
        )
        self.max_queue = (
            max_queue
            if max_queue is not None
            else _non_negative_env("PINEFORGE_SERVER_MAX_QUEUE", self.max_concurrency * 2)
        )
        self.queue_timeout_seconds = (
            queue_timeout_seconds
            if queue_timeout_seconds is not None
            else float(os.environ.get("PINEFORGE_SERVER_QUEUE_TIMEOUT", "30"))
        )
        self.execution_timeout_seconds = (
            execution_timeout_seconds
            if execution_timeout_seconds is not None
            else float(os.environ.get("PINEFORGE_SERVER_EXECUTION_TIMEOUT", "300"))
        )
        if self.max_concurrency <= 0 or self.max_queue < 0:
            raise ValueError("concurrency must be positive and max_queue non-negative")
        if self.queue_timeout_seconds <= 0 or self.execution_timeout_seconds <= 0:
            raise ValueError("queue and execution timeouts must be positive")
        self.release_image = release_image or os.environ.get(
            "PINEFORGE_RELEASE_IMAGE", DEFAULT_RELEASE_IMAGE
        )
        self.entrypoint = entrypoint or Path(
            os.environ.get("PINEFORGE_RELEASE_ENTRYPOINT", RELEASE_ENTRYPOINT)
        )
        self.run_json_path = run_json_path or Path(
            os.environ.get("PINEFORGE_RELEASE_RUN_JSON", "/opt/pineforge/bin/run_json.py")
        )
        self.cache = cache or CompileCache(
            Path(os.environ.get("PINEFORGE_SERVER_CACHE_DIR", "/tmp/pineforge-compile-cache")),
            max_entries=_positive_env("PINEFORGE_SERVER_CACHE_MAX_ENTRIES", 1_024),
            max_bytes=_positive_env("PINEFORGE_SERVER_CACHE_MAX_BYTES", 2 * 1_024 * 1_024 * 1_024),
        )
        self._executor = executor or self._execute_release
        self._requires_entrypoint = executor is None
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._state_lock = asyncio.Lock()
        self._admitted = 0
        self._running = 0

    def ready(self) -> bool:
        return not self._requires_entrypoint or (
            self.entrypoint.is_file()
            and os.access(self.entrypoint, os.X_OK)
            and self.run_json_path.is_file()
            and shutil.which("g++") is not None
        )

    async def status(self) -> dict[str, object]:
        cache_status = await self.cache.status()
        async with self._state_lock:
            return {
                "ready": self.ready(),
                "running": self._running,
                "queued": self._admitted - self._running,
                "max_concurrency": self.max_concurrency,
                "max_queue": self.max_queue,
                "release_image": self.release_image,
                "compile_cache": cache_status,
            }

    async def _admit(self) -> None:
        async with self._state_lock:
            capacity = self.max_concurrency + self.max_queue
            if self._admitted >= capacity:
                raise BacktestServiceError(
                    "server_overloaded",
                    "backtest capacity is full; retry later",
                    status_code=429,
                    phase="queue",
                )
            self._admitted += 1

    async def _release_admission(self) -> None:
        async with self._state_lock:
            self._admitted -= 1

    async def run(self, request: BacktestApiRequest, request_id: str) -> dict[str, object]:
        await self._admit()
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self.queue_timeout_seconds
                )
                acquired = True
            except TimeoutError as exc:
                raise BacktestServiceError(
                    "queue_timeout",
                    "backtest did not reach an execution slot before the queue timeout",
                    status_code=503,
                    phase="queue",
                ) from exc
            async with self._state_lock:
                self._running += 1
            try:
                result = await self._executor(request)
            finally:
                async with self._state_lock:
                    self._running -= 1
            return release_response(
                result.report,
                release_image=self.release_image,
                mode="fastapi-server",
                request_id=request_id,
                runtime_metadata=result.runtime_metadata,
            )
        finally:
            if acquired:
                self._semaphore.release()
            await self._release_admission()

    async def _run_command(
        self,
        command: list[str],
        *,
        environment: Mapping[str, str],
        phase: str,
        deadline: float,
    ) -> bytes:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise BacktestServiceError(
                "execution_timeout",
                f"backtest exceeded {self.execution_timeout_seconds:g} seconds",
                status_code=504,
                phase=phase,
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise BacktestServiceError(
                "runtime_unavailable",
                f"cannot start {phase} process: {exc}",
                status_code=503,
                phase=phase,
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=remaining)
        except TimeoutError as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            await process.communicate()
            raise BacktestServiceError(
                "execution_timeout",
                f"backtest exceeded {self.execution_timeout_seconds:g} seconds",
                status_code=504,
                phase=phase,
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip() or "pineforge-release failed"
            raise BacktestServiceError(
                f"{phase}_failed",
                detail,
                status_code=422 if phase in ("input", "transpile", "compile", "backtest") else 500,
                phase=phase,
            )
        return stdout

    def _cache_key(self, generated_cpp: bytes) -> tuple[str, str]:
        cpp_sha256 = hashlib.sha256(generated_cpp).hexdigest()
        identity = "\0".join(
            (
                "pineforge-compiled-strategy-v1",
                cpp_sha256,
                self.release_image,
                os.environ.get("PINEFORGE_ENGINE_VERSION", "unknown"),
                os.environ.get("PINEFORGE_CODEGEN_VERSION", "unknown"),
                os.environ.get("PINEFORGE_RELEASE_VERSION", "unknown"),
                platform.machine(),
                "g++-std=c++17-O2-ffp-contract=off-fPIC-shared-whole-archive",
            )
        )
        return hashlib.sha256(identity.encode()).hexdigest(), cpp_sha256

    async def _execute_release(self, request: BacktestApiRequest) -> ExecutionResult:
        pine_source, bars, instrument, options = request.domain_values()
        deadline = asyncio.get_running_loop().time() + self.execution_timeout_seconds
        with tempfile.TemporaryDirectory(prefix="pineforge-server-") as temporary:
            workspace = Path(temporary)
            try:
                await asyncio.to_thread(
                    write_release_inputs,
                    workspace,
                    pine_source,
                    bars,
                    instrument,
                )
                release_environment(
                    str(workspace),
                    instrument,
                    options,
                    request.strategy_params,
                    request.strategy_overrides,
                )
            except (ReleaseContractError, ValueError) as exc:
                raise BacktestServiceError(
                    "invalid_request",
                    str(exc),
                    status_code=400,
                    phase="input",
                ) from exc
            environment = os.environ.copy()
            environment.update(
                {
                    "PINEFORGE_IN_DIR": str(workspace),
                    "PINEFORGE_TRANSPILE_ONLY": "1",
                }
            )
            generated_cpp = await self._run_command(
                [str(self.entrypoint)],
                environment=environment,
                phase="transpile",
                deadline=deadline,
            )
            generated_path = workspace / "strategy.cpp"
            await asyncio.to_thread(generated_path.write_bytes, generated_cpp)
            cache_key, cpp_sha256 = self._cache_key(generated_cpp)
            inputs = json.dumps(request.strategy_params, separators=(",", ":"), allow_nan=False)
            overrides = json.dumps(
                request.strategy_overrides, separators=(",", ":"), allow_nan=False
            )
            cache_hit = False
            async with self.cache.compile_lock(cache_key):
                strategy_library = await self.cache.acquire(cache_key)
                if strategy_library is None:
                    temporary_library = self.cache.temporary_path(cache_key)
                    prefix = os.environ.get("PINEFORGE_PREFIX", "/opt/pineforge")
                    compile_command = [
                        "g++",
                        "-std=c++17",
                        "-O2",
                        "-ffp-contract=off",
                        "-fPIC",
                        "-shared",
                        f"-I{prefix}/include",
                        "-I/usr/include/eigen3",
                        str(generated_path),
                        "-Wl,--whole-archive",
                        f"{prefix}/lib/libpineforge.a",
                        "-Wl,--no-whole-archive",
                        "-o",
                        str(temporary_library),
                    ]
                    try:
                        await self._run_command(
                            compile_command,
                            environment=os.environ.copy(),
                            phase="compile",
                            deadline=deadline,
                        )
                        strategy_library = await self.cache.commit_and_acquire(
                            cache_key, temporary_library
                        )
                    finally:
                        if temporary_library.exists():
                            temporary_library.unlink()
                else:
                    cache_hit = True
            if strategy_library is None:
                raise BacktestServiceError(
                    "compile_failed",
                    "compiled strategy cache artifact was not created",
                    status_code=500,
                    phase="compile",
                )
            command = [
                "python3",
                str(self.run_json_path),
                "--so",
                str(strategy_library),
                "--ohlcv",
                str(workspace / "ohlcv.csv"),
                "--inputs",
                inputs,
                "--overrides",
                overrides,
                "--input-tf",
                options.input_timeframe,
                "--script-tf",
                options.script_timeframe,
                "--bar-magnifier",
                "true" if options.bar_magnifier else "false",
                "--magnifier-samples",
                str(options.magnifier_samples),
                "--magnifier-dist",
                options.magnifier_distribution.name.lower(),
                "--generated-cpp",
                str(generated_path),
                "--transpiled",
                "true",
                "--syminfo",
                str(workspace / "syminfo.json"),
                "--chart-tz",
                options.chart_timezone or "",
            ]
            if options.trade_start_time_ms is not None:
                command.extend(("--trade-start-ms", str(options.trade_start_time_ms)))
            try:
                stdout = await self._run_command(
                    command,
                    environment=os.environ.copy(),
                    phase="backtest",
                    deadline=deadline,
                )
            finally:
                await self.cache.release(cache_key)
            await self.cache.trim()
            try:
                report = parse_release_report(stdout.decode("utf-8", "replace"))
            except ReleaseContractError as exc:
                raise BacktestServiceError(
                    "invalid_release_response",
                    str(exc),
                    status_code=502,
                    phase="response",
                ) from exc
            return ExecutionResult(
                report,
                {
                    "release_version": os.environ.get("PINEFORGE_RELEASE_VERSION", "unknown"),
                    "engine_version": os.environ.get("PINEFORGE_ENGINE_VERSION", "unknown"),
                    "codegen_version": os.environ.get("PINEFORGE_CODEGEN_VERSION", "unknown"),
                    "compile_cache": {
                        "key": cache_key,
                        "hit": cache_hit,
                        "generated_cpp_sha256": cpp_sha256,
                    },
                },
            )


def _request_id(value: str | None) -> str:
    if value is None:
        return uuid4().hex
    if not _REQUEST_ID.fullmatch(value):
        raise BacktestServiceError(
            "invalid_request_id",
            "X-Request-ID must contain 1-128 safe identifier characters",
            status_code=400,
            phase="request",
        )
    return value


def create_app(
    service: BacktestService | None = None,
    *,
    api_key: str | None = None,
) -> FastAPI:
    runtime = service or BacktestService()
    expected_api_key = (
        api_key if api_key is not None else os.environ.get("PINEFORGE_SERVER_API_KEY", "")
    )
    application = FastAPI(
        title="PineForge Backtest Server",
        version="1",
        docs_url="/docs",
        redoc_url=None,
    )

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz")
    async def readyz() -> JSONResponse:
        status = await runtime.status()
        return JSONResponse(status_code=200 if status["ready"] else 503, content=status)

    @application.post("/v1/backtests")
    async def backtest(
        request: BacktestApiRequest,
        authorization: Annotated[str | None, Header()] = None,
        x_request_id: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        request_id = ""
        try:
            request_id = _request_id(x_request_id)
            if expected_api_key:
                scheme, _, credential = (authorization or "").partition(" ")
                supplied = credential if scheme.casefold() == "bearer" else ""
                if not secrets.compare_digest(supplied, expected_api_key):
                    raise BacktestServiceError(
                        "unauthorized",
                        "a valid bearer token is required",
                        status_code=401,
                        phase="authorization",
                    )
            result = await runtime.run(request, request_id)
            return JSONResponse(status_code=200, content=result)
        except BacktestServiceError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "phase": exc.phase,
                        "message": str(exc),
                        "request_id": request_id or None,
                    }
                },
            )

    return application


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "pineforge_data.server:app",
        host=os.environ.get("PINEFORGE_SERVER_HOST", "0.0.0.0"),
        port=int(os.environ.get("PINEFORGE_SERVER_PORT", "8000")),
        workers=1,
    )
