"""When a test may launch the real Bambu Studio app.

On macOS every launch of the Bambu Studio binary puts the app in the Dock,
and a launch that crashes or is stopped mid-slice (a test run interrupted, a
worker killed) is an "unexpected quit": the next time the person opens Bambu
Studio it asks whether to reopen its windows.  OrcaSlicer speaks the same
command line, so a test that tries every installed slicer covers that dialect
without Bambu Studio, and launches it only when the run is about Bambu
Studio:

- ``KILN_TEST_BAMBU=1`` asks for it (the release checks set it), ``=0``
  refuses it;
- otherwise it runs when this checkout changes slicing, Bambu or 3MF code
  against ``origin/main``, committed or not.

A test that needs Bambu Studio itself, not "every slicer", does not use this
gate: it runs wherever Bambu Studio is installed.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from pathlib import Path

import pytest

BAMBU_STUDIO = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"

#: A changed path that makes a run about Bambu Studio.
_ABOUT_BAMBU = re.compile(r"slic|bambu|orca|3mf|face_map|gcode", re.IGNORECASE)

_REPO = Path(__file__).resolve().parents[2]


def installed(path: str = BAMBU_STUDIO) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _changed_paths() -> list[str]:
    """Paths this checkout changes against ``origin/main``; empty when git
    cannot say (no remote, not a checkout)."""
    def git(*args: str) -> list[str]:
        try:
            out = subprocess.run(
                ["git", "-C", str(_REPO), *args],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in out.splitlines() if line]

    base = git("merge-base", "HEAD", "origin/main")
    committed = git("diff", "--name-only", base[0], "HEAD") if base else []
    return committed + git("diff", "--name-only", "HEAD") + git(
        "ls-files", "--others", "--exclude-standard"
    )


@functools.lru_cache(maxsize=1)
def wanted() -> tuple[bool, str]:
    """Whether a test that tries every slicer should include Bambu Studio,
    and why, in words a skip reason can carry."""
    asked = os.environ.get("KILN_TEST_BAMBU", "").strip()
    if asked in ("1", "true", "yes"):
        return True, "KILN_TEST_BAMBU=1"
    if asked in ("0", "false", "no"):
        return False, "KILN_TEST_BAMBU=0"
    touched = [p for p in _changed_paths() if _ABOUT_BAMBU.search(p)]
    if touched:
        return True, f"this checkout changes {touched[0]}"
    return False, (
        "Bambu Studio launches only when the run is about it: OrcaSlicer covers "
        "the same command line here. Set KILN_TEST_BAMBU=1 to include it"
    )


def skip_unless_wanted() -> pytest.MarkDecorator:
    """The mark for a Bambu Studio case in a test that tries every slicer."""
    if not installed():
        return pytest.mark.skipif(True, reason="BambuStudio not installed")
    run, why = wanted()
    return pytest.mark.skipif(not run, reason=why)
