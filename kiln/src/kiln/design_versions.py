"""Design version control for parametric designs.

Tracks version history of parametric OpenSCAD designs — diffs between
code revisions, rollback capability, and searchable history.  Each design
(identified by ``design_id``) maintains an ordered chain of versions with
automatic unified-diff computation between consecutive revisions.

**Provenance tracking** (v0.5.1+, requires kiln-pro): Each version can
carry provenance metadata, mesh fingerprints, and mesh diffs when
kiln-pro is installed.  The schema supports these fields even on the
free tier so that kiln-pro can enrich versions without schema changes.

Data is persisted in a SQLite database at ``~/.kiln/design_versions.db``
(configurable via the ``db_path`` constructor parameter).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.path.join(str(Path.home()), ".kiln", "design_versions.db")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class DesignVersion:
    """A single version of a parametric design.

    Attributes:
        version_id: Unique identifier for this version (UUID).
        design_id: Identifier grouping versions of the same design.
        scad_source: OpenSCAD source code for this version, or ``None``
            for mesh-only versions registered from external files
            (STL/3MF/OBJ) where no source code exists.  When ``None``,
            consumers should fall back to the mesh fingerprint stored
            in ``mesh_fingerprint`` for any geometry-aware operation.
        prompt: The natural-language prompt that produced this version.
        parameters: Parametric values (key → value) used for generation.
        diff_from_prev: Unified diff from the previous version, or ``None``
            if this is the first version OR if either side has no source.
        created_at: Unix timestamp when the version was saved.
        parent_version_id: The ``version_id`` of the preceding version, or
            ``None`` for the initial version.
        notes: Free-text notes attached to this version.
        version_number: Monotonic per-design sequence number, starting at
            1.  Mirrors the SQLite ``version_number`` column so callers
            (and sidecar writers) can name per-version artifacts without
            running a second query.  Defaults to 1 for freshly-constructed
            instances that have not yet been persisted.
    """

    version_id: str
    design_id: str
    scad_source: str | None
    prompt: str
    parameters: dict[str, Any]
    diff_from_prev: str | None
    created_at: float
    parent_version_id: str | None
    notes: str
    provenance: dict[str, Any] | None = field(default=None)
    mesh_fingerprint: dict[str, Any] | None = field(default=None)
    mesh_diff: dict[str, Any] | None = field(default=None)
    version_number: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        d = asdict(self)
        # Drop None provenance/fingerprint/diff to keep output clean
        for key in ("provenance", "mesh_fingerprint", "mesh_diff"):
            if d.get(key) is None:
                del d[key]
        return d


# ---------------------------------------------------------------------------
# Version store
# ---------------------------------------------------------------------------


class DesignVersionStore:
    """SQLite-backed store for design version history.

    Thread-safe: all database mutations are serialised through a
    :class:`threading.Lock`.

    :param db_path: Path to the SQLite database file.  Defaults to
        ``~/.kiln/design_versions.db``.
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._lock = threading.Lock()

        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS design_versions (
                version_id        TEXT PRIMARY KEY,
                design_id         TEXT NOT NULL,
                scad_source       TEXT,
                prompt            TEXT NOT NULL DEFAULT '',
                parameters        TEXT NOT NULL DEFAULT '{}',
                diff_from_prev    TEXT,
                created_at        REAL NOT NULL,
                parent_version_id TEXT,
                notes             TEXT NOT NULL DEFAULT '',
                version_number    INTEGER NOT NULL DEFAULT 1,
                provenance        TEXT,
                mesh_fingerprint  TEXT,
                mesh_diff         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dv_design_id
                ON design_versions(design_id);
            CREATE INDEX IF NOT EXISTS idx_dv_created_at
                ON design_versions(created_at);

            -- Alias table: tracks renames so "bob" → "robert" lineage
            -- is preserved.  Searching either name finds the full tree.
            CREATE TABLE IF NOT EXISTS design_aliases (
                alias       TEXT NOT NULL,
                design_id   TEXT NOT NULL,
                created_at  REAL NOT NULL,
                PRIMARY KEY (alias, design_id)
            );
            CREATE INDEX IF NOT EXISTS idx_da_alias
                ON design_aliases(alias);
            CREATE INDEX IF NOT EXISTS idx_da_design_id
                ON design_aliases(design_id);
            """
        )
        # Migrate existing databases that lack the new columns.
        for col in ("provenance", "mesh_fingerprint", "mesh_diff"):
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(
                    f"ALTER TABLE design_versions ADD COLUMN {col} TEXT"
                )
        # Drop NOT NULL from scad_source on legacy databases so external-
        # mesh imports (STL/3MF/OBJ) can register as first-class versions
        # without fabricating sentinel source code.  SQLite doesn't
        # support ALTER COLUMN DROP NOT NULL directly, so we use the
        # canonical create-copy-drop-rename dance.  Idempotent: a no-op
        # on databases already created with the nullable schema above.
        self._migrate_nullable_scad_source()
        self._conn.commit()

    def _migrate_nullable_scad_source(self) -> None:
        """Idempotently drop NOT NULL from ``design_versions.scad_source``.

        Uses ``PRAGMA table_info`` to detect the legacy NOT NULL constraint;
        if found, copies all rows into a fresh table with the relaxed
        constraint, drops the old table, and renames.  Existing rows are
        preserved bit-for-bit; new rows can carry NULL source for
        mesh-only imports.
        """
        cursor = self._conn.execute("PRAGMA table_info(design_versions)")
        cols = list(cursor.fetchall())
        if not cols:
            return  # table will be created fresh by the script above
        scad_col = next((c for c in cols if c[1] == "scad_source"), None)
        if scad_col is None or scad_col[3] == 0:
            # PRAGMA columns: (cid, name, type, notnull, dflt_value, pk)
            # notnull=0 means already nullable — nothing to do.
            return
        self._conn.executescript(
            """\
            CREATE TABLE design_versions__new (
                version_id        TEXT PRIMARY KEY,
                design_id         TEXT NOT NULL,
                scad_source       TEXT,
                prompt            TEXT NOT NULL DEFAULT '',
                parameters        TEXT NOT NULL DEFAULT '{}',
                diff_from_prev    TEXT,
                created_at        REAL NOT NULL,
                parent_version_id TEXT,
                notes             TEXT NOT NULL DEFAULT '',
                version_number    INTEGER NOT NULL DEFAULT 1,
                provenance        TEXT,
                mesh_fingerprint  TEXT,
                mesh_diff         TEXT
            );
            INSERT INTO design_versions__new
                (version_id, design_id, scad_source, prompt, parameters,
                 diff_from_prev, created_at, parent_version_id, notes,
                 version_number, provenance, mesh_fingerprint, mesh_diff)
            SELECT version_id, design_id, scad_source, prompt, parameters,
                   diff_from_prev, created_at, parent_version_id, notes,
                   version_number, provenance, mesh_fingerprint, mesh_diff
            FROM design_versions;
            DROP TABLE design_versions;
            ALTER TABLE design_versions__new RENAME TO design_versions;
            CREATE INDEX IF NOT EXISTS idx_dv_design_id
                ON design_versions(design_id);
            CREATE INDEX IF NOT EXISTS idx_dv_created_at
                ON design_versions(created_at);
            """
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_version(self, row: sqlite3.Row) -> DesignVersion:
        cols = set(row.keys())
        prov_raw = row["provenance"] if "provenance" in cols else None
        fp_raw = row["mesh_fingerprint"] if "mesh_fingerprint" in cols else None
        md_raw = row["mesh_diff"] if "mesh_diff" in cols else None
        return DesignVersion(
            version_id=row["version_id"],
            design_id=row["design_id"],
            scad_source=row["scad_source"],
            prompt=row["prompt"],
            parameters=json.loads(row["parameters"]),
            diff_from_prev=row["diff_from_prev"],
            created_at=row["created_at"],
            parent_version_id=row["parent_version_id"],
            notes=row["notes"],
            provenance=json.loads(prov_raw) if prov_raw else None,
            mesh_fingerprint=json.loads(fp_raw) if fp_raw else None,
            mesh_diff=json.loads(md_raw) if md_raw else None,
            version_number=int(row["version_number"]) if "version_number" in cols else 1,
        )

    @staticmethod
    def _compute_diff(
        old_source: str | None, new_source: str | None
    ) -> str | None:
        """Compute a unified diff between two source strings.

        Returns ``None`` when either side has no source (mesh-only
        version): a textual diff is meaningless without source code, so
        callers should fall back to the mesh fingerprint diff for
        geometry comparison.
        """
        if old_source is None or new_source is None:
            return None
        old_lines = old_source.splitlines(keepends=True)
        new_lines = new_source.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="previous",
            tofile="current",
        )
        return "".join(diff)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_version(
        self,
        design_id: str,
        scad_source: str | None = None,
        prompt: str = "",
        parameters: dict[str, Any] | None = None,
        notes: str = "",
    ) -> DesignVersion:
        """Save a new version for *design_id*.

        Automatically computes a unified diff from the previous version
        (if any), assigns a UUID version_id, and records the timestamp.

        ``scad_source=None`` is permitted and is the canonical shape for
        externally-imported mesh-only versions (STL/3MF/OBJ from
        Thingiverse, MakerWorld, etc.) where no source code exists.
        Such versions still participate in the version genealogy via
        their mesh fingerprint, but ``diff_from_prev`` will be ``None``.

        Returns the newly created :class:`DesignVersion`.

        Raises:
            ValueError: If *design_id* is empty/blank, or if any string
                argument contains null bytes.
        """
        if not design_id or not design_id.strip():
            raise ValueError("design_id must not be empty")
        # Null bytes can corrupt SQLite TEXT columns.  scad_source is
        # checked separately because None is now valid.
        for name, val in [
            ("design_id", design_id),
            ("prompt", prompt),
            ("notes", notes),
        ]:
            if "\x00" in val:
                raise ValueError(f"{name} must not contain null bytes")
        if scad_source is not None and "\x00" in scad_source:
            raise ValueError("scad_source must not contain null bytes")
        if parameters is None:
            parameters = {}

        version_id = uuid.uuid4().hex
        now = time.time()

        with self._lock:
            # Find the latest existing version for this design.
            prev = self._conn.execute(
                "SELECT * FROM design_versions "
                "WHERE design_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (design_id,),
            ).fetchone()

            if prev is not None:
                diff_text = self._compute_diff(prev["scad_source"], scad_source)
                parent_id: str | None = prev["version_id"]
                version_number = prev["version_number"] + 1
            else:
                diff_text = None
                parent_id = None
                version_number = 1

            self._conn.execute(
                "INSERT INTO design_versions "
                "(version_id, design_id, scad_source, prompt, parameters, "
                " diff_from_prev, created_at, parent_version_id, notes, version_number) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    design_id,
                    scad_source,
                    prompt,
                    json.dumps(parameters),
                    diff_text,
                    now,
                    parent_id,
                    notes,
                    version_number,
                ),
            )
            self._conn.commit()

        logger.info(
            "Saved version %s (#%d) for design %s",
            version_id,
            version_number,
            design_id,
        )
        return DesignVersion(
            version_id=version_id,
            design_id=design_id,
            scad_source=scad_source,
            prompt=prompt,
            parameters=parameters,
            diff_from_prev=diff_text,
            created_at=now,
            parent_version_id=parent_id,
            notes=notes,
            version_number=version_number,
        )

    def get_version(self, version_id: str) -> DesignVersion | None:
        """Retrieve a single version by its ID, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM design_versions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        return self._row_to_version(row) if row else None

    def list_versions(
        self, design_id: str, *, limit: int = 20
    ) -> list[DesignVersion]:
        """List versions for *design_id*, newest first."""
        if limit < 1:
            limit = 1
        rows = self._conn.execute(
            "SELECT * FROM design_versions "
            "WHERE design_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (design_id, limit),
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def diff_versions(self, version_id_a: str, version_id_b: str) -> str:
        """Return a unified diff between two arbitrary versions.

        Raises :class:`ValueError` if either version is not found.
        """
        va = self.get_version(version_id_a)
        vb = self.get_version(version_id_b)
        if va is None:
            raise ValueError(f"Version not found: {version_id_a}")
        if vb is None:
            raise ValueError(f"Version not found: {version_id_b}")
        return self._compute_diff(va.scad_source, vb.scad_source)

    def rollback(self, design_id: str, version_id: str) -> DesignVersion:
        """Create a new version that restores the source of *version_id*.

        The new version's ``notes`` field records the rollback origin.

        Raises :class:`ValueError` if the target version doesn't exist or
        belongs to a different design.
        """
        # Validate target inside the lock to avoid TOCTOU races.
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM design_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Version not found: {version_id}")
            target = self._row_to_version(row)
            if target.design_id != design_id:
                raise ValueError(
                    f"Version {version_id} belongs to design "
                    f"{target.design_id}, not {design_id}"
                )
        # save_version acquires its own lock.
        return self.save_version(
            design_id=design_id,
            scad_source=target.scad_source,
            prompt=target.prompt,
            parameters=target.parameters,
            notes=f"Rollback to version {version_id}",
        )

    def get_latest(self, design_id: str) -> DesignVersion | None:
        """Return the most recent version for *design_id*, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM design_versions "
            "WHERE design_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (design_id,),
        ).fetchone()
        return self._row_to_version(row) if row else None

    def delete_version(self, version_id: str) -> bool:
        """Delete a version by ID.  Returns ``True`` if it existed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM design_versions WHERE version_id = ?",
                (version_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def search_versions(
        self, query: str, *, limit: int = 10
    ) -> list[DesignVersion]:
        """Search versions by prompt or notes text (case-insensitive).

        LIKE metacharacters (``%``, ``_``) in *query* are escaped so the
        search is always a literal substring match.
        """
        if limit < 1:
            limit = 1
        # Escape LIKE metacharacters so user input is a literal substring.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        rows = self._conn.execute(
            "SELECT * FROM design_versions "
            "WHERE prompt LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    # ------------------------------------------------------------------
    # Aliases & ancestry
    # ------------------------------------------------------------------

    def add_alias(self, design_id: str, alias: str) -> None:
        """Register *alias* as an alternative name for *design_id*.

        Useful when a design is renamed (e.g. "bob" → "robert") — the
        alias preserves the link so searches on either name find the
        full version tree.

        Safe to call multiple times; duplicates are silently ignored.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO design_aliases "
                "(alias, design_id, created_at) VALUES (?, ?, ?)",
                (alias, design_id, time.time()),
            )
            self._conn.commit()

    def resolve_aliases(self, name: str) -> list[str]:
        """Return all design_ids associated with *name*.

        Checks both direct design_id matches and the alias table.
        Returns a deduplicated list of design_ids.
        """
        ids: set[str] = set()
        # Direct: name is a design_id
        row = self._conn.execute(
            "SELECT DISTINCT design_id FROM design_versions WHERE design_id = ?",
            (name,),
        ).fetchone()
        if row:
            ids.add(row[0])
        # Alias → design_id
        rows = self._conn.execute(
            "SELECT design_id FROM design_aliases WHERE alias = ?",
            (name,),
        ).fetchall()
        for r in rows:
            ids.add(r[0])
        # design_id → alias (reverse lookup)
        rows = self._conn.execute(
            "SELECT alias FROM design_aliases WHERE design_id = ?",
            (name,),
        ).fetchall()
        for r in rows:
            sub = self._conn.execute(
                "SELECT DISTINCT design_id FROM design_versions WHERE design_id = ?",
                (r[0],),
            ).fetchone()
            if sub:
                ids.add(sub[0])
        return sorted(ids)

    def get_ancestry(
        self, version_id: str, *, max_depth: int = 50
    ) -> list[DesignVersion]:
        """Walk the parent chain from *version_id* back to the root.

        Returns a list from the given version to the oldest ancestor,
        following parent_version_id pointers — even across design_id
        boundaries (renames).  Stops at the root or after *max_depth*
        hops to prevent infinite loops.
        """
        chain: list[DesignVersion] = []
        seen: set[str] = set()
        current_id: str | None = version_id

        for _ in range(max_depth):
            if current_id is None or current_id in seen:
                break
            seen.add(current_id)
            version = self.get_version(current_id)
            if version is None:
                break
            chain.append(version)
            current_id = version.parent_version_id

        return chain

    def rename_design(
        self, old_design_id: str, new_design_id: str
    ) -> int:
        """Rename a design, preserving version history and aliases.

        Creates an alias from *old_design_id* → *new_design_id* so
        searching for either name finds the full tree.  Future versions
        should use *new_design_id*.

        Returns the number of versions updated.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO design_aliases "
                "(alias, design_id, created_at) VALUES (?, ?, ?)",
                (old_design_id, new_design_id, time.time()),
            )
            cur = self._conn.execute(
                "UPDATE design_versions SET design_id = ? WHERE design_id = ?",
                (new_design_id, old_design_id),
            )
            self._conn.commit()
        count = cur.rowcount
        logger.info(
            "Renamed design %s → %s (%d versions updated)",
            old_design_id,
            new_design_id,
            count,
        )
        return count

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
