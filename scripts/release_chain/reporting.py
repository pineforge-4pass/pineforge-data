"""Trusted receipt snapshots, private incidents, and the hub's four-key wakeup."""

from __future__ import annotations

import argparse
import copy
import io
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .automation import api, finish, inventory, marker, pr_identity
from .contract import (
    HUB,
    LOCK,
    gh_bytes,
    hub_manifest,
    json_bytes,
    object_json,
    require,
    sha256,
    verify_local,
)
from .evidence import gate_for_run, semantic_evidence_identity
from .publishing import candidate_gate, stored_receipt, verified_package
from .receipts import (
    DEPLOY_JOBS,
    DEPLOY_SKIPS,
    FIELDS,
    OUTCOME_FILE,
    OUTCOME_SCHEMA,
    ProducerReceipt,
    WORKFLOW,
    candidate,
    canonical,
    observe_outcome,
    receipt,
    signal,
    timestamp,
)

REPORT_STAGES = {"10-adapter", "10-manual", "20-prepare", "30-github", "40-publish", "50-verify"}
STAGE_JOBS = {
    "10-adapter": "reconcile", "10-manual": "manual-pr", "20-prepare": "prepare",
    "30-github": "github-release", "40-publish": "publish", "50-verify": "verify",
}
NO_TARGET_STAGES = {
    "ordinary-main": {"20-prepare"},
    "main-proofs-pending": {"20-prepare"},
    "package-publication": {"20-prepare", "30-github", "40-publish", "50-verify"},
    "package-bump": {"10-manual"},
    "no-ready-release": {"10-adapter"},
    "publication-handoff": {"10-adapter"},
    "publisher-handoff": {"10-adapter"},
    "reporter-handoff": {"10-adapter"},
    "publisher-no-target": {"10-adapter"},
}
NO_TARGET_INPUTS = {
    "ordinary-main": {"ci_run", "ci_attempt", "source"},
    "main-proofs-pending": {"ci_run", "ci_attempt", "source", "pending"},
    "package-publication": {"ci_run", "ci_attempt", "source", "version"},
    "package-bump": {"base", "bump", "version", "pr"},
    "no-ready-release": {"base"},
    "publication-handoff": {"publication_run", "version"},
    "publisher-handoff": {"publication_run", "ci_run", "version", "source"},
    "reporter-handoff": {"publication_run", "version", "source"},
    "publisher-no-target": {"publication_run", "publication_attempt", "outcome_sha256"},
}


def current_run(repo: str) -> dict[str, Any]:
    run_id, attempt = int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])
    run = api(repo, f"actions/runs/{run_id}")
    require(
        type(run.get("id")) is int and run["id"] == run_id > 0
        and type(run.get("run_attempt")) is int and run["run_attempt"] == attempt > 0
        and run.get("path") in {
            ".github/workflows/release.yml", ".github/workflows/" + WORKFLOW,
        }
        and run.get("event") in {"repository_dispatch", "workflow_run", "workflow_dispatch", "schedule"}
        and isinstance(run.get("head_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"]),
        "release-producer-run-mismatch: untrusted current run/attempt",
    )
    return run


def stage_rows(artifacts: list[Any], run: dict[str, Any]) -> list[dict[str, Any]]:
    prefix = f"release-stage-{run['run_attempt']}-"
    rows = [row for row in artifacts if row["name"].startswith(prefix)]
    require(
        rows or not any(row["name"].startswith("release-stage-") for row in artifacts),
        f"release-snapshot-attempt-mismatch: run {run['id']} attempt {run['run_attempt']}",
    )
    require(rows, f"release-snapshot-missing: run {run['id']} attempt {run['run_attempt']}")
    allowed = REPORT_STAGES - {"10-adapter"}
    if run["path"] == ".github/workflows/" + WORKFLOW:
        allowed = {"10-adapter"}
    require(
        all(row["name"][len(prefix):] in allowed for row in rows)
        and len({row["name"] for row in rows}) == len(rows),
        "release-snapshot-invalid: unregistered or duplicate stage",
    )
    require(
        all(row["expired"] is False for row in rows),
        "release-snapshot-missing: current stage expired",
    )
    return sorted(rows, key=lambda row: row["name"])


def producer_job(stage: str, jobs: list[Any]) -> dict[str, Any]:
    matches = [job for job in jobs if job["name"] == STAGE_JOBS[stage]]
    require(
        len(matches) == 1 and matches[0]["status"] == "completed"
        and matches[0]["conclusion"] in {"success", "failure", "cancelled"},
        "release-producer-job-mismatch: producer did not execute in this attempt",
    )
    return matches[0]


def publication_stage(
    repo: str, row: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    raw = gh_bytes(f"repos/{repo}/actions/artifacts/{row['id']}/zip")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
            names = zipped.namelist()
            require(
                bool(names) and len(names) == len(set(names))
                and set(names) <= {"release-receipt.json", OUTCOME_FILE},
                "release-snapshot-invalid: unexpected stage archive contents",
            )
            return (
                object_json(zipped.read("release-receipt.json"))
                if "release-receipt.json" in names else None,
                object_json(zipped.read(OUTCOME_FILE)) if OUTCOME_FILE in names else None,
            )
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        raise ValueError("release-snapshot-invalid: unreadable stage archive") from error


def checked_no_target(repo: str, run: dict[str, Any], outcome: dict[str, Any]) -> None:
    reason, inputs = outcome["reason"], outcome["inputs"]
    require(
        set(inputs) == NO_TARGET_INPUTS[reason],
        "release-producer-outcome-invalid: decision inputs differ",
    )
    for name, value in inputs.items():
        if name in {"ci_run", "ci_attempt", "publication_run", "publication_attempt", "pr"}:
            valid = type(value) is int and value > 0
        elif name in {"source", "base"}:
            valid = isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))
        elif name == "outcome_sha256":
            valid = isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
        elif name == "version":
            valid = isinstance(value, str) and bool(re.fullmatch(r"\d+\.\d+\.\d+", value))
        elif name == "bump":
            valid = isinstance(value, str) and value in {"patch", "minor", "major"}
        else:
            valid = isinstance(value, str) and bool(value)  # ProofPending's actual reason.
        require(valid, f"release-producer-outcome-invalid: invalid {name}")
    if reason in {"ordinary-main", "main-proofs-pending", "package-publication"}:
        ci = api(repo, f"actions/runs/{inputs['ci_run']}")
        require(
            ci.get("id") == inputs["ci_run"] and ci.get("head_sha") == inputs["source"]
            and ci.get("run_attempt") == inputs["ci_attempt"]
            and ci.get("event") == "push" and ci.get("head_branch") == "main"
            and ci.get("path") in {
                ".github/workflows/ci-post.yml", ".github/workflows/native-feed-post.yml",
            },
            "release-producer-outcome-invalid: observed main input differs",
        )
        prs = api(repo, f"commits/{inputs['source']}/pulls?per_page=100")
        matches = [
            pr for pr in prs
            if pr.get("merged_at") and pr["base"]["ref"] == "main"
            and pr["merge_commit_sha"] == inputs["source"] and marker(pr)
        ]
        if reason == "ordinary-main":
            require(
                not matches,
                "release-producer-outcome-invalid: main input has a release target",
            )
        else:
            require(
                len(matches) == 1,
                "release-producer-outcome-invalid: release PR input differs",
            )
            if reason == "package-publication":
                meta = marker(api(repo, f"pulls/{matches[0]['number']}"))
                require(
                    meta is not None and meta.get("kind") == "package"
                    and meta.get("event_id") is None and meta.get("version") == inputs["version"],
                    "release-producer-outcome-invalid: package publication PR differs",
                )
    elif reason == "package-bump":
        pr = api(repo, f"pulls/{inputs['pr']}")
        meta = marker(pr)
        require(
            meta is not None and meta.get("kind") == "package"
            and meta.get("base") == inputs["base"] and meta.get("bump") == inputs["bump"]
            and meta.get("version") == inputs["version"] and meta.get("event_id") is None,
            "release-producer-outcome-invalid: package PR differs",
        )
    elif reason == "no-ready-release":
        commit = api(repo, f"commits/{inputs['base']}")
        require(commit.get("sha") == inputs["base"], "release-producer-outcome-invalid: base differs")
    else:
        publisher = api(repo, f"actions/runs/{inputs['publication_run']}")
        require(
            publisher.get("id") == inputs["publication_run"]
            and publisher.get("path") == ".github/workflows/release.yml"
            and publisher.get("status") == "completed" and publisher.get("conclusion") == "success"
            and publisher.get("event") in {"workflow_run", "workflow_dispatch"},
            "release-producer-outcome-invalid: handoff publisher differs",
        )
        if reason == "publisher-no-target":
            require(
                publisher.get("run_attempt") == inputs["publication_attempt"],
                "release-producer-outcome-invalid: publisher attempt moved",
            )
            rows = stage_rows(inventory(
                repo, f"actions/runs/{publisher['id']}/artifacts", "artifacts"
            ), publisher)
            jobs = inventory(
                repo, f"actions/runs/{publisher['id']}/attempts/{publisher['run_attempt']}/jobs", "jobs"
            )
            checked = None
            for row in rows:
                prior, decision = publication_stage(repo, row)
                current = checked_stage(
                    repo, publisher, row["name"].split("-", 3)[3], jobs, prior, decision
                )
                same_decision(checked, current)
                checked = current
            require(
                decision is not None and sha256(canonical(decision)) == inputs["outcome_sha256"],
                "release-producer-outcome-invalid: publisher outcome differs",
            )
            assert checked is not None
            require(checked["event_id"] is None, "publisher had a required current target")
        elif reason == "publisher-handoff":
            checked_handoff_target(repo, publisher, inputs["version"], inputs["source"])
            require(
                publisher["head_sha"] != inputs["source"],
                "release-producer-outcome-invalid: publisher did not need a source repair",
            )
            ci = api(repo, f"actions/runs/{inputs['ci_run']}")
            require(
                ci.get("id") == inputs["ci_run"] and ci.get("head_sha") == inputs["source"]
                and ci.get("path") == ".github/workflows/ci-post.yml"
                and ci.get("event") == "push" and ci.get("head_branch") == "main",
                "handoff main source differs",
            )
        elif reason == "reporter-handoff":
            checked_handoff_target(repo, publisher, inputs["version"], inputs["source"])
            require(
                publisher["head_sha"] == inputs["source"]
                and run["head_sha"] != inputs["source"],
                "reporter did not need a source repair",
            )
        else:
            checked_handoff_target(repo, publisher, inputs["version"])


def checked_handoff_target(
    repo: str, publisher: dict[str, Any], version: str, source: str | None = None,
) -> None:
    """A repair decision must refer to the actual completed, targeted publisher."""
    rows = stage_rows(inventory(
        repo, f"actions/runs/{publisher['id']}/artifacts", "artifacts"
    ), publisher)
    require(
        rows[-1]["name"] == f"release-stage-{publisher['run_attempt']}-50-verify",
        "release-producer-outcome-invalid: handoff lacks publisher verification",
    )
    loaded, outcome = publication_stage(repo, rows[-1])
    require(
        loaded is not None and outcome is None,
        "release-producer-outcome-invalid: handoff lacks a targeted snapshot",
    )
    assert loaded is not None
    snapshot_identity(loaded, publisher["id"], publisher["run_attempt"])
    job = producer_job("50-verify", inventory(
        repo, f"actions/runs/{publisher['id']}/attempts/{publisher['run_attempt']}/jobs", "jobs"
    ))
    pr = api(repo, f"pulls/{loaded['pr']['number']}")
    meta = marker(pr)
    require(
        job["conclusion"] == "success"
        and meta is not None and meta.get("version") == version
        and pr.get("merged") is True and pr["head"]["repo"]["full_name"] == repo
        and pr["base"]["ref"] == "main"
        and loaded["pr"] == pr_identity(pr, base=meta["base"])
        and pr["merge_commit_sha"] == (publisher["head_sha"] if source is None else source),
        "release-producer-outcome-invalid: handoff version/source differs",
    )


def checked_stage(
    repo: str, run: dict[str, Any], stage: str, jobs: list[Any],
    loaded: dict[str, Any] | None, outcome: dict[str, Any] | None,
) -> dict[str, Any]:
    job = producer_job(stage, jobs)
    if outcome is None:
        require(loaded is not None, "release-snapshot-missing: receipt and producer outcome absent")
        assert loaded is not None
        snapshot_identity(loaded, run["id"], run["run_attempt"])
        return ProducerReceipt(loaded)
    require(
        set(outcome) == {
            "schema", "run_id", "run_attempt", "head_sha", "outcome", "reason",
            "inputs", "receipt_sha256",
        }
        and outcome["schema"] == OUTCOME_SCHEMA
        and type(outcome["run_id"]) is int and outcome["run_id"] == run["id"]
        and type(outcome["run_attempt"]) is int and outcome["run_attempt"] == run["run_attempt"]
        and outcome["head_sha"] == run["head_sha"]
        and isinstance(outcome["outcome"], str) and isinstance(outcome["reason"], str)
        and isinstance(outcome["inputs"], dict) and bool(outcome["inputs"])
        and job["conclusion"] == "success",
        "release-producer-outcome-invalid: envelope or producer job differs",
    )
    if outcome["outcome"] == "no-target":
        require(
            stage in NO_TARGET_STAGES.get(outcome["reason"], set())
            and loaded is None and outcome["receipt_sha256"] is None,
            "release-producer-outcome-invalid: no-target decision conflicts with snapshot",
        )
        checked_no_target(repo, run, outcome)
        report = receipt(None)
    else:
        require(
            outcome["outcome"] == "replay" and outcome["reason"] == "stored-verified"
            and stage == "10-adapter" and loaded is not None
            and outcome["receipt_sha256"] == sha256(canonical(loaded)),
            "release-producer-outcome-invalid: missing replay proof",
        )
        assert loaded is not None
        inputs = outcome["inputs"]
        require(
            set(inputs) == {"version", "installed_source", "installed_lock_sha256"}
            and isinstance(inputs.get("version"), str)
            and re.fullmatch(r"\d+\.\d+\.\d+", inputs["version"])
            and isinstance(inputs.get("installed_source"), str)
            and re.fullmatch(r"[0-9a-f]{40}", inputs["installed_source"]),
            "release-replay-invalid: installed source/version missing",
        )
        saved = stored_receipt(repo, inputs["version"])
        require(
            saved is not None and saved == loaded and saved["state"] == "verified",
            "release-replay-invalid: immutable stored receipt differs",
        )
        snapshot_identity(loaded, loaded["attempt"], loaded["sequence"])
        installed = object_json(gh_bytes(
            f"repos/{repo}/contents/{LOCK}?ref={inputs['installed_source']}",
            accept="application/vnd.github.raw+json",
        ))
        require(
            sha256(canonical(installed)) == inputs.get("installed_lock_sha256")
            and installed["package_version"] == inputs["version"]
            and installed["event"]["event_id"] == loaded["event_id"]
            and installed["event"]["release_version"] == loaded["release_version"],
            "release-replay-invalid: installed target differs",
        )
        actual = verified_package(repo, inputs["version"], saved)
        require(
            actual == loaded["deployed"]
            and actual["image_digest"] == installed["event"]["image"]["digest"],
            "release-replay-invalid: installed/served identity differs",
        )
        report = ProducerReceipt(loaded)
    observe_outcome(report, outcome["outcome"], outcome["reason"], **outcome["inputs"])
    return report


def same_decision(previous: dict[str, Any] | None, current: dict[str, Any]) -> None:
    """Later stages cannot erase a required target or substitute another decision."""
    if previous is None:
        return
    require(
        (previous["event_id"], previous["release_version"])
        == (current["event_id"], current["release_version"])
        and getattr(previous, "outcome", None) == getattr(current, "outcome", None),
        "release-producer-outcome-invalid: stage decisions disagree",
    )


def snapshot_identity(report: dict[str, Any], run_id: int, attempt: int) -> None:
    """Check provenance before a snapshot can be relabelled or forwarded."""
    require(set(report) == FIELDS, "release-snapshot-invalid: receipt fields differ")
    require(
        report["schema"] == "pineforge/consumer-receipt/v1"
        and report["consumer"] == "data"
        and isinstance(report["event_id"], str)
        and bool(report["event_id"])
        and isinstance(report["release_version"], str)
        and bool(report["release_version"]),
        "release-snapshot-invalid: expected a targeted data receipt",
    )
    require(
        type(run_id) is int and run_id > 0
        and type(attempt) is int and attempt > 0
        and type(report["attempt"]) is int
        and report["attempt"] == run_id
        and type(report["sequence"]) is int
        and report["sequence"] == attempt,
        f"release-snapshot-attempt-mismatch: expected run {run_id} attempt {attempt}",
    )


def collect(directory: Path, *, failed: bool) -> dict[str, Any]:
    repo = os.environ["GITHUB_REPOSITORY"]
    run = current_run(repo)
    rows = stage_rows(inventory(repo, f"actions/runs/{run['id']}/artifacts", "artifacts"), run)
    jobs = inventory(repo, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
    upstream = {"reconcile"} if run["path"].endswith("/" + WORKFLOW) else {
        "manual-pr", *DEPLOY_JOBS,
    }
    observed_failed = any(
        job["name"] in upstream and job["conclusion"] in {"failure", "cancelled"} for job in jobs
    )
    require(failed == observed_failed, "release-producer-job-mismatch: failure input differs")
    prefix = f"release-stage-{run['run_attempt']}-"
    require(
        {path.name for path in directory.glob(prefix + "*")} == {row["name"] for row in rows},
        "release-snapshot-missing: downloaded current inventory differs from GitHub",
    )
    report = None
    for row in rows:
        directory_stage = directory / row["name"]
        try:
            require(
                {path.name for path in directory_stage.iterdir()}
                <= {"release-receipt.json", OUTCOME_FILE},
                "release-snapshot-invalid: unexpected stage contents",
            )
            path = directory_stage / "release-receipt.json"
            loaded = object_json(path.read_bytes()) if path.exists() else None
            path = directory_stage / OUTCOME_FILE
            outcome = object_json(path.read_bytes()) if path.exists() else None
        except (OSError, ValueError) as error:
            raise ValueError(f"release-snapshot-invalid: {row['name']}") from error
        current = checked_stage(repo, run, row["name"][len(prefix):], jobs, loaded, outcome)
        same_decision(report, current)
        report = current
    assert report is not None  # stage_rows requires a current artifact.
    if failed:
        if getattr(report, "outcome", None):
            report = receipt(None)  # A failed envelope never rewrites an original replay.
        report["state"] = "failed"
        report["timestamp"] = timestamp()
    return report


def incident(repo: str, report: dict[str, Any], owner: str, stage: str) -> None:
    if report["state"] not in {"failed", "verified", "superseded"}:
        return
    require(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner),
        "RELEASE_ALERT_OWNER must identify the incident owner",
    )
    identity = report["event_id"] or f"run-{os.environ['GITHUB_RUN_ID']}"
    marker = f"<!-- release-chain-data:{identity} -->"
    signature = sha256(
        json_bytes(
            {
                "state": report["state"],
                "stage": stage,
                "ci": report["ci"],
            }
        )
    )
    state_marker = f"<!-- release-chain-state:{signature} -->"
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    pr = report.get("pr")
    pr_link = f"\nPR: https://github.com/{repo}/pull/{pr['number']}\n" if pr else ""
    text = (
        f"@{owner} Data release state: **{report['state']}**.\n\n"
        f"Stage: `{stage}`. [Actionable workflow run and private diagnostics]({run_url}).\n"
        f"{pr_link}\nFull errors and check inventories remain in the consumer run.\n\n"
        f"{marker}\n{state_marker}\n"
    )
    issues = inventory(repo, "issues?state=all&sort=updated&direction=desc")
    matches = [issue for issue in issues if marker in (issue.get("body") or "")]
    require(len(matches) <= 1, "ambiguous release incident")
    if report["state"] == "superseded" and not matches:
        return
    if not matches:
        matches = [
            api(
                repo,
                "issues",
                "POST",
                {
                    "title": f"Data release {report['state']}: {identity}",
                    "body": text,
                    "assignees": [owner],
                },
            )
        ]
    issue = matches[0]
    if state_marker not in (issue.get("body") or ""):
        api(repo, f"issues/{issue['number']}/comments", "POST", {"body": text})
    api(
        repo,
        f"issues/{issue['number']}",
        "PATCH",
        {
            "body": text,
            "state": "open" if report["state"] == "failed" else "closed",
            "title": f"Data release {report['state']}: {identity}",
        },
    )


def public_receipt(report: dict[str, Any]) -> dict[str, Any]:
    require(set(report) == FIELDS, "receipt fields differ from the hub contract")
    return copy.deepcopy(report)


def already_verified(repo: str, event_id: str) -> bool:
    """The hub forbids verified -> failure/progress, including across attempts."""
    for release in inventory(repo, "releases"):
        for asset in release["assets"]:
            if asset["name"] != "release-receipt.json":
                continue
            saved = object_json(
                gh_bytes(
                    f"repos/{repo}/releases/assets/{asset['id']}", accept="application/octet-stream"
                )
            )
            require(set(saved) == FIELDS, "stored receipt fields differ")
            if saved["event_id"] == event_id and saved["state"] == "verified":
                return True
    return False


def callback(report: dict[str, Any]) -> None:
    # Called only by the upload-success path of the trusted reporting workflow.
    api(
        HUB,
        "dispatches",
        "POST",
        {
            "event_type": "pineforge-release-status",
            "client_payload": signal(report, int(os.environ["GITHUB_RUN_ID"])),
        },
    )


def publication(repo: str, run_id: int, report: dict[str, Any]) -> None:
    """Finalize a completed Release run using observed candidate, deploy and PyPI evidence."""
    require(type(run_id) is int and run_id > 0, "invalid publication run ID")
    run = api(repo, f"actions/runs/{run_id}")
    require(
        run.get("id") == run_id
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] > 0
        and run["path"] == ".github/workflows/release.yml"
        and run["event"] in {"workflow_run", "workflow_dispatch"}
        and run["status"] == "completed",
        "untrusted or incomplete publisher",
    )
    prefix = f"release-stage-{run['run_attempt']}-"
    rows = stage_rows(inventory(repo, f"actions/runs/{run_id}/artifacts", "artifacts"), run)
    jobs = inventory(repo, f"actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs", "jobs")
    loaded = None
    for row in rows:
        snapshot, outcome = publication_stage(repo, row)
        current = checked_stage(repo, run, row["name"][len(prefix):], jobs, snapshot, outcome)
        same_decision(loaded, current)
        loaded = current
    assert loaded is not None
    if loaded["event_id"] is None:
        require(run["conclusion"] == "success", "target-less publisher failed")
        observe_outcome(
            report, "no-target", "publisher-no-target", publication_run=run_id,
            publication_attempt=run["run_attempt"], outcome_sha256=sha256(canonical(outcome)),
        )
        return
    report.update(loaded)
    report.update(
        attempt=int(os.environ["GITHUB_RUN_ID"]),
        sequence=int(os.environ["GITHUB_RUN_ATTEMPT"]),
        timestamp=timestamp(),
    )
    # The hub manifest omits its own digest; the consumer verifies its original bytes.
    from .contract import event_from_manifest, verify_artifacts

    event = event_from_manifest(hub_manifest(report["release_version"]))
    require(report["event_id"] == event["event_id"], "publication target differs from hub manifest")
    if run["conclusion"] != "success":
        report["state"] = "failed"
        return
    if rows[-1]["name"] == prefix + "10-manual":
        return  # A successful manual run opened/tested a PR; it did not deploy.
    require(
        rows[-1]["name"] == prefix + "50-verify" and report["deployed"] is not None,
        "publisher did not verify served distributions",
    )
    pr = api(repo, f"pulls/{report['pr']['number']}")
    meta = marker(pr)
    require(
        pr["merged"]
        and pr["head"]["repo"]["full_name"] == repo
        and pr["base"]["ref"] == "main"
        and meta is not None
        and pr["head"]["sha"] == meta["generated_head"],
        "publication PR identity moved",
    )
    assert meta is not None
    source = pr["merge_commit_sha"]
    require(report["pr"] == pr_identity(pr, base=meta["base"]), "publication PR evidence differs")
    commit = api(repo, f"commits/{source}")
    require(
        commit["parents"] and commit["parents"][0]["sha"] == meta["base"],
        "merge inputs differ from tested base",
    )
    # Source remains data: never check out or execute the PR head or artifact scripts.
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary)
        bundle_name = f"validated-release-bundle-{run['run_attempt']}"
        try:
            subprocess.run(
                [
                    "gh",
                    "run",
                    "download",
                    str(run_id),
                    "--repo",
                    repo,
                    "--name",
                    bundle_name,
                    "--dir",
                    str(bundle),
                ],
                check=True,
            )
        except subprocess.CalledProcessError as error:
            raise ValueError(
                f"release-bundle-unavailable: run {run_id} artifact {bundle_name}"
            ) from error
        try:
            main_gate = object_json((bundle / "evidence.json").read_bytes())
        except (OSError, ValueError) as error:
            raise ValueError(f"release-bundle-invalid: {bundle_name} evidence") from error
        fresh_gate = gate_for_run(repo, main_gate["run_id"], head=source)
        require(
            json_bytes(semantic_evidence_identity(fresh_gate))
            == json_bytes(semantic_evidence_identity(main_gate)),
            "landed CI distributions/evidence moved",
        )
        lock = verify_local(bundle / "source")
        require(lock is not None and lock["event"] == event, "served bundle target differs")
        assert lock is not None
        version = lock["package_version"]
        require(version == meta["version"], "served package differs from PR")
        candidate(report, event, candidate_gate(repo, pr), version)
        verify_artifacts(event)
        require(report["deployed"] == verified_package(repo, version), "served identity moved")
        require(
            report["previous"]
            == verified_package(repo, lock["previous"]["package_version"], adopter_lock=lock),
            "previous/rollback distribution identity moved",
        )
    # workflow_run takes the then-current main SHA. Pin repair runs to the trusted,
    # immutable package tag if unrelated main changes arrived before either run.
    if run["head_sha"] != source:
        api(
            repo,
            "actions/workflows/release.yml/dispatches",
            "POST",
            {
                "ref": f"v{version}",
                "inputs": {"ci_run": str(main_gate["run_id"]), "bump": "patch"},
            },
        )
        report.update(receipt(None))
        observe_outcome(
            report, "no-target", "publisher-handoff", publication_run=run_id,
            ci_run=main_gate["run_id"], version=version, source=source,
        )
        return
    reporting = api(repo, f"actions/runs/{os.environ['GITHUB_RUN_ID']}")
    require(
        reporting.get("id") == int(os.environ["GITHUB_RUN_ID"])
        and type(reporting.get("run_attempt")) is int
        and reporting["run_attempt"] == int(os.environ["GITHUB_RUN_ATTEMPT"])
        and reporting["path"] == ".github/workflows/" + WORKFLOW
        and reporting["event"] in {"workflow_run", "workflow_dispatch"},
        "untrusted reporter",
    )
    if reporting["head_sha"] != source:
        api(
            repo,
            "actions/workflows/" + WORKFLOW + "/dispatches",
            "POST",
            {
                "ref": f"v{version}",
                "inputs": {"publication_run": str(run_id)},
            },
        )
        report.update(receipt(None))
        observe_outcome(
            report, "no-target", "reporter-handoff", publication_run=run_id,
            version=version, source=source,
        )
        return
    # Keep the final deployment observation after the full publication proof.
    jobs = inventory(repo, f"actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs", "jobs")
    results = {}
    for name in DEPLOY_JOBS:
        found = [job for job in jobs if job["name"] == name]
        require(
            len(found) == 1 and found[0]["status"] == "completed", "deploy job missing/incomplete"
        )
        result = found[0]["conclusion"]
        require(
            result == "success" or (result == "skipped" and name in DEPLOY_SKIPS),
            "deploy job failed/cancelled/skipped",
        )
        results[name] = result
    report["deploy"] = {
        "run_id": run_id,
        "run_attempt": run["run_attempt"],
        "sha": source,
        "expected": list(DEPLOY_JOBS),
        "approved_skips": list(DEPLOY_SKIPS),
        "results": results,
    }
    report["state"] = "verified"
    report["timestamp"] = timestamp()


def retain(repo: str, report: dict[str, Any]) -> None:
    """Persist only this contract, after the same receipt has been uploaded for the hub."""
    if report["state"] != "verified":
        return
    pr = api(repo, f"pulls/{report['pr']['number']}")
    meta = marker(pr)
    require(meta is not None, "retained release marker missing")
    assert meta is not None
    version = meta["version"]
    saved = stored_receipt(repo, version)
    if saved:
        require(
            saved["state"] == "verified"
            and saved["event_id"] == report["event_id"]
            and saved["pr"] == report["pr"]
            and saved["deployed"] == report["deployed"]
            and saved["previous"] == report["previous"],
            "immutable completion receipt differs",
        )
        return
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "release-receipt.json"
        finish(report, path)
        subprocess.run(
            ["gh", "release", "upload", f"v{version}", str(path), "--repo", repo], check=True
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("collect", "publication", "retain", "callback", "incident")
    )
    parser.add_argument("--directory", type=Path, default=Path("receipts"))
    parser.add_argument("--receipt", type=Path, default=Path("release-receipt.json"))
    parser.add_argument("--run", type=int)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    if args.command == "publication":
        report = receipt(None)
        try:
            publication(repo, args.run, report)
        except Exception:
            report["state"] = "failed"
            raise
        finally:
            finish(report, args.receipt)
    elif args.command in {"collect", "incident"}:
        incident_only = args.command == "incident"
        require(
            not incident_only or os.environ["FAILED"] == "true",
            "incident mode requires a failed report",
        )
        try:
            report = collect(
                args.directory,
                failed=os.environ["FAILED"] == "true",
            )
        except ValueError:
            if not incident_only:
                raise
            # Invalid release evidence must not prevent a run-scoped incident.
            # This target-less object is never written or sent to the hub.
            report = receipt(None)
            report["state"] = "failed"
        # A replay is checked against the stored asset and installed proof. Its
        # original attempt, sequence, timestamp and pins remain untouched.
        producer_decision = getattr(report, "outcome", None)
        has_event = (
            not incident_only
            and not producer_decision
            and bool(report["event_id"])
            and report["attempt"] == int(os.environ["GITHUB_RUN_ID"])
            and report["sequence"] == int(os.environ["GITHUB_RUN_ATTEMPT"])
        )
        if (
            has_event
            and report["state"] != "verified"
            and already_verified(repo, report["event_id"])
        ):
            has_event = False  # Keep the incident; never regress hub completion.
        if has_event:
            finish(report, args.receipt)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as handle:
            handle.write(f"has_event={str(has_event).lower()}\n")
        if not producer_decision or incident_only:
            incident(repo, report, os.environ["RELEASE_ALERT_OWNER"], os.environ["STAGE"])
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as handle:
            handle.write(
                f"Data release state: **{report['state']}**; event `{report['event_id']}`.\n"
            )
    elif args.command == "retain":
        retain(repo, object_json(args.receipt.read_bytes()))
    else:
        callback(object_json(args.receipt.read_bytes()))


if __name__ == "__main__":
    main()
