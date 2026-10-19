"""Trusted receipt snapshots, private incidents, and the hub's four-key wakeup."""

from __future__ import annotations

import argparse
import copy
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .automation import api, finish, inventory, marker, pr_identity, receipt
from .contract import (
    HUB,
    gh_bytes,
    hub_manifest,
    json_bytes,
    object_json,
    require,
    sha256,
    verify_local,
)
from .evidence import artifact_json, gate_for_run
from .publishing import candidate_gate, stored_receipt, verified_package
from .receipts import (
    DEPLOY_JOBS,
    DEPLOY_SKIPS,
    FIELDS,
    WORKFLOW,
    candidate,
    signal,
    timestamp,
)


def collect(directory: Path, *, failed: bool, stage: str) -> dict[str, Any]:
    paths = sorted(directory.glob("release-stage-*/release-receipt.json"))
    report = object_json(paths[-1].read_bytes()) if paths else receipt(None, stage)
    require(set(report) == FIELDS, "receipt fields differ from the hub contract")
    if failed:
        report["attempt"] = int(os.environ.get("GITHUB_RUN_ID", "1"))
        report["sequence"] = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
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
        run["path"] == ".github/workflows/release.yml"
        and run["event"] in {"workflow_run", "workflow_dispatch"}
        and run["status"] == "completed",
        "untrusted or incomplete publisher",
    )
    prefix = f"release-stage-{run['run_attempt']}-"
    artifacts = inventory(repo, f"actions/runs/{run_id}/artifacts", "artifacts")
    stages = sorted(
        row["name"]
        for row in artifacts
        if not row["expired"]
        and row["name"].startswith(prefix)
        and row["name"][len(prefix) : 2 + len(prefix)] in {"10", "20", "30", "40", "50"}
    )
    if not stages:
        return  # An ordinary main push/manual pre-manifest package has no product target.
    loaded = artifact_json(repo, run_id, stages[-1], "release-receipt.json")
    require(set(loaded) == FIELDS, "publisher receipt fields differ")
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
    if stages[-1] == prefix + "10-manual":
        return  # A successful manual run opened/tested a PR; it did not deploy.
    require(
        stages[-1] == prefix + "50-verify" and report["deployed"] is not None,
        "publisher did not verify served distributions",
    )
    pr = api(repo, f"pulls/{report['pr']['number']}")
    meta = marker(pr)
    require(
        pr["merged"]
        and pr["head"]["repo"]["full_name"] == repo
        and pr["base"]["ref"] == "main"
        and pr["head"]["sha"] == meta["generated_head"],
        "publication PR identity moved",
    )
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
        subprocess.run(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                repo,
                "--name",
                f"validated-release-bundle-{run['run_attempt']}",
                "--dir",
                str(bundle),
            ],
            check=True,
        )
        main_gate = object_json((bundle / "evidence.json").read_bytes())
        require(
            gate_for_run(repo, main_gate["run_id"], head=source) == main_gate,
            "landed CI distributions/evidence moved",
        )
        lock = verify_local(bundle / "source")
        require(lock is not None and lock["event"] == event, "served bundle target differs")
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
        return
    reporting = api(repo, f"actions/runs/{os.environ['GITHUB_RUN_ID']}")
    require(
        reporting["path"] == ".github/workflows/" + WORKFLOW
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
        return
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
    version = marker(pr)["version"]
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
    parser.add_argument("command", choices=("collect", "publication", "retain", "callback"))
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
    elif args.command == "collect":
        report = collect(
            args.directory, failed=os.environ["FAILED"] == "true", stage=os.environ["STAGE"]
        )
        # Stored verified replays are checked, but do not re-emit an old snapshot
        # under a new reporting source/run. Polling can recover the original artifact.
        has_event = bool(report["event_id"]) and report["attempt"] == int(
            os.environ["GITHUB_RUN_ID"]
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
