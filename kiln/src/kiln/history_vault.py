"""This computer's design history: branches, versions and releases, as records.

The branch, merge, cherry-pick and release operations run on Kiln's servers
(they need a Kiln account on a plan that includes them).  The history they
work on stays here, on this computer, until the person pushes it.  This
module keeps that history as plain records, and nothing else: it has no
merge, no comparison and no judgment of its own.

**The record format is the contract.**  A design's history is the same shape
Kiln's cloud push, pull and clone move:

* ``branches``: ``branch_id``, ``design_id``, ``name``, ``parent_branch_id``,
  ``base_version_id``, ``head_version_id``, ``created_at``, ``created_by``,
  ``status``, ``annotation``, ``metadata``;
* ``versions``: ``version_id``, ``parent_version_id``, ``created_at``;
* ``memberships``: ``branch_id``, ``version_id``, ``position``, ``added_at``;
* ``provenance_sidecars``: ``version_id`` and ``recipe_dict`` (the version's
  recipe; ``mesh_bytes_b64`` carries its mesh on the wire);
* ``releases``: a signed release as the server returned it.

A call sends :meth:`HistoryVault.bundle` for the designs it names, and writes
back the delta the server answers with (:meth:`HistoryVault.apply_delta`).
Meshes are kept once each, by SHA-256, under ``~/.kiln/history_vault/meshes``.

**Saved features and decoration presets** are kept the same way, as plain
rows keyed by kind (``features`` / ``presets``): their branches, versions,
memberships and releases, in the shape their cloud clone and pull move.  A
preset version's art travels inside its record as ``image_asset_bytes_b64``
and is kept here once by SHA-256.

**The design library is the start of every history.**  A design saved with
the version tools lives under ``~/.kiln/designs/<design_id>/`` as recipe
files, one per version.  The first time a design is asked about, its versions
are recorded here in order on a ``main`` branch.  A version saved later joins
the end of the design's CHECKED-OUT branch: ``main`` until a branch is made
(which checks it out) or another is checked out (:meth:`HistoryVault.check_out`),
and the library's head recipe is that branch's head, so the next save
continues it.  Each recorded library file gets its ``version_id`` written into
it, so a file is never mistaken for another version saved under the same
number.  A version the server makes is written back into the library too, so
the version tools see it.

**A design can be locked to this computer** (:meth:`HistoryVault.set_locked`,
read anywhere with :func:`design_locked`).  A locked design is never saved to
the person's Kiln account: every door that would save it there asks first and
refuses.  The lock lives here, beside the history it keeps here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_RECIPE_HEAD = ".kiln_recipe.json"
_MESH_SUFFIXES = (".stl", ".3mf", ".obj", ".ply", ".glb")
_MESH_KEY = "mesh_bytes_b64"
_MAIN = "main"
#: The largest mesh a library version contributes to the records.
_MAX_LIBRARY_MESH = 64 * 1024 * 1024


def _kiln_home() -> Path:
    return Path(os.path.expanduser("~")) / ".kiln"


def _designs_root() -> Path:
    """Where the version tools keep the design library, read from them."""
    from kiln.plugins.version_tools import _DESIGNS_ROOT

    return Path(os.path.expanduser(_DESIGNS_ROOT))


def _design_folder(design_id: Any) -> Path | None:
    """The library folder of *design_id*, or ``None`` when it names anything else.

    A design id arrives from the server in every answer, so it is judged the
    way the version tools judge one (``kiln.plugins.version_tools._design_dir``):
    by the folder it resolves to, which must sit directly in the library.
    """
    if not isinstance(design_id, str) or not design_id.strip():
        return None
    root = os.path.realpath(_designs_root())
    target = os.path.realpath(os.path.join(root, design_id))
    if os.path.dirname(target) != root or target == root:
        return None
    return Path(target)


#: A mesh is filed under its SHA-256; anything else is not a mesh name.
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NOT_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


#: A feature version's part mesh travels beside it under this key, named by
#: ``part_asset_name``: the SHA-256 of its bytes and a mesh type.
_PART_KEY = "part_asset_b64"
_PART_SUFFIXES = (".stl", ".3mf", ".obj")


def _part_name(name: Any) -> bool:
    if not isinstance(name, str) or "." not in name:
        return False
    digest, ext = name.split(".", 1)
    return len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) and f".{ext}" in _PART_SUFFIXES


#: A proven recipe's file name beside its design (Kiln's servers apply the
#: same rule to what they lay and hand back), and how many and how large.
_PROVEN_NAME = re.compile(r"^\.kiln_proven_recipe(?:\.v\d{1,6})?\.json$")
_MAX_PROVEN = 500
_MAX_PROVEN_BYTES = 64 * 1024

#: What one proven recipe says, as a file holds it and the account keeps it
#: (the push route's body names the same words).
_RECIPE_FIELDS = (
    "decoration_type", "product_type", "parameters", "setup", "success",
    "notes", "proven_at", "source_design", "recorded_by", "confirmed",
)


def _proven_files(folder: Path | None) -> list[tuple[str, dict[str, Any]]]:
    """The proven-recipe files in one design's folder, as ``(name, record)``."""
    out: list[tuple[str, dict[str, Any]]] = []
    if folder is None or not folder.is_dir() or folder.is_symlink():
        return out
    for path in sorted(folder.iterdir()):
        if not _PROVEN_NAME.match(path.name) or path.is_symlink():
            continue
        try:
            if path.stat().st_size > _MAX_PROVEN_BYTES:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            out.append((path.name, record))
    return out


def _file_recipes(record: Any) -> list[dict[str, Any]]:
    """The recipes one file holds: ``{"recipes": [...]}``, or a single recipe
    written before a file held one per setup."""
    if isinstance(record, dict) and isinstance(record.get("recipes"), list):
        return [r for r in record["recipes"] if isinstance(r, dict)]
    if isinstance(record, dict) and record.get("decoration_type"):
        return [record]
    return []


def _proven_ref(name: str) -> str:
    """``"3"`` for ``.kiln_proven_recipe.v3.json``, ``""`` for the latest's file."""
    match = re.match(r"^\.kiln_proven_recipe\.v(\d+)\.json$", name)
    return match.group(1) if match else ""


def _proven_name(version_ref: Any) -> str | None:
    """The file a recipe for *version_ref* lives in, or ``None`` when the
    reference is not a version number."""
    ref = "" if version_ref is None else str(version_ref).strip()
    name = f".kiln_proven_recipe.v{ref}.json" if ref else ".kiln_proven_recipe.json"
    return name if _PROVEN_NAME.match(name) else None


def _recipe_key(recipe: dict[str, Any]) -> tuple[Any, ...]:
    """A recipe's place in its file: its decoration, product and setup (an
    unknown setup fact and an absent one are the same)."""
    setup = recipe.get("setup") if isinstance(recipe.get("setup"), dict) else {}
    return (
        str(recipe.get("decoration_type") or ""),
        str(recipe.get("product_type") or ""),
        tuple(sorted((str(k), str(v)) for k, v in setup.items() if v is not None)),
    )


def _standing(recipe: dict[str, Any]) -> tuple[bool, str]:
    """Confirmed by a person first, then the newer."""
    return (bool(recipe.get("confirmed", recipe.get("recorded_by") != "auto")), str(recipe.get("proven_at") or ""))


def _names_a_file(value: Any) -> bool:
    return isinstance(value, str) and (os.path.isabs(value) or value.startswith("~"))


#: What a log entry or a bisect session is about.
_ARTIFACT_TYPES = ("design", "preset", "feature")


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256.match(value) else None


def _file_tag(version_id: str) -> str:
    """A version id made safe to put in a file name."""
    tag = _NOT_ID_CHARS.sub("", version_id)[:12]
    return tag or hashlib.sha256(version_id.encode("utf-8")).hexdigest()[:12]


def _library_mesh(folder: Path, stl_path: Any) -> Path | None:
    """The mesh a library recipe names, when it is one this design keeps.

    The version tools copy a saved mesh into the design's own folder, so a
    path anywhere else is not this design's mesh, whoever wrote it there:
    it is never read, whatever it names.
    """
    if not isinstance(stl_path, str) or not stl_path:
        return None
    real = os.path.realpath(stl_path)
    if os.path.dirname(real) != os.path.realpath(folder):
        return None
    if Path(real).suffix.lower() not in _MESH_SUFFIXES or not os.path.isfile(real):
        return None
    if os.path.getsize(real) > _MAX_LIBRARY_MESH:
        return None
    return Path(real)


#: Each library kind's record keys: the interface contract of the records.
LIBRARY_KINDS: dict[str, dict[str, str]] = {
    "features": {"id": "feature_id", "version": "feature_version_id", "head": "head_feature_version_id"},
    "presets": {"id": "preset_id", "version": "preset_version_id", "head": "head_preset_version_id"},
}
_ART_KEY = "image_asset_bytes_b64"
_ART_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp")
#: The art one call carries, across every item it sends.
_ART_BUDGET = 8 * 1024 * 1024


def _rows(items: Any, *keys: str) -> list[dict[str, Any]]:
    """The well-formed rows of an answer's list: dicts carrying every *keys* as text."""
    if not isinstance(items, list):
        return []
    return [
        r for r in items
        if isinstance(r, dict) and all(isinstance(r.get(k), str) and r.get(k) for k in keys)
    ]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


#: Recipe keys holding a file path.  A path names a folder on this computer
#: (often the person's home folder), so it leaves as its bare file name.
_PATH_KEYS = frozenset({
    "stl_path", "mesh_path", "output_path", "imported_mesh_path", "final_3mf",
    "path", "file_path", "source_path", "3mf_path", "step_path",
})


def _without_local_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (os.path.basename(v) if k in _PATH_KEYS and isinstance(v, str) and os.path.isabs(v) else _without_local_paths(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_without_local_paths(v) for v in value]
    return value


def _main_branch_id(design_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"kiln-history-main:{design_id}").hex


#: Where this computer's short name is kept, and what one looks like.
_COMPUTER_KEY = "computer_name"
_COMPUTER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def _short_host_name() -> str:
    """The host name's first label, lower case, letters, digits and dashes only."""
    import socket

    try:
        raw = socket.gethostname() or ""
    except OSError:
        raw = ""
    label = raw.split(".", 1)[0].lower()
    name = re.sub(r"[^a-z0-9]+", "-", label).strip("-")[:32].strip("-")
    return name if _COMPUTER_NAME.match(name) else "computer"


def _lock_key(design_id: Any) -> str | None:
    """The one key a design's lock is kept and looked up under, or ``None``.

    The name of the folder *design_id* resolves to in the design library,
    folded to one case.  Every spelling that reaches the same folder (``./mug``,
    ``mug/``, ``Mug`` on a disk that ignores case) is the same design to the
    pushes, so it must be the same design to the lock; folding case can only
    make two designs share a lock, never let a locked one through.
    """
    folder = _design_folder(design_id)
    return None if folder is None else folder.name.casefold()


def design_locked(design_id: str) -> bool:
    """Whether *design_id* is locked to this computer.

    A locked design is never saved to the person's Kiln account; every door
    that would save one there asks this first (this package's pushes, and
    kiln-pro's, which imports it).  ``False`` when this computer keeps no
    history records at all.  ``True`` when it keeps them but they cannot be
    read: a push waits rather than send a design the person may have kept
    here.
    """
    if not isinstance(design_id, str) or not design_id.strip():
        return False
    if not (_kiln_home() / "history_vault.db").is_file():
        return False
    try:
        vault = HistoryVault()
    except sqlite3.Error:
        logger.warning("history: this computer's records could not be read; %s is treated as locked", design_id)
        return True
    try:
        return vault.is_locked(design_id)
    except sqlite3.Error:
        logger.warning("history: this computer's records could not be read; %s is treated as locked", design_id)
        return True
    finally:
        vault.close()


class HistoryVault:
    """SQLite records of this computer's design history.  Thread-safe."""

    def __init__(self, db_path: str | None = None, mesh_dir: str | None = None) -> None:
        home = _kiln_home()
        self._db_path = db_path or str(home / "history_vault.db")
        self._mesh_dir = Path(mesh_dir or home / "history_vault" / "meshes")
        self._art_dir = self._mesh_dir.parent / "art"
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS branches (
                branch_id TEXT PRIMARY KEY,
                design_id TEXT NOT NULL,
                record TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hv_branches_design ON branches(design_id);
            CREATE TABLE IF NOT EXISTS versions (
                version_id TEXT PRIMARY KEY,
                design_id TEXT NOT NULL,
                parent_version_id TEXT,
                created_at REAL NOT NULL,
                recipe TEXT NOT NULL,
                mesh_sha256 TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_hv_versions_design ON versions(design_id);
            CREATE TABLE IF NOT EXISTS memberships (
                branch_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                added_at REAL NOT NULL,
                PRIMARY KEY (branch_id, version_id)
            );
            CREATE TABLE IF NOT EXISTS releases (
                release_id TEXT PRIMARY KEY,
                design_id TEXT NOT NULL,
                record TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hv_releases_design ON releases(design_id);
            CREATE TABLE IF NOT EXISTS library_rows (
                kind TEXT NOT NULL,
                row_type TEXT NOT NULL,
                row_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                record TEXT NOT NULL,
                PRIMARY KEY (kind, row_type, row_id)
            );
            CREATE INDEX IF NOT EXISTS idx_hv_library_item ON library_rows(kind, item_id);
            CREATE TABLE IF NOT EXISTS checkouts (
                design_id TEXT PRIMARY KEY,
                branch_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS log_entries (
                reflog_id TEXT PRIMARY KEY,
                artifact_type TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                record TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hv_log_branch ON log_entries(branch_id, created_at);
            CREATE TABLE IF NOT EXISTS bisect_sessions (
                session_id TEXT PRIMARY KEY,
                artifact_type TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                status TEXT NOT NULL,
                record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS design_locks (
                design_id TEXT PRIMARY KEY,
                locked_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- lookups -----------------------------------------------------------

    def has_design(self, design_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM branches WHERE design_id = ? LIMIT 1", (design_id,)
        ).fetchone()
        return row is not None

    def design_of_branch(self, branch_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT design_id FROM branches WHERE branch_id = ?", (branch_id,)
        ).fetchone()
        return row["design_id"] if row else None

    def design_of_version(self, version_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT design_id FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return row["design_id"] if row else None

    def design_of_release(self, release_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT design_id FROM releases WHERE release_id = ?", (release_id,)
        ).fetchone()
        return row["design_id"] if row else None

    def branches(self, design_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT record FROM branches WHERE design_id = ?", (design_id,)
        ).fetchall()
        out = [json.loads(r["record"]) for r in rows]
        return sorted(out, key=lambda b: float(b.get("created_at") or 0.0))

    def branch(self, branch_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT record FROM branches WHERE branch_id = ?", (branch_id,)
        ).fetchone()
        return json.loads(row["record"]) if row else None

    def branch_named(self, design_id: str, name: str) -> dict[str, Any] | None:
        for b in self.branches(design_id):
            if b.get("name") == name and b.get("status") != "retired":
                return b
        return None

    def memberships(self, branch_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM memberships WHERE branch_id = ? ORDER BY position, added_at",
            (branch_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def checked_out(self, design_id: str) -> dict[str, Any] | None:
        """The branch this computer's saves of *design_id* continue (``main`` by default)."""
        row = self._conn.execute(
            "SELECT branch_id FROM checkouts WHERE design_id = ?", (design_id,)
        ).fetchone()
        branch = self.branch(row["branch_id"]) if row else None
        if branch is None or branch.get("status") == "retired" or branch.get("design_id") != design_id:
            branch = self.branch(_main_branch_id(design_id)) or self.branch_named(design_id, _MAIN)
        return branch

    def check_out(self, design_id: str, branch_id: str) -> dict[str, Any] | None:
        """Make *branch_id* the branch saves of *design_id* continue; ``None`` if it is not one.

        The library's head recipe becomes that branch's head, numbered after
        every version in the library, so the next save forks from it under a
        number no other version holds.
        """
        branch = self.branch(branch_id)
        if branch is None or branch.get("design_id") != design_id or branch.get("status") == "retired":
            return None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkouts (design_id, branch_id) VALUES (?, ?)",
                (design_id, branch_id),
            )
            self._conn.commit()
        self._point_head(design_id)
        return branch

    def version(self, version_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["recipe"] = json.loads(out["recipe"])
        return out

    def mesh_path(self, sha: str | None) -> Path | None:
        if not _sha(sha):
            return None
        for suffix in _MESH_SUFFIXES:
            path = self._mesh_dir / f"{sha}{suffix}"
            if path.is_file():
                return path
        return None

    def version_count(self, design_id: str) -> int:
        """How many versions of *design_id* this computer keeps."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM versions WHERE design_id = ?", (design_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # -- locks, and this computer's own settings ------------------------------

    def is_locked(self, design_id: str) -> bool:
        """Whether *design_id* is locked to this computer.

        Judged by the design's folder, not the spelling: ``./mug``, ``mug/``
        and, on a disk that ignores case, ``Mug`` all name the folder ``mug``
        does, so they are all the locked design (:func:`_lock_key`).
        """
        key = _lock_key(design_id)
        if key is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM design_locks WHERE design_id = ?", (key,)
        ).fetchone()
        return row is not None

    def set_locked(self, design_id: str, locked: bool) -> bool:
        """Lock *design_id* to this computer, or unlock it; the state it is now in."""
        key = _lock_key(design_id)
        if key is None:
            raise ValueError(f"design_id must name a design folder, not a path: {design_id!r}")
        with self._lock:
            if locked:
                self._conn.execute(
                    "INSERT OR IGNORE INTO design_locks (design_id, locked_at) VALUES (?, ?)",
                    (key, time.time()),
                )
            else:
                self._conn.execute("DELETE FROM design_locks WHERE design_id = ?", (key,))
            self._conn.commit()
        return self.is_locked(design_id)

    def locked_designs(self) -> list[str]:
        rows = self._conn.execute("SELECT design_id FROM design_locks ORDER BY design_id").fetchall()
        return [r["design_id"] for r in rows]

    def setting(self, key: str) -> Any:
        """A value this computer keeps for itself (``None`` when unset)."""
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    def set_setting(self, key: str, value: Any) -> None:
        """Keep *value* under *key*; ``None`` forgets it."""
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, _canonical(value)),
                )
            self._conn.commit()

    def computer_name(self) -> str:
        """This computer's short name, the same every time it is asked.

        Read from the host name the first time (``Adams-MacBook.local`` is
        ``adams-macbook``) and kept, so a later rename of the computer never
        renames the branches it already named.
        """
        kept = self.setting(_COMPUTER_KEY)
        if isinstance(kept, str) and _COMPUTER_NAME.match(kept):
            return kept
        name = _short_host_name()
        self.set_setting(_COMPUTER_KEY, name)
        return name

    def keep_branch_copy(self, branch: dict[str, Any], members: list[dict[str, Any]]) -> None:
        """Record *branch* with *members* as its versions, in their order.

        For a branch this computer made from another (its work kept under a
        name of its own): the versions are already here, so only the branch
        and which versions are on it are written.
        """
        with self._lock:
            self._put_branch(branch)
            self._conn.execute("DELETE FROM memberships WHERE branch_id = ?", (branch["branch_id"],))
            for position, m in enumerate(members):
                self._put_membership({
                    "branch_id": branch["branch_id"], "version_id": m["version_id"],
                    "position": position, "added_at": m.get("added_at") or time.time(),
                })
            self._conn.commit()

    # -- what a call sends ---------------------------------------------------

    def bundle(
        self,
        design_id: str,
        *,
        meshes_for: set[str] | frozenset[str] = frozenset(),
        mesh_budget: int = 16 * 1024 * 1024,
    ) -> dict[str, Any]:
        """One design's records, with the meshes of its heads and *meshes_for*.

        Mesh bytes ride only for the versions a call is likely to need them
        for (every branch head, and the versions it names), up to
        *mesh_budget* bytes; ``meshes_left_out`` lists the rest.
        """
        branches = self.branches(design_id)
        wanted = set(meshes_for) | {b["head_version_id"] for b in branches if b.get("head_version_id")}
        rows = self._conn.execute(
            "SELECT * FROM versions WHERE design_id = ? ORDER BY created_at", (design_id,)
        ).fetchall()
        versions, sidecars, left_out = [], [], []
        spent = 0
        for row in rows:
            recipe = _without_local_paths(json.loads(row["recipe"]))
            if row["version_id"] in wanted and row["mesh_sha256"]:
                path = self.mesh_path(row["mesh_sha256"])
                size = path.stat().st_size if path else 0
                if path and spent + size <= mesh_budget:
                    recipe[_MESH_KEY] = base64.b64encode(path.read_bytes()).decode("ascii")
                    recipe["stl_path"] = path.name
                    spent += size
                elif path:
                    left_out.append(row["version_id"])
            versions.append({
                "version_id": row["version_id"],
                "parent_version_id": row["parent_version_id"],
                "created_at": row["created_at"],
            })
            sidecars.append({"version_id": row["version_id"], "recipe_dict": recipe})
        memberships = []
        for b in branches:
            memberships.extend(self.memberships(b["branch_id"]))
        releases = [
            json.loads(r["record"])
            for r in self._conn.execute(
                "SELECT record FROM releases WHERE design_id = ?", (design_id,)
            ).fetchall()
        ]
        out = {
            "design_id": design_id,
            "branches": branches,
            "versions": versions,
            "memberships": memberships,
            "provenance_sidecars": sidecars,
            "releases": releases,
        }
        if left_out:
            out["meshes_left_out"] = left_out
        return out

    # -- writing ---------------------------------------------------------------

    _ID_COLUMN = {"versions": "version_id", "branches": "branch_id", "releases": "release_id"}

    def _is_designs(self, table: str, row_id: str, design_id: str) -> bool:
        """Whether *row_id* is free here or already *design_id*'s: an answer
        about one design never rewrites another design's records."""
        row = self._conn.execute(
            f"SELECT design_id FROM {table} WHERE {self._ID_COLUMN[table]} = ?", (row_id,)
        ).fetchone()
        return row is None or row["design_id"] == design_id

    def _put_mesh(self, data: bytes, suffix: str = ".stl") -> str:
        sha = hashlib.sha256(data).hexdigest()
        if self.mesh_path(sha) is None:
            self._mesh_dir.mkdir(parents=True, exist_ok=True)
            suffix = suffix if suffix in _MESH_SUFFIXES else ".stl"
            target = self._mesh_dir / f"{sha}{suffix}"
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return sha

    def keep_mesh(self, data: bytes, recipe: dict[str, Any]) -> str:
        """Keep a version's model here by its SHA-256, filed as *recipe*
        names it (the same file an answer carrying it would have kept)."""
        return self._put_mesh(data, Path(str(recipe.get("stl_path") or "")).suffix.lower())

    def _put_version(
        self, design_id: str, version: dict[str, Any], recipe: dict[str, Any],
        kept_sha: str | None = None,
    ) -> str | None:
        """Record one version; returns the mesh SHA it now points at.

        *kept_sha* is a model already kept here for this version, fetched on
        its own rather than carried in *recipe*.
        """
        recipe = dict(recipe)
        mesh_b64 = recipe.pop(_MESH_KEY, None)
        sha = _sha(recipe.get("mesh_sha256"))
        if mesh_b64:
            try:
                data = base64.b64decode(mesh_b64)
            except (ValueError, TypeError):
                data = b""
            if data:
                suffix = Path(str(recipe.get("stl_path") or "")).suffix.lower()
                sha = self._put_mesh(data, suffix)
        elif _sha(kept_sha) and self.mesh_path(kept_sha) is not None:
            sha = kept_sha
        recipe.setdefault("version_id", version["version_id"])
        self._conn.execute(
            "INSERT OR REPLACE INTO versions "
            "(version_id, design_id, parent_version_id, created_at, recipe, mesh_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                version["version_id"],
                design_id,
                version.get("parent_version_id"),
                float(version.get("created_at") or time.time()),
                _canonical(recipe),
                sha,
            ),
        )
        return sha

    def _put_branch(self, branch: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO branches (branch_id, design_id, record) VALUES (?, ?, ?)",
            (branch["branch_id"], branch["design_id"], _canonical(branch)),
        )

    def _put_membership(self, m: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO memberships (branch_id, version_id, position, added_at) "
            "VALUES (?, ?, ?, ?)",
            (m["branch_id"], m["version_id"], int(m.get("position") or 0), float(m.get("added_at") or time.time())),
        )

    def _put_release(self, release: dict[str, Any]) -> None:
        record = {k: v for k, v in release.items() if k != "new"}
        self._conn.execute(
            "INSERT OR REPLACE INTO releases (release_id, design_id, record) VALUES (?, ?, ?)",
            (record["release_id"], record.get("design_id") or "", _canonical(record)),
        )

    def apply_delta(self, delta: dict[str, Any]) -> dict[str, Any]:
        """Write what a served call changed in one design.

        A branch is moved only from the head it had when the call was sent
        (``expected_heads``): one moved here in the meantime is left as it is
        and named in ``branches_changed_here``.  Versions, memberships and
        releases are added; a version's recipe amendment replaces its recipe.
        """
        design_id = str(delta.get("design_id") or "")
        recipes = {
            r["version_id"]: r.get("recipe_dict") if isinstance(r.get("recipe_dict"), dict) else {}
            for r in _rows(delta.get("provenance_sidecars"), "version_id")
        }
        moved_here: list[str] = []
        written: list[dict[str, Any]] = []
        with self._lock:
            for v in _rows(delta.get("versions"), "version_id"):
                if not self._is_designs("versions", v["version_id"], design_id):
                    continue
                self._put_version(design_id, v, dict(recipes.get(v["version_id"]) or {}))
                written.append(self.version(v["version_id"]) or {})
            for update in _rows(delta.get("recipe_updates"), "version_id"):
                recipe = update.get("recipe_dict") if isinstance(update.get("recipe_dict"), dict) else {}
                self._conn.execute(
                    "UPDATE versions SET recipe = ? WHERE version_id = ? AND design_id = ?",
                    (_canonical(dict(recipe)), update["version_id"], design_id),
                )
            expected = delta.get("expected_heads") if isinstance(delta.get("expected_heads"), dict) else {}
            for branch in _rows(delta.get("branches"), "branch_id"):
                if branch.get("design_id") != design_id or not self._is_designs("branches", branch["branch_id"], design_id):
                    continue
                current = self.branch(branch["branch_id"])
                if branch["branch_id"] in expected:
                    now_head = None if current is None else current.get("head_version_id")
                    if current is not None and now_head != expected[branch["branch_id"]]:
                        moved_here.append(branch["branch_id"])
                        continue
                self._put_branch({**branch, "design_id": design_id})
            for m in _rows(delta.get("memberships"), "branch_id", "version_id"):
                if m["branch_id"] in moved_here or self.design_of_branch(m["branch_id"]) != design_id:
                    continue
                if self.design_of_version(m["version_id"]) not in (None, design_id):
                    continue
                self._put_membership(m)
            for release in _rows(delta.get("releases"), "release_id"):
                if not self._is_designs("releases", release["release_id"], design_id):
                    continue
                self._put_release({**release, "design_id": design_id})
            self._conn.commit()
        library = self._write_library(design_id, [w for w in written if w])
        return {
            "design_id": design_id,
            "versions_added": len(written),
            "branches_changed_here": moved_here,
            "library_files": library,
        }

    def apply_remote(
        self, payload: dict[str, Any], *, kept_models: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Record a pull or clone answer: the remote's branches as they are there.

        *kept_models* maps a version to the model already kept here for it
        (:meth:`keep_mesh`), for an answer that named its models rather than
        carrying them.
        """
        kept_models = kept_models or {}
        bundle_like = {
            "design_id": payload.get("design_id") or (payload.get("branch") or {}).get("design_id"),
            "branches": payload.get("branches") or ([payload["branch"]] if payload.get("branch") else []),
            "versions": payload.get("versions") or [],
            "memberships": payload.get("memberships") or [],
            "provenance_sidecars": payload.get("provenance_sidecars") or [],
            "releases": payload.get("releases") or [],
        }
        design_id = str(bundle_like["design_id"] or "")
        recipes = {
            r["version_id"]: r.get("recipe_dict") if isinstance(r.get("recipe_dict"), dict) else {}
            for r in _rows(bundle_like["provenance_sidecars"], "version_id")
        }
        branches = [
            b for b in _rows(bundle_like["branches"], "branch_id")
            if b.get("design_id") in (None, design_id) and self._is_designs("branches", b["branch_id"], design_id)
        ]
        bundle_like["branches"] = branches
        written = []
        with self._lock:
            for v in _rows(bundle_like["versions"], "version_id"):
                if not self._is_designs("versions", v["version_id"], design_id):
                    continue
                self._put_version(
                    design_id, v, dict(recipes.get(v["version_id"]) or {}),
                    kept_sha=kept_models.get(v["version_id"]),
                )
                written.append(self.version(v["version_id"]) or {})
            for b in branches:
                self._put_branch({**b, "design_id": design_id})
            for m in _rows(bundle_like["memberships"], "branch_id", "version_id"):
                if self.design_of_branch(m["branch_id"]) != design_id:
                    continue
                if self.design_of_version(m["version_id"]) not in (None, design_id):
                    continue
                self._put_membership({**m, "added_at": m.get("added_at") or time.time()})
            for r in _rows(bundle_like["releases"], "release_id"):
                if not self._is_designs("releases", r["release_id"], design_id):
                    continue
                self._put_release({**r, "design_id": design_id})
            self._conn.commit()
        self._write_library(design_id, [w for w in written if w])
        return {
            "design_id": design_id,
            "branches": len(bundle_like["branches"]),
            "versions": len(written),
            "releases": len(bundle_like["releases"]),
        }


    # -- saved features and decoration presets --------------------------------

    def _lib_put(self, kind: str, row_type: str, row_id: str, item_id: str, record: dict[str, Any]) -> bool:
        """Put one library row; ``False`` when *row_id* belongs to another item."""
        row = self._conn.execute(
            "SELECT item_id FROM library_rows WHERE kind = ? AND row_type = ? AND row_id = ?",
            (kind, row_type, row_id),
        ).fetchone()
        if row is not None and row["item_id"] != item_id:
            return False
        self._conn.execute(
            "INSERT OR REPLACE INTO library_rows (kind, row_type, row_id, item_id, record) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, row_type, row_id, item_id, _canonical(record)),
        )
        return True

    def _lib_rows(self, kind: str, row_type: str, item_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT record FROM library_rows WHERE kind = ? AND row_type = ? AND item_id = ?",
            (kind, row_type, item_id),
        ).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def library_items(self, kind: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT item_id FROM library_rows WHERE kind = ?", (kind,)
        ).fetchall()
        return [r["item_id"] for r in rows]

    def library_item_of(self, kind: str, row_type: str, row_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT item_id FROM library_rows WHERE kind = ? AND row_type = ? AND row_id = ?",
            (kind, row_type, row_id),
        ).fetchone()
        return row["item_id"] if row else None

    def has_library_item(self, kind: str, item_id: str) -> bool:
        return bool(self._conn.execute(
            "SELECT 1 FROM library_rows WHERE kind = ? AND item_id = ? LIMIT 1", (kind, item_id),
        ).fetchone())

    def _put_art(self, record: dict[str, Any]) -> dict[str, Any]:
        """Keep a version's art once by SHA-256; the record keeps only its name."""
        record = dict(record)
        art = record.pop(_ART_KEY, None)
        if art:
            try:
                data = base64.b64decode(art)
            except (ValueError, TypeError):
                data = b""
            if data:
                sha = hashlib.sha256(data).hexdigest()
                suffix = Path(str(record.get("image_asset_path") or "")).suffix.lower()
                suffix = suffix if suffix in _ART_SUFFIXES else ".png"
                target = self._art_dir / f"{sha}{suffix}"
                if not target.is_file():
                    self._art_dir.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(target.suffix + ".part")
                    tmp.write_bytes(data)
                    os.replace(tmp, target)
                record["image_asset_sha256"] = sha
        part = record.pop(_PART_KEY, None)
        name = record.get("part_asset_name")
        if part and _part_name(name):
            try:
                data = base64.b64decode(part)
            except (ValueError, TypeError):
                data = b""
            # Kept only as the bytes it names: never another part's mesh.
            if data and hashlib.sha256(data).hexdigest() == name.split(".", 1)[0]:
                target = self._art_dir / name
                if not target.is_file():
                    self._art_dir.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(target.suffix + ".part")
                    tmp.write_bytes(data)
                    os.replace(tmp, target)
        return _no_paths(record)

    def _art_path(self, sha: Any) -> Path | None:
        if not _sha(sha):
            return None
        for suffix in _ART_SUFFIXES:
            path = self._art_dir / f"{sha}{suffix}"
            if path.is_file():
                return path
        return None

    def _with_art(self, record: dict[str, Any], budget: list[int]) -> dict[str, Any]:
        record = self._with_part(record, budget)
        path = self._art_path(record.get("image_asset_sha256"))
        if path is None:
            return record
        size = path.stat().st_size
        if size > budget[0]:
            return record
        budget[0] -= size
        return {**record, _ART_KEY: base64.b64encode(path.read_bytes()).decode("ascii"),
                "image_asset_path": path.name}

    def _with_part(self, record: dict[str, Any], budget: list[int]) -> dict[str, Any]:
        """A feature version with the part mesh it imports, when it is kept here."""
        name = record.get("part_asset_name")
        if not _part_name(name):
            return record
        path = self._art_dir / name
        if not path.is_file() or path.stat().st_size > budget[0]:
            return record
        budget[0] -= path.stat().st_size
        return {**record, _PART_KEY: base64.b64encode(path.read_bytes()).decode("ascii")}

    def library_bundle(self, kind: str, item_id: str, *, budget: list[int] | None = None) -> dict[str, Any]:
        """One item's records, in the clone shape, art attached within *budget*."""
        keys = LIBRARY_KINDS[kind]
        budget = budget if budget is not None else [_ART_BUDGET]
        return {
            keys["id"]: item_id,
            "branches": self._lib_rows(kind, "branch", item_id),
            "versions": [self._with_art(v, budget) for v in self._lib_rows(kind, "version", item_id)],
            "memberships": self._lib_rows(kind, "membership", item_id),
            "releases": self._lib_rows(kind, "release", item_id),
        }

    def library_branch_payload(self, kind: str, item_id: str, branch_id: str) -> dict[str, Any] | None:
        """A branch, its versions (art attached) and memberships: what a push sends."""
        keys = LIBRARY_KINDS[kind]
        branch = next((b for b in self._lib_rows(kind, "branch", item_id) if b.get("branch_id") == branch_id), None)
        if branch is None:
            return None
        members = [m for m in self._lib_rows(kind, "membership", item_id) if m.get("branch_id") == branch_id]
        members.sort(key=lambda m: (int(m.get("position") or 0), float(m.get("added_at") or 0.0)))
        wanted = {m.get(keys["version"]) for m in members}
        budget = [_ART_BUDGET]
        versions = [
            self._with_art(v, budget) for v in self._lib_rows(kind, "version", item_id)
            if v.get(keys["version"]) in wanted
        ]
        return {"branch": branch, "versions": versions, "memberships": members}

    def _put_library(self, kind: str, item_id: str, payload: dict[str, Any], *, expected: dict[str, Any] | None) -> list[str]:
        """Write a delta's or a pull's rows for *item_id*; returns branches left as they were here."""
        keys = LIBRARY_KINDS[kind]
        moved_here: list[str] = []
        for branch in _rows(payload.get("branches"), "branch_id"):
            if branch.get(keys["id"]) not in (None, item_id):
                continue
            if expected is not None and branch["branch_id"] in expected:
                now = next(
                    (b for b in self._lib_rows(kind, "branch", item_id) if b.get("branch_id") == branch["branch_id"]),
                    None,
                )
                if now is not None and now.get(keys["head"]) != expected[branch["branch_id"]]:
                    moved_here.append(branch["branch_id"])
                    continue
            self._lib_put(kind, "branch", branch["branch_id"], item_id, {**branch, keys["id"]: item_id})
        for version in _rows(payload.get("versions"), keys["version"]):
            if version.get(keys["id"]) not in (None, item_id):
                continue
            self._lib_put(kind, "version", version[keys["version"]], item_id, self._put_art(version))
        for membership in _rows(payload.get("memberships"), "branch_id", keys["version"]):
            if membership["branch_id"] in moved_here:
                continue
            row_id = f"{membership['branch_id']}:{membership[keys['version']]}"
            self._lib_put(kind, "membership", row_id, item_id, membership)
        for release in _rows(payload.get("releases"), "release_id"):
            if release.get(keys["id"]) not in (None, item_id):
                continue
            self._lib_put(kind, "release", release["release_id"], item_id, {**release, keys["id"]: item_id})
        return moved_here

    def apply_library_delta(self, kind: str, delta: dict[str, Any]) -> dict[str, Any] | None:
        """Write what a served call changed in one feature or preset."""
        keys = LIBRARY_KINDS.get(kind)
        item_id = delta.get(keys["id"]) if keys else None
        if not isinstance(item_id, str) or not item_id:
            return None
        expected = delta.get("expected_heads") if isinstance(delta.get("expected_heads"), dict) else {}
        with self._lock:
            if delta.get("removed") is True:
                # Erased for good, after the two-call confirmation the tool asks for.
                self._conn.execute(
                    "DELETE FROM library_rows WHERE kind = ? AND item_id = ?", (kind, item_id),
                )
                self._conn.commit()
                return {"kind": kind, "item_id": item_id, "branches_changed_here": [], "removed": True}
            moved_here = self._put_library(kind, item_id, delta, expected=expected)
            flags = delta.get("archived_versions")
            for version in self._lib_rows(kind, "version", item_id) if isinstance(flags, dict) else []:
                vid = version.get(keys["version"])
                if vid in flags and isinstance(flags[vid], bool):
                    self._lib_put(kind, "version", vid, item_id, {**version, "archived": flags[vid]})
            self._conn.commit()
        return {"kind": kind, "item_id": item_id, "branches_changed_here": moved_here}

    def apply_library_remote(self, kind: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a pull or clone answer for one feature or preset."""
        bundle = {
            "branches": payload.get("branches") or ([payload["branch"]] if payload.get("branch") else []),
            "versions": payload.get("versions") or [],
            "memberships": payload.get("memberships") or [],
            "releases": payload.get("releases") or [],
        }
        with self._lock:
            self._put_library(kind, item_id, bundle, expected=None)
            self._conn.commit()
        return {"kind": kind, "item_id": item_id, "branches": len(bundle["branches"]),
                "versions": len(bundle["versions"]), "releases": len(bundle["releases"])}

    # -- the design library -----------------------------------------------------

    # -- the history log and bisect sessions --------------------------------
    #
    # The log is every head move Kiln's servers made for this computer (the
    # undo list); a bisect session is one search's place.  Both come back in
    # a served call's delta and go out with the calls that read them.

    def log_entries(self, *, branch_id: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        """Log entries, newest first; one branch's when *branch_id* is given."""
        if branch_id:
            rows = self._conn.execute(
                "SELECT record FROM log_entries WHERE branch_id = ? ORDER BY created_at DESC LIMIT ?",
                (branch_id, int(limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT record FROM log_entries ORDER BY created_at DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def log_entry(self, reflog_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT record FROM log_entries WHERE reflog_id = ?", (reflog_id,)
        ).fetchone()
        return json.loads(row["record"]) if row else None

    def bisect_sessions(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        q = "SELECT record FROM bisect_sessions"
        if active_only:
            q += " WHERE status = 'active'"
        return [json.loads(r["record"]) for r in self._conn.execute(q + " ORDER BY session_id").fetchall()]

    def bisect_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT record FROM bisect_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return json.loads(row["record"]) if row else None

    def apply_log_delta(self, delta: dict[str, Any]) -> dict[str, int]:
        """Keep the log entries and bisect sessions a served call answered with.

        A log entry is only ever added (one already here is left as it is); a
        bisect session is replaced by its newer state.  A row that is not one
        is skipped.
        """
        entries = sessions = 0
        with self._lock:
            for e in delta.get("reflog") or []:
                if not (isinstance(e, dict) and e.get("artifact_type") in _ARTIFACT_TYPES
                        and all(isinstance(e.get(k), str) and e.get(k)
                                for k in ("reflog_id", "artifact_id", "branch_id"))):
                    continue
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO log_entries "
                    "(reflog_id, artifact_type, artifact_id, branch_id, created_at, record) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (e["reflog_id"], e["artifact_type"], e["artifact_id"], e["branch_id"],
                     _number(e.get("created_at")), _canonical(_no_paths(e))),
                )
                entries += cur.rowcount
            for b in delta.get("bisect") or []:
                if not (isinstance(b, dict) and b.get("artifact_type") in _ARTIFACT_TYPES
                        and all(isinstance(b.get(k), str) and b.get(k) for k in ("session_id", "artifact_id"))):
                    continue
                # A session is its own search: an answer about one artifact
                # never rewrites a session kept here for another.
                held = self._conn.execute(
                    "SELECT artifact_type, artifact_id FROM bisect_sessions WHERE session_id = ?",
                    (b["session_id"],),
                ).fetchone()
                if held is not None and (held["artifact_type"], held["artifact_id"]) != (
                    b["artifact_type"], b["artifact_id"]
                ):
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO bisect_sessions "
                    "(session_id, artifact_type, artifact_id, status, record) VALUES (?, ?, ?, ?, ?)",
                    (b["session_id"], b["artifact_type"], b["artifact_id"],
                     "ended" if b.get("status") == "ended" else "active", _canonical(_no_paths(b))),
                )
                sessions += 1
            self._conn.commit()
        return {"log_entries": entries, "bisect_sessions": sessions}

    # -- proven recipes -----------------------------------------------------
    #
    # A proven recipe is a small file beside a design in the library: what
    # worked, recorded after a good print.  They go out with the calls that
    # look them up, and one Kiln's servers record comes back to be filed here.

    def proven_recipes(self) -> list[dict[str, Any]]:
        """Every proven recipe in the design library, as ``{design_id, name, record}``."""
        root = _designs_root()
        out: list[dict[str, Any]] = []
        if not root.is_dir():
            return out
        for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
            for name, record in _proven_files(folder):
                if len(out) >= _MAX_PROVEN:
                    return out
                out.append({"design_id": folder.name, "name": name, "record": record})
        return out

    def proven_rows(self, design_id: str) -> list[dict[str, Any]]:
        """*design_id*'s proven recipes as the Kiln account keeps them: one row
        per recipe, naming the version it was proven on (``version_ref``, empty
        for the design's latest) and no file on this computer."""
        rows: list[dict[str, Any]] = []
        for name, record in _proven_files(_design_folder(design_id)):
            for recipe in _file_recipes(record):
                row = {k: recipe[k] for k in _RECIPE_FIELDS if k in recipe}
                if isinstance(row.get("parameters"), dict):
                    row["parameters"] = {k: v for k, v in row["parameters"].items() if not _names_a_file(v)}
                rows.append({"version_ref": _proven_ref(name), **_no_paths(row)})
        return rows

    def merge_proven_rows(self, design_id: str, rows: Any) -> int:
        """Keep the account's proven recipes for *design_id* beside it here;
        how many files changed.

        A version's file holds one recipe per decoration, product and setup.
        A recipe a person confirmed is never replaced by one recorded
        automatically, and of two with the same standing the newer stays.
        """
        folder = _design_folder(design_id)
        if folder is None:
            return 0
        held = dict(_proven_files(folder))
        changed: dict[str, list[dict[str, Any]]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("design_id") not in (None, design_id):
                continue
            name = _proven_name(row.get("version_ref"))
            incoming = {k: row[k] for k in _RECIPE_FIELDS if k in row}
            if name is None or not incoming.get("decoration_type") or not incoming.get("product_type"):
                continue
            recipes = changed.get(name) or _file_recipes(held.get(name))
            at = next((i for i, r in enumerate(recipes) if _recipe_key(r) == _recipe_key(incoming)), None)
            if at is None:
                recipes = [*recipes, incoming]
            elif recipes[at] != incoming and _standing(incoming) >= _standing(recipes[at]):
                recipes = [*recipes[:at], incoming, *recipes[at + 1:]]
            else:
                continue
            changed[name] = recipes
        return self.keep_proven_recipes([
            {"design_id": design_id, "name": name, "record": {"recipes": recipes}}
            for name, recipes in changed.items()
        ])

    def keep_proven_recipes(self, rows: Any) -> int:
        """File each proven recipe a served call recorded beside its design; how many."""
        kept = 0
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("record"), dict):
                continue
            name = row.get("name")
            folder = _design_folder(row.get("design_id"))
            if folder is None or not isinstance(name, str) or not _PROVEN_NAME.match(name):
                continue
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_text(json.dumps(_no_paths(row["record"]), indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, target)
            kept += 1
        return kept

    def sync_from_library(self, design_id: str) -> int:
        """Record the library's versions of *design_id* that are not recorded yet.

        New ones join ``main`` in version order.  Returns how many were added.
        """
        folder = _design_folder(design_id)
        if folder is None or not folder.is_dir():
            return 0
        found: list[tuple[int, dict[str, Any], Path]] = []
        for path in folder.glob(".kiln_recipe*.json"):
            if path.name == _RECIPE_HEAD:
                continue
            try:
                recipe = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            number = recipe.get("version")
            if isinstance(number, int) and number > 0:
                found.append((number, recipe, path))
        if not found:
            return 0
        found.sort(key=lambda item: item[0])
        added = 0
        with self._lock:
            target = self.checked_out(design_id)
            if target is None:
                target = {
                    "branch_id": _main_branch_id(design_id),
                    "design_id": design_id,
                    "name": _MAIN,
                    "parent_branch_id": None,
                    "base_version_id": None,
                    "head_version_id": None,
                    "created_at": time.time(),
                    "created_by": None,
                    "status": "active",
                    "annotation": "",
                    "metadata": {},
                }
            head = target.get("head_version_id")
            position = len(self.memberships(target["branch_id"]))
            for _number, recipe, path in found:
                vid = recipe.get("version_id") if isinstance(recipe.get("version_id"), str) else None
                if vid and self.version(vid) is not None:
                    continue
                vid = vid or uuid.uuid4().hex
                recipe = dict(recipe)
                recipe["version_id"] = vid
                mesh = _library_mesh(folder, recipe.get("stl_path"))
                if mesh is not None:
                    recipe[_MESH_KEY] = base64.b64encode(mesh.read_bytes()).decode("ascii")
                self._put_version(
                    design_id,
                    {"version_id": vid, "parent_version_id": head, "created_at": _created_at(recipe, path)},
                    recipe,
                )
                self._put_membership({
                    "branch_id": target["branch_id"], "version_id": vid,
                    "position": position, "added_at": time.time(),
                })
                _stamp(path, vid)
                position += 1
                head = vid
                added += 1
            if added:
                self._put_branch({**target, "head_version_id": head})
            self._conn.commit()
        return added

    def _write_library(self, design_id: str, versions: list[dict[str, Any]]) -> list[str]:
        """Put each version the server made into the design library as a recipe file."""
        folder = _design_folder(design_id)
        if folder is None or not versions:
            return []
        folder.mkdir(parents=True, exist_ok=True)
        taken = {p.name: p for p in folder.glob(".kiln_recipe*.json")}
        written = []
        for v in versions:
            recipe = _library_recipe(v, design_id)
            mesh = self.mesh_path(v.get("mesh_sha256"))
            if mesh is not None:
                local = folder / f"mesh-{v['mesh_sha256'][:16]}{mesh.suffix}"
                if not local.is_file():
                    local.write_bytes(mesh.read_bytes())
                recipe["stl_path"] = str(local)
            number = recipe.get("version") if isinstance(recipe.get("version"), int) else None
            name = f".kiln_recipe.v{number}.json" if number else ""
            if not name or (name in taken and _version_in(taken[name]) != v["version_id"]):
                name = f".kiln_recipe.{_file_tag(v['version_id'])}.json"
            path = folder / name
            path.write_text(json.dumps(recipe, indent=2, sort_keys=True), encoding="utf-8")
            taken[name] = path
            written.append(str(path))
        self._point_head(design_id)
        return written

    def _point_head(self, design_id: str) -> None:
        """Make the library's head recipe the checked-out branch's head.

        Numbered after every version in the library, because the version
        tools number a save one past the head: a head numbered lower would
        have the next save written over another version's file.
        """
        folder = _design_folder(design_id)
        branch = self.checked_out(design_id)
        head = self.version(branch["head_version_id"]) if branch and branch.get("head_version_id") else None
        if folder is None or head is None:
            return
        numbers = [
            n for n in (_read(p).get("version") for p in folder.glob(".kiln_recipe*.json") if p.name != _RECIPE_HEAD)
            if isinstance(n, int)
        ]
        recipe = _library_recipe(head, design_id)
        recipe["version"] = max([*numbers, int(recipe.get("version") or 0)])
        mesh = self.mesh_path(head.get("mesh_sha256"))
        if mesh is not None:
            local = folder / f"mesh-{head['mesh_sha256'][:16]}{mesh.suffix}"
            if not local.is_file():
                local.write_bytes(mesh.read_bytes())
            recipe["stl_path"] = str(local)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _RECIPE_HEAD).write_text(json.dumps(recipe, indent=2, sort_keys=True), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _no_paths(value: Any) -> Any:
    """*value* without a file path anywhere in it.

    A recipe written into the library from a server's answer names a file on
    this computer only where this module put one (the local copy of its
    mesh); a path the answer carried is dropped, so no later read follows it.
    """
    if isinstance(value, dict):
        return {k: _no_paths(v) for k, v in value.items() if k not in _PATH_KEYS}
    if isinstance(value, list):
        return [_no_paths(v) for v in value]
    return value


def _library_recipe(version: dict[str, Any], design_id: str) -> dict[str, Any]:
    """A recorded version as a library recipe file the version tools can load.

    The fields every recipe file carries (``name``, ``created``) are filled
    from the version's own record when its recipe does not state them.
    """
    recipe = _no_paths(dict(version.get("recipe") or {}))
    recipe["version_id"] = version["version_id"]
    recipe.setdefault("name", recipe.get("design_id") or design_id)
    if not isinstance(recipe.get("created"), str):
        stamp = float(version.get("created_at") or time.time())
        recipe["created"] = datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    recipe.setdefault("design_id", design_id)
    return recipe


def _stamp(path: Path, version_id: str) -> None:
    """Write *version_id* into a library recipe file, so it names one version."""
    data = _read(path)
    if not data or data.get("version_id") == version_id:
        return
    data["version_id"] = version_id
    try:
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.debug("history: could not stamp %s: %s", path, exc)


def _version_in(path: Path) -> str | None:
    vid = _read(path).get("version_id")
    return vid if isinstance(vid, str) else None


def _created_at(recipe: dict[str, Any], path: Path) -> float:
    created = recipe.get("created")
    if isinstance(created, str):
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


__all__ = ["LIBRARY_KINDS", "SCHEMA_VERSION", "HistoryVault", "design_locked"]
