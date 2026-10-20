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
FORK_TEST = 'if [ "$FORK_PR" = "true" ]; then'
BOT_TEST = (
    'if [ "$ACTOR" = \'dependabot[bot]\' ] || [ "$ACTOR" = \'pineforge-app[bot]\' ]; then'
)
PERMISSION_API = (
    'if ! perm=$(gh api "repos/${REPO}/collaborators/${ACTOR}/permission" '
    "--jq '.permission'); then"
)
FORK_PR_ENV = (
    "${{ (github.event_name == 'pull_request' || github.event_name == 'pull_request_target') && "
    "(github.event.pull_request.head.repo.full_name != github.repository || "
    "github.event.pull_request.head.repo.fork != false) }}"
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
    # old: assert "secrets." not in (WORKFLOWS / name).read_text()
    for key, value in workflow.items():
        if key != "jobs":
            assert_secret_references(value, set())
    for job_id, job in workflow["jobs"].items():
        allowed = {"secrets.GITHUB_TOKEN"} if job_id == "authz" else set()
        assert_secret_references(job, allowed)
        # old: assert job_id == "deploy"
        # The old restriction applied whenever a job had its own permissions.
        if job_id == "authz":
            assert job.get("permissions") == {"contents": "read"}
        elif name == "docs.yml" and job_id == "deploy":
            assert job["permissions"] == {"pages": "write", "id-token": "write"}
            assert job["if"] == "github.event_name != 'pull_request'"
            assert job["environment"]["name"] == "github-pages"
        else:
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


def assert_authz_branch(lines: list[str], condition: str, result: str) -> tuple[int, int]:
    assert condition in lines
    start = lines.index(condition)
    assert "fi" in lines[start + 1 :]
    end = lines.index("fi", start + 1)
    body = lines[start + 1 : end]
    assert len(body) == 2, "each identity branch reports its decision, then exits"
    assert re.fullmatch(r'echo "[^"]*"', body[0]) is not None
    assert body[1] == f"exit {result}"
    return start, end


def assert_authz_identity(job: dict[str, Any]) -> None:
    assert "if" not in job
    assert "continue-on-error" not in job
    steps = job["steps"]
    for step in steps:
        assert "${{" not in step.get("run", "")
    assert len(steps) == 1, "authz is one bash identity check"
    step = steps[0]
    assert step["shell"] == "bash"
    assert "if" not in step
    assert "continue-on-error" not in step
    env = step["env"]
    assert env["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert env["ACTOR"] == "${{ github.triggering_actor }}"
    assert env["REPO"] == "${{ github.repository }}"
    assert "FORK_PR" in env
    assert " ".join(env["FORK_PR"].split()) == FORK_PR_ENV
    script = step["run"]
    assert "=~" not in script
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    assert lines[0] == "set -euo pipefail"
    fork_start, fork_end = assert_authz_branch(lines, FORK_TEST, "1")
    bot_start, bot_end = assert_authz_branch(lines, BOT_TEST, "0")
    assert "*" not in lines[bot_start]
    assert "case" not in lines[bot_start]
    api_start, api_end = assert_authz_branch(lines, PERMISSION_API, "1")
    empty_start, empty_end = assert_authz_branch(lines, 'if [ -z "$perm" ]; then', "1")
    assert fork_start == 1, "fork rejection precedes every identity shortcut"
    assert bot_start == fork_end + 1
    assert api_start == bot_end + 1
    assert empty_start == api_end + 1
    permission_case = lines[empty_end + 1 :]
    assert len(permission_case) == 4
    assert permission_case[0] == 'case "$perm" in'
    assert permission_case[1] == "admin|maintain|write) exit 0 ;;"
    assert re.fullmatch(r'\*\) echo "[^"]*"; exit 1 ;;', permission_case[2]) is not None
    assert permission_case[3] == "esac"


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
    # old: assert set(workflow["permissions"].values()) == {"read"}
    # old: assert job_id == "deploy"
    # old: assert job["if"] == "github.event_name != 'pull_request'"
    assert_permission_policy(workflow, name)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "job_id,key,value",
    [
        ("authz", "permissions", {"contents": "write"}),
        ("authz", "permissions", {"contents": "read", "actions": "read"}),
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
    workflow = copy.deepcopy(load(name))
    assert_permission_policy(workflow, name)
    workflow["jobs"].setdefault(job_id, {})[key] = value
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
def test_authz_checks_exact_bots_then_collaborator_permissions(name: str) -> None:
    assert_authz_identity(load(name)["jobs"]["authz"])


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "login",
    [
        "github-actions[bot]",
        "pineforge-app",
        "pineforge-app[bot]x",
        "xpineforge-app[bot]",
        "dependabot",
        "dependabot[bot]x",
        "xdependabot[bot]",
    ],
)
def test_authz_rejects_additional_or_lookalike_bot_shortcuts(name: str, login: str) -> None:
    job = copy.deepcopy(load(name)["jobs"]["authz"])
    assert_authz_identity(job)
    script = job["steps"][0]["run"]
    assert BOT_TEST in script
    broader_test = BOT_TEST.removesuffix("; then") + f' || [ "$ACTOR" = \'{login}\' ]; then'
    job["steps"][0]["run"] = script.replace(BOT_TEST, broader_test)
    with pytest.raises(AssertionError):
        assert_authz_identity(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "shortcut",
    [
        'if [[ "$ACTOR" == dependabot* || "$ACTOR" == pineforge-app* ]]; then\n'
        'echo "Authorized bot ${ACTOR}."\nexit 0\nfi',
        'if [[ "$ACTOR" =~ dependabot || "$ACTOR" =~ pineforge-app ]]; then\n'
        'echo "Authorized bot ${ACTOR}."\nexit 0\nfi',
        'case "$ACTOR" in\ndependabot*|pineforge-app*) exit 0 ;;\nesac',
    ],
    ids=["glob", "regex", "case-glob"],
)
def test_authz_rejects_pattern_bot_shortcuts(name: str, shortcut: str) -> None:
    job = copy.deepcopy(load(name)["jobs"]["authz"])
    assert_authz_identity(job)
    lines = [line.strip() for line in job["steps"][0]["run"].splitlines()]
    start = lines.index(BOT_TEST)
    end = lines.index("fi", start)
    job["steps"][0]["run"] = "\n".join([*lines[:start], shortcut, *lines[end + 1 :]])
    with pytest.raises(AssertionError):
        assert_authz_identity(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "old,new",
    [
        ("set -euo pipefail", "set -eu"),
        (PERMISSION_API, PERMISSION_API.replace("if ! ", "if ")),
        ("collaborators/${ACTOR}/permission", "collaborators/permission"),
        (
            'exit 1\nfi\nif [ -z "$perm" ]; then',
            'exit 0\nfi\nif [ -z "$perm" ]; then',
        ),
        ('if [ -z "$perm" ]; then', 'if [ -n "$perm" ]; then'),
        ('exit 1\nfi\ncase "$perm" in', 'exit 0\nfi\ncase "$perm" in'),
        ("admin|maintain|write) exit 0 ;;", "admin|maintain|write|read) exit 0 ;;"),
        ("; exit 1 ;;", "; exit 0 ;;"),
        ("Authorized bot ${ACTOR}.", "Authorized bot ${{ github.actor }}."),
    ],
)
def test_authz_rejects_identity_check_regressions(name: str, old: str, new: str) -> None:
    job = copy.deepcopy(load(name)["jobs"]["authz"])
    assert_authz_identity(job)
    script = "\n".join(line.strip() for line in job["steps"][0]["run"].splitlines())
    assert old in script
    changed = script.replace(old, new)
    assert changed != script
    job["steps"][0]["run"] = changed
    with pytest.raises(AssertionError):
        assert_authz_identity(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "fault", ["missing-env", "wrong-env", "wrong-test", "exit-zero", "bot-before-fork"]
)
def test_authz_rejects_fork_bypasses(name: str, fault: str) -> None:
    job = copy.deepcopy(load(name)["jobs"]["authz"])
    assert_authz_identity(job)
    step = job["steps"][0]
    lines = [line.strip() for line in step["run"].splitlines()]
    fork_start = lines.index(FORK_TEST)
    fork_end = lines.index("fi", fork_start)
    bot_end = lines.index("fi", lines.index(BOT_TEST))
    if fault == "missing-env":
        del step["env"]["FORK_PR"]
    elif fault == "wrong-env":
        step["env"]["FORK_PR"] = "${{ false }}"
    elif fault == "wrong-test":
        lines[fork_start] = 'if [ "$FORK_PR" = "false" ]; then'
    elif fault == "exit-zero":
        lines[fork_end - 1] = "exit 0"
    else:
        lines = [
            *lines[:fork_start],
            *lines[fork_end + 1 : bot_end + 1],
            *lines[fork_start : fork_end + 1],
            *lines[bot_end + 1 :],
        ]
    step["run"] = "\n".join(lines)
    with pytest.raises(AssertionError):
        assert_authz_identity(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "target,key,value",
    [
        ("job", "if", "false"),
        ("job", "continue-on-error", "true"),
        ("step", "shell", "sh"),
        ("step", "if", "false"),
        ("step", "continue-on-error", "true"),
        ("env", "GH_TOKEN", "${{ secrets.OTHER_TOKEN }}"),
        ("env", "ACTOR", "${{ github.actor }}"),
        ("env", "REPO", "${{ github.event.pull_request.head.repo.full_name }}"),
    ],
)
def test_authz_rejects_untrusted_env_and_masked_failures(
    name: str, target: str, key: str, value: str
) -> None:
    job = copy.deepcopy(load(name)["jobs"]["authz"])
    assert_authz_identity(job)
    targets = {"job": job, "step": job["steps"][0], "env": job["steps"][0]["env"]}
    targets[target][key] = value
    with pytest.raises(AssertionError):
        assert_authz_identity(job)


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
    assert jobs["ready"]["if"] == "always()"
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
    assert aggregate["if"] == "steps.gate.outputs.evidence == 'true'"


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
