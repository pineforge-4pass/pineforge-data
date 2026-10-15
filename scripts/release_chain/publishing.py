"""Reuse the validated main CI distributions with the existing OIDC publisher."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from .automation import api, finish, git, inventory, marker, pr_identity, receipt, snapshot
from .contract import (
    fetch,
    gh_bytes,
    json_bytes,
    newest_ready,
    object_json,
    package_version,
    require,
    runtime_pin,
    sha256,
    verify_artifacts,
    verify_local,
)
from .evidence import GateRejected, distribution_hashes, gate_for_run


def pypi_files(version: str) -> dict[str, str] | None:
    try:
        value = object_json(fetch(f"https://pypi.org/pypi/pineforge-data/{version}/json"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return {entry["filename"]: entry["digests"]["sha256"] for entry in value["urls"]}


def stored_receipt(repo: str, version: str) -> dict[str, Any] | None:
    releases = inventory(repo, "releases")
    matching = [release for release in releases if release["tag_name"] == f"v{version}"]
    if not matching:
        return None
    assets = [asset for asset in matching[0]["assets"] if asset["name"] == "consumer-receipt.json"]
    require(len(assets) <= 1, "ambiguous stored completion receipt")
    if not assets:
        return None
    return object_json(
        gh_bytes(
            f"repos/{repo}/releases/assets/{assets[0]['id']}", accept="application/octet-stream"
        )
    )


def recover_applied(repo: str, source: Path, report: dict[str, Any]) -> None:
    """Replay completed receipts or repair a dropped publication transition."""
    version = package_version(source)
    saved = stored_receipt(repo, version)
    if saved:
        require(
            saved["state"] == "verified" and saved["event_id"] == report["event_id"],
            "stored publication identity differs",
        )
        require(
            saved["deployment"]["actual"]["package_version"] == version
            and saved["deployment"]["actual"]["image"] == runtime_pin(source),
            "stored publication does not match installed defaults",
        )
        require(
            pypi_files(version) == saved["deployment"]["actual"]["distributions"],
            "stored publication no longer matches PyPI",
        )
        report.update(saved)
        return
    prs = inventory(repo, "pulls?state=closed&base=main&sort=updated&direction=desc")
    candidates = [
        pr for pr in prs if pr.get("merged_at") and marker(pr) and marker(pr)["version"] == version
    ]
    require(len(candidates) == 1, "applied release has no unique merged update PR")
    pr = candidates[0]
    report["pull_request"] = pr_identity({**pr, "merged": True})
    report["pull_request"]["base"] = marker(pr)["base"]
    runs = api(
        repo,
        "actions/workflows/ci.yml/runs?event=push&per_page=100&head_sha=" + pr["merge_commit_sha"],
    )["workflow_runs"]
    runs = [run for run in runs if run["head_sha"] == pr["merge_commit_sha"]]
    if not runs or max(runs, key=lambda run: run["id"])["status"] != "completed":
        report["state"] = "testing"
        return
    run = max(runs, key=lambda run: run["id"])
    report["verification"]["ci"] = gate_for_run(repo, run["id"], head=pr["merge_commit_sha"])
    publishers = api(repo, "actions/workflows/release.yml/runs?per_page=100")["workflow_runs"]
    if any(publisher["status"] != "completed" for publisher in publishers):
        report["state"] = "deploying"
        return
    # App token needs Actions:write only on this consumer for lost-event repair.
    api(
        repo,
        "actions/workflows/release.yml/dispatches",
        "POST",
        {"ref": "main", "inputs": {"ci_run": str(run["id"]), "bump": "patch"}},
    )
    report["state"] = "deploying"


def current_intent(root: Path, repo: str) -> None:
    """Final desired-state check; never replace a tested pin with latest."""
    git("fetch", "origin", "main")
    with tempfile.TemporaryDirectory() as temporary:
        current = Path(temporary)
        snapshot("origin/main", current)
        require(package_version(current) == package_version(root), "package version superseded")
        require(runtime_pin(current) == runtime_pin(root), "runtime default superseded")
        lock = verify_local(root)
        require(verify_local(current) == lock, "release identity superseded on main")
        if lock:
            if lock.get("rollback"):
                verify_rollback(lock, repo)
            latest = newest_ready()
            require(
                latest is not None
                and (
                    latest[0]["event_id"] == lock["event"]["event_id"]
                    or (
                        lock.get("rollback")
                        and latest[0]["event_id"] in lock.get("rejected_event_ids", [])
                    )
                ),
                "a newer hub release superseded publication",
            )
            verify_artifacts(lock["event"])
    require(api(repo, "")["default_branch"] == "main", "unexpected default branch")


def verify_rollback(lock: dict[str, Any], repo: str) -> None:
    rollback = lock["rollback"]
    saved = stored_receipt(repo, rollback["package_version"])
    require(
        saved is not None and saved["state"] == "verified", "rollback has no known-good receipt"
    )
    assert saved is not None
    actual = saved["deployment"]["actual"]
    require(
        saved["event_id"] == lock["event"]["event_id"]
        and actual["source"] == rollback["source"]
        and actual["package_version"] == rollback["package_version"]
        and rollback["rejected_event_id"] in lock["rejected_event_ids"],
        "rollback provenance differs from the known-good receipt",
    )
    require(
        pypi_files(rollback["package_version"]) == actual["distributions"],
        "known-good rollback package does not verify on PyPI",
    )


def prepare(
    root: Path, repo: str, run_id: int, output: Path, report: dict[str, Any]
) -> dict[str, str]:
    run = api(repo, f"actions/runs/{run_id}")
    require(run["event"] == "push" and run["head_branch"] == "main", "not landed main CI")
    source = run["head_sha"]
    # Check the associated PR before executing or checking out any candidate code.
    prs = api(repo, f"commits/{source}/pulls?per_page=100")
    matches = [
        pr
        for pr in prs
        if pr.get("merged_at")
        and pr["base"]["ref"] == "main"
        and pr["merge_commit_sha"] == source
        and marker(pr)
    ]
    if not matches:
        return {"eligible": "false"}  # Ordinary main pushes are not release requests.
    require(len(matches) == 1, "ambiguous merged release PR")
    pr = api(repo, f"pulls/{matches[0]['number']}")
    meta = marker(pr)
    require(pr["user"]["login"] == os.environ["RELEASE_BOT_LOGIN"], "unexpected release PR owner")
    require(pr["head"]["sha"] == meta["generated_head"], "release PR head changed")
    git("fetch", "origin", source)
    # Keep this process and scripts at the trusted default branch. Read source as data.
    candidate = output / "source"
    candidate.mkdir(parents=True, exist_ok=True)
    snapshot(source, candidate)
    lock = verify_local(candidate)
    event = lock["event"] if lock else None
    report.update(receipt(event, "prepare-publication"))
    report["pull_request"] = pr_identity(pr)
    report["pull_request"]["base"] = meta["base"]
    evidence = gate_for_run(repo, run_id, head=source)
    require(
        git("rev-parse", f"{source}^{{tree}}") == evidence["tree"]
        and evidence["tested_sha"] == source,
        "CI source tree disagrees",
    )
    report["verification"]["ci"] = evidence
    report["deployment"]["preview"] = {"kind": "local-docker", "run_id": run_id}
    report["deployment"]["previous"] = lock["previous"] if lock else None
    current_intent(candidate, repo)
    require(
        package_version(candidate) == meta["version"], "package version differs from release PR"
    )
    if meta["kind"] in {"release", "rollback"}:
        require(
            event is not None and event["event_id"] == meta["event_id"],
            "release event differs from merged PR",
        )
    dist = output / "dist"
    subprocess.run(
        [
            "gh",
            "run",
            "download",
            str(run_id),
            "--repo",
            repo,
            "--name",
            f"python-package-distributions-{run['run_attempt']}",
            "--dir",
            str(dist),
        ],
        check=True,
    )
    require(distribution_hashes(dist) == evidence["distributions"], "CI distribution bytes changed")
    (output / "evidence.json").write_bytes(json_bytes(evidence))
    version = package_version(candidate)
    published = pypi_files(version)
    if published is not None:
        require(published == evidence["distributions"], "PyPI version exists with different bytes")
    report["state"] = "deploying" if published is None else "merged"
    return {
        "eligible": "true",
        "published": str(published is not None).lower(),
        "sha": source,
        "tag": f"v{version}",
        "version": version,
        "ci_run": str(run_id),
    }


def github_release(root: Path, repo: str, directory: Path, report: dict[str, Any]) -> None:
    version = package_version(directory / "source")
    source = report["verification"]["ci"]["head"]
    tag = f"v{version}"
    current_intent(directory / "source", repo)
    # Tags and assets are immutable. A rerun only fills absent assets.
    tags = git("ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    if tags:
        git("fetch", "origin", f"refs/tags/{tag}")
        require(git("rev-parse", f"{tag}^{{commit}}") == source, "existing tag source disagrees")
    releases = inventory(repo, "releases")
    existing = [release for release in releases if release["tag_name"] == tag]
    if not existing:
        subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                repo,
                "--target",
                source,
                "--title",
                f"pineforge-data {tag}",
                "--generate-notes",
                "--latest=false",
            ],
            check=True,
        )
    release = api(repo, f"releases/tags/{tag}")
    assets = {asset["name"]: asset for asset in release["assets"]}
    for path in sorted((directory / "dist").iterdir()):
        if path.name in assets:
            data = gh_bytes(
                f"repos/{repo}/releases/assets/{assets[path.name]['id']}",
                accept="application/octet-stream",
            )
            require(sha256(data) == sha256(path.read_bytes()), "existing release asset differs")
        else:
            subprocess.run(["gh", "release", "upload", tag, str(path), "--repo", repo], check=True)


def verify_publication(repo: str, directory: Path, report: dict[str, Any]) -> None:
    candidate = directory / "source"
    version = package_version(candidate)
    expected = distribution_hashes(directory / "dist")
    require(expected == report["verification"]["ci"]["distributions"], "distribution drift")
    for _attempt in range(12):
        actual = pypi_files(version)
        if actual is not None:
            require(actual == expected, "published PyPI distribution digests disagree")
            break
        time.sleep(10)
    else:
        raise ValueError("PyPI publication has no verifiable distribution inventory")
    # Read both immutable downloads, rather than treating a publish action as verification.
    metadata = object_json(fetch(f"https://pypi.org/pypi/pineforge-data/{version}/json"))
    for entry in metadata["urls"]:
        require(
            entry["url"].startswith("https://files.pythonhosted.org/"),
            "unexpected PyPI download source",
        )
        require(
            sha256(fetch(entry["url"])) == expected[entry["filename"]],
            "PyPI download bytes disagree with tested distribution",
        )
    lock = verify_local(candidate)
    report["deployment"]["actual"] = {
        "source": report["verification"]["ci"]["head"],
        "package_version": version,
        "distributions": expected,
        "image": runtime_pin(candidate),
        "facts": lock["event"]["facts"] if lock else None,
    }
    report["state"] = "verified"
    # The channel pointer only moves after all immutable publication checks succeed.
    current_intent(candidate, repo)
    subprocess.run(["gh", "release", "edit", f"v{version}", "--repo", repo, "--latest"], check=True)
    saved = stored_receipt(repo, version)
    if saved:
        require(
            saved["state"] == "verified"
            and saved["event_id"] == report["event_id"]
            and saved["deployment"]["actual"] == report["deployment"]["actual"],
            "stored completion receipt differs from verified publication",
        )
        report.update(saved)
    else:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "consumer-receipt.json"
            finish(report, path)
            subprocess.run(
                ["gh", "release", "upload", f"v{version}", str(path), "--repo", repo], check=True
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "github-release", "preflight", "verify"))
    parser.add_argument("--run", type=int)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    report = (
        object_json(args.receipt.read_bytes())
        if args.receipt.exists()
        else receipt(None, "publish")
    )
    try:
        args.directory.mkdir(parents=True, exist_ok=True)
        if args.command != "prepare":
            report["verification"]["stage"] = args.command
        if args.command == "prepare":
            outputs = prepare(Path.cwd(), repo, args.run, args.directory, report)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as handle:
                for key, value in outputs.items():
                    handle.write(f"{key}={value}\n")
        elif args.command == "github-release":
            github_release(Path.cwd(), repo, args.directory, report)
        elif args.command == "preflight":
            current_intent(args.directory / "source", repo)
            require(
                distribution_hashes(args.directory / "dist")
                == report["verification"]["ci"]["distributions"],
                "untested upload bytes",
            )
        else:
            verify_publication(repo, args.directory, report)
    except Exception as exc:
        report["state"] = "failed"
        report["verification"]["detail"] = str(exc)
        if isinstance(exc, GateRejected):
            report["verification"]["ci"] = exc.evidence
        raise
    finally:
        finish(report, args.receipt)


if __name__ == "__main__":
    main()
