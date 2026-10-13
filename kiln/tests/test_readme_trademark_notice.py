"""Every README that names OctoPrint carries its trademark notice.

OctoPrint's published rules (octoprint.org/trademark-rules) allow naming it
as compatible only beside the words "compatible with" / "can be used with"
and the line "OctoPrint is a registered trademark", linked to octoprint.org.
These READMEs are package pages (PyPI, GitHub, npm, the Claude Code plugin),
so each must carry the linked notice whenever it names OctoPrint.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
READMES = [
    ROOT / "README.md",
    ROOT / "kiln" / "README.md",
    ROOT / "npm-launcher" / "README.md",
    ROOT / "plugins" / "kiln" / "README.md",
]
_LINKED_NOTICE = re.compile(r"\[OctoPrint is a registered trademark\]\(https://octoprint\.org/?\)")


@pytest.mark.parametrize("readme", READMES, ids=lambda p: str(p.relative_to(ROOT)))
def test_readme_naming_octoprint_carries_the_linked_notice(readme: Path) -> None:
    if not readme.exists():
        pytest.skip(f"{readme} is not in this checkout")
    text = readme.read_text()
    if "octoprint" not in text.lower():
        return
    assert _LINKED_NOTICE.search(text), (
        f"{readme.relative_to(ROOT)} names OctoPrint without the linked trademark notice"
    )
