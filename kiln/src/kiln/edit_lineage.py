"""An edit of a part Kiln already holds is kept as that part's next version.

With kiln-pro installed (https://kiln3d.com), a tool that edits a mesh --
repair, rescale, texture, decorate and the rest -- keeps its result in the
version history of the part it edited, naming the tool that changed it, and
the result carries a ``saved_version`` block saying where it was kept.  A
part with no history yet gets one.

Without kiln-pro (:func:`begin_here` / :func:`finish_here`, called from the
tool dispatch and from :func:`around`), an edit of the NEWEST version of one
design saved on this computer is kept as that design's next version, the
same way, and the version records that an edit made it and whether a person
or their agent did.  Every other edit (of an older version, of a part two
designs share, of a file no design holds) is not kept, and its
``saved_version`` block says so in one line with the call that keeps it.
Which tools are edits, and the words, are the bundled manifest's
``edit_lineage`` block (:func:`load`), written by kiln-pro.

Entry points:

* :func:`install` -- run once by ``kiln serve`` after every tool has
  registered;
* :func:`around` -- for a door outside the tool registry that edits one file
  (a CLI command)::

      with edit_lineage.around("kiln repair", path) as edit:
          result = edit.finish(repair_stl(path, output_path=out))
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The key a result names where an edit was kept.
SAVED_VERSION_KEY = "saved_version"

#: Where a served answer's own ``saved_version`` (what Kiln's servers kept in
#: the person's account) goes when this computer keeps the edit too.
IN_ACCOUNT_KEY = "saved_version_in_account"

#: The version of the manifest block this build reads.
SCHEMA_VERSION = 1

#: What an install without kiln-pro says for the edits it does not keep.
#: Each names the call that keeps it.
NOTES_HERE = {
    "older": (
        "This part matches version {version} of your design “{name}”, which "
        "is not its newest version, so this edit was not added to its history. To "
        "keep it, branch from that version with create_design_branch(design_id="
        "\"{design_id}\", base_version_id=\"{version_id}\", annotation=<what the "
        "edit is for>) (Kiln names the branch) and save "
        "the edited file there with save_design_version(design_id=\"{design_id}\", "
        "scad_source=\"\", stl_path=<the edited file>)."
    ),
    "none": (
        "This part is not in any design saved on this computer, so this edit was "
        "not kept in a design's history. To keep it, save the edited file with "
        "save_design_version(design_id=<a name>, scad_source=\"\", stl_path=<the "
        "edited file>)."
    ),
    "which": (
        "Keep it in the right one with save_design_version(design_id=<that "
        "design>, scad_source=\"\", stl_path=<the edited file>)."
    ),
}

_lock = threading.Lock()
_doors: dict[str, dict[str, Any]] = {}
_people: set[str] = set()
_output_keys: tuple[str, ...] = ()
_notes: dict[str, str] = {}


def install(mcp: Any) -> dict[str, Any]:
    """Keep every registered edit tool's result in its part's history.

    Idempotent and never raises.  Returns ``{"installed": False}`` on an
    install without kiln-pro, where the tool dispatch keeps edits instead
    (:func:`begin_here`).
    """
    try:
        from kiln_pro.design_versions.edit_lineage import install as _install
    except ImportError:
        return {"installed": False}
    try:
        return _install(mcp)
    except Exception:  # noqa: BLE001 -- a server that will not start is worse
        logger.warning("edit lineage: not installed", exc_info=True)
        return {"installed": False}


def load(block: Any) -> None:
    """Take the manifest's ``edit_lineage`` block.  A block of a version this
    build cannot read keeps no edit here."""
    global _output_keys
    with _lock:
        _doors.clear()
        _people.clear()
        _notes.clear()
        _output_keys = ()
        if not isinstance(block, dict) or block.get("schema_version") != SCHEMA_VERSION:
            return
        for name, door in (block.get("doors") or {}).items():
            if isinstance(name, str) and isinstance(door, dict) and isinstance(door.get("says"), str):
                _doors[name] = door
        _people.update(n for n in block.get("people_doors") or () if isinstance(n, str))
        _output_keys = tuple(k for k in block.get("output_keys") or () if isinstance(k, str))
        _notes.update({k: v for k, v in (block.get("notes") or {}).items() if isinstance(v, str)})


# ---------------------------------------------------------------------------
# An install without kiln-pro: the design library on this computer
# ---------------------------------------------------------------------------


@dataclass
class _Held:
    """A saved version whose model is byte-for-byte the edited part."""

    design_id: str
    version: int
    version_id: str
    name: str
    recipe_path: Path


@dataclass
class Pending:
    """What was read before an edit ran.  Opaque to callers."""

    tool: str
    says: str
    by: str
    input_path: str
    sha: str
    decision: str
    parent: _Held | None = None
    held: list[_Held] = field(default_factory=list)
    #: A copy of the part, taken when the tool may write over the very file a
    #: version names; nothing in the library is touched until the edit succeeds.
    snapshot: str | None = None


def _kiln_pro_here() -> bool:
    return sys.modules.get("kiln_pro") is not None


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mesh_of(folder: Path, recipe: dict[str, Any]) -> Path | None:
    mesh = recipe.get("stl_path")
    if not isinstance(mesh, str) or not mesh:
        return None
    path = Path(mesh) if Path(mesh).is_absolute() else folder / mesh
    return path if path.is_file() else None


def _holds(path: Path | None, sha: str, size: int) -> bool:
    try:
        return path is not None and path.stat().st_size == size and _sha256(path) == sha
    except OSError:
        return False


def _decide(sha: str, size: int) -> tuple[str, _Held | None, list[_Held]]:
    """``("head", parent, held)`` when the part is the newest version of exactly
    one design here; else ``"older"``, ``"ambiguous"`` or ``"none"``."""
    from kiln.history_vault import _designs_root

    root = _designs_root()
    if not root.is_dir():
        return "none", None, []
    held: list[_Held] = []
    heads: dict[str, _Held] = {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in folder.glob(".kiln_recipe*.json"):
            recipe = _read(path)
            if not _holds(_mesh_of(folder, recipe), sha, size):
                continue
            entry = _Held(
                design_id=folder.name,
                version=int(recipe.get("version") or 0),
                version_id=str(recipe.get("version_id") or ""),
                name=str(recipe.get("name") or folder.name),
                recipe_path=path,
            )
            if path.name == ".kiln_recipe.json":
                heads[folder.name] = entry
            else:
                held.append(entry)
    designs = {h.design_id for h in held} | set(heads)
    if not designs:
        return "none", None, held
    if len(designs) > 1:
        return "ambiguous", None, held
    (design,) = designs
    if design in heads:
        head = heads[design]
        # The head's own snapshot carries the version's id when it has one.
        same = [h for h in held if h.version == head.version]
        return "head", (same[0] if same else head), held
    return "older", max(held, key=lambda h: h.version), held


def _records_naming(parent: _Held, input_path: str) -> list[Path]:
    """The records of *parent*'s design that name *input_path* as their model."""
    folder = parent.recipe_path.parent
    real = os.path.realpath(input_path)
    return [
        path for path in folder.glob(".kiln_recipe*.json")
        if (mesh := _mesh_of(folder, _read(path))) is not None and os.path.realpath(mesh) == real
    ]


def _keep_parent_apart(pending: Pending) -> None:
    """The part edited WAS the file a version names, and the edit may have
    written over it: that version keeps its own copy in its design's folder
    (the bytes read before the edit), and every record naming the file is
    pointed at the copy.  Run only after the edit succeeded."""
    parent, snapshot = pending.parent, pending.snapshot
    if parent is None or snapshot is None:
        return
    named = _records_naming(parent, pending.input_path)
    if not named:
        return
    folder = parent.recipe_path.parent
    suffix = Path(pending.input_path).suffix.lower() or ".stl"
    copy = folder / f"mesh.{pending.sha[:16]}.v{parent.version}{suffix}"
    if not copy.is_file():
        tmp = copy.with_name(copy.name + ".part")
        shutil.copyfile(snapshot, tmp)
        os.replace(tmp, copy)
    for path in named:
        recipe = _read(path)
        recipe["stl_path"] = str(copy)
        path.write_text(json.dumps(recipe, indent=2), encoding="utf-8")


def _snapshot(path: str) -> str:
    import tempfile

    folder = tempfile.mkdtemp(prefix="kiln_edit_parent_")
    copy = os.path.join(folder, os.path.basename(path))
    shutil.copyfile(path, copy)
    return copy


def discard_here(pending: Pending | None) -> None:
    """Drop what :func:`begin_here` set aside (the edit failed or never ran)."""
    if pending is not None and pending.snapshot:
        shutil.rmtree(os.path.dirname(pending.snapshot), ignore_errors=True)
        pending.snapshot = None


def _input_path(door: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    for name in door.get("inputs") or ():
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if path.is_file():
                return str(path.resolve())
    return None


def begin_here(tool: str, arguments: dict[str, Any] | None, *, by: str | None = None) -> Pending | None:
    """Read what an edit is about to change, on an install without kiln-pro;
    ``None`` for anything that is not such an edit.  Writes nothing anyone
    keeps: a part that is itself a version's file is copied aside, and the
    library is only touched by :func:`finish_here` once the edit succeeded.
    Never raises."""
    try:
        door = _doors.get(tool)
        arguments = arguments or {}
        if door is None or _kiln_pro_here():
            return None
        named = arguments.get("design_id")
        if isinstance(named, str) and named.strip():
            return None  # the door keeps it in the design it was told
        path = _input_path(door, arguments)
        if path is None:
            return None
        sha = _sha256(path)
        decision, parent, held = _decide(sha, os.path.getsize(path))
        snapshot = (
            _snapshot(path)
            if decision == "head" and parent is not None and _records_naming(parent, path)
            else None
        )
        return Pending(
            tool=tool,
            says=str(door["says"]),
            by=by or ("person" if tool in _people else "agent"),
            input_path=path,
            sha=sha,
            decision=decision,
            parent=parent,
            held=held,
            snapshot=snapshot,
        )
    except Exception:  # noqa: BLE001 -- the edit runs whatever happens here
        logger.warning("edit lineage: could not read %s's input", tool, exc_info=True)
        return None


def _envelope_of(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        dicts = [item for item in result if isinstance(item, dict)]
        if len(dicts) == 1:
            return dicts[0]
    return None


def _succeeded(envelope: dict[str, Any]) -> bool:
    return (
        envelope.get("success", True) is not False
        and envelope.get("ok", True) is not False
        and envelope.get("status") not in ("error", "failed")
    )


def _edited_file(door: dict[str, Any], envelope: dict[str, Any]) -> str | None:
    for key in (*(door.get("outputs") or ()), *_output_keys):
        value: Any = envelope
        for part in str(key).split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, str) and value.strip() and Path(value).expanduser().is_file():
            return str(Path(value).expanduser().resolve())
    return None


def _matches_line(held: list[_Held]) -> str:
    seen: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    for h in sorted(held, key=lambda x: (x.name.lower(), x.version)):
        seen.setdefault(h.design_id, []).append(h.version)
        names[h.design_id] = h.name
    return "; ".join(
        f"“{names[d]}” version {', '.join(str(n) for n in nums)}" for d, nums in seen.items()
    )


def finish_here(pending: Pending | None, result: Any) -> Any:
    """Keep a successful edit read by :func:`begin_here`, or say why it was
    not kept.  Never raises; the result comes back as the tool made it, plus
    the ``saved_version`` block."""
    if pending is None:
        return result
    envelope = _envelope_of(result)
    if envelope is None or not _succeeded(envelope):
        discard_here(pending)
        return result
    try:
        door = _doors.get(pending.tool) or {}
        output = _edited_file(door, envelope)
        if output is None:
            return result
        if _sha256(output) == pending.sha:
            block: dict[str, Any] = {"recorded": False, "unchanged": True, "note": _notes.get("unchanged", "")}
        elif pending.decision == "head" and pending.parent is not None:
            _keep_parent_apart(pending)
            block = _keep(pending, output)
        else:
            block = _not_kept(pending)
        served = envelope.get(SAVED_VERSION_KEY)
        if isinstance(served, dict):
            envelope[IN_ACCOUNT_KEY] = served
        envelope[SAVED_VERSION_KEY] = block
    except Exception:  # noqa: BLE001 -- the edited file is the answer
        logger.warning("edit lineage: %s's edit was not kept", pending.tool, exc_info=True)
        envelope[SAVED_VERSION_KEY] = {"recorded": False, "saved": False, "note": _notes.get("not_saved", "")}
    finally:
        discard_here(pending)
    return result


def _not_kept(pending: Pending) -> dict[str, Any]:
    if pending.decision == "ambiguous":
        note = _notes.get("ambiguous", "").format(matches=_matches_line(pending.held))
        note = f"{note} {NOTES_HERE['which']}".strip()
    elif pending.decision == "older" and pending.parent is not None:
        p = pending.parent
        note = NOTES_HERE["older"].format(
            version=p.version, name=p.name, design_id=p.design_id, version_id=p.version_id,
        )
    else:
        note = NOTES_HERE["none"]
    return {"recorded": False, "kept_here": False, "reason": pending.decision, "note": note}


def _keep(pending: Pending, output: str) -> dict[str, Any]:
    """Keep *output* as the next version of the design whose newest version
    the edit started from; the ``saved_version`` block."""
    from kiln.design_recipe import create_new_version, load_recipe, save_recipe
    from kiln.history_courier import after_local_save

    parent = pending.parent
    assert parent is not None
    folder = parent.recipe_path.parent
    head_path = folder / ".kiln_recipe.json"
    head = load_recipe(str(head_path))
    recipe = create_new_version(head, str(head_path), changes={"mesh": pending.says}, notes=pending.says)
    suffix = Path(output).suffix.lower() or ".stl"
    copy = folder / f"mesh.{_sha256(output)[:16]}{suffix}"
    if not copy.is_file():
        tmp = copy.with_name(copy.name + ".part")
        shutil.copyfile(output, tmp)
        os.replace(tmp, copy)
    recipe.stl_path = str(copy)
    provenance = dict(recipe.provenance or {})
    provenance["edit"] = {
        "tool": pending.tool,
        "by": pending.by,
        "derived_from": {
            "design_id": parent.design_id,
            "version_id": parent.version_id,
            "version": parent.version,
        },
        "summary": pending.says,
    }
    recipe.provenance = provenance
    save_recipe(recipe, str(folder))
    extra = after_local_save(parent.design_id)
    kept = _read(folder / f".kiln_recipe.v{recipe.version}.json")
    branch = "main"
    with contextlib.suppress(Exception):
        from kiln.history_vault import HistoryVault

        vault = HistoryVault()
        try:
            checked = vault.checked_out(parent.design_id)
            branch = str((checked or {}).get("name") or branch)
        finally:
            vault.close()
    block: dict[str, Any] = {
        "recorded": True,
        "store": "local",
        "design_id": parent.design_id,
        "name": recipe.name or parent.name,
        "version": recipe.version,
        "version_id": str(kept.get("version_id") or ""),
        "parent_version": parent.version,
        "parent_version_id": parent.version_id,
        "branch": branch,
        "forked": False,
        "tool": pending.tool,
        "by": pending.by,
        "found_by": "content",
        "note": _notes.get("kept", "").format(
            change=pending.says, version=recipe.version, name=recipe.name or parent.name,
            parent=parent.version, branch=branch,
        ),
    }
    if extra:
        block["after_save"] = extra
    return block


# ---------------------------------------------------------------------------
# A door outside the tool registry
# ---------------------------------------------------------------------------


class Edit:
    """One edit of one file, in progress (see :func:`around`)."""

    def __init__(self, door: str, path: str) -> None:
        self._pending: Any = None
        self._here: Pending | None = None
        try:
            from kiln_pro.design_versions.edit_lineage import begin_file, discard, finish
        except ImportError:
            self._here = begin_here(door, {"file_path": path}, by="person")
            return
        self._finish, self._discard = finish, discard
        self._pending = begin_file(door, path)

    def finish(self, result: Any) -> Any:
        """*result* -- the edit's answer, a dict naming the edited file under
        ``path`` or ``output_path`` -- plus ``saved_version`` when the edit was
        kept, or why it was not."""
        here, self._here = self._here, None
        if here is not None:
            return finish_here(here, result)
        pending, self._pending = self._pending, None
        if pending is None:
            return result
        return self._finish(pending, result)

    def drop(self) -> None:
        """The edit did not happen: keep nothing."""
        here, self._here = self._here, None
        discard_here(here)
        pending, self._pending = self._pending, None
        if pending is not None:
            self._discard(pending)


@contextlib.contextmanager
def around(door: str, path: str) -> Iterator[Edit]:
    """An :class:`Edit` of *path* by the door named *door*.  Read before the
    door writes, since it may overwrite *path*; call :meth:`Edit.finish` with
    its answer.  An exception, or no call to ``finish``, keeps nothing."""
    edit = Edit(door, path)
    try:
        yield edit
    finally:
        edit.drop()
