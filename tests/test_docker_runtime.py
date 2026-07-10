from __future__ import annotations

from pathlib import Path

import pytest

from pineforge_data import DockerBacktestRuntime, DockerPrerequisiteError


def test_image_tag_is_derived_from_both_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        DockerBacktestRuntime,
        "_pins",
        lambda _self: ("a" * 40, "b" * 40),
    )
    monkeypatch.setattr(DockerBacktestRuntime, "_source_digest", lambda _self: "c" * 64)
    runtime = DockerBacktestRuntime(Path("/tmp/repository"))

    assert runtime.resolved_image() == (
        f"pineforge-data-backtest:d{'c' * 12}-e{'a' * 12}-c{'b' * 12}"
    )


def test_explicit_image_does_not_replace_pin_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        DockerBacktestRuntime,
        "_pins",
        lambda _self: ("a" * 40, "b" * 40),
    )
    monkeypatch.setattr(DockerBacktestRuntime, "_source_digest", lambda _self: "c" * 64)
    runtime = DockerBacktestRuntime(Path("/tmp/repository"), image="example/image:test")

    assert runtime.resolved_image() == "example/image:test"


def test_missing_submodule_has_actionable_error(tmp_path: Path) -> None:
    runtime = DockerBacktestRuntime(tmp_path)

    with pytest.raises(DockerPrerequisiteError, match="git submodule update --init"):
        runtime._submodule_commit("vendor/pineforge-engine")
