"""Pull-request CI: runner pins, authorization, cancellation and canonical cheap PRs."""

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
CI_CANON = ROOT / "tests/fixtures/ci_canon"
PR_TIER_WORKFLOWS = ("ci.yml", "docs.yml", "native-feed.yml")
RUNTIME_JOBS = ("docker", "database-e2e", "contract", "distribution")

HOSTED_GUARD = (
    "${{ github.event_name != 'pull_request' && "
    "github.event_name != 'pull_request_target' &&"
)
HOSTED_FALLBACK = "|| 'ubuntu-24.04' }}"
POOL = "fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-x64\"]')"
POOL_LABELS = ["self-hosted", "pf-ci", "pf-ci-x64"]
ARM_POOL_LABELS = ["self-hosted", "pf-ci", "pf-ci-arm64"]
# CI-2 = A (owner 10-21 04:01): every secret-free PR/CI job in this public repository runs on
# a GitHub-hosted runner. HOSTED is the only accepted route for those jobs.
# old: HOSTED = "ubuntu-latest"
# #1050 / laneboss map #1051: the one standard hosted label is ubuntu-24.04
# (ubuntu-latest is rejected).
HOSTED = "ubuntu-24.04"
MATRIX_RUNNERS = {
    # old: "ubuntu-24.04": "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-x64\"]') }}",
    "ubuntu-24.04": "ubuntu-24.04",
    # old: "ubuntu-24.04-arm": "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-arm64\"]') }}",
    "ubuntu-24.04-arm": "ubuntu-24.04-arm",
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
# Literal transcription of ci-labels.md ac875205, including its shell comments.
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
    "    # LB-437: bind identity to the triggering run and its re-runner.\n"
    "    # These two App bots are fixed canon policy; ALLOWED_BOTS is PR-only.\n"
    '    [ -n "${WR_HEAD_REPO:-}" ] || fail "workflow_run head repository is missing"\n'
    '    [ -n "${WR_ACTOR:-}" ] || fail "workflow_run triggering actor is missing"\n'
    '    [ "$WR_HEAD_REPO" = "$REPO" ] || fail '
    '"workflow_run head repository does not match this repository"\n'
    '    case "$WR_ACTOR" in\n'
    '      "pineforge-app[bot]"|"pineforge-ci-dispatcher[bot]") ;;\n'
    '      *"[bot]") fail "workflow_run bot is not on the canon allowlist" ;;\n'
    "      *)\n"
    '        perm=$(gh api "repos/$REPO/collaborators/$WR_ACTOR/permission" '
    '--jq .permission 2>/dev/null) || fail "workflow_run permission lookup failed"\n'
    '        case "$perm" in admin|maintain|write) ;; *) fail '
    '"workflow_run triggering actor does not have write permission" ;; esac ;;\n'
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
    # old: '          *) fail "author ${PR_AUTHOR:-unknown} (${ASSOCIATION:-NONE}) '
    # old: 'is not an owner, member, collaborator or allowlisted bot" ;;\n'
    "          *)\n"
    "            # A member whose org membership is private shows as CONTRIBUTOR: ask GitHub "
    "for the\n"
    "            # author's permission on this repository; anything but admin/maintain/write "
    "(or a\n"
    "            # failed call) refuses.\n"
    '            perm=$(gh api "repos/$REPO/collaborators/$PR_AUTHOR/permission" '
    "--jq .permission 2>/dev/null || echo none)\n"
    '            case "$perm" in admin|maintain|write) ;; *) fail '
    '"author ${PR_AUTHOR:-unknown} (${ASSOCIATION:-NONE}) has permission '
    "'$perm', not write, and is not an allowlisted bot\" ;; esac ;;\n"
    "        esac ;;\n"
    "    esac ;;\n"
    '  *) fail "event $EVENT does not run CI '
    '(pull_request_target and other events are not allowed)" ;;\n'
    "esac\n"
    'echo "authz: allowed ($EVENT${PR_AUTHOR:+ by $PR_AUTHOR})" >> "$GITHUB_STEP_SUMMARY"\n'
)


def load(name: str) -> dict[str, Any]:
    loaded = yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def canon_job(filename: str, job_id: str) -> dict[str, Any]:
    loaded = yaml.load((CI_CANON / filename).read_text(), Loader=yaml.BaseLoader)
    # The heavy-job example is a job fragment; the other fixtures include jobs:.
    return dict(loaded.get("jobs", loaded)[job_id])


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
            # old: assert job["runs-on"] == ["self-hosted", "pf-ci", "pf-ci-release"]
            assert job["runs-on"] == HOSTED
        elif name == "native-feed.yml" and job_id == "release":
            # old: assert job["runs-on"] == "${{ matrix.runner || matrix.os }}"
            assert job["runs-on"] == "${{ matrix.runner }}"
            assert_native_release_matrix(job["strategy"]["matrix"])
        else:
            assert job["runs-on"] == HOSTED


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
            # old: assert job["if"] == "github.event_name != 'pull_request'"
            # The release pool admits a deploy only from main: no pull request, no other ref.
            assert job["if"] == (
                "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
            )
            assert job["environment"]["name"] == "github-pages"
        elif job_id == "authz":
            # BaseLoader preserves an empty YAML mapping as a Python dict.
            # old: assert "permissions" not in job
            assert job["permissions"] == {}
        elif name == "ci.yml" and job_id in ("changes", "workflow-lint", "check"):
            canonical_permissions = canon_job("canon-changes-job.yml", "changes")["permissions"]
            assert job["permissions"] == canonical_permissions
        elif name == "ci.yml" and job_id == "docs-tests":
            # The light documentation job may read the repository and nothing else.
            assert job["permissions"] == {"contents": "read"}
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
    # old: assert set(job) == {"runs-on", "steps"}
    assert set(job) == {"name", "runs-on", "permissions", "steps"}
    assert job["name"] == "authz"
    # CI-2 = A: the canonical authz job is secret-free, so it runs on a GitHub-hosted runner
    # in every workflow that has no credentialed job left on pf-ci.
    assert job["runs-on"] == HOSTED
    assert job["permissions"] == {}
    assert_secret_references(job, set())
    steps = job["steps"]
    assert isinstance(steps, list)
    assert len(steps) == 1, "authz is exactly the frozen canonical decision"
    assert steps == canon_job("canon-authz-job.yml", "authz")["steps"]
    step = steps[0]
    assert set(step) == {"name", "env", "run"}
    # old: assert step["name"] == "Same-repository pull requests only"
    assert step["name"] == "Only our own pull requests and our own events run CI"
    # old: env pinned EVENT, HEAD_REPO and REPO only.
    assert step["env"] == CANONICAL_AUTHZ_ENV
    script = step["run"]
    assert isinstance(script, str)
    # Line by line (newlines are shell syntax), comments included: the canon says "copy as is".
    assert [line.strip() for line in script.strip().splitlines()] == [
        line.strip() for line in CANONICAL_AUTHZ_SCRIPT.strip().splitlines()
    ]


def cancels_only_pull_requests(concurrency: dict[str, str]) -> bool:
    return (
        concurrency["cancel-in-progress"] == CANCEL_ONLY_PRS
        and PR_OR_RUN_KEY in concurrency["group"]
    )


# old: test_pull_requests_never_select_the_pool
@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_every_secret_free_pr_tier_job_runs_on_the_pinned_hosted_runner(name: str) -> None:
    workflow = load(name)
    # CI-2 = A: no job of these secret-free workflows may select the self-hosted pool.
    assert workflow["jobs"]["authz"]["runs-on"] == HOSTED
    assert_runner_policy(workflow, name)


def test_the_old_same_repo_route_and_the_pool_are_rejected() -> None:
    # old: assert unguarded({"jobs": {"build": {"runs-on": OLD_ROUTE}}}) == ["build#0"]
    for route in (
        OLD_ROUTE,
        GUARDED_ROUTE,
        # old: "ubuntu-24.04",
        "ubuntu-latest",
        "macos-14",
        "windows-latest",
        POOL_LABELS,
        ARM_POOL_LABELS,
    ):
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
        mixed = {"jobs": {"a": {"runs-on": HOSTED}, "b": {"runs-on": HOSTED}}}
        mixed["jobs"][job_id]["runs-on"] = OLD_ROUTE
        with pytest.raises(AssertionError):
            assert_runner_policy(mixed)
    # old: assert unguarded({"jobs": {"a": {"runs-on": GUARDED_ROUTE}}}) == []
    with pytest.raises(AssertionError):
        assert_runner_policy({"jobs": {"a": {"runs-on": GUARDED_ROUTE}}})
    assert_runner_policy({"jobs": {"a": {"runs-on": HOSTED}}})


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
@pytest.mark.parametrize(
    # old: "route", [GUARDED_ROUTE, "ubuntu-24.04", POOL, POOL_LABELS, ARM_POOL_LABELS]
    "route", [GUARDED_ROUTE, "ubuntu-latest", POOL, POOL_LABELS, ARM_POOL_LABELS]
)
def test_each_job_rejects_the_pool_an_expression_or_another_runner(
    name: str, route: Any
) -> None:
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
        # old: ("docs.yml", "deploy", "ubuntu-24.04"),
        ("docs.yml", "deploy", "ubuntu-latest"),
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
        # CI-2 = A: the self-hosted pool must not come back as a leg's runner.
        ("ubuntu-24.04", POOL_LABELS),
        ("ubuntu-24.04", "macos-14"),
        ("ubuntu-24.04", MATRIX_RUNNERS["ubuntu-24.04-arm"]),
        ("ubuntu-24.04", "${{ " + POOL + " }}"),
        ("ubuntu-24.04-arm", ARM_POOL_LABELS),
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


def test_documentation_permission_policy_rejects_wider_scopes() -> None:
    original = load("ci.yml")
    assert_permission_policy(original, "ci.yml")
    for job_id, permissions in (
        ("docs-tests", {"contents": "write"}),
        ("docs-tests", {"contents": "read", "pull-requests": "write"}),
        ("docker", {"contents": "read"}),
    ):
        workflow = copy.deepcopy(original)
        workflow["jobs"][job_id]["permissions"] = permissions
        assert workflow != original
        with pytest.raises(AssertionError):
            assert_permission_policy(workflow, "ci.yml")


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
        (
            '"pineforge-app[bot]"|"pineforge-ci-dispatcher[bot]")',
            '*"pineforge-app[bot]"*|"pineforge-ci-dispatcher[bot]")',
        ),
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
    # CI-2 = A: authz is hosted, so the old self-hosted pool is now the rejected route.
    # old: "route", ["ubuntu-24.04", POOL_LABELS, ARM_POOL_LABELS, "${{ " + POOL + " }}"]
    "route", ["ubuntu-latest", POOL_LABELS, ARM_POOL_LABELS, "${{ " + POOL + " }}"]
)
def test_authz_rejects_any_route_other_than_the_pinned_hosted_runner(
    name: str, route: Any
) -> None:
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
        f"pr-contributor-{permission}": (
            authz_inputs(ASSOCIATION="CONTRIBUTOR", FAKE_GH_PERMISSION=permission),
            0,
            "",
            True,
        )
        for permission in ("write", "admin", "maintain")
    },
    "pr-none-write": (
        authz_inputs(ASSOCIATION="NONE", FAKE_GH_PERMISSION="write"),
        0,
        "",
        True,
    ),
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
        for actor in ("pineforge-app[bot]", "pineforge-ci-dispatcher[bot]")
    },
    **{
        f"workflow-run-{actor}": (
            authz_inputs("workflow_run", WR_ACTOR=actor),
            1,
            "workflow_run bot is not on the canon allowlist",
            False,
        )
        for actor in ("dependabot[bot]", "github-actions[bot]", "xdependabot[bot]")
    },
    **{
        f"workflow-run-{actor}": (
            authz_inputs("workflow_run", WR_ACTOR=actor),
            1,
            "workflow_run triggering actor does not have write permission",
            True,
        )
        for actor in ("pineforge-app", "pineforge-app[bot]x")
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
    "pr-contributor-other-repository": (
        authz_inputs(
            HEAD_REPO="outsider/pineforge-data",
            HEAD_IS_FORK="true",
            ASSOCIATION="CONTRIBUTOR",
            FAKE_GH_PERMISSION="write",
        ),
        1,
        "pull request from another repository (outsider/pineforge-data)",
        False,
    ),
    "pr-contributor-fork": (
        authz_inputs(
            HEAD_IS_FORK="true",
            ASSOCIATION="CONTRIBUTOR",
            FAKE_GH_PERMISSION="write",
        ),
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
            # old: f"author outsider ({association}) is not an owner, member, "
            # old: "collaborator or allowlisted bot",
            f"author outsider ({association}) has permission 'none', not write, "
            "and is not an allowlisted bot",
            # old: False,
            True,
        )
        for association in ("NONE", "CONTRIBUTOR")
    },
    **{
        f"pr-{association.lower()}-{permission}": (
            authz_inputs(ASSOCIATION=association, FAKE_GH_PERMISSION=permission),
            1,
            f"author alice ({association}) has permission '{permission}', not write, "
            "and is not an allowlisted bot",
            True,
        )
        for association in ("NONE", "CONTRIBUTOR")
        for permission in ("read", "triage")
    },
    "pr-lookup-fails": (
        authz_inputs(
            ASSOCIATION="CONTRIBUTOR", FAKE_GH_PERMISSION="write", FAKE_GH_FAIL="true"
        ),
        1,
        "author alice (CONTRIBUTOR) has permission 'none', not write, "
        "and is not an allowlisted bot",
        True,
    ),
    "pr-lookup-empty": (
        authz_inputs(ASSOCIATION="CONTRIBUTOR", FAKE_GH_PERMISSION=""),
        1,
        "author alice (CONTRIBUTOR) has permission '', not write, "
        "and is not an allowlisted bot",
        True,
    ),
    **{
        f"pr-unlisted-{author}": (
            authz_inputs(ASSOCIATION="NONE", PR_AUTHOR=author),
            1,
            # old: f"author {author} (NONE) is not an owner, member, "
            # old: "collaborator or allowlisted bot",
            f"author {author} (NONE) has permission 'none', not write, "
            "and is not an allowlisted bot",
            # old: False,
            True,
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
        "workflow_run head repository does not match this repository",
        False,
    ),
    "workflow-run-fork-bot": (
        authz_inputs(
            "workflow_run",
            WR_HEAD_REPO="outsider/pineforge-data",
            WR_ACTOR="dependabot[bot]",
        ),
        1,
        "workflow_run head repository does not match this repository",
        False,
    ),
    "workflow-run-empty-head": (
        authz_inputs("workflow_run", WR_HEAD_REPO="", FAKE_GH_PERMISSION="write"),
        1,
        "workflow_run head repository is missing",
        False,
    ),
    "workflow-run-empty-actor": (
        authz_inputs("workflow_run", WR_ACTOR=""),
        1,
        "workflow_run triggering actor is missing",
        False,
    ),
    **{
        f"workflow-run-{permission}": (
            authz_inputs("workflow_run", FAKE_GH_PERMISSION=permission),
            1,
            "workflow_run triggering actor does not have write permission",
            True,
        )
        for permission in ("read", "triage", "none")
    },
    "workflow-run-lookup-fails": (
        authz_inputs("workflow_run", FAKE_GH_PERMISSION="write", FAKE_GH_FAIL="true"),
        1,
        "workflow_run permission lookup failed",
        True,
    ),
}

FAKE_AUTHZ_GH = r"""#!/bin/bash
set -eu
printf '%s\n' "$@" >> "$FAKE_GH_CALLS"
[ "$#" -eq 4 ] || exit 97
[ "$1" = api ] || exit 97
# old: [ "$2" = "repos/$REPO/collaborators/$WR_ACTOR/permission" ] || exit 97
case "$EVENT" in
  pull_request) actor=$PR_AUTHOR ;;
  workflow_run) actor=$WR_ACTOR ;;
  *) exit 97 ;;
esac
[ "$2" = "repos/$REPO/collaborators/$actor/permission" ] || exit 97
[ "$3" = --jq ] || exit 97
[ "$4" = .permission ] || exit 97
[ "$GH_TOKEN" = unit-test-token ] || exit 97
printf '%s\n' validated >> "$FAKE_GH_CALLS"
[ "$FAKE_GH_FAIL" = false ] || exit 1
[ -n "$FAKE_GH_PERMISSION" ] || exit 0
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
    actor = inputs["PR_AUTHOR"] if inputs["EVENT"] == "pull_request" else inputs["WR_ACTOR"]
    expected_calls = (
        [
            "api",
            # old: f"repos/{inputs['REPO']}/collaborators/{inputs['WR_ACTOR']}/permission",
            f"repos/{inputs['REPO']}/collaborators/{actor}/permission",
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
        "pr-outsider-CONTRIBUTOR",
        "pr-contributor-read",
        "pr-lookup-fails",
        "pr-lookup-empty",
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
@pytest.mark.parametrize("case", ["pr-none-read", "pr-contributor-read"])
def test_authz_pr_decision_depends_on_write_permission(
    tmp_path: Path, name: str, case: str
) -> None:
    inputs, expected_code, reason, lookup = AUTHZ_DECISIONS[case]
    assert expected_code == 1
    assert lookup
    script = load(name)["jobs"]["authz"]["steps"][0]["run"]
    prefix, separator, pr_arm = script.partition("  pull_request)\n")
    assert separator
    permissions = "admin|maintain|write)"
    assert pr_arm.count(permissions) == 1
    mutated = prefix + separator + pr_arm.replace(permissions, "admin|maintain|write|read)", 1)
    assert mutated != script
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
    # old: changed = run_authz(tmp_path / "mutated", name, inputs,
    # old:                     lookup=lookup, script=mutated)
    changed = run_authz(tmp_path / "mutated", name, inputs, lookup=False, script=mutated)
    assert changed == (0, allowed_authz_summary(inputs), "")


def heavy_job_ids(jobs: dict[str, Any]) -> list[str]:
    return sorted(
        job_id for job_id, job in jobs.items() if job_id != "check" and "changes" in needs(job)
    )


def assert_cheap_pr_policy(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    assert {"authz", "changes", "workflow-lint", "check"} <= set(jobs)
    assert "env" not in workflow
    assert "defaults" not in workflow
    pull_request = workflow["on"]["pull_request"] or {}
    assert isinstance(pull_request, dict)
    assert not {"paths", "paths-ignore", "branches", "branches-ignore"} & set(pull_request)
    assert jobs["changes"] == canon_job("canon-changes-job.yml", "changes")
    assert jobs["workflow-lint"] == canon_job("canon-workflow-lint-job.yml", "workflow-lint")
    example = canon_job("canon-heavy-job-example.yml", "shard-a")
    for job_id in ("test", *RUNTIME_JOBS):
        assert jobs[job_id]["needs"] == example["needs"]
    assert needs(jobs["ready"]) == evidence.NEEDS | needs(example)
    assert jobs["ready"]["name"] == "release/ready"
    heavy = heavy_job_ids(jobs)
    assert heavy
    for job_id in heavy:
        assert needs(jobs[job_id]) >= needs(example)
        assert jobs[job_id].get("if") == example["if"]
        assert jobs[job_id].get("continue-on-error", "false") == "false"
    for job_id, job in jobs.items():
        assert "timeout-minutes" not in job
        if job_id != "check":
            assert re.search(r"\balways\s*\(", job.get("if", ""), re.IGNORECASE) is None
    expected = canon_job("canon-check-aggregate-job.yml", "check")
    expected["needs"] = [job_id for job_id in jobs if job_id != "check"]
    expected["steps"][-1]["env"]["HEAVY_JOBS"] = json.dumps(heavy, separators=(",", ":"))
    assert jobs["check"] == expected


def test_cheap_pr_jobs_match_the_canon_and_cover_every_dependency() -> None:
    assert_cheap_pr_policy(load("ci.yml"))


def test_documentation_tests_are_a_light_job_the_aggregate_requires() -> None:
    jobs = load("ci.yml")["jobs"]
    docs = jobs["docs-tests"]
    assert needs(docs) == {"authz"}
    # A light job carries no job-level condition: every step is guarded by authz
    # success, so it cannot be skipped and cannot use a status function (R7/R10).
    assert "if" not in docs
    assert docs["runs-on"] == "ubuntu-24.04"
    assert docs["env"]["PF_CI_LABELS"] == '["ubuntu-24.04"]'
    commands = "\n".join(step.get("run", "") for step in docs["steps"])
    assert "pytest tests/test_documentation.py" in commands
    assert "docs-tests" in needs(jobs["check"])


@pytest.mark.parametrize(
    "job_id,key,value",
    [
        ("changes", "needs", []),
        ("test", "needs", []),
        ("test", "needs", ["authz"]),
        ("ready", "needs", ["changes", "test", *RUNTIME_JOBS]),
        ("ready", "needs", ["authz", "changes", "test", *RUNTIME_JOBS, "extra"]),
        *[(job_id, "needs", ["changes"]) for job_id in RUNTIME_JOBS],
        *[(job_id, "needs", ["authz", "changes", "extra"]) for job_id in RUNTIME_JOBS],
        *[(job_id, "if", OLD_RUNTIME_IF) for job_id in RUNTIME_JOBS],
    ],
)
def test_cheap_pr_policy_rejects_missing_authz_and_extra_dependencies(
    job_id: str, key: str, value: Any
) -> None:
    workflow = copy.deepcopy(load("ci.yml"))
    assert_cheap_pr_policy(workflow)
    workflow["jobs"][job_id][key] = value
    with pytest.raises(AssertionError):
        assert_cheap_pr_policy(workflow)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-lint-dependency",
        "unlisted-heavy-job",
        "renamed-check",
        "named-check",
        "always-outside-check",
        "conditional-lint",
        "missing-lint",
        "missing-docs-tests-dependency",
        "duplicate-dependency",
        "workflow-env",
        "workflow-defaults",
        "paths-filter",
    ],
)
def test_cheap_pr_structure_rejects_unsafe_mutations(fault: str) -> None:
    original = load("ci.yml")
    assert_cheap_pr_policy(original)
    workflow = copy.deepcopy(original)
    jobs = workflow["jobs"]
    if fault == "missing-lint-dependency":
        jobs["check"]["needs"].remove("workflow-lint")
    elif fault == "unlisted-heavy-job":
        jobs["extra-heavy"] = canon_job("canon-heavy-job-example.yml", "shard-a")
        jobs["check"]["needs"].append("extra-heavy")
    elif fault == "renamed-check":
        jobs["aggregate"] = jobs.pop("check")
    elif fault == "named-check":
        jobs["check"]["name"] = "check"
    elif fault == "always-outside-check":
        jobs["authz"]["if"] = "${{ always() }}"
    elif fault == "conditional-lint":
        jobs["workflow-lint"]["if"] = "github.event_name == 'push'"
    elif fault == "missing-lint":
        del jobs["workflow-lint"]
    elif fault == "missing-docs-tests-dependency":
        jobs["check"]["needs"].remove("docs-tests")
    elif fault == "duplicate-dependency":
        jobs["check"]["needs"].append("authz")
    elif fault == "workflow-env":
        workflow["env"] = {"BASH_ENV": "untrusted.sh"}
    elif fault == "workflow-defaults":
        workflow["defaults"] = {"run": {"shell": "bash untrusted.sh {0}"}}
    else:
        workflow["on"]["pull_request"] = {"paths": ["src/**"]}
    assert workflow != original
    with pytest.raises(AssertionError):
        assert_cheap_pr_policy(workflow)


@pytest.mark.parametrize("job_id", ["changes", "check"])
def test_cheap_pr_structure_rejects_one_changed_classifier_byte(job_id: str) -> None:
    workflow = copy.deepcopy(load("ci.yml"))
    assert_cheap_pr_policy(workflow)
    step = next(s for s in workflow["jobs"][job_id]["steps"] if s.get("id") == "classify")
    script = step["run"]
    step["run"] = script.replace("code=true", "code=True", 1)
    assert sum(a != b for a, b in zip(script, step["run"], strict=True)) == 1
    with pytest.raises(AssertionError):
        assert_cheap_pr_policy(workflow)


@pytest.mark.parametrize("job_id", ["test", *RUNTIME_JOBS, "ready"])
@pytest.mark.parametrize(
    "condition",
    [
        None,
        OLD_RUNTIME_IF,
        "!cancelled() && needs.changes.outputs.code == 'true'",
        "${{ needs.changes.outputs.code == 'true' }}",
        "always()",
    ],
)
def test_every_heavy_job_requires_the_exact_canon_condition(
    job_id: str, condition: str | None
) -> None:
    workflow = copy.deepcopy(load("ci.yml"))
    assert_cheap_pr_policy(workflow)
    if condition is None:
        del workflow["jobs"][job_id]["if"]
    else:
        workflow["jobs"][job_id]["if"] = condition
    with pytest.raises(AssertionError):
        assert_cheap_pr_policy(workflow)


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
    assert aggregate["if"] == "needs.authz.result == 'success'"
    assert aggregate["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert aggregate["run"] == "python -m release_chain.evidence aggregate --path dist"
    assert len(steps) == 5
    assert [step["uses"] for step in steps[:3]] == [
        "actions/checkout@v7",
        "actions/setup-python@v6",
        "actions/download-artifact@v6",
    ]
    assert steps[-1] == {
        "uses": "actions/upload-artifact@v5",
        "if": "needs.authz.result == 'success'",
        "with": {
            "name": "release-evidence-${{ github.run_attempt }}",
            "path": "evidence.json",
            "if-no-files-found": "error",
        },
    }
    for step in steps:
        assert step["if"] == "needs.authz.result == 'success'"


REFUSE_STEP = {
    "name": "authz must have passed",
    "if": "needs.authz.result != 'success'",
    "run": "exit 1",
}


def assert_always_job_refuses_first(job: dict[str, Any]) -> None:
    canonical = canon_job("canon-check-aggregate-job.yml", "check")
    assert job["if"] == canonical["if"]
    assert "authz" in needs(job)
    steps = job["steps"]
    assert steps[0] == canonical["steps"][0], "the refusal is FIRST, before any checkout"
    assert_guarded_repo_steps(steps[1:])


def assert_guarded_repo_steps(steps: list[dict[str, Any]]) -> None:
    for step in steps:
        uses_repo = (
            "actions/checkout" in step.get("uses", "")
            or step.get("uses", "").startswith("./")
            or "run" in step
        )
        if uses_repo:
            assert "needs.authz.result == 'success'" in step.get("if", ""), step


def assert_runtime_job_guards(job: dict[str, Any]) -> None:
    example = canon_job("canon-heavy-job-example.yml", "shard-a")
    assert job["if"] == example["if"]
    assert needs(job) >= needs(example)
    steps = job["steps"]
    assert steps[0] == REFUSE_STEP
    assert_guarded_repo_steps(steps[1:])


def test_always_jobs_refuse_a_failed_authz_before_any_repo_code() -> None:
    ci = load("ci.yml")["jobs"]
    assert_always_job_refuses_first(ci["check"])
    for name in RUNTIME_JOBS:
        assert_runtime_job_guards(ci[name])
    assert_guarded_repo_steps(ci["ready"]["steps"])
    for workflow in PR_TIER_WORKFLOWS:
        for job_id, job in load(workflow)["jobs"].items():
            condition = job.get("if", "")
            if job_id != "check" and ("always()" in condition or "!cancelled()" in condition):
                assert "needs.authz.result == 'success'" in condition, (workflow, job_id)


def assert_aggregate_is_always(jobs: dict[str, Any]) -> None:
    assert jobs["check"]["if"] == canon_job("canon-check-aggregate-job.yml", "check")["if"]
    for job_id, job in jobs.items():
        condition = job.get("if", "")
        if job_id == "check":
            continue
        # No other job may be a bare `!cancelled()` aggregate that a cancelled run skips.
        assert condition.replace(" ", "") not in ("!cancelled()", "${{!cancelled()}}"), job_id
        assert re.search(r"\balways\s*\(", condition, re.IGNORECASE) is None, job_id


def test_the_required_aggregate_runs_always_never_cancelled_only() -> None:
    assert_aggregate_is_always(load("ci.yml")["jobs"])


@pytest.mark.parametrize("fault", ["${{ !cancelled() }}", "!cancelled()", "success()", ""])
def test_an_aggregate_that_a_cancelled_run_can_skip_is_rejected(fault: str) -> None:
    jobs = copy.deepcopy(load("ci.yml")["jobs"])
    assert_aggregate_is_always(jobs)
    jobs["check"]["if"] = fault
    with pytest.raises(AssertionError):
        assert_aggregate_is_always(jobs)


@pytest.mark.parametrize(
    "fault", ["cancelled-only", "no-refusal", "refusal-not-first", "unguarded-checkout"]
)
def test_always_job_rejects_missing_first_refusal_or_unguarded_repo_code(fault: str) -> None:
    original = load("ci.yml")["jobs"]["check"]
    assert_always_job_refuses_first(original)
    job = copy.deepcopy(original)
    if fault == "cancelled-only":
        job["if"] = "${{ !cancelled() }}"
    elif fault == "no-refusal":
        del job["steps"][0]
    elif fault == "refusal-not-first":
        job["steps"].insert(1, job["steps"].pop(0))
    else:
        checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
        del checkout["if"]
    assert job != original
    with pytest.raises(AssertionError):
        assert_always_job_refuses_first(job)


@pytest.mark.parametrize("job_id", RUNTIME_JOBS)
@pytest.mark.parametrize("fault", ["no-refusal", "unguarded-run"])
def test_runtime_jobs_reject_missing_refusal_or_unguarded_run(job_id: str, fault: str) -> None:
    original = load("ci.yml")["jobs"][job_id]
    assert_runtime_job_guards(original)
    job = copy.deepcopy(original)
    if fault == "no-refusal":
        del job["steps"][0]
    else:
        run = next(step for step in job["steps"][1:] if "run" in step)
        del run["if"]
    assert job != original
    with pytest.raises(AssertionError):
        assert_runtime_job_guards(job)


def test_ready_rejects_an_unguarded_repository_step() -> None:
    job = copy.deepcopy(load("ci.yml")["jobs"]["ready"])
    assert_guarded_repo_steps(job["steps"])
    run = next(step for step in job["steps"] if "run" in step)
    del run["if"]
    with pytest.raises(AssertionError):
        assert_guarded_repo_steps(job["steps"])


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_job_level_always_is_reserved_for_check(path: Path) -> None:
    for job_id, job in load(path.name)["jobs"].items():
        if job_id != "check":
            assert re.search(r"\balways\s*\(", job.get("if", ""), re.IGNORECASE) is None


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


def classify(
    tmp_path: Path,
    changed: dict[str, str | None],
    job_id: str,
    *,
    kind: str = "regular",
    event: str = "pull_request",
    sha_fault: str = "",
) -> tuple[str, str, str]:
    """Run the actual classifier on base/head commits, including Git file modes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "core.filemode", "true")
    for name in ("README.md", "src/module.py", "docs/index.md", "docs/a.md"):
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
    if kind == "executable":
        (repo / next(iter(changed))).chmod(0o755)
    elif kind == "symlink":
        path = repo / next(iter(changed))
        path.unlink()
        path.symlink_to("missing-target")
    elif kind == "gitlink":
        (repo / next(iter(changed))).unlink()
    git(repo, "add", "-A")
    if kind == "gitlink":
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},{next(iter(changed))}")
        # The canon's explicit --ignore-submodules=none must override this setting.
        git(repo, "config", "diff.ignoreSubmodules", "all")
    git(repo, "commit", "-q", "--allow-empty", "-m", "change")
    head = git(repo, "rev-parse", "HEAD")
    output = tmp_path / "github-output"
    environment = {
        **os.environ,
        "EVENT": event,
        "PR_BASE": base,
        "PR_HEAD": head,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
    }
    if sha_fault == "invalid-base":
        environment["PR_BASE"] = "not-a-sha"
    elif sha_fault == "missing-base":
        environment["PR_BASE"] = "0" * 40
    elif sha_fault == "missing-head":
        environment["PR_HEAD"] = "0" * 40
    elif sha_fault == "invalid-head":
        environment["PR_HEAD"] = "not-a-sha"
    elif sha_fault == "head-mismatch":
        environment["PR_HEAD"] = base
    subprocess.run(
        ["bash", "-e", "-c", step_script("ci.yml", job_id, "classify")],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(outputs) == set(canon_job("canon-changes-job.yml", "changes")["outputs"])
    return outputs["docs_only"], outputs["workflows_only"], outputs["code"]


def label(changed: dict[str, str | None]) -> str:
    return "+".join(sorted(changed))


DOCS_ONLY: list[dict[str, str | None]] = [
    {"README.md": "x"},
    {"CONTRIBUTING.md": "x"},
    {"CHANGELOG.md": "x"},
    {"CODE_OF_CONDUCT.md": "x"},
    {"SECURITY.md": "x"},
    {"LICENSE": "x"},
    {"LICENSE.md": "x"},
    {"LICENSE.txt": "x"},
    {"docs/a.md": "x"},
    {"docs/index.md": "x"},
    {"docs/a.rst": "x"},
    {"docs/a.txt": "x"},
    {"docs/nested dir/with space.md": "x"},
    {"docs/line\nbreak.md": "x"},
    {"README.md": "x", "docs/index.md": "y"},
    {"docs/a.md": None},
]
WORKFLOWS_ONLY: list[dict[str, str | None]] = [
    {".github/workflows/x.yml": "x"},
    {".github/workflows/x.yaml": "x"},
    {".github/workflows/x.yml": "x", "docs/a.md": "y"},
    {".github/workflows/x.yaml": "x", "README.md": "y", "docs/a.txt": "z"},
]
CODE_CHANGES: list[dict[str, str | None]] = [
    {},
    {"src/module.py": "x"},
    {"pyproject.toml": "x"},
    {"mkdocs.yml": "x"},
    {"README.md": "x", "docs/index.md": "y", "mkdocs.yml": "z"},
    {".github/workflows/sub/x.yml": "x"},
    {".github/actionlint.yaml": "x"},
    {".github/workflows/x.yml": "x", "src/x.py": "y"},
    {"tests/test_pr_tier_workflows.py": "x"},
    {"tests/fixtures/CORPUS.md": "x"},
    {"native/feed/README.md": "x"},
    {"src/README.md": "x"},
    {"docs/a.MD": "x"},
    {"docs/run.sh": "x"},
    {"docs.py": "x"},
    {"docs-extra/page.md": "x"},
    {"README.md.bak": "x"},
    {"docs/index.md": "x", "src/module.py": "y"},
    # A pure rename of code into docs/ is detected as a rename by default; --no-renames keeps
    # the code path in the list.
    {"src/module.py": None, "docs/module.py": "base\n"},
    {"docs/a.md": None, "src/a.md": "base\n"},
]


@pytest.mark.parametrize("job_id", ["changes", "check"])
@pytest.mark.parametrize("changed", DOCS_ONLY, ids=label)
def test_docs_only_changes_are_classified_as_docs(
    tmp_path: Path, changed: dict[str, str | None], job_id: str
) -> None:
    assert classify(tmp_path, changed, job_id) == ("true", "false", "false")


@pytest.mark.parametrize("job_id", ["changes", "check"])
@pytest.mark.parametrize("changed", WORKFLOWS_ONLY, ids=label)
def test_workflow_and_documentation_changes_are_classified_as_workflows(
    tmp_path: Path, changed: dict[str, str | None], job_id: str
) -> None:
    assert classify(tmp_path, changed, job_id) == ("false", "true", "false")


@pytest.mark.parametrize("job_id", ["changes", "check"])
@pytest.mark.parametrize("changed", CODE_CHANGES, ids=label)
def test_every_other_path_is_classified_as_code(
    tmp_path: Path, changed: dict[str, str | None], job_id: str
) -> None:
    assert classify(tmp_path, changed, job_id) == ("false", "false", "true")


@pytest.mark.parametrize("job_id", ["changes", "check"])
@pytest.mark.parametrize("kind", ["executable", "symlink", "gitlink"])
@pytest.mark.parametrize("path", ["docs/a.md", ".github/workflows/x.yml"])
def test_nonregular_or_executable_files_are_always_code(
    tmp_path: Path, job_id: str, kind: str, path: str
) -> None:
    assert classify(tmp_path, {path: "x"}, job_id, kind=kind) == ("false", "false", "true")


@pytest.mark.parametrize("job_id", ["changes", "check"])
@pytest.mark.parametrize(
    "event",
    ["push", "workflow_dispatch", "schedule", "merge_group", "release", "workflow_call"],
)
def test_non_pr_events_run_full_suites_even_for_documentation(
    tmp_path: Path, job_id: str, event: str
) -> None:
    assert classify(tmp_path, {"docs/a.md": "x"}, job_id, event=event) == (
        "false",
        "false",
        "true",
    )


@pytest.mark.parametrize("job_id", ["changes", "check"])
@pytest.mark.parametrize(
    "fault,expected",
    [
        ("invalid-base", None),
        ("missing-base", ("false", "false", "true")),
        ("missing-head", ("false", "false", "true")),
        ("invalid-head", None),
        ("head-mismatch", None),
    ],
)
def test_classification_fails_closed_for_invalid_or_missing_objects(
    tmp_path: Path, job_id: str, fault: str, expected: tuple[str, str, str] | None
) -> None:
    if expected is None:
        with pytest.raises(subprocess.CalledProcessError):
            classify(tmp_path, {"docs/a.md": "x"}, job_id, sha_fault=fault)
    else:
        assert classify(tmp_path, {"docs/a.md": "x"}, job_id, sha_fault=fault) == expected


def gate_inputs(
    results: dict[str, str] | None = None,
    *,
    docs_only: str = "false",
    workflows_only: str = "false",
) -> dict[str, str]:
    job = load("ci.yml")["jobs"]["check"]
    outcomes = {job_id: {"result": "success"} for job_id in job["needs"]}
    for job_id, result in (results or {}).items():
        outcomes[job_id] = {"result": result}
    return {
        "NEEDS": json.dumps(outcomes),
        "HEAVY_JOBS": job["steps"][-1]["env"]["HEAVY_JOBS"],
        "DOCS_ONLY": docs_only,
        "WORKFLOWS_ONLY": workflows_only,
    }


def run_gate(tmp_path: Path, inputs: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = load("ci.yml")["jobs"]["check"]["steps"][-1]["run"]
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env={**os.environ, **inputs, "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "docs,workflows", [("false", "false"), ("true", "false"), ("false", "true")]
)
def test_aggregate_accepts_every_needed_job_succeeding(
    tmp_path: Path, docs: str, workflows: str
) -> None:
    result = run_gate(tmp_path, gate_inputs(docs_only=docs, workflows_only=workflows))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("skipped", ["all", *heavy_job_ids(load("ci.yml")["jobs"])])
@pytest.mark.parametrize(
    "docs,workflows,expected",
    [
        ("true", "false", 0),
        ("false", "true", 0),
        ("false", "false", 1),
        ("true", "true", 1),
        ("", "false", 1),
        ("maybe", "false", 1),
        ("false", "", 1),
        ("false", "maybe", 1),
    ],
)
def test_heavy_skips_require_exactly_one_independent_cheap_proof(
    tmp_path: Path, skipped: str, docs: str, workflows: str, expected: int
) -> None:
    heavy = heavy_job_ids(load("ci.yml")["jobs"])
    results = dict.fromkeys(heavy if skipped == "all" else [skipped], "skipped")
    result = run_gate(tmp_path, gate_inputs(results, docs_only=docs, workflows_only=workflows))
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize("job_id", load("ci.yml")["jobs"]["check"]["needs"])
@pytest.mark.parametrize("status", ["failure", "cancelled", "", "unknown"])
@pytest.mark.parametrize(
    "docs,workflows", [("false", "false"), ("true", "false"), ("false", "true")]
)
def test_aggregate_rejects_every_failed_cancelled_or_invalid_needed_result(
    tmp_path: Path, job_id: str, status: str, docs: str, workflows: str
) -> None:
    result = run_gate(
        tmp_path,
        gate_inputs({job_id: status}, docs_only=docs, workflows_only=workflows),
    )
    assert result.returncode != 0


@pytest.mark.parametrize("job_id", ["authz", "changes", "workflow-lint", "extra-light"])
@pytest.mark.parametrize("status", ["skipped", "failure", "cancelled"])
@pytest.mark.parametrize("docs,workflows", [("true", "false"), ("false", "true")])
def test_light_jobs_must_succeed_even_when_all_heavy_jobs_are_skipped(
    tmp_path: Path, job_id: str, status: str, docs: str, workflows: str
) -> None:
    results = dict.fromkeys(heavy_job_ids(load("ci.yml")["jobs"]), "skipped")
    results[job_id] = status
    result = run_gate(tmp_path, gate_inputs(results, docs_only=docs, workflows_only=workflows))
    assert result.returncode != 0


@pytest.mark.parametrize(
    "job_id",
    ["authz", "changes", "workflow-lint", *heavy_job_ids(load("ci.yml")["jobs"])],
)
def test_gate_rejects_a_missing_needed_job(tmp_path: Path, job_id: str) -> None:
    inputs = gate_inputs()
    outcomes = json.loads(inputs["NEEDS"])
    del outcomes[job_id]
    inputs["NEEDS"] = json.dumps(outcomes)
    assert run_gate(tmp_path, inputs).returncode != 0


@pytest.mark.parametrize("heavy_json", ["[]", '["unlisted"]', "{}", "null", "invalid"])
def test_gate_rejects_an_invalid_heavy_inventory(tmp_path: Path, heavy_json: str) -> None:
    inputs = gate_inputs()
    inputs["HEAVY_JOBS"] = heavy_json
    assert run_gate(tmp_path, inputs).returncode != 0


def test_gate_does_not_trust_the_changes_jobs_class_outputs(tmp_path: Path) -> None:
    inputs = gate_inputs(dict.fromkeys(heavy_job_ids(load("ci.yml")["jobs"]), "skipped"))
    outcomes = json.loads(inputs["NEEDS"])
    outcomes["changes"]["outputs"] = {
        "docs_only": "true",
        "workflows_only": "false",
        "code": "false",
    }
    inputs["NEEDS"] = json.dumps(outcomes)
    assert run_gate(tmp_path, inputs).returncode != 0


def test_check_refusal_script_exits_before_checkout(tmp_path: Path) -> None:
    job = load("ci.yml")["jobs"]["check"]
    assert_always_job_refuses_first(job)
    result = subprocess.run(
        ["bash", "-e", "-c", job["steps"][0]["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "authz did not pass" in result.stdout
