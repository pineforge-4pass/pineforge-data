"""Pin release authorization to the org canon and exercise its actual shell decisions."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
RELEASE_WORKFLOWS = ("pineforge-release.yml", "release.yml")
REPOSITORY = "pineforge-4pass/pineforge-data"
FORK_REPOSITORY = "outside/pineforge-data"
ALLOWED_BOTS = "dependabot[bot] pineforge-app[bot]"
AUTHZ_PASSED = "needs.authz.result == 'success'"
AUTHZ_REFUSED = "needs.authz.result != 'success'"

# Literal transcription of ci-labels.md, frozen sha ac875205.
# Full-line comments are omitted; adjacent literals only split long source lines.
CANON_SCRIPT = (
    'set -eu\n'
    'fail() { echo "::error title=authz::$1"; echo "authz: REFUSED: $1" '
    '>> "$GITHUB_STEP_SUMMARY"; exit 1; }\n'
    'case "$EVENT" in\n'
    '  push|workflow_dispatch|schedule|merge_group|release|workflow_call|'
    'repository_dispatch)\n'
    '    ;;\n'
    '  workflow_run)\n'
    '    [ -n "${WR_HEAD_REPO:-}" ] || fail "workflow_run head repository is missing"\n'
    '    [ -n "${WR_ACTOR:-}" ] || fail "workflow_run triggering actor is missing"\n'
    '    [ "$WR_HEAD_REPO" = "$REPO" ] || fail '
    '"workflow_run head repository does not match this repository"\n'
    '    case "$WR_ACTOR" in\n'
    '      "pineforge-app[bot]"|"pineforge-ci-dispatcher[bot]") ;;\n'
    '      *"[bot]") fail "workflow_run bot is not on the canon allowlist" ;;\n'
    '      *)\n'
    '        perm=$(gh api "repos/$REPO/collaborators/$WR_ACTOR/permission" '
    '--jq .permission 2>/dev/null) || fail "workflow_run permission lookup failed"\n'
    '        case "$perm" in admin|maintain|write) ;; *) fail '
    '"workflow_run triggering actor does not have write permission" ;; esac ;;\n'
    '    esac ;;\n'
    '  pull_request)\n'
    '    [ "$HEAD_REPO" = "$REPO" ] || fail '
    '"pull request from another repository (${HEAD_REPO:-unknown})"\n'
    '    [ "$HEAD_IS_FORK" = "false" ] || fail "pull request from a fork"\n'
    '    case "$ASSOCIATION" in\n'
    '      OWNER|MEMBER|COLLABORATOR) ;;\n'
    '      *)\n'
    '        case " $ALLOWED_BOTS " in\n'
    '          *" $PR_AUTHOR "*) ;;\n'
    '          *)\n'
    '            perm=$(gh api "repos/$REPO/collaborators/$PR_AUTHOR/permission" '
    '--jq .permission 2>/dev/null || echo none)\n'
    '            case "$perm" in admin|maintain|write) ;; *) fail '
    '"author ${PR_AUTHOR:-unknown} (${ASSOCIATION:-NONE}) has permission '
    "'$perm', not write, and is not an allowlisted bot\" ;; esac ;;\n"
    '        esac ;;\n'
    '    esac ;;\n'
    '  *) fail "event $EVENT does not run CI '
    '(pull_request_target and other events are not allowed)" ;;\n'
    'esac\n'
    'echo "authz: allowed ($EVENT${PR_AUTHOR:+ by $PR_AUTHOR})" >> "$GITHUB_STEP_SUMMARY"\n'
)

VERIFY_CONDITION = (
    f"always() && {AUTHZ_PASSED} && needs.prepare.outputs.eligible == 'true' && "
    "needs.github-release.result == 'success' && "
    "(needs.publish.result == 'success' || needs.prepare.outputs.published == 'true') && "
    "!inputs.claim_placeholder"
)

FAKE_GH = """#!/bin/bash
set -eu
printf '%s\\n' "$@" >> "$GH_CALLS"
[ "$#" -eq 4 ] || exit 97
[ "$1" = "api" ] || exit 97
case "$EVENT" in
  workflow_run) login=$WR_ACTOR ;;
  pull_request) login=$PR_AUTHOR ;;
  *) exit 97 ;;
esac
[ "$2" = "repos/$REPO/collaborators/$login/permission" ] || exit 97
[ "$3" = "--jq" ] || exit 97
[ "$4" = ".permission" ] || exit 97
[ "$GH_FAIL" = "0" ] || exit 1
[ "$GH_EMPTY" = "0" ] || exit 0
printf '%s\\n' "$GH_PERMISSION"
"""


def load_text(text: str) -> dict[str, Any]:
    loaded = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def load(name: str) -> dict[str, Any]:
    return load_text((WORKFLOWS / name).read_text())


def normalized_script(script: str) -> str:
    return "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def assert_workflow_authz(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    assert next(iter(jobs)) == "authz", "authz must be the first job"
    authz = jobs["authz"]
    assert set(authz) == {"name", "runs-on", "permissions", "steps"}
    assert authz["name"] == "authz"
    assert authz["runs-on"] == ["self-hosted", "pf-ci", "pf-ci-x64"]
    assert authz["permissions"] == {}
    assert len(authz["steps"]) == 1
    step = authz["steps"][0]
    assert set(step) == {"name", "env", "run"}
    assert step["name"] == "Only our own pull requests and our own events run CI"
    assert step["env"]["ALLOWED_BOTS"] == ALLOWED_BOTS, "authz: bot list"
    assert step["env"] == {
        "EVENT": "${{ github.event_name }}",
        "REPO": "${{ github.repository }}",
        "HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
        "HEAD_IS_FORK": "${{ github.event.pull_request.head.repo.fork }}",
        "ASSOCIATION": "${{ github.event.pull_request.author_association }}",
        "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
        "WR_HEAD_REPO": "${{ github.event.workflow_run.head_repository.full_name }}",
        "WR_ACTOR": "${{ github.event.workflow_run.triggering_actor.login }}",
        "GH_TOKEN": "${{ github.token }}",
        "ALLOWED_BOTS": ALLOWED_BOTS,
    }
    assert normalized_script(step["run"]) == normalized_script(CANON_SCRIPT)
    for job_id, job in jobs.items():
        if job_id == "authz":
            continue
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "authz" in needs, f"{job_id}: needs authz"

    report_tail = (
        "(needs.reconcile.result != 'skipped' || needs.reconcile-pr.result != 'skipped')"
        if "reconcile" in jobs
        else "!inputs.claim_placeholder"
    )
    report = jobs["report"]
    expected_if = f"always() && {AUTHZ_PASSED} && {report_tail}"
    assert " ".join(report["if"].split()) == expected_if, "report: authz condition"
    assert report["uses"] == "./.github/workflows/release-report.yml"
    assert report["secrets"] == "inherit"

    if "verify" in jobs:
        verify = jobs["verify"]
        assert " ".join(verify["if"].split()) == VERIFY_CONDITION, "verify: authz condition"
        steps = verify["steps"]
        assert steps[0] == {
            "name": "authz must have passed",
            "if": AUTHZ_REFUSED,
            "run": "exit 1",
        }, "verify: refuse first"
        assert any(step.get("uses", "").startswith("actions/checkout@") for step in steps[1:])
        assert any("release_chain.publishing verify" in step.get("run", "") for step in steps[1:])
        for step in steps[1:]:
            uses = step.get("uses", "")
            if "run" in step or uses.startswith(("actions/checkout@", "./")):
                assert step.get("if") == AUTHZ_PASSED, "verify: repository step guard"
            if uses.startswith("actions/upload-artifact@"):
                assert step["if"] == "always()"


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
def test_release_workflows_use_the_canonical_authz(name: str) -> None:
    assert_workflow_authz(load(name))


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
@pytest.mark.parametrize("mutation", ("missing-dependency", "unguarded-report", "wrong-bots"))
def test_structure_checker_rejects_unsafe_mutations(name: str, mutation: str) -> None:
    original = (WORKFLOWS / name).read_text()
    assert_workflow_authz(load_text(original))
    if mutation == "missing-dependency":
        before, after = "    needs: authz\n", ""
        finding = "needs authz"
    elif mutation == "unguarded-report":
        before = f"  report:\n    if: always() && {AUTHZ_PASSED} && "
        after = "  report:\n    if: always() && "
        finding = "report: authz condition"
    else:
        before = f'ALLOWED_BOTS: "{ALLOWED_BOTS}"'
        after = 'ALLOWED_BOTS: "dependabot[bot]"'
        finding = "authz: bot list"
    assert before in original
    mutated = original.replace(before, after, 1)
    assert mutated != original
    with pytest.raises(AssertionError, match=finding):
        assert_workflow_authz(load_text(mutated))


@pytest.mark.parametrize("mutation", ("condition", "refuse-first", "checkout", "run"))
def test_structure_checker_rejects_unguarded_verify(mutation: str) -> None:
    original = (WORKFLOWS / "release.yml").read_text()
    assert_workflow_authz(load_text(original))
    if mutation == "condition":
        before = f"always() && {AUTHZ_PASSED} && needs.prepare.outputs.eligible"
        after = "always() && needs.prepare.outputs.eligible"
        finding = "verify: authz condition"
    elif mutation == "refuse-first":
        before = (
            "      - name: authz must have passed\n"
            f"        if: {AUTHZ_REFUSED}\n"
            "        run: exit 1\n"
        )
        after = ""
        finding = "verify: refuse first"
    elif mutation == "checkout":
        before = f"      - uses: actions/checkout@v7\n        if: {AUTHZ_PASSED}\n"
        after = "      - uses: actions/checkout@v7\n"
        finding = "verify: repository step guard"
    else:
        title = (
            "      - name: Verify PyPI inventory and downloaded bytes "
            "before reporting success\n"
        )
        before = f"{title}        if: {AUTHZ_PASSED}\n"
        after = title
        finding = "verify: repository step guard"
    assert original.count(before) == 1
    mutated = original.replace(before, after, 1)
    assert mutated != original
    with pytest.raises(AssertionError, match=finding):
        assert_workflow_authz(load_text(mutated))


@dataclass(frozen=True)
class Case:
    event: str = "workflow_run"
    actor: str = "outsider"
    head_repo: str = REPOSITORY
    permission: str = "none"
    gh_fails: bool = False
    lookup: bool = False
    refusal: str = ""
    pr_author: str = ""
    association: str = ""
    head_is_fork: str = "false"
    gh_empty: bool = False


def actor_refusal() -> str:
    return "workflow_run triggering actor does not have write permission"


def author_refusal(author: str, association: str, permission: str = "none") -> str:
    return (
        f"author {author} ({association}) has permission '{permission}', "
        "not write, and is not an allowlisted bot"
    )


FORK_REFUSAL = "workflow_run head repository does not match this repository"
EVENT_REFUSAL = "does not run CI (pull_request_target and other events are not allowed)"
CASES = {
    "same-repo-dependabot": Case(
        actor="dependabot[bot]", refusal="workflow_run bot is not on the canon allowlist"
    ),
    "same-repo-app": Case(actor="pineforge-app[bot]"),
    "same-repo-dispatcher": Case(actor="pineforge-ci-dispatcher[bot]"),
    "same-repo-write": Case(actor="writer", permission="write", lookup=True),
    "same-repo-admin": Case(actor="admin", permission="admin", lookup=True),
    "same-repo-maintain": Case(actor="maintainer", permission="maintain", lookup=True),
    "push": Case(event="push"),
    "workflow-dispatch": Case(event="workflow_dispatch"),
    "repository-dispatch": Case(event="repository_dispatch"),
    "fork-dependabot": Case(
        actor="dependabot[bot]", head_repo=FORK_REPOSITORY, refusal=FORK_REFUSAL
    ),
    "fork-app": Case(
        actor="pineforge-app[bot]", head_repo=FORK_REPOSITORY, refusal=FORK_REFUSAL
    ),
    "fork-writer": Case(
        actor="writer", head_repo=FORK_REPOSITORY, permission="write", refusal=FORK_REFUSAL
    ),
    "unknown-none": Case(lookup=True, refusal=actor_refusal()),
    "unknown-read": Case(permission="read", lookup=True, refusal=actor_refusal()),
    "unknown-triage": Case(permission="triage", lookup=True, refusal=actor_refusal()),
    "lookup-failed": Case(
        permission="write",
        gh_fails=True,
        lookup=True,
        refusal="workflow_run permission lookup failed",
    ),
    "empty-head": Case(
        actor="pineforge-app[bot]",
        head_repo="",
        refusal="workflow_run head repository is missing",
    ),
    "empty-actor": Case(actor="", refusal="workflow_run triggering actor is missing"),
    "pull-request-target": Case(
        event="pull_request_target", refusal=f"event pull_request_target {EVENT_REFUSAL}"
    ),
    "unknown-event": Case(event="unknown_event", refusal=f"event unknown_event {EVENT_REFUSAL}"),
    "other-bot": Case(
        actor="github-actions[bot]", refusal="workflow_run bot is not on the canon allowlist"
    ),
    "app-without-bot-suffix": Case(
        actor="pineforge-app", lookup=True, refusal=actor_refusal()
    ),
    "app-with-extra-suffix": Case(
        actor="pineforge-app[bot]x", lookup=True, refusal=actor_refusal()
    ),
    "dependabot-with-prefix": Case(
        actor="xdependabot[bot]", refusal="workflow_run bot is not on the canon allowlist"
    ),
    "pr-owner": Case(event="pull_request", pr_author="owner", association="OWNER"),
    "pr-member": Case(event="pull_request", pr_author="member", association="MEMBER"),
    "pr-collaborator": Case(
        event="pull_request", pr_author="collaborator", association="COLLABORATOR"
    ),
    "pr-dependabot": Case(
        event="pull_request", pr_author="dependabot[bot]", association="NONE"
    ),
    "pr-app": Case(event="pull_request", pr_author="pineforge-app[bot]", association="NONE"),
    "pr-contributor-write": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        permission="write",
        lookup=True,
    ),
    "pr-contributor-admin": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        permission="admin",
        lookup=True,
    ),
    "pr-contributor-maintain": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        permission="maintain",
        lookup=True,
    ),
    "pr-contributor-none": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        lookup=True,
        refusal=author_refusal("contributor", "CONTRIBUTOR"),
    ),
    "pr-contributor-read": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        permission="read",
        lookup=True,
        refusal=author_refusal("contributor", "CONTRIBUTOR", "read"),
    ),
    "pr-contributor-triage": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        permission="triage",
        lookup=True,
        refusal=author_refusal("contributor", "CONTRIBUTOR", "triage"),
    ),
    "pr-none-none": Case(
        event="pull_request",
        pr_author="outsider",
        association="NONE",
        lookup=True,
        refusal=author_refusal("outsider", "NONE"),
    ),
    "pr-none-read": Case(
        event="pull_request",
        pr_author="outsider",
        association="NONE",
        permission="read",
        lookup=True,
        refusal=author_refusal("outsider", "NONE", "read"),
    ),
    "pr-lookup-failed": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        permission="write",
        gh_fails=True,
        lookup=True,
        refusal=author_refusal("contributor", "CONTRIBUTOR"),
    ),
    "pr-lookup-empty": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        permission="write",
        gh_empty=True,
        lookup=True,
        refusal=author_refusal("contributor", "CONTRIBUTOR", ""),
    ),
    "pr-other-bot": Case(
        event="pull_request",
        pr_author="github-actions[bot]",
        association="NONE",
        lookup=True,
        refusal=author_refusal("github-actions[bot]", "NONE"),
    ),
    "pr-fork-writer": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        head_is_fork="true",
        permission="write",
        refusal="pull request from a fork",
    ),
    "pr-fork-app": Case(
        event="pull_request",
        pr_author="pineforge-app[bot]",
        association="NONE",
        head_is_fork="true",
        refusal="pull request from a fork",
    ),
    "pr-other-repo-writer": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        head_repo=FORK_REPOSITORY,
        permission="write",
        refusal=f"pull request from another repository ({FORK_REPOSITORY})",
    ),
    "pr-other-repo-fork-writer": Case(
        event="pull_request",
        pr_author="contributor",
        association="CONTRIBUTOR",
        head_repo=FORK_REPOSITORY,
        head_is_fork="true",
        permission="write",
        refusal=f"pull request from another repository ({FORK_REPOSITORY})",
    ),
}


def run_authz(
    tmp_path: Path, name: str, case: Case, script: str | None = None
) -> tuple[int, str, list[str]]:
    step = load(name)["jobs"]["authz"]["steps"][0]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(0o755)
    summary = tmp_path / "summary"
    summary.write_text("")
    calls = tmp_path / "gh-calls"
    calls.write_text("")
    is_pull_request = case.event == "pull_request"
    environment = {
        "PATH": str(bin_dir),
        "EVENT": case.event,
        "REPO": REPOSITORY,
        "HEAD_REPO": case.head_repo if is_pull_request else "",
        "HEAD_IS_FORK": case.head_is_fork if is_pull_request else "",
        "ASSOCIATION": case.association,
        "PR_AUTHOR": case.pr_author,
        "WR_HEAD_REPO": "" if is_pull_request else case.head_repo,
        "WR_ACTOR": case.actor,
        "ALLOWED_BOTS": step["env"]["ALLOWED_BOTS"],
        "GH_TOKEN": "fake-token",
        "GH_PERMISSION": case.permission,
        "GH_FAIL": "1" if case.gh_fails else "0",
        "GH_EMPTY": "1" if case.gh_empty else "0",
        "GH_CALLS": str(calls),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    completed = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", step["run"] if script is None else script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode, summary.read_text(), calls.read_text().splitlines()


def allowed_summary(case: Case) -> str:
    author = f" by {case.pr_author}" if case.pr_author else ""
    return f"authz: allowed ({case.event}{author})\n"


def assert_decision(result: tuple[int, str, list[str]], case: Case) -> None:
    code, summary, calls = result
    if case.refusal:
        assert code == 1, (code, summary)
        assert summary == f"authz: REFUSED: {case.refusal}\n"
    else:
        assert code == 0, (code, summary)
        assert summary == allowed_summary(case)
    login = case.pr_author if case.event == "pull_request" else case.actor
    expected_calls = (
        ["api", f"repos/{REPOSITORY}/collaborators/{login}/permission", "--jq", ".permission"]
        if case.lookup
        else []
    )
    assert calls == expected_calls


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
@pytest.mark.parametrize("case_name", list(CASES))
def test_authz_script_decisions(tmp_path: Path, name: str, case_name: str) -> None:
    case = CASES[case_name]
    assert_decision(run_authz(tmp_path, name, case), case)


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
@pytest.mark.parametrize(
    "case_name",
    (
        "fork-dependabot",
        "fork-app",
        "fork-writer",
        "unknown-none",
        "unknown-read",
        "unknown-triage",
        "lookup-failed",
        "pr-contributor-none",
    ),
)
def test_noop_fail_mutation_allows_refused_runs(
    tmp_path: Path, name: str, case_name: str
) -> None:
    script = load(name)["jobs"]["authz"]["steps"][0]["run"]
    fail_function = CANON_SCRIPT.splitlines()[1]
    assert script.count(fail_function) == 1
    mutated = script.replace(fail_function, "fail() { :; }", 1)
    assert mutated != script
    case = CASES[case_name]
    assert_decision(run_authz(tmp_path, name, case, script), case)
    code, summary, _calls = run_authz(tmp_path, name, case, mutated)
    assert code == 0
    assert summary == allowed_summary(case)


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
@pytest.mark.parametrize("case_name", ("pr-contributor-read", "pr-none-read"))
def test_read_permission_mutation_allows_refused_prs(
    tmp_path: Path, name: str, case_name: str
) -> None:
    script = load(name)["jobs"]["authz"]["steps"][0]["run"]
    before = 'case "$perm" in admin|maintain|write) ;; *) fail "author '
    after = 'case "$perm" in admin|maintain|write|read) ;; *) fail "author '
    assert script.count(before) == 1
    mutated = script.replace(before, after, 1)
    assert mutated != script
    case = CASES[case_name]
    assert_decision(run_authz(tmp_path, name, case, script), case)
    result = run_authz(tmp_path, name, case, mutated)
    code, summary, calls = result
    assert code == 0
    assert summary == allowed_summary(case)
    assert calls == [
        "api",
        f"repos/{REPOSITORY}/collaborators/{case.pr_author}/permission",
        "--jq",
        ".permission",
    ]
    with pytest.raises(AssertionError):
        assert_decision(result, case)


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
@pytest.mark.parametrize("case_name", ("fork-writer",))
def test_missing_head_check_mutation_allows_fork_writer(
    tmp_path: Path, name: str, case_name: str
) -> None:
    script = load(name)["jobs"]["authz"]["steps"][0]["run"]
    head_check = next(
        line for line in CANON_SCRIPT.splitlines() if line.lstrip().startswith('[ "$WR_HEAD_REPO"')
    )
    assert script.count(head_check + "\n") == 1
    mutated = script.replace(head_check + "\n", "", 1)
    assert mutated != script
    case = CASES[case_name]
    assert_decision(run_authz(tmp_path, name, case, script), case)
    code, summary, calls = run_authz(tmp_path, name, case, mutated)
    assert code == 0
    assert summary == "authz: allowed (workflow_run)\n"
    assert calls == [
        "api",
        f"repos/{REPOSITORY}/collaborators/{case.actor}/permission",
        "--jq",
        ".permission",
    ]


X64_POOL = ["self-hosted", "pf-ci", "pf-ci-x64"]
RELEASE_POOL = ["self-hosted", "pf-ci", "pf-ci-release"]


def flat(text: str) -> str:
    return " ".join(text.split())


# The release pool refuses (and cancels) a workflow_run that started from a pull request or whose
# head branch is not main. The reconcile and the report that follow a pull request's CI therefore
# run as a second job on the credentialed x64 pool; the release-class job never admits that event.
# The two jobs of each pair must keep identical steps, so the PR case cannot drift from the other.
PR_ORIGINATED = (
    "github.event_name == 'workflow_run' && github.event.workflow_run.event == 'pull_request'"
)
SPLIT_PAIRS = (
    ("pineforge-release.yml", "reconcile", "reconcile-pr"),
    ("release-report.yml", "report", "report-pr"),
)
RECONCILE_IF = (
    "(vars.RELEASE_APP_ID != '' || "
    "(github.event_name != 'schedule' && github.event_name != 'workflow_run')) && "
    "(github.event_name != 'workflow_run' || (github.event.workflow_run.name == 'Release' && "
    "github.event.workflow_run.head_branch == 'main'))"
)
RECONCILE_PR_IF = (
    "vars.RELEASE_APP_ID != '' && github.event_name == 'workflow_run' && "
    "github.event.workflow_run.name == 'CI' && github.event.workflow_run.event == 'pull_request'"
)
REPORT_IF = "${{ !(" + PR_ORIGINATED + ") }}"
REPORT_PR_IF = "${{ " + PR_ORIGINATED + " }}"
PAIR_CONDITIONS = {
    "reconcile": RECONCILE_IF,
    "reconcile-pr": RECONCILE_PR_IF,
    "report": REPORT_IF,
    "report-pr": REPORT_PR_IF,
}


@pytest.mark.parametrize(("name", "release_job", "pr_job"), SPLIT_PAIRS)
def test_release_pool_is_never_requested_for_a_pull_request_originated_run(
    name: str, release_job: str, pr_job: str
) -> None:
    jobs = load(name)["jobs"]
    assert jobs[release_job]["runs-on"] == RELEASE_POOL
    assert jobs[pr_job]["runs-on"] == X64_POOL
    assert flat(jobs[release_job]["if"]) == flat(PAIR_CONDITIONS[release_job])
    assert flat(jobs[pr_job]["if"]) == flat(PAIR_CONDITIONS[pr_job])
    assert "'pull_request'" not in flat(jobs[release_job]["if"]).replace(PR_ORIGINATED, "")
    if pr_job == "reconcile-pr":
        assert "authz" in jobs[pr_job]["needs"]


@pytest.mark.parametrize(("name", "release_job", "pr_job"), SPLIT_PAIRS)
def test_the_pull_request_copy_has_exactly_the_release_jobs_steps(
    name: str, release_job: str, pr_job: str
) -> None:
    jobs = load(name)["jobs"]
    assert jobs[pr_job]["steps"] == jobs[release_job]["steps"]
    assert len(jobs[release_job]["steps"]) > 3


def test_the_report_runs_after_either_reconcile_and_fails_when_either_failed() -> None:
    report = load("pineforge-release.yml")["jobs"]["report"]
    assert report["needs"] == ["authz", "reconcile", "reconcile-pr"]
    assert flat(report["if"]) == (
        "always() && needs.authz.result == 'success' && "
        "(needs.reconcile.result != 'skipped' || needs.reconcile-pr.result != 'skipped')"
    )
    failed = flat(report["with"]["failed"])
    for job in ("reconcile", "reconcile-pr"):
        for result in ("failure", "cancelled"):
            assert f"needs.{job}.result == '{result}'" in failed
    assert "success" not in failed


def test_pages_deploy_is_main_only_and_never_a_pull_request() -> None:
    deploy = load("docs.yml")["jobs"]["deploy"]
    assert deploy["if"] == "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
    assert deploy["runs-on"] == RELEASE_POOL
