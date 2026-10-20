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
# ci-labels.md 99de80f9 (frozen 10-20): the required aggregate runs `if: always()`; never
# `!cancelled()` (a cancelled run would SKIP it and GitHub counts a skipped required check as
# success). old: AGGREGATE_IF = "${{ !cancelled() }}" (#830, reverted for security).
AGGREGATE_IF = "always()"

HOSTED_GUARD = (
    "${{ github.event_name != 'pull_request' && "
    "github.event_name != 'pull_request_target' &&"
)
HOSTED_FALLBACK = "|| 'ubuntu-24.04' }}"
POOL = "fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-x64\"]')"
POOL_LABELS = ["self-hosted", "pf-ci", "pf-ci-x64"]
MATRIX_RUNNERS = {
    # old: "ubuntu-24.04": "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-x64\"]') }}",
    "ubuntu-24.04": ["self-hosted", "pf-ci", "pf-ci-x64"],
    # old: "ubuntu-24.04-arm": "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-arm64\"]') }}",
    "ubuntu-24.04-arm": ["self-hosted", "pf-ci", "pf-ci-arm64"],
    "macos-14": "macos-14",
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
CANONICAL_AUTHZ_ENV = {
    "EVENT": "${{ github.event_name }}",
    "REPO": "${{ github.repository }}",
    "HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
    "HEAD_IS_FORK": "${{ github.event.pull_request.head.repo.fork }}",
    "ASSOCIATION": "${{ github.event.pull_request.author_association }}",
    "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
    "WR_HEAD_REPO": "${{ github.event.workflow_run.head_repository.full_name }}",
    "WR_ACTOR": "${{ github.event.workflow_run.triggering_actor.login }}",
    "GH_TOKEN": "${{ github.token }}",
    "ALLOWED_BOTS": "dependabot[bot] pineforge-app[bot]",
}
# Literal transcription of ci-labels.md 99de80f9, including its shell comments.
# old: CANONICAL_AUTHZ_SCRIPT pinned only pull_request_target and HEAD_REPO.
CANONICAL_AUTHZ_SCRIPT = (
    "set -eu\n"
    'fail() { echo "::error title=authz::$1"; echo "authz: REFUSED: $1" '
    '>> "$GITHUB_STEP_SUMMARY"; exit 1; }\n'
    'case "$EVENT" in\n'
    "  push|workflow_dispatch|schedule|merge_group|release|workflow_call|repository_dispatch)\n"
    "    # Only someone with write access to this repository (or our App's token) can cause these\n"
    "    # events; none of them runs pull-request code.\n"
    "    ;;\n"
    "  workflow_run)\n"
    "    # The pwn-request path: it runs in THIS repository with secrets even when "
    "the triggering run\n"
    "    # came from a fork. Refuse ALWAYS unless the triggering run's head is "
    "this repository, and\n"
    "    # apply the same identity rule to its triggering actor as to a pull-request author.\n"
    '    [ "$WR_HEAD_REPO" = "$REPO" ] || fail '
    '"workflow_run triggered from another repository (${WR_HEAD_REPO:-unknown})"\n'
    '    case " $ALLOWED_BOTS " in\n'
    '      *" $WR_ACTOR "*) ;;\n'
    "      *)\n"
    "        # No author_association exists on workflow_run: ask GitHub for the actor's "
    "permission on\n"
    "        # this repository; anything but admin/maintain/write (or a failed call) refuses.\n"
    '        perm=$(gh api "repos/$REPO/collaborators/$WR_ACTOR/permission" '
    "--jq .permission 2>/dev/null || echo none)\n"
    '        case "$perm" in admin|maintain|write) ;; *) fail '
    '"workflow_run triggering actor ${WR_ACTOR:-unknown} has permission '
    "'$perm', not write\" ;; esac ;;\n"
    "    esac ;;\n"
    "  pull_request)\n"
    '    [ "$HEAD_REPO" = "$REPO" ] || fail '
    '"pull request from another repository (${HEAD_REPO:-unknown})"\n'
    '    [ "$HEAD_IS_FORK" = "false" ] || fail "pull request from a fork"\n'
    '    case "$ASSOCIATION" in\n'
    "      OWNER|MEMBER|COLLABORATOR) ;;\n"
    "      *)\n"
    '        case " $ALLOWED_BOTS " in\n'
    '          *" $PR_AUTHOR "*) ;;\n'
    '          *) fail "author ${PR_AUTHOR:-unknown} (${ASSOCIATION:-NONE}) '
    'is not an owner, member, collaborator or allowlisted bot" ;;\n'
    "        esac ;;\n"
    "    esac ;;\n"
    '  *) fail "event $EVENT does not run CI '
    '(pull_request_target and other events are not allowed)" ;;\n'
    "esac\n"
    'echo "authz: allowed ($EVENT${PR_AUTHOR:+ by $PR_AUTHOR})" >> "$GITHUB_STEP_SUMMARY"\n'
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
    # old: assert set(by_os) <= set(matrix["os"])
    assert set(by_os) == set(matrix["os"])
    for os_name in matrix["os"]:
        # old: entry = by_os.get(os_name, {})
        # old: if os_name == "macos-14": assert "runner" not in entry
        # old: else: assert entry.get("runner") == MATRIX_RUNNERS[os_name]
        assert by_os[os_name].get("runner") == MATRIX_RUNNERS[os_name]


def assert_runner_policy(workflow: dict[str, Any], name: str = "ci.yml") -> None:
    for job_id, job in workflow["jobs"].items():
        if name == "docs.yml" and job_id == "deploy":
            assert job["runs-on"] == "ubuntu-latest"
        elif name == "native-feed.yml" and job_id == "release":
            # old: assert job["runs-on"] == "${{ matrix.runner || matrix.os }}"
            assert job["runs-on"] == "${{ matrix.runner }}"
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
        elif job_id == "authz":
            # BaseLoader preserves an empty YAML mapping as a Python dict.
            # old: assert "permissions" not in job
            assert job["permissions"] == {}
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


def normalized_authz_script(script: str) -> list[str]:
    return [line.strip() for line in script.splitlines() if not line.lstrip().startswith("#")]


def assert_canonical_authz(job: dict[str, Any]) -> None:
    # old: assert set(job) == {"runs-on", "steps"}
    assert set(job) == {"name", "runs-on", "permissions", "steps"}
    assert job["name"] == "authz"
    assert job["runs-on"] == ["self-hosted", "pf-ci", "pf-ci-x64"]
    assert job["permissions"] == {}
    assert_secret_references(job, set())
    steps = job["steps"]
    assert isinstance(steps, list)
    assert len(steps) == 1, "authz is exactly the frozen canonical decision"
    step = steps[0]
    assert set(step) == {"name", "env", "run"}
    # old: assert step["name"] == "Same-repository pull requests only"
    assert step["name"] == "Only our own pull requests and our own events run CI"
    # old: env pinned EVENT, HEAD_REPO and REPO only.
    assert step["env"] == CANONICAL_AUTHZ_ENV
    script = step["run"]
    assert isinstance(script, str)
    # old: assert " ".join(script.split()) == " ".join(CANONICAL_AUTHZ_SCRIPT.split())
    assert normalized_authz_script(script) == normalized_authz_script(CANONICAL_AUTHZ_SCRIPT)


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
        ("native-feed.yml", "release", "${{ matrix.runner || matrix.os }}"),
        ("native-feed.yml", "release", "${{ matrix.runner || 'ubuntu-24.04' }}"),
    ],
)
def test_runner_exceptions_reject_other_routes(name: str, job_id: str, route: Any) -> None:
    workflow = copy.deepcopy(load(name))
    assert_runner_policy(workflow, name)
    workflow["jobs"][job_id]["runs-on"] = route
    with pytest.raises(AssertionError):
        assert_runner_policy(workflow, name)


# old: def test_native_release_matrix_uses_only_the_two_linux_pools() -> None:
def test_native_release_matrix_pins_every_runner_and_the_macos_exception() -> None:
    assert_native_release_matrix(load("native-feed.yml")["jobs"]["release"]["strategy"]["matrix"])


@pytest.mark.parametrize(
    "os_name,runner",
    [
        ("ubuntu-24.04", GUARDED_ROUTE),
        ("ubuntu-24.04", "ubuntu-24.04"),
        ("ubuntu-24.04", "macos-14"),
        ("ubuntu-24.04", MATRIX_RUNNERS["ubuntu-24.04-arm"]),
        ("ubuntu-24.04", "${{ " + POOL + " }}"),
        ("ubuntu-24.04-arm", "ubuntu-24.04-arm"),
        ("ubuntu-24.04-arm", "macos-14"),
        ("ubuntu-24.04-arm", MATRIX_RUNNERS["ubuntu-24.04"]),
        (
            "ubuntu-24.04-arm",
            "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-arm64\"]') }}",
        ),
        ("macos-14", MATRIX_RUNNERS["ubuntu-24.04"]),
        ("macos-14", MATRIX_RUNNERS["ubuntu-24.04-arm"]),
        ("macos-14", "ubuntu-24.04"),
    ],
)
def test_native_release_matrix_rejects_fallbacks_and_wrong_pools(
    os_name: str, runner: Any
) -> None:
    original = load("native-feed.yml")["jobs"]["release"]["strategy"]["matrix"]
    assert_native_release_matrix(original)
    matrix = copy.deepcopy(original)
    # old: matrix["include"].append({"os": os_name, "runner": runner}) for macos-14.
    entry = next(entry for entry in matrix["include"] if entry["os"] == os_name)
    entry["runner"] = runner
    assert matrix != original
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


@pytest.mark.parametrize("os_name", list(MATRIX_RUNNERS))
@pytest.mark.parametrize("fault", ["missing-include", "missing-runner", "duplicate-include"])
def test_native_release_matrix_rejects_incomplete_or_duplicate_includes(
    os_name: str, fault: str
) -> None:
    original = load("native-feed.yml")["jobs"]["release"]["strategy"]["matrix"]
    assert_native_release_matrix(original)
    matrix = copy.deepcopy(original)
    entry = next(entry for entry in matrix["include"] if entry["os"] == os_name)
    if fault == "missing-include":
        matrix["include"].remove(entry)
    elif fault == "missing-runner":
        del entry["runner"]
    else:
        matrix["include"].append(dict(entry))
    assert matrix != original
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
        # old: 'pull_request_target) echo "..."; exit 1 ;;' was a separate branch.
        (
            '*) fail "event $EVENT does not run CI '
            '(pull_request_target and other events are not allowed)" ;;',
            "*) ;;",
        ),
        (
            '*) fail "event $EVENT does not run CI '
            '(pull_request_target and other events are not allowed)" ;;',
            "pull_request_target) exit 0 ;;",
        ),
        # old: ('; exit 1 ;;', ' ;;') and ('exit 1; } ;;', '} ;;').
        ("; exit 1; }", "; }"),
        # old: ("] || {", "] && {").
        ("] || fail", "] && fail"),
        ("exit 1; }", "return 0; }"),
        ("exit 1; }", "exit 0; }"),
        # old: ("] || {", "] || true || {").
        ("] || fail", "] || true || fail"),
        ("set -eu", "set -u"),
        ('[ "$HEAD_IS_FORK" = "false" ]', '[ "$HEAD_IS_FORK" != "true" ]'),
        ('[ "$WR_HEAD_REPO" = "$REPO" ]', '[ "$WR_HEAD_REPO" != "$REPO" ]'),
        ("OWNER|MEMBER|COLLABORATOR)", "OWNER|MEMBER|COLLABORATOR|CONTRIBUTOR)"),
        ("admin|maintain|write)", "admin|maintain|write|read)"),
        ('*" $PR_AUTHOR "*)', '*"$PR_AUTHOR"*)'),
        ('*" $WR_ACTOR "*)', '*"$WR_ACTOR"*)'),
        ("2>/dev/null || echo none)", "2>/dev/null || echo write)"),
    ],
    ids=[
        "missing-target-rejection",
        "target-success",
        "fail-exit-dropped",
        "and-instead-of-or",
        "fail-returns-success",
        "fail-exits-zero",
        "or-true",
        "no-errexit",
        "fork-not-exactly-false",
        "workflow-run-head-reversed",
        "contributor-allowed",
        "read-permission-allowed",
        "pr-bot-substring",
        "workflow-run-bot-substring",
        "failed-lookup-allowed",
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
@pytest.mark.parametrize("key", list(CANONICAL_AUTHZ_ENV))
def test_authz_rejects_missing_canonical_env(name: str, key: str) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    del job["steps"][0]["env"][key]
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize("key", ["name", "runs-on", "permissions", "steps"])
def test_authz_rejects_missing_canonical_job_keys(name: str, key: str) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    del job[key]
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
        ("job", "permissions", {"contents": "none"}),
        ("job", "permissions", "{}"),
        ("job", "permissions", []),
        ("job", "permissions", None),
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
        ("env", "HEAD_IS_FORK", "${{ github.event.repository.fork }}"),
        ("env", "ASSOCIATION", "${{ github.actor }}"),
        ("env", "PR_AUTHOR", "${{ github.actor }}"),
        ("env", "WR_HEAD_REPO", "${{ github.repository }}"),
        ("env", "WR_ACTOR", "${{ github.actor }}"),
        ("env", "GH_TOKEN", "${{ secrets.GITHUB_TOKEN }}"),
        ("env", "GH_TOKEN", "unused"),
        ("env", "ALLOWED_BOTS", "dependabot[bot] pineforge-app[bot] github-actions[bot]"),
        ("env", "ALLOWED_BOTS", "dependabot* pineforge-app*"),
        ("env", "ALLOWED_BOTS", "dependabot[bot]"),
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
    # old: the expression fixture was MATRIX_RUNNERS["ubuntu-24.04"].
    "route", ["ubuntu-24.04", ["ubuntu-24.04"], "${{ " + POOL + " }}"]
)
def test_authz_rejects_hosted_or_expression_runners(name: str, route: Any) -> None:
    original = load(name)["jobs"]["authz"]
    assert_canonical_authz(original)
    job = copy.deepcopy(original)
    job["runs-on"] = route
    assert job != original
    with pytest.raises(AssertionError):
        assert_canonical_authz(job)


AUTHZ_REPO = "pineforge/pineforge-data"


def authz_inputs(event: str = "pull_request", **overrides: str) -> dict[str, str]:
    inputs = {
        "EVENT": event,
        "REPO": AUTHZ_REPO,
        "HEAD_REPO": "",
        "HEAD_IS_FORK": "",
        "ASSOCIATION": "",
        "PR_AUTHOR": "",
        "WR_HEAD_REPO": "",
        "WR_ACTOR": "",
        "GH_TOKEN": "unit-test-token",
        "ALLOWED_BOTS": "dependabot[bot] pineforge-app[bot]",
        "FAKE_GH_PERMISSION": "none",
        "FAKE_GH_FAIL": "false",
    }
    if event == "pull_request":
        inputs.update(
            HEAD_REPO=AUTHZ_REPO,
            HEAD_IS_FORK="false",
            ASSOCIATION="MEMBER",
            PR_AUTHOR="alice",
        )
    elif event == "workflow_run":
        inputs.update(WR_HEAD_REPO=AUTHZ_REPO, WR_ACTOR="alice")
    return {**inputs, **overrides}


# Each row is (environment, exit code, refusal reason, permission lookup expected).
# Refusal reasons are literal expectations, independent of the workflow being executed.
AUTHZ_DECISIONS: dict[str, tuple[dict[str, str], int, str, bool]] = {
    **{
        f"pr-{association.lower()}": (
            authz_inputs(ASSOCIATION=association),
            0,
            "",
            False,
        )
        for association in ("MEMBER", "OWNER", "COLLABORATOR")
    },
    **{
        f"pr-{author}": (
            authz_inputs(ASSOCIATION="NONE", PR_AUTHOR=author),
            0,
            "",
            False,
        )
        for author in ("dependabot[bot]", "pineforge-app[bot]")
    },
    **{
        event: (authz_inputs(event), 0, "", False)
        for event in (
            "push",
            "workflow_dispatch",
            "schedule",
            "merge_group",
            "release",
            "workflow_call",
            "repository_dispatch",
        )
    },
    **{
        f"workflow-run-{actor}": (
            authz_inputs("workflow_run", WR_ACTOR=actor),
            0,
            "",
            False,
        )
        for actor in ("dependabot[bot]", "pineforge-app[bot]")
    },
    **{
        f"workflow-run-{permission}": (
            authz_inputs("workflow_run", FAKE_GH_PERMISSION=permission),
            0,
            "",
            True,
        )
        for permission in ("write", "admin", "maintain")
    },
    "pr-other-repository": (
        authz_inputs(HEAD_REPO="outsider/pineforge-data", HEAD_IS_FORK="true"),
        1,
        "pull request from another repository (outsider/pineforge-data)",
        False,
    ),
    "pr-fork-flag": (
        authz_inputs(HEAD_IS_FORK="true"),
        1,
        "pull request from a fork",
        False,
    ),
    "pr-empty-head": (
        authz_inputs(HEAD_REPO=""),
        1,
        "pull request from another repository (unknown)",
        False,
    ),
    **{
        f"pr-outsider-{association}": (
            authz_inputs(ASSOCIATION=association, PR_AUTHOR="outsider"),
            1,
            f"author outsider ({association}) is not an owner, member, "
            "collaborator or allowlisted bot",
            False,
        )
        for association in ("NONE", "CONTRIBUTOR")
    },
    **{
        f"pr-unlisted-{author}": (
            authz_inputs(ASSOCIATION="NONE", PR_AUTHOR=author),
            1,
            f"author {author} (NONE) is not an owner, member, collaborator or allowlisted bot",
            False,
        )
        for author in (
            "github-actions[bot]",
            "renovate[bot]",
            "pineforge-app",
            "pineforge-app[bot]x",
            "xpineforge-app[bot]",
            "dependabot",
            "dependabot[bot]x",
        )
    },
    **{
        event: (
            authz_inputs(event),
            1,
            f"event {event} does not run CI "
            "(pull_request_target and other events are not allowed)",
            False,
        )
        for event in ("pull_request_target", "unknown_event")
    },
    "workflow-run-other-repository": (
        authz_inputs(
            "workflow_run",
            WR_HEAD_REPO="outsider/pineforge-data",
            FAKE_GH_PERMISSION="write",
        ),
        1,
        "workflow_run triggered from another repository (outsider/pineforge-data)",
        False,
    ),
    "workflow-run-fork-bot": (
        authz_inputs(
            "workflow_run",
            WR_HEAD_REPO="outsider/pineforge-data",
            WR_ACTOR="dependabot[bot]",
        ),
        1,
        "workflow_run triggered from another repository (outsider/pineforge-data)",
        False,
    ),
    "workflow-run-empty-head": (
        authz_inputs("workflow_run", WR_HEAD_REPO="", FAKE_GH_PERMISSION="write"),
        1,
        "workflow_run triggered from another repository (unknown)",
        False,
    ),
    **{
        f"workflow-run-{permission}": (
            authz_inputs("workflow_run", FAKE_GH_PERMISSION=permission),
            1,
            f"workflow_run triggering actor alice has permission '{permission}', not write",
            True,
        )
        for permission in ("read", "triage", "none")
    },
    "workflow-run-lookup-fails": (
        authz_inputs("workflow_run", FAKE_GH_PERMISSION="write", FAKE_GH_FAIL="true"),
        1,
        "workflow_run triggering actor alice has permission 'none', not write",
        True,
    ),
}

FAKE_AUTHZ_GH = r"""#!/bin/bash
set -eu
printf '%s\n' "$@" >> "$FAKE_GH_CALLS"
[ "$#" -eq 4 ] || exit 97
[ "$1" = api ] || exit 97
[ "$2" = "repos/$REPO/collaborators/$WR_ACTOR/permission" ] || exit 97
[ "$3" = --jq ] || exit 97
[ "$4" = .permission ] || exit 97
[ "$GH_TOKEN" = unit-test-token ] || exit 97
printf '%s\n' validated >> "$FAKE_GH_CALLS"
[ "$FAKE_GH_FAIL" = false ] || exit 1
printf '%s\n' "$FAKE_GH_PERMISSION"
"""


def run_authz(
    tmp_path: Path,
    name: str,
    inputs: dict[str, str],
    *,
    lookup: bool,
    script: str | None = None,
) -> tuple[int, str, str]:
    """Execute the workflow's decision with no inherited environment or real gh."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_AUTHZ_GH)
    gh.chmod(0o700)
    summary = tmp_path / "summary"
    summary.write_text("")
    calls = tmp_path / "gh-calls"
    calls.write_text("")
    if script is None:
        script = load(name)["jobs"]["authz"]["steps"][0]["run"]
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=tmp_path,
        env={
            **inputs,
            # The stub is the only PATH entry; both scripts use shell builtins.
            "PATH": str(bin_dir),
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    expected_calls = (
        [
            "api",
            f"repos/{inputs['REPO']}/collaborators/{inputs['WR_ACTOR']}/permission",
            "--jq",
            ".permission",
            "validated",
        ]
        if lookup
        else []
    )
    assert calls.read_text().splitlines() == expected_calls
    assert completed.stderr == ""
    return completed.returncode, summary.read_text(), completed.stdout


def allowed_authz_summary(inputs: dict[str, str]) -> str:
    author = f" by {inputs['PR_AUTHOR']}" if inputs["PR_AUTHOR"] else ""
    return f"authz: allowed ({inputs['EVENT']}{author})\n"


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize("case", list(AUTHZ_DECISIONS))
def test_authz_executes_the_frozen_decision(tmp_path: Path, name: str, case: str) -> None:
    inputs, expected_code, reason, lookup = AUTHZ_DECISIONS[case]
    returncode, summary, output = run_authz(tmp_path, name, inputs, lookup=lookup)
    assert returncode == expected_code
    if expected_code == 1:
        assert summary == f"authz: REFUSED: {reason}\n"
        assert output == f"::error title=authz::{reason}\n"
    else:
        assert reason == ""
        assert summary == allowed_authz_summary(inputs)
        assert output == ""


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    "case",
    [
        "pr-other-repository",
        "pr-fork-flag",
        "pr-empty-head",
        "pr-outsider-NONE",
        "pr-unlisted-github-actions[bot]",
        "pull_request_target",
        "unknown_event",
        "workflow-run-fork-bot",
        "workflow-run-read",
        "workflow-run-lookup-fails",
    ],
)
def test_authz_decisions_depend_on_fail(tmp_path: Path, name: str, case: str) -> None:
    inputs, expected_code, reason, lookup = AUTHZ_DECISIONS[case]
    assert expected_code == 1
    script = load(name)["jobs"]["authz"]["steps"][0]["run"]
    fail_definition = CANONICAL_AUTHZ_SCRIPT.splitlines()[1]
    assert script.count(fail_definition) == 1
    mutated = script.replace(fail_definition, "fail() { :; }", 1)
    assert mutated != script
    assert fail_definition not in mutated
    assert "fail() { :; }" in mutated
    original = run_authz(tmp_path / "original", name, inputs, lookup=lookup, script=script)
    assert original == (1, f"authz: REFUSED: {reason}\n", f"::error title=authz::{reason}\n")
    changed = run_authz(tmp_path / "mutated", name, inputs, lookup=lookup, script=mutated)
    assert changed == (0, allowed_authz_summary(inputs), "")


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_authz_decision_depends_on_the_exact_bot_allowlist(tmp_path: Path, name: str) -> None:
    inputs, expected_code, reason, lookup = AUTHZ_DECISIONS["pr-unlisted-github-actions[bot]"]
    assert expected_code == 1
    script = load(name)["jobs"]["authz"]["steps"][0]["run"]
    mutated = (
        'ALLOWED_BOTS="dependabot[bot] pineforge-app[bot] github-actions[bot]"\n' + script
    )
    assert mutated != script
    original = run_authz(tmp_path / "original", name, inputs, lookup=lookup, script=script)
    assert original == (1, f"authz: REFUSED: {reason}\n", f"::error title=authz::{reason}\n")
    changed = run_authz(tmp_path / "mutated", name, inputs, lookup=lookup, script=mutated)
    assert changed == (0, allowed_authz_summary(inputs), "")


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
    # old: assert jobs["ready"]["if"] == "always()"  (then #830: "${{ !cancelled() }}", reverted)
    # ci-labels.md 99de80f9: the required aggregate is always(), never skippable by a cancel.
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


def assert_always_job_refuses_first(job: dict[str, Any], *, runtime: bool = False) -> None:
    """ci-labels.md rule 3a for a job with a status function that needs authz."""
    # old: assert job["if"] == AGGREGATE_IF
    if runtime:
        assert " ".join(job["if"].split()) == RUNTIME_IF
    else:
        # old: assert job["if"] == "always()"
        assert job["if"] == AGGREGATE_IF
    assert "authz" in needs(job)
    steps = job["steps"]
    assert steps[0] == REFUSE_STEP, "the refusal is the FIRST step, before any checkout"
    for step in steps[1:]:
        # old: uses_repo = "actions/checkout" in step.get("uses", "") or "run" in step
        uses_repo = (
            "actions/checkout" in step.get("uses", "")
            or step.get("uses", "").startswith("./")
            or "run" in step
        )
        # old: if uses_repo and step.get("id") != "gate":
        # old:     assert "needs.authz.result == 'success'" in step.get("if", ""), step
        if uses_repo:
            assert "needs.authz.result == 'success'" in step.get("if", ""), step


def test_always_jobs_refuse_a_failed_authz_before_any_repo_code() -> None:
    ci = load("ci.yml")["jobs"]
    assert_always_job_refuses_first(ci["ready"])
    # Status functions require the refusal and step guards even with a job-level authz check.
    for name in RUNTIME_JOBS:
        assert "needs.authz.result == 'success'" in ci[name]["if"]
        assert_always_job_refuses_first(ci[name], runtime=True)
    for workflow in ("ci.yml", "docs.yml", "native-feed.yml"):
        for job_id, job in load(workflow)["jobs"].items():
            condition = job.get("if", "")
            if job_id != "ready" and ("always()" in condition or "!cancelled()" in condition):
                assert "needs.authz.result == 'success'" in condition, (workflow, job_id)


def assert_aggregate_is_always(jobs: dict[str, Any]) -> None:
    # old: assert "always()" not in job.get("if", "") for every job (the #830 pin, reverted).
    assert jobs["ready"]["if"] == "always()"
    for job_id, job in jobs.items():
        condition = job.get("if", "")
        if job_id == "ready":
            continue
        # No other job may be a bare `!cancelled()` aggregate that a cancelled run skips.
        assert condition.replace(" ", "") not in ("!cancelled()", "${{!cancelled()}}"), job_id


def test_the_required_aggregate_runs_always_never_cancelled_only() -> None:
    assert_aggregate_is_always(load("ci.yml")["jobs"])


@pytest.mark.parametrize("fault", ["${{ !cancelled() }}", "!cancelled()", "success()", ""])
def test_an_aggregate_that_a_cancelled_run_can_skip_is_rejected(fault: str) -> None:
    jobs = copy.deepcopy(load("ci.yml")["jobs"])
    assert_aggregate_is_always(jobs)
    jobs["ready"]["if"] = fault
    with pytest.raises(AssertionError):
        assert_aggregate_is_always(jobs)


@pytest.mark.parametrize(
    # old: "always" wrote "always()" back unchanged after the canon was frozen.
    "fault", ["cancelled-only", "no-refusal", "refusal-not-first", "unguarded-checkout"]
)
def test_always_job_rejects_missing_first_refusal_or_unguarded_repo_code(fault: str) -> None:
    original = load("ci.yml")["jobs"]["ready"]
    assert_always_job_refuses_first(original)
    job = copy.deepcopy(original)
    if fault == "cancelled-only":
        # old: job["if"] = "always()"
        job["if"] = "${{ !cancelled() }}"
    elif fault == "no-refusal":
        del job["steps"][0]
    elif fault == "refusal-not-first":
        job["steps"].insert(1, job["steps"].pop(0))
    else:
        checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
        checkout["if"] = "steps.gate.outputs.evidence == 'true'"
    assert job != original
    with pytest.raises(AssertionError):
        assert_always_job_refuses_first(job)


@pytest.mark.parametrize("job_id", [*RUNTIME_JOBS, "ready"])
@pytest.mark.parametrize("fault", ["no-refusal", "unguarded-run"])
def test_status_jobs_reject_missing_refusal_or_unguarded_run(job_id: str, fault: str) -> None:
    original = load("ci.yml")["jobs"][job_id]
    runtime = job_id in RUNTIME_JOBS
    assert_always_job_refuses_first(original, runtime=runtime)
    job = copy.deepcopy(original)
    if fault == "no-refusal":
        del job["steps"][0]
    else:
        run = next(step for step in job["steps"][1:] if "run" in step)
        del run["if"]
    assert job != original
    with pytest.raises(AssertionError):
        assert_always_job_refuses_first(job, runtime=runtime)


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
