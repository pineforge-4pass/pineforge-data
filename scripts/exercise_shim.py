"""CI only: check the two installed wheels without checkout imports."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import subprocess
import sys
import sysconfig
import warnings
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        canonical = importlib.import_module("tunaxis_data")
    assert not emitted
    assert "pineforge_data" not in sys.modules
    assert not Path(canonical.__file__).resolve().is_relative_to(source)
    assert not any(Path(entry).resolve().is_relative_to(source) for entry in sys.path)
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        legacy = importlib.import_module("pineforge_data")
    assert len(emitted) == 1 and emitted[0].category is DeprecationWarning
    assert "tunaxis_data" in str(emitted[0].message)
    assert "tunaxis-data" in str(emitted[0].message)
    assert legacy is canonical
    assert importlib.import_module("pineforge_data.models") is importlib.import_module(
        "tunaxis_data.models"
    )
    assert legacy.models is canonical.models
    assert importlib.metadata.version("pineforge-data") == importlib.metadata.version(
        "tunaxis-data"
    )
    scripts = Path(sysconfig.get_path("scripts"))
    for suffix in ("backtest", "backtest-server"):
        outputs = []
        for prefix in ("tunaxis", "pineforge"):
            result = subprocess.run(
                [sys.executable, "-I", str(scripts / f"{prefix}-{suffix}"), "--help"],
                text=True,
                capture_output=True,
                check=True,
            )
            outputs.append(result.stdout)
        assert outputs[0] == outputs[1]
    print("Installed Tunaxis Data and shim imports, warning, and console scripts verified")


if __name__ == "__main__":
    main()
