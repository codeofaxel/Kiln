"""A design version's number: given once when the version is made, then read.

One number per version across the whole design, from 1.  Every door that
makes a version asks :func:`next_version_number`; every door that needs a
version's recipe file asks :func:`recipe_path_for_version`, which finds it
by the version id written inside the file.  Nothing else counts versions or
derives a file name from a count.

A design slot that holds no version yet (a head recipe with no ``version``
of its own) is not a version and takes no number.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_HEAD = ".kiln_recipe.json"
_PREFIX = ".kiln_recipe"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def recipe_files(folder: str | Path) -> list[Path]:
    """Every recipe file in *folder*: the head and each kept version."""
    root = Path(folder)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob(f"{_PREFIX}*.json") if p.is_file())


def holds_a_version(path: str | Path) -> bool:
    """Whether the recipe file at *path* records a version.

    A slot made before anything was saved carries neither a ``version`` nor
    a ``version_id``.  It is not version 1: the first save into it is.
    """
    data = _read(Path(path))
    return _number(data.get("version")) is not None or bool(data.get("version_id"))


def numbers_in_folder(folder: str | Path) -> set[int]:
    """The numbers already given to versions kept in *folder*."""
    found = (_number(_read(p).get("version")) for p in recipe_files(folder))
    return {n for n in found if n is not None}


def next_version_number(folder: str | Path | None, taken: Iterable[Any] = ()) -> int:
    """The number for the next version of the design kept in *folder*.

    One past the highest number already given: in the folder's recipe files
    and in *taken* (numbers the caller knows from its own record of the
    design).  Never a number an existing version holds.
    """
    known = {n for n in (_number(t) for t in taken) if n is not None}
    if folder is not None:
        known |= numbers_in_folder(folder)
    return max(known, default=0) + 1


def recipe_path_for_version(
    folder: str | Path, version_id: str | None, number: int | None = None
) -> Path | None:
    """The kept recipe file of one version, or ``None``.

    Matched on the ``version_id`` inside the file.  *number* is used only
    for a file that names no version id at all, and never for the head.
    """
    files = [p for p in recipe_files(folder) if p.name != _HEAD]
    if version_id:
        for path in files:
            if _read(path).get("version_id") == version_id:
                return path
    wanted = _number(number)
    if wanted is None:
        return None
    for path in files:
        data = _read(path)
        if _number(data.get("version")) == wanted and not data.get("version_id"):
            return path
    return None
