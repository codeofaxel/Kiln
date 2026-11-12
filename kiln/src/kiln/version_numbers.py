"""A design version's number: given once when the version is made, then read.

One number per version across the whole design, from 1.  Every door that
makes a version asks :func:`next_version_number`.  Nothing else counts
versions.  A Kiln account keeps the number it gave each version, and this
computer takes it (:func:`plan_numbers`, :func:`renumber_folder`), read from
the account's own answer, never worked out.

A design slot that holds no version yet (a head recipe with no ``version``
of its own) is not a version and takes no number.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_HEAD = ".kiln_recipe.json"
_PREFIX = ".kiln_recipe"
#: A version's proven recipes, kept beside it under its number.
_PROVEN = ".kiln_proven_recipe"
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


def library_design_id(
    folder: str | Path, library_of: Callable[[str], Any] | None = None
) -> str | None:
    """The design whose folder in the library *folder* is: the folder's name,
    when the library folder of that name is *folder* itself.  ``None`` for any
    other folder, whatever it is named, so a recipe kept there that names no
    design takes no design's numbers.

    *library_of* finds a design's folder in the library; this computer's
    library (``kiln.history_vault``) when it is not given.
    """
    import os

    if library_of is None:
        from kiln.history_vault import _design_folder as library_of
    path = os.fspath(folder)
    name = os.path.basename(os.path.normpath(path))
    try:
        kept = library_of(name)
    except ValueError:
        return None
    if kept is None:
        return None
    return name if os.path.realpath(os.fspath(kept)) == os.path.realpath(path) else None


def numbers_from_library(folder: str | Path | None, library: str | Path | None) -> set[int]:
    """The numbers a version written in *folder* is numbered past from
    *library*, its design's own folder in the library: every number that
    folder has given (:func:`numbers_in_folder`, its recipe files and its
    record of numbers given, which holds the numbers a Kiln account gave
    versions kept on other computers).

    Empty when the design has no folder in the library, and when *folder* is
    that folder, where its numbers are read already.
    """
    if library is None or not Path(library).is_dir():
        return set()
    if folder is not None and Path(folder).resolve() == Path(library).resolve():
        return set()
    return numbers_in_folder(library)


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


class NumbersNotTaken(RuntimeError):
    """The numbers a Kiln account gave could not be taken on this computer
    this time, and nothing here was changed."""

    def __init__(self, why: str = "") -> None:
        super().__init__(
            "The version numbers your Kiln account gave could not be taken on this "
            "computer this time" + (f" ({why})" if why else "") + ", so nothing here "
            "changed. Try again in a moment."
        )


#: After this long a lock file left by a holder that ended without removing
#: it is taken over (only where the system has no lock of its own to release).
_LOCK_STALE_SECONDS = 120.0

_held_here = threading.local()
_in_process: dict[str, threading.Lock] = {}
_in_process_guard = threading.Lock()


def _system_lock() -> tuple[Callable[[Any], bool], Callable[[Any], None]] | None:
    """``(take, release)`` over an open lock file, by the system's own file
    lock, which it releases when the holder ends however it ends; ``None``
    on a system that has neither kind."""
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    if fcntl is not None:
        def take(handle: Any) -> bool:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            return True

        return take, lambda handle: None  # released when the file is closed
    try:
        import msvcrt
    except ImportError:
        return None

    def take_region(handle: Any) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def release_region(handle: Any) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    return take_region, release_region


def _take_by_file(root: Path) -> Callable[[], None] | None:
    """Take the lock by making a file only one holder can make; ``None``
    when another holds it.  One left behind by a holder that ended is taken
    over after :data:`_LOCK_STALE_SECONDS`."""
    import os
    import time

    marker = root / f"{_LOCK}.held"
    for _attempt in (1, 2):
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            try:
                stale = time.time() - marker.stat().st_mtime > _LOCK_STALE_SECONDS
            except OSError:
                continue  # removed a moment ago: ask again
            if not stale:
                return None
            marker.unlink(missing_ok=True)
            continue
        return lambda: marker.unlink(missing_ok=True)
    return None


def _busy() -> NumbersBusy:
    return NumbersBusy(
        "Another save of this design is still running, so nothing was saved. "
        "Try again in a moment."
    )


@contextmanager
def numbers_lock(folder: str | Path) -> Iterator[None]:
    """Hold the numbers lock of the design kept in *folder*.

    Every door that gives a number holds it from reading the numbers already
    given until the version's file is written, and a pull holds it from
    planning the account's numbers until its files are written, so two saves
    of one design never take the same number and a save and a pull never
    write between each other's steps, whichever doors they come through.  A
    holder that cannot get it within :data:`_LOCK_WAIT_SECONDS` is refused
    (:class:`NumbersBusy`), never let through without it: two versions
    holding one number is how a kept file gets written over.

    Held across processes by the system's file lock (released when its
    holder ends, however it ends), or where the system has none by a file
    only one holder can make; and across the threads of one process.  The
    thread that holds it may ask for it again.
    """
    import os
    import time

    root = Path(folder)
    key = os.path.realpath(root)
    depth = _held_here.__dict__.setdefault("depth", {})
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    with _in_process_guard:
        within = _in_process.setdefault(key, threading.Lock())
    if not within.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise _busy()
    release: Callable[[], None] | None = None
    try:
        release = _take_across_processes(root, deadline)
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
    finally:
        if release is not None:
            release()
        within.release()


@contextmanager
def pull_lock(folder: str | Path | None) -> Iterator[None]:
    """Hold the numbers lock of the design kept in *folder* for a pull or a
    clone: from planning the account's numbers until the pulled versions'
    files are written, so no save takes a number or writes a file between
    the two.  A folder that does not exist yet has no numbers to hold.  A
    lock another save holds past the wait is :class:`NumbersNotTaken`:
    nothing here changed, and the pull is asked for again."""
    if folder is None or not Path(folder).is_dir():
        yield
        return
    try:
        held = numbers_lock(folder)
        held.__enter__()
    except NumbersBusy as exc:
        raise NumbersNotTaken("another save of this design was running") from exc
    try:
        yield
    finally:
        held.__exit__(None, None, None)


def _take_across_processes(root: Path, deadline: float) -> Callable[[], None] | None:
    """Take *root*'s lock from every other process, waiting until
    *deadline*; returns what releases it, or ``None`` for a folder this
    process cannot write (there is nothing to write a number into either)."""
    import time

    system = _system_lock()
    handle = None
    if system is not None:
        try:
            handle = open(root / _LOCK, "a+")  # noqa: SIM115 -- held until released
        except OSError:
            return None
    elif not root.is_dir():
        return None
    try:
        while True:
            if system is not None:
                take, give_back = system
                if take(handle):
                    def release(handle: Any = handle, give_back: Any = give_back) -> None:
                        give_back(handle)
                        handle.close()

                    return release
            else:
                try:
                    release_file = _take_by_file(root)
                except OSError:
                    return None
                if release_file is not None:
                    return release_file
            if time.monotonic() >= deadline:
                raise _busy() from None
            time.sleep(0.02)
    except BaseException:
        if handle is not None:
            handle.close()
        raise


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


def number_to_write(
    folder: str | Path, number: Any, made: Any, taken: Callable[[], Iterable[Any]] | None = None,
) -> tuple[int, str | None]:
    """The number the version made at *made* (its recipe's ``created``) is
    written under in *folder*, having been given *number*; the caller holds
    :func:`numbers_lock` and writes the file before letting go of it.

    *number*, unless a kept file already holds it for another version (one
    a pull laid down after this version took the number): then the next
    number, past what *taken* returns too (read only then), taken for good.  A version is never written
    over another.  Returns ``(number, version_id)``; the id is the one the
    version's own kept file names, when it is being written again.
    """
    root = Path(folder)
    given = _number(number)
    if given is None or not root.is_dir():
        return (given if given is not None else 1), None
    kept = _read(root / f"{_PREFIX}.v{given}.json")
    if not kept:
        return given, None
    if (kept.get("created") or kept.get("created_at") or "") == (made or ""):
        vid = kept.get("version_id")
        return given, vid if isinstance(vid, str) and vid else None
    moved = next_version_number(root, [given, *(taken() if taken else ())])
    _write_given(root, moved)
    return moved, None


def note_number_given(folder: str | Path, number: Any) -> None:
    """Record that the design kept in *folder* gave *number* to a version
    that has no recipe file here, so no later save is handed it."""
    root = Path(folder)
    if not root.is_dir():
        return
    with numbers_lock(root):
        raise_mark(root, number)


# ---------------------------------------------------------------------------
# The account's numbers, taken here
#
# A Kiln account keeps each version's number and gives it once: the number
# the version arrived with, unless another version of the design was given
# it first (two computers that each made "version 4"), and never changes it.
# A computer takes those numbers for the versions the account holds, and
# moves a version only it holds out of the way of a number the account has
# given another version.
# ---------------------------------------------------------------------------

#: The key of a kept recipe file that names no version (one saved before
#: files carried their version's id): its file name after this prefix.
FILE_KEY = "file:"


def _whole(value: Any) -> int | None:
    return _number(value)


def kept_numbers(folder: str | Path) -> dict[str, int]:
    """``{key: number}`` for every kept version file in *folder*: keyed by
    the version id inside it, or ``FILE_KEY + name`` for a file that names
    none.  The number is the one inside the file, else the one in its name.
    The head copy is not a kept version."""
    out: dict[str, int] = {}
    for path in recipe_files(folder):
        if path.name == _HEAD:
            continue
        data = _read(path)
        number = _number(data.get("version")) or _number_in_name(path)
        if number is None:
            continue
        vid = data.get("version_id")
        key = vid if isinstance(vid, str) and vid else FILE_KEY + path.name
        out.setdefault(key, number)
    return out


def plan_numbers(
    local: dict[str, int], account: dict[str, int], *, past: Iterable[Any] = ()
) -> dict[str, int]:
    """The number each version kept here holds once the account's numbers are
    taken: ``{key: number}`` for every key in *local*.

    *local* is every version this computer keeps for one design, by version
    id (or a file's own key), with the number it holds here.  *account* is
    the numbers the account keeps, by version id.  *past* is numbers given
    before that no version may take again (a folder's or a store's mark, the
    account's mark).

    A version the account holds takes the account's number.  Any other keeps
    its own, unless the account gave that number to another version or a
    version here now holds it; then it takes the next number past every
    number the account, this computer and *past* hold.  Never a number two
    versions share, never a number taken from a version the account holds.

    A kept file that names no version (``FILE_KEY``) is not known to be any
    version, so it is never moved for naming none: it keeps its number, and
    a version moved past it goes past that number too.
    """
    final: dict[str, int] = {}
    for key, n in local.items():
        given = _whole(account.get(key))
        if given is not None:
            final[key] = given
    for key, n in local.items():
        number = _whole(n)
        if key.startswith(FILE_KEY) and key not in final and number is not None:
            final[key] = number
    held = {n for k, n in final.items() if not k.startswith(FILE_KEY)}
    held |= {n for n in (_whole(v) for v in account.values()) if n is not None}
    ceiling = max(
        [*held, *(n for n in (_whole(v) for v in local.values()) if n is not None),
         *(n for n in (_whole(p) for p in past) if n is not None)],
        default=0,
    )
    waiting = ((k, v) for k, v in local.items() if k not in final)
    for key, n in sorted(waiting, key=lambda kv: (_whole(kv[1]) or 0, kv[0])):
        number = _whole(n)
        if number is None or number in held:
            ceiling += 1
            number = ceiling
        held.add(number)
        final[key] = number
    return final


def account_numbers_in(answer: Any) -> tuple[dict[str, int], int]:
    """The version numbers a Kiln account's answer says it keeps, and the
    design's mark: ``({version_id: number}, mark)``.

    Read from what the account said (a push's ``version_numbers`` and
    ``account_numbers``, a pull's or a clone's versions and
    ``account_numbers``), never worked out: an answer from an account that
    keeps no numbers says none, and nothing here changes.
    """
    numbers: dict[str, int] = {}
    if not isinstance(answer, dict):
        return numbers, 0
    for key in ("account_numbers", "version_numbers"):
        block = answer.get(key)
        if isinstance(block, dict):
            numbers.update({str(k): n for k, n in ((k, _whole(v)) for k, v in block.items()) if n is not None})
    for version in answer.get("versions") or ():
        if isinstance(version, dict) and version.get("version_id"):
            n = _whole(version.get("version_number"))
            if n is not None:
                numbers[str(version["version_id"])] = n
    return numbers, _whole(answer.get("version_number_mark")) or 0


def said_here(changes: Iterable[dict[str, Any]], account: dict[str, int]) -> str | None:
    """One plain sentence for the versions whose number changed on this
    computer to take the account's, or ``None`` when none did."""
    taken, moved = [], []
    for change in changes:
        if change.get("key") in account:
            taken.append(f"version {change['from']} is now version {change['to']}")
        else:
            moved.append(f"version {change['from']} is now version {change['to']}")
    if not taken and not moved:
        return None

    def listed(items: list[str]) -> str:
        return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"

    parts = []
    if taken:
        parts.append(f"{listed(taken)}, the number your Kiln account gave it")
    if moved:
        parts.append(f"{listed(moved)}, which is only here, because your account gave "
                     f"{'that number' if len(moved) == 1 else 'those numbers'} to another version")
    return "On this computer " + "; ".join(parts) + "."


def _write_json(path: Path, data: dict[str, Any]) -> None:
    import os

    part = path.with_name(f"{path.name}.{os.getpid()}.part")
    part.write_text(json.dumps(data, indent=2), encoding="utf-8")
    part.replace(path)


def renumber_folder(folder: str | Path, final: dict[str, int], *, held: bool = False) -> list[dict[str, Any]]:
    """Give the kept versions in *folder* the numbers in *final* (keys as in
    :func:`kept_numbers`): each moved version's file is renamed to its new
    number with that number inside, the head copy follows the version it
    is a copy of, and a recipe that names a moved file as its parent names
    it at its new place.  The folder's mark is raised past every number in
    *final*.

    Done under the numbers lock, so no save takes a number while versions
    move; *held* says the caller holds it already, having planned under it.
    The files move whole or not at all: each is set aside under a name no
    number names, every new file is written, and only then are the set-aside
    ones removed; a failure part way puts every file back as it was and
    raises.  A version is never written over another.  Returns
    ``[{"key", "from", "to"}]`` for each version moved.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    wanted = {k: n for k, n in ((k, _whole(v)) for k, v in final.items()) if n is not None}
    if held:
        return _renumber_held(root, wanted)
    with numbers_lock(root):
        return _renumber_held(root, wanted)


def _same_apart_from_number(head: dict[str, Any], kept: dict[str, Any]) -> bool:
    """Whether the head recipe is a copy of a kept one: the same record
    apart from the number and the id the head may not carry."""
    def bare(data: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in data.items() if k not in ("version", "version_id")}

    return bool(kept) and bare(head) == bare(kept)


def _renumber_held(root: Path, wanted: dict[str, int]) -> list[dict[str, Any]]:
    """:func:`renumber_folder`'s work, the numbers lock held."""
    import uuid

    files: dict[str, tuple[Path, int, dict[str, Any]]] = {}
    for path in recipe_files(root):
        if path.name == _HEAD:
            continue
        data = _read(path)
        if not data:
            continue  # unreadable: it keeps its name, and the number in it
        number = _number(data.get("version")) or _number_in_name(path)
        vid = data.get("version_id")
        key = vid if isinstance(vid, str) and vid else FILE_KEY + path.name
        if number is not None and key not in files:
            files[key] = (path, number, data)
    moving = {k: v for k, v in files.items() if k in wanted and wanted[k] != v[1]}
    aside: dict[str, Path] = {}
    written: list[Path] = []
    moved: dict[str, Path] = {}
    changes: list[dict[str, Any]] = []
    try:
        for key, (path, _n, _d) in moving.items():
            temp = root / f"{_PREFIX}.moving-{uuid.uuid4().hex[:12]}.json"
            path.replace(temp)
            aside[key] = temp
        for key, (old_path, old_number, data) in sorted(moving.items(), key=lambda kv: wanted[kv[0]]):
            new = wanted[key]
            target = root / f"{_PREFIX}.v{new}.json"
            if target.exists():
                # A file this plan does not move holds the name (its number
                # inside says otherwise): never written over.
                target = root / f"{_PREFIX}.moved-{uuid.uuid4().hex[:12]}.json"
            _write_json(target, {**data, "version": new})
            written.append(target)
            moved[old_path.name] = target
            changes.append({"key": key, "from": old_number, "to": new})
    except BaseException:
        for target in written:
            target.unlink(missing_ok=True)
        for key, temp in aside.items():
            if temp.exists():
                temp.replace(moving[key][0])
        raise
    for temp in aside.values():
        temp.unlink(missing_ok=True)
    # A version's proven recipes are kept under its number
    # (``.kiln_proven_recipe.v<n>.json``): they move with it, all set
    # aside first so two versions trading numbers never meet.
    proven_aside: dict[int, Path] = {}
    for change in changes:
        src = root / f"{_PROVEN}.v{change['from']}.json"
        if src.is_file():
            temp = root / f"{_PROVEN}.moving-{uuid.uuid4().hex[:12]}.json"
            src.replace(temp)
            proven_aside[change["to"]] = temp
    for new, temp in proven_aside.items():
        target = root / f"{_PROVEN}.v{new}.json"
        if target.exists():
            # Kept under a number no version here holds now: never
            # written over, and no longer read as any version's.
            target.replace(root / f"{_PROVEN}.v{new}.json.unclaimed-{uuid.uuid4().hex[:12]}")
        temp.replace(target)
    head = root / _HEAD
    head_data = _read(head)
    if head_data:
        vid = head_data.get("version_id")
        if isinstance(vid, str) and vid:
            key = vid if vid in wanted else None
        else:
            # A head that names no version (a save that wrote none into it)
            # follows the kept file it is a copy of, and names it from now on.
            key = next((c["key"] for c in changes if _same_apart_from_number(head_data, moving[c["key"]][2])), None)
        if key is not None and head_data.get("version") != wanted[key]:
            follows = {**head_data, "version": wanted[key]}
            if not key.startswith(FILE_KEY):
                follows["version_id"] = key
            _write_json(head, follows)
    if moved:
        for path in recipe_files(root):
            data = _read(path)
            parent = data.get("parent_version")
            if isinstance(parent, str) and parent:
                gone = Path(parent)
                if gone.name in moved and (not gone.is_absolute() or gone.parent.resolve() == root.resolve()):
                    _write_json(path, {**data, "parent_version": str(moved[gone.name].resolve())})
    raise_mark(root, max(wanted.values(), default=0))
    return changes
