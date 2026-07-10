#!/usr/bin/env python3
"""Require a GitHub release tag to match the package version exactly."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def project_version(pyproject: Path) -> str:
    """Read and validate ``project.version`` from a pyproject file."""

    with pyproject.open("rb") as handle:
        value: dict[str, Any] = tomllib.load(handle)
    project = value.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"missing [project] table in {pyproject}")
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"project.version must be a non-empty string in {pyproject}")
    return version


def verify_release_tag(tag: str, version: str) -> None:
    """Raise when ``tag`` is not exactly ``v`` followed by ``version``."""

    expected = f"v{version}"
    if tag != expected:
        raise ValueError(
            f"release tag {tag!r} does not match project version; expected {expected!r}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="GitHub release tag, for example v0.1.0")
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    args = parser.parse_args(argv)
    try:
        version = project_version(args.pyproject)
        verify_release_tag(args.tag, version)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"release tag {args.tag} matches project version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
