from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY_RELEASE = ROOT / "scripts" / "verify_release.py"
BUMP_VERSION = ROOT / "scripts" / "bump_version.py"


def test_release_script_accepts_matching_pep_621_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "example"\nversion = "1.2.3"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFY_RELEASE), "v1.2.3", "--pyproject", str(pyproject)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "matches project version 1.2.3" in result.stdout


def test_release_script_rejects_mismatched_tag(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "example"\nversion = "1.2.3"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFY_RELEASE), "v1.2.4", "--pyproject", str(pyproject)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "expected 'v1.2.3'" in result.stderr


def test_version_script_reads_and_bumps_each_semver_part(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "example"\nversion = "1.2.3"\n', encoding="utf-8")

    cases = (
        ("current", "1.2.3"),
        ("patch", "1.2.4"),
        ("minor", "1.3.0"),
        ("major", "2.0.0"),
    )
    for part, expected in cases:
        result = subprocess.run(
            [sys.executable, str(BUMP_VERSION), part, "--pyproject", str(pyproject)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == expected


def test_version_script_writes_only_project_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nversion = "1.2.3"\n\n[tool.example]\nversion = "keep-me"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(BUMP_VERSION),
            "minor",
            "--write",
            "--pyproject",
            str(pyproject),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "1.3.0"
    assert pyproject.read_text(encoding="utf-8") == (
        '[project]\nversion = "1.3.0"\n\n[tool.example]\nversion = "keep-me"\n'
    )


def test_version_script_rejects_non_plain_semver(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.2.3rc1"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(BUMP_VERSION), "patch", "--pyproject", str(pyproject)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "not plain MAJOR.MINOR.PATCH" in result.stderr
