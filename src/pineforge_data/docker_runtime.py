"""Run backtests through the published pineforge-release container."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .backtest import BacktestOptions, JsonValue
from .models import Bar, Instrument
from .release_contract import (
    DEFAULT_RELEASE_IMAGE,
    ReleaseContractError,
    parse_release_report,
    release_environment,
    release_response,
    write_release_inputs,
)

PullPolicy = Literal["always", "missing", "never"]


class DockerPrerequisiteError(RuntimeError):
    """Docker or the configured release image is unavailable."""


class DockerExecutionError(RuntimeError):
    """The isolated release-container backtest failed."""


def _completed_error(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stderr.strip() or completed.stdout.strip() or "command failed without output"


@dataclass(slots=True)
class DockerBacktestRuntime:
    """Pull and execute an immutable pineforge-release image."""

    image: str = DEFAULT_RELEASE_IMAGE
    pull_policy: PullPolicy = "missing"
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.image.strip():
            raise ValueError("image must not be empty")
        if self.pull_policy not in ("always", "missing", "never"):
            raise ValueError(f"invalid pull policy: {self.pull_policy}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def resolved_image(self) -> str:
        return self.image

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

    def _image_exists(self) -> bool:
        completed = subprocess.run(
            ["docker", "image", "inspect", self.image],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0

    def _pull_image(self) -> None:
        completed = subprocess.run(
            ["docker", "pull", self.image],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise DockerPrerequisiteError(
                f"failed to pull pineforge-release image: {_completed_error(completed)}"
            )

    def _image_identity(self) -> dict[str, object]:
        completed = subprocess.run(
            ["docker", "image", "inspect", self.image],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            return {}
        try:
            values = json.loads(completed.stdout)
            value = values[0]
            labels = value.get("Config", {}).get("Labels", {})
            digests = value.get("RepoDigests", [])
        except (IndexError, AttributeError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(labels, dict) or not isinstance(digests, list):
            return {}
        return {
            "resolved_digest": str(digests[0]) if digests else None,
            "release_version": labels.get("org.opencontainers.image.version"),
            "engine_version": labels.get("io.pineforge.engine.version"),
            "codegen_version": labels.get("io.pineforge.codegen.version"),
        }

    def ensure_image(self) -> str:
        """Apply the configured pull policy and return the immutable image ref."""

        self._check_docker()
        exists = self._image_exists()
        if self.pull_policy == "always" or (self.pull_policy == "missing" and not exists):
            self._pull_image()
            exists = True
        if not exists:
            raise DockerPrerequisiteError(
                f"pineforge-release image is not available locally: {self.image}"
            )
        return self.image

    @staticmethod
    def _remove_timed_out_container(cid_path: Path) -> None:
        if not cid_path.is_file():
            return
        container_id = cid_path.read_text(encoding="utf-8").strip()
        if container_id:
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

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
        """Run PineScript and normalized OHLCV in pineforge-release."""

        image = self.ensure_image()
        with tempfile.TemporaryDirectory(prefix="pineforge-data-") as temporary:
            workspace = Path(temporary)
            write_release_inputs(workspace, pine_source, bars, instrument)
            environment = release_environment(
                "/in",
                instrument,
                options,
                strategy_params,
                strategy_overrides,
            )
            environment["PINEFORGE_DATA_SOURCE"] = source
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
                f"type=bind,src={workspace},dst=/in,readonly",
            ]
            for key, value in sorted(environment.items()):
                command.extend(("--env", f"{key}={value}"))
            command.append(image)
            try:
                completed = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout_seconds,
                    env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired as exc:
                self._remove_timed_out_container(cid_path)
                raise DockerExecutionError(
                    f"pineforge-release exceeded {self.timeout_seconds:g} seconds"
                ) from exc
        if completed.returncode != 0:
            phase = {2: "input", 3: "compile", 4: "backtest", 5: "transpile"}.get(
                completed.returncode, "container"
            )
            raise DockerExecutionError(f"{phase} failed: {_completed_error(completed)}")
        try:
            report = parse_release_report(completed.stdout)
        except ReleaseContractError as exc:
            raise DockerExecutionError(str(exc)) from exc
        return release_response(
            report,
            release_image=image,
            mode="local-container",
            request_id=None,
            runtime_metadata=self._image_identity(),
        )
