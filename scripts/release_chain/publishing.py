"""Reuse the validated main CI distributions with the existing OIDC publisher."""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from .automation import api, finish, git, inventory, marker, pr_identity, receipt, snapshot
from .contract import (
    LOCK,
    MANIFEST,
    RUNTIME,
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
from .distributions import filenames, verify_archives
from .evidence import distribution_hashes, gate_for_run
from .receipts import candidate as record_candidate
from .receipts import served, timestamp

PYPI_PROJECTS = ("tunaxis-data", "pineforge-data")


def pypi_files(version: str, project: str | None = None) -> dict[str, str] | None:
    """Return the immutable inventory for both projects, or one named project."""
    projects = (project,) if project else PYPI_PROJECTS
    result: dict[str, str] = {}
    for name in projects:
        try:
            value = object_json(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        result.update({entry["filename"]: entry["digests"]["sha256"] for entry in value["urls"]})
    return result


def pypi_downloads(
    version: str, projects: tuple[str, ...] = PYPI_PROJECTS
) -> tuple[dict[str, str], dict[str, bytes]]:
    """Download and hash the requested PyPI projects, checking their metadata."""
    inventories = [pypi_files(version, project) for project in projects]
    require(
        all(project_inventory is not None for project_inventory in inventories),
        "required PyPI project is missing",
    )
    digests: dict[str, str] = {}
    for project_inventory in inventories:
        assert project_inventory is not None
        digests.update(project_inventory)
    data: dict[str, bytes] = {}
    for project in projects:
        metadata = object_json(fetch(f"https://pypi.org/pypi/{project}/{version}/json"))
        for entry in metadata["urls"]:
            require(
                entry["url"].startswith("https://files.pythonhosted.org/"),
                "unexpected PyPI source",
            )
            raw = fetch(entry["url"])
            require(sha256(raw) == digests[entry["filename"]], "PyPI bytes differ")
            data[entry["filename"]] = raw
    verify_archives(data, version, legacy=projects == ("pineforge-data",))
    return digests, data


def publication_state(version: str, expected: dict[str, str]) -> dict[str, str]:
    """Retain verified uploads and report each project's remaining work."""
    states = {}
    for project in PYPI_PROJECTS:
        tested = {name: expected[name] for name in filenames(project, version)}
        observed = pypi_files(version, project)
        if observed is not None:
            require(
                observed.items() <= tested.items(),
                "PyPI version exists with different bytes",
            )
        states["published_" + project.replace("-", "_")] = str(observed == tested).lower()
    states["published"] = str(all(value == "true" for value in states.values())).lower()
    return states


def candidate_gate(repo: str, pr: dict[str, Any]) -> dict[str, Any]:
    """Re-read the successful PR attempt, never substitute post-merge smoke tests."""
    meta = marker(pr)
    runs = inventory(
        repo,
        "actions/workflows/ci.yml/runs?event=pull_request&head_sha=" + pr["head"]["sha"],
        "workflow_runs",
    )
    runs = [
        run
        for run in runs
        if run["head_sha"] == pr["head"]["sha"]
        and any(item["number"] == pr["number"] for item in run["pull_requests"])
    ]
    require(runs, "pre-merge CI evidence missing")
    run = max(runs, key=lambda row: row["id"])
    evidence = gate_for_run(
        repo, run["id"], head=pr["head"]["sha"], base=meta["base"], pr=pr["number"]
    )
    jobs = inventory(repo, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
    if pr.get("merged_at"):
        require(
            all(job["completed_at"] and job["completed_at"] <= pr["merged_at"] for job in jobs),
            "candidate CI/E2E did not finish before merge",
        )
    return evidence


def verified_package(
    repo: str, version: str, saved: dict[str, Any] | None = None
) -> dict[str, str]:
    """Observe immutable PyPI bytes, matching GitHub assets and tagged source/facts."""
    main_files = pypi_files(version, "tunaxis-data")
    shim_files = pypi_files(version, "pineforge-data")
    legacy = main_files is None and shim_files is not None
    files = (
        shim_files
        if legacy
        else ({**main_files, **shim_files} if main_files and shim_files else None)
    )
    require(
        files is not None
        and len(files) == (2 if legacy else 4)
        and sum(name.endswith(".whl") for name in files) == (1 if legacy else 2)
        and sum(name.endswith(".tar.gz") for name in files) == (1 if legacy else 2),
        "publication needs the required wheel and sdist inventory",
    )
    projects = ("pineforge-data",) if legacy else PYPI_PROJECTS
    observed, downloads = pypi_downloads(version, projects)
    require(observed == files, "PyPI inventory moved during verification")
    release = api(repo, f"releases/tags/v{version}")
    for name, digest in files.items():
        assets = [asset for asset in release["assets"] if asset["name"] == name]
        require(len(assets) == 1, "distribution has no unique immutable release asset")
        require(
            sha256(
                gh_bytes(
                    f"repos/{repo}/releases/assets/{assets[0]['id']}",
                    accept="application/octet-stream",
                )
            )
            == digest,
            "release asset differs from PyPI",
        )
    git("fetch", "origin", f"refs/tags/v{version}")
    source = git("rev-parse", f"v{version}^{{commit}}")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        snapshot(source, root)
        lock = verify_local(root)
        require(
            lock is not None and package_version(root) == version,
            "previous distribution lacks manifest/facts provenance; "
            "establish a baseline before enablement",
        )
        runtime_packages = []
        for wheel_name, wheel in downloads.items():
            if not wheel_name.endswith(".whl"):
                continue
            with zipfile.ZipFile(io.BytesIO(wheel)) as zipped:
                for package in ("tunaxis_data", "pineforge_data"):
                    if f"{package}/{Path(RUNTIME).name}" not in zipped.namelist():
                        continue
                    for name in (LOCK, MANIFEST, RUNTIME):
                        member = f"{package}/{Path(name).name}"
                        require(
                            zipped.namelist().count(member) == 1
                            and zipped.read(member) == (root / name).read_bytes(),
                            "served wheel source/manifest/facts differs from the tagged source",
                        )
                    runtime_packages.append((wheel_name, package))
        require(len(runtime_packages) == 1, "served wheels need one unique runtime package")
        verify_artifacts(lock["event"])
        actual = served(lock["event"], source, version, files)
    if saved:
        require(
            saved["state"] == "verified"
            and saved["pr"]["merge"] == source
            and saved["deployed"] == actual,
            "stored publication no longer verifies",
        )
    return actual


def stored_receipt(repo: str, version: str) -> dict[str, Any] | None:
    releases = inventory(repo, "releases")
    matching = [release for release in releases if release["tag_name"] == f"v{version}"]
    if not matching:
        return None
    assets = [asset for asset in matching[0]["assets"] if asset["name"] == "release-receipt.json"]
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
            verified_package(repo, version, saved)["image_digest"]
            == verify_local(source)["event"]["image"]["digest"],
            "stored publication does not match installed defaults",
        )
        report.update(saved)
        return
    prs = inventory(repo, "pulls?state=closed&base=main&sort=updated&direction=desc")
    candidates = [
        pr for pr in prs if pr.get("merged_at") and marker(pr) and marker(pr)["version"] == version
    ]
    require(len(candidates) == 1, "applied release has no unique merged update PR")
    pr = candidates[0]
    report["pr"] = pr_identity({**pr, "merged": True})
    report["pr"]["base_sha"] = marker(pr)["base"]
    runs = api(
        repo,
        "actions/workflows/ci.yml/runs?event=push&per_page=100&head_sha=" + pr["merge_commit_sha"],
    )["workflow_runs"]
    runs = [run for run in runs if run["head_sha"] == pr["merge_commit_sha"]]
    if not runs or max(runs, key=lambda run: run["id"])["status"] != "completed":
        report["state"] = "testing"
        return
    run = max(runs, key=lambda run: run["id"])
    gate_for_run(repo, run["id"], head=pr["merge_commit_sha"])
    lock = verify_local(source)
    if lock:
        record_candidate(report, lock["event"], candidate_gate(repo, pr), version)
    publishers = api(repo, "actions/workflows/release.yml/runs?per_page=100")["workflow_runs"]
    matching = [row for row in publishers if row["head_sha"] == pr["merge_commit_sha"]]
    if any(row["status"] != "completed" for row in matching):
        report["state"] = "deploying"
        return
    completed = [
        row
        for row in matching
        if row["conclusion"] == "success"
        and any(
            artifact["name"] == f"release-stage-{row['run_attempt']}-50-verify"
            and not artifact["expired"]
            for artifact in inventory(repo, f"actions/runs/{row['id']}/artifacts", "artifacts")
        )
    ]
    if completed:
        # Repair a lost completion event without repeating OIDC publication.
        api(
            repo,
            "actions/workflows/pineforge-release.yml/dispatches",
            "POST",
            {
                "ref": f"v{version}",
                "inputs": {"publication_run": str(max(completed, key=lambda row: row["id"])["id"])},
            },
        )
        report.update(receipt(None))  # The pinned reporting run owns the next snapshot.
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
    actual = verified_package(repo, rollback["package_version"], saved)
    require(
        saved["event_id"] == lock["event"]["event_id"]
        and actual["source"] == rollback["source"]
        and rollback["rejected_event_id"] in lock["rejected_event_ids"],
        "rollback provenance differs from the known-good receipt",
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
    report["pr"] = pr_identity(pr)
    report["pr"]["base_sha"] = meta["base"]
    evidence = gate_for_run(repo, run_id, head=source)
    require(
        git("rev-parse", f"{source}^{{tree}}") == evidence["tree"]
        and evidence["tested_sha"] == source,
        "CI source tree disagrees",
    )
    # Retain the independent pre-merge candidate gates, as required by the hub.
    # Main CI still authorizes the exact distributions we publish.
    if event:
        record_candidate(report, event, candidate_gate(repo, pr), meta["version"])
        previous_version = lock["previous"]["package_version"]
        report["previous"] = verified_package(repo, previous_version)
    require(
        git("rev-parse", f"{source}^1") == meta["base"],
        "landed first parent differs from the tested base",
    )
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
    published = publication_state(version, evidence["distributions"])
    report["state"] = "merged" if published["published"] == "true" else "deploying"
    return {
        "eligible": "true",
        **published,
        "sha": source,
        "tag": f"v{version}",
        "version": version,
        "ci_run": str(run_id),
        "has_target": str(event is not None).lower(),
    }


def github_release(root: Path, repo: str, directory: Path, report: dict[str, Any]) -> None:
    version = package_version(directory / "source")
    source = object_json((directory / "evidence.json").read_bytes())["head"]
    require(
        report["pr"] is None or report["pr"]["merge"] == source,
        "distribution source differs from recorded merge",
    )
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
                f"tunaxis-data and pineforge-data shim {tag}",
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
    gate = object_json((directory / "evidence.json").read_bytes())
    require(expected == gate["distributions"], "distribution drift")
    for _attempt in range(12):
        actual = pypi_files(version)
        if actual is not None:
            require(actual == expected, "published PyPI distribution digests disagree")
            break
        time.sleep(10)
    else:
        raise ValueError("PyPI publication has no verifiable distribution inventory")
    # Read both immutable downloads, rather than treating a publish action as verification.
    observed, _downloads = pypi_downloads(version)
    require(observed == expected, "PyPI download bytes disagree with tested distribution")
    lock = verify_local(candidate)
    if lock:
        report["deployed"] = served(lock["event"], report["pr"]["merge"], version, expected)
        require(report["previous"] is not None, "previous distribution identity missing")
    # Only the later, completed-run coordinator can claim verified and store a receipt.
    report["state"] = "deploying"
    report["timestamp"] = timestamp()
    current_intent(candidate, repo)
    subprocess.run(["gh", "release", "edit", f"v{version}", "--repo", repo, "--latest"], check=True)


def stage_distributions(directory: Path) -> None:
    """Split verified artifacts inside the bundle mounted by container actions."""
    version = package_version(directory / "source")
    for project in PYPI_PROJECTS:
        destination = directory / "pypi" / project
        destination.mkdir(parents=True, exist_ok=True)
        for name in filenames(project, version):
            shutil.copyfile(directory / "dist" / name, destination / name)


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
        if args.command == "prepare":
            outputs = prepare(Path.cwd(), repo, args.run, args.directory, report)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as handle:
                for key, value in outputs.items():
                    handle.write(f"{key}={value}\n")
        elif args.command == "github-release":
            github_release(Path.cwd(), repo, args.directory, report)
        elif args.command == "preflight":
            current_intent(args.directory / "source", repo)
            hashes = distribution_hashes(args.directory / "dist")
            require(
                hashes
                == object_json((args.directory / "evidence.json").read_bytes())["distributions"],
                "untested upload bytes",
            )
            publication_state(package_version(args.directory / "source"), hashes)
            stage_distributions(args.directory)
        else:
            verify_publication(repo, args.directory, report)
    except Exception:
        report["state"] = "failed"
        raise
    finally:
        finish(report, args.receipt)


if __name__ == "__main__":
    main()
