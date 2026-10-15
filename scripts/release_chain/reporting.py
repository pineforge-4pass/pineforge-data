"""Private incident details and neutral, separately scoped hub callbacks."""

from __future__ import annotations

import argparse
import copy
import os
import re
from pathlib import Path
from typing import Any

from .automation import api, finish, inventory, receipt
from .contract import HEX64, HUB, json_bytes, object_json, require, sha256


def collect(directory: Path, *, failed: bool, stage: str) -> dict[str, Any]:
    paths = sorted(directory.glob("release-receipt-*/receipt.json"))
    # Stage prefixes preserve order across separate jobs without relying on clocks.
    report = object_json(paths[-1].read_bytes()) if paths else receipt(None, stage)
    if failed:
        if report["state"] != "failed":
            report["verification"]["stage"] = stage
            report["completed_at"] = None
        report["state"] = "failed"
        report["verification"].setdefault("detail", f"{stage} failed or was cancelled; see run")
    return report


def incident(repo: str, report: dict[str, Any], owner: str) -> None:
    if report["state"] not in {"failed", "verified", "superseded"}:
        return
    require(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner),
        "RELEASE_ALERT_OWNER must identify the incident owner",
    )
    identity = report["event_id"] or f"run-{os.environ['GITHUB_RUN_ID']}"
    marker = f"<!-- release-chain-data:{identity} -->"
    verification = report["verification"]
    signature = sha256(
        json_bytes(
            {
                "state": report["state"],
                "stage": verification["stage"],
                "detail": verification.get("detail"),
                "ci": verification.get("ci"),
            }
        )
    )
    state_marker = f"<!-- release-chain-state:{signature} -->"
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    pr = report.get("pull_request")
    pr_link = f"\nPR: https://github.com/{repo}/pull/{pr['number']}\n" if pr else ""
    text = (
        f"@{owner} Data release state: **{report['state']}**.\n\n"
        f"Stage: `{verification['stage']}`. [Actionable workflow run]({run_url}).\n"
        f"{pr_link}\n{verification.get('detail', 'Publication and downloaded bytes verified.')}\n\n"
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
    result = copy.deepcopy(report)
    # Exceptions, repository URLs and operator details stay in the consumer issue.
    result["verification"].pop("detail", None)
    result["verification"].pop("superseded", None)
    return result


def callback(report: dict[str, Any], event_type: str) -> None:
    for superseded in report["verification"].get("superseded", []):
        callback(superseded, event_type)
    require(
        isinstance(report["event_id"], str)
        and report["event_id"].startswith("sha256:")
        and HEX64.fullmatch(report["event_id"][7:]),
        "callback needs a manifest identity",
    )
    require(
        re.fullmatch(r"[a-z][a-z0-9-]{0,99}", event_type)
        and event_type not in {"pineforge-release", "facts-update"},
        "RELEASE_RECEIPT_EVENT_TYPE must match the hub lane's status receiver",
    )
    api(
        HUB,
        "dispatches",
        "POST",
        {"event_type": event_type, "client_payload": public_receipt(report)},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "callback"))
    parser.add_argument("--directory", type=Path, default=Path("receipts"))
    parser.add_argument("--receipt", type=Path, default=Path("receipt.json"))
    args = parser.parse_args()
    if args.command == "collect":
        report = collect(
            args.directory, failed=os.environ["FAILED"] == "true", stage=os.environ["STAGE"]
        )
        finish(report, args.receipt)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as handle:
            handle.write(f"has_event={str(report['event_id'] is not None).lower()}\n")
        incident(os.environ["GITHUB_REPOSITORY"], report, os.environ["RELEASE_ALERT_OWNER"])
        for superseded in report["verification"].get("superseded", []):
            incident(os.environ["GITHUB_REPOSITORY"], superseded, os.environ["RELEASE_ALERT_OWNER"])
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as handle:
            handle.write(
                f"Data release state: **{report['state']}**; event `{report['event_id']}`.\n"
            )
    else:
        callback(object_json(args.receipt.read_bytes()), os.environ["RELEASE_RECEIPT_EVENT_TYPE"])


if __name__ == "__main__":
    main()
