"""Keep pull request code hosted and run the complete landed proofs on main."""

from __future__ import annotations

import copy
import itertools
import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from release_chain import evidence

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
PAIRS = ("ci", "native-feed")
ROUTES = {
    "workflow-lint": "gke-2cpu-4gb-amd64",
    "test": "gke-4cpu-8gb-amd64",
    "docker": "ubuntu-24.04",
    "database-e2e": "ubuntu-24.04",
    "contract": "gke-2cpu-4gb-amd64",
    "distribution": "ubuntu-24.04",
    "docs-tests": "gke-2cpu-4gb-amd64",
    "ready": "gke-2cpu-4gb-amd64",
}
NATIVE_ROUTES = {
    "release-amd64": ("Release (ubuntu-24.04)", "gke-4cpu-8gb-amd64"),
    "release-arm64": ("Release (ubuntu-24.04-arm)", "gke-4cpu-8gb-arm64"),
    "release-macos": ("Release (macos-14)", "macos-14"),
    "asan-ubsan": ("ASan + UBSan", "gke-4cpu-8gb-amd64"),
    "tsan": ("TSan", "gke-4cpu-8gb-amd64"),
}
PINS = {
    "actions/checkout@v7": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python@v6": "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact@v5": (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    ),
}


def load(stem: str, *, post: bool = False) -> dict[str, Any]:
    path = WORKFLOWS / f"{stem}{'-post' if post else ''}.yml"
    # On the unsplit revision inspect the actual mixed workflow, so regressions
    # fail on its event/routing behavior rather than on a missing successor file.
    if not path.exists():
        path = WORKFLOWS / f"{stem}.yml"
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    return {value} if isinstance(value, str) else set(value)


def assert_post_events(workflow: dict[str, Any]) -> None:
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["concurrency"]["cancel-in-progress"] == "false"


@pytest.mark.parametrize("stem", PAIRS)
def test_pr_workflows_have_only_the_pull_request_trigger(stem: str) -> None:
    assert set(load(stem)["on"]) == {"pull_request"}


@pytest.mark.parametrize("stem", PAIRS)
def test_post_workflows_have_only_main_push_and_dispatch(stem: str) -> None:
    assert_post_events(load(stem, post=True))


@pytest.mark.parametrize("stem", PAIRS)
@pytest.mark.parametrize("event", ["pull_request", "pull_request_target", "pull_request_review"])
def test_post_event_control_rejects_every_pr_event(stem: str, event: str) -> None:
    original = load(stem, post=True)
    assert_post_events(original)
    changed = copy.deepcopy(original)
    changed["on"][event] = ""
    with pytest.raises(AssertionError):
        assert_post_events(changed)


def test_native_paths_cover_both_workflow_definitions() -> None:
    paths = [
        "native/feed/**",
        ".github/workflows/native-feed.yml",
        ".github/workflows/native-feed-post.yml",
    ]
    assert load("native-feed")["on"]["pull_request"]["paths"] == paths
    assert load("native-feed", post=True)["on"]["push"]["paths"] == paths


def assert_post_ci(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    assert "changes" not in jobs
    assert set(jobs) == {"authz", "main-ref", "check", *ROUTES}
    for job_id, route in ROUTES.items():
        job = jobs[job_id]
        assert job["runs-on"] == route, job_id
        assert "if" not in job, job_id
        expected = {"authz", "main-ref"}
        if job_id == "ready":
            expected |= evidence.NEEDS
        assert needs(job) == expected, job_id
    check = jobs["check"]
    assert check["if"] == "${{ always() }}"
    assert needs(check) == set(jobs) - {"check"}
    assert len(check["needs"]) == len(needs(check))
    assert check["runs-on"] == "ubuntu-24.04"
    assert len(check["steps"]) == 2
    refusal, gate = check["steps"]
    assert refusal["if"] == "${{ needs.authz.result != 'success' }}"
    assert "exit 1" in refusal["run"]
    assert gate["env"] == {"RESULTS": "${{ toJSON(needs.*.result) }}"}
    assert "changes" not in json.dumps(check)
    assert jobs["test"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]
    assert workflow["name"] == load("ci")["name"] == "CI"
    for job_id in ROUTES:
        assert jobs[job_id].get("name", job_id) == load("ci")["jobs"][job_id].get("name", job_id)
        if job_id == "workflow-lint":
            expected = copy.deepcopy(load("ci")["jobs"][job_id]["steps"])
            expected[0]["uses"] = PINS[expected[0]["uses"]]
            expected[1]["run"] = expected[1]["run"].replace(
                " --speed-limit 1024 --speed-time 60", ""
            )
            assert jobs[job_id]["steps"] == expected
            continue
        expected = copy.deepcopy(load("ci")["jobs"][job_id]["steps"])
        for step in expected:
            if step.get("uses") in PINS:
                step["uses"] = PINS[step["uses"]]
        if job_id in {"docker", "distribution"}:
            before = next(
                index for index, step in enumerate(expected)
                if "pytest" in step.get("run", "") or step.get("run") == "python -m build"
            )
            expected.insert(before, {
                "name": "Verify the Docker sidecar is available",
                "if": "needs.authz.result == 'success'",
                "run": "docker info\n",
            })
        if job_id == "ready":
            expected[2] = {
                "name": "Download this attempt's tested distributions",
                "if": "needs.authz.result == 'success'",
                "env": {
                    "GH_TOKEN": "${{ github.token }}",
                    "GH_REPO": "${{ github.repository }}",
                    "RUN_ID": "${{ github.run_id }}",
                    "ATTEMPT": "${{ github.run_attempt }}",
                },
                "run": (
                    'gh run download "$RUN_ID" --name '
                    '"python-package-distributions-$ATTEMPT" --dir dist'
                ),
            }
        assert jobs[job_id]["steps"] == expected, job_id


def test_post_ci_has_every_proof_without_pr_classification() -> None:
    assert_post_ci(load("ci", post=True))


@pytest.mark.parametrize("fault", ["classifier", "skip", "dependency", "gate", "runner"])
def test_post_ci_control_rejects_reverting_a_proof(fault: str) -> None:
    original = load("ci", post=True)
    assert_post_ci(original)
    changed = copy.deepcopy(original)
    jobs = changed["jobs"]
    if fault == "classifier":
        jobs["changes"] = copy.deepcopy(load("ci")["jobs"]["changes"])
    elif fault == "skip":
        jobs["test"]["if"] = "needs.changes.outputs.code == 'true'"
    elif fault == "dependency":
        jobs["check"]["needs"].remove("ready")
    elif fault == "gate":
        jobs["check"]["if"] = "${{ !cancelled() }}"
    else:
        jobs["ready"]["runs-on"] = "ubuntu-24.04"
    with pytest.raises(AssertionError):
        assert_post_ci(changed)


def assert_native_post(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    assert set(jobs) == {"authz", "main-ref", *NATIVE_ROUTES}
    for job_id, (name, route) in NATIVE_ROUTES.items():
        job = jobs[job_id]
        assert job["name"] == name
        assert job["runs-on"] == route
        assert needs(job) == {"authz", "main-ref"}
        assert "if" not in job
    # All three existing platform check names survive the scalar-runner split.
    original = load("native-feed")["jobs"]["release"]
    assert {f"Release ({os_name})" for os_name in original["strategy"]["matrix"]["os"]} == {
        name for name, _ in NATIVE_ROUTES.values() if name.startswith("Release (")
    }
    for job_id in ("release-amd64", "release-arm64", "release-macos"):
        expected = copy.deepcopy(original["steps"])
        expected[0]["uses"] = PINS[expected[0]["uses"]]
        expected[1]["run"] += " libdigest-sha-perl"
        assert jobs[job_id]["steps"] == expected
    for job_id in ("asan-ubsan", "tsan"):
        expected = copy.deepcopy(load("native-feed")["jobs"][job_id]["steps"][1:])
        expected[0]["run"] += " libdigest-sha-perl"
        assert jobs[job_id]["steps"][1:] == expected


def test_native_post_keeps_linux_proofs_and_the_hosted_macos_leg() -> None:
    assert_native_post(load("native-feed", post=True))


def test_native_pods_install_the_curl_scripts_checksum_tool() -> None:
    jobs = load("native-feed", post=True)["jobs"]
    for job_id, job in jobs.items():
        if job_id in {"authz", "main-ref", "release-macos"}:
            continue
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        assert "libdigest-sha-perl" in commands
        assert commands.index("libdigest-sha-perl") < commands.index("build_ci_curl.sh")


@pytest.mark.parametrize("job_id", NATIVE_ROUTES)
def test_native_control_rejects_a_missing_or_rerouted_leg(job_id: str) -> None:
    original = load("native-feed", post=True)
    assert_native_post(original)
    for fault in ("missing", "runner"):
        changed = copy.deepcopy(original)
        if fault == "missing":
            del changed["jobs"][job_id]
        else:
            changed["jobs"][job_id]["runs-on"] = "ubuntu-latest"
        with pytest.raises(AssertionError):
            assert_native_post(changed)


@pytest.mark.parametrize("stem", PAIRS)
def test_post_ref_is_refused_on_hosted_before_a_pod_is_requested(stem: str) -> None:
    jobs = load(stem, post=True)["jobs"]
    assert "main-ref" in jobs
    guard = jobs["main-ref"]
    assert guard["runs-on"] == "ubuntu-24.04"
    assert needs(guard) == {"authz"}
    assert "if" not in guard
    assert len(guard["steps"]) == 1
    step = guard["steps"][0]
    assert step["env"] == {"REF": "${{ github.ref }}"}
    assert step["run"] == 'set -eu\n[ "$REF" = refs/heads/main ]\n'
    for job_id, job in jobs.items():
        if job_id not in {"authz", "main-ref"}:
            assert "main-ref" in needs(job)


@pytest.mark.parametrize("stem", PAIRS)
def test_post_authz_stays_canonical_and_pods_hold_no_credentials(stem: str) -> None:
    workflow = load(stem, post=True)
    assert workflow["jobs"]["authz"] == load(stem)["jobs"]["authz"]
    assert set(workflow["permissions"].values()) == {"read"}
    for job in workflow["jobs"].values():
        if str(job["runs-on"]).startswith("gke-"):
            assert "environment" not in job
            assert "secrets." not in json.dumps(job)
            assert "write" not in job.get("permissions", {}).values()
            assert "container" not in job
            assert "services" not in job


@pytest.mark.parametrize("stem", PAIRS)
def test_post_workflow_caps_each_label_at_eight_pods(stem: str) -> None:
    counts: Counter[str] = Counter()
    for job in load(stem, post=True)["jobs"].values():
        route = job["runs-on"]
        if not isinstance(route, str) or not route.startswith("gke-"):
            continue
        matrix = job.get("strategy", {}).get("matrix", {})
        axes = [values for key, values in matrix.items() if key not in {"include", "exclude"}]
        legs = len(list(itertools.product(*axes))) if axes else 1
        counts[route] += legs
    assert counts
    assert max(counts.values()) <= 8


@pytest.mark.parametrize("job_id", ["docker", "distribution"])
def test_docker_consumers_check_the_sidecar_before_using_it(job_id: str) -> None:
    job = load("ci", post=True)["jobs"][job_id]
    commands = [step.get("run", "") for step in job["steps"]]
    assert "docker info\n" in commands
    assert commands.index("docker info\n") < next(
        index for index, command in enumerate(commands)
        if "pytest" in command or "python -m build" in command
    )
    assert load("ci", post=True)["jobs"]["database-e2e"]["runs-on"] == "ubuntu-24.04"
    assert load("ci", post=True)["jobs"]["database-e2e"]["services"] == (
        load("ci")["jobs"]["database-e2e"]["services"]
    )


def test_post_workflow_bytes_require_independent_review(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    workflows = root / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_bytes((WORKFLOWS / "ci.yml").read_bytes())
    post = WORKFLOWS / "ci-post.yml"
    raw = post.read_bytes() if post.exists() else (WORKFLOWS / "ci.yml").read_bytes()
    (workflows / "ci-post.yml").write_bytes(raw + b"\n# unreviewed workflow edit\n")
    with pytest.raises(ValueError, match=r"ci-post\.yml changed"):
        evidence.require_no_continue_on_error(root)


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", "unknown"])
def test_post_gate_reads_every_real_dependency_result(tmp_path: Path, result: str) -> None:
    gate = load("ci", post=True)["jobs"]["check"]
    assert len(gate["steps"]) == 2
    script = gate["steps"][-1]["run"]
    for dependency in gate["needs"]:
        results = dict.fromkeys(gate["needs"], "success")
        results[dependency] = result
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env={**os.environ, "RESULTS": json.dumps(list(results.values()))},
            capture_output=True,
            text=True,
            check=False,
        )
        assert (completed.returncode == 0) is (result == "success"), dependency
