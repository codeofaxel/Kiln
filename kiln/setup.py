"""Build-time step beside ``pyproject.toml``: carry the skill file into the wheel.

``SKILL.md`` is maintained once, at the repository root, where ClawHub and
the README link to it.  The package is built from this directory, and a
build never reaches above it, so no ``package-data`` glob could ship that
file -- and ``kiln.skill_manifest.get_skill_definition_path()`` looks for
it beside the package at ``kiln/data/SKILL.md``.  No release up to 1.5.0
carried it: a ``uv tool install kiln3d`` had no skill file at all.

So at build time the root file is copied to where the resolver looks.  The
copy is generated, ignored by git, and overwritten by every build, so there
is one source of truth.  Building from an sdist (no repository above) keeps
the copy the sdist already carries.  A tree with neither -- a Docker build
that copied only this directory -- builds, and says so; the release gate
(``scripts/audit_wheel_inventory.py``) refuses a wheel without the file.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from setuptools import setup

_HERE = Path(__file__).resolve().parent
_SOURCE = _HERE.parent / "SKILL.md"
_SHIPPED = _HERE / "src" / "kiln" / "data" / "SKILL.md"

if _SOURCE.is_file():
    _SHIPPED.parent.mkdir(parents=True, exist_ok=True)
    if not _SHIPPED.is_file() or _SHIPPED.read_bytes() != _SOURCE.read_bytes():
        shutil.copyfile(_SOURCE, _SHIPPED)
elif not _SHIPPED.is_file():
    sys.stderr.write(
        "kiln3d: no SKILL.md beside this package and none at src/kiln/data/SKILL.md; "
        "the wheel will carry no skill file\n"
    )

setup()
