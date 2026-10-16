from __future__ import annotations

import copy
import io
import json
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
    monkeypatch.setattr(
        reporting,
        "verify_local",
        lambda root: {
            "event": event,
            "package_version": "0.3.1",
            "previous": {"package_version": "0.3.0"},
        },
    )
    monkeypatch.setattr(contract, "verify_artifacts", lambda target: None)
    monkeypatch.setattr(
        reporting,
        "verified_package",
        lambda repo, version: copy.deepcopy(
            staged["deployed" if version == "0.3.1" else "previous"]
        ),
    )

    def download(command, *, check):
        assert command[:3] == ["gh", "run", "download"] and check
        (Path(command[-1]) / "evidence.json").write_bytes(contract.json_bytes(main_gate))

    monkeypatch.setattr(reporting.subprocess, "run", download)
    report = receipts.receipt(None)
    reporting.publication(REPO, 200, report)
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


def test_previous_identity_requires_observed_manifest_provenance(monkeypatch):
    # Byte-identical downloads/assets still cannot manufacture missing prior facts.
    files = {"previous.whl": b"legacy wheel", "previous.tar.gz": b"legacy sdist"}
    hashes = {name: contract.sha256(raw) for name, raw in files.items()}
    metadata = {
        "urls": [
            {
                "filename": name,
                "digests": {"sha256": digest},
                "url": "https://files.pythonhosted.org/" + name,
            }
            for name, digest in hashes.items()
        ]
    }
    monkeypatch.setattr(publishing, "pypi_files", lambda version: hashes)
    monkeypatch.setattr(
        publishing,
        "fetch",
        lambda url: (
            contract.json_bytes(metadata)
            if url.startswith("https://pypi.org/")
            else files[url.rsplit("/", 1)[1]]
        ),
    )
    monkeypatch.setattr(
        publishing,
        "api",
        lambda *args: {
            "assets": [{"name": name, "id": i} for i, name in enumerate(files)],
        },
    )
    monkeypatch.setattr(
        publishing,
        "gh_bytes",
        lambda route, **kwargs: list(files.values())[int(route.rsplit("/", 1)[1])],
    )
    monkeypatch.setattr(publishing, "git", lambda *args: "d" * 40)
    monkeypatch.setattr(publishing, "snapshot", lambda *args: None)
    monkeypatch.setattr(publishing, "verify_local", lambda root: None)
    with pytest.raises(ValueError, match="lacks manifest/facts provenance"):
        publishing.verified_package(REPO, "0.3.0")


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
        "(vars.RELEASE_APP_ID != '' || (github.event_name != 'schedule' && github.event_name != 'workflow_run')) &&"
    )
    assert coordinator["jobs"]["report"]["if"] == "always() && needs.reconcile.result != 'skipped'"
