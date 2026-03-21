"""Design version control for parametric designs.

Tracks version history of parametric OpenSCAD designs — diffs between
code revisions, rollback capability, and searchable history.  Each design
(identified by ``design_id``) maintains an ordered chain of versions with
automatic unified-diff computation between consecutive revisions.

Data is persisted in a SQLite database at ``~/.kiln/design_versions.db``
(configurable via the ``db_path`` constructor parameter).

This is a **free-tier** feature.  The paid extension (print-outcome
correlation across versions) lives in kiln-pro.
"""

from __future__ import annotations

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
        scad_source: Full OpenSCAD source code for this version.
        prompt: The natural-language prompt that produced this version.
        parameters: Parametric values (key → value) used for generation.
        diff_from_prev: Unified diff from the previous version, or ``None``
            if this is the first version.
        created_at: Unix timestamp when the version was saved.
        parent_version_id: The ``version_id`` of the preceding version, or
            ``None`` for the initial version.
        notes: Free-text notes attached to this version.
    """

    version_id: str
    design_id: str
    scad_source: str
    prompt: str
    parameters: dict[str, Any]
    diff_from_prev: str | None
    created_at: float
    parent_version_id: str | None
    notes: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        return asdict(self)


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
                scad_source       TEXT NOT NULL,
                prompt            TEXT NOT NULL DEFAULT '',
                parameters        TEXT NOT NULL DEFAULT '{}',
                diff_from_prev    TEXT,
                created_at        REAL NOT NULL,
                parent_version_id TEXT,
                notes             TEXT NOT NULL DEFAULT '',
                version_number    INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_dv_design_id
                ON design_versions(design_id);
            CREATE INDEX IF NOT EXISTS idx_dv_created_at
                ON design_versions(created_at);
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_version(self, row: sqlite3.Row) -> DesignVersion:
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
        )

    @staticmethod
    def _compute_diff(old_source: str, new_source: str) -> str:
        """Compute a unified diff between two source strings."""
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
        scad_source: str,
        prompt: str = "",
        parameters: dict[str, Any] | None = None,
        notes: str = "",
    ) -> DesignVersion:
        """Save a new version for *design_id*.

        Automatically computes a unified diff from the previous version
        (if any), assigns a UUID version_id, and records the timestamp.

        Returns the newly created :class:`DesignVersion`.

        Raises:
            ValueError: If *design_id* or *scad_source* is empty/blank,
                or if any string argument contains null bytes.
        """
        if not design_id or not design_id.strip():
            raise ValueError("design_id must not be empty")
        if not scad_source:
            raise ValueError("scad_source must not be empty")
        # Null bytes can corrupt SQLite TEXT columns.
        for name, val in [
            ("design_id", design_id),
            ("scad_source", scad_source),
            ("prompt", prompt),
            ("notes", notes),
        ]:
            if "\x00" in val:
                raise ValueError(f"{name} must not contain null bytes")
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

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
