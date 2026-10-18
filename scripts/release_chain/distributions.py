"""Validate the paired projects' real wheel/sdist metadata and separate ownership."""

from __future__ import annotations

import io
import re
import tarfile
import zipfile
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

from .contract import require

PROJECTS = ("tunaxis-data", "pineforge-data")


def filenames(project: str, version: str) -> tuple[str, str]:
    stem = f"{project.replace('-', '_')}-{version}"
    return f"{stem}-py3-none-any.whl", f"{stem}.tar.gz"


def shim_requirements(requirements: list[str], version: str, extras: set[str]) -> None:
    """Only an exact base pin plus one exact forwarding pin per extra is allowed."""
    base = f"tunaxis-data=={version}"
    require(requirements.count(base) == 1, "shim Requires-Dist must be exactly " + base)
    forwarded = []
    pattern = re.compile(
        r"tunaxis-data\[([^]]+)\]==" + re.escape(version)
        + r";\s*extra\s*==\s*(['\"])([^'\"]+)\2"
    )
    for requirement in requirements:
        if requirement == base:
            continue
        match = pattern.fullmatch(requirement)
        require(match is not None and match[1] == match[3], "invalid shim extra forwarding pin")
        assert match is not None
        forwarded.append(match[1])
    require(
        len(forwarded) == len(extras) and set(forwarded) == extras,
        "shim must forward every extra exactly once",
    )


def archive_metadata(name: str, data: bytes) -> tuple[Message, set[str]]:
    if name.endswith(".whl"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {member for member in archive.namelist() if not member.endswith("/")}
            metadata = [member for member in files if member.endswith(".dist-info/METADATA")]
            require(len(metadata) == 1, "wheel needs unique METADATA")
            return BytesParser().parsebytes(archive.read(metadata[0])), files
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        metadata = [
            member for member in archive.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        require(len(metadata) == 1, "sdist needs unique PKG-INFO")
        handle = archive.extractfile(metadata[0])
        require(handle is not None, "sdist metadata is not a file")
        assert handle is not None
        with handle:
            members = {
                member.name
                for member in archive.getmembers()
                if member.isfile()
            }
            return BytesParser().parsebytes(handle.read()), members


def verify_archives(files: dict[str, bytes], version: str, *, legacy: bool = False) -> None:
    projects = ("pineforge-data",) if legacy else PROJECTS
    require(
        set(files) == {name for project in projects for name in filenames(project, version)},
        "expected one wheel and one sdist for each distribution",
    )
    metadata = {}
    ownership = {}
    source_members = {}
    for project in projects:
        for name in filenames(project, version):
            info, members = archive_metadata(name, files[name])
            require(
                info["Name"] == project and info["Version"] == version,
                "archive identity drift",
            )
            metadata[name] = info
            if name.endswith(".whl"):
                ownership[project] = members
            else:
                source_members[project] = members
    if legacy:
        return
    extras = set(metadata[filenames("tunaxis-data", version)[0]].get_all("Provides-Extra", []))
    for project in projects:
        for name in filenames(project, version):
            info = metadata[name]
            require(set(info.get_all("Provides-Extra", [])) == extras, "archive extras disagree")
            if project == "pineforge-data":
                shim_requirements(info.get_all("Requires-Dist", []), version, extras)
    require(
        not ownership["tunaxis-data"] & ownership["pineforge-data"],
        "main and shim wheels share files",
    )
    require(
        any(name.startswith("tunaxis_data/") for name in ownership["tunaxis-data"])
        and not any(name.startswith("pineforge_data/") for name in ownership["tunaxis-data"]),
        "main wheel must own only the canonical package",
    )
    require(
        "pineforge_data/__init__.py" in ownership["pineforge-data"]
        and all(
            name.startswith(("pineforge_data/", f"pineforge_data-{version}.dist-info/"))
            for name in ownership["pineforge-data"]
        ),
        "shim wheel contains implementation files",
    )
    require(
        not any("/src/pineforge_data/" in name for name in source_members["tunaxis-data"]),
        "main sdist must not ship the compatibility package",
    )
    require(
        any("/src/pineforge_data/" in name for name in source_members["pineforge-data"]),
        "shim sdist must ship the single compatibility source",
    )


def paired_hashes(directory: Path) -> dict[str, str]:
    from .contract import sha256

    files = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    wheels = [name for name in files if name.startswith("tunaxis_data-") and name.endswith(".whl")]
    require(len(wheels) == 1, "expected exactly one tunaxis-data wheel")
    info, _members = archive_metadata(wheels[0], files[wheels[0]])
    version = info["Version"]
    require(isinstance(version, str), "wheel version is missing")
    verify_archives(files, version)
    return {name: sha256(data) for name, data in sorted(files.items())}
