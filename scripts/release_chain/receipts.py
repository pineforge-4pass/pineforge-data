"""The hub's consumer receipt v1; no private diagnostics or publication URLs."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from .contract import require, sha256
from .evidence import JOBS

ARTIFACT = "release-receipt-data"
FILENAME = "release-receipt.json"
WORKFLOW = "pineforge-release.yml"
CI_JOBS = [*JOBS, "release/ready"]
DEPLOY_JOBS = ["prepare", "build", "github-release", "publish", "verify"]
DEPLOY_SKIPS = ["publish"]  # Only an already-published, byte-identical version skips upload.
FIELDS = {
    "schema",
    "consumer",
    "event_id",
    "release_version",
    "attempt",
    "sequence",
    "state",
    "timestamp",
    "pr",
    "ci",
    "e2e",
    "deploy",
    "preview",
    "deployed",
    "previous",
}


def canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode()


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def receipt(event: dict[str, Any] | None, _stage: str = "") -> dict[str, Any]:
    # A target-less working object is never written or uploaded as a receipt.
    return {
        "schema": "pineforge/consumer-receipt/v1",
        "consumer": "data",
        "event_id": event["event_id"] if event else None,
        "release_version": event["release_version"] if event else None,
        "attempt": int(os.environ.get("GITHUB_RUN_ID", "1")),
        "sequence": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
        "state": "pending",
        "timestamp": timestamp(),
        **dict.fromkeys(("pr", "ci", "e2e", "deploy", "preview", "deployed", "previous")),
    }


def checks(gate: dict[str, Any]) -> dict[str, Any]:
    """Called only after gate_for_run has checked every job and the CI artifact."""
    return {
        "run_id": gate["run_id"],
        "run_attempt": gate["attempt"],
        "sha": gate["head"],
        "expected": list(CI_JOBS),
        "approved_skips": [],
        "results": dict.fromkeys(CI_JOBS, "success"),
    }


def served(
    event: dict[str, Any], source: str, version: str, distributions: dict[str, str]
) -> dict[str, str]:
    # A distribution identity is reproducible from the verified wheel/sdist bytes.
    # Neither the raw identity nor download URLs are put in the receipt.
    return {
        "source": source,
        "image_digest": event["image"]["digest"],
        "facts_commit": event["facts"]["commit"],
        "facts_sha256": event["facts"]["sha256"],
        "deployment_id_sha256": sha256(
            canonical(
                {
                    "package": "pineforge-data",
                    "version": version,
                    "distributions": distributions,
                }
            )
        ),
    }


def candidate(
    report: dict[str, Any], event: dict[str, Any], gate: dict[str, Any], version: str
) -> None:
    report["ci"] = checks(gate)
    report["e2e"] = checks(gate)
    report["preview"] = served(event, report["pr"]["head"], version, gate["distributions"])


def signal(report: dict[str, Any], run_id: int) -> dict[str, Any]:
    require(type(run_id) is int and 0 < run_id <= 9007199254740991, "invalid reporting run ID")
    require(report["consumer"] == "data" and report["event_id"], "signal needs a data target")
    return {
        "schema": "pineforge/consumer-signal/v1",
        "consumer": "data",
        "release_version": report["release_version"],
        "run_id": run_id,
    }
