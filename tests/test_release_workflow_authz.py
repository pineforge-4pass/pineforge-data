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

# Literal transcription of ci-labels.md, frozen sha 99de80f9, lines 53-109.
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
    '    [ "$WR_HEAD_REPO" = "$REPO" ] || fail '
    '"workflow_run triggered from another repository (${WR_HEAD_REPO:-unknown})"\n'
    '    case " $ALLOWED_BOTS " in\n'
    '      *" $WR_ACTOR "*) ;;\n'
    '      *)\n'
    '        perm=$(gh api "repos/$REPO/collaborators/$WR_ACTOR/permission" '
    '--jq .permission 2>/dev/null || echo none)\n'
    '        case "$perm" in admin|maintain|write) ;; *) fail '
    '"workflow_run triggering actor ${WR_ACTOR:-unknown} has permission '
    "'$perm', not write\" ;; esac ;;\n"
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
    '          *) fail "author ${PR_AUTHOR:-unknown} (${ASSOCIATION:-NONE}) '
    'is not an owner, member, collaborator or allowlisted bot" ;;\n'
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
[ "$2" = "repos/$REPO/collaborators/$WR_ACTOR/permission" ] || exit 97
[ "$3" = "--jq" ] || exit 97
[ "$4" = ".permission" ] || exit 97
[ "$GH_FAIL" = "0" ] || exit 1
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
        "needs.reconcile.result != 'skipped'"
        if "reconcile" in jobs
        else "!inputs.claim_placeholder"
    )
    report = jobs["report"]
    assert report["if"] == f"always() && {AUTHZ_PASSED} && {report_tail}", "report: authz condition"
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


def actor_refusal(actor: str, permission: str = "none") -> str:
    return f"workflow_run triggering actor {actor} has permission '{permission}', not write"


FORK_REFUSAL = f"workflow_run triggered from another repository ({FORK_REPOSITORY})"
EVENT_REFUSAL = "does not run CI (pull_request_target and other events are not allowed)"
CASES = {
    "same-repo-dependabot": Case(actor="dependabot[bot]"),
    "same-repo-app": Case(actor="pineforge-app[bot]"),
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
    "unknown-none": Case(lookup=True, refusal=actor_refusal("outsider")),
    "unknown-read": Case(
        permission="read", lookup=True, refusal=actor_refusal("outsider", "read")
    ),
    "unknown-triage": Case(
        permission="triage", lookup=True, refusal=actor_refusal("outsider", "triage")
    ),
    "lookup-failed": Case(
        permission="write", gh_fails=True, lookup=True, refusal=actor_refusal("outsider")
    ),
    "empty-head": Case(
        actor="pineforge-app[bot]",
        head_repo="",
        refusal="workflow_run triggered from another repository (unknown)",
    ),
    "pull-request-target": Case(
        event="pull_request_target", refusal=f"event pull_request_target {EVENT_REFUSAL}"
    ),
    "unknown-event": Case(event="unknown_event", refusal=f"event unknown_event {EVENT_REFUSAL}"),
    "other-bot": Case(
        actor="github-actions[bot]", lookup=True, refusal=actor_refusal("github-actions[bot]")
    ),
    "app-without-bot-suffix": Case(
        actor="pineforge-app", lookup=True, refusal=actor_refusal("pineforge-app")
    ),
    "app-with-extra-suffix": Case(
        actor="pineforge-app[bot]x", lookup=True, refusal=actor_refusal("pineforge-app[bot]x")
    ),
    "dependabot-with-prefix": Case(
        actor="xdependabot[bot]", lookup=True, refusal=actor_refusal("xdependabot[bot]")
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
    environment = {
        "PATH": str(bin_dir),
        "EVENT": case.event,
        "REPO": REPOSITORY,
        "HEAD_REPO": "",
        "HEAD_IS_FORK": "",
        "ASSOCIATION": "",
        "PR_AUTHOR": "",
        "WR_HEAD_REPO": case.head_repo,
        "WR_ACTOR": case.actor,
        "ALLOWED_BOTS": step["env"]["ALLOWED_BOTS"],
        "GH_TOKEN": "fake-token",
        "GH_PERMISSION": case.permission,
        "GH_FAIL": "1" if case.gh_fails else "0",
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


def assert_decision(result: tuple[int, str, list[str]], case: Case) -> None:
    code, summary, calls = result
    if case.refusal:
        assert code == 1, (code, summary)
        assert summary == f"authz: REFUSED: {case.refusal}\n"
    else:
        assert code == 0, (code, summary)
        assert summary == f"authz: allowed ({case.event})\n"
    expected_calls = (
        ["api", f"repos/{REPOSITORY}/collaborators/{case.actor}/permission", "--jq", ".permission"]
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
    assert summary == "authz: allowed (workflow_run)\n"


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
@pytest.mark.parametrize("case_name", ("fork-dependabot", "fork-app"))
def test_missing_head_check_mutation_allows_fork_bots(
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
    assert calls == []
