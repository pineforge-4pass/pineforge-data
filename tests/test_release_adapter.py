from __future__ import annotations

import copy
import io
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
import yaml
from release_chain import automation, contract, evidence, publishing, receipts, reporting

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/release_chain"


@pytest.fixture
def ready() -> tuple[dict[str, Any], bytes]:
    return (
        contract.object_json((FIXTURES / "ready-event.json").read_bytes()),
        (FIXTURES / "release-manifest.json").read_bytes(),
    )


@pytest.fixture
def baseline(tmp_path: Path) -> Path:
    for name in (contract.RUNTIME, contract.DOCKERFILE, "pyproject.toml"):
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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pineforge-data"\nversion = "0.3.0"\n\n'
        '[tool.example]\nversion = "keep-this"\n'
    )
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
    "repository",
    [
        pytest.param("ghcr.io/pineforge-4pass/pineforge-release", id="old-org"),
        pytest.param("ghcr.io/tunaxis/pineforge-release", id="new-org"),
    ],
)
def test_ready_contract_accepts_each_exact_image_repository(ready, repository) -> None:
    event, raw = ready
    manifest = contract.object_json(raw)
    manifest["image"]["repository"] = repository
    raw = contract.json_bytes(manifest)
    digest = contract.sha256(raw)
    event["image"]["repository"] = repository
    event["manifest"]["sha256"] = digest
    event["event_id"] = f"sha256:{digest}"

    contract.validate_event(event)
    contract.verify_manifest(event, raw)
    assert contract.event_from_manifest(raw) == event


@pytest.mark.parametrize(
    "repository",
    [
        pytest.param("ghcr.io/other/pineforge-release", id="third-org"),
        pytest.param("ghcr.io/pineforge-4pass/other", id="old-org-other-repo"),
        pytest.param("ghcr.io/tunaxis/other", id="new-org-other-repo"),
        pytest.param("", id="empty"),
        pytest.param("GHCR.IO/tunaxis/pineforge-release", id="different-case"),
        pytest.param("ghcr.io/tunaxis/pineforge-release/", id="trailing-slash"),
        pytest.param(None, id="null"),
        pytest.param(["ghcr.io/pineforge-4pass/pineforge-release"], id="old-org-list"),
        pytest.param(["ghcr.io/tunaxis/pineforge-release"], id="new-org-list"),
    ],
)
def test_ready_contract_rejects_unexpected_image_repository(ready, repository) -> None:
    event, _manifest = ready
    event["image"]["repository"] = repository
    with pytest.raises(ValueError, match=r"^unexpected image repository$"):
        contract.validate_event(event)


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


@pytest.mark.parametrize(
    "event_repository,manifest_repository",
    [
        pytest.param(
            "ghcr.io/tunaxis/pineforge-release",
            "ghcr.io/pineforge-4pass/pineforge-release",
            id="new-event-old-manifest",
        ),
        pytest.param(
            "ghcr.io/pineforge-4pass/pineforge-release",
            "ghcr.io/tunaxis/pineforge-release",
            id="old-event-new-manifest",
        ),
    ],
)
def test_allowed_repository_cannot_hide_different_manifest_repository(
    ready, event_repository, manifest_repository
) -> None:
    event, raw = ready
    manifest = contract.object_json(raw)
    manifest["image"]["repository"] = manifest_repository
    raw = contract.json_bytes(manifest)
    digest = contract.sha256(raw)
    event["image"]["repository"] = event_repository
    event["manifest"]["sha256"] = digest
    event["event_id"] = f"sha256:{digest}"

    with pytest.raises(ValueError, match=r"^manifest disagrees: image$"):
        contract.verify_manifest(event, raw)


def test_update_changes_both_pins_and_independent_package_version(baseline, ready) -> None:
    event, manifest = ready
    patch = contract.update_files(baseline, event, manifest)
    assert set(patch) == contract.UPDATE_FILES
    apply(baseline, patch)
    assert contract.runtime_pin(baseline) == contract.image_ref(event)
    assert (
        f"PINEFORGE_RELEASE_IMAGE={contract.image_ref(event)}\n"
        in (baseline / contract.DOCKERFILE).read_text()
    )
    assert contract.package_version(baseline) == "0.3.1"
    assert 'version = "keep-this"' in (baseline / "pyproject.toml").read_text()
    lock = contract.verify_local(baseline)
    assert lock["event"]["event_id"] == event["event_id"]
    assert lock["previous"]["package_version"] == "0.3.0"
    assert (baseline / contract.MANIFEST).read_bytes() == manifest


@pytest.mark.parametrize(
    "repository",
    [
        pytest.param("ghcr.io/pineforge-4pass/pineforge-release", id="old-org"),
        pytest.param("ghcr.io/tunaxis/pineforge-release", id="new-org"),
    ],
)
def test_update_pins_preserve_the_validated_image_repository(baseline, ready, repository) -> None:
    _event, raw = ready
    manifest = contract.object_json(raw)
    manifest["image"]["repository"] = repository
    raw = contract.json_bytes(manifest)
    event = contract.event_from_manifest(raw)
    expected_pin = (
        f"{event['image']['repository']}:{event['release_version']}@{event['image']['digest']}"
    )
    expected_runtime = (
        "DEFAULT_RELEASE_IMAGE = (\n"
        f'    "{event["image"]["repository"]}:{event["release_version"]}@"\n'
        f'    "{event["image"]["digest"]}"\n)\n'
    )

    assert contract.image_ref(event) == expected_pin
    patch = contract.update_files(baseline, event, raw)
    assert patch[contract.RUNTIME].decode() == expected_runtime
    assert f"ARG PINEFORGE_RELEASE_IMAGE={expected_pin}\n" in patch[contract.DOCKERFILE].decode()
    apply(baseline, patch)
    assert contract.runtime_pin(baseline) == expected_pin
    lock = contract.verify_local(baseline)
    assert lock["event"] == event


def test_new_repository_pins_never_use_the_old_organization(baseline, ready) -> None:
    _event, raw = ready
    manifest = contract.object_json(raw)
    manifest["image"]["repository"] = "ghcr.io/tunaxis/pineforge-release"
    raw = contract.json_bytes(manifest)
    event = contract.event_from_manifest(raw)
    expected_pin = (
        f"{event['image']['repository']}:{event['release_version']}@{event['image']['digest']}"
    )

    pin = contract.image_ref(event)
    patch = contract.update_files(baseline, event, raw)
    assert "pineforge-4pass" not in pin
    assert "pineforge-4pass" not in patch[contract.RUNTIME].decode()
    assert "pineforge-4pass" not in patch[contract.DOCKERFILE].decode()
    assert pin == expected_pin
    apply(baseline, patch)
    assert contract.runtime_pin(baseline) == expected_pin
    assert f"ARG PINEFORGE_RELEASE_IMAGE={expected_pin}\n" in patch[contract.DOCKERFILE].decode()


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
    apply(baseline, contract.update_files(baseline, event, manifest))
    with pytest.raises(ValueError, match="duplicate, older or conflicting"):
        contract.update_files(baseline, event, manifest)
    assert contract.package_version(baseline) == "0.3.1"


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


MAIN_NETWORK_NODE = (
    "tests/test_consumer_receipt.py::test_bootstrap_recorded_hashes_match_live_pypi"
)
MAIN_NATIVE_JOBS = {
    "release-amd64": "Release (ubuntu-24.04)",
    "release-arm64": "Release (ubuntu-24.04-arm)",
    "release-macos": "Release (macos-15)",
    "asan-ubsan": "ASan + UBSan",
    "tsan": "TSan",
}


@pytest.fixture(params=[("landed-one", 80, 2), ("landed-two", 180, 3)])
def main_proof_inputs(request):
    """Decided schema as unit input; these are not observed hosted results."""
    label, run_id, attempt = request.param
    gate, run, jobs = green_gate()
    head = contract.sha256(label.encode())[:40]
    tree = contract.sha256(f"{label}:tree".encode())[:40]
    gate.update(
        run_id=run_id, attempt=attempt, head=head, tested_sha=head, tree=tree,
        event="push", pr=None, base=None,
    )
    run.update(
        id=run_id, run_attempt=attempt, head_sha=head, head_branch="main",
        event="push", path=".github/workflows/ci-post.yml",
    )
    # The job label is fixture data, not a new public API or product constant.
    # Once present, use the actual workflow's display name rather than pinning a choice here.
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci-post.yml").read_text(), Loader=yaml.BaseLoader
    )
    network_names = [
        job.get("name", job_id)
        for job_id, job in workflow["jobs"].items()
        if any(MAIN_NETWORK_NODE in step.get("run", "") for step in job.get("steps", []))
    ]
    network_name = network_names[0] if len(network_names) == 1 else "Live PyPI hashes"
    if not any(job["name"] == network_name for job in jobs):
        jobs.append({"name": network_name, "status": "completed", "conclusion": "success"})
    native_run = {
        **run, "id": run_id + 10, "run_attempt": attempt + 1,
        "path": ".github/workflows/native-feed-post.yml",
    }
    legs = {}
    for leg in MAIN_NATIVE_JOBS:
        names = ["feed-unit", "feed-export-unit", "feed-mock-export", "feed-mock"]
        if leg == "tsan":
            names.append("feed-tsan-seeded-race")
        junit = ElementTree.Element("testsuite", name=f"{label}:{leg}")
        for name in names:
            ElementTree.SubElement(junit, "testcase", name=name)
        legs[leg] = {
            "registered": names, "executed": list(names), "skipped": [],
            "failures": [], "errors": [],
            "junit_sha256": contract.sha256(ElementTree.tostring(junit)),
        }
    network_junit = ElementTree.Element("testsuite", name=f"{label}:network")
    ElementTree.SubElement(network_junit, "testcase", name=MAIN_NETWORK_NODE)
    gate["main_proofs"] = {
        "native": {
            "workflow_path": native_run["path"],
            "run": native_run,
            "jobs": [
                {"name": name, "status": "completed", "conclusion": "success"}
                for name in (*MAIN_NATIVE_JOBS.values(), "Native proof")
            ],
            "attestation": {
                "run_id": native_run["id"], "attempt": native_run["run_attempt"],
                "head": head, "tested_sha": head, "tree": tree, "legs": legs,
            },
        },
        "network": {
            "run_id": run_id, "attempt": attempt, "head": head, "tested_sha": head,
            "tree": tree, "node_id": MAIN_NETWORK_NODE,
            "registered": [MAIN_NETWORK_NODE], "executed": [MAIN_NETWORK_NODE],
            "skipped": [], "failures": [], "errors": [],
            "junit_sha256": contract.sha256(ElementTree.tostring(network_junit)),
        },
    }
    return gate, run, jobs


def test_main_complete_matching_proofs_positive_twin(main_proof_inputs) -> None:
    gate, run, jobs = main_proof_inputs
    # Acceptance alone is uncredited until the negative twins and I/O controls work.
    evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


@pytest.mark.parametrize("missing", ["main_proofs", "native", "network"])
def test_main_python_green_requires_both_proof_slots(main_proof_inputs, missing) -> None:
    gate, run, jobs = main_proof_inputs
    if missing == "main_proofs":
        gate.pop("main_proofs")
    else:
        gate["main_proofs"].pop(missing)
    with pytest.raises(ValueError):
        evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


@pytest.mark.parametrize("shape", ["null", "empty", "extra"])
def test_main_proof_object_has_exactly_the_decided_slots(main_proof_inputs, shape) -> None:
    gate, run, jobs = main_proof_inputs
    if shape == "null":
        gate["main_proofs"] = None
    elif shape == "empty":
        gate["main_proofs"] = {}
    else:
        gate["main_proofs"]["unobserved"] = {"conclusion": "success"}
    with pytest.raises(ValueError):
        evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


@pytest.mark.parametrize(
    "path,value",
    [
        (("workflow_path",), ".github/workflows/native-feed.yml"),
        (("run", "path"), ".github/workflows/ci-post.yml"),
        (("run", "event"), "pull_request"),
        (("run", "head_branch"), "topic"),
        (("run", "head_sha"), "f" * 40),
        (("run", "id"), 999),
        (("run", "run_attempt"), 1),
        (("run", "status"), "in_progress"),
        (("run", "conclusion"), "failure"),
        (("attestation", "run_id"), 999),
        (("attestation", "attempt"), 1),
        (("attestation", "head"), "e" * 40),
        (("attestation", "tested_sha"), "e" * 40),
        (("attestation", "tree"), "e" * 40),
    ],
)
def test_main_refuses_native_identity_or_run_drift(main_proof_inputs, path, value) -> None:
    gate, run, jobs = main_proof_inputs
    target = gate["main_proofs"]["native"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


@pytest.mark.parametrize("name", [*MAIN_NATIVE_JOBS.values(), "Native proof"])
@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "in_progress", "failure", "cancelled", "skipped"]
)
def test_main_refuses_incomplete_native_jobs(main_proof_inputs, name, fault) -> None:
    gate, run, jobs = main_proof_inputs
    native_jobs = gate["main_proofs"]["native"]["jobs"]
    job = next(row for row in native_jobs if row["name"] == name)
    if fault == "missing":
        native_jobs.remove(job)
    elif fault == "duplicate":
        native_jobs.append(copy.deepcopy(job))
    elif fault == "in_progress":
        job.update(status="in_progress", conclusion=None)
    else:
        job["conclusion"] = fault
    with pytest.raises(ValueError):
        evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


@pytest.mark.parametrize("leg", MAIN_NATIVE_JOBS)
@pytest.mark.parametrize(
    "fault",
    ["missing", "empty", "omitted", "duplicate", "skipped", "failures", "errors", "digest"],
)
def test_main_refuses_vacuous_or_incomplete_native_inventory(
    main_proof_inputs, leg, fault
) -> None:
    gate, run, jobs = main_proof_inputs
    legs = gate["main_proofs"]["native"]["attestation"]["legs"]
    row = legs[leg]
    if fault == "missing":
        legs.pop(leg)
    elif fault == "empty":
        row.update(registered=[], executed=[])
    elif fault == "omitted":
        row["executed"].pop()
    elif fault == "duplicate":
        row["registered"].append(row["registered"][0])
        row["executed"].append(row["executed"][0])
    elif fault == "digest":
        row["junit_sha256"] = ""
    else:
        row[fault] = [row["registered"][0]]
    with pytest.raises(ValueError):
        evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", 999), ("attempt", 1), ("head", "e" * 40),
        ("tested_sha", "e" * 40), ("tree", "e" * 40),
        ("node_id", "tests/test_consumer_receipt.py::other"),
        ("registered", []), ("executed", []),
        ("registered", ["tests/test_consumer_receipt.py::other"]),
        ("executed", ["tests/test_consumer_receipt.py::other"]),
        ("registered", [MAIN_NETWORK_NODE, MAIN_NETWORK_NODE]),
        ("executed", [MAIN_NETWORK_NODE, MAIN_NETWORK_NODE]),
        ("skipped", [MAIN_NETWORK_NODE]), ("failures", [MAIN_NETWORK_NODE]),
        ("errors", [MAIN_NETWORK_NODE]), ("junit_sha256", ""),
    ],
)
def test_main_disabled_or_wrong_network_row_is_non_full(
    main_proof_inputs, field, value
) -> None:
    gate, run, jobs = main_proof_inputs
    gate["main_proofs"]["network"][field] = value
    with pytest.raises(ValueError):
        evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


def test_gate_accepts_only_the_complete_current_attempt() -> None:
    gate, run, jobs = green_gate()
    evidence.validate_gate(gate, run, jobs, head="a" * 40, base="b" * 40, pr=7)


def test_landed_evidence_requires_the_post_workflow(main_proof_inputs) -> None:
    gate, run, jobs = main_proof_inputs
    evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)
    run["path"] = ".github/workflows/ci.yml"
    with pytest.raises(ValueError, match="unexpected CI workflow"):
        evidence.validate_gate(gate, run, jobs, head=gate["head"], base=None, pr=None)


def test_pr_evidence_cannot_use_the_post_workflow() -> None:
    gate, run, jobs = green_gate()
    run["path"] = ".github/workflows/ci-post.yml"
    with pytest.raises(ValueError, match="unexpected CI workflow"):
        evidence.validate_gate(gate, run, jobs, head="a" * 40, base="b" * 40, pr=7)


def test_recovery_queries_the_landed_post_workflow(baseline, monkeypatch) -> None:
    pr = {
        "number": 7,
        "merged_at": "2026-10-01T00:00:00Z",
        "merge_commit_sha": "a" * 40,
        "head": {"sha": "b" * 40},
        "base": {"ref": "main", "sha": "c" * 40},
        "body": automation.body({"version": "0.3.0", "base": "c" * 40}),
    }
    monkeypatch.setattr(publishing, "stored_receipt", lambda *args: None)

    def inventory(repo, endpoint):
        assert repo == "owner/data"
        assert endpoint == "pulls?state=closed&base=main&sort=updated&direction=desc"
        return [pr]

    def api(repo, endpoint):
        assert repo == "owner/data"
        assert endpoint == (
            "actions/workflows/ci-post.yml/runs?event=push&per_page=100&head_sha=" + "a" * 40
        )
        return {"workflow_runs": []}

    monkeypatch.setattr(publishing, "inventory", inventory)
    monkeypatch.setattr(publishing, "api", api)
    report = {"pr": None, "state": "queued"}
    publishing.recover_applied("owner/data", baseline, report)
    assert report["state"] == "testing"
    assert report["pr"]["merge"] == "a" * 40


class RecoveryPublishersBoundary(RuntimeError):
    """A validated recovery is about to read the later publisher-run inventory."""


@pytest.fixture
def recovery_main_transport(baseline, main_proof_inputs, monkeypatch):
    """Keep recovery, markers, local metadata, artifact parsing and gates real."""
    gate, python, jobs = main_proof_inputs
    native = gate["main_proofs"]["native"]
    python_proof = copy.deepcopy(gate)
    python_proof["main_proofs"] = {"network": gate["main_proofs"]["network"]}
    pr = {
        "number": 7, "merged_at": "2026-11-07T12:00:00Z",
        "merge_commit_sha": python["head_sha"],
        "head": {"sha": "b" * 40}, "base": {"ref": "main", "sha": "c" * 40},
        "body": automation.body({
            "version": contract.package_version(baseline), "base": "c" * 40,
        }),
    }
    state = {
        "root": baseline, "pr": pr, "python": python, "python_runs": [python],
        "python_proof": python_proof, "python_jobs": jobs,
        "native": native, "native_runs": [native["run"]],
        "native_artifact_missing": False, "reads": [], "writes": [],
        "parsed": [], "gate_calls": [], "validated": [], "local_checks": [],
        "publisher_reads": [], "local_attempts": [],
        "waiting_inputs": [], "waiting_validations": [],
    }

    def gh_bytes(route, **kwargs):
        assert not kwargs
        state["reads"].append(route)
        prefix = "repos/owner/data/"
        assert route.startswith(prefix)
        endpoint = route.removeprefix(prefix)
        path = endpoint.split("?", 1)[0]
        if path == "actions/workflows/native-feed-post.yml/runs":
            assert endpoint == (
                "actions/workflows/native-feed-post.yml/runs"
                f"?event=push&head_sha={python['head_sha']}&per_page=100"
            )
            return contract.json_bytes({"workflow_runs": state["native_runs"]})
        for run, observed_jobs, payload, artifact, filename in (
            (python, jobs, python_proof, "release-evidence", "evidence.json"),
            (native["run"], native["jobs"], native["attestation"],
             "native-evidence", "native-evidence.json"),
        ):
            run_id, attempt = run["id"], run["run_attempt"]
            if path == f"actions/runs/{run_id}":
                return contract.json_bytes(run)
            if path == f"actions/runs/{run_id}/attempts/{attempt}/jobs":
                return contract.json_bytes({"jobs": observed_jobs})
            if path == f"actions/runs/{run_id}/artifacts":
                missing = artifact == "native-evidence" and state["native_artifact_missing"]
                return contract.json_bytes({"artifacts": [] if missing else [{
                    "id": run_id * 100, "name": f"{artifact}-{attempt}", "expired": False,
                }]})
            if path == f"actions/artifacts/{run_id * 100}/zip":
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as zipped:
                    zipped.writestr(filename, contract.json_bytes(payload))
                return archive.getvalue()
        raise AssertionError(f"unexpected recovery proof transport: {route}")

    def api(repo, endpoint, method="GET", data=None):
        assert repo == "owner/data"
        if method != "GET":
            state["writes"].append((endpoint, method, data))
            raise AssertionError("recovery dispatched or mutated before its proof boundary")
        assert data is None
        state["reads"].append(endpoint)
        if endpoint == "releases?per_page=100&page=1":
            return []
        if endpoint == (
            "pulls?state=closed&base=main&sort=updated&direction=desc&per_page=100&page=1"
        ):
            return [copy.deepcopy(pr)]
        if endpoint == (
            "actions/workflows/ci-post.yml/runs?event=push&per_page=100&head_sha="
            + pr["merge_commit_sha"]
        ):
            return {"workflow_runs": copy.deepcopy(state["python_runs"])}
        if endpoint == "actions/workflows/release.yml/runs?per_page=100":
            state["publisher_reads"].append(endpoint)
            assert len(state["validated"]) == 1
            assert state["local_checks"] == [(baseline, None)]
            raise RecoveryPublishersBoundary("before publisher-run inventory read")
        raise AssertionError(f"unexpected recovery API transport: {endpoint}")

    real_parse = evidence.object_json
    real_gate = publishing.gate_for_run
    real_verify_local = publishing.verify_local
    real_validate_waiting_python = evidence._validate_waiting_python

    def parse(raw):
        result = real_parse(raw)
        state["parsed"].append(copy.deepcopy(result))
        return result

    def observe_gate(repo, run_id, **kwargs):
        state["gate_calls"].append((repo, run_id, kwargs))
        result = real_gate(repo, run_id, **kwargs)
        state["validated"].append(copy.deepcopy(result))
        return result

    def verify_local(root):
        state["local_attempts"].append(root)
        result = real_verify_local(root)
        state["local_checks"].append((root, copy.deepcopy(result)))
        return result

    def validate_waiting_python(proof, run, observed_jobs, **kwargs):
        state["waiting_inputs"].append(copy.deepcopy((proof, run, observed_jobs, kwargs)))
        result = real_validate_waiting_python(proof, run, observed_jobs, **kwargs)
        state["waiting_validations"].append(copy.deepcopy(proof))
        return result

    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(publishing, "api", api)
    monkeypatch.setattr(reporting, "api", api)
    monkeypatch.setattr(evidence, "gh_bytes", gh_bytes)
    monkeypatch.setattr(evidence, "object_json", parse)
    monkeypatch.setattr(evidence, "_validate_waiting_python", validate_waiting_python)
    monkeypatch.setattr(publishing, "gate_for_run", observe_gate)
    monkeypatch.setattr(publishing, "verify_local", verify_local)
    return state


@pytest.mark.parametrize("readiness", ["missing", "queued", "in_progress"])
def test_recovery_waits_for_missing_or_pending_native_proof(recovery_main_transport, readiness):
    state = recovery_main_transport
    if readiness == "missing":
        state["native_runs"] = []
    else:
        state["native"]["run"].update(status=readiness, conclusion=None)
        state["native_artifact_missing"] = True
        for job in state["native"]["jobs"]:
            job.update(status=readiness, conclusion=None)
    report = receipts.receipt(None)
    publishing.recover_applied("owner/data", state["root"], report)
    assert report["state"] == "testing"
    assert report["pr"] == automation.pr_identity({**state["pr"], "merged": True})
    assert state["gate_calls"] == [
        ("owner/data", state["python"]["id"], {"head": state["python"]["head_sha"]}),
    ]
    assert {"workflow_runs": state["native_runs"]} in state["parsed"]
    assert state["python_proof"] in state["parsed"], "the Python artifact must be parsed"
    assert state["validated"] == state["local_checks"] == state["publisher_reads"] == []
    reporting.incident("owner/data", report, "release-owner", "recover-applied")
    assert state["writes"] == []


@pytest.mark.parametrize("readiness", ["missing", "queued", "in_progress"])
@pytest.mark.parametrize(
    "fault,reason",
    [
        ("stale-attempt", "stale CI attempt"),
        ("network-non-object", "network: expected an object"),
        (
            "network-job-red",
            "required job 'Live PyPI hashes' missing, duplicated, failed, cancelled or skipped",
        ),
        ("wrong-tree", "network: wrong tree"),
        ("e2e-incomplete", "incomplete E2E evidence"),
    ],
)
def test_recovery_refuses_invalid_python_while_native_pending(
    recovery_main_transport, readiness, fault, reason
):
    state = recovery_main_transport
    python, native = state["python"], state["native"]
    current_python = copy.deepcopy(python)
    proof = state["python_proof"]
    if readiness == "missing":
        state["native_runs"] = []
    else:
        native["run"].update(status=readiness, conclusion=None)
        state["native_artifact_missing"] = True
        for job in native["jobs"]:
            job.update(status=readiness, conclusion=None)
    if fault == "stale-attempt":
        proof["attempt"] -= 1
    elif fault == "network-non-object":
        proof["main_proofs"]["network"] = []
    elif fault == "network-job-red":
        next(job for job in state["python_jobs"] if job["name"] == "Live PyPI hashes")[
            "conclusion"
        ] = "failure"
    elif fault == "wrong-tree":
        original_tree = proof["tree"]
        proof["tree"] = contract.sha256(b"another completed recovery evidence tree")[:40]
        assert proof["tree"] != original_tree
        assert proof["main_proofs"]["network"]["tree"] == original_tree
    else:
        assert fault == "e2e-incomplete"
        for suite in ("docker", "database"):
            proof["e2e"][suite].pop()
    assert python == current_python
    assert python["status"] == "completed" and python["conclusion"] == "success"
    report = receipts.receipt(None)
    before = copy.deepcopy(report)
    with pytest.raises(evidence.GateRejected) as rejected:
        publishing.recover_applied("owner/data", state["root"], report)
    assert type(rejected.value) is evidence.GateRejected
    assert str(rejected.value) == reason
    assert report["state"] == before["state"] == "pending"
    assert report == {
        **before, "pr": automation.pr_identity({**state["pr"], "merged": True}),
    }
    assert state["gate_calls"] == [
        ("owner/data", python["id"], {"head": python["head_sha"]}),
    ]
    assert {"workflow_runs": state["native_runs"]} in state["parsed"]
    if readiness != "missing":
        assert native["run"] in state["parsed"]
    assert proof in state["parsed"]
    assert {"jobs": state["python_jobs"]} in state["parsed"]
    assert state["waiting_inputs"] == [
        (proof, python, state["python_jobs"], {"head": python["head_sha"], "base": None}),
    ]
    assert state["waiting_validations"] == state["validated"] == []
    assert state["local_attempts"] == state["local_checks"] == state["publisher_reads"] == []
    assert state["writes"] == []
    native_id = native["run"]["id"]
    assert not any(
        f"/runs/{native_id}/attempts/" in route
        or f"/runs/{native_id}/artifacts" in route
        or f"/artifacts/{native_id * 100}/" in route
        for route in state["reads"]
    )


@pytest.mark.parametrize("readiness", ["missing", "queued", "in_progress"])
def test_recovery_waits_with_valid_python_while_native_pending_positive_twin(
    recovery_main_transport, readiness
):
    state = recovery_main_transport
    python, native = state["python"], state["native"]
    if readiness == "missing":
        state["native_runs"] = []
    else:
        native["run"].update(status=readiness, conclusion=None)
        state["native_artifact_missing"] = True
        for job in native["jobs"]:
            job.update(status=readiness, conclusion=None)
    assert python["status"] == "completed" and python["conclusion"] == "success"
    report = receipts.receipt(None)
    publishing.recover_applied("owner/data", state["root"], report)
    assert report["state"] == "testing"
    assert report["pr"] == automation.pr_identity({**state["pr"], "merged": True})
    assert state["gate_calls"] == [
        ("owner/data", python["id"], {"head": python["head_sha"]}),
    ]
    assert {"workflow_runs": state["native_runs"]} in state["parsed"]
    if readiness != "missing":
        assert native["run"] in state["parsed"]
    proof = state["python_proof"]
    assert proof in state["parsed"]
    assert {"jobs": state["python_jobs"]} in state["parsed"]
    assert state["waiting_inputs"] == [
        (proof, python, state["python_jobs"], {"head": python["head_sha"], "base": None}),
    ]
    assert state["waiting_validations"] == [proof]
    assert state["validated"] == state["local_attempts"] == state["local_checks"] == []
    assert state["publisher_reads"] == state["writes"] == []
    reporting.incident("owner/data", report, "release-owner", "recover-applied")
    assert state["writes"] == []


def test_recovery_without_python_observation_keeps_testing(recovery_main_transport):
    state = recovery_main_transport
    state["python_runs"] = []
    report = receipts.receipt(None)
    publishing.recover_applied("owner/data", state["root"], report)
    assert report["state"] == "testing" and report["pr"]["merge"] == state["python"]["head_sha"]
    assert state["gate_calls"] == state["parsed"] == state["writes"] == []
    assert state["publisher_reads"] == state["local_checks"] == []
    assert not any("native-feed-post.yml" in route for route in state["reads"])


@pytest.mark.parametrize(
    "fault",
    ["native-failure", "native-cancelled", "native-attempt", "native-malformed",
     "native-missing-artifact", "python-failure", "python-cancelled"],
)
def test_recovery_completed_red_or_malformed_proof_is_not_waiting(
    recovery_main_transport, fault
):
    state = recovery_main_transport
    native = state["native"]
    if fault in {"native-failure", "native-cancelled"}:
        native["run"]["conclusion"] = fault.removeprefix("native-")
        reason = "newest native proof has not completed successfully"
    elif fault in {"python-failure", "python-cancelled"}:
        state["python"]["conclusion"] = fault.removeprefix("python-")
        reason = "CI has not completed successfully"
    elif fault == "native-attempt":
        native["attestation"]["attempt"] -= 1
        reason = "native: wrong attempt"
    elif fault == "native-malformed":
        native["attestation"]["legs"] = []
        reason = "native legs: expected an object"
    else:
        state["native_artifact_missing"] = True
        reason = f"missing unique native-evidence-{native['run']['run_attempt']} artifact"
    report = receipts.receipt(None)
    with pytest.raises(evidence.GateRejected) as rejected:
        publishing.recover_applied("owner/data", state["root"], report)
    assert str(rejected.value) == reason
    assert report["state"] == "pending", "a completed refusal must not become testing"
    assert state["gate_calls"] and state["parsed"]
    assert state["validated"] == state["local_checks"] == state["publisher_reads"] == []
    assert state["writes"] == []


def test_recovery_complete_pair_reaches_publisher_inventory_boundary(recovery_main_transport):
    state = recovery_main_transport
    report = receipts.receipt(None)
    with pytest.raises(RecoveryPublishersBoundary, match="before publisher-run inventory read"):
        publishing.recover_applied("owner/data", state["root"], report)
    observed = state["validated"][0]
    assert observed["main_proofs"]["native"] == state["native"]
    assert observed["main_proofs"]["network"] == state["python_proof"]["main_proofs"]["network"]
    assert state["native"]["attestation"] in state["parsed"]
    assert state["python_proof"] in state["parsed"]
    assert state["local_checks"] == [(state["root"], None)]
    assert len(state["publisher_reads"]) == 1 and state["writes"] == []
    # Partial lifecycle only: publisher selection, dispatch, publication and replay are unproved.


def test_recovery_complete_pair_still_reads_local_metadata(recovery_main_transport):
    state = recovery_main_transport
    (state["root"] / contract.DOCKERFILE).write_text(
        "ARG PINEFORGE_RELEASE_IMAGE=wrong-runtime-pin\n"
    )
    report = receipts.receipt(None)
    with pytest.raises(ValueError) as rejected:
        publishing.recover_applied("owner/data", state["root"], report)
    assert str(rejected.value) == "runtime/server pin mismatch"
    assert len(state["validated"]) == 1
    assert state["local_checks"] == state["publisher_reads"] == state["writes"] == []


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


@pytest.fixture
def evidence_inputs(tmp_path: Path, monkeypatch):
    from release_chain import main_proofs

    root = tmp_path / "repository"
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes((ROOT / ".github" / "workflows" / "ci.yml").read_bytes())
    (workflow.parent / "ci-post.yml").write_bytes(
        (ROOT / ".github/workflows/ci-post.yml").read_bytes()
    )
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", ".github/workflows/ci.yml", ".github/workflows/ci-post.yml"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release evidence test",
            "-c",
            "user.email=release-evidence@example.invalid",
            "commit",
            "-m",
            "Test release evidence",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / "pineforge_data-0.0.0-py3-none-any.whl").write_bytes(b"wheel")
    (directory / "pineforge_data-0.0.0.tar.gz").write_bytes(b"sdist")
    event = tmp_path / "event.json"
    event.write_text("{}")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "pineforge-4pass/pineforge-data")
    monkeypatch.setenv("GITHUB_RUN_ID", "70")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "pineforge-4pass/pineforge-data/.github/workflows/ci-post.yml@refs/heads/main",
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    monkeypatch.setenv("GITHUB_SHA", head)
    # Authored unit report bytes, outside the exact two-distribution inventory.
    network = tmp_path / "network"
    network.mkdir()
    suite = ElementTree.Element(
        "testsuite", tests="1", failures="0", errors="0", skipped="0"
    )
    ElementTree.SubElement(
        suite,
        "testcase",
        classname="tests.test_consumer_receipt",
        name="test_bootstrap_recorded_hashes_match_live_pypi",
    )
    (network / "network.xml").write_bytes(ElementTree.tostring(suite))
    (network / "network-proof.json").write_bytes(
        contract.json_bytes(main_proofs.network_proof(root, network))
    )
    needs = {name: {"result": "success"} for name in evidence.NEEDS | {"network-proof"}}
    jobs = [{"name": name} for name in (*evidence.JOBS, "Live PyPI hashes")]

    def attempt_jobs(repo, run_id, attempt):
        assert (repo, run_id, attempt) == ("pineforge-4pass/pineforge-data", 70, 2)
        return jobs

    monkeypatch.setattr(evidence, "attempt_jobs", attempt_jobs)
    return root, directory, network, needs, jobs


@pytest.mark.parametrize("stale_statuses", [False, True], ids=["names-only", "stale-statuses"])
def test_create_evidence_accepts_api_inventory_without_waiting(
    evidence_inputs, monkeypatch, stale_statuses
) -> None:
    root, directory, network, needs, jobs = evidence_inputs
    if stale_statuses:
        for index, job in enumerate(jobs):
            job["status"] = "in_progress" if index % 2 == 0 else "queued"
            job["conclusion"] = None if index % 2 == 0 else "failure"

    def reject_sleep(_seconds: float) -> None:
        raise AssertionError("sleep")

    monkeypatch.setattr(time, "sleep", reject_sleep)
    result = evidence.create_evidence(root, directory, needs, network=network)
    assert result["jobs"] == dict.fromkeys(evidence.JOBS, "success")
    assert result["event"] == "push" and result["pr"] is None and result["base"] is None
    assert result["head"] == result["tested_sha"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    assert result["tree"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
    ).strip()
    assert result["main_proofs"] == {
        "network": contract.object_json((network / "network-proof.json").read_bytes())
    }
    assert result["main_proofs"]["network"]["junit_sha256"] == contract.sha256(
        (network / "network.xml").read_bytes()
    )
    assert result["distributions"] == {
        "pineforge_data-0.0.0-py3-none-any.whl": contract.sha256(b"wheel"),
        "pineforge_data-0.0.0.tar.gz": contract.sha256(b"sdist"),
    }
    assert network.parent == directory.parent and network != directory
    assert not hasattr(evidence, "JOBS_CLOSE_TIMEOUT")
    assert not hasattr(evidence, "closed_jobs")


@pytest.mark.parametrize("job_id", sorted(evidence.NEEDS | {"network-proof"}))
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_create_evidence_rejects_unsuccessful_needs(evidence_inputs, job_id, result) -> None:
    root, directory, network, needs, _jobs = evidence_inputs
    needs[job_id]["result"] = result
    with pytest.raises(ValueError, match="a required CI job failed"):
        evidence.create_evidence(root, directory, needs, network=network)


@pytest.mark.parametrize("job_id", ["test", "network-proof"])
def test_create_evidence_rejects_missing_need(evidence_inputs, job_id) -> None:
    root, directory, network, needs, _jobs = evidence_inputs
    needs.pop(job_id)
    with pytest.raises(ValueError, match="required CI job inventory changed"):
        evidence.create_evidence(root, directory, needs, network=network)


@pytest.mark.parametrize("job_name", [*evidence.JOBS, "Live PyPI hashes"])
@pytest.mark.parametrize("defect", ["missing", "duplicated"])
def test_create_evidence_rejects_missing_or_duplicated_api_names(
    evidence_inputs, job_name, defect
) -> None:
    root, directory, network, needs, jobs = evidence_inputs
    if defect == "missing":
        jobs[:] = [job for job in jobs if job["name"] != job_name]
    else:
        jobs.append({"name": job_name})
    with pytest.raises(ValueError, match="missing or duplicated"):
        evidence.create_evidence(root, directory, needs, network=network)


def test_create_evidence_main_requires_network_input(evidence_inputs) -> None:
    root, directory, _network, needs, _jobs = evidence_inputs
    with pytest.raises(ValueError, match="main requires the actual live-PyPI report"):
        evidence.create_evidence(root, directory, needs, network=None)


@pytest.mark.parametrize("fault", ["raw-report", "recorded-digest", "recorded-attempt"])
def test_create_evidence_consumes_network_report_bytes(evidence_inputs, fault) -> None:
    root, directory, network, needs, _jobs = evidence_inputs
    if fault == "raw-report":
        report = network / "network.xml"
        report.write_bytes(report.read_bytes() + b"\n")
    else:
        path = network / "network-proof.json"
        proof = contract.object_json(path.read_bytes())
        if fault == "recorded-digest":
            proof["junit_sha256"] = contract.sha256(b"different raw report")
        else:
            proof["attempt"] -= 1
        path.write_bytes(contract.json_bytes(proof))
    with pytest.raises(ValueError, match="network report bytes or producer identity changed"):
        evidence.create_evidence(root, directory, needs, network=network)


@pytest.mark.parametrize(
    "workflow",
    [
        (ROOT / ".github" / "workflows" / "ci.yml")
        .read_text()
        .replace("  test:\n", "  test:\n    continue-on-error: true\n", 1),
        (ROOT / ".github" / "workflows" / "ci.yml").read_text() + "# continue-on-error: true\n",
        (ROOT / ".github" / "workflows" / "ci.yml")
        .read_text()
        .replace("  test:\n", '  test:\n    "\\u0063ontinue-on-error": true\n', 1),
        (ROOT / ".github" / "workflows" / "ci.yml").read_text() + "\n",
    ],
    ids=["enabled", "comment", "escaped-key", "whitespace-only"],
)
def test_create_evidence_rejects_continue_on_error(evidence_inputs, workflow) -> None:
    root, directory, network, needs, _jobs = evidence_inputs
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow)
    with pytest.raises(ValueError, match=r"ci\.yml changed or uses continue-on-error"):
        evidence.create_evidence(root, directory, needs, network=network)


@pytest.mark.parametrize(
    "filename,pin",
    [
        ("ci.yml", "REVIEWED_CI_WORKFLOW_SHA256"),
        ("ci-post.yml", "REVIEWED_CI_POST_WORKFLOW_SHA256"),
    ],
)
def test_reviewed_ci_workflow_pin_matches_the_file(filename: str, pin: str) -> None:
    assert (
        evidence.sha256((ROOT / ".github/workflows" / filename).read_bytes())
        == getattr(evidence, pin)
    ), (
        f"{filename} changed: update {pin} after reviewing that "
        'needs.<job>.result == "success" still proves every matrix leg succeeded'
    )


def test_create_evidence_rejects_missing_workflow(evidence_inputs) -> None:
    root, directory, network, needs, _jobs = evidence_inputs
    (root / ".github" / "workflows" / "ci.yml").unlink()
    with pytest.raises(ValueError, match=r"cannot read ci\.yml"):
        evidence.create_evidence(root, directory, needs, network=network)


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


@pytest.fixture
def candidate_ci(monkeypatch):
    pr = {
        "number": 7,
        "body": (
            '<!-- pineforge-data-release '
            '{"base": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"} -->'
        ),
        "head": {
            "sha": "a" * 40,
            "ref": "bot/pineforge-release/test",
            "repo": {"full_name": "owner/data"},
        },
        "merged_at": "2020-01-01T00:05:00Z",
    }
    runs = [
        {
            "id": 70,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "event": "pull_request",
            "pull_requests": [],
            "head_branch": "bot/pineforge-release/test",
            "head_repository": {"full_name": "owner/data"},
            "created_at": "2020-01-01T00:03:00Z",
        }
    ]
    jobs = [
        {"completed_at": "2020-01-01T00:04:00Z"},
        {"completed_at": "2020-01-01T00:04:30Z"},
    ]
    checked = []

    def inventory(repo, endpoint, key):
        assert repo == "owner/data"
        if key == "workflow_runs":
            assert endpoint == (
                "actions/workflows/ci.yml/runs?event=pull_request&head_sha=" + "a" * 40
            )
            return runs
        assert key == "jobs"
        run = next(row for row in runs if row["id"] == checked[-1])
        assert endpoint == f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs"
        return jobs

    def gate_for_run(repo, run_id, *, head, base, pr):
        assert repo == "owner/data"
        assert head == "a" * 40 and base == "b" * 40 and pr == 7
        checked.append(run_id)
        return {"run_id": run_id}

    monkeypatch.setattr(publishing, "inventory", inventory)
    monkeypatch.setattr(publishing, "gate_for_run", gate_for_run)
    return pr, runs, jobs, checked


@pytest.mark.parametrize(
    "run_changes,pr_changes",
    [
        pytest.param({"head_sha": "c" * 40}, {}, id="other-head"),
        pytest.param({"created_at": "2020-01-01T00:06:00Z"}, {}, id="created-after-merge"),
        pytest.param({"event": "push"}, {}, id="push-event"),
        pytest.param({"head_branch": "other-branch"}, {}, id="other-branch"),
        pytest.param(
            {"head_repository": {"full_name": "other-owner/data"}},
            {},
            id="fork-repository",
        ),
        pytest.param(
            {"pull_requests": [{"number": 8}], "head_branch": "other-branch"},
            {},
            id="other-pr-and-branch",
        ),
        pytest.param({}, {"merged_at": None}, id="unmerged-pr"),
        pytest.param({"pull_requests": [{"number": 8}]}, {}, id="other-pr-same-branch"),
        pytest.param(
            {"head_sha": "c" * 40, "pull_requests": [{"number": 7}]},
            {},
            id="associated-other-head",
        ),
        pytest.param(
            {"event": "push", "pull_requests": [{"number": 7}]},
            {},
            id="associated-push-event",
        ),
        pytest.param({"pull_requests": None}, {}, id="null-associations"),
        pytest.param({"pull_requests": [{}]}, {}, id="missing-association-number"),
        pytest.param({"created_at": "2020-01-01T00:05:00Z"}, {}, id="created-at-merge"),
        pytest.param(
            {"created_at": "2020-01-01T01:05:00+01:00"},
            {},
            id="created-at-merge-with-offset",
        ),
        pytest.param(
            {"created_at": "2020-01-01T00:04:00-01:00"},
            {},
            id="created-after-merge-with-offset",
        ),
        pytest.param({"created_at": "invalid"}, {}, id="invalid-created-time"),
        pytest.param({}, {"merged_at": "invalid"}, id="invalid-merge-time"),
        pytest.param({"created_at": "2020-01-01T00:03:00"}, {}, id="naive-created-time"),
        pytest.param({}, {"merged_at": "2020-01-01T00:05:00"}, id="naive-merge-time"),
    ],
)
def test_candidate_gate_rejects_unrelated_or_post_merge_ci(
    candidate_ci, run_changes, pr_changes
) -> None:
    pr, runs, _jobs, checked = candidate_ci
    runs[0].update(run_changes)
    pr.update(pr_changes)
    assert not publishing.run_belongs_to_pr(runs[0], pr)
    with pytest.raises(ValueError, match=r"^pre-merge CI evidence missing$"):
        publishing.candidate_gate("owner/data", pr)
    assert checked == []


@pytest.mark.parametrize(
    "target,path",
    [
        ("run", ("head_sha",)),
        ("run", ("event",)),
        ("run", ("pull_requests",)),
        ("run", ("head_branch",)),
        ("run", ("head_repository",)),
        ("run", ("head_repository", "full_name")),
        ("run", ("created_at",)),
        ("pr", ("number",)),
        ("pr", ("head",)),
        ("pr", ("head", "sha")),
        ("pr", ("head", "ref")),
        ("pr", ("head", "repo")),
        ("pr", ("head", "repo", "full_name")),
        ("pr", ("merged_at",)),
    ],
)
def test_candidate_gate_rejects_missing_ci_identity(candidate_ci, target, path) -> None:
    pr, runs, _jobs, checked = candidate_ci
    value = runs[0] if target == "run" else pr
    for key in path[:-1]:
        value = value[key]
    del value[path[-1]]
    assert not publishing.run_belongs_to_pr(runs[0], pr)
    with pytest.raises(ValueError, match=r"^pre-merge CI evidence missing$"):
        publishing.candidate_gate("owner/data", pr)
    assert checked == []


@pytest.mark.parametrize("run_key,pr_key", [("head_sha", "sha"), ("head_branch", "ref")])
def test_candidate_gate_rejects_null_head_identity(candidate_ci, run_key, pr_key) -> None:
    pr, runs, _jobs, checked = candidate_ci
    runs[0][run_key] = None
    pr["head"][pr_key] = None
    assert not publishing.run_belongs_to_pr(runs[0], pr)
    with pytest.raises(ValueError, match=r"^pre-merge CI evidence missing$"):
        publishing.candidate_gate("owner/data", pr)
    assert checked == []


@pytest.mark.parametrize("full_name", [None, "", 123])
def test_candidate_gate_rejects_invalid_head_repository_name(candidate_ci, full_name) -> None:
    pr, runs, _jobs, checked = candidate_ci
    runs[0]["head_repository"]["full_name"] = full_name
    pr["head"]["repo"]["full_name"] = full_name
    assert not publishing.run_belongs_to_pr(runs[0], pr)
    with pytest.raises(ValueError, match=r"^pre-merge CI evidence missing$"):
        publishing.candidate_gate("owner/data", pr)
    assert checked == []


@pytest.mark.parametrize(
    "created_at,merged_at",
    [
        ("2020-01-01T00:03:00Z", "2020-01-01T00:05:00Z"),
        ("2020-01-01T01:03:00.500000+01:00", "2020-01-01T00:05:00Z"),
        ("2020-01-01T00:03:00Z", "2020-01-01T01:05:00+01:00"),
    ],
)
def test_candidate_gate_recovers_merged_pr_with_empty_associations(
    candidate_ci, created_at, merged_at
) -> None:
    pr, runs, _jobs, checked = candidate_ci
    runs[0]["created_at"] = created_at
    pr["merged_at"] = merged_at
    assert publishing.run_belongs_to_pr(runs[0], pr)
    assert publishing.candidate_gate("owner/data", pr) == {"run_id": 70}
    assert checked == [70]


def test_candidate_gate_accepts_explicit_association_for_open_pr(candidate_ci) -> None:
    pr, runs, _jobs, checked = candidate_ci
    pr["merged_at"] = None
    runs[0]["pull_requests"] = [{"number": 8}, {"number": 7}]
    del runs[0]["head_branch"]
    del runs[0]["created_at"]
    del pr["head"]["ref"]
    assert publishing.run_belongs_to_pr(runs[0], pr)
    assert publishing.candidate_gate("owner/data", pr) == {"run_id": 70}
    assert checked == [70]


@pytest.mark.parametrize("latest_first", [False, True])
def test_candidate_gate_chooses_highest_matching_run_id(candidate_ci, latest_first) -> None:
    pr, runs, _jobs, checked = candidate_ci
    runs.append({**runs[0], "id": 71, "run_attempt": 1})
    if latest_first:
        runs.reverse()
    assert all(publishing.run_belongs_to_pr(run, pr) for run in runs)
    assert publishing.candidate_gate("owner/data", pr) == {"run_id": 71}
    assert checked == [71]


@pytest.mark.parametrize(
    "run_changes",
    [
        pytest.param({"created_at": "2020-01-01T00:06:00Z"}, id="created-after-merge"),
        pytest.param({"head_sha": "c" * 40}, id="other-head"),
        pytest.param(
            {"head_repository": {"full_name": "other-owner/data"}},
            id="fork-repository",
        ),
    ],
)
@pytest.mark.parametrize("latest_first", [False, True])
def test_candidate_gate_ignores_higher_id_unrelated_run(
    candidate_ci, run_changes, latest_first
) -> None:
    pr, runs, _jobs, checked = candidate_ci
    assert publishing.run_belongs_to_pr(runs[0], pr)
    unrelated = {**runs[0], "id": 71, "run_attempt": 1, **run_changes}
    assert not publishing.run_belongs_to_pr(unrelated, pr)
    runs.append(unrelated)
    if latest_first:
        runs.reverse()
    assert publishing.candidate_gate("owner/data", pr) == {"run_id": 70}
    assert checked == [70]


@pytest.mark.parametrize("completed_at", [None, "2020-01-01T00:06:00Z"])
def test_candidate_gate_still_requires_every_job_to_finish_before_merge(
    candidate_ci, completed_at
) -> None:
    pr, runs, jobs, checked = candidate_ci
    jobs[-1]["completed_at"] = completed_at
    assert publishing.run_belongs_to_pr(runs[0], pr)
    with pytest.raises(ValueError, match="candidate CI/E2E did not finish before merge"):
        publishing.candidate_gate("owner/data", pr)
    assert checked == [70]


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
        "version": "0.3.1",
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
        for name in (contract.RUNTIME, contract.DOCKERFILE, "pyproject.toml"):
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
    bundle = tmp_path / "bundle"
    (bundle / "source").mkdir(parents=True)
    (bundle / "source/pyproject.toml").write_text('[project]\nversion = "0.3.1"\n')
    dist = bundle / "dist"
    dist.mkdir()
    (dist / "pineforge_data-0.3.1-py3-none-any.whl").write_bytes(b"tested wheel")
    (dist / "pineforge_data-0.3.1.tar.gz").write_bytes(b"tested sdist")
    hashes = evidence.distribution_hashes(dist)
    report = automation.receipt(None, "verify")
    (bundle / "evidence.json").write_bytes(contract.json_bytes({"distributions": hashes}))
    report["state"] = "deploying"
    monkeypatch.setattr(publishing, "pypi_files", lambda version: dict.fromkeys(hashes, "0" * 64))
    with pytest.raises(ValueError, match="digests disagree"):
        publishing.verify_publication("owner/data", bundle, report)
    assert report["state"] == "deploying"
    assert report["deployed"] is None


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
                for name in (contract.RUNTIME, contract.DOCKERFILE, "pyproject.toml")
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
                for name in (contract.RUNTIME, contract.DOCKERFILE, "pyproject.toml")
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
    saved["deployed"] = receipts.served(event, "a" * 40, "0.3.1", {"wheel": "a", "sdist": "b"})
    monkeypatch.setattr(publishing, "stored_receipt", lambda *args: saved)
    monkeypatch.setattr(publishing, "verified_package", lambda *args: saved["deployed"])
    monkeypatch.setattr(publishing, "api", lambda *args: pytest.fail("replayed publication"))
    report = automation.receipt(event, "reconcile")
    publishing.recover_applied("owner/data", baseline, report)
    assert report["state"] == "verified"
    assert report["deployed"] == saved["deployed"]


@pytest.fixture
def saved_recovery_transport(ready, baseline, monkeypatch):
    """Real saved-package/lock/OCI verification over immutable fixture bytes."""
    event, manifest = ready
    tagged = baseline / "tagged-source"
    baseline_files = {
        name: (baseline / name).read_bytes()
        for name in (contract.RUNTIME, contract.DOCKERFILE, "pyproject.toml")
    }
    apply(tagged, baseline_files)
    apply(tagged, contract.update_files(tagged, event, manifest))
    source_files = {name: (tagged / name).read_bytes() for name in contract.UPDATE_FILES}
    # The installed original pin intentionally has no lock or manifest.
    # Its package version identifies the stored receipt without claiming a new pin.
    (baseline / "pyproject.toml").write_bytes(source_files["pyproject.toml"])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for name in (contract.LOCK, contract.MANIFEST, contract.RUNTIME):
            zipped.writestr(name.removeprefix("src/"), source_files[name])
    downloads = {
        "pineforge_data-0.3.1-py3-none-any.whl": archive.getvalue(),
        "pineforge_data-0.3.1.tar.gz": b"retained source distribution fixture",
    }
    hashes = {name: contract.sha256(raw) for name, raw in downloads.items()}
    metadata = {"urls": [
        {
            "filename": name, "digests": {"sha256": digest},
            "url": f"https://files.pythonhosted.org/{name}",
        }
        for name, digest in hashes.items()
    ]}
    saved = contract.object_json((FIXTURES / "sample-release-receipt.json").read_bytes())
    source = saved["pr"]["merge"]
    saved.update(
        event_id=event["event_id"], release_version=event["release_version"],
        deployed=receipts.served(event, source, "0.3.1", hashes),
    )
    assets = [
        {"id": index, "name": name}
        for index, name in enumerate([*downloads, "release-receipt.json"], 1)
    ]
    release = {"tag_name": "v0.3.1", "assets": assets}
    state = {
        "root": baseline, "baseline_files": baseline_files,
        "source_files": source_files, "saved": saved,
        "event": event, "downloads": downloads, "reads": [], "git": [],
        "steps": [], "package_calls": [], "local_reads": [], "local_results": [],
        "processes": [], "snapshot_files": [],
    }
    registry_fixture(monkeypatch, event)

    def api(repo, endpoint, method="GET", data=None):
        state["reads"].append(endpoint)
        assert repo == "owner/data" and method == "GET" and data is None
        if endpoint == "releases?per_page=100&page=1":
            return [copy.deepcopy(release)]
        assert endpoint == "releases/tags/v0.3.1"
        return copy.deepcopy(release)

    def gh_bytes(endpoint, *, accept):
        state["reads"].append(endpoint)
        assert accept == "application/octet-stream"
        asset = next(
            item for item in assets
            if endpoint == f"repos/owner/data/releases/assets/{item['id']}"
        )
        if asset["name"] == "release-receipt.json":
            return receipts.canonical(saved)
        return downloads[asset["name"]]

    def fetch(url, headers=None):
        state["reads"].append(url)
        assert headers is None
        if url == "https://pypi.org/pypi/pineforge-data/0.3.1/json":
            return contract.json_bytes(metadata)
        assert url.startswith("https://files.pythonhosted.org/")
        return downloads[url.rsplit("/", 1)[1]]

    def git(*args):
        state["git"].append(args)
        if args == ("fetch", "origin", "refs/tags/v0.3.1"):
            return ""
        assert args == ("rev-parse", "v0.3.1^{commit}")
        return source

    def snapshot(revision, root):
        assert revision == source
        paths = [root / name for name in sorted(source_files)]
        for path in paths:
            if (
                path.name.endswith(".env")
                or not path.resolve().is_relative_to(baseline.resolve())
            ):
                raise AssertionError(f"refused saved publication fixture file: {path}")
        state["snapshot_files"].append([path.relative_to(root).as_posix() for path in paths])
        apply(root, source_files)

    real_verify_local = publishing.verify_local
    real_verified_package = publishing.verified_package
    real_temporary_directory = publishing.tempfile.TemporaryDirectory

    def observe_local(root):
        state["steps"].append(("verify-local", root))
        state["local_reads"].append(root)
        result = real_verify_local(root)
        state["local_results"].append(copy.deepcopy(result))
        return result

    def observe_package(repo, version, stored):
        state["package_calls"].append((repo, version, copy.deepcopy(stored)))
        state["steps"].append(("verify-package-start", version))
        result = real_verified_package(repo, version, stored)
        state["steps"].append(("verify-package-complete", version))
        return result

    def reject_process(command, **kwargs):
        state["processes"].append(command)
        raise AssertionError("saved receipt replay must not dispatch or publish")

    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(publishing, "api", api)
    monkeypatch.setattr(publishing, "gh_bytes", gh_bytes)
    monkeypatch.setattr(publishing, "fetch", fetch)
    monkeypatch.setattr(publishing, "git", git)
    monkeypatch.setattr(publishing, "snapshot", snapshot)
    # These observers return only the result of the complete original validators.
    monkeypatch.setattr(publishing, "verify_local", observe_local)
    monkeypatch.setattr(publishing, "verified_package", observe_package)
    monkeypatch.setattr(publishing.subprocess, "run", reject_process)
    monkeypatch.setattr(
        publishing.tempfile, "TemporaryDirectory",
        lambda: real_temporary_directory(dir=baseline),
    )
    return state


def assert_saved_recovery_verification_order(state):
    assert state["package_calls"] == [("owner/data", "0.3.1", state["saved"])]
    assert len(state["local_reads"]) == len(state["local_results"]) == 2
    assert state["local_reads"][0] != state["root"]
    assert state["local_reads"][1] == state["root"]
    assert state["local_results"][0]["event"] == state["event"]
    assert state["steps"][-2:] == [
        ("verify-package-complete", "0.3.1"), ("verify-local", state["root"]),
    ]
    assert state["git"] == [
        ("fetch", "origin", "refs/tags/v0.3.1"), ("rev-parse", "v0.3.1^{commit}"),
    ]
    assert state["reads"].count("https://pypi.org/pypi/pineforge-data/0.3.1/json") == 2
    assert state["snapshot_files"] == [sorted(contract.UPDATE_FILES)]
    assert state["processes"] == []


def test_recovery_saved_receipt_refuses_missing_installed_lock(saved_recovery_transport):
    state = saved_recovery_transport
    assert contract.verify_local(state["root"]) is None
    report = receipts.receipt(state["event"], "reconcile")
    before = copy.deepcopy(report)
    with pytest.raises(ValueError, match=r"^stored publication source lock missing$"):
        publishing.recover_applied("owner/data", state["root"], report)
    assert_saved_recovery_verification_order(state)
    assert state["local_results"][1] is None
    assert report == before


def test_recovery_saved_receipt_replays_after_real_verification(saved_recovery_transport):
    state = saved_recovery_transport
    apply(state["root"], state["source_files"])
    report = receipts.receipt(state["event"], "reconcile")
    publishing.recover_applied("owner/data", state["root"], report)
    assert_saved_recovery_verification_order(state)
    assert state["local_results"][1] == state["local_results"][0]
    assert report == state["saved"]


def test_recovery_saved_receipt_keeps_installed_image_mismatch_refusal(saved_recovery_transport):
    state = saved_recovery_transport
    manifest = contract.object_json(state["source_files"][contract.MANIFEST])
    manifest["image"]["digest"] = "sha256:" + "0" * 64
    raw = contract.json_bytes(manifest)
    installed_event = contract.event_from_manifest(raw)
    apply(state["root"], state["baseline_files"])
    apply(state["root"], contract.update_files(state["root"], installed_event, raw))
    report = receipts.receipt(state["event"], "reconcile")
    before = copy.deepcopy(report)
    with pytest.raises(ValueError, match=r"^stored publication does not match installed defaults$"):
        publishing.recover_applied("owner/data", state["root"], report)
    assert_saved_recovery_verification_order(state)
    assert state["local_results"][1]["event"] == installed_event
    assert report == before


def test_recovery_keeps_stored_identity_priority_before_local_lock(saved_recovery_transport):
    state = saved_recovery_transport
    state["saved"]["event_id"] = "sha256:" + "0" * 64
    report = receipts.receipt(state["event"], "reconcile")
    before = copy.deepcopy(report)
    with pytest.raises(ValueError, match=r"^stored publication identity differs$"):
        publishing.recover_applied("owner/data", state["root"], report)
    assert state["package_calls"] == state["local_reads"] == state["git"] == []
    assert state["processes"] == []
    assert report == before


def test_recovery_verifies_package_before_requiring_local_lock(saved_recovery_transport):
    state = saved_recovery_transport
    name = "pineforge_data-0.3.1-py3-none-any.whl"
    state["downloads"][name] += b"changed"
    report = receipts.receipt(state["event"], "reconcile")
    before = copy.deepcopy(report)
    with pytest.raises(ValueError, match=r"^PyPI bytes differ$"):
        publishing.recover_applied("owner/data", state["root"], report)
    assert len(state["package_calls"]) == 1
    assert state["local_reads"] == state["git"] == state["processes"] == []
    assert report == before


def assert_pull_request_filter_policy(workflow: dict[str, Any], name: str) -> None:
    assert "pull_request" in workflow["on"]
    assert "pull_request_target" not in workflow["on"]
    pull_request = workflow["on"]["pull_request"]
    if name == "native-feed.yml":
        assert isinstance(pull_request, dict)
        assert pull_request.get("paths") == [
            "native/feed/**",
            ".github/workflows/native-feed.yml",
            ".github/workflows/native-feed-post.yml",
        ]
    else:
        assert name in {"ci.yml", "docs.yml"}
        assert pull_request in (None, "") or isinstance(pull_request, dict)
        if isinstance(pull_request, dict):
            filters = {"paths", "paths-ignore", "branches", "branches-ignore"}
            assert not filters & pull_request.keys()


@pytest.mark.parametrize("name", ["ci.yml", "docs.yml", "native-feed.yml"])
def test_pull_request_trigger_filters_match_authz_roles(name: str) -> None:
    workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
    assert_pull_request_filter_policy(workflow, name)


@pytest.mark.parametrize("name", ["ci.yml", "docs.yml", "native-feed.yml"])
def test_pr_tier_triggers_reject_pull_request_target(name: str) -> None:
    original = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
    assert_pull_request_filter_policy(original, name)
    workflow = copy.deepcopy(original)
    workflow["on"]["pull_request_target"] = ""
    assert workflow != original
    with pytest.raises(AssertionError):
        assert_pull_request_filter_policy(workflow, name)


@pytest.mark.parametrize("name", ["ci.yml", "docs.yml"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("paths", ["src/**"]),
        ("paths-ignore", ["docs/**"]),
        ("branches", ["main"]),
        ("branches-ignore", ["release/**"]),
    ],
)
def test_required_authz_rejects_pull_request_filters(
    name: str, key: str, value: list[str]
) -> None:
    original = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
    assert_pull_request_filter_policy(original, name)
    workflow = copy.deepcopy(original)
    workflow["on"]["pull_request"] = {key: value}
    assert workflow != original
    with pytest.raises(AssertionError):
        assert_pull_request_filter_policy(workflow, name)


@pytest.mark.parametrize("name", ["ci.yml", "docs.yml"])
def test_required_authz_rejects_missing_pull_request_trigger(name: str) -> None:
    original = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
    assert_pull_request_filter_policy(original, name)
    workflow = copy.deepcopy(original)
    del workflow["on"]["pull_request"]
    assert workflow != original
    with pytest.raises(AssertionError):
        assert_pull_request_filter_policy(workflow, name)


@pytest.mark.parametrize("paths", [None, [], ["src/**"]])
def test_native_authz_guard_rejects_missing_or_changed_pull_request_paths(
    paths: list[str] | None,
) -> None:
    name = "native-feed.yml"
    original = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
    assert_pull_request_filter_policy(original, name)
    workflow = copy.deepcopy(original)
    if paths is None:
        del workflow["on"]["pull_request"]["paths"]
    else:
        workflow["on"]["pull_request"]["paths"] = paths
    assert workflow != original
    with pytest.raises(AssertionError):
        assert_pull_request_filter_policy(workflow, name)


def assert_ready_dependencies(job: dict[str, Any]) -> None:
    # old: assert set(ci["jobs"]["ready"]["needs"]) == evidence.NEEDS | {"changes"}
    assert set(job["needs"]) == evidence.NEEDS | {"changes", "authz"}


def test_workflows_keep_every_gate_ahead_of_oidc_publication() -> None:
    workflows = ROOT / ".github/workflows"
    ci = yaml.load((workflows / "ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert ci["jobs"]["test"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]
    ready_job = ci["jobs"]["ready"]
    # old: assert set(ci["jobs"]["ready"]["needs"]) == evidence.NEEDS | {"changes"}
    assert_ready_dependencies(ready_job)
    # The canon gates `ready` on the code class; the authz refusal step is gone
    # because a skipped dependency is refused by `check`'s aggregate instead.
    assert ready_job["if"] == "needs.changes.outputs.code == 'true'"
    assert ready_job["name"] == "release/ready"
    assert ready_job["steps"][0] == {
        "uses": "actions/checkout@v7",
        "if": "needs.authz.result == 'success'",
        "with": {"persist-credentials": "false"},
    }
    for job, suite, enabled in (
        ("docker", "docker", "PINEFORGE_DOCKER_TEST=1"),
        ("database-e2e", "database", None),
    ):
        steps = ci["jobs"][job]["steps"]
        assert steps[0] == {
            "name": "authz must have passed",
            "if": "needs.authz.result != 'success'",
            "run": "exit 1",
        }
        assert steps[1]["uses"] == "actions/checkout@v7"
        assert steps[1]["if"] == "needs.authz.result == 'success'"
        commands = "\n".join(step.get("run", "") for step in ci["jobs"][job]["steps"])
        assert f"--suite {suite}" in commands
        if enabled:
            assert enabled in commands
        else:
            assert ci["jobs"][job]["env"]["PINEFORGE_DATABASE_E2E"] == "1"
    release = yaml.load((workflows / "release.yml").read_text(), Loader=yaml.BaseLoader)
    assert release["on"]["workflow_run"]["workflows"] == ["CI", "Native feed"]
    # old: assert set(release["jobs"]["publish"]["needs"]) == {"prepare", "build", "github-release"}
    assert set(release["jobs"]["publish"]["needs"]) == {
        "authz",
        "prepare",
        "build",
        "github-release",
    }
    steps = release["jobs"]["publish"]["steps"]
    preflight = next(i for i, step in enumerate(steps) if "preflight" in step.get("run", ""))
    publish = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("pypa/"))
    assert preflight < publish
    assert release["jobs"]["publish"]["environment"]["name"] == "pypi"
    receiver = yaml.load((workflows / "pineforge-release.yml").read_text(), Loader=yaml.BaseLoader)
    assert receiver["on"]["repository_dispatch"]["types"] == ["pineforge-release"]


@pytest.mark.parametrize("dependency", ["authz", "changes", *sorted(evidence.NEEDS), "extra"])
def test_ready_rejects_missing_or_extra_publication_dependencies(dependency: str) -> None:
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = copy.deepcopy(ci["jobs"]["ready"])
    assert_ready_dependencies(job)
    if dependency == "extra":
        job["needs"].append(dependency)
    else:
        job["needs"].remove(dependency)
    with pytest.raises(AssertionError):
        assert_ready_dependencies(job)


def test_rollback_is_a_new_patch_and_rejected_events_cannot_return(ready, baseline) -> None:
    good, good_raw = ready
    apply(baseline, contract.update_files(baseline, good, good_raw))
    rejected, rejected_raw = next_ready(good)
    apply(baseline, contract.update_files(baseline, rejected, rejected_raw))
    rollback = {
        "package_version": "0.3.1",
        "source": "a" * 40,
        "rejected_event_id": rejected["event_id"],
    }
    apply(baseline, contract.update_files(baseline, good, good_raw, rollback=rollback))
    lock = contract.verify_local(baseline)
    assert lock["package_version"] == "0.3.3"
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
            "package_version": "0.3.1",
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
    rejected, rejected_raw = next_ready(good)
    apply(baseline, contract.update_files(baseline, rejected, rejected_raw))
    apply(
        baseline,
        contract.update_files(
            baseline,
            good,
            good_raw,
            rollback={
                "package_version": "0.3.1",
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
