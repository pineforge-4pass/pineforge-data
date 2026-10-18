"""Include the single alias source from a checkout or its self-contained sdist."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        source = root / "src/pineforge_data"
        if not source.is_dir():
            source = root.parent / "src/pineforge_data"
        if not source.is_dir():
            raise FileNotFoundError("missing the pineforge_data compatibility alias source")
        destination = "src/pineforge_data" if self.target_name == "sdist" else "pineforge_data"
        build_data["force_include"][str(source)] = destination
