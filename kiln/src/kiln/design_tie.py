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

The tie is one small file in the design folder (:data:`TIE_FILE`) holding
what Kiln's servers name the design by in every push, pull and clone answer:
its key (:data:`KEY_FIELD`), which is one design row's own, and its line
(:data:`LINE_FIELD`), which every design of that name in that account shares.
Both are opaque and name no account; the file and every refusal say "a
different design" and nothing else about it.  The tie also names the design
id it was made for: a folder renamed or copied under another id carries a tie
that is not its own, and is tied to no design (:func:`tie_of`).

A tied folder whose answer names another key is refused, with one exception:
the answer names the folder's own line.  Then the account deleted its design
and made one of that name again, and the folder is judged as one tied to no
design is, by its history: joined again when the two share a version, refused
as two histories when they share none.  A different line is never judged by
history, so a folder cannot move between two accounts that hold one history.

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
#: The field of an answer, a push's request and a tie that holds the
#: design's line: what every design of one name in one account shares.
LINE_FIELD = "design_line"
#: The field of a tie that names the design id the tie was made for.
ID_FIELD = "design_id"
#: The code of the refusal :func:`refusal` words.
REFUSED_CODE = "TIED_TO_ANOTHER_DESIGN"
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

_KEY = re.compile(r"^k2:[0-9a-f]{64}$")
_LINE = re.compile(r"^k1:[0-9a-f]{64}$")
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


def _line_shaped(line: Any) -> bool:
    return isinstance(line, str) and _LINE.match(line) is not None


def named_in(data: Any) -> tuple[str | None, str | None]:
    """``(key, line)`` as *data* (an answer, a request or a tie) names them.

    An older Kiln named a design by its line alone, in the key's field: read
    here as a line and no key.
    """
    if not isinstance(data, dict):
        return None, None
    key, line = data.get(KEY_FIELD), data.get(LINE_FIELD)
    if _line_shaped(key) and not _line_shaped(line):
        key, line = None, key
    return (key if well_formed(key) else None), (line if _line_shaped(line) else None)


def key_in(answer: Any) -> str | None:
    """The design key a push, pull or clone answer names, or ``None``."""
    return named_in(answer)[0]


@dataclass(frozen=True)
class Tie:
    """The tie a design folder carries for its own design id."""

    key: str | None
    line: str | None
    design_id: str | None


def tie_of(folder: str | Path | None, design_id: str | None = None) -> Tie | None:
    """The tie of the design folder *folder*, or ``None`` when it is tied to
    no design: no tie file, one that cannot be read or names nothing, or one
    made for another design id than *design_id* (the folder's own name when
    left out), which a renamed or copied folder carries."""
    if folder is None:
        return None
    path = Path(folder)
    try:
        data = json.loads((path / TIE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key, line = named_in(data)
    if key is None and line is None:
        return None
    made_for = data.get(ID_FIELD)
    made_for = made_for if isinstance(made_for, str) and made_for else None
    if made_for is not None and made_for != (design_id or path.name):
        return None
    return Tie(key, line, made_for)


def tied_key(folder: str | Path | None, design_id: str | None = None) -> str | None:
    """The key the design folder *folder* is tied to, or ``None`` when its
    tie names none or it has no tie of its own (:func:`tie_of`)."""
    tie = tie_of(folder, design_id)
    return tie.key if tie is not None else None


def push_fields(
    folder: str | Path | None, here: Iterable[Any] = (), *, design_id: str | None = None,
) -> dict[str, Any]:
    """The fields a push of the design kept in *folder* carries: its tie,
    when it has one, and the versions the folder holds (*here*, and the
    recipe files read here), so Kiln's servers can refuse joining it with a
    design of the account that is another, or that shares none of them."""
    if _hosted():
        return {}
    tie = tie_of(folder, design_id)
    fields: dict[str, Any] = {}
    if tie is not None:
        if tie.key:
            fields[KEY_FIELD] = tie.key
        if tie.line:
            fields[LINE_FIELD] = tie.line
    fields[HISTORY_FIELD] = sorted(history_kept_in(folder) | {str(v) for v in here if v})
    return fields


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
    *design_id*: its design in the account is a different design from the
    one this computer's folder is synced with (*code* :data:`REFUSED_CODE`),
    or the folder and
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
            f"{design_id} on this computer is synced with a different design from the {design_id} "
            f"this Kiln account holds, so Kiln did not {verb} this account, and nothing changed. "
            f"To keep working with this folder, sign in to Kiln the way you were when it was "
            f"first synced. To have this account's {design_id} on this computer as well, use "
            f"another user on this computer: Kiln keeps one design of each name for each user."
        )
        ways = ["Sign in to Kiln the way you were when this folder was first synced, then run it again.",
                elsewhere]
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
    line: str | None = None


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
    key, line = named_in(answer)
    if _hosted() or path is None or (key is None and line is None):
        return Verdict(design_id, path, key, doing, line=line)
    current = tie_of(path, design_id)
    if current is not None:
        if key is not None and current.key == key:
            # The tied design.  A tie an older Kiln wrote is brought up to
            # date (its line, the id it was made for).
            whole = current.line == line and current.design_id == design_id
            return Verdict(design_id, path, key, doing, ties=not whole, line=line)
        if line is None or current.line != line:
            return Verdict(design_id, path, key, doing, line=line,
                           refused=refusal(design_id, doing, tool=tool))
        # The folder's own line under another key: the account made its
        # design of this name again.  Judged by history, as an untied folder is.
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
        return Verdict(design_id, path, key, doing, line=line,
                       refused=refusal(design_id, doing, tool=tool, code=UNRELATED_CODE))
    return Verdict(design_id, path, key, doing, ties=True, line=line)


def keep(verdict: Verdict) -> dict[str, Any] | None:
    """After a sync judged by :func:`judge` succeeded, write the folder's tie
    when it had none of its own for this design.  Returns ``{"tied": True}``
    when a tie was written, else ``None``."""
    if not verdict.ties or verdict.refused is not None or _hosted():
        return None
    if verdict.key is None and verdict.line is None:
        return None
    folder = verdict.folder
    if folder is None or not folder.is_dir():
        return None
    record = {
        **({KEY_FIELD: verdict.key} if verdict.key else {}),
        **({LINE_FIELD: verdict.line} if verdict.line else {}),
        ID_FIELD: verdict.design_id,
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


def numbers_may_be_taken(
    folder: str | Path | None, answer: Any, *, design_id: str | None = None, here: Iterable[Any] = (),
) -> bool:
    """Whether the version numbers *answer* gives may be taken in *folder*:
    always for a folder tied to no design, and for a tied folder only when
    the answer names the design it is tied to, or names the folder's line
    and a version the folder holds (*here*, and its recipe files): the
    design made again in the account from this same history."""
    if _hosted() or folder is None:
        return True
    current = tie_of(folder, design_id)
    if current is None:
        return True
    key, line = named_in(answer)
    if key is not None and key == current.key:
        return True
    if line is None or line != current.line:
        return False
    if key is None and current.key is None:
        return True  # both sides name the line alone: an older Kiln's whole rule
    in_account = _versions_in(answer) - {str(v) for v in answer.get("pushed_versions") or () if v}
    return not in_account or related({str(v) for v in here if v} | history_kept_in(folder), in_account)


#: Said when an answer's numbers were not taken because it does not name the
#: design the folder is synced with.
NUMBERS_NOT_FOR_THIS_FOLDER = (
    "The version numbers in this answer were not taken on this computer: the answer did not "
    "name the design this folder is synced with, so nothing here was renumbered."
)


__all__ = [
    "ID_FIELD",
    "KEY_FIELD",
    "LINE_FIELD",
    "NUMBERS_NOT_FOR_THIS_FOLDER",
    "REFUSED_CODE",
    "REFUSED_STATUS",
    "UNRELATED_CODE",
    "HISTORY_FIELD",
    "history_kept_in",
    "holds_versions",
    "related",
    "TIE_FILE",
    "Tie",
    "Verdict",
    "judge",
    "keep",
    "key_in",
    "named_in",
    "numbers_may_be_taken",
    "push_fields",
    "refusal",
    "tie_of",
    "tied_key",
    "versions_kept_in",
    "well_formed",
]
