"""Closed CI inventory, immutable distribution evidence and exact-head gates."""

from __future__ import annotations

import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .contract import gh_bytes, json_bytes, object_json, require, sha256
from .distributions import paired_hashes

JOBS = (
    "Python 3.11",
    "Python 3.12",
    "Python 3.13",
    "Release contract",
    "Docker runtime",
    "Database E2E",
    "Distribution",
)
NEEDS = {"test", "contract", "docker", "database-e2e", "distribution"}
TESTS = {
    "docker": {
        "test_local_runtime_uses_published_release_image",
        "test_report_structures_match_the_release_header",
        "test_in_process_runner_matches_the_release_harness",
        "test_server_handles_concurrent_requests_and_reuses_compile_cache",
    },
    "database": {
        "test_bundled_corpus_sample_has_pinned_provenance",
        "test_public_database_provider_api_normalizes_corpus_across_backends",
    },
}


class GateRejected(ValueError):
    def __init__(self, message: str, run: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.evidence = {
            "run_id": run["id"],
            "attempt": run["run_attempt"],
            "head": run["head_sha"],
            "jobs": {
                name: next(
                    (job["conclusion"] or job["status"] for job in jobs if job["name"] == name),
                    "missing",
                )
                for name in (*JOBS, "release/ready")
            },
        }


def check_junit(path: Path, suite: str) -> None:
    cases = list(ElementTree.parse(path).getroot().iter("testcase"))
    names = [case.attrib["name"] for case in cases]
    require(
        len(names) == len(TESTS[suite]) and set(names) == TESTS[suite],
        f"{suite}: missing or unexpected E2E test inventory",
    )
    require(
        all(len(case) == 0 for case in cases),
        f"{suite}: a required E2E failed, errored or was skipped",
    )


def distribution_hashes(directory: Path) -> dict[str, str]:
    return paired_hashes(directory)


def create_evidence(root: Path, directory: Path, needs: dict[str, Any]) -> dict[str, Any]:
    require(set(needs) == NEEDS, "required CI job inventory changed")
    require(
        all(value.get("result") == "success" for value in needs.values()),
        "a required CI job failed, was cancelled, missing or skipped",
    )
    trigger = object_json(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    pr = trigger.get("pull_request")
    tested = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    jobs = object_json(
        gh_bytes(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    )["jobs"]
    validate_jobs(jobs, aggregate=False)
    return {
        "run_id": run_id,
        "attempt": attempt,
        "event": os.environ["GITHUB_EVENT_NAME"],
        "pr": pr["number"] if pr else None,
        "base": pr["base"]["sha"] if pr else None,
        "head": pr["head"]["sha"] if pr else tested,
        "tested_sha": tested,
        "tree": tree,
        "jobs": {name: "success" for name in JOBS},
        "e2e": {name: sorted(tests) for name, tests in TESTS.items()},
        "approved_skips": [
            "opt-in Docker/database cases in the unit matrix only; "
            "both dedicated E2E jobs require zero skips"
        ],
        "distributions": distribution_hashes(directory),
    }


def validate_gate(
    evidence: dict[str, Any],
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    head: str,
    base: str | None,
    pr: int | None,
) -> None:
    require(
        run["status"] == "completed" and run["conclusion"] == "success",
        "CI has not completed successfully",
    )
    require(run["path"] == ".github/workflows/ci.yml", "unexpected CI workflow")
    require(run["head_sha"] == head and evidence["head"] == head, "stale CI head")
    require(evidence["base"] == base and evidence["pr"] == pr, "stale CI base or PR")
    require(
        evidence["run_id"] == run["id"] and evidence["attempt"] == run["run_attempt"],
        "stale CI attempt",
    )
    event = "pull_request" if pr else "push"
    require(run["event"] == evidence["event"] == event, "wrong CI trigger")
    require(evidence["jobs"] == dict.fromkeys(JOBS, "success"), "incomplete CI evidence")
    require(
        evidence["e2e"] == {name: sorted(tests) for name, tests in TESTS.items()},
        "incomplete E2E evidence",
    )
    validate_jobs(jobs, aggregate=True)


def validate_jobs(jobs: list[dict[str, Any]], *, aggregate: bool) -> None:
    for name in (*JOBS, "release/ready") if aggregate else JOBS:
        matching = [job for job in jobs if job["name"] == name]
        require(
            len(matching) == 1
            and matching[0]["status"] == "completed"
            and matching[0]["conclusion"] == "success",
            f"required job {name!r} missing, failed, cancelled or skipped",
        )


def artifact_json(repo: str, run_id: int, name: str, filename: str) -> dict[str, Any]:
    artifacts = object_json(gh_bytes(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"))[
        "artifacts"
    ]
    matches = [a for a in artifacts if a["name"] == name and not a["expired"]]
    require(len(matches) == 1, f"missing unique {name} artifact")
    archive = gh_bytes(f"repos/{repo}/actions/artifacts/{matches[0]['id']}/zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(zipped.namelist() == [filename], f"unexpected {name} artifact contents")
        return object_json(zipped.read(filename))


def gate_for_run(
    repo: str, run_id: int, *, head: str, base: str | None = None, pr: int | None = None
) -> dict[str, Any]:
    run = object_json(gh_bytes(f"repos/{repo}/actions/runs/{run_id}"))
    jobs = object_json(
        gh_bytes(
            f"repos/{repo}/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100"
        )
    )["jobs"]
    try:
        validate_jobs(jobs, aggregate=True)
        evidence = artifact_json(
            repo, run_id, f"release-evidence-{run['run_attempt']}", "evidence.json"
        )
        validate_gate(evidence, run, jobs, head=head, base=base, pr=pr)
    except ValueError as exc:
        raise GateRejected(str(exc), run, jobs) from exc
    return evidence


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("junit", "aggregate"))
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--suite", choices=TESTS)
    args = parser.parse_args()
    if args.command == "junit":
        check_junit(args.path, args.suite)
    else:
        evidence = create_evidence(Path.cwd(), args.path, json.loads(os.environ["NEEDS_JSON"]))
        Path("evidence.json").write_bytes(json_bytes(evidence))


if __name__ == "__main__":
    main()
