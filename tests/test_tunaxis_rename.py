from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
import zipfile
from configparser import ConfigParser
from email.parser import BytesParser
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from bump_version import bumped_version, project_version, synchronized_version
from release_chain.contract import object_json
from release_chain.distributions import verify_archives

from tunaxis_data.cli.backtest import main as backtest_main
from tunaxis_data.server import main as server_main

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _wheel_metadata(path: Path) -> tuple[dict[str, str], list[str], set[str]]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        parsed = BytesParser().parsebytes(archive.read(metadata_name))
    return (
        {"name": parsed["Name"], "version": parsed["Version"]},
        parsed.get_all("Requires-Dist", []),
        set(parsed.get_all("Provides-Extra", [])),
    )


def test_canonical_and_legacy_imports_share_objects_and_warning() -> None:
    script = """
import importlib
import warnings
with warnings.catch_warnings(record=True) as new_warnings:
    warnings.simplefilter('always')
    canonical = importlib.import_module('tunaxis_data')
assert not new_warnings
with warnings.catch_warnings(record=True) as old_warnings:
    warnings.simplefilter('always')
    legacy = importlib.import_module('pineforge_data')
assert len(old_warnings) == 1
assert old_warnings[0].category is DeprecationWarning
assert 'tunaxis_data' in str(old_warnings[0].message)
assert 'tunaxis-data' in str(old_warnings[0].message)
assert legacy is canonical
new_nested = importlib.import_module('tunaxis_data.providers.ccxt')
old_nested = importlib.import_module('pineforge_data.providers.ccxt')
assert old_nested is new_nested
assert canonical.TunaxisBacktestRunner is legacy.TunaxisBacktestRunner
"""
    environment = dict(os.environ, PYTHONPATH=str(SRC))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_python_module_execution_matches_canonical(tmp_path: Path) -> None:
    results = []
    for package in ("tunaxis_data", "pineforge_data"):
        result = subprocess.run(
            [
                sys.executable, "-W", "always::DeprecationWarning",
                "-m", f"{package}.cli.backtest", "--help",
            ],
            cwd=tmp_path,
            env=dict(os.environ, PYTHONPATH=str(SRC)),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        results.append(result)
    assert results[0].stdout == results[1].stdout
    assert "DeprecationWarning" not in results[0].stderr
    assert results[1].stderr.count("DeprecationWarning: pineforge_data is deprecated") == 1


def _wheel_scripts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith("/entry_points.txt")]
        assert len(members) == 1
        config = ConfigParser(interpolation=None)
        config.read_string(archive.read(members[0]).decode("utf-8"))
    return dict(config["console_scripts"])


def test_new_and_old_console_help_are_identical_subprocesses(
    paired_artifacts, tmp_path: Path
) -> None:
    expected = {
        "tunaxis-data": {
            "tunaxis-backtest": backtest_main,
            "tunaxis-backtest-server": server_main,
        },
        "pineforge-data": {
            "pineforge-backtest": backtest_main,
            "pineforge-backtest-server": server_main,
        },
    }
    wheels = []
    for project, pyproject in (
        ("tunaxis-data", ROOT / "pyproject.toml"),
        ("pineforge-data", ROOT / "shim/pyproject.toml"),
    ):
        declarations = tomllib.loads(pyproject.read_text())["project"]["scripts"]
        wheel = next(paired_artifacts.glob(project.replace("-", "_") + "-*.whl"))
        packaged = _wheel_scripts(wheel)
        assert packaged == declarations
        assert set(packaged) == set(expected[project])
        for name, target in packaged.items():
            assert EntryPoint(name=name, value=target, group="console_scripts").load() is (
                expected[project][name]
            )
        wheels.append(str(wheel))
    installed = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
            "--target", str(installed), *wheels,
        ],
        cwd=tmp_path,
        check=True,
    )
    outputs = {}
    environment = dict(os.environ, PYTHONPATH=str(installed))
    for name in (*expected["tunaxis-data"], *expected["pineforge-data"]):
        result = subprocess.run(
            [sys.executable, str(installed / "bin" / name), "--help"],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outputs[name] = result.stdout
    assert outputs["tunaxis-backtest"] == outputs["pineforge-backtest"]
    assert outputs["tunaxis-backtest-server"] == outputs["pineforge-backtest-server"]


@pytest.fixture(scope="module")
def paired_artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("paired-distributions")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(output), "."],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output), "shim"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(output), "."],
        cwd=ROOT,
        check=True,
    )
    return output


def test_real_paired_wheels_have_exact_metadata_and_no_overlap(paired_artifacts) -> None:
    main = next(paired_artifacts.glob("tunaxis_data-*.whl"))
    shim = next(paired_artifacts.glob("pineforge_data-*.whl"))
    main_meta, _main_requires, extras = _wheel_metadata(main)
    shim_meta, shim_requires, shim_extras = _wheel_metadata(shim)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert main_meta == {"name": "tunaxis-data", "version": version}
    assert shim_meta == {"name": "pineforge-data", "version": version}
    assert shim_extras == extras
    assert shim_requires.count(f"tunaxis-data=={version}") == 1
    assert {
        re.search(r"tunaxis-data\[([^]]+)\]==" + re.escape(version), item).group(1)
        for item in shim_requires
        if "[" in item
    } == extras
    with zipfile.ZipFile(main) as main_archive, zipfile.ZipFile(shim) as shim_archive:
        assert not set(main_archive.namelist()) & set(shim_archive.namelist())
        main_licenses = [
            name for name in main_archive.namelist()
            if name.endswith(".dist-info/licenses/LICENSE")
        ]
        assert len(main_licenses) == 1
        shim_license = f"pineforge_data-{version}.dist-info/licenses/LICENSE"
        assert main_archive.read(main_licenses[0]) == shim_archive.read(shim_license)
        assert shim_archive.read(shim_license) == (ROOT / "LICENSE").read_bytes()
    files = {path.name: path.read_bytes() for path in paired_artifacts.iterdir()}
    verify_archives(files, version)


def test_shim_direct_wheel_matches_wheel_from_sdist(paired_artifacts, tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), "shim"],
        cwd=ROOT,
        check=True,
    )
    direct = next(tmp_path.glob("pineforge_data-*.whl"))
    from_sdist = next(paired_artifacts.glob("pineforge_data-*.whl"))
    with zipfile.ZipFile(direct) as direct_archive, zipfile.ZipFile(from_sdist) as sdist_archive:
        direct_names = direct_archive.namelist()
        sdist_names = sdist_archive.namelist()
        assert len(direct_names) == len(set(direct_names))
        assert len(sdist_names) == len(set(sdist_names))
        assert set(direct_names) == set(sdist_names)
        assert direct_names.count("pineforge_data/__init__.py") == 1
        assert sdist_names.count("pineforge_data/__init__.py") == 1
        assert {name for name in direct_names if name.startswith("pineforge_data/")} == {
            "pineforge_data/__init__.py"
        }
        assert not any(name.startswith(("src/", "tunaxis_data/")) for name in direct_names)
        for name in direct_names:
            assert direct_archive.read(name) == sdist_archive.read(name)


def test_shim_pin_and_all_extras_cannot_drift(tmp_path: Path) -> None:
    main = tmp_path / "pyproject.toml"
    shim = tmp_path / "shim/pyproject.toml"
    shim.parent.mkdir()
    main.write_text((ROOT / "pyproject.toml").read_text())
    current = project_version(main)
    mismatched = bumped_version(current, "patch")
    shim.write_text(
        (ROOT / "shim/pyproject.toml").read_text().replace(
            f'=={current}"', f'=={mismatched}"'
        )
    )
    with pytest.raises(ValueError, match=r"must match|exactly"):
        synchronized_version(main)


def test_existing_release_fixture_still_validates_unchanged() -> None:
    fixture = ROOT / "tests/fixtures/release_chain"
    event = object_json((fixture / "ready-event.json").read_bytes())
    manifest = (fixture / "release-manifest.json").read_bytes()
    from release_chain.contract import event_from_manifest, verify_manifest

    verify_manifest(event, manifest)
    assert event_from_manifest(manifest) == event
