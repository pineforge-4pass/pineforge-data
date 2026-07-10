from __future__ import annotations

import subprocess

import pytest

from pineforge_data import (
    DEFAULT_RELEASE_IMAGE,
    DockerBacktestRuntime,
    DockerPrerequisiteError,
)


def test_default_runtime_is_a_digest_pinned_release_image() -> None:
    runtime = DockerBacktestRuntime()

    assert runtime.resolved_image() == DEFAULT_RELEASE_IMAGE
    assert "pineforge-release:0.1.12@sha256:" in runtime.resolved_image()


def test_latest_is_an_explicit_rolling_channel() -> None:
    runtime = DockerBacktestRuntime(
        image="ghcr.io/pineforge-4pass/pineforge-release:latest",
        pull_policy="always",
    )

    assert runtime.resolved_image().endswith(":latest")
    assert runtime.pull_policy == "always"


def test_missing_image_with_never_policy_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DockerBacktestRuntime(pull_policy="never")
    monkeypatch.setattr(DockerBacktestRuntime, "_check_docker", lambda _self: None)
    monkeypatch.setattr(DockerBacktestRuntime, "_image_exists", lambda _self: False)

    with pytest.raises(DockerPrerequisiteError, match="not available locally"):
        runtime.ensure_image()


def test_missing_policy_pulls_once(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = DockerBacktestRuntime(pull_policy="missing")
    pulled: list[bool] = []
    monkeypatch.setattr(DockerBacktestRuntime, "_check_docker", lambda _self: None)
    monkeypatch.setattr(DockerBacktestRuntime, "_image_exists", lambda _self: False)
    monkeypatch.setattr(DockerBacktestRuntime, "_pull_image", lambda _self: pulled.append(True))

    assert runtime.ensure_image() == DEFAULT_RELEASE_IMAGE
    assert pulled == [True]


def test_completed_error_prefers_stderr() -> None:
    from pineforge_data.docker_runtime import _completed_error

    completed = subprocess.CompletedProcess(["docker"], 1, "stdout", "stderr")

    assert _completed_error(completed) == "stderr"
