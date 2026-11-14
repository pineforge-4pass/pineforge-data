"""Offline report-job entry tests; replace transport, keep receipt validation real."""

from __future__ import annotations

import copy
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import hub_receipt_validator as hub
import pytest
import yaml
from release_chain import automation, contract, evidence, publishing, receipts, reporting

FIXTURES = Path(__file__).parent / "fixtures/release_chain"
REPO = "owner/data"


def receipt_archive(report):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("release-receipt.json", receipts.canonical(report))
    return archive.getvalue()


class ReportObservations:
    """Mutable GitHub responses and retained bytes, never a validation result."""

    def __init__(self, report):
        self.report = report
        self.reads, self.writes, self.downloads = [], [], []
        self.issues, self.releases = [], []
        self.archives, self.assets, self.artifacts = {}, {}, {}
        self.bundles = {}
        pr = report["pr"]
        self.pr = {
            "number": pr["number"], "merged": True,
            "merged_at": "2020-01-01T00:05:00Z", "merge_commit_sha": pr["merge"],
            "base": {"ref": pr["base"], "sha": pr["base_sha"]},
            "head": {"sha": pr["head"], "repo": {"full_name": REPO}},
            "body": automation.body({
                "version": "0.3.1", "generated_head": pr["head"], "base": pr["base_sha"],
            }),
        }
        self.runs, self.jobs = {}, {}
        for slot, workflow in (("ci", "ci.yml"), ("e2e", "ci.yml"), ("deploy", "release.yml")):
            checks = report[slot]
            run_id = checks["run_id"]
            self.runs[run_id] = {
                "id": run_id, "run_attempt": checks["run_attempt"],
                "path": ".github/workflows/" + workflow, "head_sha": checks["sha"],
                "event": "workflow_run" if slot == "deploy" else "pull_request",
                "status": "completed", "conclusion": "success",
            }
            finished = "2020-01-01T00:06:00Z" if slot == "deploy" else "2020-01-01T00:04:00Z"
            self.jobs[run_id] = [
                {"name": name, "status": "completed", "conclusion": result,
                 "completed_at": finished}
                for name, result in checks["results"].items()
            ]
        self.reporter = {
            "id": report["attempt"], "run_attempt": report["sequence"],
            "path": ".github/workflows/" + receipts.WORKFLOW,
            "head_sha": pr["merge"], "event": "workflow_run",
        }

    def api(self, route):
        self.reads.append(route)
        path = route.split("?", 1)[0].removeprefix(f"repos/{REPO}/")
        if path == "issues":
            return copy.deepcopy(self.issues)
        if path == "releases":
            return copy.deepcopy(self.releases)
        if path == f"pulls/{self.pr['number']}":
            return copy.deepcopy(self.pr)
        if path == f"commits/{self.pr['merge_commit_sha']}":
            return {"parents": [{"sha": self.pr["base"]["sha"]}]}
        parts = path.split("/")
        if path.startswith("actions/runs/"):
            run_id = int(parts[2])
            if parts[-1] == "artifacts":
                return {"artifacts": copy.deepcopy(self.artifacts.get(run_id, []))}
            if "attempts" in parts:
                assert int(parts[4]) == self.runs[run_id]["run_attempt"]
            if parts[-1] == "jobs":
                return {"jobs": copy.deepcopy(self.jobs[run_id])}
            return copy.deepcopy(self.runs[run_id])
        raise AssertionError(f"unregistered GitHub read: {route}")

    def consumer_api(self, repo, endpoint, method="GET", data=None):
        assert repo == REPO
        if method == "GET":
            assert data is None
            return self.api(f"repos/{repo}/{endpoint}")
        self.writes.append((endpoint, method, copy.deepcopy(data)))
        if endpoint == "issues" and method == "POST":
            issue = {"number": 41, **copy.deepcopy(data)}
            self.issues.append(issue)
            return copy.deepcopy(issue)
        assert self.issues and endpoint.startswith("issues/41")
        if endpoint == "issues/41/comments" and method == "POST":
            return {"id": len(self.writes)}
        assert endpoint == "issues/41" and method == "PATCH"
        self.issues[0].update(copy.deepcopy(data))
        return copy.deepcopy(self.issues[0])

    def gh_bytes(self, route, **kwargs):
        assert set(kwargs) <= {"accept"}
        if route == f"repos/{contract.HUB}/releases/tags/v{self.report['release_version']}":
            return contract.json_bytes({
                "draft": False, "prerelease": False,
                "assets": [{"id": 81, "name": "release-manifest.json"}],
            })
        if route == f"repos/{contract.HUB}/releases/assets/81":
            return (FIXTURES / "release-manifest.json").read_bytes()
        path = route.removeprefix(f"repos/{REPO}/")
        if path.startswith("actions/artifacts/"):
            return self.archives[int(path.split("/")[2])]
        if path.startswith("releases/assets/"):
            return self.assets[int(path.split("/")[2])]
        return contract.json_bytes(self.api(route))

    def request(self, url, **kwargs):
        assert kwargs == {"github": True, "accept": "application/octet-stream"}
        prefix = f"https://api.github.com/repos/{REPO}/actions/artifacts/"
        assert url.startswith(prefix) and url.endswith("/zip")
        return self.archives[int(url.removeprefix(prefix).split("/")[0])]

    def retain(self, report):
        self.releases = [{
            "tag_name": "v0.3.1",
            "assets": [{"id": 98, "name": "release-receipt.json"}],
        }]
        self.assets[98] = receipts.canonical(report)

    def download(self, command, *, check):
        assert check is True and command[:3] == ["gh", "run", "download"]
        publisher = self.report["deploy"]["run_id"]
        assert command[3:7] == [str(publisher), "--repo", REPO, "--name"]
        assert command[8] == "--dir" and len(command) == 10
        name = command[7]
        self.downloads.append(name)
        if name not in self.bundles:
            raise subprocess.CalledProcessError(1, command, stderr=b"artifact not found")
        directory = Path(command[9])
        for filename, raw in self.bundles[name].items():
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)


@pytest.fixture(params=[(930, 2), (1930, 11)], ids=["attempt-2", "attempt-11"])
def report_job(tmp_path, monkeypatch, request):
    run_id, attempt = request.param
    report = contract.object_json((FIXTURES / "sample-release-receipt.json").read_bytes())
    report.update(attempt=run_id, sequence=attempt)
    report["deploy"]["run_attempt"] = attempt + 2
    observed = ReportObservations(report)
    for key, value in {
        "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": str(run_id),
        "GITHUB_RUN_ATTEMPT": str(attempt), "FAILED": "false", "STAGE": "publication",
        "RELEASE_ALERT_OWNER": "release-owner",
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(automation, "api", observed.consumer_api)
    monkeypatch.setattr(reporting, "api", observed.consumer_api)
    monkeypatch.setattr(contract, "gh_bytes", observed.gh_bytes)
    monkeypatch.setattr(evidence, "gh_bytes", observed.gh_bytes)
    monkeypatch.setattr(publishing, "gh_bytes", observed.gh_bytes)
    monkeypatch.setattr(reporting, "gh_bytes", observed.gh_bytes)
    monkeypatch.setattr(reporting.subprocess, "run", observed.download)
    return {
        "root": tmp_path, "report": report, "observed": observed,
        "event": contract.object_json((FIXTURES / "ready-event.json").read_bytes()),
        "policy": contract.object_json((FIXTURES / "receipt-policy.json").read_bytes())["data"],
    }


def write_stage(job, *, report=None, attempt=None, suffix="50-verify"):
    if report is None:
        report = job["report"]
    if attempt is None:
        attempt = job["report"]["sequence"]
    path = job["root"] / "receipts" / f"release-stage-{attempt}-{suffix}" / "release-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contract.json_bytes(report))
    return path


def run_report(job, monkeypatch, command="collect"):
    monkeypatch.setattr(sys, "argv", [
        "release_chain.reporting", command,
        "--directory", str(job["root"] / "receipts"),
        "--receipt", str(job["root"] / "release-receipt.json"),
    ])
    reporting.main()


def assert_no_delivery(job):
    assert not (job["root"] / "release-receipt.json").exists()
    assert not (job["root"] / "outputs").exists()
    assert not (job["root"] / "summary").exists()
    assert job["observed"].writes == job["observed"].downloads == []


def test_report_collect_refuses_absent_snapshot(report_job, monkeypatch):
    with pytest.raises(ValueError, match="^release-snapshot-missing:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)


def test_report_collect_refuses_receiptless_stage(report_job, monkeypatch):
    report = report_job["report"]
    (report_job["root"] / "receipts" / f"release-stage-{report['sequence']}-20-prepare").mkdir(
        parents=True
    )
    with pytest.raises(ValueError, match="^release-snapshot-missing:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)


@pytest.mark.parametrize("offset", [-1, 1], ids=["previous", "later"])
def test_report_collect_refuses_other_attempt_snapshot(report_job, monkeypatch, offset):
    old = copy.deepcopy(report_job["report"])
    old["sequence"] += offset
    write_stage(report_job, report=old, attempt=old["sequence"])
    with pytest.raises(ValueError, match="^release-snapshot-attempt-mismatch:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)


@pytest.mark.parametrize("field", ["attempt", "sequence"], ids=["wrong-run", "wrong-attempt"])
@pytest.mark.parametrize("failed", [False, True], ids=["success-path", "failure-path"])
def test_report_collect_refuses_relabelled_snapshot(report_job, monkeypatch, field, failed):
    changed = copy.deepcopy(report_job["report"])
    changed[field] += 1
    write_stage(report_job, report=changed)
    monkeypatch.setenv("FAILED", str(failed).lower())
    with pytest.raises(ValueError, match="^release-snapshot-attempt-mismatch:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)


@pytest.mark.parametrize("damage", ["json", "fields", "target", "schema", "consumer", "stage"])
def test_report_collect_refuses_invalid_snapshot(report_job, monkeypatch, damage):
    changed = copy.deepcopy(report_job["report"])
    if damage == "fields":
        changed["extra"] = "unregistered"
    elif damage == "target":
        changed.update(event_id=None, release_version=None)
    elif damage == "schema":
        changed["schema"] = "pineforge/consumer-signal/v1"
    elif damage == "consumer":
        changed["consumer"] = "web"
    path = write_stage(
        report_job, report=changed, suffix="50-unregistered" if damage == "stage" else "50-verify"
    )
    if damage == "json":
        path.write_bytes(b"{")
    with pytest.raises(ValueError, match="^release-snapshot-invalid:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)


def test_report_collect_does_not_substitute_retained_receipt(report_job, monkeypatch):
    report_job["observed"].retain(report_job["report"])
    with pytest.raises(ValueError, match="^release-snapshot-missing:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)
    assert report_job["observed"].reads == []


def test_report_collect_preserves_current_snapshot_and_all_pins(report_job, monkeypatch):
    report = report_job["report"]
    for offset in (-1, 1):
        unrelated = copy.deepcopy(report)
        unrelated["sequence"] += offset
        unrelated["deployed"]["facts_sha256"] = contract.sha256(str(offset).encode())
        write_stage(report_job, report=unrelated, attempt=unrelated["sequence"])
    progress = {**copy.deepcopy(report), "state": "deploying"}
    write_stage(report_job, report=progress, suffix="20-prepare")
    write_stage(report_job)
    run_report(report_job, monkeypatch)
    raw = (report_job["root"] / "release-receipt.json").read_bytes()
    assert raw == receipts.canonical(report)
    assert (report_job["root"] / "outputs").read_text() == "has_event=true\n"
    assert "**verified**" in (report_job["root"] / "summary").read_text()
    observed = report_job["observed"]
    public = hub.validate_receipt(
        observed, REPO, contract.object_json(raw),
        report_job["event"], report_job["policy"], observed.reporter,
    )
    assert public["state"] == "verified"
    for name in ("pr", "preview", "deployed", "previous"):
        assert public[name] == report[name]
    artifact_id = report["attempt"] * 100
    observed.archives[artifact_id] = receipt_archive(contract.object_json(raw))
    observed.artifacts[report["attempt"]] = [{
        "id": artifact_id, "name": receipts.ARTIFACT, "expired": False,
        "digest": "sha256:" + contract.sha256(observed.archives[artifact_id]),
    }]
    assert hub.fetch_artifact(observed, REPO, observed.reporter, report_job["policy"]) == report


def test_report_collect_preserves_retained_completion(report_job, monkeypatch):
    observed, report = report_job["observed"], report_job["report"]
    saved = copy.deepcopy(report)
    saved.update(attempt=report["attempt"] - 100, sequence=1)
    observed.retain(saved)
    retained = observed.assets[98]
    write_stage(report_job)
    run_report(report_job, monkeypatch)
    collected = contract.object_json(
        (report_job["root"] / "release-receipt.json").read_bytes()
    )
    assert collected == report
    reporting.retain(REPO, collected)
    assert observed.assets[98] == retained == receipts.canonical(saved)
    public = hub.validate_receipt(
        observed, REPO, collected, report_job["event"], report_job["policy"], observed.reporter
    )
    assert public["state"] == "verified"
    assert observed.downloads == []
    assert all(endpoint.startswith("issues") for endpoint, _method, _data in observed.writes)


def test_report_collect_failed_current_snapshot_preserves_pins(report_job, monkeypatch):
    write_stage(report_job)
    monkeypatch.setenv("FAILED", "true")
    run_report(report_job, monkeypatch)
    report = contract.object_json((report_job["root"] / "release-receipt.json").read_bytes())
    assert report["state"] == "failed"
    expected = {**report_job["report"], "state": "failed", "timestamp": report["timestamp"]}
    assert report == expected
    assert report_job["observed"].issues[0]["state"] == "open"


@pytest.mark.parametrize("slot", ["event_id", "pr", "deployed", "previous"])
def test_retained_completion_rejects_pin_drift(report_job, slot):
    changed = copy.deepcopy(report_job["report"])
    if slot == "event_id":
        changed[slot] = "sha256:" + contract.sha256(b"other release")
    elif slot == "pr":
        changed[slot]["head"] = contract.sha256(b"other PR head")[:40]
    else:
        changed[slot]["facts_sha256"] = contract.sha256(slot.encode())
    observed = report_job["observed"]
    observed.retain(changed)
    with pytest.raises(ValueError, match="^immutable completion receipt differs$"):
        reporting.retain(REPO, report_job["report"])
    assert observed.downloads == observed.writes == []


@pytest.mark.parametrize(
    ("slot", "cause"),
    [
        ("event_id", "receipt event differs"),
        ("pr", "PR base differs from policy"),
        ("ci", "checks ran against a different source"),
        ("e2e", "reported checks differ from GitHub"),
        ("deploy", "required check inventory differs from owner policy"),
        ("preview", "preview did not exercise the candidate source/image"),
        ("deployed", "actual served source/image differs"),
        ("previous", "malformed identity"),
    ],
    ids=["event", "pr", "ci", "e2e", "deploy", "preview", "deployed", "previous"],
)
def test_hub_validator_consumes_varied_receipt_pins(report_job, slot, cause):
    changed = copy.deepcopy(report_job["report"])
    other_sha = contract.sha256(slot.encode())
    if slot == "event_id":
        changed[slot] = "sha256:" + other_sha
    elif slot == "pr":
        changed[slot]["base"] = "dev"
    elif slot == "ci":
        changed[slot]["sha"] = other_sha[:40]
    elif slot == "e2e":
        changed[slot]["results"][changed[slot]["expected"][0]] = "failure"
    elif slot == "deploy":
        changed[slot]["approved_skips"] = []
    elif slot == "preview":
        changed[slot]["source"] = other_sha[:40]
    elif slot == "deployed":
        changed[slot]["image_digest"] = "sha256:" + other_sha
    else:
        changed[slot]["facts_sha256"] = "invalid"
    observed = report_job["observed"]
    with pytest.raises(hub.Invalid, match=cause):
        hub.validate_receipt(
            observed, REPO, changed, report_job["event"], report_job["policy"], observed.reporter
        )


@pytest.mark.parametrize("snapshot", ["absent", "present", "invalid"])
def test_incident_command_cannot_enable_receipt_transport(report_job, monkeypatch, snapshot):
    if snapshot != "absent":
        path = write_stage(report_job)
        if snapshot == "invalid":
            path.write_bytes(b"{")
    monkeypatch.setenv("FAILED", "true")
    run_report(report_job, monkeypatch, "incident")
    assert not (report_job["root"] / "release-receipt.json").exists()
    assert (report_job["root"] / "outputs").read_text() == "has_event=false\n"
    assert "**failed**" in (report_job["root"] / "summary").read_text()
    assert report_job["observed"].issues[0]["state"] == "open"
    assert report_job["observed"].downloads == []


@pytest.fixture
def publisher_job(report_job):
    observed, report = report_job["observed"], report_job["report"]
    run_id, attempt = report["deploy"]["run_id"], report["deploy"]["run_attempt"]
    staged = {
        **copy.deepcopy(report), "attempt": run_id, "sequence": attempt, "state": "deploying",
    }
    artifact_id = run_id * 100
    observed.archives[artifact_id] = receipt_archive(staged)
    observed.artifacts[run_id] = [{
        "id": artifact_id, "name": f"release-stage-{attempt}-50-verify", "expired": False,
    }]
    report_job.update(publisher=run_id, publisher_attempt=attempt, staged=staged)
    return report_job


@pytest.mark.parametrize("inventory", ["absent", "previous", "later", "expired", "unregistered"])
def test_publication_refuses_missing_attempt_snapshot(publisher_job, inventory):
    job = publisher_job
    observed = job["observed"]
    rows = observed.artifacts[job["publisher"]]
    if inventory == "absent":
        rows.clear()
    elif inventory in {"previous", "later"}:
        offset = -1 if inventory == "previous" else 1
        rows[0]["name"] = f"release-stage-{job['publisher_attempt'] + offset}-50-verify"
    elif inventory == "expired":
        rows[0]["expired"] = True
    else:
        rows[0]["name"] = f"release-stage-{job['publisher_attempt']}-50-unregistered"
    report = receipts.receipt(None)
    before = copy.deepcopy(report)
    with pytest.raises(ValueError, match="^release-snapshot-missing:"):
        reporting.publication(REPO, job["publisher"], report)
    assert report == before
    assert observed.downloads == observed.writes == []


@pytest.mark.parametrize("field", ["attempt", "sequence"], ids=["wrong-run", "wrong-attempt"])
def test_publication_refuses_relabelled_snapshot(publisher_job, field):
    job = publisher_job
    changed = copy.deepcopy(job["staged"])
    changed[field] += 1
    job["observed"].archives[job["publisher"] * 100] = receipt_archive(changed)
    report = receipts.receipt(None)
    before = copy.deepcopy(report)
    with pytest.raises(ValueError, match="^release-snapshot-attempt-mismatch:"):
        reporting.publication(REPO, job["publisher"], report)
    assert report == before
    assert job["observed"].downloads == job["observed"].writes == []


def test_publication_preserves_current_manual_progress(publisher_job):
    job = publisher_job
    observed = job["observed"]
    progress = copy.deepcopy(job["staged"])
    progress.update(
        state="pr_open", ci=None, e2e=None, deploy=None,
        preview=None, deployed=None, previous=None,
    )
    progress["pr"]["merge"] = None
    observed.archives[job["publisher"] * 100] = receipt_archive(progress)
    observed.artifacts[job["publisher"]][0]["name"] = (
        f"release-stage-{job['publisher_attempt']}-10-manual"
    )
    report = receipts.receipt(None)
    reporting.publication(REPO, job["publisher"], report)
    expected = {
        **progress, "attempt": job["report"]["attempt"], "sequence": job["report"]["sequence"],
        "timestamp": report["timestamp"],
    }
    assert report == expected
    assert observed.downloads == observed.writes == []


@pytest.mark.parametrize("inventory", ["absent", "previous", "later"])
def test_publication_refuses_unavailable_attempt_bundle(publisher_job, inventory):
    job = publisher_job
    observed = job["observed"]
    if inventory != "absent":
        offset = -1 if inventory == "previous" else 1
        observed.bundles[f"validated-release-bundle-{job['publisher_attempt'] + offset}"] = {
            "evidence.json": contract.json_bytes({"run_id": 80}),
        }
    with pytest.raises(ValueError, match="^release-bundle-unavailable:"):
        reporting.publication(REPO, job["publisher"], receipts.receipt(None))
    assert observed.downloads == [f"validated-release-bundle-{job['publisher_attempt']}"]
    assert observed.writes == []


@pytest.mark.parametrize("contents", ["missing-evidence", "invalid-evidence"])
def test_publication_refuses_invalid_bundle(publisher_job, contents):
    job = publisher_job
    observed = job["observed"]
    observed.bundles[f"validated-release-bundle-{job['publisher_attempt']}"] = (
        {} if contents == "missing-evidence" else {"evidence.json": b"{"}
    )
    with pytest.raises(ValueError, match="^release-bundle-invalid:"):
        reporting.publication(REPO, job["publisher"], receipts.receipt(None))
    assert observed.writes == []


class MainGateBoundary(RuntimeError):
    """The bundle was read; full main-proof validation must run next."""


def test_publication_current_bundle_reaches_main_gate(publisher_job, monkeypatch):
    job = publisher_job
    observed = job["observed"]
    main = {"run_id": job["report"]["attempt"] - 1}
    bundle = f"validated-release-bundle-{job['publisher_attempt']}"
    observed.bundles[bundle] = {"evidence.json": contract.json_bytes(main)}
    calls = []

    def before_main_gate(repo, run_id, *, head):
        calls.append((repo, run_id, head))
        raise MainGateBoundary("before full main-proof gate")

    monkeypatch.setattr(reporting, "gate_for_run", before_main_gate)
    report = receipts.receipt(None)
    with pytest.raises(MainGateBoundary, match="^before full main-proof gate$"):
        reporting.publication(REPO, job["publisher"], report)
    assert observed.downloads == [bundle]
    assert calls == [(REPO, main["run_id"], job["report"]["pr"]["merge"])]
    assert report["state"] == "deploying"
    assert report["deploy"] == job["staged"]["deploy"]
    assert observed.writes == []


def test_reporting_workflow_requires_collection_before_transport():
    workflow = yaml.load(
        (Path(__file__).parents[1] / ".github/workflows/release-report.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["report"]["steps"]
    collect = next(step for step in steps if step.get("id") == "receipt")
    assert collect["run"] == "python -m release_chain.reporting collect"
    assert "if" not in collect and "continue-on-error" not in collect
    download = next(step for step in steps if "actions/download-artifact@" in step.get("uses", ""))
    assert download["with"]["pattern"] == "release-stage-${{ github.run_attempt }}-*"
    upload = next(step for step in steps if step.get("id") == "upload")
    assert upload["if"] == "inputs.hub && steps.receipt.outputs.has_event == 'true'"
    alert = steps[-1]
    assert alert["if"] == "failure()"
    assert alert["run"] == (
        "python -m release_chain.reporting incident --receipt incident-release-receipt.json"
    )
