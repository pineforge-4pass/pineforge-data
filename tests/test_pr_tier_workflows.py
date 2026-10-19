"""Pull-request tier of the CI workflows: hosted runners, cancellation and the docs-only path."""

from __future__ import annotations

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

RUNTIME_IF = (
    "!cancelled() && (github.event_name != 'pull_request' || "
    "(needs.changes.result == 'success' && needs.changes.outputs.code == 'true'))"
)


def load(name: str) -> dict[str, Any]:
    loaded = yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def step_script(workflow: str, job: str, step_id: str) -> str:
    steps = load(workflow)["jobs"][job]["steps"]
    return str(next(step["run"] for step in steps if step.get("id") == step_id))


def pool_routes(workflow: dict[str, Any]) -> dict[str, str]:
    """Whitespace-normalized runs-on and matrix runner expressions that can pick the pool."""
    routes: dict[str, str] = {}
    for job_id, job in workflow["jobs"].items():
        matrix = job.get("strategy", {}).get("matrix", {})
        includes = matrix.get("include", []) if isinstance(matrix, dict) else []
        candidates = [job.get("runs-on", ""), *(entry.get("runner", "") for entry in includes)]
        for index, text in enumerate(candidates):
            if isinstance(text, str) and "pf-ci-x64" in text:
                routes[f"{job_id}#{index}"] = " ".join(text.split())
    return routes


def unguarded(workflow: dict[str, Any]) -> list[str]:
    return [
        route
        for route, text in pool_routes(workflow).items()
        if not (text.startswith(HOSTED_GUARD) and text.endswith(HOSTED_FALLBACK))
    ]


def cancels_only_pull_requests(concurrency: dict[str, str]) -> bool:
    return (
        concurrency["cancel-in-progress"] == CANCEL_ONLY_PRS
        and PR_OR_RUN_KEY in concurrency["group"]
    )


@pytest.mark.parametrize("name", PR_TIER_WORKFLOWS)
def test_pull_requests_never_select_the_pool(name: str) -> None:
    workflow = load(name)
    assert pool_routes(workflow), "push and dispatch keep the pool"
    assert unguarded(workflow) == []


def test_the_old_same_repo_route_to_the_pool_is_rejected() -> None:
    assert unguarded({"jobs": {"build": {"runs-on": OLD_ROUTE}}}) == ["build#0"]
    matrix = {"jobs": {"release": {"strategy": {"matrix": {"include": [{"runner": OLD_ROUTE}]}}}}}
    assert unguarded(matrix) == ["release#1"]
    mixed = {"jobs": {"a": {"runs-on": GUARDED_ROUTE}, "b": {"runs-on": OLD_ROUTE}}}
    assert unguarded(mixed) == ["b#0"]
    assert unguarded({"jobs": {"a": {"runs-on": GUARDED_ROUTE}}}) == []


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
    assert "secrets." not in (WORKFLOWS / name).read_text()
    workflow = load(name)
    assert set(workflow["permissions"].values()) == {"read"}
    for job_id, job in workflow["jobs"].items():
        if "permissions" in job:
            # Only the Pages deployment writes, and it never runs for a pull request.
            assert job_id == "deploy"
            assert job["if"] == "github.event_name != 'pull_request'"


def test_docs_only_prs_skip_only_the_runtime_jobs() -> None:
    jobs = load("ci.yml")["jobs"]
    assert jobs["changes"]["if"] == "github.event_name == 'pull_request'"
    # The unit matrix always runs: tests/test_documentation.py checks README.md, CONTRIBUTING.md
    # and docs/ links, and its check names stay reported.
    assert "needs" not in jobs["test"]
    assert "if" not in jobs["test"]
    for name in RUNTIME_JOBS:
        assert jobs[name]["needs"] == "changes"
        assert " ".join(jobs[name]["if"].split()) == RUNTIME_IF
    assert set(jobs["ready"]["needs"]) == {"changes", "test", *RUNTIME_JOBS}
    assert jobs["ready"]["if"] == "always()"
    assert jobs["ready"]["name"] == "release/ready"


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
        "EVENT_NAME": event,
        "CHANGES_RESULT": changes,
        "CODE_CHANGED": code,
        "UNIT_RESULT": unit,
        "RUNTIME_RESULTS": runtime,
    }


def run_gate(tmp_path: Path, inputs: dict[str, str]) -> tuple[int, str]:
    output = tmp_path / "github-output"
    output.write_text("")
    completed = subprocess.run(
        ["bash", "-e", "-c", step_script("ci.yml", "ready", "gate")],
        env={**os.environ, **inputs, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    return completed.returncode, output.read_text().strip()


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
REJECTED = {
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
    assert run_gate(tmp_path, inputs) == (0, expected)


@pytest.mark.parametrize("case", list(REJECTED))
def test_release_ready_rejects_every_unexpected_outcome(tmp_path: Path, case: str) -> None:
    returncode, output = run_gate(tmp_path, REJECTED[case])
    assert returncode != 0
    assert output == ""
