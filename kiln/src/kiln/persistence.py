"""SQLite persistence layer for Kiln.

Provides durable storage for jobs, events, printers, and settings so that
state survives process restarts.  The database is created automatically at
``~/.kiln/kiln.db`` (override with the ``KILN_DB_PATH`` environment
variable).

Only stdlib modules are used (``sqlite3``, ``json``, ``os``, ``threading``).

Example::

    db = get_db()
    db.save_job({"id": "abc123", "file_name": "benchy.gcode", "status": "queued", ...})
    job = db.get_job("abc123")
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Default DB location
# ---------------------------------------------------------------------------

_DEFAULT_DB_DIR = os.path.join(str(Path.home()), ".kiln")
_DEFAULT_DB_PATH = os.path.join(_DEFAULT_DB_DIR, "kiln.db")


class KilnDB:
    """Thread-safe SQLite wrapper for Kiln persistence.

    Parameters:
        db_path: Filesystem path for the SQLite database file.  Defaults to
            the value of ``KILN_DB_PATH`` or ``~/.kiln/kiln.db``.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or os.environ.get("KILN_DB_PATH", _DEFAULT_DB_PATH)

        # Ensure the parent directory exists.
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._write_lock = threading.Lock()

        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create tables if they do not already exist."""
        with self._write_lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id              TEXT PRIMARY KEY,
                    file_name       TEXT NOT NULL,
                    printer_name    TEXT,
                    status          TEXT NOT NULL,
                    priority        INTEGER NOT NULL DEFAULT 0,
                    submitted_by    TEXT NOT NULL DEFAULT 'unknown',
                    submitted_at    REAL NOT NULL,
                    started_at      REAL,
                    completed_at    REAL,
                    error_message   TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type      TEXT NOT NULL,
                    source          TEXT NOT NULL DEFAULT '',
                    data            TEXT NOT NULL DEFAULT '{}',
                    timestamp       REAL NOT NULL,
                    created_at      REAL DEFAULT (strftime('%s', 'now'))
                );

                CREATE TABLE IF NOT EXISTS printers (
                    name            TEXT PRIMARY KEY,
                    printer_type    TEXT NOT NULL,
                    host            TEXT NOT NULL,
                    api_key         TEXT,
                    registered_at   REAL NOT NULL,
                    last_seen       REAL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key             TEXT PRIMARY KEY,
                    value           TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def save_job(self, job_dict: Dict[str, Any]) -> None:
        """Insert or replace a job record.

        The dict must contain at least ``id``, ``file_name``, ``status``,
        and ``submitted_at``.
        """
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO jobs
                    (id, file_name, printer_name, status, priority,
                     submitted_by, submitted_at, started_at, completed_at,
                     error_message)
                VALUES
                    (:id, :file_name, :printer_name, :status, :priority,
                     :submitted_by, :submitted_at, :started_at, :completed_at,
                     :error_message)
                """,
                {
                    "id": job_dict["id"],
                    "file_name": job_dict["file_name"],
                    "printer_name": job_dict.get("printer_name"),
                    "status": job_dict["status"],
                    "priority": job_dict.get("priority", 0),
                    "submitted_by": job_dict.get("submitted_by", "unknown"),
                    "submitted_at": job_dict["submitted_at"],
                    "started_at": job_dict.get("started_at"),
                    "completed_at": job_dict.get("completed_at"),
                    "error_message": job_dict.get("error_message"),
                },
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single job by ID, or ``None`` if not found."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return jobs ordered by priority DESC then submitted_at ASC.

        Args:
            status: Filter by status string, or ``None`` for all.
            limit: Maximum rows to return.
        """
        if status is not None:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? "
                "ORDER BY priority DESC, submitted_at ASC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs "
                "ORDER BY priority DESC, submitted_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        source: str = "",
        timestamp: Optional[float] = None,
    ) -> int:
        """Insert an event and return the row id."""
        ts = timestamp if timestamp is not None else time.time()
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO events (event_type, source, data, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (event_type, source, json.dumps(data), ts),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def recent_events(
        self,
        event_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return recent events, newest first.

        The ``data`` column is deserialised from JSON back into a dict.
        """
        if event_type is not None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE event_type = ? "
                "ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["data"] = json.loads(d["data"])
            results.append(d)
        return results

    # ------------------------------------------------------------------
    # Printers
    # ------------------------------------------------------------------

    def save_printer(
        self,
        name: str,
        printer_type: str,
        host: str,
        api_key: Optional[str] = None,
    ) -> None:
        """Insert or replace a printer record."""
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO printers
                    (name, printer_type, host, api_key, registered_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, printer_type, host, api_key, now, now),
            )
            self._conn.commit()

    def list_printers(self) -> List[Dict[str, Any]]:
        """Return all registered printers."""
        rows = self._conn.execute(
            "SELECT * FROM printers ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_printer(self, name: str) -> bool:
        """Delete a printer by name.  Returns ``True`` if a row was deleted."""
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM printers WHERE name = ?", (name,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_setting(
        self,
        key: str,
        default: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieve a setting value by key, or *default* if missing."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return row["value"]

    def set_setting(self, key: str, value: str) -> None:
        """Create or update a setting."""
        with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    @property
    def path(self) -> str:
        """The filesystem path of the database file."""
        return self._db_path


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_db: Optional[KilnDB] = None


def get_db() -> KilnDB:
    """Return the module-level :class:`KilnDB` singleton.

    The instance is lazily created on first call.
    """
    global _db
    if _db is None:
        _db = KilnDB()
    return _db
