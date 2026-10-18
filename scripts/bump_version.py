#!/usr/bin/env python3
"""Read or bump the PEP 621 project version using semantic-version rules."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
VERSION_LINE = re.compile(r'^(\s*version\s*=\s*")([^"]+)(".*)$')
BumpPart = Literal["patch", "minor", "major"]


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


def shim_project(pyproject: Path) -> Path | None:
    """The renamed distribution requires its sibling compatibility project."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    return (
        pyproject.parent / "shim/pyproject.toml"
        if project.get("name") == "tunaxis-data"
        else None
    )


def synchronized_version(pyproject: Path) -> str:
    """Refuse version or dependency drift before building, tagging or editing."""
    version = project_version(pyproject)
    shim = shim_project(pyproject)
    if shim is None:
        return version
    main = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    compatibility = tomllib.loads(shim.read_text(encoding="utf-8"))["project"]
    if compatibility.get("name") != "pineforge-data" or project_version(shim) != version:
        raise ValueError("main and shim project versions must match")
    if compatibility.get("dependencies") != [f"tunaxis-data=={version}"]:
        raise ValueError("shim must depend on exactly tunaxis-data==" + version)
    expected = {
        extra: [f"tunaxis-data[{extra}]=={version}"]
        for extra in main.get("optional-dependencies", {})
    }
    if compatibility.get("optional-dependencies", {}) != expected:
        raise ValueError("shim must forward every extra with the same exact version pin")
    return version


def version_edits(pyproject: Path, replacement: str) -> dict[Path, str]:
    """Prepare both complete edits before any write; also used by release PRs."""
    current = synchronized_version(pyproject)
    edits = {
        pyproject: replace_project_version(
            pyproject.read_text(encoding="utf-8"), current, replacement
        )
    }
    shim = shim_project(pyproject)
    if shim is not None:
        updated = replace_project_version(shim.read_text(encoding="utf-8"), current, replacement)
        updated = re.sub(
            r'("tunaxis-data(?:\[[^\]"\r\n]+\])?==)' + re.escape(current) + r'"',
            lambda match: f'{match[1]}{replacement}"',
            updated,
        )
        edits[shim] = updated
    return edits


def bumped_version(version: str, part: BumpPart) -> str:
    """Return the next semantic version for ``part``."""

    match = SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(
            f"project version {version!r} is not plain MAJOR.MINOR.PATCH semantic versioning"
        )
    major, minor, patch = (int(value) for value in match.groups())
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        major += 1
        minor = 0
        patch = 0
    return f"{major}.{minor}.{patch}"


def replace_project_version(text: str, current: str, replacement: str) -> str:
    """Replace exactly one version assignment inside the PEP 621 project table."""

    lines = text.splitlines(keepends=True)
    in_project = False
    replacements = 0
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        section = re.match(r"^\s*\[([^]]+)]\s*$", body)
        if section is not None:
            in_project = section.group(1).strip() == "project"
            continue
        if not in_project:
            continue
        match = VERSION_LINE.match(body)
        if match is None:
            continue
        if match.group(2) != current:
            raise ValueError(
                f"project.version changed while editing: expected {current!r}, "
                f"found {match.group(2)!r}"
            )
        lines[index] = f"{match.group(1)}{replacement}{match.group(3)}{ending}"
        replacements += 1
    if replacements != 1:
        raise ValueError(f"expected exactly one project.version assignment, found {replacements}")
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("part", choices=("current", "patch", "minor", "major"))
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument("--write", action="store_true", help="write the bumped version")
    args = parser.parse_args(argv)
    try:
        current = synchronized_version(args.pyproject)
        if args.part == "current":
            if args.write:
                raise ValueError("--write requires patch, minor, or major")
            result = current
        else:
            result = bumped_version(current, args.part)
            if args.write:
                for path, updated in version_edits(args.pyproject, result).items():
                    path.write_text(updated, encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
