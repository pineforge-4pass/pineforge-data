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
import release_adapter
import yaml
from release_chain import automation, contract, evidence, publishing, receipts, reporting

FIXTURES = Path(__file__).parent / "fixtures/release_chain"
REPO = "owner/data"
HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40


def receipt_archive(report):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("release-receipt.json", receipts.canonical(report))
    return archive.getvalue()


class ReportObservations:
    """Mutable GitHub responses and retained bytes, never a validation result."""

    def __init__(self, run_id, attempt):
        # These observations are installed before the receipt is loaded or mutated.
        # Never derive API answers from the receipt under test.
        self.event = contract.object_json((FIXTURES / "ready-event.json").read_bytes())
        self.reads, self.writes, self.downloads = [], [], []
        self.issues, self.releases = [], []
        self.archives, self.assets, self.artifacts = {}, {}, {}
        self.bundles = {}
        self.open_prs = []
        self.hub_releases = []
        self.pr = {
            "number": 7, "merged": True,
            "merged_at": "2020-01-01T00:05:00Z", "merge_commit_sha": MERGE,
            "base": {"ref": "main", "sha": BASE},
            "head": {"sha": HEAD, "repo": {"full_name": REPO}},
            "body": automation.body({
                "kind": "release", "version": "0.3.1", "generated_head": HEAD, "base": BASE,
            }),
        }
        self.runs, self.jobs = {}, {}
        for observed_id, observed_attempt, workflow, source, names in (
            (70, 2, "ci.yml", HEAD, receipts.CI_JOBS),
            (200, attempt + 2, "release.yml", MERGE, receipts.DEPLOY_JOBS),
        ):
            self.runs[observed_id] = {
                "id": observed_id, "run_attempt": observed_attempt,
                "path": ".github/workflows/" + workflow, "head_sha": source,
                "event": "workflow_run" if observed_id == 200 else "pull_request",
                "status": "completed", "conclusion": "success",
            }
            finished = "2020-01-01T00:06:00Z" if observed_id == 200 else "2020-01-01T00:04:00Z"
            self.jobs[observed_id] = [
                {"name": name, "status": "completed", "conclusion": "success",
                 "completed_at": finished}
                for name in names
            ]
        self.reporter = {
            "id": run_id, "run_attempt": attempt,
            "path": ".github/workflows/" + receipts.WORKFLOW,
            "head_sha": MERGE, "event": "workflow_run",
        }
        self.runs[run_id] = self.reporter
        self.jobs[run_id] = [{"name": "reconcile", "status": "completed", "conclusion": "success"}]
        self.installed = {}

    def api(self, route):
        self.reads.append(route)
        path = route.split("?", 1)[0].removeprefix(f"repos/{REPO}/")
        if path == "issues":
            return copy.deepcopy(self.issues)
        if path == "releases":
            return copy.deepcopy(self.releases)
        if path == "pulls":
            return copy.deepcopy(self.open_prs)
        if path == f"pulls/{self.pr['number']}":
            return copy.deepcopy(self.pr)
        if path == f"commits/{self.pr['merge_commit_sha']}":
            return {"sha": MERGE, "parents": [{"sha": BASE}]}
        if path == f"commits/{BASE}":
            return {"sha": BASE, "parents": []}
        if path == f"commits/{MERGE}/pulls":
            return []  # Independent ordinary-main observation; tests may replace the PR route.
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
        if route.startswith(f"repos/{contract.HUB}/releases?"):
            return contract.json_bytes(self.hub_releases)
        if route == f"repos/{contract.HUB}/releases/tags/v{self.event['release_version']}":
            return contract.json_bytes({
                "draft": False, "prerelease": False,
                "assets": [{"id": 81, "name": "release-manifest.json"}],
            })
        if route == f"repos/{contract.HUB}/releases/assets/81":
            return (FIXTURES / "release-manifest.json").read_bytes()
        path = route.removeprefix(f"repos/{REPO}/")
        if path.startswith("contents/"):
            return self.installed[route]
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
        publisher = 200
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
    observed = ReportObservations(run_id, attempt)
    report = contract.object_json((FIXTURES / "sample-release-receipt.json").read_bytes())
    report.update(attempt=run_id, sequence=attempt)
    report["deploy"]["run_attempt"] = attempt + 2
    for key, value in {
        "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": str(run_id),
        "GITHUB_RUN_ATTEMPT": str(attempt), "FAILED": "false", "STAGE": "publication",
        "GITHUB_SHA": MERGE,
        "RELEASE_ALERT_OWNER": "release-owner",
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(automation, "api", observed.consumer_api)
    monkeypatch.setattr(publishing, "api", observed.consumer_api)
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


def register_stage(job, attempt, suffix):
    observed = job["observed"]
    run_id = observed.reporter["id"]
    name = f"release-stage-{attempt}-{suffix}"
    rows = observed.artifacts.setdefault(run_id, [])
    if not any(row["name"] == name for row in rows):
        rows.append({"id": len(rows) + 50000, "name": name, "expired": False})
    return job["root"] / "receipts" / name


def write_stage(job, *, report=None, attempt=None, suffix="10-adapter"):
    if report is None:
        report = job["report"]
    if attempt is None:
        attempt = job["observed"].reporter["run_attempt"]
    path = register_stage(job, attempt, suffix) / "release-receipt.json"
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


def ship_producer(job, directory, suffix):
    """Model upload/download: an empty producer directory creates no artifact."""
    names = ("release-receipt.json", "producer-outcome.json")
    files = [directory / name for name in names if (directory / name).exists()]
    if not files:
        return None
    destination = register_stage(job, job["observed"].reporter["run_attempt"], suffix)
    destination.mkdir(parents=True, exist_ok=True)
    for path in files:
        (destination / path.name).write_bytes(path.read_bytes())
    return destination


def archive_stage(directory):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in sorted(directory.iterdir()):
            zipped.writestr(path.name, path.read_bytes())
    return archive.getvalue()


def ordinary_producer(job, monkeypatch, workflow="ci-post.yml"):
    observed = job["observed"]
    observed.reporter["path"] = ".github/workflows/release.yml"
    observed.jobs[observed.reporter["id"]] = [
        {"name": "prepare", "status": "completed", "conclusion": "success"},
    ]
    observed.runs[810] = {
        "id": 810, "run_attempt": 3, "path": ".github/workflows/" + workflow,
        "event": "push", "head_branch": "main", "head_sha": MERGE,
        "status": "completed", "conclusion": "success",
    }
    directory = job["root"] / "producer"
    with monkeypatch.context() as producer:
        producer.setenv("GITHUB_EVENT_NAME", "workflow_run")
        producer.setenv("GITHUB_OUTPUT", str(job["root"] / "producer-outputs"))
        producer.setattr(sys, "argv", [
            "release_chain.publishing", "prepare", "--run", "810",
            "--directory", str(job["root"] / "bundle"),
            "--receipt", str(directory / "release-receipt.json"),
        ])
        publishing.main()
    return directory


def assert_silent_noop(job):
    assert not (job["root"] / "release-receipt.json").exists()
    assert (job["root"] / "outputs").read_text() == "has_event=false\n"
    assert job["observed"].writes == job["observed"].downloads == []
    assert job["observed"].issues == []


@pytest.mark.parametrize("workflow", ["ci-post.yml", "native-feed-post.yml"])
def test_cli_ordinary_main_actual_producer_is_silent(report_job, monkeypatch, workflow):
    job = report_job
    produced = ordinary_producer(job, monkeypatch, workflow)
    ship_producer(job, produced, "20-prepare")
    run_report(job, monkeypatch)
    assert_silent_noop(job)
    assert not (produced / "release-receipt.json").exists()
    witness = contract.object_json((produced / "producer-outcome.json").read_bytes())
    assert witness["reason"] == "ordinary-main"
    assert witness["inputs"] == {"ci_run": 810, "ci_attempt": 3, "source": MERGE}
    assert any("/actions/runs/810" in route for route in job["observed"].reads)


def complete_no_target_publisher(job, monkeypatch, produced, suffix):
    observed = job["observed"]
    publisher_id = observed.reporter["id"]
    stage = ship_producer(job, produced, suffix)
    observed.reporter.update(status="completed", conclusion="success")
    if stage is not None:
        row = observed.artifacts[publisher_id][0]
        observed.archives[row["id"]] = archive_stage(stage)
        (job["root"] / "receipts").rename(job["root"] / "publisher-downloads")
    observed.reporter = {
        "id": 3100, "run_attempt": 7, "path": ".github/workflows/pineforge-release.yml",
        "event": "workflow_run", "head_sha": MERGE,
    }
    observed.runs[3100] = observed.reporter
    observed.jobs[3100] = [{
        "name": "reconcile", "status": "completed", "conclusion": "success",
    }]
    monkeypatch.setenv("GITHUB_RUN_ID", "3100")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "7")
    adapter_directory = job["root"] / "adapter-producer"
    monkeypatch.setattr(sys, "argv", [
        "release_chain.reporting", "publication", "--run", str(publisher_id),
        "--receipt", str(adapter_directory / "release-receipt.json"),
    ])
    reporting.main()
    ship_producer(job, adapter_directory, "10-adapter")
    run_report(job, monkeypatch)
    assert_silent_noop(job)
    assert not (adapter_directory / "release-receipt.json").exists()
    assert any(f"/actions/runs/{publisher_id}/artifacts" in route for route in observed.reads)


@pytest.mark.parametrize("workflow", ["ci-post.yml", "native-feed-post.yml"])
def test_cli_publication_and_adapter_report_preserve_actual_no_target(
    report_job, monkeypatch, workflow
):
    produced = ordinary_producer(report_job, monkeypatch, workflow)
    complete_no_target_publisher(report_job, monkeypatch, produced, "20-prepare")


@pytest.mark.parametrize("damage", [
    "missing", "json", "run", "attempt", "head", "reason", "inputs", "input-source",
    "expired", "unregistered", "not-downloaded", "producer-skipped", "api-attempt",
])
def test_cli_no_target_requires_current_bound_producer_evidence(report_job, monkeypatch, damage):
    job = report_job
    produced = ordinary_producer(job, monkeypatch)
    # Do not depend on a new Python API: defective-first reaches the real collect command.
    if damage == "missing":
        register_stage(job, job["observed"].reporter["run_attempt"], "20-prepare").mkdir(
            parents=True)
    else:
        stage = ship_producer(job, produced, "20-prepare")
        if stage is not None:
            path = stage / "producer-outcome.json"
            if damage == "json":
                path.write_bytes(b"{")
            elif damage in {"run", "attempt", "head", "reason", "inputs", "input-source"}:
                value = contract.object_json(path.read_bytes())
                if damage == "run":
                    value["run_id"] += 1
                elif damage == "attempt":
                    value["run_attempt"] += 1
                elif damage == "head":
                    value["head_sha"] = "9" * 40
                elif damage == "reason":
                    value["reason"] = "unregistered"
                elif damage == "inputs":
                    value["inputs"] = {"ignored": True}
                else:
                    value["inputs"]["source"] = "9" * 40
                path.write_bytes(contract.json_bytes(value))
            elif damage == "not-downloaded":
                # Download a different directory, preserving the API's current row.
                stage.rename(stage.with_name("unregistered-download"))
    rows = job["observed"].artifacts.get(job["observed"].reporter["id"], [])
    if rows and damage == "expired":
        rows[0]["expired"] = True
    elif rows and damage == "unregistered":
        rows[0]["name"] += "-unknown"
    elif damage == "producer-skipped":
        job["observed"].jobs[job["observed"].reporter["id"]][0]["conclusion"] = "skipped"
    elif damage == "api-attempt":
        job["observed"].reporter["run_attempt"] += 1
    with pytest.raises(ValueError):
        run_report(job, monkeypatch)
    assert_no_delivery(job)


def test_cli_report_only_rerun_cannot_reuse_prior_no_target_decision(report_job, monkeypatch):
    job = report_job
    produced = ordinary_producer(job, monkeypatch)
    ship_producer(job, produced, "20-prepare")
    # The download action in the new attempt fetches no prior-attempt directories.
    downloaded = job["root"] / "receipts"
    if downloaded.exists():
        downloaded.rename(job["root"] / "prior-downloads")
    old = job["observed"].reporter["run_attempt"]
    job["observed"].reporter["run_attempt"] = old + 1
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", str(old + 1))
    with pytest.raises(ValueError, match="^release-snapshot-attempt-mismatch:"):
        run_report(job, monkeypatch)
    assert_no_delivery(job)


@pytest.mark.parametrize("entry", ["collect", "publication"])
def test_cli_later_no_target_cannot_erase_a_current_target(report_job, monkeypatch, entry):
    job = report_job
    observed = job["observed"]
    produced = ordinary_producer(job, monkeypatch)
    ship_producer(job, produced, "20-prepare")
    # Keep an earlier, independently registered targeted producer snapshot.
    current = copy.deepcopy(job["report"])
    write_stage(job, report=current, suffix="10-manual")
    observed.jobs[observed.reporter["id"]].append({
        "name": "manual-pr", "status": "completed", "conclusion": "success",
    })
    if entry == "collect":
        with pytest.raises(ValueError, match="stage decisions disagree"):
            run_report(job, monkeypatch)
    else:
        publisher_id = observed.reporter["id"]
        observed.reporter.update(status="completed", conclusion="success")
        for row in observed.artifacts[publisher_id]:
            observed.archives[row["id"]] = archive_stage(
                job["root"] / "receipts" / row["name"]
            )
        monkeypatch.setattr(sys, "argv", [
            "release_chain.reporting", "publication", "--run", str(publisher_id),
            "--receipt", str(job["root"] / "release-receipt.json"),
        ])
        with pytest.raises(ValueError, match="stage decisions disagree"):
            reporting.main()
    assert_no_delivery(job)


@pytest.mark.parametrize("failed", [False, True])
def test_cli_failure_input_is_checked_against_producer_jobs(report_job, monkeypatch, failed):
    job = report_job
    write_stage(job)
    monkeypatch.setenv("FAILED", str(failed).lower())
    observed = job["observed"]
    observed.jobs[observed.reporter["id"]][0]["conclusion"] = "success" if failed else "failure"
    with pytest.raises(ValueError, match="failure input differs"):
        run_report(job, monkeypatch)
    assert_no_delivery(job)


def test_cli_unreadable_download_still_reaches_diagnostic_incident(report_job, monkeypatch):
    job = report_job
    path = register_stage(job, job["observed"].reporter["run_attempt"], "10-adapter")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a directory")
    monkeypatch.setenv("FAILED", "true")
    job["observed"].jobs[job["observed"].reporter["id"]][0]["conclusion"] = "failure"
    run_report(job, monkeypatch, "incident")
    assert not (job["root"] / "release-receipt.json").exists()
    assert (job["root"] / "outputs").read_text() == "has_event=false\n"
    assert job["observed"].issues[0]["state"] == "open"


def pre_manifest_files():
    pin = (
        "ghcr.io/pineforge-4pass/pineforge-release:1.0.0@sha256:"
        "b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86"
    )
    return {
        contract.RUNTIME: f'DEFAULT_RELEASE_IMAGE = "{pin}"\n'.encode(),
        contract.DOCKERFILE: f"ARG PINEFORGE_RELEASE_IMAGE={pin}\n".encode(),
        "pyproject.toml": b'[project]\nname = "pineforge-data"\nversion = "0.3.0"\n',
    }


def snapshot_files(files):
    def snapshot(revision, directory):
        assert revision == BASE
        for name, raw in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    return snapshot


@pytest.mark.parametrize("bump,forward", [
    (None, False), ("patch", False), ("minor", False), ("major", False),
    ("patch", True), ("minor", True), ("major", True),
])
def test_cli_reconcile_actual_targetless_decisions(report_job, monkeypatch, bump, forward):
    job = report_job
    observed = job["observed"]
    expected_versions = {"patch": "0.3.1", "minor": "0.4.0", "major": "1.0.0"}
    if bump:
        observed.reporter["path"] = ".github/workflows/release.yml"
        observed.reporter["event"] = "workflow_dispatch"
        observed.jobs[observed.reporter["id"]] = [
            {"name": "manual-pr", "status": "completed", "conclusion": "success"},
        ]
    created_patch = {}

    def git(*args):
        if args[0] == "diff":
            assert args == ("diff", "--name-only", BASE, HEAD)
            return "\n".join(created_patch)
        if args[0] == "fetch":
            if args != ("fetch", "origin", "main"):
                assert args == (
                    "fetch", "origin", "main", "refs/heads/" + observed.pr["head"]["ref"],
                )
            return ""
        assert args in {("rev-parse", "origin/main"), ("merge-base", BASE, HEAD)}
        return BASE

    def source_bytes(command):
        assert command == ["git", "show", f"{HEAD}:pyproject.toml"]
        return created_patch["pyproject.toml"]

    monkeypatch.setattr(automation, "git", git)
    monkeypatch.setattr(automation.subprocess, "check_output", source_bytes)
    monkeypatch.setattr(automation, "snapshot", snapshot_files(pre_manifest_files()))

    def create_pr(repo, base, patch, branch, meta, bot):
        # Replace the remote creation boundary; consume the real deterministic patch.
        assert repo == REPO and base == BASE and bot == "release[bot]"
        assert patch["pyproject.toml"].count(expected_versions[bump].encode()) == 1
        created_patch.update(patch)
        assert branch.startswith("bot/pineforge-package/main/")
        meta.update(base=BASE, generated_head=HEAD)
        observed.pr.update(
            merged=False, merge_commit_sha=None,
            user={"login": bot}, state="open", body=automation.body(meta),
        )
        observed.pr["head"]["ref"] = branch
        return copy.deepcopy(observed.pr)

    monkeypatch.setattr(automation, "create_pr", create_pr)
    real_api = observed.consumer_api

    def api(repo, endpoint, method="GET", data=None):
        if endpoint.startswith("actions/workflows/ci.yml/runs?"):
            return {"workflow_runs": []}
        return real_api(repo, endpoint, method, data)

    monkeypatch.setattr(automation, "api", api)
    event_path = job["root"] / "event.json"
    event_path.write_bytes(b"{}")
    produced = job["root"] / "producer"
    with monkeypatch.context() as producer:
        producer.setenv("GITHUB_EVENT_PATH", str(event_path))
        producer.setenv("RELEASE_BOT_LOGIN", "release[bot]")
        argv = ["release_adapter.py", "manual" if bump else "reconcile"]
        if bump:
            argv += ["--bump", bump]
        producer.setattr(sys, "argv", argv + [
            "--receipt", str(produced / "release-receipt.json"),
        ])
        release_adapter.main()
    if forward:
        complete_no_target_publisher(job, monkeypatch, produced, "10-manual")
    else:
        ship_producer(job, produced, "10-manual" if bump else "10-adapter")
        run_report(job, monkeypatch)
        assert_silent_noop(job)
    witness = contract.object_json((produced / "producer-outcome.json").read_bytes())
    assert witness["reason"] == ("package-bump" if bump else "no-ready-release")
    if bump:
        assert witness["inputs"]["version"] == expected_versions[bump]


def test_report_collect_refuses_absent_snapshot(report_job, monkeypatch):
    with pytest.raises(ValueError, match="^release-snapshot-missing:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)


def test_report_collect_refuses_receiptless_stage(report_job, monkeypatch):
    register_stage(report_job, report_job["observed"].reporter["run_attempt"], "10-adapter").mkdir(
        parents=True)
    with pytest.raises(ValueError, match="^release-snapshot-missing:"):
        run_report(report_job, monkeypatch)
    assert_no_delivery(report_job)


@pytest.mark.parametrize("offset", [-1, 1], ids=["previous", "later"])
def test_report_collect_refuses_other_attempt_snapshot(report_job, monkeypatch, offset):
    old = copy.deepcopy(report_job["report"])
    old["sequence"] += offset
    path = write_stage(report_job, report=old, attempt=old["sequence"])
    path.parent.rename(report_job["root"] / "withheld-other-attempt")
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
    if failed:
        report_job["observed"].jobs[report_job["observed"].reporter["id"]][0]["conclusion"] = "failure"
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
        report_job, report=changed, suffix="50-unregistered" if damage == "stage" else "10-adapter"
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
    assert not any("/releases" in route for route in report_job["observed"].reads)


def test_report_collect_preserves_current_snapshot_and_all_pins(report_job, monkeypatch):
    report = report_job["report"]
    # Match the download action: only the current attempt's stage directory exists.
    for offset in (-1, 1):
        report_job["observed"].artifacts.setdefault(report["attempt"], []).append({
            "id": 60000 + offset, "name": f"release-stage-{report['sequence'] + offset}-10-adapter",
            "expired": False,
        })
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
    report_job["observed"].jobs[report_job["observed"].reporter["id"]][0]["conclusion"] = "failure"
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
    publisher = observed.runs[200]
    run_id, attempt = publisher["id"], publisher["run_attempt"]
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
    cause = (
        "release-snapshot-attempt-mismatch:" if inventory in {"previous", "later"}
        else "release-snapshot-invalid:" if inventory == "unregistered"
        else "release-snapshot-missing:"
    )
    with pytest.raises(ValueError, match="^" + cause):
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


@pytest.mark.parametrize("fault", ["absent", "wrong-run", "wrong-attempt", "receiptless"])
def test_publication_cli_requires_current_target(publisher_job, monkeypatch, fault):
    job = publisher_job
    observed = job["observed"]
    if fault == "absent":
        observed.artifacts[job["publisher"]] = []
    else:
        changed = copy.deepcopy(job["staged"])
        if fault == "wrong-run":
            changed["attempt"] += 1
        elif fault == "wrong-attempt":
            changed["sequence"] += 1
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            if fault != "receiptless":
                zipped.writestr("release-receipt.json", receipts.canonical(changed))
        observed.archives[job["publisher"] * 100] = archive.getvalue()
    monkeypatch.setattr(sys, "argv", [
        "release_chain.reporting", "publication", "--run", str(job["publisher"]),
        "--receipt", str(job["root"] / "release-receipt.json"),
    ])
    with pytest.raises(ValueError):
        reporting.main()
    assert_no_delivery(job)


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
    observed.jobs[job["publisher"]].append({
        "name": "manual-pr", "status": "completed", "conclusion": "success",
    })
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
