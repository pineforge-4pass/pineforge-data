"""Validate section 3.2 release-ready identities and generate the data update."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HUB = "pineforge-4pass/pineforge-release"
IMAGE = f"ghcr.io/{HUB}"
SCHEMA = "pineforge/release-ready/v1"
LOCK = "src/pineforge_data/release_lock.json"
MANIFEST = "src/pineforge_data/release_manifest.json"
RUNTIME = "src/pineforge_data/release_contract.py"
DOCKERFILE = "docker/server.Dockerfile"
UPDATE_FILES = {LOCK, MANIFEST, RUNTIME, DOCKERFILE, "pyproject.toml"}
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
)
IDENTITY_FIELDS = (
    "schema",
    "release_version",
    "prerelease",
    "hub_commit",
    "versions",
    "image",
    "facts",
    "run_id",
)


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_json(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(data, object_pairs_hook=pairs)
    require(isinstance(value, dict), "expected a JSON object")
    return value


def version_key(version: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(version)
    require(match is not None, f"invalid release semver: {version!r}")
    assert match is not None
    return tuple(int(part) for part in match.groups()[:3])  # type: ignore[return-value]


def version_order(version: str) -> tuple[Any, ...]:
    key = version_key(version)
    prerelease = version.split("-", 1)[1] if "-" in version else None
    parts = (
        tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split("."))
        if prerelease
        else ()
    )
    return (*key, prerelease is None, parts)


def normalized_package(value: object) -> str:
    require(isinstance(value, str), "package version must be a string")
    assert isinstance(value, str)
    if SEMVER.fullmatch(value):
        return value
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(a|b|rc)(0|[1-9][0-9]*)", value)
    require(match is not None, "malformed package version")
    assert match is not None
    stage = {"a": "alpha", "b": "beta", "rc": "rc"}[match[2]]
    return f"{match[1]}-{stage}.{match[3]}"


def _string(value: object, pattern: re.Pattern[str], name: str) -> None:
    require(isinstance(value, str) and pattern.fullmatch(value), f"invalid {name}")


def validate_event(event: dict[str, Any]) -> None:
    require(
        set(event) == {*IDENTITY_FIELDS, "event_id", "manifest"},
        "release-ready/v1 fields differ from section 3.2",
    )
    require(event["schema"] == SCHEMA, "unsupported release-ready schema")
    _string(event["release_version"], SEMVER, "release_version")
    version = event["release_version"]
    require(type(event["prerelease"]) is bool, "prerelease must be boolean")
    require(event["prerelease"] == ("-" in version), "contradictory prerelease flag")
    _string(event["hub_commit"], HEX40, "hub_commit")
    require(type(event["run_id"]) is int and event["run_id"] > 0, "invalid run_id")
    versions = event["versions"]
    require(
        isinstance(versions, dict) and set(versions) == {"engine", "codegen", "hpo"},
        "unexpected producer identities",
    )
    fields = {
        "engine": {"version", "tag", "commit"},
        "codegen": {
            "version",
            "pypi_version",
            "commit",
            "sdist_sha256",
            "npm_version",
            "npm_integrity",
        },
        "hpo": {"version", "package_version", "tag", "commit", "wheel_sha256", "sdist_sha256"},
    }
    for name, expected in fields.items():
        component = versions[name]
        if name == "hpo":
            if version_order(version) < version_order("1.6.0-rc.1"):
                require(component is None, "HPO must be explicitly null before its cutoff")
                continue
            require(component is not None, "HPO is required from 1.6.0-rc.1")
        require(
            isinstance(component, dict) and set(component) == expected, f"invalid {name} identity"
        )
        require(component["version"] == version, f"{name} violates release lockstep")
        _string(component["commit"], HEX40, f"{name}.commit")
        if "tag" in component:
            # Stable input is identical in semver and PEP 440. For prereleases,
            # the hub's normalized version remains authoritative.
            package_version = component.get("package_version", version)
            require(component["tag"] == f"v{package_version}", f"invalid {name} tag")
        for key in ("sdist_sha256", "wheel_sha256"):
            if key in component:
                _string(component[key], HEX64, f"{name}.{key}")
        for key in ("pypi_version", "package_version"):
            if key in component:
                require(
                    normalized_package(component[key]) == version, f"contradictory {name}.{key}"
                )
        if name == "codegen":
            integrity = component["npm_integrity"]
            require(
                isinstance(integrity, str)
                and re.fullmatch(r"sha(256|384|512)-[A-Za-z0-9+/]+={0,2}", integrity),
                "invalid npm integrity",
            )
            require(component["npm_version"] == version, "contradictory codegen.npm_version")
            algorithm, encoded = integrity.split("-", 1)
            decoded = base64.b64decode(encoded, validate=True)
            require(len(decoded) == int(algorithm[3:]) // 8, "invalid npm integrity length")
    image = event["image"]
    require(
        isinstance(image, dict)
        and set(image) == {"repository", "digest", "amd64_digest", "arm64_digest"},
        "invalid image fields",
    )
    require(image["repository"] == IMAGE, "unexpected image repository")
    for key in ("digest", "amd64_digest", "arm64_digest"):
        _string(image[key], re.compile(r"sha256:[0-9a-f]{64}"), f"image.{key}")
    facts = event["facts"]
    require(
        isinstance(facts, dict) and set(facts) == {"commit", "path", "sha256", "release_key"},
        "invalid facts fields",
    )
    require(facts["path"] == "facts/facts.json", "unexpected facts source")
    require(facts["release_key"] == version, "facts release_key disagrees")
    _string(facts["commit"], HEX40, "facts.commit")
    _string(facts["sha256"], HEX64, "facts.sha256")
    manifest = event["manifest"]
    require(
        isinstance(manifest, dict) and set(manifest) == {"asset", "sha256"},
        "invalid manifest fields",
    )
    require(manifest["asset"] == "release-manifest.json", "unexpected manifest asset")
    _string(manifest["sha256"], HEX64, "manifest.sha256")
    require(event["event_id"] == f"sha256:{manifest['sha256']}", "event_id disagrees")


def verify_manifest(event: dict[str, Any], data: bytes) -> None:
    validate_event(event)
    require(sha256(data) == event["manifest"]["sha256"], "manifest digest mismatch")
    manifest = object_json(data)
    require(
        "event_id" not in manifest and "sha256" not in manifest.get("manifest", {}),
        "manifest contains a circular self hash",
    )
    for field in IDENTITY_FIELDS:
        require(manifest.get(field) == event[field], f"manifest disagrees: {field}")


def event_from_manifest(data: bytes) -> dict[str, Any]:
    manifest = object_json(data)
    event = {field: manifest[field] for field in IDENTITY_FIELDS}
    digest = sha256(data)
    event.update(
        event_id=f"sha256:{digest}", manifest={"asset": "release-manifest.json", "sha256": digest}
    )
    verify_manifest(event, data)
    return event


def fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
    request = Request(url, headers={"User-Agent": "pineforge-data-release", **(headers or {})})
    with urlopen(request, timeout=60) as response:
        return response.read()


def gh_bytes(endpoint: str, *, accept: str = "application/vnd.github+json") -> bytes:
    return subprocess.run(
        ["gh", "api", "-H", f"Accept: {accept}", endpoint], check=True, capture_output=True
    ).stdout


def gh_list(endpoint: str, key: str | None = None) -> list[Any]:
    result = []
    separator = "&" if "?" in endpoint else "?"
    page = 1
    while True:
        data = json.loads(gh_bytes(f"{endpoint}{separator}per_page=100&page={page}"))
        items = data[key] if key else data
        require(isinstance(items, list), "unexpected GitHub inventory response")
        result.extend(items)
        if len(items) < 100:
            return result
        page += 1


def hub_manifest(version: str) -> bytes:
    _string(version, SEMVER, "release tag")
    release = object_json(gh_bytes(f"repos/{HUB}/releases/tags/v{version}"))
    require(not release["draft"], "hub release is a draft")
    require(release["prerelease"] == ("-" in version), "hub release channel disagrees")
    assets = [asset for asset in release["assets"] if asset["name"] == "release-manifest.json"]
    require(len(assets) == 1, "hub release has no unique ready manifest")
    return gh_bytes(
        f"repos/{HUB}/releases/assets/{int(assets[0]['id'])}", accept="application/octet-stream"
    )


def newest_ready() -> tuple[dict[str, Any], bytes] | None:
    # Enumerate immutable ready assets; never substitute a moving image alias.
    releases = gh_list(f"repos/{HUB}/releases")
    candidates = [
        release
        for release in releases
        if not release["draft"]
        and not release["prerelease"]
        and release["tag_name"].startswith("v")
        and SEMVER.fullmatch(release["tag_name"][1:])
        and any(a["name"] == "release-manifest.json" for a in release["assets"])
    ]
    if not candidates:
        return None
    version = max(candidates, key=lambda item: version_key(item["tag_name"][1:]))["tag_name"][1:]
    data = hub_manifest(version)
    event = event_from_manifest(data)
    require(event["release_version"] == version, "manifest stored on the wrong hub release")
    return event, data


def verify_artifacts(event: dict[str, Any]) -> None:
    """Authenticate the actual consumed image, source tag and immutable facts."""
    version = event["release_version"]
    commit = object_json(gh_bytes(f"repos/{HUB}/commits/v{version}"))["sha"]
    require(commit == event["hub_commit"], "hub tag source disagrees")
    facts = fetch(
        f"https://raw.githubusercontent.com/{HUB}/{event['facts']['commit']}/facts/facts.json"
    )
    require(sha256(facts) == event["facts"]["sha256"], "facts digest mismatch")
    release_facts = object_json(facts).get("releases", {}).get(event["facts"]["release_key"])
    require(
        isinstance(release_facts, dict) and release_facts,
        "immutable facts have no target-release evidence",
    )
    scope = urlencode({"service": "ghcr.io", "scope": f"repository:{HUB}:pull"})
    token = object_json(fetch(f"https://ghcr.io/token?{scope}"))["token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join(
            (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
        ),
    }

    def registry(kind: str, digest: str) -> dict[str, Any]:
        raw = fetch(f"https://ghcr.io/v2/{HUB}/{kind}/{digest}", headers)
        require(f"sha256:{sha256(raw)}" == digest, "OCI content digest mismatch")
        return object_json(raw)

    index = registry("manifests", event["image"]["digest"])
    for arch in ("amd64", "arm64"):
        digest = event["image"][f"{arch}_digest"]
        children = [
            entry
            for entry in index["manifests"]
            if entry.get("platform", {}).get("architecture") == arch
            and entry.get("platform", {}).get("os") == "linux"
        ]
        require(
            len(children) == 1 and children[0]["digest"] == digest,
            f"OCI index disagrees for {arch}",
        )
        child = registry("manifests", digest)
        config = registry("blobs", child["config"]["digest"])
        require(
            config["os"] == "linux" and config["architecture"] == arch,
            "OCI configuration platform disagrees",
        )
        labels = config.get("config", {}).get("Labels", {})
        expected = {
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.revision": event["hub_commit"],
            "io.pineforge.engine.version": event["versions"]["engine"]["version"],
            "io.pineforge.codegen.version": event["versions"]["codegen"]["version"],
        }
        if event["versions"]["hpo"] is not None:
            expected["io.pineforge.hpo.version"] = event["versions"]["hpo"]["package_version"]
        require(
            all(labels.get(key) == value for key, value in expected.items()),
            f"{arch} image provenance disagrees with manifest",
        )


def runtime_pin(root: Path) -> str:
    module = ast.parse((root / RUNTIME).read_text())
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_RELEASE_IMAGE"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            require(isinstance(value, str), "runtime pin is not literal")
            return value
    raise ValueError("runtime default is missing")


def package_version(root: Path) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def load_lock(root: Path) -> dict[str, Any] | None:
    return object_json((root / LOCK).read_bytes()) if (root / LOCK).exists() else None


def verify_local(root: Path) -> dict[str, Any] | None:
    pin = runtime_pin(root)
    docker = (root / DOCKERFILE).read_text()
    require(f"ARG PINEFORGE_RELEASE_IMAGE={pin}\n" in docker, "runtime/server pin mismatch")
    lock = load_lock(root)
    if lock is None:
        require(not (root / MANIFEST).exists(), "manifest exists without a lock")
        # Only the pre-adapter baseline is allowed without a manifest.
        require(
            pin == f"{IMAGE}:1.0.0@sha256:"
            "b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86",
            "a new runtime pin requires a verified manifest",
        )
        return None
    event = lock["event"]
    verify_manifest(event, (root / MANIFEST).read_bytes())
    require(not event["prerelease"], "prerelease cannot become the production default")
    require(pin == image_ref(event), "defaults disagree with manifest")
    require(
        f"ARG PINEFORGE_HUB_COMMIT={event['hub_commit']}\n" in docker,
        "server source pin disagrees with manifest",
    )
    require(lock["package_version"] == package_version(root), "package bump disagrees with lock")
    rejected = lock.get("rejected_event_ids", [])
    require(
        isinstance(rejected, list)
        and all(
            isinstance(item, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item)
            for item in rejected
        ),
        "invalid rejected release identities",
    )
    require(event["event_id"] not in rejected, "installed release was explicitly rejected")
    return lock


def image_ref(event: dict[str, Any]) -> str:
    return f"{IMAGE}:{event['release_version']}@{event['image']['digest']}"


def update_files(
    root: Path, event: dict[str, Any], manifest: bytes, *, rollback: dict[str, Any] | None = None
) -> dict[str, bytes]:
    """Compute a reproducible patch, without touching an existing branch."""
    verify_manifest(event, manifest)
    require(not event["prerelease"], "prerelease is skipped by production policy")
    previous = verify_local(root)
    old_pin = runtime_pin(root)
    old_version = old_pin.split(":")[-2].split("@", 1)[0]
    require(
        version_key(event["release_version"]) > version_key(old_version)
        or (
            rollback is not None
            and version_key(event["release_version"]) < version_key(old_version)
        ),
        "duplicate, older or conflicting release identity",
    )
    current = package_version(root)
    major, minor, patch = version_key(current)
    replacement = f"{major}.{minor}.{patch + 1}"
    pyproject = (root / "pyproject.toml").read_text()
    # Import the existing version editor rather than duplicating its table rules.
    from bump_version import replace_project_version

    lock = {
        "event": event,
        "package_version": replacement,
        "previous": {
            "package_version": current,
            "image": old_pin,
            "event_id": previous["event"]["event_id"] if previous else None,
        },
    }
    rejected = previous.get("rejected_event_ids", []) if previous else []
    require(event["event_id"] not in rejected, "release was explicitly rejected by rollback")
    if rollback:
        require(
            previous is not None and rollback["rejected_event_id"] == previous["event"]["event_id"],
            "rollback must identify the rejected default",
        )
        lock["rollback"] = rollback
        rejected = [*rejected, rollback["rejected_event_id"]]
    if rejected:
        lock["rejected_event_ids"] = rejected
    runtime = re.sub(
        rb"DEFAULT_RELEASE_IMAGE = \([\s\S]*?\n\)",
        (
            f'DEFAULT_RELEASE_IMAGE = (\n    "{IMAGE}:{event["release_version"]}@"\n'
            f'    "{event["image"]["digest"]}"\n)'
        ).encode(),
        (root / RUNTIME).read_bytes(),
        count=1,
    )
    docker = re.sub(
        rb"^ARG PINEFORGE_HUB_COMMIT=[^\n]+",
        f"ARG PINEFORGE_HUB_COMMIT={event['hub_commit']}".encode(),
        (root / DOCKERFILE).read_bytes().replace(old_pin.encode(), image_ref(event).encode()),
        count=1,
        flags=re.MULTILINE,
    )
    return {
        RUNTIME: runtime,
        DOCKERFILE: docker,
        "pyproject.toml": replace_project_version(pyproject, current, replacement).encode(),
        LOCK: json_bytes(lock),
        MANIFEST: manifest,
    }
