"""A design version's number: given once when the version is made, then read.

One number per version across the whole design, from 1.  Every door that
makes a version asks :func:`next_version_number`.  Nothing else counts
versions.

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


def numbers_in_store(design_id: str | None, db_path: str | None = None) -> set[int]:
    """The numbers this computer's version store holds for *design_id*.

    Read without opening the store for writing, and only when its file
    exists: asking for a number never creates a store.  On Kiln's shared
    server there is no store to read, and none is read.
    """
    if not design_id:
        return set()
    import os
    import sqlite3

    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        return set()
    if db_path is None:
        from kiln import design_versions

        db_path = design_versions._DEFAULT_DB_PATH
    if not os.path.isfile(db_path):
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT version_number FROM design_versions WHERE design_id = ?", (design_id,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {n for n in (_number(r[0]) for r in rows) if n is not None}


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
