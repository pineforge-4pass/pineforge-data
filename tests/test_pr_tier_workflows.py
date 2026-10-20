"""Pull-request tier of CI: pool runners, authorization, cancellation and the docs-only path."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from release_chain import evidence

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
PR_TIER_WORKFLOWS = ("ci.yml", "docs.yml", "native-feed.yml")
RUNTIME_JOBS = ("docker", "database-e2e", "contract", "distribution")
# ci-labels.md #830: the aggregate job runs unless the run was cancelled (never `always()`).
AGGREGATE_IF = "${{ !cancelled() }}"

HOSTED_GUARD = (
    "${{ github.event_name != 'pull_request' && "
    "github.event_name != 'pull_request_target' &&"
)
HOSTED_FALLBACK = "|| 'ubuntu-24.04' }}"
POOL = "fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-x64\"]')"
POOL_LABELS = ["self-hosted", "pf-ci", "pf-ci-x64"]
MATRIX_RUNNERS = {
    "ubuntu-24.04": "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-x64\"]') }}",
    "ubuntu-24.04-arm": "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-arm64\"]') }}",
}
# These expressions are rejection fixtures for the superseded hosted-fallback policy.
OLD_ROUTE = (
    "${{ github.event_name != 'pull_request_target' && github.actor != 'dependabot[bot]' && "
    "(github.event_name != 'pull_request' || "
    "(github.event.pull_request.head.repo.full_name == github.repository && "
    "github.event.pull_request.head.repo.fork == false)) && "
    f"{POOL} || 'ubuntu-24.04' }}}}"
)
GUARDED_ROUTE = f"{HOSTED_GUARD} {POOL} {HOSTED_FALLBACK}"

CANCEL_ONLY_PRS = "${{ github.event_name == 'pull_request' }}"
PR_OR_RUN_KEY = (
    "${{ github.event_name == 'pull_request' && "
    "github.event.pull_request.number || github.run_id }}"
)

OLD_RUNTIME_IF = (
    "!cancelled() && (github.event_name != 'pull_request' || "
    "(needs.changes.result == 'success' && needs.changes.outputs.code == 'true'))"
)
# old: RUNTIME_IF = (
#     "!cancelled() && (github.event_name != 'pull_request' || "
#     "(needs.changes.result == 'success' && needs.changes.outputs.code == 'true'))"
# )
RUNTIME_IF = (
    "!cancelled() && needs.authz.result == 'success' && "
    "(github.event_name != 'pull_request' || "
    "(needs.changes.result == 'success' && needs.changes.outputs.code == 'true'))"
)
CANONICAL_AUTHZ_SCRIPT = (
    'case "$EVENT" in\n'
    '  pull_request_target) echo "::error::pull_request_target is not allowed for CI"; '
    "exit 1 ;;\n"
    '  pull_request) [ "$HEAD_REPO" = "$REPO" ] || { '
    'echo "::error::fork pull requests do not run CI"; exit 1; } ;;\n'
    "esac\n"
    'echo "authz ok: $EVENT"\n'
)
READY_AUTHZ_CHECK = (
    'if [ "$AUTHZ_RESULT" != "success" ]; then echo "authz: $AUTHZ_RESULT"; exit 1; fi'
)


def load(name: str) -> dict[str, Any]:
    loaded = yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def step_script(workflow: str, job: str, step_id: str) -> str:
    steps = load(workflow)["jobs"][job]["steps"]
    return str(next(step["run"] for step in steps if step.get("id") == step_id))


def assert_native_release_matrix(matrix: dict[str, Any]) -> None:
    assert matrix["os"] == ["ubuntu-24.04", "ubuntu-24.04-arm", "macos-14"]
    includes = matrix["include"]
    by_os = {entry["os"]: entry for entry in includes}
    assert len(by_os) == len(includes), "matrix includes must not override the same OS twice"
    assert set(by_os) <= set(matrix["os"])
    for os_name in matrix["os"]:
        entry = by_os.get(os_name, {})
        if os_name == "macos-14":
            assert "runner" not in entry
        else:
            assert entry.get("runner") == MATRIX_RUNNERS[os_name]


def assert_runner_policy(workflow: dict[str, Any], name: str = "ci.yml") -> None:
    for job_id, job in workflow["jobs"].items():
        if name == "docs.yml" and job_id == "deploy":
            assert job["runs-on"] == "ubuntu-latest"
        elif name == "native-feed.yml" and job_id == "release":
            assert job["runs-on"] == "${{ matrix.runner || matrix.os }}"
            assert_native_release_matrix(job["strategy"]["matrix"])
        else:
            assert job["runs-on"] == POOL_LABELS


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for pair in value.items() for item in pair for text in strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    return []


def assert_secret_references(value: Any, allowed: set[str]) -> None:
    for text in strings(value):
        assert re.search(r"\bsecrets\s*\[", text) is None
        references = re.findall(r"\bsecrets\s*\.\s*[A-Za-z0-9_-]*", text)
        assert set(references) <= allowed


def assert_permission_policy(workflow: dict[str, Any], name: str) -> None:
    assert set(workflow["permissions"].values()) == {"read"}
    # old: allowed = {"secrets.GITHUB_TOKEN"} if job_id == "authz" else set()
    assert_secret_references(workflow, set())
    for job_id, job in workflow["jobs"].items():
        if name == "docs.yml" and job_id == "deploy":
            assert job["permissions"] == {"pages": "write", "id-token": "write"}
            assert job["if"] == "github.event_name != 'pull_request'"
            assert job["environment"]["name"] == "github-pages"
        else:
            # old (for authz): assert job.get("permissions") == {"contents": "read"}
            assert "permissions" not in job


def needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    assert isinstance(value, (str, list))
    return {value} if isinstance(value, str) else set(value)


def assert_authz_dependencies(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    assert next(iter(jobs)) == "authz"
    assert "needs" not in jobs["authz"]
    for job_id, job in jobs.items():
        if job_id != "authz":
            assert "authz" in needs(job)


def assert_canonical_authz(job: dict[str, Any]) -> None:
    assert set(job) == {"runs-on", "steps"}
    assert job["runs-on"] == ["self-hosted", "pf-ci", "pf-ci-x64"]
    assert_secret_references(job, set())
    steps = job["steps"]
    assert isinstance(steps, list)
    assert len(steps) == 1, "authz is exactly the canonical same-repository check"
    step = steps[0]
    assert set(step) == {"name", "env", "run"}
    assert step["name"] == "Same-repository pull requests only"
    assert step["env"] == {
        "EVENT": "${{ github.event_name }}",
        "HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
        "REPO": "${{ github.repository }}",
    }
    script = step["run"]
    assert isinstance(script, str)
    assert " ".join(script.split()) == " ".join(CANONICAL_AUTHZ_SCRIPT.split())


def assert_ready_authz(job: dict[str, Any]) -> None:
    gate = next(step for step in job["steps"] if step.get("id") == "gate")
    assert gate["env"].get("AUTHZ_RESULT") == "${{ needs.authz.result }}"
    assert gate["run"].splitlines()[0] == READY_AUTHZ_CHECK


def cancels_only_pull_requests(concurrency: dict[str, str]) -> bool:
    return (
        concurrency["cancel-in-progress"] == CANCEL_ONLY_PRS
        and PR_OR_RUN_KEY in concurrency["group"]
    )


# old: test_pull_requests_never_select_the_pool
@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_pool_jobs_use_plain_labels_without_hosted_fallback(name: str) -> None:
    workflow = load(name)
    # old: assert pool_routes(workflow), "push and dispatch keep the pool"
    assert workflow["jobs"]["authz"]["runs-on"] == POOL_LABELS
    # old: assert unguarded(workflow) == []
    assert_runner_policy(workflow, name)


def test_the_old_same_repo_route_to_the_pool_is_rejected() -> None:
    # old: assert unguarded({"jobs": {"build": {"runs-on": OLD_ROUTE}}}) == ["build#0"]
    for route in (OLD_ROUTE, GUARDED_ROUTE, "ubuntu-24.04", "macos-14", "windows-latest"):
        with pytest.raises(AssertionError):
            assert_runner_policy({"jobs": {"build": {"runs-on": route}}})
    matrix = copy.deepcopy(load("native-feed.yml"))
    assert_runner_policy(matrix, "native-feed.yml")
    matrix["jobs"]["release"]["strategy"]["matrix"]["include"][0]["runner"] = OLD_ROUTE
    # old: assert unguarded(matrix) == ["release#1"]
    with pytest.raises(AssertionError):
        assert_runner_policy(matrix, "native-feed.yml")
    # old: assert unguarded(mixed) == ["b#0"]
    for job_id in ("a", "b"):
        mixed = {"jobs": {"a": {"runs-on": POOL_LABELS}, "b": {"runs-on": POOL_LABELS}}}
        mixed["jobs"][job_id]["runs-on"] = OLD_ROUTE
        with pytest.raises(AssertionError):
            assert_runner_policy(mixed)
    # old: assert unguarded({"jobs": {"a": {"runs-on": GUARDED_ROUTE}}}) == []
    with pytest.raises(AssertionError):
        assert_runner_policy({"jobs": {"a": {"runs-on": GUARDED_ROUTE}}})
    assert_runner_policy({"jobs": {"a": {"runs-on": POOL_LABELS}}})


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize("route", [GUARDED_ROUTE, "ubuntu-24.04", POOL])
def test_each_pool_job_rejects_expression_or_hosted_routes(name: str, route: str) -> None:
    workflow = load(name)
    assert_runner_policy(workflow, name)
    for job_id in workflow["jobs"]:
        if (name, job_id) in {("docs.yml", "deploy"), ("native-feed.yml", "release")}:
            continue
        changed = copy.deepcopy(workflow)
        changed["jobs"][job_id]["runs-on"] = route
        with pytest.raises(AssertionError):
            assert_runner_policy(changed, name)


@pytest.mark.parametrize(
    "name,job_id,route",
    [
        ("docs.yml", "deploy", POOL_LABELS),
        ("docs.yml", "deploy", "ubuntu-24.04"),
        ("native-feed.yml", "release", POOL_LABELS),
        ("native-feed.yml", "release", "ubuntu-24.04"),
    ],
)
def test_runner_exceptions_reject_other_routes(name: str, job_id: str, route: Any) -> None:
    workflow = copy.deepcopy(load(name))
    assert_runner_policy(workflow, name)
    workflow["jobs"][job_id]["runs-on"] = route
    with pytest.raises(AssertionError):
        assert_runner_policy(workflow, name)


def test_native_release_matrix_uses_only_the_two_linux_pools() -> None:
    assert_native_release_matrix(load("native-feed.yml")["jobs"]["release"]["strategy"]["matrix"])


@pytest.mark.parametrize(
    "os_name,runner",
    [
        ("ubuntu-24.04", GUARDED_ROUTE),
        ("ubuntu-24.04", "ubuntu-24.04"),
        ("ubuntu-24.04-arm", "ubuntu-24.04-arm"),
        ("ubuntu-24.04-arm", MATRIX_RUNNERS["ubuntu-24.04"]),
        ("macos-14", MATRIX_RUNNERS["ubuntu-24.04"]),
    ],
)
def test_native_release_matrix_rejects_fallbacks_and_wrong_pools(
    os_name: str, runner: str
) -> None:
    matrix = copy.deepcopy(load("native-feed.yml")["jobs"]["release"]["strategy"]["matrix"])
    assert_native_release_matrix(matrix)
    if os_name == "macos-14":
        matrix["include"].append({"os": os_name, "runner": runner})
    else:
        entry = next(entry for entry in matrix["include"] if entry["os"] == os_name)
        entry["runner"] = runner
    with pytest.raises(AssertionError):
        assert_native_release_matrix(matrix)


@pytest.mark.parametrize(
    "fault",
    ["missing-os", "extra-os", "missing-linux-runner", "duplicate-include", "unknown-include"],
)
def test_native_release_matrix_rejects_missing_or_extra_legs(fault: str) -> None:
    matrix = copy.deepcopy(load("native-feed.yml")["jobs"]["release"]["strategy"]["matrix"])
    assert_native_release_matrix(matrix)
    if fault == "missing-os":
        matrix["os"].remove("macos-14")
    elif fault == "extra-os":
        matrix["os"].append("windows-latest")
    elif fault == "missing-linux-runner":
        del matrix["include"][1]["runner"]
    elif fault == "duplicate-include":
        matrix["include"].append(dict(matrix["include"][0]))
    else:
        matrix["include"].append({"os": "windows-latest", "runner": "windows-latest"})
    with pytest.raises(AssertionError):
        assert_native_release_matrix(matrix)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_only_superseded_pull_request_runs_are_cancelled(name: str) -> None:
    assert cancels_only_pull_requests(load(name)["concurrency"])


def test_unconditional_cancellation_is_rejected() -> None:
    assert cancels_only_pull_requests(
        {"group": f"x-{PR_OR_RUN_KEY}", "cancel-in-progress": CANCEL_ONLY_PRS}
    )
    assert not cancels_only_pull_requests(
        {"group": "pages-${{ github.ref }}", "cancel-in-progress": "true"}
    )
    assert not cancels_only_pull_requests(
        {"group": f"x-{PR_OR_RUN_KEY}", "cancel-in-progress": "true"}
    )
    assert not cancels_only_pull_requests(
        {"group": "x-${{ github.ref }}", "cancel-in-progress": CANCEL_ONLY_PRS}
    )


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_pull_request_jobs_hold_no_secret_and_no_write_scope(name: str) -> None:
    workflow = load(name)
    # old: assert "secrets." not in (WORKFLOWS / name).read_text()
    assert "secrets." not in (WORKFLOWS / name).read_text()
    # old: assert set(workflow["permissions"].values()) == {"read"}
    # old: assert job_id == "deploy"
    # old: assert job["if"] == "github.event_name != 'pull_request'"
    assert_permission_policy(workflow, name)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "job_id,key,value",
    [
        ("authz", "permissions", {"contents": "write"}),
        ("authz", "permissions", {"contents": "read"}),
        ("authz", "permissions", {"contents": "read", "actions": "read"}),
        ("authz", "env", {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}),
        ("authz", "env", {"TOKEN": "${{ secrets.OTHER_TOKEN }}"}),
        ("authz", "env", {"TOKEN": "${{ secrets.GITHUB_TOKEN_EXTRA }}"}),
        ("authz", "env", {"TOKEN": "${{ secrets['GITHUB_TOKEN'] }}"}),
        ("extra", "env", {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}),
        ("extra", "steps", [{"run": "echo '${{ secrets.OTHER_TOKEN }}'"}]),
        ("extra", "permissions", {"contents": "write"}),
        ("deploy", "permissions", {"contents": "write"}),
    ],
)
def test_secret_and_permission_policy_rejects_unauthorized_scope(
    name: str, job_id: str, key: str, value: Any
) -> None:
    original = load(name)
    assert_permission_policy(original, name)
    workflow = copy.deepcopy(original)
    workflow["jobs"].setdefault(job_id, {})[key] = value
    assert workflow != original
    with pytest.raises(AssertionError):
        assert_permission_policy(workflow, name)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize("key", ["env", "permissions"])
def test_workflow_level_secrets_and_write_scope_are_rejected(name: str, key: str) -> None:
    workflow = copy.deepcopy(load(name))
    assert_permission_policy(workflow, name)
    workflow[key] = (
        {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"} if key == "env" else {"contents": "write"}
    )
    with pytest.raises(AssertionError):
        assert_permission_policy(workflow, name)


@pytest.mark.parametrize(
    "key,value",
    [
        ("permissions", {"pages": "write"}),
        ("permissions", {"pages": "write", "id-token": "write", "contents": "write"}),
        ("if", "always()"),
        ("environment", {"name": "other"}),
    ],
)
def test_pages_write_scope_is_limited_to_non_pr_deployment(key: str, value: Any) -> None:
    workflow = copy.deepcopy(load("docs.yml"))
    assert_permission_policy(workflow, "docs.yml")
    workflow["jobs"]["deploy"][key] = value
    with pytest.raises(AssertionError):
        assert_permission_policy(workflow, "docs.yml")


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_every_job_waits_for_the_first_authz_job(name: str) -> None:
    assert_authz_dependencies(load(name))


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_missing_authz_dependencies_and_authz_prerequisites_are_rejected(name: str) -> None:
    workflow = load(name)
    assert_authz_dependencies(workflow)
    for job_id in workflow["jobs"]:
        changed = copy.deepcopy(workflow)
        if job_id == "authz":
            changed["jobs"][job_id]["needs"] = "build"
        else:
            changed["jobs"][job_id]["needs"] = sorted(needs(changed["jobs"][job_id]) - {"authz"})
        with pytest.raises(AssertionError):
            assert_authz_dependencies(changed)
    changed = copy.deepcopy(workflow)
    changed["jobs"]["authz"] = changed["jobs"].pop("authz")
    with pytest.raises(AssertionError):
        assert_authz_dependencies(changed)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_authz_matches_the_canonical_same_repository_job(name: str) -> None:
    assert_canonical_authz(load(name)["jobs"]["authz"])


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "comparison",
    [
        '[ "$HEAD_REPO" != "$REPO" ]',
        '[[ "$HEAD_REPO" =~ "$REPO" ]]',
        '[[ "$HEAD_REPO" == "$REPO"* ]]',
        '[[ "$HEAD_REPO" == * ]]',
    ],
    ids=["not-equal", "regex", "prefix", "glob"],
)
def test_authz_rejects_weakened_repository_comparisons(name: str, comparison: str) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    script = job["steps"][0]["run"]
    exact_comparison = '[ "$HEAD_REPO" = "$REPO" ]'
    assert exact_comparison in script
    job["steps"][0]["run"] = script.replace(exact_comparison, comparison)
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "old,new",
    [
        (
            'pull_request_target) echo "::error::pull_request_target is not allowed for CI"; '
            "exit 1 ;;",
            "",
        ),
        (
            'pull_request_target) echo "::error::pull_request_target is not allowed for CI"; '
            "exit 1 ;;",
            "pull_request_target) exit 0 ;;",
        ),
        ("; exit 1 ;;", " ;;"),
        ("] || {", "] && {"),
        ("exit 1; } ;;", "} ;;"),
        ("exit 1; } ;;", "exit 0; } ;;"),
        ("] || {", "] || true || {"),
    ],
    ids=[
        "missing-target-rejection",
        "target-success",
        "target-exit-dropped",
        "and-instead-of-or",
        "fork-exit-dropped",
        "fork-exit-zero",
        "or-true",
    ],
)
def test_authz_rejects_canonical_script_regressions(name: str, old: str, new: str) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    script = "\n".join(line.strip() for line in job["steps"][0]["run"].splitlines())
    assert old in script
    changed = script.replace(old, new)
    assert changed != script
    job["steps"][0]["run"] = changed
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize("key", ["EVENT", "HEAD_REPO", "REPO"])
def test_authz_rejects_missing_canonical_env(name: str, key: str) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    del job["steps"][0]["env"][key]
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "target,key,value",
    [
        ("job", "name", "Authorize CI"),
        ("job", "if", "false"),
        ("job", "needs", "build"),
        ("job", "permissions", {"contents": "read"}),
        ("job", "permissions", {"contents": "write"}),
        ("job", "timeout-minutes", "5"),
        ("job", "continue-on-error", "true"),
        ("step", "name", "Authorize CI"),
        ("step", "shell", "bash"),
        ("step", "shell", "sh"),
        ("step", "if", "false"),
        ("step", "continue-on-error", "true"),
        ("env", "EVENT", "${{ github.event.action }}"),
        ("env", "HEAD_REPO", "${{ github.head_ref }}"),
        ("env", "HEAD_REPO", "${{ github.event.pull_request.head.repo.fork }}"),
        ("env", "HEAD_REPO", "${{ github.repository }}"),
        ("env", "REPO", "${{ github.event.pull_request.head.repo.full_name }}"),
        ("env", "GH_TOKEN", "${{ secrets.GITHUB_TOKEN }}"),
        ("env", "GH_TOKEN", "unused"),
        ("env", "EXTRA", "unused"),
    ],
)
def test_authz_rejects_untrusted_env_and_masked_failures(
    name: str, target: str, key: str, value: Any
) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    targets = {"job": job, "step": job["steps"][0], "env": job["steps"][0]["env"]}
    targets[target][key] = value
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize("fault", ["missing-step", "second-step"])
def test_authz_rejects_noncanonical_step_counts(name: str, fault: str) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    if fault == "missing-step":
        job["steps"].clear()
    else:
        job["steps"].append({"name": "Extra step", "run": "true"})
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "route", ["ubuntu-24.04", ["ubuntu-24.04"], MATRIX_RUNNERS["ubuntu-24.04"]]
)
def test_authz_rejects_hosted_or_expression_runners(name: str, route: Any) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    job["runs-on"] = route
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


def assert_docs_only_policy(jobs: dict[str, Any]) -> None:
    assert jobs["changes"]["if"] == "github.event_name == 'pull_request'"
    assert jobs["changes"]["needs"] == "authz"
    # The unit matrix always runs: tests/test_documentation.py checks README.md, CONTRIBUTING.md
    # and docs/ links, and its check names stay reported.
    # old: assert "needs" not in jobs["test"]
    assert jobs["test"]["needs"] == "authz"
    assert "if" not in jobs["test"]
    for name in RUNTIME_JOBS:
        # old: assert jobs[name]["needs"] == "changes"
        assert needs(jobs[name]) == {"authz", "changes"}
        # old: assert " ".join(jobs[name]["if"].split()) == RUNTIME_IF
        # old: RUNTIME_IF had "!cancelled() &&" immediately followed by the event condition.
        assert " ".join(jobs[name]["if"].split()) == RUNTIME_IF
    # old: assert set(jobs["ready"]["needs"]) == {"changes", "test", *RUNTIME_JOBS}
    assert set(jobs["ready"]["needs"]) == {"authz", "changes", "test", *RUNTIME_JOBS}
    # old: assert jobs["ready"]["if"] == "always()"
    # ci-labels.md #830: the aggregate uses !cancelled() so a superseded run never blocks the head.
    assert jobs["ready"]["if"] == AGGREGATE_IF
    assert jobs["ready"]["name"] == "release/ready"


def test_docs_only_prs_skip_only_the_runtime_jobs() -> None:
    assert_docs_only_policy(load("ci.yml")["jobs"])


@pytest.mark.parametrize(
    "job_id,key,value",
    [
        ("changes", "needs", []),
        ("test", "needs", []),
        ("test", "if", "needs.changes.outputs.code == 'true'"),
        ("ready", "needs", ["changes", "test", *RUNTIME_JOBS]),
        ("ready", "needs", ["authz", "changes", "test", *RUNTIME_JOBS, "extra"]),
        *[(job_id, "needs", ["changes"]) for job_id in RUNTIME_JOBS],
        *[(job_id, "needs", ["authz", "changes", "extra"]) for job_id in RUNTIME_JOBS],
        *[(job_id, "if", OLD_RUNTIME_IF) for job_id in RUNTIME_JOBS],
    ],
)
def test_docs_only_policy_rejects_missing_authz_and_extra_dependencies(
    job_id: str, key: str, value: Any
) -> None:
    jobs = copy.deepcopy(load("ci.yml")["jobs"])
    assert_docs_only_policy(jobs)
    jobs[job_id][key] = value
    with pytest.raises(AssertionError):
        assert_docs_only_policy(jobs)


def test_evidence_inventory_stays_the_five_gated_jobs() -> None:
    steps = load("ci.yml")["jobs"]["ready"]["steps"]
    aggregate = next(
        step for step in steps if step.get("name", "").startswith("Require the complete")
    )
    rendered = re.sub(
        r"\$\{\{ toJSON\(needs\.([a-z0-9-]+)\) \}\}",
        '{"result": "success", "outputs": {}}',
        aggregate["env"]["NEEDS_JSON"],
    )
    needs = json.loads(rendered)
    assert set(needs) == evidence.NEEDS
    # create_evidence rejects anything else, so the path decision job must not leak in.
    assert "changes" not in needs
    # old: assert aggregate["if"] == "steps.gate.outputs.evidence == 'true'"
    # ci-labels.md rule 3a: a step that runs repo code also requires a successful authz.
    assert aggregate["if"] == (
        "needs.authz.result == 'success' && steps.gate.outputs.evidence == 'true'"
    )


REFUSE_STEP = {
    "name": "authz must have passed",
    "if": "needs.authz.result != 'success'",
    "run": "exit 1",
}


def assert_always_job_refuses_first(job: dict[str, Any]) -> None:
    """ci-labels.md rule 3a for an aggregate job (status function) that needs authz."""
    # old: assert job["if"] == "always()"
    assert job["if"] == AGGREGATE_IF
    assert "authz" in needs(job)
    steps = job["steps"]
    assert steps[0] == REFUSE_STEP, "the refusal is the FIRST step, before any checkout"
    for step in steps[1:]:
        uses_repo = "actions/checkout" in step.get("uses", "") or "run" in step
        if uses_repo and step.get("id") != "gate":
            assert "needs.authz.result == 'success'" in step.get("if", ""), step


def test_always_jobs_refuse_a_failed_authz_before_any_repo_code() -> None:
    ci = load("ci.yml")["jobs"]
    assert_always_job_refuses_first(ci["ready"])
    # The four runtime jobs use !cancelled(); their own if requires a successful authz.
    for name in RUNTIME_JOBS:
        assert "needs.authz.result == 'success'" in ci[name]["if"]
    for workflow in ("ci.yml", "docs.yml", "native-feed.yml"):
        for job_id, job in load(workflow)["jobs"].items():
            condition = job.get("if", "")
            if job_id != "ready" and ("always()" in condition or "!cancelled()" in condition):
                assert "needs.authz.result == 'success'" in condition, (workflow, job_id)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_no_job_uses_always_so_a_superseded_run_never_blocks_the_head(name: str) -> None:
    for job_id, job in load(name)["jobs"].items():
        assert "always()" not in job.get("if", ""), (name, job_id)


@pytest.mark.parametrize(
    "fault", ["always", "no-refusal", "refusal-not-first", "unguarded-checkout"]
)
def test_always_job_rejects_missing_first_refusal_or_unguarded_repo_code(fault: str) -> None:
    job = copy.deepcopy(load("ci.yml")["jobs"]["ready"])
    assert_always_job_refuses_first(job)
    if fault == "always":
        job["if"] = "always()"
    elif fault == "no-refusal":
        del job["steps"][0]
    elif fault == "refusal-not-first":
        job["steps"].insert(1, job["steps"].pop(0))
    else:
        checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
        checkout["if"] = "steps.gate.outputs.evidence == 'true'"
    with pytest.raises(AssertionError):
        assert_always_job_refuses_first(job)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=ci",
            "-c",
            "user.email=ci@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def classify(tmp_path: Path, changed: dict[str, str | None]) -> str:
    """Run the workflow's own classification step on a throwaway base and tested merge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name in ("README.md", "src/module.py", "docs/index.md"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    for name, text in changed.items():
        if text is None:
            git(repo, "rm", "-q", name)
        else:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "change")
    output = tmp_path / "github-output"
    environment = {
        **os.environ,
        "BASE_SHA": base,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
    }
    subprocess.run(
        ["bash", "-e", "-c", step_script("ci.yml", "changes", "paths")],
        cwd=repo,
        env=environment,
        check=True,
    )
    return output.read_text().strip()


def label(changed: dict[str, str | None]) -> str:
    return "+".join(sorted(changed))


DOCS_ONLY = [
    {"README.md": "x"},
    {"CONTRIBUTING.md": "x"},
    {"LICENSE": "x"},
    {"mkdocs.yml": "x"},
    {"docs/index.md": "x"},
    {"docs/nested dir/with space.md": "x"},
    {"README.md": "x", "docs/index.md": "y", "mkdocs.yml": "z"},
]
CODE_CHANGES: list[dict[str, str | None]] = [
    {"src/module.py": "x"},
    {"pyproject.toml": "x"},
    {".github/workflows/ci.yml": "x"},
    {"tests/fixtures/CORPUS.md": "x"},
    {"native/feed/README.md": "x"},
    {"src/README.md": "x"},
    {"docs.py": "x"},
    {"docs-extra/page.md": "x"},
    {"README.md.bak": "x"},
    {"docs/index.md": "x", "src/module.py": "y"},
    # A pure rename of code into docs/ is detected as a rename by default; --no-renames keeps
    # the code path in the list.
    {"src/module.py": None, "docs/module.py": "base\n"},
]


@pytest.mark.parametrize("changed", DOCS_ONLY, ids=label)
def test_docs_only_changes_are_classified_as_docs(tmp_path: Path, changed: dict[str, str]) -> None:
    assert classify(tmp_path, dict(changed)) == "code=false"


@pytest.mark.parametrize("changed", CODE_CHANGES, ids=label)
def test_every_other_path_is_classified_as_code(
    tmp_path: Path, changed: dict[str, str | None]
) -> None:
    assert classify(tmp_path, changed) == "code=true"


def gate_inputs(event: str, changes: str, code: str, unit: str, runtime: str) -> dict[str, str]:
    return {
        # The previously accepted outcomes had no AUTHZ_RESULT input.
        "AUTHZ_RESULT": "success",
        "EVENT_NAME": event,
        "CHANGES_RESULT": changes,
        "CODE_CHANGED": code,
        "UNIT_RESULT": unit,
        "RUNTIME_RESULTS": runtime,
    }


def run_gate(tmp_path: Path, inputs: dict[str, str]) -> tuple[int, str, str]:
    output = tmp_path / "github-output"
    output.write_text("")
    completed = subprocess.run(
        ["bash", "-e", "-c", step_script("ci.yml", "ready", "gate")],
        # old: env={**os.environ, **inputs, "GITHUB_OUTPUT": str(output)}
        env={**inputs, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    return completed.returncode, output.read_text().strip(), completed.stdout


ALL_SUCCESS = "success success success success"
ALL_SKIPPED = "skipped skipped skipped skipped"
ACCEPTED = {
    "push": (gate_inputs("push", "skipped", "", "success", ALL_SUCCESS), "evidence=true"),
    "code PR": (
        gate_inputs("pull_request", "success", "true", "success", ALL_SUCCESS),
        "evidence=true",
    ),
    "docs-only PR": (
        gate_inputs("pull_request", "success", "false", "success", ALL_SKIPPED),
        "evidence=false",
    ),
}
AUTHZ_REJECTED = {
    f"{case}: authz {result or 'empty'}": {**inputs, "AUTHZ_RESULT": result}
    for case, (inputs, _expected) in ACCEPTED.items()
    for result in ("failure", "cancelled", "skipped", "")
}
REJECTED = {
    **AUTHZ_REJECTED,
    "push skips a runtime job": gate_inputs(
        "push", "skipped", "", "success", "success skipped success success"
    ),
    "code PR skips every runtime job": gate_inputs(
        "pull_request", "success", "true", "success", ALL_SKIPPED
    ),
    "code PR with a failed runtime job": gate_inputs(
        "pull_request", "success", "true", "success", "success success failure success"
    ),
    "docs-only PR ran the runtime jobs": gate_inputs(
        "pull_request", "success", "false", "success", ALL_SUCCESS
    ),
    "docs-only PR with a failed runtime job": gate_inputs(
        "pull_request", "success", "false", "success", "skipped skipped skipped failure"
    ),
    "PR whose path decision failed": gate_inputs(
        "pull_request", "failure", "", "success", ALL_SKIPPED
    ),
    "PR whose path decision was skipped": gate_inputs(
        "pull_request", "skipped", "", "success", ALL_SKIPPED
    ),
    "PR with an invalid path decision": gate_inputs(
        "pull_request", "success", "maybe", "success", ALL_SKIPPED
    ),
    "PR with an empty path decision": gate_inputs(
        "pull_request", "success", "", "success", ALL_SKIPPED
    ),
    "code PR with a failed unit matrix": gate_inputs(
        "pull_request", "success", "true", "failure", ALL_SUCCESS
    ),
    "docs-only PR with a skipped unit matrix": gate_inputs(
        "pull_request", "success", "false", "skipped", ALL_SKIPPED
    ),
    "push with a failed unit matrix": gate_inputs("push", "skipped", "", "failure", ALL_SUCCESS),
    "a runtime result is missing": gate_inputs(
        "push", "skipped", "", "success", "success success success"
    ),
    "an unknown runtime result is present": gate_inputs(
        "push", "skipped", "", "success", "success success success success success"
    ),
}


@pytest.mark.parametrize("case", list(ACCEPTED))
def test_release_ready_accepts_only_the_complete_outcomes(tmp_path: Path, case: str) -> None:
    inputs, expected = ACCEPTED[case]
    assert inputs["AUTHZ_RESULT"] == "success"
    returncode, output, _message = run_gate(tmp_path, inputs)
    # old: assert run_gate(tmp_path, inputs) == (0, expected)
    assert (returncode, output) == (0, expected)


@pytest.mark.parametrize("case", list(REJECTED))
def test_release_ready_rejects_every_unexpected_outcome(tmp_path: Path, case: str) -> None:
    # old: returncode, output = run_gate(tmp_path, REJECTED[case])
    returncode, output, _message = run_gate(tmp_path, REJECTED[case])
    assert returncode != 0
    assert output == ""


def test_release_ready_checks_authz_first() -> None:
    assert_ready_authz(load("ci.yml")["jobs"]["ready"])


@pytest.mark.parametrize("fault", ["missing-result", "wrong-result", "late-check"])
def test_release_ready_rejects_missing_or_late_authz_checks(fault: str) -> None:
    job = copy.deepcopy(load("ci.yml")["jobs"]["ready"])
    assert_ready_authz(job)
    gate = next(step for step in job["steps"] if step.get("id") == "gate")
    if fault == "missing-result":
        del gate["env"]["AUTHZ_RESULT"]
    elif fault == "wrong-result":
        gate["env"]["AUTHZ_RESULT"] = "${{ needs.test.result }}"
    else:
        script = gate["run"]
        assert script.startswith(READY_AUTHZ_CHECK + "\n")
        gate["run"] = script.removeprefix(READY_AUTHZ_CHECK + "\n") + READY_AUTHZ_CHECK + "\n"
    with pytest.raises(AssertionError):
        assert_ready_authz(job)


@pytest.mark.parametrize("case", list(AUTHZ_REJECTED))
@pytest.mark.parametrize("other_failure", [False, True])
def test_release_ready_rejects_failed_authz_before_other_outcomes(
    tmp_path: Path, case: str, other_failure: bool
) -> None:
    inputs = dict(AUTHZ_REJECTED[case])
    if other_failure:
        inputs.update(CHANGES_RESULT="failure", UNIT_RESULT="failure")
    returncode, output, message = run_gate(tmp_path, inputs)
    assert returncode != 0
    assert output == ""
    assert message == f"authz: {inputs['AUTHZ_RESULT']}\n"
