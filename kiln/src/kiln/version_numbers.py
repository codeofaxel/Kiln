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
#: The highest number the design in a folder has ever given, kept beside its
#: recipes so a number is never given twice: not to two saves at once, and
#: not again after the version that held it is removed.
_GIVEN = ".kiln_numbers.json"
_LOCK = ".kiln_numbers.lock"


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


def _number_in_name(path: Path) -> int | None:
    """The N of ``.kiln_recipe.vN.json``; ``None`` for any other name."""
    stem = path.name[len(_PREFIX) : -len(".json")]
    return int(stem[2:]) if stem[:2] == ".v" and stem[2:].isdigit() and int(stem[2:]) > 0 else None


def numbers_in_folder(folder: str | Path) -> set[int]:
    """The numbers already given to versions kept in *folder*.

    The number inside each recipe file, and the number in a kept file's
    name: a file whose contents cannot be read still holds its number, so
    no later version is written over it.
    """
    found: set[int] = set()
    for path in recipe_files(folder):
        for n in (_number(_read(path).get("version")), _number_in_name(path)):
            if n is not None:
                found.add(n)
    highest = _number(_read(Path(folder) / _GIVEN).get("highest"))
    if highest is not None:
        found.add(highest)
    return found


def numbers_in_store(design_id: str | None, db_path: str | None = None) -> set[int]:
    """The numbers this computer's version store holds for *design_id*.

    Read only when the store's file exists: asking for a number never
    creates a store on a person's computer.
    """
    if not design_id:
        return set()
    import os

    from kiln import design_versions
    from kiln.runtime_env import is_hosted_multitenant

    hosted = is_hosted_multitenant()
    # On a shared server the one default file would be every customer's.  A
    # store is opened there only when the server has made the class open
    # nothing but the caller's own copy; otherwise no store is read at all.
    if hosted and not getattr(design_versions.DesignVersionStore, "_kiln_guarded_on_hosted", False):
        return set()
    if db_path is None and not hosted and not os.path.isfile(design_versions._DEFAULT_DB_PATH):
        return set()
    if db_path is not None and not os.path.isfile(db_path):
        return set()
    # Opened through the store's own class, so whatever decides which file a
    # store may open on a shared server decides it here too.
    try:
        store = design_versions.DesignVersionStore(db_path=db_path)
    except Exception:  # noqa: BLE001 -- no store this caller may read: no numbers from it
        return set()
    try:
        held = [v.version_number for v in store.list_versions(design_id, limit=1_000_000)]
    except Exception:  # noqa: BLE001 -- an unreadable store holds no number we can know
        return set()
    finally:
        store.close()
    return {n for n in (_number(h) for h in held) if n is not None}


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


def reserve_version_number(folder: str | Path, taken: Iterable[Any] = ()) -> int:
    """Take the next number for the design kept in *folder*, for good.

    :func:`next_version_number`, made safe for two saves at once and for a
    version removed later: the number is written down in the folder before
    it is returned, under a lock, so no other save is handed it.  A save
    that takes a number and then fails leaves a gap, never a repeat.
    """
    root = Path(folder)
    if not root.is_dir():
        return next_version_number(None, taken)
    try:
        import fcntl
    except ImportError:  # no file locks on this system: the count alone
        fcntl = None  # type: ignore[assignment]
    lock_path = root / _LOCK
    try:
        lock = open(lock_path, "a+")  # noqa: SIM115 -- held across the read and the write
    except OSError:
        return next_version_number(root, taken)
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        number = next_version_number(root, taken)
        tmp = root / (_GIVEN + ".part")
        try:
            tmp.write_text(json.dumps({"highest": number}), encoding="utf-8")
            tmp.replace(root / _GIVEN)
        except OSError:
            pass
        return number
    finally:
        lock.close()
