"""Which design, in which Kiln account, a design folder on this computer is synced with.

A design folder is tied to one design in one Kiln account by the first push,
pull or clone of it that succeeds.  From then on:

* a pull or clone whose answer is a different design (another account's
  design of the same name) is refused before anything here changes;
* a push carries the folder's tie (:func:`push_fields`), and Kiln's servers
  refuse a push into a different design before they keep anything of it;
* the version numbers an answer gives are taken here only when the answer is
  the tied design (:func:`numbers_may_be_taken`), so a version of one design
  is never renumbered for another.

The tie is one small file in the design folder (:data:`TIE_FILE`) holding the
key Kiln's servers name the design by (:data:`KEY_FIELD` in every push, pull
and clone answer).  The key is opaque and names no account; the file and
every refusal say "a different Kiln account" and nothing else about it.

A folder with no tie is tied by its next sync whose answer names a key, when
the two histories are one: the folder and the account's design share a
version (by id, or by the id an older Kiln wrote into a kept file's name), or
one side holds no version yet (an empty folder pulls or clones freely; a new
design's first push makes the account's design).  Two histories that share no
version are never joined: the pull or clone is refused before anything here
changes (:func:`judge`), and Kiln's servers refuse the push before they keep
anything (:func:`push_fields` carries the folder's history for that, and
:func:`related` is the rule they judge it by).  A tie file that cannot be read
is no tie.  An answer that names no key (a server that keeps none) ties
nothing and refuses nothing.

Nothing here runs on Kiln's servers: there each request works in a workspace
of one account's own, so nothing is judged and no tie is written.

Public Kiln's history courier and kiln-pro's sync both call this module, so
the two clients keep one rule and one wording.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The file in a design folder that names the design it is synced with.
TIE_FILE = ".kiln_tie.json"
#: The field of a push, pull or clone answer, and of a push's request, that
#: holds the design's key.
KEY_FIELD = "design_key"
#: The code of the refusal :func:`refusal` words.
REFUSED_CODE = "TIED_TO_ANOTHER_ACCOUNT"
#: The code of the refusal of two histories that share no version.
UNRELATED_CODE = "UNRELATED_HISTORIES"
#: The field of a push's request that names the versions the folder holds,
#: when it is tied to no design yet.
HISTORY_FIELD = "design_history"
#: A kept recipe file an older Kiln named by the first 12 hex of its
#: version's id, writing no id inside it.
_NAMED_BY_ID = re.compile(r"^\.kiln_recipe\.([0-9a-f]{12})\.json$")
#: The HTTP status Kiln's servers answer a refused push with.
REFUSED_STATUS = 412

_KEY = re.compile(r"^k1:[0-9a-f]{64}$")
_DOING = {"pull": "pull it from", "clone": "clone it from", "push": "push it to"}


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant
    except ImportError:  # a Kiln with no shared-server posture runs on a person's own computer
        return False
    return bool(is_hosted_multitenant())


def well_formed(key: Any) -> bool:
    """Whether *key* has the shape of a design key."""
    return isinstance(key, str) and _KEY.match(key) is not None


def key_in(answer: Any) -> str | None:
    """The design key a push, pull or clone answer names, or ``None``."""
    if not isinstance(answer, dict):
        return None
    key = answer.get(KEY_FIELD)
    return key if well_formed(key) else None


def tied_key(folder: str | Path | None) -> str | None:
    """The key the design folder *folder* is tied to, or ``None`` when it is
    tied to none (no tie file, or one that cannot be read)."""
    if folder is None:
        return None
    try:
        data = json.loads((Path(folder) / TIE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = data.get(KEY_FIELD) if isinstance(data, dict) else None
    return key if well_formed(key) else None


def push_fields(folder: str | Path | None, here: Iterable[Any] = ()) -> dict[str, Any]:
    """The fields a push of the design kept in *folder* carries: its tie,
    when it has one; else the versions the folder holds (*here*, and the
    recipe files read here), so Kiln's servers can refuse joining it with a
    design of the account that shares none of them."""
    if _hosted():
        return {}
    key = tied_key(folder)
    if key:
        return {KEY_FIELD: key}
    return {HISTORY_FIELD: sorted(history_kept_in(folder) | {str(v) for v in here if v})}


def versions_kept_in(folder: str | Path | None) -> set[str]:
    """The ids of the versions whose recipe files in *folder* name them."""
    if folder is None:
        return set()
    from kiln.version_numbers import FILE_KEY, kept_numbers

    return {key for key in kept_numbers(folder) if not key.startswith(FILE_KEY)}


def history_kept_in(folder: str | Path | None) -> set[str]:
    """The versions the recipe files in *folder* are: each id a file names,
    and the 12-hex start of an id an older Kiln wrote only into a file's name
    (:func:`related` matches it against whole ids)."""
    found = versions_kept_in(folder)
    if folder is not None and Path(folder).is_dir():
        for path in Path(folder).iterdir():
            m = _NAMED_BY_ID.match(path.name)
            if m and not any(v.startswith(m.group(1)) for v in found):
                found.add(m.group(1))
    return found


def holds_versions(folder: str | Path | None, here: Iterable[Any] = ()) -> bool:
    """Whether this computer keeps any version of the design in *folder*:
    a record (*here*) or a kept recipe file, whether or not it names one."""
    if any(here):
        return True
    if folder is None:
        return False
    from kiln.version_numbers import kept_numbers

    return bool(kept_numbers(folder)) or bool(history_kept_in(folder))


def related(history: Iterable[Any], held: Iterable[Any]) -> bool:
    """Whether a folder's *history* and an account design's *held* versions
    share a version: an id in both, or an id of *held* that starts with a
    12-hex start in *history*."""
    held_ids = {str(v) for v in held if v}
    for entry in (str(v) for v in history if v):
        if entry in held_ids:
            return True
        if len(entry) == 12 and any(h.startswith(entry) for h in held_ids):
            return True
    return False


def _versions_in(answer: dict[str, Any]) -> set[str]:
    """The versions of the design an answer says the account holds."""
    ids: set[str] = set()
    for key in ("account_numbers", "version_numbers"):
        block = answer.get(key)
        if isinstance(block, dict):
            ids |= {str(k) for k in block}
    for key in ("versions", "provenance_sidecars"):
        for row in answer.get(key) or ():
            if isinstance(row, dict) and row.get("version_id"):
                ids.add(str(row["version_id"]))
    for key in ("pushed_versions", "skipped_versions"):
        ids |= {str(v) for v in answer.get(key) or () if v}
    return ids


def refusal(
    design_id: str, doing: str, *, tool: str | None = None, code: str = REFUSED_CODE,
) -> dict[str, Any]:
    """The refusal of a *doing* (``pull``, ``clone`` or ``push``) of
    *design_id*: its design in the account is not the one this computer's
    folder is synced with (*code* :data:`REFUSED_CODE`), or the folder and
    the account's design are two histories that share no version
    (:data:`UNRELATED_CODE`).  Nothing changed, here or in the account."""
    verb = _DOING.get(doing, f"{doing} it with")
    elsewhere = f"Use another user on this computer for this account's {design_id}."
    if code == UNRELATED_CODE:
        said = (
            f"{design_id} on this computer and {design_id} in this Kiln account are two "
            f"different histories: they share no version, so Kiln did not {verb} this account, "
            f"and nothing changed. This folder keeps its own work. To have this account's "
            f"{design_id} on this computer as well, use another user on this computer: Kiln "
            f"keeps one design of each name for each user."
        )
        ways = ["Keep working in this folder: nothing of it was changed or sent.", elsewhere]
    else:
        code = REFUSED_CODE
        said = (
            f"{design_id} on this computer is synced with a design of that name in a different "
            f"Kiln account, so Kiln did not {verb} this account, and nothing changed. To keep "
            f"working with this folder, sign in to the Kiln account it is synced with. To have "
            f"this account's {design_id} on this computer as well, use another user on this "
            f"computer: Kiln keeps one design of each name for each user."
        )
        ways = ["Sign in to the Kiln account this folder is synced with, then run it again.", elsewhere]
    out: dict[str, Any] = {
        "status": "error",
        "success": False,
        "code": code,
        "error": said,
        "design_id": design_id,
        "changed": False,
        "ways_forward": ways,
    }
    if tool:
        out["tool"] = tool
    return out


@dataclass(frozen=True)
class Verdict:
    """One answer judged against a design folder's tie, before anything changes.

    *refused* is the answer to give instead (nothing changed); otherwise the
    sync goes on and :func:`keep` writes the tie when *ties* is set.
    """

    design_id: str
    folder: Path | None
    key: str | None
    doing: str
    refused: dict[str, Any] | None = None
    ties: bool = False


def judge(
    design_id: str,
    folder: str | Path | None,
    answer: Any,
    *,
    doing: str,
    here: Iterable[Any] | Callable[[], Iterable[Any]] = (),
    tool: str | None = None,
) -> Verdict:
    """Judge an *answer* about *design_id* against the tie of its folder.

    *doing* is ``pull``, ``clone`` or ``push``.  *here* names the versions
    this computer keeps of the design, beside the recipe files in *folder*
    (read here); a callable is called only when the folder is tied to no
    design.  For a push, *answer* is the account's reply.
    """
    path = Path(folder) if folder is not None else None
    key = key_in(answer)
    if _hosted() or path is None or key is None:
        return Verdict(design_id, path, key, doing)
    current = tied_key(path)
    if current is not None:
        if current == key:
            return Verdict(design_id, path, key, doing)
        return Verdict(design_id, path, key, doing, refused=refusal(design_id, doing, tool=tool))
    kept = here() if callable(here) else here
    records = [str(v) for v in kept or () if v]
    in_account = _versions_in(answer)
    if doing == "push":
        # What the account held before this push: what it holds now, less
        # the versions this push added.
        in_account -= {str(v) for v in answer.get("pushed_versions") or () if v}
    if in_account and holds_versions(path, records) and not related(
        set(records) | history_kept_in(path), in_account,
    ):
        # Two histories that share no version are never joined.
        return Verdict(design_id, path, key, doing,
                       refused=refusal(design_id, doing, tool=tool, code=UNRELATED_CODE))
    return Verdict(design_id, path, key, doing, ties=True)


def keep(verdict: Verdict) -> dict[str, Any] | None:
    """After a sync judged by :func:`judge` succeeded, write the folder's tie
    when it had none.  Returns ``{"tied": True}`` when a tie was written, else
    ``None``."""
    if not verdict.ties or verdict.refused is not None or verdict.key is None or _hosted():
        return None
    folder = verdict.folder
    if folder is None or not folder.is_dir():
        return None
    record = {
        KEY_FIELD: verdict.key,
        "tied_by": verdict.doing,
        "tied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    part = folder / f"{TIE_FILE}.{os.getpid()}.part"
    try:
        part.write_text(json.dumps(record, indent=2), encoding="utf-8")
        part.replace(folder / TIE_FILE)
    except OSError:
        part.unlink(missing_ok=True)
        return None
    return {"tied": True}


def numbers_may_be_taken(folder: str | Path | None, answer: Any) -> bool:
    """Whether the version numbers *answer* gives may be taken in *folder*:
    always for a folder tied to no design, and for a tied folder only when
    the answer names the design it is tied to."""
    if _hosted() or folder is None:
        return True
    current = tied_key(folder)
    return current is None or key_in(answer) == current


#: Said when an answer's numbers were not taken because it does not name the
#: design the folder is synced with.
NUMBERS_NOT_FOR_THIS_FOLDER = (
    "The version numbers in this answer were not taken on this computer: the answer did not "
    "name the design this folder is synced with, so nothing here was renumbered."
)


__all__ = [
    "KEY_FIELD",
    "NUMBERS_NOT_FOR_THIS_FOLDER",
    "REFUSED_CODE",
    "REFUSED_STATUS",
    "UNRELATED_CODE",
    "HISTORY_FIELD",
    "history_kept_in",
    "holds_versions",
    "related",
    "TIE_FILE",
    "Verdict",
    "judge",
    "keep",
    "key_in",
    "numbers_may_be_taken",
    "push_fields",
    "refusal",
    "tied_key",
    "versions_kept_in",
    "well_formed",
]
