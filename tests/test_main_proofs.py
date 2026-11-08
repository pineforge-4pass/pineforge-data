"""Authored report fixtures; these controls do not execute CTest or live PyPI."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest
from release_chain import contract, evidence, main_proofs

ROOT = Path(__file__).resolve().parents[1]
NETWORK_NODE = (
    "tests/test_consumer_receipt.py::test_bootstrap_recorded_hashes_match_live_pypi"
)
NATIVE_LEGS = ("release-amd64", "release-arm64", "release-macos", "asan-ubsan", "tsan")
NATIVE_WORKFLOW = ".github/workflows/native-feed-post.yml"
# Required subsets declared independently from the validators, from native/feed/CMakeLists.txt.
COMMON_NATIVE_ROWS = ("feed-unit", "feed-export-unit", "feed-mock-export", "feed-mock")
REQUIRED_NATIVE_ROWS = {
    leg: COMMON_NATIVE_ROWS + (("feed-tsan-seeded-race",) if leg == "tsan" else ())
    for leg in NATIVE_LEGS
}
REQUIRED_NATIVE_OMISSIONS = [
    pytest.param(leg, row, id=f"{leg}-without-{row}")
    for leg, rows in REQUIRED_NATIVE_ROWS.items()
    for row in rows
]
NATIVE_JOB_NAMES = (
    "Release (ubuntu-24.04)", "Release (ubuntu-24.04-arm)", "Release (macos-15)",
    "ASan + UBSan", "TSan", "Native proof",
)


def native_report_names(leg: str) -> list[str]:
    # Distinct extra rows keep cross-leg report substitution observable.
    return [*REQUIRED_NATIVE_ROWS[leg], f"{leg}-unit", f"{leg}-integration"]


def write_junit(path: Path, names: list[str], *, network: bool = False) -> bytes:
    """Minimal pytest/CTest-shaped XML, with counters tied to actual fixture rows."""
    root = ElementTree.Element("testsuites", tests=str(len(names)))
    suite = ElementTree.SubElement(
        root, "testsuite", tests=str(len(names)), failures="0", errors="0", skipped="0"
    )
    for name in names:
        attributes = {"name": name}
        if network:
            attributes["classname"] = "tests.test_consumer_receipt"
        ElementTree.SubElement(suite, "testcase", **attributes)
    raw = ElementTree.tostring(root)
    path.write_bytes(raw)
    return raw


def write_registration(path: Path, names: list[str]) -> bytes:
    raw = json.dumps(
        {"kind": "ctestInfo", "version": {"major": 1, "minor": 0},
         "tests": [{"name": name} for name in names]},
        sort_keys=True,
    ).encode()
    path.write_bytes(raw)
    return raw


def expected_inventory(names: list[str], raw: bytes) -> dict:
    return {
        "registered": names, "executed": list(names),
        "skipped": [], "failures": [], "errors": [],
        "junit_sha256": hashlib.sha256(raw).hexdigest(),
    }


def expected_native_inventory(directory: Path, names: list[str]) -> dict:
    return {
        **expected_inventory(names, (directory / "native-junit.xml").read_bytes()),
        "registration_sha256": hashlib.sha256(
            (directory / "native-registration.json").read_bytes()
        ).hexdigest(),
    }


def write_native_report_pair(directory: Path, names: list[str]) -> dict:
    registered_raw = write_registration(directory / "native-registration.json", names)
    junit_raw = write_junit(directory / "native-junit.xml", names)
    return {
        **expected_inventory(names, junit_raw),
        "registration_sha256": hashlib.sha256(registered_raw).hexdigest(),
    }


def record_native_leg(
    directory: Path, identity: dict, leg: str, inventory: dict
) -> None:
    """Record authored report content, without asking a producer to accept it."""
    (directory / "native-leg.json").write_bytes(contract.json_bytes({
        **identity, "leg": leg, "inventory": inventory,
    }))


@pytest.mark.parametrize("network", [False, True], ids=["ctest", "pytest-network"])
@pytest.mark.parametrize("status", [None, "run", "passed"])
def test_junit_inventory_uses_actual_rows_and_raw_digest(tmp_path, network, status):
    names = (
        ["test_bootstrap_recorded_hashes_match_live_pypi"] if network
        else ["native-unit-one", "native-unit-two"]
    )
    path = tmp_path / "report.xml"
    write_junit(path, names, network=network)
    root = ElementTree.fromstring(path.read_bytes())
    if status is not None:
        for case in root.iter("testcase"):
            case.set("status", status)
    raw = ElementTree.tostring(root)
    path.write_bytes(raw)
    actual_names = [NETWORK_NODE] if network else names
    assert main_proofs.junit_inventory(path, network=network) == expected_inventory(
        actual_names, raw
    )
    # An input-dependent second report prevents a constant inventory/digest twin.
    changed_names = names[:1] if not network else [names[0] + "_different"]
    changed_raw = write_junit(path, changed_names, network=network)
    changed = main_proofs.junit_inventory(path, network=network)
    expected_names = [NETWORK_NODE + "_different"] if network else changed_names
    assert changed == expected_inventory(expected_names, changed_raw)
    assert changed["junit_sha256"] != hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("empty", "missing nonempty inventory"),
        ("empty-name", "missing nonempty inventory"),
        ("duplicate", "duplicate inventory"),
        ("skipped", "registration/execution disagree|required rows skipped"),
        ("failure", "required rows failures"),
        ("error", "required rows errors"),
        ("notrun", "registration/execution disagree|required rows skipped"),
        ("disabled", "registration/execution disagree|required rows skipped"),
        ("unknown-status", "unsuccessful or unknown testcase status"),
        ("omitted-row", "suite count disagrees"),
        ("suite-overcount", "suite count disagrees"),
        ("suite-undercount", "suite count disagrees"),
        ("parent-overcount", "suite count disagrees"),
        ("suite-skipped", "suite skipped"),
        ("suite-failures", "suite failures"),
        ("suite-errors", "suite errors"),
        ("suite-disabled", "suite disabled"),
        ("collection-error", "suite errors"),
    ],
)
def test_junit_inventory_refuses_non_full_reports(tmp_path, fault, reason):
    path = tmp_path / "report.xml"
    names = [] if fault == "empty" else ["native-one", "native-two"]
    write_junit(path, names)
    root = ElementTree.fromstring(path.read_bytes())
    suite = root.find("testsuite")
    cases = list(root.iter("testcase"))
    if fault == "empty-name":
        cases[0].set("name", "")
    elif fault == "duplicate":
        cases[1].set("name", cases[0].get("name"))
    elif fault in {"skipped", "failure", "error"}:
        ElementTree.SubElement(cases[0], fault, message="authored unsuccessful row")
    elif fault in {"notrun", "disabled"}:
        cases[0].set("status", fault)
    elif fault == "unknown-status":
        cases[0].set("status", "failed")
    elif fault == "omitted-row":
        suite.remove(cases[-1])
    elif fault in {"suite-overcount", "suite-undercount"}:
        suite.set("tests", "3" if fault == "suite-overcount" else "1")
    elif fault == "parent-overcount":
        root.set("tests", "3")
    elif fault.startswith("suite-"):
        suite.set(fault.removeprefix("suite-"), "1")
    elif fault == "collection-error":
        suite.set("errors", "1")
        ElementTree.SubElement(suite, "error", message="collection failed")
    path.write_bytes(ElementTree.tostring(root))
    with pytest.raises(ValueError, match=reason):
        main_proofs.junit_inventory(path)


@pytest.mark.parametrize("parent", ["testsuite", "testsuites"])
@pytest.mark.parametrize("tag", ["error", "failure", "skipped"])
def test_junit_inventory_refuses_suite_error_even_when_counter_claims_zero(
    tmp_path, parent, tag
):
    path = tmp_path / "collection.xml"
    write_junit(path, ["collected-before-error"])
    root = ElementTree.fromstring(path.read_bytes())
    container = root if parent == "testsuites" else root.find("testsuite")
    ElementTree.SubElement(container, tag, message="authored suite problem")
    path.write_bytes(ElementTree.tostring(root))
    # A collection error is not made successful by an inconsistent zero counter.
    with pytest.raises(ValueError) as rejected:
        main_proofs.junit_inventory(path)
    assert str(rejected.value) == f"{path}: suite contains {tag} element"


@pytest.mark.parametrize("parent", ["testsuite", "testsuites"])
@pytest.mark.parametrize("content", ["system-out", "system-err", "text"])
@pytest.mark.parametrize(
    "names", [["unit-alpha"], ["unit-alpha", "unit-beta"]], ids=["one-row", "two-rows"]
)
def test_junit_inventory_accepts_harmless_suite_logs_and_text(
    tmp_path, parent, content, names
):
    path = tmp_path / "logs.xml"
    original = write_junit(path, names)
    root = ElementTree.fromstring(original)
    container = root if parent == "testsuites" else root.find("testsuite")
    message = "error/failure/skipped are harmless words in captured output"
    if content == "text":
        container.text = message
    else:
        ElementTree.SubElement(container, content).text = message
    raw = ElementTree.tostring(root)
    path.write_bytes(raw)
    assert main_proofs.junit_inventory(path) == expected_inventory(names, raw)
    assert raw != original


@pytest.mark.parametrize("raw", [b"", b"<testsuite>", b"not XML"])
def test_junit_inventory_refuses_malformed_xml(tmp_path, raw):
    path = tmp_path / "malformed.xml"
    path.write_bytes(raw)
    with pytest.raises(ElementTree.ParseError):
        main_proofs.junit_inventory(path)


def test_junit_inventory_requires_a_report_and_junit_root(tmp_path):
    # File absence and malformed syntax are I/O/parser refusals, not semantic results.
    with pytest.raises(FileNotFoundError):
        main_proofs.junit_inventory(tmp_path / "absent.xml")
    path = tmp_path / "wrong-root.xml"
    path.write_bytes(b'<results><testcase name="unit"/></results>')
    with pytest.raises(ValueError, match="not a JUnit report"):
        main_proofs.junit_inventory(path)


@pytest.mark.parametrize("names", [["unit-alpha"], ["unit-alpha", "unit-beta"]])
def test_native_inventory_consumes_registration_and_junit(tmp_path, names):
    registration, junit = tmp_path / "registration.json", tmp_path / "junit.xml"
    registered_raw = write_registration(registration, list(reversed(names)))
    junit_raw = write_junit(junit, names)
    expected = expected_inventory(names, junit_raw)
    expected["registered"] = list(reversed(names))
    expected["registration_sha256"] = hashlib.sha256(registered_raw).hexdigest()
    assert main_proofs.native_inventory(registration, junit) == expected


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("empty-registration", "missing nonempty inventory"),
        ("duplicate-registration", "duplicate inventory"),
        ("omitted-registration", "registration/execution disagree"),
        ("omitted-execution", "registration/execution disagree"),
        ("extra-execution", "registration/execution disagree"),
        ("empty-execution", "missing nonempty inventory"),
    ],
)
def test_native_inventory_refuses_registration_execution_drift(tmp_path, fault, reason):
    registered, executed = ["unit-alpha", "unit-beta"], ["unit-alpha", "unit-beta"]
    if fault == "empty-registration":
        registered = []
    elif fault == "duplicate-registration":
        registered = ["unit-alpha", "unit-alpha"]
    elif fault == "omitted-registration":
        registered = registered[:1]
    elif fault == "omitted-execution":
        executed = executed[:1]
    elif fault == "extra-execution":
        executed.append("unregistered")
    else:
        executed = []
    registration, junit = tmp_path / "registration.json", tmp_path / "junit.xml"
    write_registration(registration, registered)
    write_junit(junit, executed)
    with pytest.raises(ValueError, match=reason):
        main_proofs.native_inventory(registration, junit)


@pytest.mark.parametrize(
    "collected,reason",
    [
        ({}, "CTest registration has no tests list"),
        ({"tests": None}, "CTest registration has no tests list"),
        ({"tests": {}}, "CTest registration has no tests list"),
        ({"tests": [None]}, "CTest test: expected an object"),
        ({"tests": [{}]}, "missing nonempty inventory"),
    ],
)
def test_native_inventory_refuses_malformed_registration(tmp_path, collected, reason):
    registration, junit = tmp_path / "registration.json", tmp_path / "junit.xml"
    registration.write_text(json.dumps(collected))
    write_junit(junit, ["unit-alpha"])
    with pytest.raises(ValueError, match=reason):
        main_proofs.native_inventory(registration, junit)


@pytest.mark.parametrize("missing", ["registration", "junit"])
def test_native_inventory_requires_both_report_files(tmp_path, missing):
    registration, junit = tmp_path / "registration.json", tmp_path / "junit.xml"
    if missing != "registration":
        write_registration(registration, ["unit-alpha"])
    if missing != "junit":
        write_junit(junit, ["unit-alpha"])
    absent = registration if missing == "registration" else junit
    with pytest.raises(FileNotFoundError) as rejected:
        main_proofs.native_inventory(registration, junit)
    assert rejected.value.filename == str(absent)


@pytest.fixture
def producer_repository(tmp_path, monkeypatch):
    """A normal local test repository; no hook/config/signing overrides."""
    root = tmp_path / "repository"
    workflows = root / ".github/workflows"
    workflows.mkdir(parents=True)
    for name in ("ci-post.yml", "native-feed-post.yml"):
        (workflows / name).write_bytes((ROOT / ".github/workflows" / name).read_bytes())
    commands = [
        ["git", "init", "-b", "main"],
        ["git", "add", ".github/workflows/ci-post.yml", NATIVE_WORKFLOW],
        ["git", "-c", "user.name=Producer control",
         "-c", "user.email=producer-control@example.invalid",
         "commit", "-m", "Unit producer identity"],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
    ).strip()
    for name, value in {
        "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "owner/data",
        "GITHUB_WORKFLOW_REF": "owner/data/.github/workflows/ci-post.yml@refs/heads/main",
        "GITHUB_SHA": head, "GITHUB_RUN_ID": "731", "GITHUB_RUN_ATTEMPT": "3",
    }.items():
        monkeypatch.setenv(name, value)
    return root, {
        "run_id": 731, "attempt": 3, "head": head, "tested_sha": head, "tree": tree,
    }


@pytest.fixture
def network_reports(producer_repository, tmp_path):
    root, identity = producer_repository
    directory = tmp_path / "network-reports"
    directory.mkdir()
    raw = write_junit(
        directory / "network.xml",
        ["test_bootstrap_recorded_hashes_match_live_pypi"],
        network=True,
    )
    proof = main_proofs.network_proof(root, directory)
    (directory / "network-proof.json").write_bytes(contract.json_bytes(proof))
    return root, directory, identity, raw


def test_network_producer_and_consumer_bind_actual_report(network_reports):
    root, directory, identity, raw = network_reports
    expected = {
        **identity, "node_id": NETWORK_NODE,
        **expected_inventory([NETWORK_NODE], raw),
    }
    assert main_proofs.network_proof(root, directory) == expected
    assert main_proofs.consume_network(root, directory) == expected


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("missing-classname", "network JUnit has no source classname"),
        ("wrong-classname", "wrong live-PyPI inventory"),
        ("wrong-node", "wrong live-PyPI inventory"),
        ("duplicate-node", "duplicate inventory"),
        ("omitted-node", "missing nonempty inventory"),
        ("skipped-node", "executed: missing nonempty inventory"),
        ("failed-node", "required rows failures"),
        ("errored-node", "required rows errors"),
    ],
)
def test_network_proof_refuses_wrong_source_or_non_full_row(network_reports, fault, reason):
    root, directory, _identity, _raw = network_reports
    path = directory / "network.xml"
    xml = ElementTree.fromstring(path.read_bytes())
    suite, case = xml.find("testsuite"), xml.find(".//testcase")
    if fault == "missing-classname":
        case.attrib.pop("classname")
    elif fault == "wrong-classname":
        case.set("classname", "tests.test_release_adapter")
    elif fault == "wrong-node":
        case.set("name", "test_an_unrelated_network_request")
    elif fault == "duplicate-node":
        suite.append(copy.deepcopy(case))
        suite.set("tests", "2")
        xml.set("tests", "2")
    elif fault == "omitted-node":
        suite.remove(case)
        suite.set("tests", "0")
        xml.set("tests", "0")
    else:
        tag = {"skipped-node": "skipped", "failed-node": "failure", "errored-node": "error"}[fault]
        ElementTree.SubElement(case, tag, message="authored network refusal")
    path.write_bytes(ElementTree.tostring(xml))
    with pytest.raises(ValueError, match=reason):
        main_proofs.network_proof(root, directory)


@pytest.mark.parametrize(
    "name,value,reason",
    [
        ("GITHUB_EVENT_NAME", "pull_request", "required push/main workflow"),
        ("GITHUB_REF", "refs/heads/topic", "required push/main workflow"),
        ("GITHUB_REPOSITORY", "other/data", "required push/main workflow"),
        ("GITHUB_WORKFLOW_REF", "owner/data/.github/workflows/ci.yml@refs/heads/main",
         "required push/main workflow"),
        ("GITHUB_SHA", "e" * 40, "checkout differs from event head"),
    ],
)
def test_network_proof_refuses_wrong_producer_source(
    network_reports, monkeypatch, name, value, reason
):
    root, directory, _identity, _raw = network_reports
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=reason):
        main_proofs.network_proof(root, directory)


@pytest.mark.parametrize(
    "field", ["run_id", "attempt", "head", "tested_sha", "tree", "junit_sha256", "node_id"]
)
def test_consume_network_refuses_recorded_identity_or_digest_tampering(network_reports, field):
    root, directory, _identity, _raw = network_reports
    path = directory / "network-proof.json"
    proof = contract.object_json(path.read_bytes())
    original = proof[field]
    proof[field] = original + 1 if type(original) is int else (
        hashlib.sha256(f"tampered:{field}".encode()).hexdigest()[:len(original)]
    )
    path.write_bytes(contract.json_bytes(proof))
    with pytest.raises(ValueError, match="network report bytes or producer identity changed"):
        main_proofs.consume_network(root, directory)


def test_consume_network_refuses_changed_raw_bytes_with_unchanged_rows(network_reports):
    root, directory, _identity, _raw = network_reports
    path = directory / "network.xml"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="network report bytes or producer identity changed"):
        main_proofs.consume_network(root, directory)


@pytest.mark.parametrize("field", ["registered", "executed", "skipped", "failures", "errors"])
def test_consume_network_refuses_recorded_inventory_tampering(network_reports, field):
    root, directory, _identity, _raw = network_reports
    path = directory / "network-proof.json"
    proof = contract.object_json(path.read_bytes())
    proof[field] = [] if field in {"registered", "executed"} else [NETWORK_NODE]
    path.write_bytes(contract.json_bytes(proof))
    with pytest.raises(ValueError, match="network report bytes or producer identity changed"):
        main_proofs.consume_network(root, directory)


@pytest.mark.parametrize("missing", ["network.xml", "network-proof.json"])
def test_consume_network_requires_both_report_files(network_reports, tmp_path, missing):
    root, directory, _identity, _raw = network_reports
    incomplete = tmp_path / "incomplete-network"
    incomplete.mkdir()
    for name in ("network.xml", "network-proof.json"):
        if name != missing:
            (incomplete / name).write_bytes((directory / name).read_bytes())
    with pytest.raises(FileNotFoundError) as rejected:
        main_proofs.consume_network(root, incomplete)
    assert rejected.value.filename == str(incomplete / missing)


@pytest.mark.parametrize("name", ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"])
def test_consume_network_refuses_previous_run_or_attempt(network_reports, monkeypatch, name):
    root, directory, identity, _raw = network_reports
    previous = identity["run_id" if name == "GITHUB_RUN_ID" else "attempt"]
    monkeypatch.setenv(name, str(previous + 1))
    with pytest.raises(ValueError, match="network report bytes or producer identity changed"):
        main_proofs.consume_network(root, directory)


@pytest.fixture
def native_reports(producer_repository, tmp_path, monkeypatch):
    root, identity = producer_repository
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF", f"owner/data/{NATIVE_WORKFLOW}@refs/heads/main"
    )
    directory = tmp_path / "native-reports"
    directory.mkdir()
    for leg in NATIVE_LEGS:
        reports = directory / leg
        reports.mkdir()
        # Different reports in each leg make accidental report substitution observable.
        names = native_report_names(leg)
        write_registration(reports / "native-registration.json", names)
        write_junit(reports / "native-junit.xml", names)
        (reports / "native-leg.json").write_bytes(
            contract.json_bytes(main_proofs.native_leg(root, leg, reports))
        )
    needs = {name: {"result": "success"} for name in ("authz", "main-ref", *NATIVE_LEGS)}
    return root, directory, identity, needs


def test_native_leg_and_aggregate_consume_real_files_and_authoritative_needs(
    native_reports, monkeypatch
):
    root, directory, identity, needs = native_reports
    api_calls = []

    def reject_job_api(*args, **kwargs):
        api_calls.append((args, kwargs))
        raise AssertionError("a running native aggregate must not query its own job")

    monkeypatch.setattr(contract, "gh_bytes", reject_job_api)
    monkeypatch.setattr(evidence, "gh_bytes", reject_job_api)
    git_reads = []
    real_check_output = subprocess.check_output

    def observe_identity_read(command, **kwargs):
        git_reads.append(command)
        assert command in (
            ["git", "rev-parse", "HEAD"], ["git", "rev-parse", "HEAD^{tree}"]
        ), "native aggregation may read local identity, never query a completed job"
        assert kwargs == {"cwd": root, "text": True}
        return real_check_output(command, **kwargs)

    monkeypatch.setattr(main_proofs.subprocess, "check_output", observe_identity_read)
    result = main_proofs.native_aggregate(root, directory, needs)
    expected_legs = {}
    for leg in NATIVE_LEGS:
        reports = directory / leg
        inventory = expected_native_inventory(reports, native_report_names(leg))
        assert main_proofs.native_leg(root, leg, reports) == {
            **identity, "leg": leg, "inventory": inventory,
        }
        expected_legs[leg] = {
            **inventory, "artifact": f"native-leg-{leg}-{identity['attempt']}",
        }
    assert result == {**identity, "legs": expected_legs}
    assert api_calls == []
    assert git_reads
    assert "Native proof" not in needs


@pytest.mark.parametrize("leg", NATIVE_LEGS)
def test_native_required_subset_accepts_extra_rows_and_binds_their_bytes(native_reports, leg):
    root, directory, identity, needs = native_reports
    reports = directory / leg
    required = list(REQUIRED_NATIVE_ROWS[leg])
    minimum = write_native_report_pair(reports, required)
    record_native_leg(reports, identity, leg, minimum)
    assert main_proofs.native_leg(root, leg, reports) == {
        **identity, "leg": leg, "inventory": minimum,
    }
    before = main_proofs.native_aggregate(root, directory, needs)
    artifact = f"native-leg-{leg}-{identity['attempt']}"
    assert before["legs"][leg] == {**minimum, "artifact": artifact}

    # Represent CTest's WILL_FAIL success with a normal, unskipped fixture row.
    if leg == "tsan":
        assert "feed-tsan-seeded-race" in minimum["executed"]
    else:
        assert "feed-tsan-seeded-race" not in minimum["registered"]
    extended = write_native_report_pair(reports, native_report_names(leg))
    record_native_leg(reports, identity, leg, extended)
    assert main_proofs.native_leg(root, leg, reports) == {
        **identity, "leg": leg, "inventory": extended,
    }
    after = main_proofs.native_aggregate(root, directory, needs)
    assert after == {
        **before, "legs": {**before["legs"], leg: {**extended, "artifact": artifact}},
    }
    assert extended["executed"] != minimum["executed"]
    for field in ("registration_sha256", "junit_sha256"):
        assert extended[field] != minimum[field]


@pytest.mark.parametrize("leg,missing", REQUIRED_NATIVE_OMISSIONS)
@pytest.mark.parametrize("producer", ["native-leg", "native-aggregate"])
def test_native_producers_refuse_mutually_reduced_required_coverage(
    native_reports, leg, missing, producer
):
    root, directory, identity, needs = native_reports
    reports = directory / leg
    complete = expected_native_inventory(reports, native_report_names(leg))
    assert main_proofs.native_leg(root, leg, reports) == {
        **identity, "leg": leg, "inventory": complete,
    }
    positive = main_proofs.native_aggregate(root, directory, needs)
    assert positive["legs"][leg] == {
        **complete, "artifact": f"native-leg-{leg}-{identity['attempt']}",
    }

    names = [name for name in native_report_names(leg) if name != missing]
    reduced = write_native_report_pair(reports, names)
    record_native_leg(reports, identity, leg, reduced)
    # Both inventories agree, counters are honest, and the recorded hashes are current.
    assert names and missing not in names
    assert main_proofs.native_inventory(
        reports / "native-registration.json", reports / "native-junit.xml"
    ) == reduced
    for field in ("registration_sha256", "junit_sha256"):
        assert reduced[field] != complete[field]
    with pytest.raises(ValueError) as rejected:
        if producer == "native-leg":
            main_proofs.native_leg(root, leg, reports)
        else:
            main_proofs.native_aggregate(root, directory, needs)
    assert str(rejected.value) == f"native {leg}: missing required native rows: {missing}"


@pytest.fixture
def complete_main_reports(native_reports, tmp_path, monkeypatch):
    """Real local producers supply both proofs; job/run observations are offline inputs."""
    root, directory, identity, needs = native_reports
    network_directory = tmp_path / "main-network"
    network_directory.mkdir()
    network_raw = write_junit(
        network_directory / "network.xml",
        ["test_bootstrap_recorded_hashes_match_live_pypi"],
        network=True,
    )
    python_identity = {
        **identity, "run_id": identity["run_id"] + 10, "attempt": identity["attempt"] + 1,
    }
    with monkeypatch.context() as network_environment:
        network_environment.setenv(
            "GITHUB_WORKFLOW_REF", "owner/data/.github/workflows/ci-post.yml@refs/heads/main"
        )
        network_environment.setenv("GITHUB_RUN_ID", str(python_identity["run_id"]))
        network_environment.setenv("GITHUB_RUN_ATTEMPT", str(python_identity["attempt"]))
        network = main_proofs.network_proof(root, network_directory)
        expected_network = {
            **python_identity, "node_id": NETWORK_NODE,
            **expected_inventory([NETWORK_NODE], network_raw),
        }
        assert network == expected_network
        (network_directory / "network-proof.json").write_bytes(contract.json_bytes(network))
        assert main_proofs.consume_network(root, network_directory) == expected_network

    native = main_proofs.native_aggregate(root, directory, needs)
    expected_legs = {
        leg: {
            **expected_native_inventory(directory / leg, native_report_names(leg)),
            "artifact": f"native-leg-{leg}-{identity['attempt']}",
        }
        for leg in NATIVE_LEGS
    }
    assert native == {**identity, "legs": expected_legs}
    run = {
        "id": python_identity["run_id"], "run_attempt": python_identity["attempt"],
        "head_sha": identity["head"], "head_branch": "main", "event": "push",
        "path": ".github/workflows/ci-post.yml",
        "status": "completed", "conclusion": "success",
    }
    native_run = {
        **run, "id": identity["run_id"], "run_attempt": identity["attempt"],
        "path": NATIVE_WORKFLOW,
    }

    def observed_jobs(names, observation):
        return [
            {
                "name": name, "status": "completed", "conclusion": "success",
                "run_id": observation["id"], "run_attempt": observation["run_attempt"],
                "head_sha": observation["head_sha"],
            }
            for name in names
        ]

    jobs = observed_jobs((*evidence.JOBS, "release/ready", "Live PyPI hashes"), run)
    proof = {
        **python_identity, "event": "push", "base": None, "pr": None,
        "jobs": dict.fromkeys(evidence.JOBS, "success"),
        "e2e": {suite: sorted(rows) for suite, rows in evidence.TESTS.items()},
        "main_proofs": {
            "network": network,
            "native": {
                "workflow_path": NATIVE_WORKFLOW, "run": native_run,
                "jobs": observed_jobs(NATIVE_JOB_NAMES, native_run), "attestation": native,
            },
        },
    }
    return root, directory, identity, proof, run, jobs


def test_validate_main_accepts_complete_local_proofs_and_observed_jobs(complete_main_reports):
    _root, _directory, _identity, proof, run, jobs = complete_main_reports
    main_proofs.validate_main(proof, run, jobs)
    evidence.validate_gate(proof, run, jobs, head=proof["head"], base=None, pr=None)


@pytest.mark.parametrize("leg,missing", REQUIRED_NATIVE_OMISSIONS)
def test_validate_main_refuses_mutually_reduced_required_coverage(
    complete_main_reports, leg, missing
):
    _root, directory, identity, proof, run, jobs = complete_main_reports
    # Validate the complete twin before changing any report or proof content.
    main_proofs.validate_main(proof, run, jobs)
    evidence.validate_gate(proof, run, jobs, head=proof["head"], base=None, pr=None)
    native = proof["main_proofs"]["native"]["attestation"]
    before = copy.deepcopy(native["legs"][leg])
    reports = directory / leg
    names = [name for name in native_report_names(leg) if name != missing]
    reduced = write_native_report_pair(reports, names)
    record_native_leg(reports, identity, leg, reduced)
    assert names and missing not in names
    assert main_proofs.native_inventory(
        reports / "native-registration.json", reports / "native-junit.xml"
    ) == reduced
    native["legs"][leg] = {**reduced, "artifact": before["artifact"]}
    for field in ("registration_sha256", "junit_sha256"):
        assert native["legs"][leg][field] != before[field]
    # The consumer sees an internally consistent reduced proof, not stale evidence.
    with pytest.raises(ValueError) as rejected:
        main_proofs.validate_main(proof, run, jobs)
    assert str(rejected.value) == f"native {leg}: missing required native rows: {missing}"


@pytest.mark.parametrize("dependency", ["authz", "main-ref", *NATIVE_LEGS])
@pytest.mark.parametrize("result", ["missing", "failure", "cancelled", "skipped", "in_progress"])
def test_native_aggregate_refuses_missing_or_unsuccessful_needs(native_reports, dependency, result):
    root, directory, _identity, needs = native_reports
    if result == "missing":
        needs.pop(dependency)
        reason = "native needs inventory changed"
    else:
        needs[dependency]["result"] = result
        reason = "native dependency failed, cancelled or skipped"
    with pytest.raises(ValueError, match=reason):
        main_proofs.native_aggregate(root, directory, needs)


def test_native_aggregate_refuses_unexpected_dependency(native_reports):
    root, directory, _identity, needs = native_reports
    needs["unreviewed-leg"] = {"result": "success"}
    with pytest.raises(ValueError, match="native needs inventory changed"):
        main_proofs.native_aggregate(root, directory, needs)


@pytest.mark.parametrize("leg", NATIVE_LEGS)
@pytest.mark.parametrize(
    "field", ["run_id", "attempt", "head", "tested_sha", "tree", "leg",
              "junit_sha256", "registration_sha256"]
)
def test_native_aggregate_refuses_one_stale_leg_identity_or_digest(native_reports, leg, field):
    root, directory, _identity, needs = native_reports
    path = directory / leg / "native-leg.json"
    row = contract.object_json(path.read_bytes())
    target = row["inventory"] if field.endswith("sha256") else row
    old = target[field]
    target[field] = old + 1 if type(old) is int else (
        "other-leg" if field == "leg"
        else hashlib.sha256(f"tampered:{field}".encode()).hexdigest()[:len(old)]
    )
    path.write_bytes(contract.json_bytes(row))
    with pytest.raises(ValueError, match=f"{leg}: report bytes or producer identity changed"):
        main_proofs.native_aggregate(root, directory, needs)


@pytest.mark.parametrize("leg", NATIVE_LEGS)
@pytest.mark.parametrize("field", ["registered", "executed", "skipped", "failures", "errors"])
def test_native_aggregate_refuses_recorded_inventory_tampering(native_reports, leg, field):
    root, directory, _identity, needs = native_reports
    path = directory / leg / "native-leg.json"
    row = contract.object_json(path.read_bytes())
    row["inventory"][field] = (
        [] if field in {"registered", "executed"} else [f"{leg}-unit"]
    )
    path.write_bytes(contract.json_bytes(row))
    with pytest.raises(ValueError, match=f"{leg}: report bytes or producer identity changed"):
        main_proofs.native_aggregate(root, directory, needs)


@pytest.mark.parametrize("leg", NATIVE_LEGS)
@pytest.mark.parametrize(
    "missing", ["leg-directory", "native-leg.json", "native-registration.json", "native-junit.xml"]
)
def test_native_aggregate_requires_every_current_leg_report(native_reports, tmp_path, leg, missing):
    root, directory, _identity, needs = native_reports
    incomplete = tmp_path / "incomplete-native"
    incomplete.mkdir()
    for selected in NATIVE_LEGS:
        if selected == leg and missing == "leg-directory":
            continue
        reports = incomplete / selected
        reports.mkdir()
        for name in ("native-leg.json", "native-registration.json", "native-junit.xml"):
            if selected != leg or name != missing:
                (reports / name).write_bytes((directory / selected / name).read_bytes())
    absent = incomplete / leg / ("native-leg.json" if missing == "leg-directory" else missing)
    with pytest.raises(FileNotFoundError) as rejected:
        main_proofs.native_aggregate(root, incomplete, needs)
    assert rejected.value.filename == str(absent)


@pytest.mark.parametrize("leg", NATIVE_LEGS)
@pytest.mark.parametrize("filename", ["native-registration.json", "native-junit.xml"])
def test_native_aggregate_reparses_raw_report_bytes(native_reports, leg, filename):
    root, directory, _identity, needs = native_reports
    path = directory / leg / filename
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match=f"{leg}: report bytes or producer identity changed"):
        main_proofs.native_aggregate(root, directory, needs)


@pytest.mark.parametrize("leg", NATIVE_LEGS)
@pytest.mark.parametrize(
    "fault,reason",
    [
        ("empty", "missing nonempty inventory"),
        ("omitted", "registration/execution disagree"),
        ("duplicate", "duplicate inventory"),
        ("skipped", "registration/execution disagree|required rows skipped"),
        ("failure", "required rows failures"),
        ("error", "required rows errors"),
    ],
)
def test_native_aggregate_refuses_non_full_leg_reports(native_reports, leg, fault, reason):
    root, directory, _identity, needs = native_reports
    reports = directory / leg
    names = native_report_names(leg)
    if fault == "empty":
        names = []
    elif fault == "omitted":
        names = names[:1]
    elif fault == "duplicate":
        names = [names[0], names[0]]
    path = reports / "native-junit.xml"
    write_junit(path, names)
    if fault in {"skipped", "failure", "error"}:
        xml = ElementTree.fromstring(path.read_bytes())
        ElementTree.SubElement(xml.find(".//testcase"), fault)
        path.write_bytes(ElementTree.tostring(xml))
    # Even a digest refreshed to match the bad bytes cannot bypass report parsing.
    saved = reports / "native-leg.json"
    recorded = contract.object_json(saved.read_bytes())
    recorded["inventory"]["junit_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    saved.write_bytes(contract.json_bytes(recorded))
    with pytest.raises(ValueError, match=reason):
        main_proofs.native_aggregate(root, directory, needs)


@pytest.mark.parametrize("name", ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"])
def test_native_aggregate_refuses_previous_run_or_attempt(native_reports, monkeypatch, name):
    root, directory, identity, needs = native_reports
    previous = identity["run_id" if name == "GITHUB_RUN_ID" else "attempt"]
    monkeypatch.setenv(name, str(previous + 1))
    with pytest.raises(ValueError, match="report bytes or producer identity changed"):
        main_proofs.native_aggregate(root, directory, needs)


@pytest.mark.parametrize(
    "name,value,reason",
    [
        ("GITHUB_EVENT_NAME", "workflow_dispatch", "required push/main workflow"),
        ("GITHUB_REF", "refs/heads/topic", "required push/main workflow"),
        ("GITHUB_WORKFLOW_REF", "owner/data/.github/workflows/ci-post.yml@refs/heads/main",
         "required push/main workflow"),
        ("GITHUB_SHA", "e" * 40, "checkout differs from event head"),
    ],
)
def test_native_leg_refuses_wrong_producer_source(native_reports, monkeypatch, name, value, reason):
    root, directory, _identity, _needs = native_reports
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=reason):
        main_proofs.native_leg(root, "tsan", directory / "tsan")


def test_native_producers_refuse_changed_workflow_bytes(native_reports):
    root, directory, _identity, needs = native_reports
    path = root / NATIVE_WORKFLOW
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="authoritative needs require independent workflow review"):
        main_proofs.native_leg(root, "tsan", directory / "tsan")
    with pytest.raises(ValueError, match="authoritative needs require independent workflow review"):
        main_proofs.native_aggregate(root, directory, needs)
