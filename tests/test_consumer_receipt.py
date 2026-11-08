from __future__ import annotations

import base64
import copy
import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import hub_receipt_validator as hub
import pytest
import yaml
from release_chain import automation, contract, evidence, publishing, receipts, reporting

FIXTURES = Path(__file__).parent / "fixtures/release_chain"
REPO = "owner/data"
HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture(params=[("transport-one", 480, 2), ("transport-two", 580, 5)])
def main_proof_transport(monkeypatch, request):
    """Offline GitHub observations; only the bytes transport is replaced."""
    label, python_id, attempt = request.param
    head = contract.sha256(label.encode())[:40]
    tree = contract.sha256(f"{label}:tree".encode())[:40]
    node = "tests/test_consumer_receipt.py::test_bootstrap_recorded_hashes_match_live_pypi"
    native_names = {
        "release-amd64": "Release (ubuntu-24.04)",
        "release-arm64": "Release (ubuntu-24.04-arm)",
        "release-macos": "Release (macos-15)",
        "asan-ubsan": "ASan + UBSan",
        "tsan": "TSan",
    }
    python_run = {
        "id": python_id, "run_attempt": attempt, "head_sha": head, "head_branch": "main",
        "status": "completed", "conclusion": "success", "event": "push",
        "path": ".github/workflows/ci-post.yml",
    }
    native_run = {
        **python_run, "id": python_id + 20, "run_attempt": attempt + 1,
        "path": ".github/workflows/native-feed-post.yml",
    }
    legs = {}
    for leg in native_names:
        names = ["feed-unit", "feed-export-unit", "feed-mock-export", "feed-mock"]
        if leg == "tsan":
            names.append("feed-tsan-seeded-race")
        junit = ElementTree.Element("testsuite", name=f"{label}:{leg}")
        for name in names:
            ElementTree.SubElement(junit, "testcase", name=name)
        legs[leg] = {
            "registered": names, "executed": list(names), "skipped": [],
            "failures": [], "errors": [],
            "junit_sha256": contract.sha256(ElementTree.tostring(junit)),
        }
    network_junit = ElementTree.Element("testsuite", name=f"{label}:network")
    ElementTree.SubElement(network_junit, "testcase", name=node)
    network = {
        "run_id": python_id, "attempt": attempt, "head": head, "tested_sha": head,
        "tree": tree, "node_id": node, "registered": [node], "executed": [node],
        "skipped": [], "failures": [], "errors": [],
        "junit_sha256": contract.sha256(ElementTree.tostring(network_junit)),
    }
    python_evidence = {
        "run_id": python_id, "attempt": attempt, "head": head, "tested_sha": head,
        "tree": tree, "base": None, "pr": None, "event": "push",
        "jobs": dict.fromkeys(evidence.JOBS, "success"),
        "e2e": {key: sorted(value) for key, value in evidence.TESTS.items()},
        # Native proof must be discovered independently, never supplied by this artifact.
        "main_proofs": {"network": network},
    }
    workflow = yaml.load(
        (Path(__file__).parents[1] / ".github/workflows/ci-post.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    network_names = [
        job.get("name", job_id)
        for job_id, job in workflow["jobs"].items()
        if any(node in step.get("run", "") for step in job.get("steps", []))
    ]
    network_name = network_names[0] if len(network_names) == 1 else "Live PyPI hashes"
    python_jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (*evidence.JOBS, "release/ready")
    ]
    if not any(job["name"] == network_name for job in python_jobs):
        python_jobs.append(
            {"name": network_name, "status": "completed", "conclusion": "success"}
        )
    native_jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (*native_names.values(), "Native proof")
    ]
    attestation = {
        "run_id": native_run["id"], "attempt": native_run["run_attempt"],
        "head": head, "tested_sha": head, "tree": tree, "legs": legs,
    }
    state = {
        "python_run": python_run, "python_evidence": python_evidence,
        "python_jobs": python_jobs, "network_name": network_name,
        "native_run": native_run, "native_jobs": native_jobs,
        "native_runs": [native_run], "attestation": attestation,
        "other_native_jobs": copy.deepcopy(native_jobs),
        "other_attestation": copy.deepcopy(attestation),
        "native_artifact_missing": False, "calls": [],
        "native_artifact_attempt_delta": 0,
        "python_runs": [python_run],
        "other_python_jobs": copy.deepcopy(python_jobs),
        "other_python_evidence": copy.deepcopy(python_evidence),
        "python_artifact_missing": False, "python_artifact_attempt_delta": 0,
        "artifact_observations": [],
        "artifact_raw": {},
    }

    def gh_bytes(route, **kwargs):
        state["calls"].append(route)
        assert set(kwargs) <= {"accept"}, "the gate transport must remain read-only"
        parsed = urlsplit(route)
        path = parsed.path.removeprefix(f"repos/{REPO}/")
        if path == "actions/workflows/native-feed-post.yml/runs":
            query = parse_qs(parsed.query)
            assert query.get("event") == ["push"]
            assert query.get("head_sha") == [head]
            return contract.json_bytes({"workflow_runs": state["native_runs"]})
        if path == "actions/workflows/ci-post.yml/runs":
            query = parse_qs(parsed.query)
            assert query.get("event") == ["push"]
            assert query.get("head_sha") == [head]
            return contract.json_bytes({"workflow_runs": state["python_runs"]})
        observed_python = {row["id"]: row for row in state["python_runs"]}
        # A list observation may predate a rerun. Detail reads expose the current attempt.
        observed_python[python_id] = python_run
        selected_python = None
        for observed in observed_python.values():
            observed_id, observed_attempt = observed["id"], observed["run_attempt"]
            if path == f"actions/runs/{observed_id}":
                return contract.json_bytes(observed)
            if path == f"actions/runs/{observed_id}/attempts/{observed_attempt}/jobs":
                observed_jobs = (
                    python_jobs if observed_id == python_id else state["other_python_jobs"]
                )
                return contract.json_bytes({"jobs": observed_jobs})
            if path == f"actions/runs/{observed_id}/artifacts":
                artifact_attempt = observed_attempt + state["python_artifact_attempt_delta"]
                name = f"release-evidence-{artifact_attempt}"
                state["artifact_observations"].append((observed_id, observed_attempt, name))
                missing = observed_id == python_id and state["python_artifact_missing"]
                return contract.json_bytes({"artifacts": [] if missing else [{
                    "id": observed_id * 100, "name": name, "expired": False,
                }]})
            if path == f"actions/artifacts/{observed_id * 100}/zip":
                selected_python = observed
                break
        if selected_python is not None:
            filename = "evidence.json"
            payload = copy.deepcopy(
                python_evidence if selected_python["id"] == python_id
                else state["other_python_evidence"]
            )
            if selected_python["id"] != python_id:
                payload.update(
                    run_id=selected_python["id"], attempt=selected_python["run_attempt"],
                    head=selected_python["head_sha"], tested_sha=selected_python["head_sha"],
                )
                network = payload.get("main_proofs", {}).get("network")
                if network is not None:
                    network.update(
                        run_id=selected_python["id"], attempt=selected_python["run_attempt"],
                        head=selected_python["head_sha"], tested_sha=selected_python["head_sha"],
                    )
        else:
            # Serve a fully usable older green proof too: an erroneous fallback
            # must be caught by the verdict/call assertions, not by absent mock setup.
            observed_runs = {row["id"]: row for row in state["native_runs"]}
            observed_runs[native_run["id"]] = native_run
            selected = None
            for observed in observed_runs.values():
                native_id = observed["id"]
                native_attempt = observed["run_attempt"]
                if path == f"actions/runs/{native_id}":
                    return contract.json_bytes(observed)
                if path == f"actions/runs/{native_id}/attempts/{native_attempt}/jobs":
                    observed_jobs = (
                        native_jobs if native_id == native_run["id"]
                        else state["other_native_jobs"]
                    )
                    return contract.json_bytes({"jobs": observed_jobs})
                if path == f"actions/runs/{native_id}/artifacts":
                    missing = (
                        native_id == native_run["id"] and state["native_artifact_missing"]
                    )
                    artifacts = [] if missing else [{
                        "id": native_id * 100,
                        "name": (
                            "native-evidence-"
                            f"{native_attempt + state['native_artifact_attempt_delta']}"
                        ),
                        "expired": False,
                    }]
                    state["artifact_observations"].append((
                        native_id, native_attempt,
                        "native-evidence-"
                        f"{native_attempt + state['native_artifact_attempt_delta']}",
                    ))
                    return contract.json_bytes({"artifacts": artifacts})
                if path == f"actions/artifacts/{native_id * 100}/zip":
                    selected = observed
                    break
            if selected is None:
                raise AssertionError(f"unexpected gate transport read: {route}")
            payload = copy.deepcopy(
                attestation if selected["id"] == native_run["id"]
                else state["other_attestation"]
            )
            if selected["id"] != native_run["id"]:
                payload.update(
                    run_id=selected["id"], attempt=selected["run_attempt"],
                    head=selected["head_sha"], tested_sha=selected["head_sha"],
                )
            filename = "native-evidence.json"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            raw = state["artifact_raw"].get(filename, contract.json_bytes(payload))
            zipped.writestr(filename, raw)
        return archive.getvalue()

    monkeypatch.setattr(evidence, "gh_bytes", gh_bytes)
    return state


def assert_native_discovery(state):
    assert any(
        "/actions/workflows/native-feed-post.yml/runs?" in route
        for route in state["calls"]
    ), "the real gate never requested native workflow observations"


@pytest.fixture
def observed_main_parser(main_proof_transport, monkeypatch):
    """Record inputs while executing the original JSON and proof validators."""
    state = main_proof_transport
    state.update(
        parsed_bytes=[], parsed_objects=[], gate_inputs=[], main_inputs=[], main_validations=[],
        waiting_inputs=[], waiting_validations=[],
    )
    real_object_json = evidence.object_json
    real_validate_gate = evidence.validate_gate
    real_validate_main = evidence.validate_main
    real_validate_waiting_python = evidence._validate_waiting_python

    def parse(raw):
        state["parsed_bytes"].append(raw)
        result = real_object_json(raw)
        state["parsed_objects"].append(copy.deepcopy(result))
        return result

    def validate_gate(proof, run, jobs, **kwargs):
        state["gate_inputs"].append(copy.deepcopy((proof, run, jobs, kwargs)))
        return real_validate_gate(proof, run, jobs, **kwargs)

    def validate_main(proof, run, jobs):
        state["main_inputs"].append(copy.deepcopy((proof, run, jobs)))
        result = real_validate_main(proof, run, jobs)
        state["main_validations"].append(copy.deepcopy(proof))
        return result

    def validate_waiting_python(proof, run, jobs, **kwargs):
        state["waiting_inputs"].append(copy.deepcopy((proof, run, jobs, kwargs)))
        result = real_validate_waiting_python(proof, run, jobs, **kwargs)
        state["waiting_validations"].append(copy.deepcopy(proof))
        return result

    monkeypatch.setattr(evidence, "object_json", parse)
    monkeypatch.setattr(evidence, "validate_gate", validate_gate)
    monkeypatch.setattr(evidence, "validate_main", validate_main)
    monkeypatch.setattr(evidence, "_validate_waiting_python", validate_waiting_python)
    return state


@pytest.fixture
def incident_transport(monkeypatch):
    """Real incident/inventory logic, with mutable issue observations at the API."""
    state = {"issues": [], "reads": [], "writes": [], "comments": []}
    monkeypatch.setenv("GITHUB_RUN_ID", "930")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")

    def api(repo, endpoint, method="GET", data=None):
        assert repo == REPO
        if method == "GET":
            state["reads"].append(endpoint)
            assert data is None
            assert endpoint == (
                "issues?state=all&sort=updated&direction=desc&per_page=100&page=1"
            )
            return copy.deepcopy(state["issues"])
        state["writes"].append((endpoint, method, copy.deepcopy(data)))
        if endpoint == "issues" and method == "POST":
            issue = {"number": 41, "state": "open", **copy.deepcopy(data)}
            state["issues"].append(issue)
            return copy.deepcopy(issue)
        assert len(state["issues"]) == 1
        issue = state["issues"][0]
        if endpoint == f"issues/{issue['number']}/comments" and method == "POST":
            state["comments"].append(copy.deepcopy(data))
            return {"id": len(state["comments"]), **data}
        assert endpoint == f"issues/{issue['number']}" and method == "PATCH"
        issue.update(copy.deepcopy(data))
        return copy.deepcopy(issue)

    # inventory's original implementation resolves automation.api at call time.
    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(reporting, "api", api)
    return state


def collect_callback_without_incident(state, incidents):
    directory = state["callback_root"] / "downloaded-stages"
    assert list(directory.glob("release-stage-*/release-receipt.json")) == []
    report = reporting.collect(directory, failed=False, stage="prepare-publication")
    assert report["state"] == "pending" and report["event_id"] is None
    assert report["pr"] is report["ci"] is report["deployed"] is None
    reporting.incident(REPO, report, "release-owner", "prepare-publication")
    assert incidents["reads"] == incidents["writes"] == []
    return report


def test_main_gate_consumes_matching_native_transport_positive_twin(main_proof_transport):
    state = main_proof_transport
    run = state["python_run"]
    native = state["native_run"]
    # An older green match and higher-id unrelated runs cannot replace the selected pair.
    older = {**native, "id": native["id"] - 1, "run_attempt": 1}
    unrelated = [
        {**native, "id": native["id"] + 1, "head_sha": "e" * 40},
        {**native, "id": native["id"] + 2, "head_branch": "topic"},
        {**native, "id": native["id"] + 3, "event": "pull_request"},
        {**native, "id": native["id"] + 4, "path": ".github/workflows/native-feed.yml"},
    ]
    state["native_runs"] = [older, *unrelated, native]
    if run["run_attempt"] % 2 == 0:
        state["native_runs"].reverse()
    result = publishing.gate_for_run(REPO, run["id"], head=run["head_sha"])
    assert_native_discovery(state)
    prefix = f"repos/{REPO}/actions"
    assert f"{prefix}/runs/{run['id']}" in state["calls"]
    assert (
        f"{prefix}/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
        in state["calls"]
    )
    assert f"{prefix}/runs/{run['id']}/artifacts?per_page=100" in state["calls"]
    assert f"{prefix}/artifacts/{run['id'] * 100}/zip" in state["calls"]
    assert (
        f"{prefix}/runs/{native['id']}/attempts/{native['run_attempt']}/jobs?per_page=100"
        in state["calls"]
    )
    assert f"{prefix}/runs/{native['id']}/artifacts?per_page=100" in state["calls"]
    assert f"{prefix}/artifacts/{native['id'] * 100}/zip" in state["calls"]
    assert not any(f"/runs/{older['id']}/" in route for route in state["calls"])
    assert set(result.get("main_proofs", {})) == {"native", "network"}
    assert result["main_proofs"]["native"] == {
        "workflow_path": native["path"], "run": native,
        "jobs": state["native_jobs"], "attestation": state["attestation"],
    }
    assert result["main_proofs"]["network"] == state["python_evidence"]["main_proofs"]["network"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing-run", "wrong-head", "failed-run", "missing-artifact", "wrong-tree",
        "wrong-attempt", "old-attempt-artifact", "native-job-red", "aggregate-red",
        "network-disabled", "network-skipped", "network-job-red", "network-job-missing",
        "network-job-duplicate",
    ],
)
def test_main_gate_refuses_incomplete_observed_proofs(main_proof_transport, fault):
    state = main_proof_transport
    run = state["python_run"]
    if fault == "missing-run":
        state["native_runs"] = []
    elif fault == "wrong-head":
        state["native_run"]["head_sha"] = "e" * 40
    elif fault == "failed-run":
        state["native_run"]["conclusion"] = "failure"
    elif fault == "missing-artifact":
        state["native_artifact_missing"] = True
    elif fault == "wrong-tree":
        state["attestation"]["tree"] = "e" * 40
    elif fault == "wrong-attempt":
        state["attestation"]["attempt"] -= 1
    elif fault == "old-attempt-artifact":
        state["native_artifact_attempt_delta"] = -1
    elif fault in {"native-job-red", "aggregate-red"}:
        name = "TSan" if fault == "native-job-red" else "Native proof"
        next(job for job in state["native_jobs"] if job["name"] == name)["conclusion"] = "failure"
    elif fault in {"network-job-red", "network-job-missing", "network-job-duplicate"}:
        job = next(
            job for job in state["python_jobs"] if job["name"] == state["network_name"]
        )
        if fault == "network-job-missing":
            state["python_jobs"].remove(job)
        elif fault == "network-job-duplicate":
            state["python_jobs"].append(copy.deepcopy(job))
        else:
            job["conclusion"] = "skipped"
    else:
        network = state["python_evidence"]["main_proofs"]["network"]
        network["executed"] = []
        if fault == "network-skipped":
            network["skipped"] = [network["node_id"]]
    with pytest.raises(evidence.GateRejected):
        publishing.gate_for_run(REPO, run["id"], head=run["head_sha"])


@pytest.mark.parametrize(
    "fault",
    ["in_progress", "failure", "cancelled", "missing-artifact", "job-failure", "wrong-tree"],
)
def test_main_gate_never_falls_back_from_newer_bad_native_proof(main_proof_transport, fault):
    state = main_proof_transport
    run, native = state["python_run"], state["native_run"]
    older = {**native, "id": native["id"] - 1, "run_attempt": 1}
    state["native_runs"] = [older, native]
    if fault == "in_progress":
        native.update(status="in_progress", conclusion=None)
    elif fault == "missing-artifact":
        state["native_artifact_missing"] = True
    elif fault == "job-failure":
        next(job for job in state["native_jobs"] if job["name"] == "TSan")[
            "conclusion"
        ] = "failure"
    elif fault == "wrong-tree":
        state["attestation"]["tree"] = "e" * 40
    else:
        native["conclusion"] = fault
    with pytest.raises(evidence.GateRejected):
        publishing.gate_for_run(REPO, run["id"], head=run["head_sha"])
    assert_native_discovery(state)
    assert not any(
        f"/runs/{older['id']}/" in route or f"/artifacts/{older['id'] * 100}/" in route
        for route in state["calls"]
    ), "an older successful native run must never substitute for the newest matching run"


def test_pr_gate_keeps_its_existing_contract_without_main_discovery(main_proof_transport):
    state = main_proof_transport
    gate, run = state["python_evidence"], state["python_run"]
    gate.update(event="pull_request", pr=7, base=BASE)
    gate.pop("main_proofs")
    gate["jobs"].pop(state["network_name"], None)
    state["python_jobs"][:] = [
        job for job in state["python_jobs"] if job["name"] != state["network_name"]
    ]
    run.update(event="pull_request", path=".github/workflows/ci.yml")
    result = publishing.gate_for_run(
        REPO, run["id"], head=run["head_sha"], base=BASE, pr=7
    )
    assert result == gate
    assert not any("native-feed-post.yml" in route for route in state["calls"])


@pytest.fixture
def candidate_marker_transport(main_proof_transport, monkeypatch):
    state = main_proof_transport
    gate, run = state["python_evidence"], state["python_run"]
    gate.update(event="pull_request", pr=7, base=BASE)
    gate.pop("main_proofs")
    state["python_jobs"][:] = [
        job for job in state["python_jobs"] if job["name"] != state["network_name"]
    ]
    for job in state["python_jobs"]:
        job["completed_at"] = "2026-11-07T11:59:00Z"
    run.update(
        event="pull_request", path=".github/workflows/ci.yml",
        pull_requests=[{"number": 7}],
    )
    pr = {
        "number": 7, "merged_at": "2026-11-07T12:00:00Z",
        "head": {"sha": run["head_sha"], "repo": {"full_name": REPO}},
        "body": automation.body({
            "kind": "release", "version": "0.3.1", "base": BASE,
            "generated_head": run["head_sha"],
        }),
    }
    state.update(candidate_pr=pr, candidate_reads=[])

    def api(repo, endpoint, method="GET", data=None):
        assert repo == REPO and method == "GET" and data is None
        state["candidate_reads"].append(endpoint)
        if endpoint == (
            "actions/workflows/ci.yml/runs?event=pull_request&head_sha="
            + run["head_sha"] + "&per_page=100&page=1"
        ):
            return {"workflow_runs": copy.deepcopy(state["python_runs"])}
        assert endpoint == (
            f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs"
            "?per_page=100&page=1"
        )
        return contract.object_json(evidence.gh_bytes(f"repos/{repo}/{endpoint}"))

    monkeypatch.setattr(automation, "api", api)
    return state


@pytest.mark.parametrize("body", [None, "", "Release notes without machine metadata"])
def test_candidate_gate_refuses_missing_release_marker(candidate_marker_transport, body):
    state = candidate_marker_transport
    pr = state["candidate_pr"]
    pr["body"] = body
    assert automation.marker(pr) is None
    with pytest.raises(ValueError, match=r"^pre-merge release marker missing$"):
        publishing.candidate_gate(REPO, pr)
    assert len(state["candidate_reads"]) == 1
    assert state["calls"] == state["artifact_observations"] == []


def test_candidate_gate_accepts_release_marker_with_real_gate(candidate_marker_transport):
    state = candidate_marker_transport
    result = publishing.candidate_gate(REPO, state["candidate_pr"])
    assert result == state["python_evidence"]
    assert len(state["candidate_reads"]) == 2
    run = state["python_run"]
    assert f"repos/{REPO}/actions/artifacts/{run['id'] * 100}/zip" in state["calls"]
    assert not any("native-feed-post.yml" in route for route in state["calls"])


@pytest.mark.parametrize("missing", ["head", "run"])
def test_candidate_gate_keeps_ci_priority_before_missing_marker(
    candidate_marker_transport, missing
):
    state = candidate_marker_transport
    pr = state["candidate_pr"]
    pr["body"] = None
    if missing == "head":
        pr["head"]["sha"] = None
    else:
        state["python_runs"] = []
    with pytest.raises(ValueError, match=r"^pre-merge CI evidence missing$"):
        publishing.candidate_gate(REPO, pr)
    assert len(state["candidate_reads"]) == int(missing == "run")
    assert state["calls"] == []


class PublicationFetchBoundary(RuntimeError):
    """Stop immediately before prepare's first external git fetch."""


@pytest.fixture
def main_callback_transport(main_proof_transport, tmp_path, monkeypatch):
    """Real prepare/gate/validators; only observed API bytes and I/O boundaries vary."""
    state = main_proof_transport
    source = state["python_run"]["head_sha"]
    generated = contract.sha256(f"{source}:release-pr".encode())[:40]
    event = contract.object_json((FIXTURES / "ready-event.json").read_bytes())
    pr = {
        "number": 7, "merged": True, "merged_at": "2026-11-07T12:00:00Z",
        "merge_commit_sha": source, "base": {"ref": "main", "sha": BASE},
        "head": {"sha": generated, "repo": {"full_name": REPO}},
        "user": {"login": "release[bot]"},
        "body": automation.body({
            "kind": "release", "version": "0.3.1",
            "generated_head": generated, "base": BASE, "event_id": event["event_id"],
        }),
    }
    state.update(
        callback_pr=pr, api_calls=[], gate_attempts=[], validated_pairs=[], effects=[],
        callback_root=tmp_path, output=tmp_path / "publication",
        callback_report=receipts.receipt(None),
    )
    state["report_before"] = copy.deepcopy(state["callback_report"])

    def api(repo, endpoint, method="GET", data=None):
        assert repo == REPO and method == "GET" and data is None
        state["api_calls"].append(endpoint)
        if endpoint.startswith("actions/runs/"):
            return contract.object_json(evidence.gh_bytes(f"repos/{repo}/{endpoint}"))
        if endpoint == f"commits/{source}/pulls?per_page=100":
            return [copy.deepcopy(pr)]
        if endpoint == "pulls/7":
            return copy.deepcopy(pr)
        raise AssertionError(f"unexpected callback API read: {endpoint}")

    real_gate = publishing.gate_for_run

    def observe_real_gate(repo, run_id, **kwargs):
        state["gate_attempts"].append((repo, run_id, kwargs))
        result = real_gate(repo, run_id, **kwargs)
        state["validated_pairs"].append(copy.deepcopy(result))
        return result

    def before_git_fetch(*args, **kwargs):
        state["effects"].append(("git", args))
        assert not kwargs
        assert args == ("fetch", "origin", source)
        # This sentinel is after the actual gate, before the fetch is executed.
        raise PublicationFetchBoundary("before git fetch origin exact main head")

    def reject_snapshot(*args, **kwargs):
        state["effects"].append(("snapshot", args))
        raise AssertionError("prepare must not snapshot before complete proof")

    def reject_process(command, **kwargs):
        state["effects"].append(("process", command))
        raise AssertionError("prepare must not download or publish before complete proof")

    def reject_publication_transport(*args, **kwargs):
        state["effects"].append(("publication-transport", args))
        raise AssertionError("publication byte transport reached before the fetch boundary")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("RELEASE_BOT_LOGIN", "release[bot]")
    monkeypatch.setattr(publishing, "api", api)
    monkeypatch.setattr(publishing, "gate_for_run", observe_real_gate)
    monkeypatch.setattr(publishing, "git", before_git_fetch)
    monkeypatch.setattr(publishing, "snapshot", reject_snapshot)
    monkeypatch.setattr(publishing.subprocess, "run", reject_process)
    monkeypatch.setattr(publishing, "fetch", reject_publication_transport)
    monkeypatch.setattr(publishing, "gh_bytes", reject_publication_transport)
    return state


def prepare_main_callback(state, source, *, run_id=None):
    if run_id is None:
        run_id = state["python_run" if source == "CI" else "native_run"]["id"]
    return publishing.prepare(
        state["callback_root"], REPO, run_id, state["output"], state["callback_report"]
    )


def assert_callback_created_nothing(state):
    assert state["effects"] == []
    assert not state["output"].exists()
    assert state["callback_report"] == state["report_before"]
    assert state["validated_pairs"] == []


def assert_callback_validated_current_pair(state, *, since=0):
    python, native = state["python_run"], state["native_run"]
    calls = state["calls"][since:]
    result = state["validated_pairs"][-1]
    assert result["run_id"] == python["id"] and result["attempt"] == python["run_attempt"]
    assert result["main_proofs"]["network"] == state["python_evidence"]["main_proofs"]["network"]
    assert result["main_proofs"]["native"] == {
        "workflow_path": native["path"], "run": native, "jobs": state["native_jobs"],
        "attestation": state["attestation"],
    }
    assert state["gate_attempts"][-1] == (REPO, python["id"], {"head": python["head_sha"]})
    assert "pulls/7" in state["api_calls"]
    assert any("/actions/workflows/native-feed-post.yml/runs?" in route for route in calls)
    prefix = f"repos/{REPO}/actions"
    for run, artifact in ((python, "release-evidence"), (native, "native-evidence")):
        assert (
            f"{prefix}/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
            in calls
        )
        assert f"{prefix}/artifacts/{run['id'] * 100}/zip" in calls
        assert (
            run["id"], run["run_attempt"], f"{artifact}-{run['run_attempt']}"
        ) in state["artifact_observations"]
    assert not state["output"].exists()
    assert state["callback_report"] == state["report_before"]


@pytest.fixture
def refreshed_release_pr(main_callback_transport, monkeypatch):
    state = main_callback_transport
    refreshed = copy.deepcopy(state["callback_pr"])
    real_api = publishing.api

    def api(repo, endpoint, method="GET", data=None):
        observed = real_api(repo, endpoint, method, data)
        return copy.deepcopy(refreshed) if endpoint == "pulls/7" else observed

    monkeypatch.setattr(publishing, "api", api)
    return state, refreshed


def test_prepare_refuses_missing_refreshed_release_marker(refreshed_release_pr):
    state, refreshed = refreshed_release_pr
    refreshed["body"] = None
    assert automation.marker(state["callback_pr"]) is not None
    assert automation.marker(refreshed) is None
    with pytest.raises(ValueError, match=r"^release PR marker missing$"):
        prepare_main_callback(state, "CI")
    source = state["python_run"]["head_sha"]
    assert state["api_calls"] == [
        f"actions/runs/{state['python_run']['id']}",
        f"commits/{source}/pulls?per_page=100", "pulls/7",
    ]
    assert state["calls"] == [f"repos/{REPO}/actions/runs/{state['python_run']['id']}"]
    assert state["gate_attempts"] == []
    assert_callback_created_nothing(state)


def test_prepare_keeps_owner_priority_before_missing_marker(refreshed_release_pr):
    state, refreshed = refreshed_release_pr
    refreshed.update(body=None, user={"login": "other-owner"})
    with pytest.raises(ValueError, match=r"^unexpected release PR owner$"):
        prepare_main_callback(state, "CI")
    assert state["gate_attempts"] == []
    assert_callback_created_nothing(state)


def test_prepare_accepts_refreshed_release_marker(refreshed_release_pr):
    state, _refreshed = refreshed_release_pr
    with pytest.raises(
        PublicationFetchBoundary, match=r"^before git fetch origin exact main head$"
    ):
        prepare_main_callback(state, "CI")
    assert_callback_validated_current_pair(state)
    assert state["effects"] == [("git", ("fetch", "origin", state["python_run"]["head_sha"]))]


@pytest.mark.parametrize("first_source", ["CI", "Native feed"])
def test_main_callbacks_validate_the_same_pair_before_fetch(main_callback_transport, first_source):
    state = main_callback_transport
    second_source = "Native feed" if first_source == "CI" else "CI"
    for source in (first_source, second_source):
        since = len(state["calls"])
        with pytest.raises(PublicationFetchBoundary, match="before git fetch"):
            prepare_main_callback(state, source)
        assert_callback_validated_current_pair(state, since=since)
    assert len(state["gate_attempts"]) == len(state["validated_pairs"]) == 2
    assert state["validated_pairs"][0] == state["validated_pairs"][1]
    assert state["effects"] == [
        ("git", ("fetch", "origin", state["python_run"]["head_sha"]))
    ] * 2
    # Two unit callbacks reach one validated pair. Publication/idempotency is not run.


@pytest.mark.parametrize("first_source", ["CI", "Native feed"])
@pytest.mark.parametrize("counterpart_state", ["missing", "pending", "failure"])
def test_main_callback_completion_order_waits_for_counterpart(
    main_callback_transport, incident_transport, first_source, counterpart_state
):
    state = main_callback_transport
    counterpart = "native" if first_source == "CI" else "python"
    run = state[f"{counterpart}_run"]
    if counterpart_state == "missing":
        state[f"{counterpart}_runs"] = []
        workflow = "native-feed-post.yml" if counterpart == "native" else "ci-post.yml"
        reason = f"missing exact-head main {workflow} run"
    else:
        if counterpart_state == "pending":
            run.update(status="in_progress", conclusion=None)
        else:
            run["conclusion"] = "failure"
        reason = (
            "newest native proof has not completed successfully" if counterpart == "native"
            else "CI has not completed successfully"
        )
    if counterpart_state == "failure":
        with pytest.raises(ValueError) as rejected:
            prepare_main_callback(state, first_source)
        assert str(rejected.value) == reason
    else:
        assert prepare_main_callback(state, first_source) == {
            "eligible": "false", "waiting": "main-proofs-pending",
        }
        collect_callback_without_incident(state, incident_transport)
    assert_callback_created_nothing(state)
    if counterpart == "python" and counterpart_state == "missing":
        assert state["gate_attempts"] == []
    else:
        assert state["gate_attempts"] == [
            (REPO, state["python_run"]["id"], {"head": state["python_run"]["head_sha"]})
        ]
    # The later counterpart completion changes the input; both real gates remain.
    run.update(status="completed", conclusion="success")
    state[f"{counterpart}_runs"] = [run]
    second_source = "Native feed" if first_source == "CI" else "CI"
    since = len(state["calls"])
    with pytest.raises(PublicationFetchBoundary, match="before git fetch"):
        prepare_main_callback(state, second_source)
    assert_callback_validated_current_pair(state, since=since)
    assert state["effects"] == [("git", ("fetch", "origin", run["head_sha"]))]


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize("slot", ["python", "native"])
@pytest.mark.parametrize("readiness", ["missing", "queued", "in_progress"])
def test_main_callbacks_wait_without_incident_for_observed_pending_pair(
    main_callback_transport, observed_main_parser, incident_transport, source, slot, readiness
):
    state = main_callback_transport
    run = state[f"{slot}_run"]
    if readiness == "missing":
        state[f"{slot}_runs"] = []
    else:
        run.update(status=readiness, conclusion=None)
        state[f"{slot}_artifact_missing"] = True
        for job in state[f"{slot}_jobs"]:
            job.update(status=readiness, conclusion=None)
    assert prepare_main_callback(state, source) == {
        "eligible": "false", "waiting": "main-proofs-pending",
    }
    assert_callback_created_nothing(state)
    collect_callback_without_incident(state, incident_transport)
    workflow = "ci-post.yml" if slot == "python" else "native-feed-post.yml"
    assert any(f"/actions/workflows/{workflow}/runs?" in row for row in state["calls"])
    observed = {"workflow_runs": state[f"{slot}_runs"]}
    assert observed in state["parsed_objects"], "the real parser did not consume readiness"
    assert state["main_validations"] == []
    if slot == "python" and readiness == "missing":
        assert state["gate_attempts"] == []  # There is no Python run ID to gate yet.
    else:
        python = state["python_run"]
        assert state["gate_attempts"] == [(REPO, python["id"], {"head": python["head_sha"]})]

    # The same lifecycle consumes new completion input and reaches the real gate.
    run.update(status="completed", conclusion="success")
    state[f"{slot}_runs"] = [run]
    state[f"{slot}_artifact_missing"] = False
    for job in state[f"{slot}_jobs"]:
        job.update(status="completed", conclusion="success")
    with pytest.raises(PublicationFetchBoundary, match="before git fetch"):
        prepare_main_callback(state, source)
    assert_callback_validated_current_pair(state)
    assert state["main_validations"][-1] == state["validated_pairs"][-1]
    assert incident_transport["reads"] == incident_transport["writes"] == []


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize("readiness", ["missing", "queued", "in_progress"])
@pytest.mark.parametrize(
    "fault,reason",
    [
        ("stale-attempt", "stale CI attempt"),
        ("network-non-object", "network: expected an object"),
        (
            "network-job-red",
            "required job 'Live PyPI hashes' missing, duplicated, failed, cancelled or skipped",
        ),
        ("wrong-tree", "network: wrong tree"),
        ("e2e-incomplete", "incomplete E2E evidence"),
    ],
)
@pytest.mark.parametrize("existing_issue", [False, True], ids=["new-issue", "existing-issue"])
def test_main_callbacks_refuse_invalid_python_while_native_pending(
    main_callback_transport, observed_main_parser, incident_transport,
    source, readiness, fault, reason, existing_issue,
):
    state = main_callback_transport
    assert observed_main_parser is state
    python, native = state["python_run"], state["native_run"]
    current_python = copy.deepcopy(python)
    proof = state["python_evidence"]
    if readiness == "missing":
        state["native_runs"] = []
    else:
        native.update(status=readiness, conclusion=None)
        state["native_artifact_missing"] = True
        for job in state["native_jobs"]:
            job.update(status=readiness, conclusion=None)
    if fault == "stale-attempt":
        proof["attempt"] -= 1
    elif fault == "network-non-object":
        proof["main_proofs"]["network"] = []
    elif fault == "network-job-red":
        next(job for job in state["python_jobs"] if job["name"] == state["network_name"])[
            "conclusion"
        ] = "failure"
    elif fault == "wrong-tree":
        original_tree = proof["tree"]
        proof["tree"] = contract.sha256(b"another completed Python evidence tree")[:40]
        assert proof["tree"] != original_tree
        assert proof["main_proofs"]["network"]["tree"] == original_tree
    else:
        assert fault == "e2e-incomplete"
        for suite in ("docker", "database"):
            proof["e2e"][suite].pop()
    assert python == current_python
    assert python["status"] == "completed" and python["conclusion"] == "success"
    with pytest.raises(evidence.GateRejected) as rejected:
        prepare_main_callback(state, source)
    assert type(rejected.value) is evidence.GateRejected
    assert str(rejected.value) == reason
    assert_callback_created_nothing(state)
    assert state["gate_attempts"] == [(REPO, python["id"], {"head": python["head_sha"]})]
    assert_native_discovery(state)
    assert {"workflow_runs": state["native_runs"]} in state["parsed_objects"]
    if readiness != "missing":
        assert native in state["parsed_objects"]
    assert proof in state["parsed_objects"]
    assert {"jobs": state["python_jobs"]} in state["parsed_objects"]
    assert state["waiting_inputs"] == [
        (proof, python, state["python_jobs"], {"head": python["head_sha"], "base": None}),
    ]
    assert state["waiting_validations"] == state["gate_inputs"] == state["main_inputs"] == []
    assert state["artifact_observations"] == [
        (python["id"], python["run_attempt"], f"release-evidence-{python['run_attempt']}"),
    ]
    incidents = incident_transport
    if existing_issue:
        incidents["issues"].append({
            "number": 41, "state": "closed",
            "body": "<!-- release-chain-data:run-930 -->\nolder observation",
        })
    report = reporting.collect(
        state["callback_root"] / "downloaded-stages", failed=True, stage="prepare-publication"
    )
    assert report["state"] == "failed" and report["event_id"] is None
    assert report["attempt"] == 930 and report["sequence"] == 2
    reporting.incident(REPO, report, "release-owner", "prepare-publication")
    assert len(incidents["reads"]) == 1
    assert [(route, method) for route, method, _ in incidents["writes"]] == (
        [("issues/41/comments", "POST"), ("issues/41", "PATCH")] if existing_issue else
        [("issues", "POST"), ("issues/41", "PATCH")]
    )
    assert len(incidents["issues"]) == 1
    issue = incidents["issues"][0]
    assert issue["state"] == "open" and issue["title"] == "Data release failed: run-930"
    assert "**failed**" in issue["body"] and "`prepare-publication`" in issue["body"]
    assert_callback_created_nothing(state)


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize("readiness", ["missing", "queued", "in_progress"])
def test_main_callbacks_wait_with_valid_python_while_native_pending_positive_twin(
    main_callback_transport, observed_main_parser, incident_transport, source, readiness
):
    state = main_callback_transport
    assert observed_main_parser is state
    python, native = state["python_run"], state["native_run"]
    if readiness == "missing":
        state["native_runs"] = []
    else:
        native.update(status=readiness, conclusion=None)
        state["native_artifact_missing"] = True
        for job in state["native_jobs"]:
            job.update(status=readiness, conclusion=None)
    assert python["status"] == "completed" and python["conclusion"] == "success"
    assert prepare_main_callback(state, source) == {
        "eligible": "false", "waiting": "main-proofs-pending",
    }
    assert_callback_created_nothing(state)
    collect_callback_without_incident(state, incident_transport)
    assert incident_transport["issues"] == incident_transport["comments"] == []
    assert state["gate_attempts"] == [(REPO, python["id"], {"head": python["head_sha"]})]
    assert_native_discovery(state)
    assert {"workflow_runs": state["native_runs"]} in state["parsed_objects"]
    if readiness != "missing":
        assert native in state["parsed_objects"]
    proof = state["python_evidence"]
    assert proof in state["parsed_objects"]
    assert {"jobs": state["python_jobs"]} in state["parsed_objects"]
    assert state["waiting_inputs"] == [
        (proof, python, state["python_jobs"], {"head": python["head_sha"], "base": None}),
    ]
    assert state["waiting_validations"] == [proof]
    assert state["gate_inputs"] == state["main_inputs"] == []
    assert state["artifact_observations"] == [
        (python["id"], python["run_attempt"], f"release-evidence-{python['run_attempt']}"),
    ]


@pytest.mark.parametrize("existing_issue", [False, True], ids=["new-issue", "existing-issue"])
def test_callback_reporting_failure_still_opens_or_updates_incident(
    tmp_path, incident_transport, existing_issue
):
    incidents = incident_transport
    if existing_issue:
        incidents["issues"].append({
            "number": 41, "state": "closed",
            "body": "<!-- release-chain-data:run-930 -->\nolder observation",
        })
    report = reporting.collect(tmp_path, failed=True, stage="prepare-publication")
    assert report["state"] == "failed" and report["event_id"] is None
    assert report["attempt"] == 930 and report["sequence"] == 2
    reporting.incident(REPO, report, "release-owner", "prepare-publication")
    assert len(incidents["reads"]) == 1
    assert [(route, method) for route, method, _ in incidents["writes"]] == (
        [("issues/41/comments", "POST"), ("issues/41", "PATCH")] if existing_issue else
        [("issues", "POST"), ("issues/41", "PATCH")]
    )
    issue = incidents["issues"][0]
    assert issue["state"] == "open"
    assert issue["title"] == "Data release failed: run-930"
    assert "**failed**" in issue["body"] and "`prepare-publication`" in issue["body"]


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize("slot", ["python", "native"])
@pytest.mark.parametrize(
    "fault", ["failure", "cancelled", "wrong-head", "wrong-attempt",
              "malformed-proof", "malformed-json", "missing-artifact"],
)
def test_main_callbacks_keep_completed_and_malformed_proofs_decisive(
    main_callback_transport, observed_main_parser, incident_transport, source, slot, fault
):
    state = main_callback_transport
    run = state[f"{slot}_run"]
    callback_id = None
    if fault in {"failure", "cancelled"}:
        run["conclusion"] = fault
        reason = (
            "CI has not completed successfully" if slot == "python"
            else "newest native proof has not completed successfully"
        )
    elif fault == "wrong-head":
        # The list still has the requested head; the current detail read contradicts it.
        state[f"{slot}_runs"] = [copy.deepcopy(run)]
        if source == ("CI" if slot == "python" else "Native feed"):
            older_callback = {**run, "id": run["id"] - 1, "run_attempt": 1}
            state[f"{slot}_runs"].append(older_callback)
            callback_id = older_callback["id"]
        run["head_sha"] = contract.sha256(b"contradictory current head")[:40]
        reason = (
            "counterpart is not exact-head main CI" if slot == "python"
            else "newest native proof has not completed successfully"
        )
    elif fault == "wrong-attempt":
        if slot == "python":
            state["python_evidence"]["attempt"] -= 1
            reason = "stale CI attempt"
        else:
            state["attestation"]["attempt"] -= 1
            reason = "native: wrong attempt"
    elif fault == "malformed-proof":
        if slot == "python":
            state["python_evidence"]["main_proofs"]["network"] = []
            reason = "network: expected an object"
        else:
            state["attestation"]["legs"] = []
            reason = "native legs: expected an object"
    elif fault == "malformed-json":
        filename = "evidence.json" if slot == "python" else "native-evidence.json"
        state["artifact_raw"][filename] = b'{"run_id":1,"run_id":2}'
        reason = "duplicate JSON key: run_id"
    else:
        state[f"{slot}_artifact_missing"] = True
        artifact = "release-evidence" if slot == "python" else "native-evidence"
        reason = f"missing unique {artifact}-{run['run_attempt']} artifact"
    with pytest.raises(ValueError) as rejected:
        prepare_main_callback(state, source, run_id=callback_id)
    assert str(rejected.value) == reason
    assert_callback_created_nothing(state)
    assert state["parsed_objects"], "the real parser must observe the supplied refusal input"
    if fault in {"wrong-attempt", "malformed-proof"}:
        assert state["gate_inputs"], "the real validator must consume the artifact mutation"
    if fault == "malformed-json":
        assert b'{"run_id":1,"run_id":2}' in state["parsed_bytes"]
    # A real refusal must still take the actionable failure path.
    report = reporting.collect(
        state["callback_root"], failed=True, stage="prepare-publication"
    )
    reporting.incident(REPO, report, "release-owner", "prepare-publication")
    assert [(route, method) for route, method, _ in incident_transport["writes"]] == [
        ("issues", "POST"), ("issues/41", "PATCH"),
    ]


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize(
    "fault",
    ["native-tree", "native-attempt", "native-leg", "native-job-red",
     "network-skipped", "network-job-red", "missing-native-artifact"],
)
def test_main_callbacks_refuse_invalid_proof_before_any_preparation(
    main_callback_transport, source, fault
):
    state = main_callback_transport
    if fault == "native-tree":
        state["attestation"]["tree"] = contract.sha256(b"other tree")[:40]
        reason = "native: wrong tree"
    elif fault == "native-attempt":
        state["attestation"]["attempt"] -= 1
        reason = "native: wrong attempt"
    elif fault == "native-leg":
        state["attestation"]["legs"].pop("tsan")
        reason = "native: missing or unexpected leg"
    elif fault == "native-job-red":
        next(job for job in state["native_jobs"] if job["name"] == "TSan")[
            "conclusion"
        ] = "failure"
        reason = "required job 'TSan' missing, duplicated, failed, cancelled or skipped"
    elif fault == "network-skipped":
        network = state["python_evidence"]["main_proofs"]["network"]
        network.update(executed=[], skipped=[network["node_id"]])
        reason = "network executed: missing nonempty inventory"
    elif fault == "network-job-red":
        next(job for job in state["python_jobs"] if job["name"] == state["network_name"])[
            "conclusion"
        ] = "failure"
        reason = "required job 'Live PyPI hashes' missing, duplicated, failed, cancelled or skipped"
    else:
        state["native_artifact_missing"] = True
        reason = f"missing unique native-evidence-{state['native_run']['run_attempt']} artifact"
    with pytest.raises(evidence.GateRejected) as rejected:
        prepare_main_callback(state, source)
    assert str(rejected.value) == reason
    assert state["gate_attempts"] == [
        (REPO, state["python_run"]["id"], {"head": state["python_run"]["head_sha"]})
    ]
    assert_callback_created_nothing(state)


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize("fault", ["owner", "changed-pr-head", "ambiguous-marker"])
def test_main_callbacks_keep_release_authorization_before_gate(
    main_callback_transport, source, fault
):
    state = main_callback_transport
    pr = state["callback_pr"]
    if fault == "owner":
        pr["user"]["login"] = "other[bot]"
        reason = "unexpected release PR owner"
    elif fault == "changed-pr-head":
        pr["head"]["sha"] = contract.sha256(b"changed PR head")[:40]
        reason = "release PR head changed"
    else:
        pr["body"] += pr["body"]
        reason = "ambiguous release PR marker"
    with pytest.raises(ValueError) as rejected:
        prepare_main_callback(state, source)
    assert str(rejected.value) == reason
    assert state["gate_attempts"] == []
    assert_callback_created_nothing(state)


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize(
    "fault", ["pending", "failure", "cancelled", "stale-attempt", "network-red"]
)
def test_main_callbacks_never_replace_newer_bad_python_with_old_green(
    main_callback_transport, source, fault
):
    state = main_callback_transport
    python = state["python_run"]
    older = {**python, "id": python["id"] - 1, "run_attempt": 1}
    state["python_runs"] = [python, older]
    if fault == "pending":
        python.update(status="in_progress", conclusion=None)
        reason = "CI has not completed successfully"
    elif fault in {"failure", "cancelled"}:
        python["conclusion"] = fault
        reason = "CI has not completed successfully"
    elif fault == "stale-attempt":
        state["python_evidence"]["attempt"] -= 1
        reason = "stale CI attempt"
    else:
        next(job for job in state["python_jobs"] if job["name"] == state["network_name"])[
            "conclusion"
        ] = "failure"
        reason = "required job 'Live PyPI hashes' missing, duplicated, failed, cancelled or skipped"
    callback_id = older["id"] if source == "CI" else state["native_run"]["id"]
    if fault == "pending":
        assert prepare_main_callback(state, source, run_id=callback_id) == {
            "eligible": "false", "waiting": "main-proofs-pending",
        }
    else:
        with pytest.raises(evidence.GateRejected) as rejected:
            prepare_main_callback(state, source, run_id=callback_id)
        assert str(rejected.value) == reason
    assert state["gate_attempts"] == [(REPO, python["id"], {"head": python["head_sha"]})]
    assert not any(
        f"/runs/{older['id']}/" in route or f"/artifacts/{older['id'] * 100}/" in route
        for route in state["calls"]
    ), "the old callback may be read; its jobs/artifacts cannot replace the newer Python run"
    assert_callback_created_nothing(state)


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize(
    "fault", ["pending", "failure", "cancelled", "missing-artifact", "wrong-tree"]
)
def test_main_callbacks_never_replace_newer_bad_native_with_old_green(
    main_callback_transport, source, fault
):
    state = main_callback_transport
    native = state["native_run"]
    older = {**native, "id": native["id"] - 1, "run_attempt": 1}
    state["native_runs"] = [native, older]
    if fault == "pending":
        native.update(status="in_progress", conclusion=None)
        reason = "newest native proof has not completed successfully"
    elif fault in {"failure", "cancelled"}:
        native["conclusion"] = fault
        reason = "newest native proof has not completed successfully"
    elif fault == "missing-artifact":
        state["native_artifact_missing"] = True
        reason = f"missing unique native-evidence-{native['run_attempt']} artifact"
    else:
        state["attestation"]["tree"] = contract.sha256(b"other native tree")[:40]
        reason = "native: wrong tree"
    callback_id = older["id"] if source == "Native feed" else state["python_run"]["id"]
    if fault == "pending":
        assert prepare_main_callback(state, source, run_id=callback_id) == {
            "eligible": "false", "waiting": "main-proofs-pending",
        }
    else:
        with pytest.raises(evidence.GateRejected) as rejected:
            prepare_main_callback(state, source, run_id=callback_id)
        assert str(rejected.value) == reason
    assert state["gate_attempts"] == [
        (REPO, state["python_run"]["id"], {"head": state["python_run"]["head_sha"]})
    ]
    assert_native_discovery(state)
    assert not any(
        f"/runs/{older['id']}/" in route or f"/artifacts/{older['id'] * 100}/" in route
        for route in state["calls"]
    ), "an old native callback cannot select old native jobs/artifacts"
    assert_callback_created_nothing(state)


@pytest.mark.parametrize("source", ["CI", "Native feed"])
def test_main_callbacks_use_reobserved_current_attempt(main_callback_transport, source):
    state = main_callback_transport
    python, native = state["python_run"], state["native_run"]
    state["python_runs"] = [copy.deepcopy(python)]
    state["native_runs"] = [copy.deepcopy(native)]
    for run in (python, native):
        run["run_attempt"] += 1
    state["python_evidence"]["attempt"] = python["run_attempt"]
    state["python_evidence"]["main_proofs"]["network"]["attempt"] = python["run_attempt"]
    state["attestation"]["attempt"] = native["run_attempt"]
    with pytest.raises(PublicationFetchBoundary, match="before git fetch"):
        prepare_main_callback(state, source)
    assert_callback_validated_current_pair(state)
    for run in (python, native):
        assert not any(
            f"/runs/{run['id']}/attempts/{run['run_attempt'] - 1}/" in route
            for route in state["calls"]
        )


@pytest.mark.parametrize("source", ["CI", "Native feed"])
@pytest.mark.parametrize("slot", ["python", "native"])
def test_main_callbacks_refuse_artifact_from_previous_attempt(
    main_callback_transport, source, slot
):
    state = main_callback_transport
    state[f"{slot}_artifact_attempt_delta"] = -1
    run = state[f"{slot}_run"]
    artifact = "release-evidence" if slot == "python" else "native-evidence"
    with pytest.raises(evidence.GateRejected) as rejected:
        prepare_main_callback(state, source)
    assert str(rejected.value) == f"missing unique {artifact}-{run['run_attempt']} artifact"
    assert state["gate_attempts"] == [
        (REPO, state["python_run"]["id"], {"head": state["python_run"]["head_sha"]})
    ]
    assert_callback_created_nothing(state)


def test_main_callback_old_green_transport_is_a_usable_positive_twin(main_callback_transport):
    state = main_callback_transport
    python, native = state["python_run"], state["native_run"]
    older_python = {**python, "id": python["id"] - 1, "run_attempt": 1}
    older_native = {**native, "id": native["id"] - 1, "run_attempt": 1}
    state["python_runs"], state["native_runs"] = [older_python], [older_native]
    with pytest.raises(PublicationFetchBoundary, match="before git fetch"):
        prepare_main_callback(state, "CI", run_id=older_python["id"])
    result = state["validated_pairs"][-1]
    assert result["run_id"] == older_python["id"] and result["attempt"] == 1
    assert result["main_proofs"]["network"]["run_id"] == older_python["id"]
    assert result["main_proofs"]["native"]["run"]["id"] == older_native["id"]
    assert result["main_proofs"]["native"]["attestation"]["run_id"] == older_native["id"]
    assert state["effects"] == [("git", ("fetch", "origin", python["head_sha"]))]
    assert not state["output"].exists()
    assert state["callback_report"] == state["report_before"]


class PublicationCandidateBoundary(RuntimeError):
    """Stop before the later, independent pre-merge candidate API inventory."""


@pytest.fixture
def publication_main_transport(
    main_callback_transport, observed_main_parser, bootstrap_lock, tmp_path, monkeypatch
):
    """Save a real full gate, then re-observe it through publication's lower I/O."""
    state = main_callback_transport
    python, native = state["python_run"], state["native_run"]
    source = python["head_sha"]
    event = contract.object_json((FIXTURES / "ready-event.json").read_bytes())
    manifest = (FIXTURES / "release-manifest.json").read_bytes()
    pr = state["callback_pr"]
    # Reuse the authored package-byte inputs of test_prepare_passes_adopter_lock_for_previous.
    # These controls validate proof/hash handling; they do not install or serve a package.
    package_directory = tmp_path / "observed-package-bytes"
    package_directory.mkdir()
    for name, raw in {
        "pineforge_data-0.3.1-py3-none-any.whl": b"tested wheel",
        "pineforge_data-0.3.1.tar.gz": b"tested sdist",
    }.items():
        (package_directory / name).write_bytes(raw)
    state["python_evidence"]["distributions"] = evidence.distribution_hashes(package_directory)
    native.update(
        updated_at="2026-11-07T12:00:00Z", display_title="Original completed run title",
    )
    for run, jobs in ((python, state["python_jobs"]), (native, state["native_jobs"])):
        for index, job in enumerate(jobs, 1):
            job.update(
                id=run["id"] * 1000 + index, run_id=run["id"],
                run_attempt=run["run_attempt"], head_sha=source,
                updated_at="2026-11-07T12:00:00Z",
            )
    for leg, inventory in state["attestation"]["legs"].items():
        inventory["artifact"] = f"native-leg-{leg}-{native['run_attempt']}"
        inventory["registration_sha256"] = contract.sha256(contract.json_bytes({
            "tests": [{"name": name} for name in inventory["registered"]],
        }))
    saved = reporting.gate_for_run(REPO, python["id"], head=source)
    assert set(saved["main_proofs"]) == {"network", "native"}
    assert state["main_validations"][-1] == saved
    staged = contract.object_json((FIXTURES / "sample-release-receipt.json").read_bytes())
    staged.update(
        pr=automation.pr_identity(pr), state="deploying",
        deployed=receipts.served(event, source, "0.3.1", saved["distributions"]),
    )
    lock = {**copy.deepcopy(bootstrap_lock), "event": event}
    source_files = {
        contract.RUNTIME: (
            f'DEFAULT_RELEASE_IMAGE = (\n    "{contract.image_ref(event)}"\n)\n'
        ).encode(),
        contract.DOCKERFILE: (
            f"ARG PINEFORGE_RELEASE_IMAGE={contract.image_ref(event)}\n"
            f"ARG PINEFORGE_HUB_COMMIT={event['hub_commit']}\n"
        ).encode(),
        "pyproject.toml": b'[project]\nname = "pineforge-data"\nversion = "0.3.1"\n',
        contract.LOCK: contract.json_bytes(lock), contract.MANIFEST: manifest,
    }
    state.update(
        saved_gate=copy.deepcopy(saved), staged=staged, package_directory=package_directory,
        publication_reads=[], publication_writes=[], fresh_gates=[], local_validations=[],
        candidate_boundaries=[], downloads=[], download_files=[],
    )
    real_transport = evidence.gh_bytes
    real_gate = reporting.gate_for_run
    real_verify_local = reporting.verify_local
    real_temporary_directory = reporting.tempfile.TemporaryDirectory

    def api(repo, endpoint, method="GET", data=None):
        assert repo == REPO
        if method != "GET":
            state["publication_writes"].append((endpoint, method, copy.deepcopy(data)))
            raise AssertionError("publication must not mutate before the candidate boundary")
        assert data is None
        state["publication_reads"].append(endpoint)
        if endpoint == "actions/runs/200":
            return {
                "id": 200, "run_attempt": 1, "path": ".github/workflows/release.yml",
                "event": "workflow_run", "head_sha": source,
                "status": "completed", "conclusion": "success",
            }
        if endpoint == "actions/runs/200/artifacts?per_page=100&page=1":
            return {"artifacts": [{
                "id": 200001, "name": "release-stage-1-50-verify", "expired": False,
            }]}
        if endpoint == "pulls/7":
            return copy.deepcopy(pr)
        if endpoint == f"commits/{source}":
            return {"parents": [{"sha": BASE}]}
        if endpoint == (
            "actions/workflows/ci.yml/runs?event=pull_request&head_sha="
            + pr["head"]["sha"] + "&per_page=100&page=1"
        ):
            state["candidate_boundaries"].append(endpoint)
            assert len(state["fresh_gates"]) == len(state["local_validations"]) == 1
            assert state["local_validations"][0] == lock
            raise PublicationCandidateBoundary("before pre-merge candidate inventory read")
        raise AssertionError(f"unexpected publication API read: {endpoint}")

    def gh_bytes(route, **kwargs):
        if route == f"repos/{REPO}/actions/runs/200/artifacts?per_page=100":
            return contract.json_bytes({"artifacts": [{
                "id": 200001, "name": "release-stage-1-50-verify", "expired": False,
            }]})
        if route == f"repos/{REPO}/actions/artifacts/200001/zip":
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("release-receipt.json", contract.json_bytes(staged))
            return archive.getvalue()
        return real_transport(route, **kwargs)

    def hub_bytes(route, *, accept="application/vnd.github+json"):
        state["publication_reads"].append(route)
        if route == f"repos/{contract.HUB}/releases/tags/v{event['release_version']}":
            assert accept == "application/vnd.github+json"
            return contract.json_bytes({
                "draft": False, "prerelease": False,
                "assets": [{"id": 81, "name": "release-manifest.json"}],
            })
        assert route == f"repos/{contract.HUB}/releases/assets/81"
        assert accept == "application/octet-stream"
        return manifest

    def download(command, *, check):
        assert check is True
        bundle = Path(command[-1])
        assert command == [
            "gh", "run", "download", "200", "--repo", REPO,
            "--name", "validated-release-bundle-1", "--dir", str(bundle),
        ]
        paths = [bundle / "evidence.json", *(bundle / "source" / name for name in source_files)]
        for path in paths:
            if (
                path.name.endswith(".env")
                or not path.resolve().is_relative_to(tmp_path.resolve())
            ):
                raise AssertionError(f"refused publication fixture file: {path}")
        state["download_files"].append([str(path) for path in paths])
        state["downloads"].append(copy.deepcopy(command))
        (bundle / "evidence.json").write_bytes(contract.json_bytes(state["saved_gate"]))
        for name, raw in source_files.items():
            path = bundle / "source" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def observe_gate(repo, run_id, **kwargs):
        result = real_gate(repo, run_id, **kwargs)
        state["fresh_gates"].append(copy.deepcopy(result))
        return result

    def verify_local(root):
        result = real_verify_local(root)
        state["local_validations"].append(copy.deepcopy(result))
        return result

    monkeypatch.setenv("GITHUB_RUN_ID", "300")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(reporting, "api", api)
    monkeypatch.setattr(evidence, "gh_bytes", gh_bytes)
    monkeypatch.setattr(contract, "gh_bytes", hub_bytes)
    monkeypatch.setattr(reporting.subprocess, "run", download)
    monkeypatch.setattr(reporting, "gate_for_run", observe_gate)
    monkeypatch.setattr(reporting, "verify_local", verify_local)
    monkeypatch.setattr(
        reporting.tempfile, "TemporaryDirectory",
        lambda: real_temporary_directory(dir=tmp_path),
    )
    return state


@pytest.mark.parametrize(
    "metadata",
    ["unchanged", "native-updated-at", "native-display-title",
     "native-job-updated-at", "native-job-order"],
)
def test_publication_revalidates_full_main_proofs_despite_benign_metadata(
    publication_main_transport, metadata
):
    state = publication_main_transport
    native = state["native_run"]
    if metadata == "native-updated-at":
        native["updated_at"] = "2026-11-08T00:00:00Z"
    elif metadata == "native-display-title":
        native["display_title"] = "Updated display title for the same completed run"
    elif metadata == "native-job-updated-at":
        state["native_jobs"][0]["updated_at"] = "2026-11-08T00:01:00Z"
    elif metadata == "native-job-order":
        state["native_jobs"].reverse()
    report = receipts.receipt(None)
    with pytest.raises(PublicationCandidateBoundary, match="before pre-merge candidate inventory"):
        reporting.publication(REPO, 200, report)
    fresh = state["fresh_gates"][0]
    assert fresh["main_proofs"]["native"]["run"] == native
    assert fresh["main_proofs"]["native"]["jobs"] == state["native_jobs"]
    assert fresh == state["main_validations"][-1]
    assert native in state["parsed_objects"]
    assert {"jobs": state["native_jobs"]} in state["parsed_objects"]
    assert state["python_evidence"] in state["parsed_objects"]
    assert fresh["distributions"] == evidence.distribution_hashes(state["package_directory"])
    assert (fresh == state["saved_gate"]) is (metadata == "unchanged")
    assert len(state["candidate_boundaries"]) == len(state["downloads"]) == 1
    assert len(state["download_files"][0]) == 6
    assert report["state"] == "deploying" and state["publication_writes"] == []
    # Partial lifecycle: real main proof and local metadata validation precede the sentinel.
    # Pre-merge candidate validation, OCI/PyPI/previous identity and finalization do not run.


def test_publication_refuses_missing_release_marker(publication_main_transport):
    state = publication_main_transport
    state["callback_pr"]["body"] = None
    assert automation.marker(state["callback_pr"]) is None
    report = receipts.receipt(None)
    with pytest.raises(ValueError, match=r"^publication PR identity moved$"):
        reporting.publication(REPO, 200, report)
    assert state["publication_reads"][-1] == "pulls/7"
    assert state["fresh_gates"] == state["local_validations"] == []
    assert state["downloads"] == state["download_files"] == []
    assert state["candidate_boundaries"] == state["publication_writes"] == []
    assert report["state"] == "deploying"


def test_publication_accepts_release_marker_before_candidate_boundary(
    publication_main_transport,
):
    state = publication_main_transport
    report = receipts.receipt(None)
    with pytest.raises(PublicationCandidateBoundary, match="before pre-merge candidate inventory"):
        reporting.publication(REPO, 200, report)
    assert len(state["fresh_gates"]) == len(state["local_validations"]) == 1
    assert len(state["downloads"]) == len(state["candidate_boundaries"]) == 1
    assert state["publication_writes"] == []
    assert report["state"] == "deploying"


@pytest.mark.parametrize(
    "fault,reason,gate_completes",
    [
        ("native-id", "landed CI distributions/evidence moved", True),
        ("native-attempt", "landed CI distributions/evidence moved", True),
        ("tree", "landed CI distributions/evidence moved", True),
        ("native-job-id", "landed CI distributions/evidence moved", True),
        ("native-inventory", "landed CI distributions/evidence moved", True),
        ("native-junit-digest", "landed CI distributions/evidence moved", True),
        ("native-registration-digest", "landed CI distributions/evidence moved", True),
        ("network-junit-digest", "landed CI distributions/evidence moved", True),
        ("distribution", "landed CI distributions/evidence moved", True),
        ("native-head", "newest native proof has not completed successfully", False),
        ("native-workflow", "newest native proof has not completed successfully", False),
        ("native-event", "newest native proof has not completed successfully", False),
        ("native-conclusion", "newest native proof has not completed successfully", False),
        ("native-tested-sha", "native: wrong tested_sha", False),
        ("native-stale-attestation", "native: wrong attempt", False),
        ("native-job-conclusion",
         "required job 'TSan' missing, duplicated, failed, cancelled or skipped", False),
        ("network-inventory", "network: missing unique required live-PyPI row", False),
    ],
)
def test_publication_refuses_real_main_proof_identity_or_content_change(
    publication_main_transport, fault, reason, gate_completes
):
    state = publication_main_transport
    native, attestation = state["native_run"], state["attestation"]
    network = state["python_evidence"]["main_proofs"]["network"]
    leg = attestation["legs"]["tsan"]
    if fault in {"native-id", "native-attempt"}:
        field = "id" if fault == "native-id" else "run_attempt"
        native[field] += 1
        attestation.update(run_id=native["id"], attempt=native["run_attempt"])
        for job in state["native_jobs"]:
            job.update(run_id=native["id"], run_attempt=native["run_attempt"])
        for name, inventory in attestation["legs"].items():
            inventory["artifact"] = f"native-leg-{name}-{native['run_attempt']}"
    elif fault == "tree":
        tree = contract.sha256(b"different landed tree")[:40]
        state["python_evidence"]["tree"] = network["tree"] = attestation["tree"] = tree
    elif fault == "native-job-id":
        state["native_jobs"][0]["id"] += 1
    elif fault == "native-inventory":
        leg["registered"].append("additional-native-row")
        leg["executed"].append("additional-native-row")
        junit = ElementTree.Element("testsuite")
        for name in leg["executed"]:
            ElementTree.SubElement(junit, "testcase", name=name)
        leg["junit_sha256"] = contract.sha256(ElementTree.tostring(junit))
        leg["registration_sha256"] = contract.sha256(contract.json_bytes({
            "tests": [{"name": name} for name in leg["registered"]],
        }))
    elif fault in {"native-junit-digest", "native-registration-digest", "network-junit-digest"}:
        target = network if fault == "network-junit-digest" else leg
        field = "registration_sha256" if fault == "native-registration-digest" else "junit_sha256"
        target[field] = contract.sha256(f"different report bytes: {fault}".encode())
    elif fault == "distribution":
        wheel = state["package_directory"] / "pineforge_data-0.3.1-py3-none-any.whl"
        wheel.write_bytes(wheel.read_bytes() + b"changed")
        state["python_evidence"]["distributions"] = evidence.distribution_hashes(
            state["package_directory"]
        )
    elif fault in {"native-head", "native-workflow", "native-event"}:
        state["native_runs"] = [copy.deepcopy(native)]
        field, value = {
            "native-head": ("head_sha", contract.sha256(b"different head")[:40]),
            "native-workflow": ("path", ".github/workflows/native-feed.yml"),
            "native-event": ("event", "workflow_dispatch"),
        }[fault]
        native[field] = value
    elif fault == "native-conclusion":
        native["conclusion"] = "failure"
    elif fault == "native-tested-sha":
        attestation["tested_sha"] = contract.sha256(b"different tested source")[:40]
    elif fault == "native-stale-attestation":
        attestation["attempt"] -= 1
    elif fault == "native-job-conclusion":
        next(job for job in state["native_jobs"] if job["name"] == "TSan")[
            "conclusion"
        ] = "failure"
    else:
        network["registered"].append("unrelated-network-row")
        network["executed"].append("unrelated-network-row")
    report = receipts.receipt(None)
    with pytest.raises(ValueError) as rejected:
        reporting.publication(REPO, 200, report)
    assert str(rejected.value) == reason
    assert len(state["fresh_gates"]) == int(gate_completes)
    if gate_completes:
        assert state["fresh_gates"][0] == state["main_validations"][-1]
        assert state["fresh_gates"][0] != state["saved_gate"]
    assert state["local_validations"] == state["candidate_boundaries"] == []
    assert len(state["downloads"]) == 1 and state["publication_writes"] == []
    assert report["state"] == "deploying"


@pytest.fixture
def retained_receipt_transport(tmp_path, monkeypatch):
    report = contract.object_json((FIXTURES / "sample-release-receipt.json").read_bytes())
    pr = {
        "number": report["pr"]["number"], "merged": True, "merge_commit_sha": MERGE,
        "base": {"ref": "main", "sha": BASE},
        "head": {"sha": HEAD, "repo": {"full_name": REPO}},
        "body": automation.body({
            "kind": "release", "version": "0.3.1", "base": BASE,
            "generated_head": HEAD, "event_id": report["event_id"],
        }),
    }
    state = {
        "report": report, "pr": pr, "saved": True,
        "reads": [], "asset_reads": [], "uploads": [], "upload_files": [],
        "temporary_directories": [],
    }

    def api(repo, endpoint, method="GET", data=None):
        state["reads"].append(endpoint)
        assert repo == REPO and method == "GET" and data is None
        if endpoint == "pulls/7":
            return copy.deepcopy(pr)
        assert endpoint == "releases?per_page=100&page=1"
        return [{
            "tag_name": "v0.3.1",
            "assets": [{"id": 98, "name": "release-receipt.json"}] if state["saved"] else [],
        }]

    def gh_bytes(endpoint, *, accept):
        state["asset_reads"].append(endpoint)
        assert endpoint == f"repos/{REPO}/releases/assets/98"
        assert accept == "application/octet-stream"
        return receipts.canonical(report)

    def upload(command, *, check):
        path = Path(command[4])
        if (
            path.name.endswith(".env")
            or not path.resolve().is_relative_to(tmp_path.resolve())
        ):
            raise AssertionError(f"refused retained receipt fixture file: {path}")
        assert check is True
        assert command == [
            "gh", "release", "upload", "v0.3.1", str(path), "--repo", REPO,
        ]
        state["upload_files"].append([path.name])
        state["uploads"].append(path.read_bytes())

    real_temporary_directory = reporting.tempfile.TemporaryDirectory

    def temporary_directory():
        state["temporary_directories"].append(tmp_path)
        return real_temporary_directory(dir=tmp_path)

    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(reporting, "api", api)
    monkeypatch.setattr(publishing, "gh_bytes", gh_bytes)
    monkeypatch.setattr(reporting.subprocess, "run", upload)
    monkeypatch.setattr(reporting.tempfile, "TemporaryDirectory", temporary_directory)
    return state


def test_retain_refuses_missing_release_marker(retained_receipt_transport):
    state = retained_receipt_transport
    state["pr"]["body"] = None
    report = state["report"]
    before = copy.deepcopy(report)
    assert automation.marker(state["pr"]) is None
    with pytest.raises(ValueError, match=r"^retained release marker missing$"):
        reporting.retain(REPO, report)
    assert state["reads"] == ["pulls/7"]
    assert state["asset_reads"] == state["uploads"] == state["upload_files"] == []
    assert state["temporary_directories"] == []
    assert report == before


@pytest.mark.parametrize("saved", [True, False])
def test_retain_accepts_release_marker_and_preserves_receipt(retained_receipt_transport, saved):
    state = retained_receipt_transport
    state["saved"] = saved
    report = state["report"]
    before = copy.deepcopy(report)
    reporting.retain(REPO, report)
    assert state["reads"] == ["pulls/7", "releases?per_page=100&page=1"]
    assert state["asset_reads"] == ([f"repos/{REPO}/releases/assets/98"] if saved else [])
    assert state["upload_files"] == ([] if saved else [["release-receipt.json"]])
    assert state["uploads"] == ([] if saved else [receipts.canonical(report)])
    assert len(state["temporary_directories"]) == int(not saved)
    assert report == before


@pytest.mark.parametrize("status", ["pending", "testing", "merged", "deploying", "failed"])
def test_retain_other_states_do_not_require_a_marker(retained_receipt_transport, status):
    state = retained_receipt_transport
    state["pr"]["body"] = None
    state["report"]["state"] = status
    before = copy.deepcopy(state["report"])
    reporting.retain(REPO, state["report"])
    assert state["reads"] == state["asset_reads"] == state["uploads"] == state["upload_files"] == []
    assert state["temporary_directories"] == []
    assert state["report"] == before


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
    # old: assert report_if == "always() && needs.reconcile.result != 'skipped'"
    # The report job (a reusable-workflow call, secrets: inherit) must not run when authz failed.
    assert report_if == (
        "!cancelled() && needs.authz.result == 'success' && needs.reconcile.result != 'skipped'"
    )
