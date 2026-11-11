"""A design version's number: given once when the version is made, then read.

One number per version across the whole design, from 1.  Every door that
makes a version asks :func:`next_version_number`.  Nothing else counts
versions.

A design slot that holds no version yet (a head recipe with no ``version``
of its own) is not a version and takes no number.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_HEAD = ".kiln_recipe.json"
_PREFIX = ".kiln_recipe"
#: The highest number the design in a folder has ever given, kept beside its
#: recipes so a number is never given twice: not to two saves at once, and
#: not again after the version that held it is removed.
_GIVEN = ".kiln_numbers.json"
_LOCK = ".kiln_numbers.lock"
#: How long a save waits for another save of the same design to take its
#: number before it is refused.
_LOCK_WAIT_SECONDS = 30.0


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: No design has this many versions; a larger number is damage, not a count.
_MOST = 10**9


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= _MOST else None


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
    return _number(int(stem[2:])) if stem[:2] == ".v" and stem[2:].isdigit() else None


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
        held = store.numbers(design_id)
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


class NumbersBusy(RuntimeError):
    """Another save of this design is still taking its number."""


@contextmanager
def numbers_lock(folder: str | Path) -> Iterator[None]:
    """Hold the numbers lock of the design kept in *folder*.

    Every door that gives a number holds it from reading the numbers already
    given to writing down the one it takes, so two saves of one design never
    take the same number, whichever doors they come through.  A save that
    cannot get it within :data:`_LOCK_WAIT_SECONDS` is refused
    (:class:`NumbersBusy`), never let through without it: two saves holding
    one number is how a kept file gets written over.  The lock is released
    by the system when its holder ends, however it ends.  Not re-entrant: a
    holder never asks for it again.
    """
    import time

    root = Path(folder)
    try:
        import fcntl
    except ImportError:  # no file locks on this system: the count alone
        yield
        return
    try:
        lock = open(root / _LOCK, "a+")  # noqa: SIM115 -- held across the read and the write
    except OSError:  # a folder this process cannot write: nothing to write a number into either
        yield
        return
    try:
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise NumbersBusy(
                        "Another save of this design is still running, so nothing was saved. "
                        "Try again in a moment."
                    ) from None
                time.sleep(0.02)
        yield
    finally:
        lock.close()


def _write_given(root: Path, number: int) -> None:
    import os

    tmp = root / f"{_GIVEN}.{os.getpid()}.part"
    try:
        tmp.write_text(json.dumps({"highest": number}), encoding="utf-8")
        tmp.replace(root / _GIVEN)
    except OSError:
        pass


def raise_mark(folder: str | Path, number: Any) -> None:
    """Write down that the design kept in *folder* gave *number*, when it is
    past every number written down so far.  The caller holds
    :func:`numbers_lock`."""
    root = Path(folder)
    given = _number(number)
    if given is not None and root.is_dir() and given > max(numbers_in_folder(root), default=0):
        _write_given(root, given)


def reserve_version_number(
    folder: str | Path, taken: Iterable[Any] = (), *, also: Callable[[], Iterable[Any]] | None = None
) -> int:
    """Take the next number for the design kept in *folder*, for good.

    :func:`next_version_number` over the folder, *taken*, and what *also*
    returns (the design's history record, read while the lock is held so a
    version recorded there by another door a moment ago is counted).  The
    number is written down in the folder before it is returned, so no other
    save is handed it.  A save that takes a number and then fails leaves a
    gap, never a repeat.
    """
    root = Path(folder)
    if not root.is_dir():
        return next_version_number(None, [*taken, *(also() if also else ())])
    with numbers_lock(root):
        number = next_version_number(root, [*taken, *(also() if also else ())])
        _write_given(root, number)
        return number


def note_number_given(folder: str | Path, number: Any) -> None:
    """Record that the design kept in *folder* gave *number* to a version
    that has no recipe file here, so no later save is handed it."""
    root = Path(folder)
    if not root.is_dir():
        return
    with numbers_lock(root):
        raise_mark(root, number)
