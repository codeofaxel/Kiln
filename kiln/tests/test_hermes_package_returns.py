"""The Hermes Agent package comes back with the first release after 1.5.0.

The package (plugins/hermes) starts Kiln from PyPI at this repo's version, and
it relies on settings 1.5.0 does not have: on 1.5.0 its "ask before sharing"
setting is ignored and Kiln would share by default.  So it waits on branch
claude/hermes-package and has to be merged back in the release that bumps the
version.  This fails that release until it is.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_hermes_package_is_back_after_1_5_0() -> None:
    text = (ROOT / "kiln" / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', text).group(1)
    if version == "1.5.0":
        return
    assert (ROOT / "plugins" / "hermes" / "mcp.json").is_file(), (
        f"kiln is {version}: merge the Hermes Agent package back before releasing "
        "(git merge origin/claude/hermes-package). Its own tests then pin it to this "
        "version. Next step after the release: the Hermes catalog checklist."
    )
