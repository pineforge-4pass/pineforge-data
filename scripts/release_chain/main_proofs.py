"""Produce and validate the actual native and live-PyPI main inventories."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

from .contract import HEX40, HEX64, json_bytes, object_json, require, sha256

NATIVE_WORKFLOW = ".github/workflows/native-feed-post.yml"
NATIVE_LEGS = {
    "release-amd64": "Release (ubuntu-24.04)",
    "release-arm64": "Release (ubuntu-24.04-arm)",
    "release-macos": "Release (macos-15)",
    "asan-ubsan": "ASan + UBSan",
    "tsan": "TSan",
}
NETWORK_JOB = "Live PyPI hashes"
NETWORK_NODE = (
    "tests/test_consumer_receipt.py::test_bootstrap_recorded_hashes_match_live_pypi"
)
NATIVE_WORKFLOW_SHA256 = "2f720000c43809ac80e98e6ab7a96f58dff461691bb2c68f0f2fd149e167ffaa"


def object_value(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label}: expected an object")
    return cast(dict[str, Any], value)


def names(value: Any, label: str) -> list[str]:
    require(
        isinstance(value, list)
        and bool(value)
        and all(isinstance(name, str) and name for name in value),
        f"{label}: missing nonempty inventory",
    )
    require(len(value) == len(set(value)), f"{label}: duplicate inventory")
    return cast(list[str], value)


def validate_inventory(value: Any, label: str) -> None:
    row = object_value(value, label)
    registered = names(row.get("registered"), f"{label} registered")
    executed = names(row.get("executed"), f"{label} executed")
    require(set(registered) == set(executed), f"{label}: registration/execution disagree")
    for field in ("skipped", "failures", "errors"):
        require(row.get(field) == [], f"{label}: required rows {field}")
    digest = row.get("junit_sha256")
    require(
        isinstance(digest, str) and HEX64.fullmatch(digest),
        f"{label}: missing raw JUnit SHA256",
    )


def _validate_native_inventory(value: Any, leg: str) -> None:
    label = f"native {leg}"
    require(leg in NATIVE_LEGS, f"{label}: unknown leg")
    validate_inventory(value, label)
    required = {"feed-unit", "feed-export-unit", "feed-mock-export", "feed-mock"}
    if leg == "tsan":
        required.add("feed-tsan-seeded-race")
    observed = set(value["registered"]) & set(value["executed"])
    missing = sorted(required - observed)
    require(
        not missing,
        f"{label}: missing required native rows: {', '.join(missing)}",
    )


def successful_jobs(
    jobs: Any, required: tuple[str, ...], *, run: dict[str, Any]
) -> None:
    require(isinstance(jobs, list), "missing observed jobs")
    for name in required:
        matches = [row for row in jobs if isinstance(row, dict) and row.get("name") == name]
        require(
            len(matches) == 1
            and matches[0].get("status") == "completed"
            and matches[0].get("conclusion") == "success",
            f"required job {name!r} missing, duplicated, failed, cancelled or skipped",
        )
        for field, expected in (
            ("run_id", run.get("id")),
            ("run_attempt", run.get("run_attempt")),
            ("head_sha", run.get("head_sha")),
        ):
            if field in matches[0]:
                require(matches[0][field] == expected, f"{name}: wrong observed {field}")


def validate_identity(
    value: Any, run: dict[str, Any], evidence: dict[str, Any], label: str
) -> None:
    row = object_value(value, label)
    for field, expected in (
        ("run_id", run.get("id")),
        ("attempt", run.get("run_attempt")),
        ("head", evidence.get("head")),
        ("tested_sha", evidence.get("tested_sha")),
        ("tree", evidence.get("tree")),
    ):
        require(expected is not None and row.get(field) == expected, f"{label}: wrong {field}")
    require(
        type(run.get("id")) is int
        and type(run.get("run_attempt")) is int
        and type(row["run_id"]) is int
        and row["run_id"] > 0
        and type(row["attempt"]) is int
        and row["attempt"] > 0,
        f"{label}: invalid run/attempt",
    )
    for field in ("head", "tested_sha", "tree"):
        require(
            isinstance(row[field], str) and HEX40.fullmatch(row[field]),
            f"{label}: invalid {field}",
        )


def validate_main(
    evidence: dict[str, Any], run: dict[str, Any], jobs: list[dict[str, Any]]
) -> None:
    proofs = object_value(evidence.get("main_proofs"), "main_proofs")
    require(set(proofs) == {"native", "network"}, "main requires exactly native/network proofs")
    require(
        run.get("head_branch") == "main"
        and evidence.get("tested_sha") == evidence.get("head"),
        "main proof must test the landed main head",
    )
    network = object_value(proofs["network"], "network")
    validate_identity(network, run, evidence, "network")
    validate_inventory(network, "network")
    require(
        network.get("node_id") == NETWORK_NODE
        and network["registered"] == network["executed"] == [NETWORK_NODE],
        "network: missing unique required live-PyPI row",
    )
    successful_jobs(jobs, (NETWORK_JOB,), run=run)

    native = object_value(proofs["native"], "native")
    observed = object_value(native.get("run"), "native run")
    require(
        native.get("workflow_path") == observed.get("path") == NATIVE_WORKFLOW
        and observed.get("event") == "push"
        and observed.get("head_branch") == "main"
        and observed.get("head_sha") == evidence["head"]
        and observed.get("status") == "completed"
        and observed.get("conclusion") == "success",
        "native: wrong source/workflow or incomplete run",
    )
    successful_jobs(native.get("jobs"), (*NATIVE_LEGS.values(), "Native proof"), run=observed)
    attestation = object_value(native.get("attestation"), "native attestation")
    validate_identity(attestation, observed, evidence, "native")
    legs = object_value(attestation.get("legs"), "native legs")
    require(set(legs) == set(NATIVE_LEGS), "native: missing or unexpected leg")
    for leg in NATIVE_LEGS:
        _validate_native_inventory(legs[leg], leg)


def junit_inventory(path: Path, *, network: bool = False) -> dict[str, Any]:
    raw = path.read_bytes()
    root = ElementTree.fromstring(raw)
    require(root.tag in {"testsuite", "testsuites"}, f"{path}: not a JUnit report")
    cases = list(root.iter("testcase"))
    registered, executed = [], []
    problems: dict[str, list[str]] = {"skipped": [], "failures": [], "errors": []}
    for case in cases:
        name = case.attrib.get("name", "")
        if network:
            classname = case.attrib.get("classname", "")
            require(bool(classname), "network JUnit has no source classname")
            name = f"{classname.replace('.', '/')}.py::{name}"
        registered.append(name)
        require(
            case.get("status") in {None, "run", "passed", "notrun", "disabled"},
            f"{path}: unsuccessful or unknown testcase status",
        )
        if case.find("skipped") is not None or case.get("status") in {"notrun", "disabled"}:
            problems["skipped"].append(name)
        else:
            executed.append(name)
        for tag, field in (("failure", "failures"), ("error", "errors")):
            if case.find(tag) is not None:
                problems[field].append(name)
    row = {
        "registered": registered,
        "executed": executed,
        **problems,
        "junit_sha256": sha256(raw),
    }
    validate_inventory(row, str(path))
    # Collection/setup errors may be reported on a suite rather than a testcase.
    for suite in root.iter():
        if suite.tag in {"testsuite", "testsuites"}:
            if "tests" in suite.attrib:
                require(
                    int(suite.attrib["tests"]) == len(list(suite.iter("testcase"))),
                    f"{path}: suite count disagrees with actual testcase rows",
                )
            for field in ("failures", "errors", "skipped", "disabled"):
                require(int(suite.get(field, "0")) == 0, f"{path}: suite {field}")
            for tag in ("failure", "error", "skipped"):
                require(
                    suite.find(tag) is None, f"{path}: suite contains {tag} element"
                )
    return row


def native_inventory(registration: Path, junit: Path) -> dict[str, Any]:
    raw = registration.read_bytes()
    collected = object_json(raw)
    tests = collected.get("tests")
    require(isinstance(tests, list), "CTest registration has no tests list")
    assert isinstance(tests, list)
    registered = names(
        [object_value(test, "CTest test").get("name") for test in tests], "CTest registration"
    )
    row = junit_inventory(junit)
    row["registered"] = registered
    row["registration_sha256"] = sha256(raw)
    validate_inventory(row, "CTest")
    return row


def local_identity(root: Path, workflow: str) -> dict[str, Any]:
    require(
        os.environ["GITHUB_EVENT_NAME"] == "push"
        and os.environ["GITHUB_REF"] == "refs/heads/main"
        and os.environ["GITHUB_WORKFLOW_REF"].split("@", 1)[0]
        == f"{os.environ['GITHUB_REPOSITORY']}/{workflow}",
        "producer is not the required push/main workflow",
    )
    tested = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
    ).strip()
    require(tested == os.environ["GITHUB_SHA"], "producer checkout differs from event head")
    return {
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "head": os.environ["GITHUB_SHA"],
        "tested_sha": tested,
        "tree": tree,
    }


def require_native_workflow(root: Path) -> None:
    raw = (root / NATIVE_WORKFLOW).read_bytes()
    require(
        sha256(raw) == NATIVE_WORKFLOW_SHA256 and b"continue-on-error" not in raw,
        "native workflow changed: authoritative needs require independent workflow review",
    )


def network_proof(root: Path, directory: Path) -> dict[str, Any]:
    identity = local_identity(root, ".github/workflows/ci-post.yml")
    row = junit_inventory(directory / "network.xml", network=True)
    require(row["registered"] == row["executed"] == [NETWORK_NODE], "wrong live-PyPI inventory")
    return {**identity, "node_id": NETWORK_NODE, **row}


def consume_network(root: Path, directory: Path) -> dict[str, Any]:
    recorded = object_json((directory / "network-proof.json").read_bytes())
    actual = network_proof(root, directory)
    require(recorded == actual, "network report bytes or producer identity changed")
    return actual


def native_leg(root: Path, leg: str, directory: Path) -> dict[str, Any]:
    require_native_workflow(root)
    identity = local_identity(root, NATIVE_WORKFLOW)
    inventory = native_inventory(
        directory / "native-registration.json", directory / "native-junit.xml"
    )
    _validate_native_inventory(inventory, leg)
    return {
        **identity,
        "leg": leg,
        "inventory": inventory,
    }


def native_aggregate(root: Path, directory: Path, needs: dict[str, Any]) -> dict[str, Any]:
    require(set(needs) == {"authz", "main-ref", *NATIVE_LEGS}, "native needs inventory changed")
    require(
        all(isinstance(row, dict) and row.get("result") == "success" for row in needs.values()),
        "native dependency failed, cancelled or skipped",
    )
    require_native_workflow(root)
    identity = local_identity(root, NATIVE_WORKFLOW)
    legs = {}
    for leg in NATIVE_LEGS:
        reports = directory / leg
        recorded = object_json((reports / "native-leg.json").read_bytes())
        actual = native_leg(root, leg, reports)
        require(recorded == actual, f"{leg}: report bytes or producer identity changed")
        require(
            all(recorded.get(key) == value for key, value in identity.items()),
            f"{leg}: stale producer",
        )
        legs[leg] = {
            **actual["inventory"],
            "artifact": f"native-leg-{leg}-{identity['attempt']}",
        }
    # Do not query/wait for this running aggregate's own completed job.
    return {**identity, "legs": legs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("network", "native-leg", "native-aggregate"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--leg", choices=NATIVE_LEGS)
    args = parser.parse_args()
    root = Path.cwd()
    if args.command == "network":
        value = network_proof(root, args.directory)
    elif args.command == "native-leg":
        require(args.leg is not None, "native leg identifier is required")
        value = native_leg(root, args.leg, args.directory)
    else:
        value = native_aggregate(
            root, args.directory, object_json(os.environ["NEEDS_JSON"].encode())
        )
    args.output.write_bytes(json_bytes(value))


if __name__ == "__main__":
    main()
