from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY_RELEASE = ROOT / "scripts" / "verify_release.py"


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
