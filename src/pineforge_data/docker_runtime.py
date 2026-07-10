"""Docker boundary for raw PineScript compilation and engine execution."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .backtest import BacktestOptions, JsonValue
from .models import Bar, Instrument


class DockerPrerequisiteError(RuntimeError):
    """Docker or an initialized source submodule is unavailable."""


class DockerExecutionError(RuntimeError):
    """The isolated transpile, compile, or backtest command failed."""


def discover_repository_root(start: Path | None = None) -> Path:
    """Find a checkout containing the Dockerfile and both pinned submodules."""

    configured = os.environ.get("PINEFORGE_DATA_ROOT")
    origin = Path(configured).expanduser() if configured else start
    if origin is None:
        origin = Path(__file__).resolve()
    candidates = [origin, *origin.parents]
    for candidate in candidates:
        if (candidate / "docker/Dockerfile").is_file() and (candidate / ".gitmodules").is_file():
            return candidate.resolve()
    raise DockerPrerequisiteError(
        "pineforge-data checkout not found; set PINEFORGE_DATA_ROOT to a cloned repository"
    )


def _completed_error(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stderr.strip() or completed.stdout.strip() or "command failed without output"


@dataclass(slots=True)
class DockerBacktestRuntime:
    """Build and execute the pinned raw-Pine backtest image."""

    repository_root: Path
    image: str | None = None
    rebuild: bool = False
    build_if_missing: bool = True

    def _submodule_commit(self, relative_path: str) -> str:
        path = self.repository_root / relative_path
        if not (path / ".git").exists() or not any(path.iterdir()):
            raise DockerPrerequisiteError(
                f"missing submodule {relative_path}; run `git submodule update --init`"
            )
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise DockerPrerequisiteError(_completed_error(completed))
        return completed.stdout.strip()

    def _pins(self) -> tuple[str, str]:
        engine = self._submodule_commit("vendor/pineforge-engine")
        codegen = self._submodule_commit("vendor/pineforge-codegen-oss")
        return engine, codegen

    def _source_digest(self) -> str:
        digest = hashlib.sha256()
        paths = [
            self.repository_root / "docker/Dockerfile",
            self.repository_root / "docker/entrypoint.py",
            *sorted((self.repository_root / "src/pineforge_data").rglob("*.py")),
        ]
        for path in paths:
            relative = path.relative_to(self.repository_root).as_posix()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def resolved_image(self) -> str:
        """Return the explicit image or a tag derived from both submodule pins."""

        if self.image:
            return self.image
        engine, codegen = self._pins()
        source = self._source_digest()
        return f"pineforge-data-backtest:d{source[:12]}-e{engine[:12]}-c{codegen[:12]}"

    def _check_docker(self) -> None:
        if shutil.which("docker") is None:
            raise DockerPrerequisiteError(
                "Docker is required but the `docker` command was not found"
            )
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise DockerPrerequisiteError(
                f"Docker daemon is unavailable: {_completed_error(completed)}"
            )

    def _image_exists(self, image: str) -> bool:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0

    def ensure_image(self) -> str:
        """Check Docker and build the pin-addressed image when necessary."""

        self._check_docker()
        engine, codegen = self._pins()
        source = self._source_digest()
        image = self.resolved_image()
        if not self.rebuild and self._image_exists(image):
            return image
        if not self.build_if_missing:
            raise DockerPrerequisiteError(f"Docker image is not available locally: {image}")
        command = [
            "docker",
            "build",
            "--file",
            str(self.repository_root / "docker/Dockerfile"),
            "--tag",
            image,
            "--build-arg",
            f"ENGINE_SHA={engine}",
            "--build-arg",
            f"CODEGEN_SHA={codegen}",
            "--build-arg",
            f"DATA_SOURCE_DIGEST={source}",
            str(self.repository_root),
        ]
        completed = subprocess.run(command, text=True, check=False)
        if completed.returncode != 0:
            raise DockerExecutionError(
                f"Docker image build failed with exit {completed.returncode}"
            )
        return image

    def run(
        self,
        pine_source: str,
        bars: Sequence[Bar],
        *,
        instrument: Instrument,
        source: str,
        options: BacktestOptions,
        strategy_params: Mapping[str, JsonValue] | None = None,
    ) -> dict[str, object]:
        """Run raw Pine and normalized OHLCV inside the isolated image."""

        if not pine_source.strip():
            raise ValueError("PineScript source must not be empty")
        if not bars:
            raise ValueError("bars must not be empty")
        image = self.ensure_image()
        with tempfile.TemporaryDirectory(prefix="pineforge-data-") as temporary:
            workspace = Path(temporary)
            (workspace / "strategy.pine").write_text(pine_source, encoding="utf-8")
            with (workspace / "ohlcv.csv").open("w", newline="", encoding="utf-8") as handle:
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
            request = {
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
                    "magnifier_distribution": int(options.magnifier_distribution),
                    "trace_enabled": options.trace_enabled,
                    "chart_timezone": options.chart_timezone,
                },
                "strategy_params": dict(strategy_params or {}),
            }
            (workspace / "request.json").write_text(
                json.dumps(request, separators=(",", ":"), allow_nan=False),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
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
                    f"type=bind,src={workspace},dst=/work,readonly",
                    image,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        if completed.returncode != 0:
            raise DockerExecutionError(_completed_error(completed))
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DockerExecutionError("container returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DockerExecutionError("container report must be a JSON object")
        return payload
