"""Keep pull request code hosted and run the complete landed proofs on main."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
import yaml
from release_chain import evidence

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
PAIRS = ("ci", "native-feed")
NETWORK_NODE = (
    "tests/test_consumer_receipt.py::test_bootstrap_recorded_hashes_match_live_pypi"
)
ROUTES = {
    "workflow-lint": "ubuntu-24.04",
    "test": "ubuntu-24.04",
    "docker": "ubuntu-24.04",
    "database-e2e": "ubuntu-24.04",
    "contract": "ubuntu-24.04",
    "distribution": "ubuntu-24.04",
    "docs-tests": "ubuntu-24.04",
    "ready": "ubuntu-24.04",
}
NATIVE_ROUTES = {
    "release-amd64": ("Release (ubuntu-24.04)", "ubuntu-24.04"),
    "release-arm64": ("Release (ubuntu-24.04-arm)", "ubuntu-24.04-arm"),
    "release-macos": ("Release (macos-15)", "macos-15"),
    "asan-ubsan": ("ASan + UBSan", "ubuntu-24.04"),
    "tsan": ("TSan", "ubuntu-24.04"),
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


def workflow_uses(value: Any, location: str = "$") -> list[tuple[str, Any]]:
    """Observe step and reusable-job references through every mapping/list."""
    references = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{location}.{key}"
            if key == "uses":
                references.append((path, child))
            references.extend(workflow_uses(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            references.extend(workflow_uses(child, f"{location}[{index}]"))
    return references


def assert_action_pins(workflow: dict[str, Any]) -> None:
    references = workflow_uses(workflow)
    assert references, "post workflow has no uses references"
    offending = [
        (path, reference) for path, reference in references
        if not isinstance(reference, str)
        or re.fullmatch(r"[^@\s]+@[0-9a-fA-F]{40}", reference) is None
    ]
    assert not offending, (
        "uses references require an @ suffix of exactly 40 hexadecimal characters: "
        + "; ".join(f"{path}={reference!r}" for path, reference in offending)
    )


@pytest.mark.parametrize("stem", PAIRS)
def test_post_proof_workflows_pin_every_actual_uses_reference(stem: str) -> None:
    path = WORKFLOWS / f"{stem}-post.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert_action_pins(workflow)


@pytest.fixture(params=PAIRS)
def pinned_uses_control(request) -> dict[str, Any]:
    path = WORKFLOWS / f"{request.param}-post.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    observed = workflow_uses(workflow)
    assert observed, f"{path.name}: missing actual uses inventory for the control"
    pinned = [
        reference for _path, reference in observed
        if isinstance(reference, str)
        and re.fullmatch(r"[^@\s]+@[0-9a-fA-F]{40}", reference)
    ]
    assert pinned, f"{path.name}: no existing pinned reference for the positive control"
    # Fixture-only mappings exercise traversal; they are not runnable workflows.
    return {
        "jobs": {
            "reusable-control": {"uses": pinned[0]},
            "step-control": {"steps": [{"uses": reference} for reference in pinned]},
        },
        "nested-control": [{"mapping": {"uses": pinned[-1]}}],
    }


def test_action_pin_control_accepts_references_from_the_actual_files(pinned_uses_control) -> None:
    assert_action_pins(pinned_uses_control)


@pytest.mark.parametrize("location", ["step", "reusable-job", "nested"])
@pytest.mark.parametrize("fault", ["floating-tag", "short-sha", "long-sha", "nonhex-sha"])
def test_action_pin_control_refuses_a_changed_reference(
    pinned_uses_control, location: str, fault: str
) -> None:
    assert_action_pins(pinned_uses_control)
    changed = copy.deepcopy(pinned_uses_control)
    owners = {
        "step": ("$.jobs.step-control.steps[0].uses",
                 changed["jobs"]["step-control"]["steps"][0]),
        "reusable-job": ("$.jobs.reusable-control.uses",
                         changed["jobs"]["reusable-control"]),
        "nested": ("$.nested-control[0].mapping.uses",
                   changed["nested-control"][0]["mapping"]),
    }
    path, owner = owners[location]
    action, revision = owner["uses"].rsplit("@", 1)
    suffix = {
        "floating-tag": "v6", "short-sha": revision[:7],
        "long-sha": revision + "0", "nonhex-sha": "g" + revision[1:],
    }[fault]
    owner["uses"] = offending = f"{action}@{suffix}"
    with pytest.raises(AssertionError) as rejected:
        assert_action_pins(changed)
    message = str(rejected.value)
    assert "uses references require an @ suffix of exactly 40 hexadecimal characters" in message
    assert f"{path}={offending!r}" in message


def test_action_pin_control_refuses_an_empty_reference_inventory() -> None:
    with pytest.raises(AssertionError) as rejected:
        assert_action_pins({"jobs": {"run-only": {"steps": [{"run": "true"}]}}})
    assert str(rejected.value).splitlines()[0] == "post workflow has no uses references"


def native_ctest_step(workflow: dict[str, Any], job_id: str) -> dict[str, Any]:
    steps = [
        step for step in workflow["jobs"][job_id]["steps"]
        if re.search(r"(?m)^[ \t]*ctest(?:[ \t]|$)", step.get("run", ""))
    ]
    assert len(steps) == 1, f"{job_id}: expected one actual CTest-producing step"
    return steps[0]


def assert_native_ctest_serial(workflow: dict[str, Any], job_id: str) -> None:
    step = native_ctest_step(workflow, job_id)
    commands = [
        shlex.split(line, comments=True)
        for line in step["run"].replace("\\\n", " ").splitlines()
    ]
    ctest = [command for command in commands if command and command[0] == "ctest"]
    assert len(ctest) == 2, f"{job_id}: require registration and execution commands"
    for command in ctest:
        parallel = [
            token for token in command[1:]
            if token.startswith("-j") or token == "--parallel" or token.startswith("--parallel=")
        ]
        assert not parallel, (
            f"{job_id}: CTest must remain serial; parallel request {parallel!r} "
            f"in {shlex.join(command)!r}"
        )
    assert ctest[0] == [
        "ctest", "--test-dir", "build-feed", "--show-only=json-v1",
        ">", "native-registration.json",
    ], f"{job_id}: registration command changed"
    assert ctest[1] == [
        "ctest", "--test-dir", "build-feed", "--output-on-failure",
        "--output-junit", "$GITHUB_WORKSPACE/native-junit.xml",
    ], f"{job_id}: JUnit execution command changed"
    assert ["cmake", "--build", "build-feed", "-j4"] in commands, (
        f"{job_id}: preserve existing build parallelism"
    )


@pytest.mark.parametrize("job_id", NATIVE_ROUTES)
def test_native_post_ctest_execution_remains_serial(job_id: str) -> None:
    path = WORKFLOWS / "native-feed-post.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert_native_ctest_serial(workflow, job_id)


@pytest.fixture(params=NATIVE_ROUTES)
def serial_ctest_control(request) -> tuple[dict[str, Any], str]:
    path = WORKFLOWS / "native-feed-post.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    job_id = request.param
    step = native_ctest_step(workflow, job_id)
    # Fixture-only serial copy of the actual step, never a workflow-file repair.
    step["run"] = re.sub(
        r"(?m)^([ \t]*ctest[^\n]*?) -j4[ \t]*$", r"\1", step["run"]
    )
    return workflow, job_id


def test_native_ctest_control_accepts_serial_tests_with_parallel_builds(
    serial_ctest_control,
) -> None:
    workflow, job_id = serial_ctest_control
    assert_native_ctest_serial(workflow, job_id)


@pytest.mark.parametrize("command", ["registration", "execution"])
@pytest.mark.parametrize("parallel", ["-j4", "-j 4", "--parallel 4", "--parallel=4"])
def test_native_ctest_control_refuses_inserted_parallelism(
    serial_ctest_control, command: str, parallel: str
) -> None:
    workflow, job_id = serial_ctest_control
    assert_native_ctest_serial(workflow, job_id)
    changed = copy.deepcopy(workflow)
    step = native_ctest_step(changed, job_id)
    lines = step["run"].splitlines(keepends=True)
    targets = [
        index for index, line in enumerate(lines)
        if line.lstrip().startswith("ctest ")
        and ("--show-only=json-v1" in line) == (command == "registration")
    ]
    assert len(targets) == 1
    index = targets[0]
    # Put the option on the CTest invocation, before any registration redirection.
    lines[index] = lines[index].replace("ctest ", f"ctest {parallel} ", 1)
    step["run"] = "".join(lines)
    with pytest.raises(AssertionError) as rejected:
        assert_native_ctest_serial(changed, job_id)
    message = str(rejected.value)
    assert f"{job_id}: CTest must remain serial; parallel request" in message
    assert parallel in message


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


def test_native_pr_paths_cover_both_workflow_definitions() -> None:
    paths = [
        "native/feed/**",
        ".github/workflows/native-feed.yml",
        ".github/workflows/native-feed-post.yml",
    ]
    assert load("native-feed")["on"]["pull_request"]["paths"] == paths


@pytest.mark.parametrize(
    "changed_path",
    [
        "scripts/release_chain/evidence.py",
        "tests/test_consumer_receipt.py",
        ".github/workflows/ci-post.yml",
    ],
)
def test_main_native_proof_runs_for_changes_outside_native_paths(changed_path: str) -> None:
    workflow = load("native-feed", post=True)
    assert_post_events(workflow)
    assert (ROOT / changed_path).is_file()
    assert not changed_path.startswith("native/feed/")
    push = workflow["on"]["push"]
    paths = push.get("paths")
    assert paths is None or any(fnmatchcase(changed_path, pattern) for pattern in paths), (
        f"the actual main push path list omits a merge changing only {changed_path}"
    )
    assert not any(fnmatchcase(changed_path, pattern) for pattern in push.get("paths-ignore", []))
    assert not {"paths", "paths-ignore"} & set(push), (
        f"a main merge changing only {changed_path} must still schedule all native legs"
    )
    assert_native_post(workflow)


def assert_main_network_job(workflow: dict[str, Any]) -> str:
    jobs = workflow["jobs"]
    selected = [
        (job_id, job, step)
        for job_id, job in jobs.items()
        for step in job.get("steps", [])
        if NETWORK_NODE in step.get("run", "")
    ]
    assert len(selected) == 1, "main CI is missing its single dedicated live-PyPI row"
    job_id, job, step = selected[0]
    assert job_id != "test" and "matrix" not in job.get("strategy", {})
    assert "if" not in job and "continue-on-error" not in job
    assert needs(job) == {"authz", "main-ref"}
    assert step.get("if") in {
        None, "needs.authz.result == 'success'", "${{ needs.authz.result == 'success' }}"
    }
    assert "continue-on-error" not in step
    tokens = shlex.split(step["run"])
    assert NETWORK_NODE in tokens
    assert "pytest" in tokens
    assert [token for token in tokens if token.startswith("tests/")] == [NETWORK_NODE]
    assert not {"--collect-only", "--co"} & set(tokens)
    enabled = {
        **workflow.get("env", {}), **job.get("env", {}), **step.get("env", {})
    }.get("PINEFORGE_NETWORK_TEST")
    assert enabled == "1" or "PINEFORGE_NETWORK_TEST=1" in tokens, (
        "the required row is skipped unless its real opt-in is enabled"
    )
    assert any(token.startswith("--junitxml=") for token in tokens) or "--junitxml" in tokens
    assert job_id in needs(jobs["ready"])
    assert job_id in needs(jobs["check"])
    return job_id


def test_main_ci_explicitly_executes_the_required_live_pypi_row() -> None:
    assert_main_network_job(load("ci", post=True))


def test_pr_ci_keeps_the_live_pypi_proof_in_the_main_tier() -> None:
    workflow = load("ci")
    assert workflow.get("env", {}).get("PINEFORGE_NETWORK_TEST") != "1"
    for job in workflow["jobs"].values():
        assert job.get("env", {}).get("PINEFORGE_NETWORK_TEST") != "1"
        for step in job.get("steps", []):
            assert step.get("env", {}).get("PINEFORGE_NETWORK_TEST") != "1"
            command = step.get("run", "")
            assert NETWORK_NODE not in command
            assert "PINEFORGE_NETWORK_TEST=1" not in command


def assert_native_aggregate(jobs: dict[str, Any]) -> str:
    aggregates = [
        (job_id, job) for job_id, job in jobs.items() if job.get("name") == "Native proof"
    ]
    assert len(aggregates) == 1, "native legs alone do not produce immutable native evidence"
    job_id, aggregate = aggregates[0]
    assert needs(aggregate) == {"authz", "main-ref", *NATIVE_ROUTES}
    assert "continue-on-error" not in aggregate
    return job_id


def test_main_native_workflow_requires_the_producing_aggregate() -> None:
    assert_native_aggregate(load("native-feed", post=True)["jobs"])


def assert_post_ci(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    network_id = assert_main_network_job(workflow)
    assert "changes" not in jobs
    assert set(jobs) == {"authz", "main-ref", "check", network_id, *ROUTES}
    for job_id, route in ROUTES.items():
        job = jobs[job_id]
        assert job["runs-on"] == route, job_id
        assert "if" not in job, job_id
        expected = {"authz", "main-ref"}
        if job_id == "ready":
            expected |= evidence.NEEDS | {network_id}
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
            expected.insert(3, {
                "name": "Download this attempt's actual live PyPI report",
                "if": "needs.authz.result == 'success'",
                "env": {
                    "GH_TOKEN": "${{ github.token }}",
                    "GH_REPO": "${{ github.repository }}",
                    "RUN_ID": "${{ github.run_id }}",
                    "ATTEMPT": "${{ github.run_attempt }}",
                },
                "run": (
                    'gh run download "$RUN_ID" --name '
                    '"network-proof-$ATTEMPT" --dir reports/network'
                ),
            })
            observed = copy.deepcopy(jobs[job_id]["steps"])
            aggregate_steps = [
                step for step in observed if "NEEDS_JSON" in step.get("env", {})
            ]
            assert len(aggregate_steps) == 1
            encoded_needs = aggregate_steps[0]["env"].pop("NEEDS_JSON")
            # Check every dependency-to-result binding, including the new main-only job.
            # Keep all other existing step bytes under the original comparison.
            expanded = re.sub(
                r"\$\{\{\s*toJSON\(needs\.([A-Za-z0-9_-]+)\)\s*\}\}",
                r'"\1"',
                encoded_needs,
            )
            pairs = json.loads(expanded, object_pairs_hook=list)
            required = evidence.NEEDS | {network_id}
            assert len(pairs) == len(required)
            assert dict(pairs) == {name: name for name in required}
            expected_aggregate = next(
                step for step in expected if "NEEDS_JSON" in step.get("env", {})
            )
            expected_aggregate["env"].pop("NEEDS_JSON")
            expected_aggregate["run"] += " --network-proof reports/network"
        else:
            observed = jobs[job_id]["steps"]
        assert observed == expected, job_id


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
        # A hosted runner is the canonical route; the mutation must use a retired label.
        jobs["ready"]["runs-on"] = "ubuntu-latest"
    with pytest.raises(AssertionError):
        assert_post_ci(changed)


def with_native_reports(steps: list[dict[str, Any]], leg: str) -> list[dict[str, Any]]:
    """Only augment the original commands with registration/JUnit production."""
    test_command = "ctest --test-dir build-feed --output-on-failure\n"
    producing = [step for step in steps if test_command in step.get("run", "")]
    assert len(producing) == 1
    producing[0]["run"] = "set -euo pipefail\n" + producing[0]["run"].replace(
        test_command,
        "ctest --test-dir build-feed --show-only=json-v1 > native-registration.json\n"
        'ctest --test-dir build-feed --output-on-failure --output-junit '
        '"$GITHUB_WORKSPACE/native-junit.xml"\n',
    )
    return [
        *steps,
        {
            "uses": PINS["actions/setup-python@v6"],
            "with": {"python-version": "3.13"},
        },
        {
            "name": "Bind the actual registration and JUnit to this leg",
            "env": {"PYTHONPATH": "scripts"},
            "run": (
                f"python -m release_chain.main_proofs native-leg --leg {leg} "
                "--directory . --output native-leg.json"
            ),
        },
        {
            "uses": PINS["actions/upload-artifact@v5"],
            "if": "always()",
            "with": {
                "name": f"native-leg-{leg}-${{{{ github.run_attempt }}}}",
                "path": "native-registration.json\nnative-junit.xml\nnative-leg.json\n",
                "if-no-files-found": "error",
            },
        },
    ]


def assert_native_post(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    aggregate_id = assert_native_aggregate(jobs)
    assert set(jobs) == {"authz", "main-ref", aggregate_id, *NATIVE_ROUTES}
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
        assert jobs[job_id]["steps"] == with_native_reports(expected, job_id)
    for job_id in ("asan-ubsan", "tsan"):
        expected = copy.deepcopy(load("native-feed")["jobs"][job_id]["steps"][1:])
        expected[0]["run"] += " libdigest-sha-perl"
        assert jobs[job_id]["steps"][1:] == with_native_reports(expected, job_id)


def test_native_post_keeps_linux_proofs_and_the_hosted_macos_leg() -> None:
    assert_native_post(load("native-feed", post=True))


def test_native_pods_install_the_curl_scripts_checksum_tool() -> None:
    jobs = load("native-feed", post=True)["jobs"]
    for job_id in NATIVE_ROUTES:
        if job_id == "release-macos":
            continue
        job = jobs[job_id]
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
    hosted = {"ubuntu-24.04", "ubuntu-24.04-arm", "macos-15"}
    for job in load(stem, post=True)["jobs"].values():
        route = job["runs-on"]
        if not isinstance(route, str):
            continue
        assert route in hosted, route


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
