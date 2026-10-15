"""App-scoped PR reconciliation; all mutations use trusted default-branch code."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from bump_version import bumped_version, replace_project_version

from .contract import (
    DOCKERFILE,
    LOCK,
    MANIFEST,
    RUNTIME,
    UPDATE_FILES,
    gh_bytes,
    hub_manifest,
    json_bytes,
    load_lock,
    newest_ready,
    object_json,
    package_version,
    require,
    runtime_pin,
    update_files,
    validate_event,
    verify_artifacts,
    verify_local,
    verify_manifest,
    version_key,
)
from .evidence import gate_for_run
from .receipts import FIELDS, candidate, canonical, receipt, timestamp

MARKER = re.compile(r"<!-- pineforge-data-release (\{[^\n]+\}) -->")


def git(*args: str, root: Path | None = None) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def api(repo: str, endpoint: str, method: str = "GET", data: object = None) -> Any:
    target = f"repos/{repo}" + (f"/{endpoint}" if endpoint else "")
    command = ["gh", "api", target, "--method", method]
    if data is not None:
        command.extend(("--input", "-"))
    result = subprocess.run(
        command,
        input=json_bytes(data) if data is not None else None,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout else None


def marker(pr: dict[str, Any]) -> dict[str, Any] | None:
    matches = MARKER.findall(pr.get("body") or "")
    require(len(matches) <= 1, "ambiguous release PR marker")
    return object_json(matches[0].encode()) if matches else None


def inventory(repo: str, endpoint: str, key: str | None = None) -> list[Any]:
    result = []
    separator = "&" if "?" in endpoint else "?"
    page = 1
    while True:
        data = api(repo, f"{endpoint}{separator}per_page=100&page={page}")
        items = data[key] if key else data
        require(isinstance(items, list), "unexpected GitHub inventory response")
        result.extend(items)
        if len(items) < 100:
            return result
        page += 1


def body(metadata: dict[str, Any]) -> str:
    return (
        f"Publish pineforge-data {metadata['version']} after its own CI and runtime E2E.\n\n"
        "Required: Python 3.11/3.12/3.13, manifest/source/OCI verification, real Docker "
        "harness and ABI checks, concurrent server/cache checks, disposable database E2E, "
        "and installation/backtesting of both distributions. `release/ready` must pass "
        "for the current head and base. Publication reuses the landed commit's CI artifacts.\n\n"
        f"<!-- pineforge-data-release {json.dumps(metadata, sort_keys=True)} -->\n"
    )


def finish(report: dict[str, Any], path: Path) -> None:
    require(set(report) == FIELDS, "receipt fields differ from the hub contract")
    if report["event_id"] is None:
        return  # No target means no receipt, including ordinary non-release main pushes.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(report))


def snapshot(revision: str, root: Path) -> None:
    for name in (RUNTIME, DOCKERFILE, "pyproject.toml", LOCK, MANIFEST):
        result = subprocess.run(["git", "show", f"{revision}:{name}"], capture_output=True)
        if result.returncode:
            require(name in {LOCK, MANIFEST}, f"missing source {name}")
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.stdout)


def manual_patch(root: Path, part: str) -> dict[str, bytes]:
    verify_local(root)
    current = package_version(root)
    version = bumped_version(current, part)  # type: ignore[arg-type]
    patch = {
        "pyproject.toml": replace_project_version(
            (root / "pyproject.toml").read_text(), current, version
        ).encode()
    }
    lock = load_lock(root)
    if lock:
        lock["previous"] = {
            "package_version": current,
            "image": runtime_pin(root),
            "event_id": lock["event"]["event_id"],
        }
        lock["package_version"] = version
        patch[LOCK] = json_bytes(lock)
    return patch


def assert_patch(base: str, head: str, patch: dict[str, bytes]) -> None:
    names = set(git("diff", "--name-only", base, head).splitlines())
    require(names == set(patch), "bot branch contains edits outside the deterministic update")
    for name, expected in patch.items():
        actual = subprocess.check_output(["git", "show", f"{head}:{name}"])
        require(actual == expected, f"bot branch has unexpected content in {name}")


def rollback_patch(root: Path, repo: str, known_good_package: str) -> dict[str, bytes]:
    from .publishing import stored_receipt

    version_key(known_good_package)
    saved = stored_receipt(repo, known_good_package)
    require(saved is not None and saved["state"] == "verified", "rollback target is not verified")
    assert saved is not None
    from .publishing import verified_package

    actual = verified_package(repo, known_good_package, saved)
    revision = actual["source"]
    installed = verify_local(root)
    require(installed is not None, "rollback needs a recorded current manifest")
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary)
        snapshot(revision, target)
        lock = verify_local(target)
        require(
            lock is not None and lock["event"]["event_id"] == saved["event_id"],
            "known-good package has no matching release manifest",
        )
        assert lock is not None and installed is not None
        event = lock["event"]
        verify_artifacts(event)
        return update_files(
            root,
            event,
            (target / MANIFEST).read_bytes(),
            rollback={
                "package_version": known_good_package,
                "source": revision,
                "rejected_event_id": installed["event"]["event_id"],
            },
        )


def create_pr(
    repo: str, base: str, patch: dict[str, bytes], branch: str, metadata: dict[str, Any], bot: str
) -> dict[str, Any]:
    require(set(patch) <= UPDATE_FILES, "generated change exceeds release allowlist")
    refs = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    user = object_json(gh_bytes(f"users/{bot}"))
    email = f"{user['id']}+{bot}@users.noreply.github.com"
    if refs:
        # Recover a push-before-PR crash without touching the retained branch.
        git("fetch", "origin", f"refs/heads/{branch}")
        head = git("rev-parse", "FETCH_HEAD")
        assert_patch(base, head, patch)
        require(
            set(git("log", "--format=%ae", f"{base}..{head}").splitlines()) == {email},
            "orphan bot branch contains human commits",
        )
    else:
        git("switch", "--create", branch, base)
        for name, content in patch.items():
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        git("config", "user.name", bot)
        git("config", "user.email", email)
        git("add", "--", *sorted(patch))
        git("commit", "-m", f"Update PineForge data release to {metadata['version']}")
        head = git("rev-parse", "HEAD")
        git("push", "origin", f"HEAD:refs/heads/{branch}")  # No base push or force push.
    metadata.update(generated_head=head, base=base)
    return api(
        repo,
        "pulls",
        "POST",
        {
            "base": "main",
            "head": branch,
            "title": f"Release pineforge-data {metadata['version']}",
            "body": body(metadata),
        },
    )


def open_bot_prs(repo: str) -> list[dict[str, Any]]:
    prs = inventory(repo, "pulls?state=open&base=main")
    return [pr for pr in prs if pr["head"]["ref"].startswith("bot/pineforge-") and marker(pr)]


def pr_identity(pr: dict[str, Any], *, base: str | None = None) -> dict[str, Any]:
    return {
        "number": pr["number"],
        "base": pr["base"]["ref"],
        "base_sha": base or pr["base"]["sha"],
        "head": pr["head"]["sha"],
        "merge": pr.get("merge_commit_sha") if pr.get("merged") else None,
    }


def maybe_merge(
    repo: str,
    pr: dict[str, Any],
    report: dict[str, Any],
    bot: str,
    event: dict[str, Any] | None,
    manifest: bytes | None,
) -> None:
    number = pr["number"]
    pr = api(repo, f"pulls/{number}")
    meta = marker(pr)
    assert meta is not None
    report["pr"] = pr_identity(pr)
    require(
        pr["user"]["login"] == bot and pr["head"]["repo"]["full_name"] == repo,
        "PR is not owned by the scoped release App",
    )
    require(
        pr["head"]["sha"] == meta["generated_head"],
        "bot branch has human/unrecorded commits; retaining its history",
    )
    require(pr["base"]["ref"] == "main" and pr["state"] == "open", "PR is no longer eligible")
    git("fetch", "origin", "main", f"refs/heads/{pr['head']['ref']}")
    base = git("rev-parse", "origin/main")
    head = pr["head"]["sha"]
    behind = git("merge-base", base, head) != base
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary)
        comparison_base = meta["base"] if behind else base
        snapshot(comparison_base, source)
        if meta["kind"] == "rollback":
            patch = rollback_patch(source, repo, meta["rollback_package"])
            event = object_json(patch[LOCK])["event"]
            manifest = patch[MANIFEST]
            report.update(receipt(event, "rollback"))
            report["pr"] = pr_identity(pr, base=comparison_base)
        else:
            patch = (
                update_files(source, event, manifest)
                if event
                else manual_patch(source, meta["bump"])
            )
        assert_patch(comparison_base, head, patch)
    if behind:
        # GitHub adds a merge commit; it never rewrites or deletes branch history.
        result = api(repo, f"pulls/{number}/update-branch", "PUT", {"expected_head_sha": head})
        require(result is not None, "base update failed")
        for _attempt in range(15):
            updated = api(repo, f"pulls/{number}")
            if updated["head"]["sha"] != head:
                break
            time.sleep(2)
        else:
            raise ValueError("base update did not produce a new head; no stale evidence accepted")
        meta.update(generated_head=updated["head"]["sha"], base=base)
        api(repo, f"pulls/{number}", "PATCH", {"body": body(meta)})
        report["pr"] = pr_identity(updated, base=base)
        report["state"] = "testing"
        return
    report["pr"] = pr_identity(pr, base=base)
    runs = api(
        repo, f"actions/workflows/ci.yml/runs?event=pull_request&head_sha={head}&per_page=100"
    )["workflow_runs"]
    runs = [
        run
        for run in runs
        if run["head_sha"] == head
        and any(item["number"] == number for item in run["pull_requests"])
    ]
    if not runs:
        report["state"] = "pr_open"
        return
    run = max(runs, key=lambda item: item["id"])
    if run["status"] != "completed":
        report["state"] = "testing"
        return
    evidence = gate_for_run(repo, run["id"], head=head, base=base, pr=number)
    if event:
        candidate(report, event, evidence, meta["version"])
    git("fetch", "origin", f"refs/pull/{number}/merge")
    require(
        git("rev-parse", "FETCH_HEAD") == evidence["tested_sha"]
        and git("rev-parse", "FETCH_HEAD^{tree}") == evidence["tree"],
        "tested merge candidate has changed",
    )
    if event:
        latest = newest_ready()
        require(
            latest is not None
            and (
                latest[0]["event_id"] == event["event_id"]
                or (
                    meta["kind"] == "rollback"
                    and latest[0]["event_id"] in object_json(patch[LOCK])["rejected_event_ids"]
                )
            ),
            "release superseded immediately before merge",
        )
    fresh = api(repo, f"pulls/{number}")
    require(
        fresh["head"]["sha"] == head and fresh["base"]["sha"] == base,
        "PR inputs moved immediately before merge",
    )
    require(
        api(repo, "git/ref/heads/main")["object"]["sha"] == base,
        "main moved immediately before merge",
    )
    require(fresh["mergeable_state"] == "clean", "branch protections or reviews block merge")
    report["state"] = "ready"
    result = api(
        repo,
        f"pulls/{number}/merge",
        "PUT",
        {
            "sha": head,
            "merge_method": "squash",
            "commit_title": fresh["title"],
        },
    )  # Normal branch protection applies. No admin/bypass merge or branch deletion.
    require(result["merged"], "GitHub refused protected merge")
    report["state"] = "merged"
    report["pr"]["merge"] = result["sha"]
    report["timestamp"] = timestamp()


def reconcile(
    repo: str,
    trigger: dict[str, Any],
    report: dict[str, Any],
    bot: str,
    *,
    bump: str | None = None,
    rollback_package: str | None = None,
) -> None:
    git("fetch", "origin", "main")
    base = git("rev-parse", "origin/main")
    prs = open_bot_prs(repo)
    incoming = trigger.get("client_payload")
    event: dict[str, Any] | None = None
    manifest: bytes | None = None
    if incoming is not None:
        validate_event(incoming)
        manifest = hub_manifest(incoming["release_version"])
        verify_manifest(incoming, manifest)
        report.update(receipt(incoming, "reconcile"))
        if incoming["prerelease"]:
            report["state"] = "skipped_by_policy"
            return
        event = incoming
    latest = newest_ready()
    if latest:
        if event and latest[0]["release_version"] == event["release_version"]:
            require(latest[0]["event_id"] == event["event_id"], "fixed release identity changed")
        if event and version_key(latest[0]["release_version"]) > version_key(
            event["release_version"]
        ):
            report["state"] = "superseded"
            return
        if event is None:
            event, manifest = latest
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary)
        snapshot(base, source)
        installed = verify_local(source)
        if rollback_package:
            patch = rollback_patch(source, repo, rollback_package)
            event = object_json(patch[LOCK])["event"]
            manifest = patch[MANIFEST]
            report.update(receipt(event, "rollback"))
            version = object_json(patch[LOCK])["package_version"]
            meta = {
                "kind": "rollback",
                "rollback_package": rollback_package,
                "event_id": event["event_id"],
                "version": version,
            }
            branch = (
                f"bot/pineforge-release/main/rollback/{version}-{event['manifest']['sha256'][:12]}"
            )
        elif bump:
            installed_version = runtime_pin(source).split(":")[-2].split("@", 1)[0]
            require(
                not latest
                or (installed and latest[0]["event_id"] in installed.get("rejected_event_ids", []))
                or version_key(latest[0]["release_version"]) <= version_key(installed_version),
                "a hub update is pending; reconcile that PR before a separate package bump",
            )
            require(
                not any(marker(pr)["kind"] == "release" for pr in prs),
                "a release PR is pending; do not race its independent package bump",
            )
            patch = manual_patch(source, bump)
            version = bumped_version(package_version(source), bump)  # type: ignore[arg-type]
            meta = {"kind": "package", "bump": bump, "version": version, "event_id": None}
            branch = f"bot/pineforge-package/main/{version}-{base[:12]}"
        elif event:
            report.update(receipt(event, "reconcile"))
            rollbacks = [pr for pr in prs if marker(pr)["kind"] == "rollback"]
            if rollbacks and installed and installed["event"]["event_id"] == event["event_id"]:
                require(len(rollbacks) == 1, "ambiguous pending rollback PR")
                maybe_merge(repo, rollbacks[0], report, bot, None, None)
                return
            if installed and event["event_id"] in installed.get("rejected_event_ids", []):
                if incoming is None:
                    for pr in prs:
                        if marker(pr)["kind"] == "package":
                            maybe_merge(repo, pr, report, bot, None, None)
                            return
                    from .publishing import recover_applied

                    report.update(receipt(installed["event"], "reconcile-rollback"))
                    recover_applied(repo, source, report)
                    return
                report["state"] = "superseded"
                return
            if installed and installed["event"]["event_id"] == event["event_id"]:
                for pr in prs:
                    if marker(pr)["kind"] == "package":
                        maybe_merge(repo, pr, report, bot, None, None)
                        return
                # Applied is not the same as published; the publisher owns verified.
                report["state"] = "merged"
                from .publishing import recover_applied

                recover_applied(repo, source, report)
                return
            installed_version = runtime_pin(source).split(":")[-2].split("@", 1)[0]
            if version_key(event["release_version"]) <= version_key(installed_version):
                require(
                    event["release_version"] != installed_version,
                    "same release version has an unrecognized identity",
                )
                report["state"] = "superseded"
                return
            newer_prs = [
                pr
                for pr in prs
                if marker(pr).get("release_version")
                and version_key(marker(pr)["release_version"])
                > version_key(event["release_version"])
            ]
            if newer_prs:
                report["state"] = "superseded"
                return
            assert manifest is not None
            verify_artifacts(event)
            patch = update_files(source, event, manifest)
            version = object_json(patch[LOCK])["package_version"]
            meta = {
                "kind": "release",
                "event_id": event["event_id"],
                "release_version": event["release_version"],
                "version": version,
            }
            branch = (
                f"bot/pineforge-release/main/stable/{event['release_version']}-"
                f"{event['manifest']['sha256'][:12]}"
            )
        else:
            # Still finish a manual package PR when the hub has no ready releases.
            for pr in prs:
                if marker(pr)["kind"] == "package":
                    maybe_merge(repo, pr, report, bot, None, None)
            return
    same = [
        pr
        for pr in prs
        if pr["head"]["ref"] == branch
        or (not bump and not rollback_package and marker(pr)["event_id"] == meta["event_id"])
    ]
    require(len(same) <= 1, "duplicate bot PR identities")
    # New PR first, supersession second; a failed create leaves the old PR intact.
    pr = same[0] if same else create_pr(repo, base, patch, branch, meta, bot)
    report["state"] = "pr_open"
    report["pr"] = pr_identity(pr)
    for old in prs:
        old_meta = marker(old)
        if old["number"] != pr["number"] and not bump:
            require(
                old["head"]["sha"] == old_meta["generated_head"],
                "obsolete branch has human commits; not closing it automatically",
            )
            require(old["user"]["login"] == bot, "obsolete PR has unexpected owner")
            api(
                repo,
                f"issues/{old['number']}/comments",
                "POST",
                {
                    "body": f"Superseded by #{pr['number']}; promotion eligibility revoked. "
                    "Branch history is retained."
                },
            )
            api(repo, f"pulls/{old['number']}", "PATCH", {"state": "closed"})
            # The retained PR comment is the human-facing supersession record.
            # One reporting run owns one target and one immutable hub receipt.
    maybe_merge(repo, pr, report, bot, event if not bump else None, manifest)
