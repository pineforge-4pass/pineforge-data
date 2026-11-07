"""Pin release authorization to the org canon and exercise its actual shell decisions."""

from __future__ import annotations

import re
import subprocess
import warnings
from collections.abc import Iterator
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
    f"!cancelled() && {AUTHZ_PASSED} && needs.prepare.outputs.eligible == 'true' && "
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


PRIVATE_KEY = re.compile(r"private[-_\s]*key", re.IGNORECASE)
SECRET_REFERENCE = re.compile(
    r"""\bsecrets\s*(?:\.\s*([\w-]+)|\[\s*(['"])(.*?)\2\s*\])""", re.IGNORECASE
)
DYNAMIC_SECRET = re.compile(r"""\bsecrets\s*\[(?!\s*['"])""", re.IGNORECASE)
SCRIPT_MINT = re.compile(
    r"/(?:app/)?installations/[^\n]+/(?:access_tokens|tokens)\b"
    r"|\b(?:createInstallationAccessToken|getInstallationAccessToken|createAppAuth"
    r"|create_installation_access_token|get_installation_access_token|get_installation)\b"
    r"|\bjwt\b|\bopenssl\b[^\n]*\s-sign\b",
    re.IGNORECASE,
)


def app_token_scalars(
    value: Any, key: str = "", seen: set[int] | None = None
) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, (dict, list)):
        if seen is None:
            seen = set()
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, dict):
            for name, child in value.items():
                yield from app_token_scalars(child, str(name), seen)
        else:
            for child in value:
                yield from app_token_scalars(child, key, seen)


def assert_app_token_environments(workflow: dict[str, Any]) -> None:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict), "workflow jobs must be a mapping"
    for job_id, job in jobs.items():
        assert isinstance(job, dict), f"{job_id}: workflow job must be a mapping"
        steps = job.get("steps", [])
        assert isinstance(steps, list), f"{job_id}: workflow steps must be a list"
        known_action = any(
            isinstance(step, dict)
            and isinstance(step.get("uses"), str)
            and step["uses"].strip().partition("@")[0].casefold()
            == "actions/create-github-app-token"
            for step in steps
        )
        scalars = list(app_token_scalars([workflow.get("env", {}), job]))
        private_key = any(
            DYNAMIC_SECRET.search(value)
            or any(
                PRIVATE_KEY.search(match.group(1) or match.group(3))
                or PRIVATE_KEY.search(key)
                for match in SECRET_REFERENCE.finditer(value)
            )
            for key, value in scalars
        )
        script_mint = any(SCRIPT_MINT.search(value) for _key, value in scalars)
        if not (known_action or private_key or script_mint):
            continue
        environment = job.get("environment")
        if isinstance(environment, dict):
            environment = environment.get("name")
        assert (
            isinstance(environment, str)
            and environment.strip()
            and "${{" not in environment
        ), f"{job_id}: App-token mint requires a literal non-empty environment"
        if script_mint or (private_key and not known_action):
            warnings.warn(
                f"{job_id}: possible App-token mint; "
                "human review of private-key/script use required",
                UserWarning,
                stacklevel=2,
            )


def load_text(text: str) -> dict[str, Any]:
    loaded = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    # SafeLoader preserves null/bool/number types for the environment check;
    # BaseLoader keeps the existing canon comparisons (including "on") unchanged.
    assert_app_token_environments(yaml.safe_load(text))
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
    # old: assert authz["runs-on"] == ["self-hosted", "pf-ci", "pf-ci-x64"]
    assert authz["runs-on"] == "ubuntu-24.04"
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
        "needs.reconcile.result != 'skipped'"
        if "reconcile" in jobs
        else "!inputs.claim_placeholder"
    )
    report = jobs["report"]
    assert report["if"] == (
        f"!cancelled() && {AUTHZ_PASSED} && {report_tail}"
    ), "report: authz condition"
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
        before = f"  report:\n    if: >-\n      !cancelled() && {AUTHZ_PASSED} && "
        after = "  report:\n    if: >-\n      !cancelled() && "
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
        before = f"!cancelled() && {AUTHZ_PASSED} && needs.prepare.outputs.eligible"
        after = "!cancelled() && needs.prepare.outputs.eligible"
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


HOSTED_RELEASE_RUNNER = "ubuntu-24.04"


def flat(text: str) -> str:
    return " ".join(text.split())


# #1050: every release-class job runs on the standard hosted label; none selects a runner pool.
# The reconcile after a pull request's CI and the report that follows are the single `reconcile`
# and `report` jobs again (no `reconcile-pr` / `report-pr` copies).
@pytest.mark.parametrize(
    "name", ("docs.yml", "pineforge-release.yml", "release.yml", "release-report.yml")
)
def test_release_class_jobs_run_on_the_hosted_runner_and_never_on_a_pool(name: str) -> None:
    jobs = load(name)["jobs"]
    assert jobs
    for job_id, job in jobs.items():
        if "uses" in job:  # a reusable-workflow call has no runner of its own
            continue
        assert job["runs-on"] == HOSTED_RELEASE_RUNNER, f"{name}:{job_id}"
    assert "reconcile-pr" not in jobs
    assert "report-pr" not in jobs


def test_reconcile_admits_a_release_run_only_from_main() -> None:
    reconcile = load("pineforge-release.yml")["jobs"]["reconcile"]
    assert flat(reconcile["if"]) == (
        "(vars.RELEASE_APP_ID != '' || "
        "(github.event_name != 'schedule' && github.event_name != 'workflow_run')) && "
        "(github.event_name != 'workflow_run' || "
        "(github.event.workflow_run.name == 'CI' && "
        "github.event.workflow_run.event == 'pull_request') || "
        "(github.event.workflow_run.name == 'Release' && "
        "github.event.workflow_run.head_branch == 'main'))"
    )


def test_pages_deploy_is_main_only_and_never_a_pull_request() -> None:
    deploy = load("docs.yml")["jobs"]["deploy"]
    assert deploy["if"] == "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
    assert deploy["runs-on"] == HOSTED_RELEASE_RUNNER


# CR-304: these refusals exercise the existing load_text preflight, which on
# b8180f9 returns the parsed mapping without checking App-token environments.
APP_ENV_FINDING = "mint: App-token mint requires a literal non-empty environment"
APP_ENV_INVENTORY = (
    ("pineforge-release.yml", "reconcile", "release-bot"),
    ("release-report.yml", "report", "release-bot"),
    ("release.yml", "manual-pr", "release"),
    ("release.yml", "prepare", "release-bot"),
)


def app_token_workflow(
    environment: str = "",
    step: str = "uses: actions/create-github-app-token@v2",
) -> str:
    return (
        "jobs:\n  mint:\n    runs-on: ubuntu-24.04\n"
        + "".join(f"    {line}\n" for line in environment.splitlines())
        + "    steps:\n      - "
        + "\n        ".join(step.splitlines())
        + "\n"
    )


def assert_app_environment_refusal(text: str, job_id: str = "mint") -> None:
    with pytest.raises(AssertionError) as refused:
        load_text(text)
    assert str(refused.value) == (
        f"{job_id}: App-token mint requires a literal non-empty environment"
    )


def check_workflow_files(directory: Path) -> None:
    # Refuse sensitive names and symlinks before enumerating or opening inputs.
    def safe_input(path: Path) -> None:
        parts = tuple(part.casefold() for part in path.expanduser().absolute().parts)
        forbidden = {".ssh", ".aws", ".codex"}
        assert not forbidden.intersection(parts), f"refused workflow input: {path}"
        named = "/".join(parts)
        assert "/.config/zsh/keys" not in named, f"refused workflow input: {path}"
        assert "/.config/pineforge" not in named, f"refused workflow input: {path}"
        assert not any(part.endswith(".env") for part in parts), (
            f"refused workflow input: {path}"
        )
        assert ".." not in path.parts, f"refused workflow input: {path}"
        assert not any(item.is_symlink() for item in (*reversed(path.parents), path)), (
            f"refused workflow symlink: {path}"
        )

    safe_input(directory)
    files = sorted(path for path in directory.iterdir() if path.suffix in {".yml", ".yaml"})
    assert files, "no workflow YAML files found"
    for path in files:
        safe_input(path)
    print("Workflow preflight inputs:\n" + "\n".join(str(path) for path in files))
    for path in files:
        try:
            load_text(path.read_text(encoding="utf-8"))
        except AssertionError as error:
            raise AssertionError(f"{path.name}:{error}") from error


@pytest.mark.parametrize("ref", ("v1", "v2", "v99", "a" * 40))
def test_app_token_any_ref_without_environment_is_refused(ref: str) -> None:
    step = f"uses: actions/create-github-app-token@{ref}"
    assert_app_environment_refusal(app_token_workflow(step=step))
    load_text(app_token_workflow("environment: release-bot", step))


@pytest.mark.parametrize(
    "environment",
    (
        "environment: release-bot",
        'environment: "release"',
        "environment:\n  name: release-bot\n  url: https://example.com/release",
        "environment: {name: release-bot}",
    ),
)
def test_app_token_literal_environment_controls(environment: str) -> None:
    assert_app_environment_refusal(app_token_workflow())
    load_text(app_token_workflow(environment))


@pytest.mark.parametrize(
    "environment",
    (
        "environment:",
        'environment: ""',
        "environment: '   '",
        "environment: null",
        "environment: false",
        "environment: 42",
        "environment: []",
        "environment: ${{ inputs.environment }}",
        "environment: release-${{ github.ref_name }}",
        "environment:\n  url: https://example.com/release",
        "environment:\n  name:",
        "environment:\n  name: ${{ matrix.environment }}",
        'environment: {name: "${{ inputs.environment }}"}',
    ),
)
def test_app_token_empty_or_dynamic_environment_is_refused(environment: str) -> None:
    assert_app_environment_refusal(app_token_workflow(environment))
    load_text(app_token_workflow("environment:\n  name: release-bot"))


@pytest.mark.parametrize(
    "refused,accepted",
    (
        (
            'jobs: {mint: {steps: [{uses: "actions/create-github-app-token@v3"}]}}',
            'jobs: {mint: {environment: release-bot, '
            'steps: [{uses: "actions/create-github-app-token@v3"}]}}',
        ),
        (
            '"jobs": {"mint": {"environment": "", '
            '"steps": [{"uses": "actions/create-github-app-token@v3"}]}}',
            '"jobs": {"mint": {"environment": {"name": "release"}, '
            '"steps": [{"uses": "actions/create-github-app-token@v3"}]}}',
        ),
        (
            'jobs: {mint: {"envir\\u006fnment": "", '
            'steps: [{"us\\u0065s": "actions/create-github-app-token@v3"}]}}',
            'jobs: {mint: {"envir\\u006fnment": {name: release-bot}, '
            'steps: [{"us\\u0065s": "actions/create-github-app-token@v3"}]}}',
        ),
    ),
)
def test_app_token_quoted_keys_and_flow_mappings_are_checked(
    refused: str, accepted: str
) -> None:
    assert_app_environment_refusal(refused)
    load_text(accepted)


@pytest.mark.parametrize(
    "step",
    (
        "uses: vendor/mint@v1\nwith:\n  private-key: ${{ secrets.DEPLOY_CREDENTIAL }}",
        "uses: vendor/other@v1\nwith:\n  credential: ${{ secrets.BUILD_PRIVATE_KEY }}",
        "run: ./mint.sh\nenv:\n  APP_KEY: ${{ secrets['BUILD_PRIVATE_KEY'] }}",
        "run: ./mint.sh\nenv:\n  APP_KEY: ${{ secrets[vars.KEY_SECRET] }}",
        "uses: actions/github-script@v8\nwith:\n  script: |\n"
        "    github.rest.apps.createInstallationAccessToken({installation_id: 7})",
        "run: gh api --method POST /app/installations/7/access_tokens",
        "run: curl -X POST https://api.github.com/app/installations/7/access_tokens",
        "run: python -c 'jwt.encode(payload, key, algorithm=\"RS256\")'",
        "run: openssl dgst -sha256 -sign app.pem",
    ),
)
def test_alternative_mints_and_private_key_transfers_are_flagged(step: str) -> None:
    assert_app_environment_refusal(app_token_workflow(step=step))
    with pytest.warns(UserWarning, match="possible App-token mint; human review"):
        load_text(app_token_workflow("environment: release-bot", step))


@pytest.mark.parametrize("scope", ("workflow", "job"))
def test_inherited_private_key_secret_is_checked(scope: str) -> None:
    workflow = yaml.safe_load(app_token_workflow(step="run: ./mint.sh"))
    target = workflow if scope == "workflow" else workflow["jobs"]["mint"]
    target["env"] = {"APP_KEY": "${{ secrets.RELEASE_APP_PRIVATE_KEY }}"}
    assert_app_environment_refusal(yaml.safe_dump(workflow))
    workflow["jobs"]["mint"]["environment"] = "release-bot"
    with pytest.warns(UserWarning, match="possible App-token mint; human review"):
        load_text(yaml.safe_dump(workflow))


def test_mint_in_a_second_workflow_file_is_refused(tmp_path: Path) -> None:
    first = tmp_path / "a.yml"
    second = tmp_path / "z.yaml"
    first.write_text("jobs: {docs: {steps: [{run: 'echo docs'}]}}\n", encoding="utf-8")
    second.write_text(app_token_workflow(), encoding="utf-8")
    with pytest.raises(AssertionError) as refused:
        check_workflow_files(tmp_path)
    assert str(refused.value) == f"z.yaml:{APP_ENV_FINDING}"
    second.write_text(app_token_workflow("environment: release-bot"), encoding="utf-8")
    check_workflow_files(tmp_path)


def test_a_sibling_jobs_environment_does_not_cover_a_mint() -> None:
    workflow = yaml.safe_load(app_token_workflow())
    workflow["jobs"]["docs"] = {
        "environment": "release-bot",
        "steps": [{"run": "echo docs"}],
    }
    assert_app_environment_refusal(yaml.safe_dump(workflow))
    workflow["jobs"]["mint"]["environment"] = "release-bot"
    load_text(yaml.safe_dump(workflow))


def test_non_minting_jobs_do_not_need_an_environment() -> None:
    assert_app_environment_refusal(app_token_workflow())
    load_text(
        "jobs: {docs: {steps: [{run: 'echo docs'}]}, "
        "read: {steps: [{run: 'gh api repos/org/repo', "
        "env: {GH_TOKEN: '${{ github.token }}'}}]}}"
    )
    # The existing mapping-style deployment environment is also accepted.
    load_text(
        "jobs: {publish: {environment: {name: pypi, url: 'https://pypi.org'}, "
        "steps: [{uses: 'pypa/gh-action-pypi-publish@release/v1'}]}}"
    )


@pytest.mark.parametrize("name,job_id,environment", APP_ENV_INVENTORY)
def test_environment_removal_mutation_is_refused(
    name: str, job_id: str, environment: str
) -> None:
    workflow = load(name)
    assert workflow["jobs"][job_id].get("environment") == environment
    workflow["jobs"][job_id].pop("environment")
    assert_app_environment_refusal(yaml.safe_dump(workflow), job_id)


def test_every_workflow_file_has_app_token_environment_preflight() -> None:
    check_workflow_files(WORKFLOWS)
    for name, job_id, environment in APP_ENV_INVENTORY:
        assert load(name)["jobs"][job_id].get("environment") == environment
