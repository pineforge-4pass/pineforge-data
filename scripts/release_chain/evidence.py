"""Closed CI inventory, immutable distribution evidence and exact-head gates."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .contract import HEX40, gh_bytes, json_bytes, object_json, require, sha256
from .main_proofs import (
    NATIVE_WORKFLOW,
    NETWORK_JOB,
    NETWORK_NODE,
    consume_network,
    successful_jobs,
    validate_identity,
    validate_main,
)
from .main_proofs import validate_inventory as validate_proof_inventory

# SHA256 of both reviewed CI definitions: no continue-on-error, matrix leg names = JOBS.
# Edits must update the pin in the same PR after re-review: needs.<job>.result == "success" proves
# every leg succeeded (2026-10-23, VP-145 review P0: a lexical test alone misses escaped YAML keys).
REVIEWED_CI_WORKFLOW_SHA256 = "e2b41563fbb8ab32c3c16675fc3d411b9dade719593ab0157fd47d641062ea2a"
REVIEWED_CI_POST_WORKFLOW_SHA256 = (
    "b8d3d92bb4b106f8985f8bbe4d96f12bff67d4d2b71b5bbf07bb46c76f9a8d49"
)

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
PENDING_STATUSES = {"queued", "in_progress", "requested", "waiting", "pending"}
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
    def __init__(
        self, message: str, run: dict[str, Any] | None, jobs: list[dict[str, Any]] | None
    ) -> None:
        super().__init__(message)
        self.run = run
        self.jobs = jobs
        self.evidence = {
            "run_id": run.get("id") if run is not None else None,
            "attempt": run.get("run_attempt") if run is not None else None,
            "head": run.get("head_sha") if run is not None else None,
            "jobs": {
                name: next(
                    (
                        job.get("conclusion") or job.get("status")
                        for job in jobs if job.get("name") == name
                    ),
                    "missing",
                )
                for name in (*JOBS, "release/ready")
            } if jobs is not None else None,
        }


class ProofPending(GateRejected):
    """An observed main counterpart is absent or has not completed yet."""

    def __init__(
        self,
        message: str,
        run: dict[str, Any] | None,
        jobs: list[dict[str, Any]] | None,
        *,
        workflow: str,
    ) -> None:
        super().__init__(message, run, jobs)
        self.workflow = workflow
        self.observations = {workflow: {"run": run, "jobs": jobs}}


def require_main_ready(
    run: dict[str, Any], run_id: int, *, head: str, workflow: str, message: str
) -> None:
    require(
        type(run_id) is int
        and run_id > 0
        and type(run.get("id")) is int
        and run["id"] == run_id
        and run.get("path") == workflow
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and isinstance(head, str)
        and HEX40.fullmatch(head)
        and run.get("head_sha") == head
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] > 0,
        message,
    )
    require(isinstance(run.get("status"), str) and "conclusion" in run, message)
    if run["status"] in PENDING_STATUSES and run["conclusion"] is None:
        raise ProofPending(message, run, None, workflow=workflow)
    require(run["status"] == "completed" and run["conclusion"] == "success", message)


def semantic_evidence_identity(evidence: dict[str, Any]) -> dict[str, Any]:
    """Preserve proof content while projecting raw API run/job identity."""
    identity = copy.deepcopy(evidence)
    proofs = identity.get("main_proofs")
    if isinstance(proofs, dict) and isinstance(proofs.get("native"), dict):
        native = proofs["native"]
        run = native.get("run")
        require(isinstance(run, dict), "native run: expected an object")
        native["run"] = {
            field: run[field]
            for field in (
                "id", "workflow_id", "path", "event", "head_branch", "head_sha",
                "run_attempt", "status", "conclusion",
            )
            if field in run
        }
        jobs = native.get("jobs")
        require(
            isinstance(jobs, list) and all(isinstance(job, dict) for job in jobs),
            "missing observed jobs",
        )
        native["jobs"] = sorted(
            (
                {
                    field: job[field]
                    for field in (
                        "id", "name", "run_id", "run_attempt", "head_sha", "status", "conclusion",
                    )
                    if field in job
                }
                for job in jobs
            ),
            key=json_bytes,
        )
    return identity


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
    files = [path for path in directory.iterdir() if path.is_file()]
    require(
        len(files) == 2
        and sum(p.suffix == ".whl" for p in files) == 1
        and sum(p.name.endswith(".tar.gz") for p in files) == 1,
        "expected exactly one wheel and one sdist",
    )
    return {path.name: sha256(path.read_bytes()) for path in sorted(files)}


def attempt_jobs(repo: str, run_id: int, attempt: int) -> list[dict[str, Any]]:
    jobs = object_json(
        gh_bytes(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    ).get("jobs")
    require(
        isinstance(jobs, list) and all(isinstance(job, dict) for job in jobs),
        "missing observed jobs",
    )
    assert isinstance(jobs, list)
    return jobs


# Completion and success come from needs (authoritative).
# The jobs API supplies names only: it can lag a finished leg by minutes (2026-10-22).
# No timer by owner rule.
def validate_inventory(jobs: list[dict[str, Any]], *, main: bool = False) -> None:
    for name in (*JOBS, NETWORK_JOB) if main else JOBS:
        matching = [job for job in jobs if job["name"] == name]
        require(
            len(matching) == 1,
            f"required job {name!r} missing or duplicated in the attempt's job list",
        )


def require_no_continue_on_error(root: Path) -> None:
    for filename, reviewed in (
        ("ci.yml", REVIEWED_CI_WORKFLOW_SHA256),
        ("ci-post.yml", REVIEWED_CI_POST_WORKFLOW_SHA256),
    ):
        try:
            raw = (root / ".github/workflows" / filename).read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            require(False, f"cannot read {filename}: {exc}")
        require(
            sha256(raw) == reviewed and "continue-on-error" not in text,
            f"{filename} changed or uses continue-on-error: needs results require workflow review",
        )


def create_evidence(
    root: Path, directory: Path, needs: dict[str, Any], *, network: Path | None = None
) -> dict[str, Any]:
    main = os.environ["GITHUB_EVENT_NAME"] == "push"
    require(
        set(needs) == NEEDS | ({"network-proof"} if main else set()),
        "required CI job inventory changed",
    )
    require(
        all(value.get("result") == "success" for value in needs.values()),
        "a required CI job failed, was cancelled, missing or skipped",
    )
    require_no_continue_on_error(root)
    trigger = object_json(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    pr = trigger.get("pull_request")
    tested = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    jobs = attempt_jobs(repo, run_id, attempt)
    validate_inventory(jobs, main=main)
    result: dict[str, Any] = {
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
    if main:
        require(network is not None, "main requires the actual live-PyPI report")
        assert network is not None
        result["main_proofs"] = {"network": consume_network(root, network)}
    return result


def _validate_ci_evidence(
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
    workflow = "ci.yml" if pr else "ci-post.yml"
    require(run["path"] == f".github/workflows/{workflow}", "unexpected CI workflow")
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


def validate_gate(
    evidence: dict[str, Any],
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    head: str,
    base: str | None,
    pr: int | None,
) -> None:
    _validate_ci_evidence(evidence, run, jobs, head=head, base=base, pr=pr)
    if pr is None:
        validate_main(evidence, run, jobs)


def _validate_waiting_python(
    evidence: dict[str, Any],
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    head: str,
    base: str | None,
) -> None:
    _validate_ci_evidence(evidence, run, jobs, head=head, base=base, pr=None)
    require(
        evidence.get("tested_sha") == evidence.get("head"),
        "main proof must test the landed main head",
    )
    network = evidence["main_proofs"]["network"]
    require(isinstance(network, dict), "network: expected an object")
    validate_identity(network, run, evidence, "network")
    validate_proof_inventory(network, "network")
    require(
        network.get("node_id") == NETWORK_NODE
        and network["registered"] == network["executed"] == [NETWORK_NODE],
        "network: missing unique required live-PyPI row",
    )
    successful_jobs(jobs, (NETWORK_JOB,), run=run)


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


def matching_main_runs(observed: Any, workflow: str, *, head: str) -> list[dict[str, Any]]:
    require(isinstance(head, str) and HEX40.fullmatch(head), "invalid main proof head")
    require(isinstance(observed, list), "missing workflow run observations")
    runs = []
    for row in observed:
        require(
            isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0,
            "workflow observation has no valid run identity",
        )
        require(
            type(row.get("run_attempt")) is int
            and row["run_attempt"] > 0
            and all(
                isinstance(row.get(field), str) and row[field]
                for field in ("event", "head_branch", "head_sha", "path", "status")
            )
            and HEX40.fullmatch(row["head_sha"])
            and row["status"] in PENDING_STATUSES | {"completed"}
            and "conclusion" in row
            and (
                row["conclusion"] is None
                or (isinstance(row["conclusion"], str) and bool(row["conclusion"]))
            ),
            "malformed workflow run observation",
        )
        if (
            row["event"] == "push"
            and row["head_branch"] == "main"
            and row["head_sha"] == head
            and row["path"] == f".github/workflows/{workflow}"
        ):
            runs.append(row)
    return runs


def latest_main_run(repo: str, workflow: str, *, head: str) -> dict[str, Any]:
    """Choose by identity/id before examining status; never fall back to green."""
    runs = []
    page = 1
    while True:
        suffix = "" if page == 1 else f"&page={page}"
        observed = object_json(
            gh_bytes(
                f"repos/{repo}/actions/workflows/{workflow}/runs"
                f"?event=push&head_sha={head}&per_page=100{suffix}"
            )
        ).get("workflow_runs")
        runs.extend(matching_main_runs(observed, workflow, head=head))
        assert isinstance(observed, list)
        if len(observed) < 100:
            break
        page += 1
    if not runs:
        raise ProofPending(
            f"missing exact-head main {workflow} run", None, None,
            workflow=f".github/workflows/{workflow}",
        )
    return max(runs, key=lambda row: row["id"])


def gate_for_run(
    repo: str, run_id: int, *, head: str, base: str | None = None, pr: int | None = None
) -> dict[str, Any]:
    run = object_json(gh_bytes(f"repos/{repo}/actions/runs/{run_id}"))
    jobs: list[dict[str, Any]] | None = None
    try:
        if pr is None:
            require_main_ready(
                run, run_id, head=head, workflow=".github/workflows/ci-post.yml",
                message="CI has not completed successfully",
            )
        jobs = attempt_jobs(repo, run_id, run["run_attempt"])
        validate_jobs(jobs, aggregate=True)
        evidence = artifact_json(
            repo, run_id, f"release-evidence-{run['run_attempt']}", "evidence.json"
        )
        if pr is None:
            proofs = evidence.get("main_proofs")
            require(
                isinstance(proofs, dict) and set(proofs) == {"network"},
                "Python producer must supply its network proof only",
            )
            assert isinstance(proofs, dict)
            try:
                native = latest_main_run(repo, "native-feed-post.yml", head=head)
                # Bind the current attempt rather than the cached list observation.
                current = object_json(gh_bytes(f"repos/{repo}/actions/runs/{native['id']}"))
                require_main_ready(
                    current, native["id"], head=head, workflow=NATIVE_WORKFLOW,
                    message="newest native proof has not completed successfully",
                )
            except ProofPending:
                # A native wait cannot hide invalid completed Python evidence.
                _validate_waiting_python(evidence, run, jobs, head=head, base=base)
                raise
            native_jobs = attempt_jobs(repo, current["id"], current["run_attempt"])
            attestation = artifact_json(
                repo, current["id"], f"native-evidence-{current['run_attempt']}",
                "native-evidence.json",
            )
            evidence["main_proofs"] = {
                **proofs,
                "native": {
                    "workflow_path": NATIVE_WORKFLOW,
                    "run": current,
                    "jobs": native_jobs,
                    "attestation": attestation,
                },
            }
        validate_gate(evidence, run, jobs, head=head, base=base, pr=pr)
    except ProofPending as exc:
        exc.observations.setdefault(
            ".github/workflows/ci-post.yml", {"run": run, "jobs": jobs}
        )
        raise
    except ValueError as exc:
        raise GateRejected(str(exc), run, jobs) from exc
    return evidence


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("junit", "aggregate"))
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--suite", choices=TESTS)
    parser.add_argument("--network-proof", type=Path)
    args = parser.parse_args()
    if args.command == "junit":
        check_junit(args.path, args.suite)
    else:
        evidence = create_evidence(
            Path.cwd(), args.path, json.loads(os.environ["NEEDS_JSON"]),
            network=args.network_proof,
        )
        Path("evidence.json").write_bytes(json_bytes(evidence))


if __name__ == "__main__":
    main()
