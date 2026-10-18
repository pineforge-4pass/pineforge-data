from __future__ import annotations

import copy
import io
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from xml.etree import ElementTree

import pytest
import yaml
from bump_version import bumped_version, project_version
from release_chain import automation, contract, evidence, publishing, receipts, reporting

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/release_chain"
BASELINE_FILES = (contract.RUNTIME, contract.DOCKERFILE, "pyproject.toml", contract.SHIM_PROJECT)
EXTRAS = ("ccxt", "database", "database-e2e", "server", "docs", "release", "dev")


def paired_test_archives(directory: Path, version: str) -> None:
    """Create valid, independently parsed wheel/sdist bytes for hash-drift tests."""
    for project, package in (
        ("tunaxis-data", "tunaxis_data"),
        ("pineforge-data", "pineforge_data"),
    ):
        normalized = project.replace("-", "_")
        dist_info = f"{normalized}-{version}.dist-info"
        lines = [
            "Metadata-Version: 2.3",
            f"Name: {project}",
            f"Version: {version}",
            *(f"Provides-Extra: {extra}" for extra in EXTRAS),
        ]
        if project == "pineforge-data":
            lines.extend(
                [f"Requires-Dist: tunaxis-data=={version}"]
                + [
                    f"Requires-Dist: tunaxis-data[{extra}]=={version}; extra == '{extra}'"
                    for extra in EXTRAS
                ]
            )
        metadata = ("\n".join(lines) + "\n\n").encode()
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(f"{package}/__init__.py", b"\n")
            archive.writestr(f"{dist_info}/METADATA", metadata)
            archive.writestr(
                f"{dist_info}/WHEEL",
                b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
        (directory / f"{normalized}-{version}-py3-none-any.whl").write_bytes(wheel.getvalue())
        sdist = io.BytesIO()
        with tarfile.open(fileobj=sdist, mode="w:gz") as archive:
            info = tarfile.TarInfo(f"{normalized}-{version}/PKG-INFO")
            info.size = len(metadata)
            archive.addfile(info, io.BytesIO(metadata))
            if project == "pineforge-data":
                alias = tarfile.TarInfo(f"{normalized}-{version}/src/pineforge_data/__init__.py")
                alias.size = 1
                archive.addfile(alias, io.BytesIO(b"\n"))
        (directory / f"{normalized}-{version}.tar.gz").write_bytes(sdist.getvalue())


@pytest.fixture
def ready() -> tuple[dict[str, Any], bytes]:
    return (
        contract.object_json((FIXTURES / "ready-event.json").read_bytes()),
        (FIXTURES / "release-manifest.json").read_bytes(),
    )


@pytest.fixture
def baseline(tmp_path: Path) -> Path:
    for name in BASELINE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    # Independent from future updates to this repository's production default.
    pin = (
        "ghcr.io/pineforge-4pass/pineforge-release:1.0.0@sha256:"
        "b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86"
    )
    (tmp_path / contract.RUNTIME).write_text(f'DEFAULT_RELEASE_IMAGE = (\n    "{pin}"\n)\n')
    docker = tmp_path / contract.DOCKERFILE
    docker.write_text(f"ARG PINEFORGE_RELEASE_IMAGE={pin}\nARG PINEFORGE_HUB_COMMIT=unknown\n")
    main = (ROOT / "pyproject.toml").read_text()
    (tmp_path / "pyproject.toml").write_text(main + '\n[tool.example]\nversion = "keep-this"\n')
    (tmp_path / contract.SHIM_PROJECT).write_text((ROOT / contract.SHIM_PROJECT).read_text())
    return tmp_path


def apply(root: Path, patch: dict[str, bytes]) -> None:
    for name, data in patch.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_ready_event_binds_the_original_manifest_bytes(ready) -> None:
    event, manifest = ready
    contract.verify_manifest(event, manifest)
    assert contract.event_from_manifest(manifest) == event
    with pytest.raises(ValueError, match="digest mismatch"):
        contract.verify_manifest(event, manifest + b"\n")


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema",), "pineforge/release-ready/v2"),
        (("run_id",), True),
        (("prerelease",), "false"),
        (("prerelease",), True),
        (("event_id",), "sha256:" + "0" * 64),
        (("hub_commit",), "main"),
        (("image", "repository"), "ghcr.io/untrusted/release"),
        (("image", "digest"), "latest"),
        (("image", "arm64_digest"), "sha256:" + "A" * 64),
        (("facts", "path"), "../../secret"),
        (("facts", "release_key"), "1.5.0"),
        (("versions", "hpo"), None),
        (("versions", "codegen", "version"), "1.5.0"),
        (("versions", "codegen", "npm_version"), "1.5.0"),
        (("versions", "hpo", "package_version"), "1.5.0"),
        (("versions", "engine", "commit"), 1234),
        (("manifest", "asset"), "other.json"),
    ],
)
def test_malformed_or_contradictory_payload_is_rejected(ready, path, value) -> None:
    event, _manifest = ready
    target = event
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        contract.validate_event(event)


def test_legacy_three_field_payload_cannot_authorize_a_release() -> None:
    with pytest.raises(ValueError, match=r"section 3\.2"):
        contract.validate_event({"release_version": "1.6.0", "prerelease": False, "run_id": 1})


def test_valid_manifest_hash_cannot_hide_different_payload_identity(ready) -> None:
    event, raw = ready
    event["image"]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="manifest disagrees: image"):
        contract.verify_manifest(event, raw)


def test_update_changes_both_pins_and_independent_package_version(baseline, ready) -> None:
    event, manifest = ready
    current = project_version(baseline / "pyproject.toml")
    expected = bumped_version(current, "patch")
    patch = contract.update_files(baseline, event, manifest)
    assert set(patch) == contract.UPDATE_FILES
    apply(baseline, patch)
    assert contract.runtime_pin(baseline) == contract.image_ref(event)
    assert (
        f"PINEFORGE_RELEASE_IMAGE={contract.image_ref(event)}\n"
        in (baseline / contract.DOCKERFILE).read_text()
    )
    assert contract.package_version(baseline) == expected
    assert project_version(baseline / contract.SHIM_PROJECT) == expected
    assert 'version = "keep-this"' in (baseline / "pyproject.toml").read_text()
    lock = contract.verify_local(baseline)
    assert lock["event"]["event_id"] == event["event_id"]
    assert lock["previous"]["package_version"] == current
    assert (baseline / contract.MANIFEST).read_bytes() == manifest


@pytest.mark.parametrize("which", [contract.RUNTIME, contract.DOCKERFILE])
def test_a_single_stale_default_blocks_the_release(baseline, ready, which) -> None:
    event, manifest = ready
    apply(baseline, contract.update_files(baseline, event, manifest))
    path = baseline / which
    path.write_text(path.read_text().replace("1.6.0", "1.5.0", 1))
    with pytest.raises(ValueError, match=r"pin mismatch|defaults disagree"):
        contract.verify_local(baseline)


def test_replay_cannot_bump_or_republish_an_installed_identity(baseline, ready) -> None:
    event, manifest = ready
    expected = bumped_version(project_version(baseline / "pyproject.toml"), "patch")
    apply(baseline, contract.update_files(baseline, event, manifest))
    with pytest.raises(ValueError, match="duplicate, older or conflicting"):
        contract.update_files(baseline, event, manifest)
    assert contract.package_version(baseline) == expected


def registry_fixture(
    monkeypatch, event, *, bad_facts=False, corrupt_arch=None, extra_blobs=None
) -> None:
    blobs = {}
    for path in FIXTURES.glob("*.json"):
        data = path.read_bytes()
        blobs[f"sha256:{contract.sha256(data)}"] = data
    blobs.update(extra_blobs or {})

    def fetch(url, headers=None):
        if "raw.githubusercontent.com" in url:
            if bad_facts:
                return b'{"releases":{}}'
            return (FIXTURES / "facts.json").read_bytes()
        if "/token?" in url:
            return b'{"token":"offline-test-only"}'
        digest = url.rsplit("/", 1)[1]
        if corrupt_arch and digest == event["image"][f"{corrupt_arch}_digest"]:
            return blobs[digest] + b"\n"
        return blobs[digest]

    monkeypatch.setattr(contract, "fetch", fetch)
    monkeypatch.setattr(
        contract, "gh_bytes", lambda endpoint: contract.json_bytes({"sha": event["hub_commit"]})
    )


def test_image_index_both_children_and_facts_are_verified(ready, monkeypatch) -> None:
    event, _manifest = ready
    registry_fixture(monkeypatch, event)
    contract.verify_artifacts(event)


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_corrupt_child_prevents_pr_creation(ready, monkeypatch, arch) -> None:
    event, _manifest = ready
    registry_fixture(monkeypatch, event, corrupt_arch=arch)
    with pytest.raises(ValueError, match="OCI content digest mismatch"):
        contract.verify_artifacts(event)


def test_facts_without_target_evidence_are_not_a_ready_release(ready, monkeypatch) -> None:
    event, _manifest = ready
    registry_fixture(monkeypatch, event, bad_facts=True)
    event["facts"]["sha256"] = contract.sha256(b'{"releases":{}}')
    with pytest.raises(ValueError, match="no target-release evidence"):
        contract.verify_artifacts(event)


@pytest.mark.parametrize(
    "label,value",
    [
        ("org.opencontainers.image.version", "1.6.0"),
        ("io.pineforge.engine.version", "1.5.0"),
    ],
)
def test_valid_oci_hashes_cannot_hide_wrong_runtime_provenance(
    ready, monkeypatch, label, value
) -> None:
    event, _manifest = ready
    config = contract.object_json((FIXTURES / "amd64-config.json").read_bytes())
    config["config"]["Labels"][label] = value
    config_raw = contract.json_bytes(config)
    config_digest = f"sha256:{contract.sha256(config_raw)}"
    child = contract.object_json((FIXTURES / "amd64-manifest.json").read_bytes())
    child["config"]["digest"] = config_digest
    child_raw = contract.json_bytes(child)
    child_digest = f"sha256:{contract.sha256(child_raw)}"
    index = contract.object_json((FIXTURES / "index.json").read_bytes())
    index["manifests"][0]["digest"] = child_digest
    index_raw = contract.json_bytes(index)
    index_digest = f"sha256:{contract.sha256(index_raw)}"
    event["image"].update(digest=index_digest, amd64_digest=child_digest)
    registry_fixture(
        monkeypatch,
        event,
        extra_blobs={config_digest: config_raw, child_digest: child_raw, index_digest: index_raw},
    )
    with pytest.raises(ValueError, match="image provenance disagrees"):
        contract.verify_artifacts(event)


def green_gate() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    gate = {
        "run_id": 70,
        "attempt": 2,
        "head": "a" * 40,
        "base": "b" * 40,
        "pr": 7,
        "event": "pull_request",
        "jobs": dict.fromkeys(evidence.JOBS, "success"),
        "e2e": {key: sorted(value) for key, value in evidence.TESTS.items()},
    }
    run = {
        "id": 70,
        "run_attempt": 2,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
    }
    jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (*evidence.JOBS, "release/ready")
    ]
    return gate, run, jobs


def test_gate_accepts_only_the_complete_current_attempt() -> None:
    gate, run, jobs = green_gate()
    evidence.validate_gate(gate, run, jobs, head="a" * 40, base="b" * 40, pr=7)


@pytest.mark.parametrize("job_name", [*evidence.JOBS, "release/ready"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "missing"])
def test_green_workflow_cannot_hide_a_missing_or_failed_gate(job_name, result) -> None:
    gate, run, jobs = green_gate()
    if result == "missing":
        jobs = [job for job in jobs if job["name"] != job_name]
    else:
        next(job for job in jobs if job["name"] == job_name)["conclusion"] = result
    with pytest.raises(ValueError, match="required job"):
        evidence.validate_gate(gate, run, jobs, head="a" * 40, base="b" * 40, pr=7)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head", "c" * 40),
        ("base", "c" * 40),
        ("attempt", 1),
        ("pr", 8),
    ],
)
def test_old_green_evidence_does_not_authorize_new_merge_inputs(field, value) -> None:
    gate, run, jobs = green_gate()
    gate[field] = value
    with pytest.raises(ValueError, match="stale"):
        evidence.validate_gate(gate, run, jobs, head="a" * 40, base="b" * 40, pr=7)


@pytest.mark.parametrize("suite", list(evidence.TESTS))
@pytest.mark.parametrize("bad", ["skipped", "failure", "error", "empty", "missing"])
def test_required_runtime_inventory_cannot_be_vacuously_green(tmp_path, suite, bad) -> None:
    root = ElementTree.Element("testsuite")
    names = sorted(evidence.TESTS[suite])
    if bad == "missing":
        names.pop()
    if bad != "empty":
        for name in names:
            ElementTree.SubElement(root, "testcase", name=name)
        if bad in {"skipped", "failure", "error"}:
            ElementTree.SubElement(root[0], bad)
    path = tmp_path / "junit.xml"
    ElementTree.ElementTree(root).write(path)
    with pytest.raises(ValueError):
        evidence.check_junit(path, suite)


def bot_pr(event, *, human=False):
    meta = {
        "kind": "release",
        "event_id": event["event_id"],
        "version": bumped_version(project_version(ROOT / "pyproject.toml"), "patch"),
        "release_version": "1.6.0",
        "generated_head": "a" * 40,
        "base": "b" * 40,
    }
    return {
        "number": 7,
        "body": automation.body(meta),
        "user": {"login": "release[bot]"},
        "head": {
            "sha": ("c" if human else "a") * 40,
            "ref": "bot/pineforge-release/test",
            "repo": {"full_name": "owner/data"},
        },
        "base": {"sha": "b" * 40, "ref": "main"},
        "state": "open",
    }


def test_human_commit_blocks_all_bot_branch_mutations(ready, monkeypatch) -> None:
    event, manifest = ready
    pr = bot_pr(event, human=True)
    writes = []

    def api(repo, endpoint, method="GET", data=None):
        if method != "GET":
            writes.append(endpoint)
        return pr

    monkeypatch.setattr(automation, "api", api)
    with pytest.raises(ValueError, match="human/unrecorded"):
        automation.maybe_merge(
            "owner/data", pr, automation.receipt(event, "test"), "release[bot]", event, manifest
        )
    assert writes == []


def test_failed_consumer_e2e_never_calls_merge_api(ready, baseline, monkeypatch) -> None:
    event, manifest = ready
    pr = bot_pr(event)
    writes = []
    gate, run, jobs = green_gate()
    run["pull_requests"] = [{"number": 7}]
    next(job for job in jobs if job["name"] == "Docker runtime")["conclusion"] = "failure"

    def api(repo, endpoint, method="GET", data=None):
        if method != "GET":
            writes.append(endpoint)
        if endpoint.startswith("actions/"):
            return {"workflow_runs": [run]}
        return pr

    def gate_for_run(*args, **kwargs):
        evidence.validate_gate(gate, run, jobs, head="a" * 40, base="b" * 40, pr=7)
        return gate

    def snapshot(revision, target):
        for name in BASELINE_FILES:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(baseline / name, destination)

    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(automation, "git", lambda *args: "b" * 40)
    monkeypatch.setattr(automation, "snapshot", snapshot)
    monkeypatch.setattr(automation, "assert_patch", lambda *args: None)
    monkeypatch.setattr(automation, "gate_for_run", gate_for_run)
    with pytest.raises(ValueError, match="Docker runtime"):
        automation.maybe_merge(
            "owner/data", pr, automation.receipt(event, "test"), "release[bot]", event, manifest
        )
    assert writes == []


def test_replayed_applied_event_creates_no_pr_or_merge(ready, baseline, monkeypatch) -> None:
    event, manifest = ready
    apply(baseline, contract.update_files(baseline, event, manifest))
    monkeypatch.setattr(automation, "git", lambda *args: "b" * 40)
    monkeypatch.setattr(automation, "open_bot_prs", lambda repo: [])
    monkeypatch.setattr(automation, "newest_ready", lambda: (event, manifest))
    monkeypatch.setattr(automation, "hub_manifest", lambda version: manifest)
    monkeypatch.setattr(
        automation,
        "snapshot",
        lambda revision, target: apply(
            target, {name: (baseline / name).read_bytes() for name in contract.UPDATE_FILES}
        ),
    )
    monkeypatch.setattr(automation, "api", lambda *args: pytest.fail("duplicate mutated GitHub"))
    monkeypatch.setattr(publishing, "recover_applied", lambda *args: None)
    report = automation.receipt(None, "test")
    automation.reconcile("owner/data", {"client_payload": event}, report, "release[bot]")
    assert report["state"] == "merged"


def test_distribution_publication_refuses_different_pypi_bytes(tmp_path, monkeypatch) -> None:
    version = bumped_version(project_version(ROOT / "pyproject.toml"), "patch")
    bundle = tmp_path / "bundle"
    (bundle / "source").mkdir(parents=True)
    (bundle / "source/pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    dist = bundle / "dist"
    dist.mkdir()
    paired_test_archives(dist, version)
    hashes = evidence.distribution_hashes(dist)
    report = automation.receipt(None, "verify")
    (bundle / "evidence.json").write_bytes(contract.json_bytes({"distributions": hashes}))
    report["state"] = "deploying"
    monkeypatch.setattr(publishing, "pypi_files", lambda version: dict.fromkeys(hashes, "0" * 64))
    with pytest.raises(ValueError, match="digests disagree"):
        publishing.verify_publication("owner/data", bundle, report)
    assert report["state"] == "deploying"
    assert report["deployed"] is None


def test_publication_stages_exact_files_inside_the_bundle(baseline) -> None:
    bundle = baseline / "bundle"
    apply(
        bundle / "source",
        {name: (baseline / name).read_bytes() for name in BASELINE_FILES},
    )
    dist = bundle / "dist"
    dist.mkdir()
    paired_test_archives(dist, contract.package_version(baseline))
    original = {path.name: path.read_bytes() for path in dist.iterdir()}
    publishing.stage_distributions(bundle)
    for project in publishing.PYPI_PROJECTS:
        staged = bundle / "pypi" / project
        expected = {
            name: data
            for name, data in original.items()
            if name.startswith(project.replace("-", "_") + "-")
        }
        assert {path.name: path.read_bytes() for path in staged.iterdir()} == expected
    assert {path.name: path.read_bytes() for path in dist.iterdir()} == original


@pytest.mark.parametrize("existing_project", ["tunaxis-data", "pineforge-data"])
def test_prepare_reports_only_the_missing_pypi_project(
    baseline, ready, monkeypatch, existing_project
) -> None:
    event, _manifest = ready
    version = project_version(baseline / "pyproject.toml")
    artifacts = baseline / "artifacts"
    artifacts.mkdir()
    paired_test_archives(artifacts, version)
    hashes = evidence.distribution_hashes(artifacts)
    pr = bot_pr(event)
    source, tree, base = "c" * 40, "d" * 40, "b" * 40
    pr.update(merged=True, merged_at="2020-01-01T00:05:00Z", merge_commit_sha=source)
    pr["body"] = automation.body(
        {"kind": "package", "version": version, "generated_head": pr["head"]["sha"], "base": base}
    )
    run = {"event": "push", "head_branch": "main", "head_sha": source, "run_attempt": 1}
    responses = {
        "actions/runs/80": run,
        f"commits/{source}/pulls?per_page=100": [pr],
        "pulls/7": pr,
    }
    monkeypatch.setattr(publishing, "api", lambda repo, route: responses[route])
    monkeypatch.setenv("RELEASE_BOT_LOGIN", pr["user"]["login"])
    monkeypatch.setattr(
        publishing, "git",
        lambda *args: {
            ("fetch", "origin", source): "",
            ("rev-parse", f"{source}^{{tree}}"): tree,
            ("rev-parse", f"{source}^1"): base,
        }[args],
    )
    monkeypatch.setattr(
        publishing, "snapshot",
        lambda revision, target: apply(
            target, {name: (baseline / name).read_bytes() for name in BASELINE_FILES}
        ),
    )
    monkeypatch.setattr(
        publishing, "gate_for_run",
        lambda *args, **kwargs: {"tree": tree, "tested_sha": source, "distributions": hashes},
    )
    monkeypatch.setattr(publishing, "current_intent", lambda *args: None)

    def download(command, *, check):
        assert command[:3] == ["gh", "run", "download"] and check
        destination = Path(command[-1])
        destination.mkdir()
        for artifact in artifacts.iterdir():
            shutil.copyfile(artifact, destination / artifact.name)

    requested = []

    def fetch(url):
        requested.append(url)
        if url != f"https://pypi.org/pypi/{existing_project}/{version}/json":
            raise HTTPError(url, 404, "project version is missing", None, None)
        return contract.json_bytes({"urls": [
            {"filename": name, "digests": {"sha256": digest}}
            for name, digest in hashes.items()
            if name.startswith(existing_project.replace("-", "_") + "-")
        ]})

    monkeypatch.setattr(publishing.subprocess, "run", download)
    monkeypatch.setattr(publishing, "fetch", fetch)
    report = automation.receipt(None)
    outputs = publishing.prepare(baseline, "owner/data", 80, baseline / "bundle", report)
    assert outputs["eligible"] == "true"
    assert outputs["published"] == "false"
    assert report["state"] == "deploying"
    for project in publishing.PYPI_PROJECTS:
        assert outputs["published_" + project.replace("-", "_")] == str(
            project == existing_project
        ).lower()
    assert set(requested) == {
        f"https://pypi.org/pypi/{project}/{version}/json" for project in publishing.PYPI_PROJECTS
    }


def test_public_receipt_cannot_contain_homegrown_or_private_fields(ready) -> None:
    event, _manifest = ready
    report = automation.receipt(event, "test")
    assert set(report) == receipts.FIELDS
    assert reporting.public_receipt(report) == report
    report["detail"] = "https://github.com/private-owner/private-repo/runs/1"
    with pytest.raises(ValueError, match="receipt fields differ"):
        reporting.public_receipt(report)


def test_failed_downstream_job_overrides_earlier_green_receipt(tmp_path, ready) -> None:
    event, _manifest = ready
    report = automation.receipt(event, "prepare")
    report["state"] = "deploying"
    directory = tmp_path / "release-stage-1-20-prepare"
    directory.mkdir()
    (directory / "release-receipt.json").write_bytes(contract.json_bytes(report))
    result = reporting.collect(tmp_path, failed=True, stage="publication")
    assert result["state"] == "failed"
    assert result["event_id"] == event["event_id"]
    assert result["deployed"] is None


def test_duplicate_open_pr_is_reused_without_a_new_commit(ready, baseline, monkeypatch) -> None:
    event, manifest = ready
    pr = bot_pr(event)
    checked = []
    monkeypatch.setattr(automation, "git", lambda *args: "b" * 40)
    monkeypatch.setattr(automation, "open_bot_prs", lambda repo: [pr])
    monkeypatch.setattr(automation, "newest_ready", lambda: (event, manifest))
    monkeypatch.setattr(automation, "hub_manifest", lambda version: manifest)
    monkeypatch.setattr(automation, "verify_artifacts", lambda event: None)
    monkeypatch.setattr(
        automation,
        "snapshot",
        lambda revision, target: apply(
            target,
            {
                name: (baseline / name).read_bytes()
                for name in BASELINE_FILES
            },
        ),
    )
    monkeypatch.setattr(automation, "create_pr", lambda *args: pytest.fail("duplicate PR"))
    monkeypatch.setattr(
        automation, "maybe_merge", lambda repo, pr, *args: checked.append(pr["number"])
    )
    report = automation.receipt(None, "test")
    automation.reconcile("owner/data", {"client_payload": event}, report, "release[bot]")
    assert checked == [7]
    assert report["pr"]["number"] == 7


def next_ready(event):
    manifest = {key: copy.deepcopy(event[key]) for key in contract.IDENTITY_FIELDS}
    manifest["release_version"] = "1.6.1"
    manifest["facts"]["release_key"] = "1.6.1"
    for component in manifest["versions"].values():
        for key in ("version", "package_version", "pypi_version", "npm_version"):
            if key in component:
                component[key] = "1.6.1"
        if "tag" in component:
            component["tag"] = "v1.6.1"
    raw = contract.json_bytes(manifest)
    return contract.event_from_manifest(raw), raw


def test_older_delivery_is_reported_as_superseded(ready, monkeypatch) -> None:
    event, manifest = ready
    monkeypatch.setattr(automation, "git", lambda *args: "b" * 40)
    monkeypatch.setattr(automation, "open_bot_prs", lambda repo: [])
    monkeypatch.setattr(automation, "newest_ready", lambda: next_ready(event))
    monkeypatch.setattr(automation, "hub_manifest", lambda version: manifest)
    monkeypatch.setattr(automation, "create_pr", lambda *args: pytest.fail("older PR"))
    report = automation.receipt(None, "test")
    automation.reconcile("owner/data", {"client_payload": event}, report, "release[bot]")
    assert report["state"] == "superseded"
    assert report["event_id"] == event["event_id"]


def test_newer_pr_exists_before_obsolete_pr_is_closed(ready, baseline, monkeypatch) -> None:
    old_event, _raw = ready
    event, raw = next_ready(old_event)
    old = bot_pr(old_event)
    operations = []
    monkeypatch.setattr(automation, "git", lambda *args: "b" * 40)
    monkeypatch.setattr(automation, "open_bot_prs", lambda repo: [old])
    monkeypatch.setattr(automation, "newest_ready", lambda: (event, raw))
    monkeypatch.setattr(automation, "hub_manifest", lambda version: raw)
    monkeypatch.setattr(automation, "verify_artifacts", lambda event: None)
    monkeypatch.setattr(
        automation,
        "snapshot",
        lambda revision, target: apply(
            target,
            {
                name: (baseline / name).read_bytes()
                for name in BASELINE_FILES
            },
        ),
    )

    def create(*args):
        operations.append("create")
        return {**bot_pr(event), "number": 8}

    def api(repo, endpoint, method="GET", data=None):
        operations.append((method, endpoint, data))

    monkeypatch.setattr(automation, "create_pr", create)
    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(automation, "maybe_merge", lambda *args: None)
    report = automation.receipt(None, "test")
    automation.reconcile("owner/data", {"client_payload": event}, report, "release[bot]")
    assert operations[0] == "create"
    assert ("PATCH", "pulls/7", {"state": "closed"}) in operations
    assert set(report) == receipts.FIELDS
    assert all(operation[0] != "DELETE" for operation in operations if isinstance(operation, tuple))


def test_prerelease_is_a_policy_receipt_without_a_production_pr(ready, monkeypatch) -> None:
    original, _raw = ready
    manifest = {key: copy.deepcopy(original[key]) for key in contract.IDENTITY_FIELDS}
    manifest.update(release_version="1.6.0-rc.1", prerelease=True)
    manifest["facts"]["release_key"] = "1.6.0-rc.1"
    for component in manifest["versions"].values():
        component["version"] = "1.6.0-rc.1"
    manifest["versions"]["engine"]["tag"] = "v1.6.0-rc.1"
    manifest["versions"]["codegen"].update(pypi_version="1.6.0rc1", npm_version="1.6.0-rc.1")
    manifest["versions"]["hpo"].update(package_version="1.6.0rc1", tag="v1.6.0rc1")
    raw = contract.json_bytes(manifest)
    event = contract.event_from_manifest(raw)
    monkeypatch.setattr(automation, "git", lambda *args: "b" * 40)
    monkeypatch.setattr(automation, "open_bot_prs", lambda repo: [])
    monkeypatch.setattr(automation, "hub_manifest", lambda version: raw)
    monkeypatch.setattr(
        automation, "newest_ready", lambda: pytest.fail("prerelease selected stable")
    )
    report = automation.receipt(None, "test")
    automation.reconcile("owner/data", {"client_payload": event}, report, "release[bot]")
    assert report["state"] == "skipped_by_policy"


def test_completed_receipt_replay_does_not_dispatch_or_publish(
    ready, baseline, monkeypatch
) -> None:
    event, raw = ready
    apply(baseline, contract.update_files(baseline, event, raw))
    saved = automation.receipt(event, "verify")
    saved["state"] = "verified"
    saved["pr"] = {
        "number": 7,
        "base": "main",
        "base_sha": "b" * 40,
        "head": "c" * 40,
        "merge": "a" * 40,
    }
    saved["deployed"] = receipts.served(
        event, "a" * 40, project_version(baseline / "pyproject.toml"), {"wheel": "a", "sdist": "b"}
    )
    monkeypatch.setattr(publishing, "stored_receipt", lambda *args: saved)
    monkeypatch.setattr(publishing, "verified_package", lambda *args: saved["deployed"])
    monkeypatch.setattr(publishing, "api", lambda *args: pytest.fail("replayed publication"))
    report = automation.receipt(event, "reconcile")
    publishing.recover_applied("owner/data", baseline, report)
    assert report["state"] == "verified"
    assert report["deployed"] == saved["deployed"]


def test_workflows_keep_every_gate_ahead_of_oidc_publication() -> None:
    workflows = ROOT / ".github/workflows"
    ci = yaml.load((workflows / "ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert ci["jobs"]["test"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]
    assert set(ci["jobs"]["ready"]["needs"]) == evidence.NEEDS
    assert ci["jobs"]["ready"]["if"] == "always()"
    assert ci["jobs"]["ready"]["name"] == "release/ready"
    for job, suite, enabled in (
        ("docker", "docker", "PINEFORGE_DOCKER_TEST=1"),
        ("database-e2e", "database", None),
    ):
        commands = "\n".join(step.get("run", "") for step in ci["jobs"][job]["steps"])
        assert f"--suite {suite}" in commands
        if enabled:
            assert enabled in commands
        else:
            assert ci["jobs"][job]["env"]["PINEFORGE_DATABASE_E2E"] == "1"
    release = yaml.load((workflows / "release.yml").read_text(), Loader=yaml.BaseLoader)
    assert release["on"]["workflow_run"]["workflows"] == ["CI"]
    assert set(release["jobs"]["publish"]["needs"]) == {"prepare", "build", "github-release"}
    steps = release["jobs"]["publish"]["steps"]
    preflight = next(i for i, step in enumerate(steps) if "preflight" in step.get("run", ""))
    publish = [
        (i, step)
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("pypa/")
    ]
    assert len(publish) == 2
    assert preflight < publish[0][0] < publish[1][0]
    assert publish[0][1]["with"]["packages-dir"] == "bundle/pypi/tunaxis-data/"
    assert publish[1][1]["with"]["packages-dir"] == "bundle/pypi/pineforge-data/"
    assert publish[0][1]["if"] == "needs.prepare.outputs.published_tunaxis_data != 'true'"
    assert publish[1][1]["if"] == "needs.prepare.outputs.published_pineforge_data != 'true'"
    assert all(step["with"]["skip-existing"] == "true" for _index, step in publish)
    for project in publishing.PYPI_PROJECTS:
        key = "published_" + project.replace("-", "_")
        assert release["jobs"]["prepare"]["outputs"][key] == (
            "${{ steps.release.outputs." + key + " }}"
        )
    assert release["jobs"]["publish"]["environment"]["name"] == "pypi"
    receiver = yaml.load((workflows / "pineforge-release.yml").read_text(), Loader=yaml.BaseLoader)
    assert receiver["on"]["repository_dispatch"]["types"] == ["pineforge-release"]


def test_rollback_is_a_new_patch_and_rejected_events_cannot_return(ready, baseline) -> None:
    good, good_raw = ready
    apply(baseline, contract.update_files(baseline, good, good_raw))
    good_version = project_version(baseline / "pyproject.toml")
    rejected, rejected_raw = next_ready(good)
    apply(baseline, contract.update_files(baseline, rejected, rejected_raw))
    expected = bumped_version(project_version(baseline / "pyproject.toml"), "patch")
    rollback = {
        "package_version": good_version,
        "source": "a" * 40,
        "rejected_event_id": rejected["event_id"],
    }
    apply(baseline, contract.update_files(baseline, good, good_raw, rollback=rollback))
    lock = contract.verify_local(baseline)
    assert lock["package_version"] == expected
    assert project_version(baseline / contract.SHIM_PROJECT) == expected
    assert lock["event"]["event_id"] == good["event_id"]
    assert lock["rejected_event_ids"] == [rejected["event_id"]]
    assert contract.runtime_pin(baseline) == contract.image_ref(good)
    with pytest.raises(ValueError, match="explicitly rejected"):
        contract.update_files(baseline, rejected, rejected_raw)


def test_rollback_requires_a_previously_verified_package(ready, monkeypatch) -> None:
    event, _raw = ready
    lock = {
        "event": event,
        "rollback": {
            "package_version": project_version(ROOT / "pyproject.toml"),
            "source": "a" * 40,
            "rejected_event_id": "sha256:" + "b" * 64,
        },
        "rejected_event_ids": ["sha256:" + "b" * 64],
    }
    monkeypatch.setattr(publishing, "stored_receipt", lambda *args: None)
    with pytest.raises(ValueError, match="no known-good receipt"):
        publishing.verify_rollback(lock, "owner/data")


def test_reconciliation_repairs_a_dropped_rollback_publication(
    ready, baseline, monkeypatch
) -> None:
    good, good_raw = ready
    apply(baseline, contract.update_files(baseline, good, good_raw))
    good_version = project_version(baseline / "pyproject.toml")
    rejected, rejected_raw = next_ready(good)
    apply(baseline, contract.update_files(baseline, rejected, rejected_raw))
    apply(
        baseline,
        contract.update_files(
            baseline,
            good,
            good_raw,
            rollback={
                "package_version": good_version,
                "source": "a" * 40,
                "rejected_event_id": rejected["event_id"],
            },
        ),
    )
    recovered = []
    monkeypatch.setattr(automation, "git", lambda *args: "b" * 40)
    monkeypatch.setattr(automation, "open_bot_prs", lambda repo: [])
    monkeypatch.setattr(automation, "newest_ready", lambda: (rejected, rejected_raw))
    monkeypatch.setattr(
        automation,
        "snapshot",
        lambda revision, target: apply(
            target, {name: (baseline / name).read_bytes() for name in contract.UPDATE_FILES}
        ),
    )

    def recover(repo, source, report):
        recovered.append(report["event_id"])
        report["state"] = "deploying"

    monkeypatch.setattr(publishing, "recover_applied", recover)
    report = automation.receipt(None, "test")
    automation.reconcile("owner/data", {}, report, "release[bot]")
    assert recovered == [good["event_id"]]
    assert report["state"] == "deploying"
