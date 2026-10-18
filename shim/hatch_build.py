"""Include the single alias source from a checkout or its self-contained sdist."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        destination = "src/pineforge_data" if self.target_name == "sdist" else "pineforge_data"
        for name in ("__init__.py", "py.typed"):
            relative = Path("src/pineforge_data") / name
            if (root / relative).is_file():
                # An extracted sdist selects this file normally; do not add it twice.
                continue
            source = root.parent / relative
            if not source.is_file():
                raise FileNotFoundError(f"missing the pineforge_data alias file: {name}")
            # A checkout selects only these external source files, never bytecode.
            build_data["force_include"][str(source)] = f"{destination}/{name}"
