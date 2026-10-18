from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from release_chain.contract import UPDATE_FILES

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^]]*]\(([^)]+)\)")
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    *sorted((ROOT / "docs").rglob("*.md")),
]


@pytest.mark.parametrize(
    "document",
    DOCUMENTS,
    ids=lambda path: str(path.relative_to(ROOT)),
)
def test_relative_documentation_links_exist(document: Path) -> None:
    for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
        target = raw_target.split("#", 1)[0]
        if not target or urlsplit(target).scheme:
            continue
        resolved = (document.parent / target).resolve()
        assert resolved.exists(), f"broken link in {document.relative_to(ROOT)}: {raw_target}"


def test_release_guide_matches_workflow_name_and_updated_file_inventory() -> None:
    guide = (ROOT / "docs/releasing.md").read_text(encoding="utf-8")
    workflow = yaml.load(
        (ROOT / ".github/workflows/pineforge-release.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert f"dispatch `{workflow['name']}`" in guide
    for name in UPDATE_FILES:
        assert f"`{name}`" in guide
