"""Public-Kiln → Kiln's servers: a saved decoration's memory.

A decoration saved in the library (:mod:`kiln.decoration_library`) keeps
everything about it in its own folder on this computer.  Its memory -- the
settings it printed well with, its versions, a rollback -- is Kiln Pro
(https://kiln3d.com/pricing) and is worked out on Kiln's servers.  This
module sends what one change needs and carries out the plan that comes back
on the decoration's own folder.  Nothing is kept on Kiln's servers.

**What is sent**, per version file of the one decoration: its version number,
creation time, parent and changes, the settings it printed with (depth, mode,
style, counts and dates), its print count, the prints already counted and its
content type.  Never the decoration's name, text, tags, files or folder.

**What comes back** is a plan: which version file to copy to an archive name,
which version file the new version starts from, the fields to set on it, and
the name a new content file takes.  Every part is checked here before anything
is written -- names a decoration folder holds, fields a plan may set -- and a
plan that fails a check writes nothing.

Asked through the door every served tool uses (:func:`kiln.server._pro_api_call`).
A miss (offline, signed out, the servers silent or saying no) changes nothing
on disk and is raised as :class:`MemoryUnavailable`, carrying the answer.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

#: The served wire that answers for a decoration's memory.
WIRE_TOOL = "decoration_memory"
#: The version files a decoration folder holds.
_RECORD_NAME = re.compile(r"^manifest(?:\.v(\d{1,6}))?\.json$")
_ARCHIVE_NAME = re.compile(r"^manifest\.v\d{1,6}\.json$")
_CURRENT = "manifest.json"
#: A new content file's name, as a plan may give it.
_CONTENT_NAME = re.compile(r"^content\.v\d{1,6}\.[A-Za-z0-9]{1,8}$")
#: What leaves this computer, per version file.
_SENT_FIELDS = (
    "version", "created", "changes", "parent_version", "proven_settings",
    "print_count", "counted_jobs", "content_type",
)
#: How long a change nobody is waiting on (a print's outcome) waits.
_BACKGROUND_TIMEOUT_S = 8.0
#: What a plan may set on the new version.
_SET_FIELDS = frozenset({
    "proven_settings", "print_count", "counted_jobs", "version",
    "parent_version", "changes", "created",
})


class MemoryUnavailable(Exception):
    """Kiln's servers gave no plan (offline, signed out, silent, or a no)."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        super().__init__(str(answer.get("error") or "Kiln's servers gave no answer."))


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _records(deco_dir: Path) -> dict[str, dict[str, Any]]:
    """Each version file in *deco_dir*, cut down to what is sent."""
    out: dict[str, dict[str, Any]] = {}
    for child in sorted(deco_dir.iterdir()) if deco_dir.is_dir() else []:
        if not _RECORD_NAME.match(child.name) or not child.is_file():
            continue
        data = _read(child)
        if data is not None:
            out[child.name] = {k: data[k] for k in _SENT_FIELDS if k in data}
    return out


def ask(op: str, deco_dir: Path, *, asked_by_user: bool = True, **args: Any) -> dict[str, Any]:
    """Kiln's servers' answer for *op* on the decoration in *deco_dir*.

    :raises ValueError: the servers read the request and said it cannot be
        answered (an archived version that does not exist, say).
    :raises MemoryUnavailable: no answer (offline, signed out, silent, or a
        plan that does not include decoration memory).
    """
    from kiln.served_answer import classify_answer
    from kiln.server import _pro_api_call

    records = _records(deco_dir)
    if _CURRENT not in records:
        raise ValueError(f"Decoration folder has no readable {_CURRENT}: {deco_dir.name}")
    answer = _pro_api_call(
        WIRE_TOOL,
        # A change the person asked for waits as long as any served tool; one
        # made on the way to something else (a print's outcome) does not.
        _timeout=None if asked_by_user else _BACKGROUND_TIMEOUT_S,
        _asked_by_user=asked_by_user, op=op, records=records, **args,
    )
    if isinstance(answer, dict) and classify_answer(answer) is None and answer.get("op") == op:
        return answer
    if isinstance(answer, dict) and answer.get("code") == "INVALID_ARGUMENT":
        raise ValueError(str(answer.get("error") or "Kiln's servers could not use this request."))
    raise MemoryUnavailable(answer if isinstance(answer, dict) else {"error": "Kiln's servers gave no answer."})


def carry_out(
    deco_dir: Path,
    slug: str,
    plan: Any,
    *,
    content_path: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Carry out *plan* on the decoration in *deco_dir*; return the new
    ``manifest.json``.  Checked whole before anything is written.

    :raises ValueError: the plan names a file or field a decoration folder
        does not hold, or the version it starts from is missing.
    """
    from kiln.decoration_library import Decoration

    if not isinstance(plan, dict):
        raise ValueError("Kiln's servers sent no plan.")
    base = plan.get("base") or _CURRENT
    if base != _CURRENT and not (isinstance(base, str) and _ARCHIVE_NAME.match(base)):
        raise ValueError(f"Plan starts from {base!r}, which is not a decoration version file.")
    archives = plan.get("archive") or []
    if not isinstance(archives, list) or not all(
        isinstance(a, dict) and isinstance(a.get("as"), str) and _ARCHIVE_NAME.match(a["as"])
        for a in archives
    ):
        raise ValueError("Plan archives to a name that is not a decoration version file.")
    fields = plan.get("set") or {}
    if not isinstance(fields, dict) or set(fields) - _SET_FIELDS:
        raise ValueError("Plan sets a field a decoration's memory does not own.")
    copy_as = plan.get("copy_content_as")
    if copy_as is not None and not (isinstance(copy_as, str) and _CONTENT_NAME.match(copy_as)):
        raise ValueError("Plan names a content file a decoration folder does not hold.")
    if copy_as and not content_path:
        raise ValueError("Plan names a new content file but none was given.")

    current_path = deco_dir / _CURRENT
    base_record = _read(deco_dir / base)
    if base_record is None:
        raise ValueError(f"Version file {base} is missing or unreadable.")
    new = {**base_record, **fields, "slug": slug}
    for key, value in (overrides or {}).items():
        if value is not None:
            new[key] = value
    if copy_as:
        new["content_file"] = copy_as
    manifest = Decoration.from_dict(new).to_dict()  # refuses a record the library cannot read

    for archive in archives:
        target = deco_dir / archive["as"]
        if archive.get("only_if_absent") and target.exists():
            continue
        if current_path.exists():
            shutil.copy2(current_path, target)
    if copy_as:
        shutil.copy2(content_path, deco_dir / copy_as)
    current_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
