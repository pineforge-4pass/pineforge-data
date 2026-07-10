from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^]]*]\(([^)]+)\)")
DOCUMENTS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_relative_documentation_links_exist(document: Path) -> None:
    for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
        target = raw_target.split("#", 1)[0]
        if not target or urlsplit(target).scheme:
            continue
        resolved = (document.parent / target).resolve()
        assert resolved.exists(), f"broken link in {document.relative_to(ROOT)}: {raw_target}"
