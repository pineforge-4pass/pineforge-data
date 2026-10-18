"""Include the single alias source from a checkout or its self-contained sdist."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        source = root / "src/pineforge_data/__init__.py"
        if source.is_file():
            # The extracted sdist already selects this source through pyproject.toml.
            # Force-including it too would add the same wheel member twice.
            return
        source = root.parent / "src/pineforge_data/__init__.py"
        if not source.is_file():
            raise FileNotFoundError("missing the pineforge_data compatibility alias source")
        # In a checkout the alias lives outside the shim root, so normal file
        # selection cannot reach it. Include it once at the target's package path.
        # Include only the alias module, never generated files such as bytecode.
        destination = "src/pineforge_data" if self.target_name == "sdist" else "pineforge_data"
        build_data["force_include"][str(source)] = f"{destination}/__init__.py"
