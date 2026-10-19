from __future__ import annotations

import base64
import copy
import io
import json
import os
import zipfile
from pathlib import Path

import hub_receipt_validator as hub
import pytest
import yaml
from release_chain import automation, contract, publishing, receipts, reporting

FIXTURES = Path(__file__).parent / "fixtures/release_chain"
REPO = "owner/data"
HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40


class ObservedGitHub:
    def __init__(self):
        self.writes = []
        self.archive = b""
        self.pr = {
            "number": 7,
            "merged": True,
            "merged_at": "2020-01-01T00:05:00Z",
            "merge_commit_sha": MERGE,
            "base": {"ref": "main", "sha": BASE},
            "head": {"sha": HEAD, "repo": {"full_name": REPO}},
            "body": automation.body({"version": "0.3.1", "generated_head": HEAD, "base": BASE}),
        }
        self.runs = {
            70: {
                "id": 70,
                "run_attempt": 2,
                "path": ".github/workflows/ci.yml",
                "head_sha": HEAD,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            },
            200: {
                "id": 200,
                "run_attempt": 1,
                "path": ".github/workflows/release.yml",
                "head_sha": MERGE,
                "event": "workflow_run",
                "status": "completed",
                "conclusion": "success",
            },
            300: {
                "id": 300,
                "path": ".github/workflows/pineforge-release.yml",
                "head_sha": MERGE,
                "event": "workflow_run",
            },
        }
        self.jobs = {
            70: [
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2020-01-01T00:04:00Z",
                }
                for name in receipts.CI_JOBS
            ],
            200: [
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2020-01-01T00:06:00Z",
                }
                for name in receipts.DEPLOY_JOBS
            ],
        }

    def api(self, route):
        route = route.split("?", 1)[0].removeprefix(f"repos/{REPO}/")
        if route == "pulls/7":
            return copy.deepcopy(self.pr)
        if route == f"commits/{MERGE}":
            return {"parents": [{"sha": BASE}]}
        parts = route.split("/")
        if route.startswith("actions/runs/"):
            run_id = int(parts[2])
            if parts[-1] == "jobs":
                return {"jobs": copy.deepcopy(self.jobs[run_id])}
            if parts[-1] == "artifacts":
                name = "release-stage-1-50-verify" if run_id == 200 else "release-receipt-data"
                return {
                    "artifacts": [
                        {
                            "id": run_id,
                            "name": name,
                            "expired": False,
                            "digest": "sha256:" + hub.sha256(self.archive),
                        }
                    ]
                }
            return copy.deepcopy(self.runs[run_id])
        raise AssertionError(route)

    def request(self, url, **kwargs):
        assert url == f"https://api.github.com/repos/{REPO}/actions/artifacts/300/zip"
        return self.archive

    def consumer_api(self, repo, endpoint, method="GET", data=None):
        if method != "GET":
            self.writes.append((repo, endpoint, method, data))
            return None
        return self.api(f"repos/{repo}/{endpoint}")


@pytest.fixture
def completed_data_receipt(monkeypatch):
    event = contract.object_json((FIXTURES / "ready-event.json").read_bytes())
    expected = contract.object_json((FIXTURES / "sample-release-receipt.json").read_bytes())
    monkeypatch.setenv("GITHUB_RUN_ID", "300")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("RELEASE_RECEIPT_POLICY", (FIXTURES / "receipt-policy.json").read_text())
    monkeypatch.setattr(receipts, "timestamp", lambda: "2020-01-01T00:07:00Z")
    monkeypatch.setattr(reporting, "timestamp", lambda: "2020-01-01T00:07:00Z")
    pr_gate = {
        "run_id": 70,
        "attempt": 2,
        "head": HEAD,
        "distributions": {"wheel": "e" * 64, "sdist": "f" * 64},
    }
    main_gate = {
        "run_id": 80,
        "head": MERGE,
        "distributions": {"wheel": "1" * 64, "sdist": "2" * 64},
    }
    staged = receipts.receipt(event)
    staged.update(
        pr=copy.deepcopy(expected["pr"]),
        state="deploying",
        deployed=receipts.served(event, MERGE, "0.3.1", main_gate["distributions"]),
        previous=copy.deepcopy(expected["previous"]),
    )
    staged["attempt"] = 200
    receipts.candidate(staged, event, pr_gate, "0.3.1")
    client = ObservedGitHub()
    monkeypatch.setattr(reporting, "api", client.consumer_api)
    monkeypatch.setattr(
        reporting, "inventory", lambda repo, route, key: client.api(f"repos/{repo}/{route}")[key]
    )
    monkeypatch.setattr(reporting, "artifact_json", lambda *args: copy.deepcopy(staged))
    monkeypatch.setattr(
        reporting, "hub_manifest", lambda version: (FIXTURES / "release-manifest.json").read_bytes()
    )
    monkeypatch.setattr(reporting, "candidate_gate", lambda *args: pr_gate)

    def landed_gate(repo, run_id, *, head):
        assert run_id == 80 and head == MERGE
        return main_gate

    monkeypatch.setattr(reporting, "gate_for_run", landed_gate)
    lock = {
        "event": event,
        "package_version": "0.3.1",
        "previous": {"package_version": "0.3.0"},
    }
    monkeypatch.setattr(reporting, "verify_local", lambda root: lock)
    monkeypatch.setattr(contract, "verify_artifacts", lambda target: None)
    verified_versions = []

    def verified_package(repo, version, *, adopter_lock=None):
        assert repo == REPO
        if version == "0.3.1":
            assert adopter_lock is None
            identity = staged["deployed"]
        else:
            assert version == "0.3.0"
            assert adopter_lock is lock
            assert adopter_lock["package_version"] == "0.3.1"
            identity = staged["previous"]
        verified_versions.append(version)
        return copy.deepcopy(identity)

    monkeypatch.setattr(reporting, "verified_package", verified_package)

    def download(command, *, check):
        assert command[:3] == ["gh", "run", "download"] and check
        (Path(command[-1]) / "evidence.json").write_bytes(contract.json_bytes(main_gate))

    monkeypatch.setattr(reporting.subprocess, "run", download)
    report = receipts.receipt(None)
    reporting.publication(REPO, 200, report)
    assert verified_versions == ["0.3.1", "0.3.0"]
    assert report == expected
    return report, event, client, hub.profile("data")


def test_coordinator_builds_receipt_accepted_by_hubs_own_validator(
    completed_data_receipt, tmp_path
):
    report, event, client, policy = completed_data_receipt
    public = hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])
    assert public["state"] == "verified"
    assert public["deployed"]["source"] == MERGE
    assert "owner/data" not in json.dumps(report)
    assert "https://" not in json.dumps(report)
    assert set(report) == hub.COMMON_FIELDS | {"event_id", "release_version"}
    path = tmp_path / "release-receipt.json"
    automation.finish(report, path)
    assert path.read_bytes() == hub.canonical(report)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(path.name, path.read_bytes())
    client.archive = archive.getvalue()
    assert hub.fetch_artifact(client, REPO, client.runs[300], policy) == report
    assert client.writes == []


def test_callback_is_exact_four_key_signal_for_the_uploaded_reporting_run(completed_data_receipt):
    report, _event, client, _policy = completed_data_receipt
    reporting.callback(report)
    assert client.writes == [
        (
            contract.HUB,
            "dispatches",
            "POST",
            {
                "event_type": "pineforge-release-status",
                "client_payload": {
                    "schema": "pineforge/consumer-signal/v1",
                    "consumer": "data",
                    "release_version": "1.6.0",
                    "run_id": 300,
                },
            },
        )
    ]
    signal = client.writes[0][3]["client_payload"]
    assert hub.signal_targets(signal, ["data"]) == [{"consumer": "data", "run_id": "300"}]
    with pytest.raises(hub.Invalid, match="unexpected object fields"):
        hub.signal_targets({**signal, "receipt": report}, ["data"])


@pytest.mark.parametrize("mutation", ["extra", "attempt", "base", "ci", "skip", "served", "source"])
def test_hub_rejects_contract_or_gate_drift(completed_data_receipt, mutation):
    original, event, client, policy = completed_data_receipt
    report = copy.deepcopy(original)
    reporting_run = copy.deepcopy(client.runs[300])
    if mutation == "extra":
        report["manifest"] = event["manifest"]
    elif mutation == "attempt":
        report["attempt"] = {"run_id": 300, "number": 1}
    elif mutation == "base":
        report["pr"]["base"] = BASE
    elif mutation == "ci":
        report["ci"]["sha"] = MERGE
    elif mutation == "skip":
        client.jobs[200][4]["conclusion"] = "skipped"
    elif mutation == "served":
        report["deployed"]["image_digest"] = "sha256:" + "0" * 64
    else:
        reporting_run["event"] = "pull_request"
    with pytest.raises(hub.Invalid):
        hub.validate_receipt(client, REPO, report, event, policy, reporting_run)


def test_moved_reporting_source_queues_a_trusted_immutable_tag_run(completed_data_receipt):
    _report, _event, client, _policy = completed_data_receipt
    client.runs[300]["head_sha"] = "9" * 40
    result = receipts.receipt(None)
    reporting.publication(REPO, 200, result)
    assert result["event_id"] is None
    assert client.writes == [
        (
            REPO,
            "actions/workflows/pineforge-release.yml/dispatches",
            "POST",
            {
                "ref": "v0.3.1",
                "inputs": {"publication_run": "200"},
            },
        )
    ]


def test_only_the_documented_replay_upload_can_skip(completed_data_receipt):
    report, event, client, policy = completed_data_receipt
    client.jobs[200][3]["conclusion"] = "skipped"
    report["deploy"]["results"]["publish"] = "skipped"
    assert (
        hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])["state"]
        == "verified"
    )


def test_workflow_transport_is_trusted_upload_then_signal():
    root = Path(__file__).parents[1]
    coordinator = yaml.load(
        (root / ".github/workflows/pineforge-release.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert coordinator["on"]["workflow_run"]["workflows"] == ["CI", "Release"]
    assert coordinator["jobs"]["report"]["with"]["hub"] == "true"
    steps = coordinator["jobs"]["reconcile"]["steps"]
    assert (
        next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))["with"][
            "ref"
        ]
        == "main"
    )
    workflow = yaml.load(
        (root / ".github/workflows/release-report.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["report"]["steps"]
    upload = next(i for i, step in enumerate(steps) if step.get("id") == "upload")
    callback = next(
        i for i, step in enumerate(steps) if "reporting callback" in step.get("run", "")
    )
    assert upload < callback
    assert steps[upload]["with"] == {
        "name": "release-receipt-data",
        "path": "release-receipt.json",
        "if-no-files-found": "error",
        "overwrite": "true",
        "retention-days": "90",
    }
    assert "steps.upload.outcome == 'success'" in steps[callback]["if"]
    assert workflow["on"]["workflow_call"]["inputs"]["hub"]["default"] == "false"
    assert not any("RELEASE_RECEIPT_EVENT_TYPE" in step.get("env", {}) for step in steps)


@pytest.fixture
def bootstrap_identity():
    return contract.object_json((FIXTURES / "bootstrap/identity.json").read_bytes())


@pytest.fixture
def bootstrap_lock():
    return {
        "package_version": "0.3.1",
        "previous": {
            "package_version": "0.3.0",
            "event_id": None,
            "image": (
                "ghcr.io/pineforge-4pass/pineforge-release:1.0.0@sha256:"
                "b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86"
            ),
        },
    }


def test_prepare_passes_adopter_lock_for_previous(
    monkeypatch, tmp_path, bootstrap_lock, bootstrap_identity
):
    event = contract.object_json((FIXTURES / "ready-event.json").read_bytes())
    lock = {**bootstrap_lock, "event": event}
    client = ObservedGitHub()
    client.pr["user"] = {"login": "release[bot]"}
    client.pr["body"] = automation.body(
        {
            "kind": "release",
            "event_id": event["event_id"],
            "version": "0.3.1",
            "generated_head": HEAD,
            "base": BASE,
        }
    )
    client.runs[80] = {
        "event": "push",
        "head_branch": "main",
        "head_sha": MERGE,
        "run_attempt": 1,
    }
    files = {
        "pineforge_data-0.3.1-py3-none-any.whl": b"tested wheel",
        "pineforge_data-0.3.1.tar.gz": b"tested sdist",
    }
    gate = {
        "run_id": 80,
        "attempt": 1,
        "head": MERGE,
        "tested_sha": MERGE,
        "tree": "d" * 40,
        "distributions": {name: contract.sha256(data) for name, data in files.items()},
    }
    output = tmp_path / "bundle"
    verified_versions = []

    def api(repo, endpoint):
        assert repo == REPO
        if endpoint == f"commits/{MERGE}/pulls?per_page=100":
            return [copy.deepcopy(client.pr)]
        return client.consumer_api(repo, endpoint)

    def git(*args):
        return {
            ("fetch", "origin", MERGE): "",
            ("rev-parse", f"{MERGE}^{{tree}}"): gate["tree"],
            ("rev-parse", f"{MERGE}^1"): BASE,
        }[args]

    def snapshot(source, candidate):
        assert source == MERGE and candidate == output / "source"
        (candidate / "pyproject.toml").write_text('[project]\nversion = "0.3.1"\n')

    def landed_gate(repo, run_id, *, head):
        assert repo == REPO and run_id == 80 and head == MERGE
        return gate

    def verified_package(repo, version, *, adopter_lock=None):
        assert repo == REPO and version == "0.3.0"
        assert adopter_lock is lock
        assert adopter_lock["package_version"] == "0.3.1"
        verified_versions.append(version)
        return copy.deepcopy(bootstrap_identity)

    def download(command, *, check):
        assert check
        assert command == [
            "gh",
            "run",
            "download",
            "80",
            "--repo",
            REPO,
            "--name",
            "python-package-distributions-1",
            "--dir",
            str(output / "dist"),
        ]
        dist = output / "dist"
        dist.mkdir()
        for name, data in files.items():
            (dist / name).write_bytes(data)

    monkeypatch.setenv("RELEASE_BOT_LOGIN", "release[bot]")
    monkeypatch.setattr(publishing, "api", api)
    monkeypatch.setattr(publishing, "git", git)
    monkeypatch.setattr(publishing, "snapshot", snapshot)
    monkeypatch.setattr(publishing, "verify_local", lambda candidate: lock)
    monkeypatch.setattr(publishing, "gate_for_run", landed_gate)
    monkeypatch.setattr(
        publishing,
        "candidate_gate",
        lambda *args: {**gate, "run_id": 70, "attempt": 2, "head": HEAD},
    )
    monkeypatch.setattr(publishing, "verified_package", verified_package)
    monkeypatch.setattr(publishing, "current_intent", lambda *args: None)
    monkeypatch.setattr(publishing.subprocess, "run", download)
    monkeypatch.setattr(publishing, "pypi_files", lambda version: None)
    report = receipts.receipt(None)
    result = publishing.prepare(tmp_path, REPO, 80, output, report)
    assert verified_versions == ["0.3.0"]
    assert report["previous"] == bootstrap_identity
    assert report["state"] == "deploying"
    assert contract.object_json((output / "evidence.json").read_bytes()) == gate
    assert result == {
        "eligible": "true",
        "published": "false",
        "sha": MERGE,
        "tag": "v0.3.1",
        "version": "0.3.1",
        "ci_run": "80",
        "has_target": "true",
    }


@pytest.fixture
def bootstrap_observed(monkeypatch, bootstrap_identity):
    # Literal archived bytes and a separately transcribed identity; no verifier,
    # inventory, provenance or hash function is replaced.
    files = {
        name: base64.b64decode((FIXTURES / "bootstrap" / f"{name}.b64").read_bytes())
        for name in (
            "pineforge_data-0.3.0-py3-none-any.whl",
            "pineforge_data-0.3.0.tar.gz",
        )
    }
    observed = {
        "downloads": dict(files),
        "release_bytes": dict(files),
        "assets": [{"name": name, "id": i} for i, name in enumerate(files)],
        "source": "fb5cec39b6c6d70af103df0df182d47bb5812191",
        "calls": [],
        "urls": [
            {
                "filename": name,
                "digests": {"sha256": digest},
                "url": "https://files.pythonhosted.org/" + name,
            }
            for name, digest in bootstrap_identity["distributions"].items()
        ],
    }

    def fetch(url):
        observed["calls"].append(("fetch", url))
        if url.startswith("https://pypi.org/pypi/pineforge-data/"):
            return contract.json_bytes({"urls": observed["urls"]})
        assert url.startswith("https://files.pythonhosted.org/")
        return observed["downloads"][url.rsplit("/", 1)[1]]

    def api(repo, endpoint):
        observed["calls"].append(("api", repo, endpoint))
        assert endpoint in ("releases/tags/v0.3.0", "releases/tags/v0.3.1", "releases/tags/v0.2.0")
        return {"assets": copy.deepcopy(observed["assets"])}

    def gh_bytes(route, *, accept):
        observed["calls"].append(("gh_bytes", route))
        assert accept == "application/octet-stream"
        name = list(files)[int(route.rsplit("/", 1)[1])]
        return observed["release_bytes"][name]

    def git(*args):
        observed["calls"].append(("git", *args))
        if args[0] == "fetch":
            assert args[1] == "origin" and args[2].startswith("refs/tags/v")
            return ""
        assert args[0] == "rev-parse" and args[1].endswith("^{commit}")
        return observed["source"]

    def snapshot(source, root):
        observed["calls"].append(("snapshot", source))
        # Exercise the real verify_local refusal when the exemption is ineligible.
        runtime = root / contract.RUNTIME
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            'DEFAULT_RELEASE_IMAGE = "ghcr.io/pineforge-4pass/pineforge-release:1.0.0@sha256:'
            'b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86"\n'
        )
        docker = root / contract.DOCKERFILE
        docker.parent.mkdir(parents=True, exist_ok=True)
        docker.write_text(
            "ARG PINEFORGE_RELEASE_IMAGE=ghcr.io/pineforge-4pass/pineforge-release:1.0.0@sha256:"
            "b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86\n"
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname = "pineforge-data"\nversion = "0.3.0"\n'
        )

    for name, function in (
        ("fetch", fetch),
        ("api", api),
        ("gh_bytes", gh_bytes),
        ("git", git),
        ("snapshot", snapshot),
    ):
        monkeypatch.setattr(publishing, name, function)
    return observed


def test_previous_identity_requires_observed_manifest_provenance(bootstrap_observed):
    # Byte-identical downloads/assets still cannot manufacture missing prior facts.
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(REPO, "0.3.0")


def test_first_adopter_returns_exact_bootstrap_identity(
    bootstrap_observed, bootstrap_lock, bootstrap_identity
):
    actual = publishing.verified_package(
        "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
    )
    assert actual == bootstrap_identity
    assert not any(name.startswith("facts_") for name in actual)
    assert (
        receipts.canonical(
            {name: actual[name] for name in ("package", "version", "distributions")}
        )
        == (FIXTURES / "bootstrap/distribution-identity.json").read_bytes()
    )
    assert ("git", "fetch", "origin", "refs/tags/v0.3.0") in bootstrap_observed["calls"]
    assert ("git", "rev-parse", "v0.3.0^{commit}") in bootstrap_observed["calls"]
    assert not any(call[0] == "snapshot" for call in bootstrap_observed["calls"])


@pytest.mark.parametrize(
    "name",
    ["pineforge_data-0.3.0-py3-none-any.whl", "pineforge_data-0.3.0.tar.gz"],
)
def test_bootstrap_recorded_hashes_bind_independent_archived_bytes(
    bootstrap_observed, bootstrap_identity, name
):
    # A one-character edit of either recorded hash fails against actual bytes.
    assert (
        contract.sha256(bootstrap_observed["downloads"][name])
        == bootstrap_identity["distributions"][name]
        == publishing.BOOTSTRAP_PACKAGE["distributions"][name]
    )


@pytest.mark.parametrize(
    "name",
    ["pineforge_data-0.3.0-py3-none-any.whl", "pineforge_data-0.3.0.tar.gz"],
)
def test_bootstrap_rejects_wrong_pypi_bytes(bootstrap_observed, bootstrap_lock, name):
    bootstrap_observed["downloads"][name] += b"\n"
    with pytest.raises(ValueError, match="PyPI bytes differ"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize(
    "name",
    ["pineforge_data-0.3.0-py3-none-any.whl", "pineforge_data-0.3.0.tar.gz"],
)
def test_bootstrap_rejects_unrecorded_bytes_even_when_both_services_agree(
    bootstrap_observed, bootstrap_lock, name
):
    wrong = bootstrap_observed["downloads"][name] + b"\n"
    bootstrap_observed["downloads"][name] = wrong
    bootstrap_observed["release_bytes"][name] = wrong
    entry = next(row for row in bootstrap_observed["urls"] if row["filename"] == name)
    entry["digests"]["sha256"] = contract.sha256(wrong)
    with pytest.raises(ValueError, match="bootstrap PyPI inventory differs"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize(
    "name",
    ["pineforge_data-0.3.0-py3-none-any.whl", "pineforge_data-0.3.0.tar.gz"],
)
def test_bootstrap_rejects_different_release_asset_bytes(bootstrap_observed, bootstrap_lock, name):
    bootstrap_observed["release_bytes"][name] += b"\n"
    with pytest.raises(ValueError, match="release asset differs from PyPI"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
@pytest.mark.parametrize(
    "name",
    ["pineforge_data-0.3.0-py3-none-any.whl", "pineforge_data-0.3.0.tar.gz"],
)
def test_bootstrap_requires_unique_release_assets(
    bootstrap_observed, bootstrap_lock, mutation, name
):
    assets = bootstrap_observed["assets"]
    if mutation == "missing":
        assets[:] = [asset for asset in assets if asset["name"] != name]
    else:
        assets.append(copy.deepcopy(next(asset for asset in assets if asset["name"] == name)))
    with pytest.raises(ValueError, match="no unique immutable release asset"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra"])
def test_bootstrap_requires_exact_pypi_inventory(bootstrap_observed, bootstrap_lock, mutation):
    entries = bootstrap_observed["urls"]
    if mutation == "missing":
        entries.pop()
    elif mutation == "duplicate":
        entries.append(copy.deepcopy(entries[0]))
    else:
        extra = copy.deepcopy(entries[0])
        extra["filename"] = "another.whl"
        entries.append(extra)
    with pytest.raises(ValueError, match=r"wheel and sdist|bootstrap PyPI inventory differs"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


def test_bootstrap_rejects_a_moved_tag(bootstrap_observed, bootstrap_lock):
    bootstrap_observed["source"] = "fb5cec39b6c6d70af103df0df182d47bb5812190"
    with pytest.raises(ValueError, match="bootstrap tag differs from the recorded commit"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize("saved", [{}, {"state": "verified"}])
def test_bootstrap_never_applies_to_saved_receipts(bootstrap_observed, bootstrap_lock, saved):
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", saved, adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize("version", ["0.3.1", "0.2.0"])
def test_other_previous_versions_still_require_provenance(
    bootstrap_observed, bootstrap_lock, version
):
    bootstrap_lock["previous"]["package_version"] = version
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", version, adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize(
    "repo", ["owner/data", "other/pineforge-data", "pineforge-4pass/pineforge-codegen"]
)
def test_bootstrap_is_exclusive_to_the_data_repo(bootstrap_observed, bootstrap_lock, repo):
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(repo, "0.3.0", adopter_lock=bootstrap_lock)


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "sha256:" + "1" * 64),
        ("image", "ghcr.io/pineforge-4pass/pineforge-release:1.0.0@sha256:" + "1" * 64),
        (
            "image",
            "ghcr.io/pineforge-4pass/pineforge-release:1.0.1@sha256:"
            "b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86",
        ),
        ("package_version", "0.2.0"),
        ("provenance", "pre-manifest-bootstrap"),
    ],
)
def test_bootstrap_requires_exact_previous_context(
    bootstrap_observed, bootstrap_lock, field, value
):
    bootstrap_lock["previous"][field] = value
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


@pytest.mark.parametrize("field", ["event_id", "image", "package_version"])
def test_bootstrap_rejects_incomplete_previous_context(bootstrap_observed, bootstrap_lock, field):
    del bootstrap_lock["previous"][field]
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


def test_bootstrap_requires_caller_lock(bootstrap_observed):
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package("pineforge-4pass/pineforge-data", "0.3.0")


def test_bootstrap_cannot_be_reused_by_a_later_adopter(bootstrap_observed, bootstrap_lock):
    bootstrap_lock["package_version"] = "0.3.2"
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


def test_bootstrap_cannot_be_used_by_a_rollback_lock(bootstrap_observed, bootstrap_lock):
    bootstrap_lock["rollback"] = {"package_version": "0.3.0"}
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(
            "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
        )


@pytest.fixture
def bootstrap_receipt(bootstrap_observed, bootstrap_lock):
    # Do not use completed_data_receipt: it predates this exemption and mocks the
    # verifier for synthetic coordinator evidence. This fixture calls it for real.
    report = contract.object_json((FIXTURES / "sample-release-receipt.json").read_bytes())
    report["previous"] = publishing.verified_package(
        "pineforge-4pass/pineforge-data", "0.3.0", adopter_lock=bootstrap_lock
    )
    event = contract.object_json((FIXTURES / "ready-event.json").read_bytes())
    policy = contract.object_json((FIXTURES / "receipt-policy.json").read_bytes())["data"]
    return report, event, ObservedGitHub(), policy


def test_bootstrap_previous_survives_receipt_writer_and_hub_validation(
    bootstrap_receipt, bootstrap_identity, tmp_path
):
    report, event, client, policy = bootstrap_receipt
    public = hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])
    assert public["previous"] == bootstrap_identity
    path = tmp_path / "release-receipt.json"
    automation.finish(report, path)
    assert contract.object_json(path.read_bytes())["previous"] == bootstrap_identity
    assert path.read_bytes() == hub.canonical(report)
    assert reporting.public_receipt(report)["previous"] == bootstrap_identity


@pytest.mark.parametrize(
    "field,value",
    [
        ("provenance", "manifest"),
        ("package", "pineforge-codegen"),
        ("version", "0.3.1"),
        ("version", "0.2.0"),
        ("source", "fb5cec39b6c6d70af103df0df182d47bb5812190"),
        ("image_digest", "sha256:" + "1" * 64),
        ("deployment_id_sha256", "1" * 64),
        ("distributions", None),
    ],
)
def test_hub_rejects_bootstrap_identity_drift(bootstrap_receipt, field, value):
    report, event, client, policy = bootstrap_receipt
    report["previous"][field] = value
    with pytest.raises(hub.Invalid):
        hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])


@pytest.mark.parametrize(
    "field",
    [
        "provenance",
        "package",
        "version",
        "source",
        "image_digest",
        "deployment_id_sha256",
        "distributions",
    ],
)
def test_hub_rejects_missing_bootstrap_keys(bootstrap_receipt, field):
    report, event, client, policy = bootstrap_receipt
    del report["previous"][field]
    with pytest.raises(hub.Invalid):
        hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])


@pytest.mark.parametrize(
    "field,value",
    [
        ("facts_commit", None),
        ("facts_commit", "e" * 40),
        ("facts_sha256", None),
        ("facts_sha256", "d" * 64),
        ("extra", "not permitted"),
    ],
)
def test_hub_rejects_facts_or_extra_bootstrap_keys(bootstrap_receipt, field, value):
    report, event, client, policy = bootstrap_receipt
    report["previous"][field] = value
    with pytest.raises(hub.Invalid):
        hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])


@pytest.mark.parametrize("mutation", ["wheel", "sdist", "missing", "extra"])
def test_hub_rejects_unrecorded_bootstrap_distributions(bootstrap_receipt, mutation):
    report, event, client, policy = bootstrap_receipt
    previous = report["previous"]
    distributions = previous["distributions"]
    if mutation == "wheel":
        distributions["pineforge_data-0.3.0-py3-none-any.whl"] = "1" * 64
    elif mutation == "sdist":
        distributions["pineforge_data-0.3.0.tar.gz"] = "1" * 64
    elif mutation == "missing":
        del distributions["pineforge_data-0.3.0.tar.gz"]
    else:
        distributions["extra.whl"] = "1" * 64
    previous["deployment_id_sha256"] = hub.sha256(
        hub.canonical({name: previous[name] for name in ("package", "version", "distributions")})
    )
    with pytest.raises(hub.Invalid):
        hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])


@pytest.mark.parametrize("consumer", ["offline", "hosted", "application", "web", "license"])
@pytest.mark.parametrize("state", ["pending", "verified"])
def test_hub_rejects_bootstrap_for_other_consumers(bootstrap_receipt, consumer, state):
    report, event, client, policy = bootstrap_receipt
    report.update(consumer=consumer, state=state)
    with pytest.raises(hub.Invalid, match=r"only valid for product data\.previous"):
        hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])


@pytest.mark.parametrize("slot", ["preview", "deployed"])
@pytest.mark.parametrize("state", ["pending", "verified"])
def test_hub_rejects_bootstrap_outside_previous(bootstrap_receipt, slot, state):
    report, event, client, policy = bootstrap_receipt
    report[slot] = copy.deepcopy(report["previous"])
    report["state"] = state
    with pytest.raises(hub.Invalid, match=r"only valid for product data\.previous"):
        hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])


def test_hub_rejects_bootstrap_for_facts_only_identity(bootstrap_identity):
    with pytest.raises(hub.Invalid, match=r"only valid for product data\.previous"):
        hub.identity(bootstrap_identity, facts_only=True, consumer="data", slot="previous")


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("PINEFORGE_NETWORK_TEST") != "1",
    reason="set PINEFORGE_NETWORK_TEST=1 to check the recorded PyPI bootstrap hashes",
)
def test_bootstrap_recorded_hashes_match_live_pypi(bootstrap_identity):
    metadata = contract.object_json(
        publishing.fetch("https://pypi.org/pypi/pineforge-data/0.3.0/json")
    )
    assert len(metadata["urls"]) == 2
    assert (
        {entry["filename"]: entry["digests"]["sha256"] for entry in metadata["urls"]}
        == bootstrap_identity["distributions"]
        == publishing.BOOTSTRAP_PACKAGE["distributions"]
    )


def test_verified_target_cannot_regress_even_on_a_new_attempt(completed_data_receipt, monkeypatch):
    report, event, client, policy = completed_data_receipt
    monkeypatch.setattr(
        reporting,
        "inventory",
        lambda *args: [
            {
                "assets": [{"name": "release-receipt.json", "id": 8}],
            }
        ],
    )
    monkeypatch.setattr(reporting, "gh_bytes", lambda *args, **kwargs: receipts.canonical(report))
    assert reporting.already_verified(REPO, event["event_id"])
    previous = hub.validate_receipt(client, REPO, report, event, policy, client.runs[300])
    failed = {**previous, "attempt": 301, "state": "failed"}
    with pytest.raises(hub.Invalid, match="verified release cannot regress"):
        hub.transition(previous, failed)


def test_successful_manual_pr_run_reports_progress_without_claiming_deploy(
    completed_data_receipt, monkeypatch
):
    report, _event, client, _policy = completed_data_receipt
    progress = copy.deepcopy(report)
    progress.update(
        state="pr_open", ci=None, e2e=None, deploy=None, preview=None, deployed=None, previous=None
    )
    progress["pr"]["merge"] = None
    monkeypatch.setattr(
        reporting,
        "inventory",
        lambda *args: [
            {
                "name": "release-stage-1-10-manual",
                "expired": False,
            }
        ],
    )
    monkeypatch.setattr(reporting, "artifact_json", lambda *args: copy.deepcopy(progress))
    result = receipts.receipt(None)
    reporting.publication(REPO, 200, result)
    assert result["state"] == "pr_open" and result["deploy"] is None
    assert client.writes == []


def test_automatic_triggers_wait_for_a_configured_chain():
    root = Path(__file__).parents[1]
    coordinator = yaml.load(
        (root / ".github/workflows/pineforge-release.yml").read_text(), Loader=yaml.BaseLoader
    )
    condition = coordinator["jobs"]["reconcile"]["if"]
    assert condition.startswith(
        "(vars.RELEASE_APP_ID != '' || "
        "(github.event_name != 'schedule' && github.event_name != 'workflow_run')) &&"
    )
    report_if = coordinator["jobs"]["report"]["if"]
    assert report_if == "always() && needs.reconcile.result != 'skipped'"
