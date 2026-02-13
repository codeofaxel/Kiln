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

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._write_lock = threading.Lock()

        self._ensure_schema()
        self._migrate_agent_memory()
        self._enforce_permissions()

    # ------------------------------------------------------------------
    # File permissions
    # ------------------------------------------------------------------

    def _migrate_agent_memory(self) -> None:
        """Add version and expires_at columns to existing agent_memory tables."""
        cur = self._conn.cursor()
        # Check existing columns
        columns = {row[1] for row in cur.execute("PRAGMA table_info(agent_memory)").fetchall()}
        if "version" not in columns:
            cur.execute("ALTER TABLE agent_memory ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "expires_at" not in columns:
            cur.execute("ALTER TABLE agent_memory ADD COLUMN expires_at REAL DEFAULT NULL")
        self._conn.commit()

    def _enforce_permissions(self) -> None:
        """Enforce restrictive file permissions on the data directory and DB.

        Sets the parent directory to mode ``0700`` and the database file to
        mode ``0600`` so that other users on a shared system cannot read
        printer credentials or job history.  Skipped on Windows where POSIX
        chmod semantics do not apply.
        """
        if sys.platform == "win32":
            return

        db_dir = os.path.dirname(self._db_path)

        # --- Directory permissions ---
        try:
            dir_stat = os.stat(db_dir)
            dir_mode = stat.S_IMODE(dir_stat.st_mode)
            if dir_mode & 0o077:
                logger.warning(
                    "~/.kiln/ directory has overly permissive permissions "
                    "(mode %04o). Run 'chmod 700 ~/.kiln/' to fix.",
                    dir_mode,
                )
            os.chmod(db_dir, 0o700)
        except OSError as exc:
            logger.warning("Unable to set permissions on %s: %s", db_dir, exc)

        # --- Database file permissions ---
        try:
            file_stat = os.stat(self._db_path)
            file_mode = stat.S_IMODE(file_stat.st_mode)
            if file_mode & 0o077:
                logger.warning(
                    "Database file %s has overly permissive permissions "
                    "(mode %04o). Fixing to 0600.",
                    self._db_path,
                    file_mode,
                )
            os.chmod(self._db_path, 0o600)
        except OSError as exc:
            logger.warning(
                "Unable to set permissions on %s: %s", self._db_path, exc
            )

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

                CREATE TABLE IF NOT EXISTS printer_materials (
                    printer_name    TEXT NOT NULL,
                    tool_index      INTEGER NOT NULL DEFAULT 0,
                    material_type   TEXT NOT NULL,
                    color           TEXT,
                    spool_id        TEXT,
                    loaded_at       REAL NOT NULL,
                    remaining_grams REAL,
                    PRIMARY KEY (printer_name, tool_index)
                );

                CREATE TABLE IF NOT EXISTS spools (
                    id              TEXT PRIMARY KEY,
                    material_type   TEXT NOT NULL,
                    color           TEXT,
                    brand           TEXT,
                    weight_grams    REAL NOT NULL DEFAULT 1000.0,
                    remaining_grams REAL NOT NULL DEFAULT 1000.0,
                    cost_usd        REAL,
                    purchase_date   REAL,
                    notes           TEXT
                );

                CREATE TABLE IF NOT EXISTS leveling_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_name    TEXT NOT NULL,
                    triggered_by    TEXT NOT NULL DEFAULT 'manual',
                    started_at      REAL NOT NULL,
                    completed_at    REAL,
                    success         INTEGER DEFAULT 0,
                    mesh_data       TEXT,
                    trigger_reason  TEXT
                );

                CREATE TABLE IF NOT EXISTS sync_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type     TEXT NOT NULL,
                    entity_id       TEXT NOT NULL,
                    synced_at       REAL NOT NULL,
                    sync_direction  TEXT NOT NULL DEFAULT 'push',
                    status          TEXT NOT NULL DEFAULT 'success'
                );
                CREATE INDEX IF NOT EXISTS idx_sync_log_entity
                    ON sync_log(entity_type, entity_id);

                CREATE TABLE IF NOT EXISTS billing_charges (
                    id              TEXT PRIMARY KEY,
                    job_id          TEXT NOT NULL UNIQUE,
                    order_id        TEXT,
                    fee_amount      REAL NOT NULL,
                    fee_percent     REAL NOT NULL,
                    job_cost        REAL NOT NULL,
                    total_cost      REAL NOT NULL,
                    currency        TEXT NOT NULL DEFAULT 'USD',
                    waived          INTEGER NOT NULL DEFAULT 0,
                    waiver_reason   TEXT,
                    payment_id      TEXT,
                    payment_rail    TEXT,
                    payment_status  TEXT NOT NULL DEFAULT 'pending',
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_billing_charges_job
                    ON billing_charges(job_id);
                CREATE INDEX IF NOT EXISTS idx_billing_charges_created
                    ON billing_charges(created_at);

                CREATE TABLE IF NOT EXISTS payment_methods (
                    id              TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    rail            TEXT NOT NULL,
                    provider_ref    TEXT NOT NULL,
                    method_ref      TEXT,
                    label           TEXT,
                    is_default      INTEGER NOT NULL DEFAULT 0,
                    created_at      REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id              TEXT PRIMARY KEY,
                    charge_id       TEXT NOT NULL,
                    provider_id     TEXT NOT NULL,
                    rail            TEXT NOT NULL,
                    amount          REAL NOT NULL,
                    currency        TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    tx_hash         TEXT,
                    error           TEXT,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_payments_charge
                    ON payments(charge_id);

                CREATE TABLE IF NOT EXISTS print_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id          TEXT NOT NULL,
                    printer_name    TEXT NOT NULL,
                    file_name       TEXT,
                    status          TEXT NOT NULL,
                    duration_seconds REAL,
                    material_type   TEXT,
                    file_hash       TEXT,
                    slicer_profile  TEXT,
                    notes           TEXT,
                    agent_id        TEXT,
                    metadata        TEXT,
                    started_at      REAL,
                    completed_at    REAL,
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_print_history_printer
                    ON print_history(printer_name);
                CREATE INDEX IF NOT EXISTS idx_print_history_status
                    ON print_history(status);

                CREATE TABLE IF NOT EXISTS agent_memory (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id        TEXT NOT NULL,
                    scope           TEXT NOT NULL,
                    key             TEXT NOT NULL,
                    value           TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL,
                    version         INTEGER NOT NULL DEFAULT 1,
                    expires_at      REAL DEFAULT NULL,
                    UNIQUE(agent_id, scope, key)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_memory_agent
                    ON agent_memory(agent_id, scope);

                CREATE TABLE IF NOT EXISTS print_outcomes (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id          TEXT NOT NULL,
                    printer_name    TEXT NOT NULL,
                    file_name       TEXT,
                    file_hash       TEXT,
                    material_type   TEXT,
                    outcome         TEXT NOT NULL,
                    quality_grade   TEXT,
                    failure_mode    TEXT,
                    settings        TEXT,
                    environment     TEXT,
                    notes           TEXT,
                    agent_id        TEXT,
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_print_outcomes_printer
                    ON print_outcomes(printer_name);
                CREATE INDEX IF NOT EXISTS idx_print_outcomes_file
                    ON print_outcomes(file_hash);
                CREATE INDEX IF NOT EXISTS idx_print_outcomes_outcome
                    ON print_outcomes(outcome);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_print_outcomes_job_id
                    ON print_outcomes(job_id);
                CREATE INDEX IF NOT EXISTS idx_print_outcomes_material
                    ON print_outcomes(material_type);

                CREATE TABLE IF NOT EXISTS safety_audit_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL NOT NULL,
                    tool_name       TEXT NOT NULL,
                    safety_level    TEXT NOT NULL,
                    action          TEXT NOT NULL,
                    agent_id        TEXT,
                    printer_name    TEXT,
                    details         TEXT,
                    created_at      REAL DEFAULT (strftime('%s', 'now'))
                );
                CREATE INDEX IF NOT EXISTS idx_audit_tool
                    ON safety_audit_log(tool_name);
                CREATE INDEX IF NOT EXISTS idx_audit_action
                    ON safety_audit_log(action);
                CREATE INDEX IF NOT EXISTS idx_audit_time
                    ON safety_audit_log(timestamp);

                CREATE TABLE IF NOT EXISTS model_cache (
                    cache_id        TEXT PRIMARY KEY,
                    file_name       TEXT NOT NULL,
                    file_path       TEXT NOT NULL,
                    file_hash       TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    source          TEXT NOT NULL,
                    source_id       TEXT,
                    prompt          TEXT,
                    tags            TEXT,
                    dimensions      TEXT,
                    print_count     INTEGER DEFAULT 0,
                    last_printed_at REAL,
                    created_at      REAL NOT NULL,
                    metadata        TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_model_cache_hash
                    ON model_cache(file_hash);
                CREATE INDEX IF NOT EXISTS idx_model_cache_source
                    ON model_cache(source);

                CREATE TABLE IF NOT EXISTS fulfillment_orders (
                    id              TEXT PRIMARY KEY,
                    order_id        TEXT NOT NULL UNIQUE,
                    provider        TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'submitted',
                    file_path       TEXT NOT NULL,
                    material_id     TEXT NOT NULL,
                    quantity        INTEGER NOT NULL DEFAULT 1,
                    total_price     REAL NOT NULL DEFAULT 0.0,
                    currency        TEXT NOT NULL DEFAULT 'USD',
                    shipping_address TEXT,
                    tracking_url    TEXT,
                    tracking_number TEXT,
                    quote_id        TEXT,
                    notes           TEXT,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fulfillment_orders_order
                    ON fulfillment_orders(order_id);
                CREATE INDEX IF NOT EXISTS idx_fulfillment_orders_status
                    ON fulfillment_orders(status);
                CREATE INDEX IF NOT EXISTS idx_fulfillment_orders_provider
                    ON fulfillment_orders(provider);

                CREATE TABLE IF NOT EXISTS snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id          TEXT,
                    printer_name    TEXT NOT NULL,
                    phase           TEXT NOT NULL DEFAULT 'unknown',
                    image_path      TEXT NOT NULL,
                    image_size_bytes INTEGER,
                    analysis         TEXT,
                    agent_notes      TEXT,
                    confidence       REAL,
                    completion_pct   REAL,
                    created_at       REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_job
                    ON snapshots(job_id);
                CREATE INDEX IF NOT EXISTS idx_snapshots_printer
                    ON snapshots(printer_name);
                CREATE INDEX IF NOT EXISTS idx_snapshots_phase
                    ON snapshots(phase);
                """
            )

            # Add hmac_signature column to safety_audit_log if missing.
            try:
                cur.execute(
                    "ALTER TABLE safety_audit_log ADD COLUMN hmac_signature TEXT"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists.

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
    # Materials
    # ------------------------------------------------------------------

    def save_material(
        self,
        printer_name: str,
        tool_index: int,
        material_type: str,
        color: Optional[str] = None,
        spool_id: Optional[str] = None,
        remaining_grams: Optional[float] = None,
    ) -> None:
        """Insert or replace a loaded material record."""
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO printer_materials
                    (printer_name, tool_index, material_type, color,
                     spool_id, loaded_at, remaining_grams)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (printer_name, tool_index, material_type, color,
                 spool_id, time.time(), remaining_grams),
            )
            self._conn.commit()

    def get_material(
        self, printer_name: str, tool_index: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Fetch material loaded in a specific tool slot."""
        row = self._conn.execute(
            "SELECT * FROM printer_materials WHERE printer_name = ? AND tool_index = ?",
            (printer_name, tool_index),
        ).fetchone()
        return dict(row) if row else None

    def list_materials(self, printer_name: str) -> List[Dict[str, Any]]:
        """Return all material slots for a printer."""
        rows = self._conn.execute(
            "SELECT * FROM printer_materials WHERE printer_name = ? ORDER BY tool_index",
            (printer_name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_material_remaining(
        self, printer_name: str, tool_index: int, remaining_grams: float,
    ) -> None:
        """Update remaining grams for a loaded material."""
        with self._write_lock:
            self._conn.execute(
                "UPDATE printer_materials SET remaining_grams = ? "
                "WHERE printer_name = ? AND tool_index = ?",
                (remaining_grams, printer_name, tool_index),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Spools
    # ------------------------------------------------------------------

    def save_spool(self, spool: Dict[str, Any]) -> None:
        """Insert or replace a spool record."""
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO spools
                    (id, material_type, color, brand, weight_grams,
                     remaining_grams, cost_usd, purchase_date, notes)
                VALUES (:id, :material_type, :color, :brand, :weight_grams,
                        :remaining_grams, :cost_usd, :purchase_date, :notes)
                """,
                {
                    "id": spool["id"],
                    "material_type": spool["material_type"],
                    "color": spool.get("color"),
                    "brand": spool.get("brand"),
                    "weight_grams": spool.get("weight_grams", 1000.0),
                    "remaining_grams": spool.get("remaining_grams", 1000.0),
                    "cost_usd": spool.get("cost_usd"),
                    "purchase_date": spool.get("purchase_date"),
                    "notes": spool.get("notes", ""),
                },
            )
            self._conn.commit()

    def get_spool(self, spool_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a spool by ID."""
        row = self._conn.execute(
            "SELECT * FROM spools WHERE id = ?", (spool_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_spools(self) -> List[Dict[str, Any]]:
        """Return all spools."""
        rows = self._conn.execute(
            "SELECT * FROM spools ORDER BY material_type, color"
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_spool(self, spool_id: str) -> bool:
        """Delete a spool.  Returns ``True`` if a row was deleted."""
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM spools WHERE id = ?", (spool_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_spool_remaining(self, spool_id: str, remaining_grams: float) -> None:
        """Update remaining grams for a spool."""
        with self._write_lock:
            self._conn.execute(
                "UPDATE spools SET remaining_grams = ? WHERE id = ?",
                (remaining_grams, spool_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Leveling history
    # ------------------------------------------------------------------

    def save_leveling(self, record: Dict[str, Any]) -> int:
        """Insert a leveling record and return the row id."""
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO leveling_history
                    (printer_name, triggered_by, started_at, completed_at,
                     success, mesh_data, trigger_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["printer_name"],
                    record.get("triggered_by", "manual"),
                    record["started_at"],
                    record.get("completed_at"),
                    1 if record.get("success") else 0,
                    json.dumps(record["mesh_data"]) if record.get("mesh_data") else None,
                    record.get("trigger_reason"),
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def last_leveling(self, printer_name: str) -> Optional[Dict[str, Any]]:
        """Return the most recent leveling record for a printer."""
        row = self._conn.execute(
            "SELECT * FROM leveling_history WHERE printer_name = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (printer_name,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("mesh_data"):
            d["mesh_data"] = json.loads(d["mesh_data"])
        return d

    def leveling_count_since(self, printer_name: str, since: float) -> int:
        """Count leveling events for a printer since a timestamp."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM leveling_history "
            "WHERE printer_name = ? AND started_at >= ?",
            (printer_name, since),
        ).fetchone()
        return row["cnt"] if row else 0

    # ------------------------------------------------------------------
    # Sync log
    # ------------------------------------------------------------------

    def log_sync(
        self,
        entity_type: str,
        entity_id: str,
        direction: str = "push",
        status: str = "success",
    ) -> None:
        """Record a sync operation."""
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO sync_log (entity_type, entity_id, synced_at,
                                      sync_direction, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entity_type, entity_id, time.time(), direction, status),
            )
            self._conn.commit()

    def get_unsynced_jobs(self, since: float) -> List[Dict[str, Any]]:
        """Return jobs submitted after *since* that have not been synced."""
        rows = self._conn.execute(
            """
            SELECT j.* FROM jobs j
            WHERE j.submitted_at > ?
              AND j.id NOT IN (
                  SELECT entity_id FROM sync_log
                  WHERE entity_type = 'job' AND status = 'success'
              )
            ORDER BY j.submitted_at ASC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unsynced_events(self, since: float) -> List[Dict[str, Any]]:
        """Return events logged after *since* that have not been synced."""
        rows = self._conn.execute(
            """
            SELECT e.* FROM events e
            WHERE e.timestamp > ?
              AND CAST(e.id AS TEXT) NOT IN (
                  SELECT entity_id FROM sync_log
                  WHERE entity_type = 'event' AND status = 'success'
              )
            ORDER BY e.id ASC
            """,
            (since,),
        ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["data"] = json.loads(d["data"])
            results.append(d)
        return results

    def mark_synced(self, entity_type: str, entity_ids: List[str]) -> None:
        """Mark entities as synced."""
        with self._write_lock:
            now = time.time()
            for eid in entity_ids:
                self._conn.execute(
                    """
                    INSERT INTO sync_log (entity_type, entity_id, synced_at,
                                          sync_direction, status)
                    VALUES (?, ?, ?, 'push', 'success')
                    """,
                    (entity_type, eid, now),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Billing charges
    # ------------------------------------------------------------------

    def save_billing_charge(self, charge: Dict[str, Any]) -> None:
        """Insert a billing charge record, ignoring duplicates on job_id."""
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO billing_charges
                    (id, job_id, order_id, fee_amount, fee_percent,
                     job_cost, total_cost, currency, waived, waiver_reason,
                     payment_id, payment_rail, payment_status, created_at)
                VALUES (:id, :job_id, :order_id, :fee_amount, :fee_percent,
                        :job_cost, :total_cost, :currency, :waived,
                        :waiver_reason, :payment_id, :payment_rail,
                        :payment_status, :created_at)
                """,
                {
                    "id": charge["id"],
                    "job_id": charge["job_id"],
                    "order_id": charge.get("order_id"),
                    "fee_amount": charge["fee_amount"],
                    "fee_percent": charge["fee_percent"],
                    "job_cost": charge["job_cost"],
                    "total_cost": charge["total_cost"],
                    "currency": charge.get("currency", "USD"),
                    "waived": 1 if charge.get("waived") else 0,
                    "waiver_reason": charge.get("waiver_reason"),
                    "payment_id": charge.get("payment_id"),
                    "payment_rail": charge.get("payment_rail"),
                    "payment_status": charge.get("payment_status", "pending"),
                    "created_at": charge.get("created_at", time.time()),
                },
            )
            self._conn.commit()

    def get_billing_charge(self, charge_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a billing charge by ID."""
        row = self._conn.execute(
            "SELECT * FROM billing_charges WHERE id = ?", (charge_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["waived"] = bool(d["waived"])
        return d

    def list_billing_charges(
        self,
        limit: int = 50,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return billing charges, newest first.

        Optionally filter by calendar month.
        """
        if month is not None and year is not None:
            from datetime import datetime, timezone
            start = datetime(year, month, 1, tzinfo=timezone.utc).timestamp()
            if month == 12:
                end = datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
            else:
                end = datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp()
            rows = self._conn.execute(
                "SELECT * FROM billing_charges "
                "WHERE created_at >= ? AND created_at < ? "
                "ORDER BY created_at DESC LIMIT ?",
                (start, end, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM billing_charges "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["waived"] = bool(d["waived"])
            results.append(d)
        return results

    def monthly_billing_summary(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Aggregate billing data for a calendar month.

        Returns dict with ``total_fees``, ``job_count``, ``waived_count``.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        target_year = year if year is not None else now.year
        target_month = month if month is not None else now.month
        start = datetime(target_year, target_month, 1, tzinfo=timezone.utc).timestamp()
        if target_month == 12:
            end = datetime(target_year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
        else:
            end = datetime(target_year, target_month + 1, 1, tzinfo=timezone.utc).timestamp()

        row = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(fee_amount), 0.0) AS total_fees,
                COUNT(*) AS job_count,
                SUM(CASE WHEN waived = 1 THEN 1 ELSE 0 END) AS waived_count
            FROM billing_charges
            WHERE created_at >= ? AND created_at < ?
            """,
            (start, end),
        ).fetchone()
        return {
            "total_fees": round(row["total_fees"], 2),
            "job_count": row["job_count"],
            "waived_count": row["waived_count"],
        }

    def billing_charges_this_month(self) -> int:
        """Count billing charges in the current calendar month."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM billing_charges WHERE created_at >= ?",
            (start,),
        ).fetchone()
        return row["cnt"] if row else 0

    def monthly_fee_total(self) -> float:
        """Sum of fee_amount for billing charges in the current month."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(fee_amount), 0.0) AS total "
            "FROM billing_charges WHERE created_at >= ?",
            (start,),
        ).fetchone()
        return round(row["total"], 2) if row else 0.0

    # ------------------------------------------------------------------
    # Payment methods
    # ------------------------------------------------------------------

    def save_payment_method(self, method: Dict[str, Any]) -> None:
        """Insert or replace a saved payment method."""
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO payment_methods
                    (id, user_id, rail, provider_ref, method_ref,
                     label, is_default, created_at)
                VALUES (:id, :user_id, :rail, :provider_ref, :method_ref,
                        :label, :is_default, :created_at)
                """,
                {
                    "id": method["id"],
                    "user_id": method["user_id"],
                    "rail": method["rail"],
                    "provider_ref": method["provider_ref"],
                    "method_ref": method.get("method_ref"),
                    "label": method.get("label"),
                    "is_default": 1 if method.get("is_default") else 0,
                    "created_at": method.get("created_at", time.time()),
                },
            )
            self._conn.commit()

    def get_default_payment_method(
        self, user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the default payment method for a user, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM payment_methods "
            "WHERE user_id = ? AND is_default = 1 LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["is_default"] = bool(d["is_default"])
        return d

    def list_payment_methods(
        self, user_id: str,
    ) -> List[Dict[str, Any]]:
        """Return all payment methods for a user."""
        rows = self._conn.execute(
            "SELECT * FROM payment_methods WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["is_default"] = bool(d["is_default"])
            results.append(d)
        return results

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------

    def save_payment(self, payment: Dict[str, Any]) -> None:
        """Insert a payment transaction record."""
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO payments
                    (id, charge_id, provider_id, rail, amount, currency,
                     status, tx_hash, error, created_at, updated_at)
                VALUES (:id, :charge_id, :provider_id, :rail, :amount,
                        :currency, :status, :tx_hash, :error,
                        :created_at, :updated_at)
                """,
                {
                    "id": payment["id"],
                    "charge_id": payment["charge_id"],
                    "provider_id": payment["provider_id"],
                    "rail": payment["rail"],
                    "amount": payment["amount"],
                    "currency": payment.get("currency", "USD"),
                    "status": payment.get("status", "pending"),
                    "tx_hash": payment.get("tx_hash"),
                    "error": payment.get("error"),
                    "created_at": payment.get("created_at", now),
                    "updated_at": payment.get("updated_at", now),
                },
            )
            self._conn.commit()

    def update_payment_status(
        self,
        payment_id: str,
        status: str,
        tx_hash: Optional[str] = None,
    ) -> None:
        """Update the status (and optionally tx_hash) of a payment."""
        with self._write_lock:
            if tx_hash is not None:
                self._conn.execute(
                    "UPDATE payments SET status = ?, tx_hash = ?, "
                    "updated_at = ? WHERE id = ?",
                    (status, tx_hash, time.time(), payment_id),
                )
            else:
                self._conn.execute(
                    "UPDATE payments SET status = ?, updated_at = ? WHERE id = ?",
                    (status, time.time(), payment_id),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Print history
    # ------------------------------------------------------------------

    def save_print_record(self, record: Dict[str, Any]) -> int:
        """Insert a print history record and return the row id."""
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO print_history
                    (job_id, printer_name, file_name, status,
                     duration_seconds, material_type, file_hash,
                     slicer_profile, notes, agent_id, metadata,
                     started_at, completed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["job_id"],
                    record["printer_name"],
                    record.get("file_name"),
                    record["status"],
                    record.get("duration_seconds"),
                    record.get("material_type"),
                    record.get("file_hash"),
                    record.get("slicer_profile"),
                    record.get("notes"),
                    record.get("agent_id"),
                    json.dumps(record["metadata"]) if record.get("metadata") else None,
                    record.get("started_at"),
                    record.get("completed_at"),
                    record.get("created_at", time.time()),
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_print_record(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a print history record by job_id, or ``None`` if not found."""
        row = self._conn.execute(
            "SELECT * FROM print_history WHERE job_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("metadata"):
            d["metadata"] = json.loads(d["metadata"])
        return d

    def list_print_history(
        self,
        printer_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return print history records, newest first.

        Args:
            printer_name: Filter by printer name, or ``None`` for all.
            status: Filter by status string, or ``None`` for all.
            limit: Maximum rows to return.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if printer_name is not None:
            clauses.append("printer_name = ?")
            params.append(printer_name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM print_history {where} "
            "ORDER BY completed_at DESC LIMIT ?",
            params,
        ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            if d.get("metadata"):
                d["metadata"] = json.loads(d["metadata"])
            results.append(d)
        return results

    def get_printer_stats(self, printer_name: str) -> Dict[str, Any]:
        """Aggregate statistics for a printer.

        Returns dict with ``total_prints``, ``success_rate``,
        ``avg_duration_seconds``, and ``total_print_hours``.
        """
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS total_prints,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS successes,
                AVG(CASE WHEN duration_seconds IS NOT NULL THEN duration_seconds END) AS avg_duration,
                COALESCE(SUM(duration_seconds), 0.0) AS total_seconds
            FROM print_history
            WHERE printer_name = ?
            """,
            (printer_name,),
        ).fetchone()
        total = row["total_prints"] if row else 0
        successes = row["successes"] if row else 0
        avg_dur = row["avg_duration"] if row else None
        total_secs = row["total_seconds"] if row else 0.0
        return {
            "printer_name": printer_name,
            "total_prints": total,
            "success_rate": round(successes / total, 4) if total > 0 else 0.0,
            "avg_duration_seconds": round(avg_dur, 1) if avg_dur is not None else None,
            "total_print_hours": round(total_secs / 3600.0, 2),
        }

    def update_print_notes(self, job_id: str, notes: str) -> bool:
        """Update the notes field on a print history record.

        Returns ``True`` if a row was updated.
        """
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE print_history SET notes = ? WHERE job_id = ?",
                (notes, job_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Agent memory
    # ------------------------------------------------------------------

    def save_memory(
        self,
        agent_id: str,
        scope: str,
        key: str,
        value: Any,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Insert or replace an agent memory entry.

        The *value* is JSON-encoded before storage.  When *ttl_seconds* is
        provided the entry will expire after that many seconds.  Overwriting
        an existing key auto-increments the version number.

        Args:
            agent_id: Agent identifier.
            scope: Namespace scope (e.g. ``"global"``, ``"printer:ender3"``).
            key: Memory key name.
            value: Value to store (will be JSON-encoded).
            ttl_seconds: Optional time-to-live in seconds.  ``None`` means
                the entry never expires.
        """
        now = time.time()
        expires_at = (now + ttl_seconds) if ttl_seconds is not None else None
        with self._write_lock:
            # Read current version for auto-increment
            row = self._conn.execute(
                "SELECT version, created_at FROM agent_memory "
                "WHERE agent_id = ? AND scope = ? AND key = ?",
                (agent_id, scope, key),
            ).fetchone()
            if row is not None:
                new_version = row["version"] + 1
                original_created = row["created_at"]
            else:
                new_version = 1
                original_created = now

            self._conn.execute(
                """
                INSERT OR REPLACE INTO agent_memory
                    (agent_id, scope, key, value, created_at, updated_at,
                     version, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, scope, key, json.dumps(value),
                 original_created, now, new_version, expires_at),
            )
            self._conn.commit()

    def get_memory(
        self,
        agent_id: str,
        scope: str,
        key: str,
    ) -> Optional[Any]:
        """Fetch a single agent memory value, or ``None`` if not found or expired."""
        row = self._conn.execute(
            "SELECT value FROM agent_memory "
            "WHERE agent_id = ? AND scope = ? AND key = ? "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (agent_id, scope, key, time.time()),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["value"])

    def list_memory(
        self,
        agent_id: str,
        scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return all non-expired memory entries for an agent.

        Args:
            agent_id: The agent whose memory to retrieve.
            scope: Optional scope filter.
        """
        now = time.time()
        if scope is not None:
            rows = self._conn.execute(
                "SELECT * FROM agent_memory "
                "WHERE agent_id = ? AND scope = ? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY updated_at DESC",
                (agent_id, scope, now),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM agent_memory "
                "WHERE agent_id = ? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY updated_at DESC",
                (agent_id, now),
            ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["value"] = json.loads(d["value"])
            results.append(d)
        return results

    def delete_memory(
        self,
        agent_id: str,
        scope: str,
        key: str,
    ) -> bool:
        """Delete a single agent memory entry.

        Returns ``True`` if a row was deleted.
        """
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM agent_memory "
                "WHERE agent_id = ? AND scope = ? AND key = ?",
                (agent_id, scope, key),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def clean_expired_notes(self) -> int:
        """Delete all expired agent memory entries.

        Returns the number of rows deleted.
        """
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM agent_memory "
                "WHERE expires_at IS NOT NULL AND expires_at < ?",
                (time.time(),),
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Print outcomes (cross-printer learning)
    # ------------------------------------------------------------------

    def save_print_outcome(self, outcome: Dict[str, Any]) -> int:
        """Save an agent-curated print outcome record.  Returns row id."""
        VALID_OUTCOMES = {"success", "failed", "partial"}
        outcome_val = outcome.get("outcome", "")
        if outcome_val not in VALID_OUTCOMES:
            raise ValueError(f"Invalid outcome {outcome_val!r}. Must be one of: {sorted(VALID_OUTCOMES)}")
        if outcome.get("quality_grade") and outcome["quality_grade"] not in {"excellent", "good", "acceptable", "poor"}:
            raise ValueError(f"Invalid quality_grade {outcome['quality_grade']!r}")
        if outcome.get("failure_mode") and outcome["failure_mode"] not in {
            "spaghetti", "layer_shift", "warping", "adhesion", "stringing",
            "under_extrusion", "over_extrusion", "clog", "thermal_runaway",
            "power_loss", "mechanical", "other",
        }:
            raise ValueError(f"Invalid failure_mode {outcome['failure_mode']!r}")

        with self._write_lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO print_outcomes
                       (job_id, printer_name, file_name, file_hash, material_type,
                        outcome, quality_grade, failure_mode, settings, environment,
                        notes, agent_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        outcome["job_id"],
                        outcome["printer_name"],
                        outcome.get("file_name"),
                        outcome.get("file_hash"),
                        outcome.get("material_type"),
                        outcome["outcome"],
                        outcome.get("quality_grade"),
                        outcome.get("failure_mode"),
                        json.dumps(outcome["settings"]) if outcome.get("settings") else None,
                        json.dumps(outcome["environment"]) if outcome.get("environment") else None,
                        outcome.get("notes"),
                        outcome.get("agent_id"),
                        outcome.get("created_at", time.time()),
                    ),
                )
                self._conn.commit()
                return cur.lastrowid  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                raise ValueError(f"Outcome for job_id {outcome['job_id']!r} already recorded")

    def get_print_outcome(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return the outcome record for *job_id*, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM print_outcomes WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return self._outcome_row_to_dict(row)

    def list_print_outcomes(
        self,
        printer_name: Optional[str] = None,
        file_hash: Optional[str] = None,
        outcome: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return outcome records, optionally filtered."""
        clauses: List[str] = []
        params: List[Any] = []
        if printer_name:
            clauses.append("printer_name = ?")
            params.append(printer_name)
        if file_hash:
            clauses.append("file_hash = ?")
            params.append(file_hash)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM print_outcomes{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._outcome_row_to_dict(r) for r in rows]

    def get_printer_learning_insights(self, printer_name: str) -> Dict[str, Any]:
        """Return aggregated outcome insights for a single printer."""
        total = self._conn.execute(
            "SELECT COUNT(*) FROM print_outcomes WHERE printer_name = ?",
            (printer_name,),
        ).fetchone()[0]
        if total == 0:
            return {
                "printer_name": printer_name,
                "total_outcomes": 0,
                "success_rate": 0.0,
                "failure_breakdown": {},
                "material_stats": {},
            }
        successes = self._conn.execute(
            "SELECT COUNT(*) FROM print_outcomes WHERE printer_name = ? AND outcome = 'success'",
            (printer_name,),
        ).fetchone()[0]
        # Failure breakdown
        failure_rows = self._conn.execute(
            "SELECT failure_mode, COUNT(*) FROM print_outcomes "
            "WHERE printer_name = ? AND outcome = 'failed' AND failure_mode IS NOT NULL "
            "GROUP BY failure_mode ORDER BY COUNT(*) DESC",
            (printer_name,),
        ).fetchall()
        failure_breakdown = {row[0]: row[1] for row in failure_rows}
        # Material stats
        material_rows = self._conn.execute(
            "SELECT material_type, "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) as wins "
            "FROM print_outcomes "
            "WHERE printer_name = ? AND material_type IS NOT NULL "
            "GROUP BY material_type ORDER BY total DESC",
            (printer_name,),
        ).fetchall()
        material_stats = {}
        for row in material_rows:
            material_stats[row[0]] = {
                "count": row[1],
                "success_rate": round(row[2] / row[1], 2) if row[1] else 0.0,
            }
        return {
            "printer_name": printer_name,
            "total_outcomes": total,
            "success_rate": round(successes / total, 2) if total else 0.0,
            "failure_breakdown": failure_breakdown,
            "material_stats": material_stats,
        }

    def get_file_outcomes(self, file_hash: str) -> Dict[str, Any]:
        """Return outcome data for a specific file across all printers."""
        rows = self._conn.execute(
            "SELECT printer_name, "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) as wins "
            "FROM print_outcomes "
            "WHERE file_hash = ? "
            "GROUP BY printer_name ORDER BY (CAST(wins AS REAL) / total) DESC, total DESC",
            (file_hash,),
        ).fetchall()
        outcomes_by_printer = {}
        for row in rows:
            outcomes_by_printer[row[0]] = {
                "total": row[1],
                "successes": row[2],
                "success_rate": round(row[2] / row[1], 2) if row[1] else 0.0,
            }
        printers_tried = list(outcomes_by_printer.keys())
        best = printers_tried[0] if printers_tried else None
        return {
            "file_hash": file_hash,
            "printers_tried": printers_tried,
            "best_printer": best,
            "outcomes_by_printer": outcomes_by_printer,
        }

    def suggest_printer_for_outcome(
        self,
        file_hash: Optional[str] = None,
        material_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return printers ranked by success rate for the given criteria."""
        clauses: List[str] = []
        params: List[Any] = []
        if file_hash:
            clauses.append("file_hash = ?")
            params.append(file_hash)
        if material_type:
            clauses.append("material_type = ?")
            params.append(material_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT printer_name, "
            f"  COUNT(*) as total, "
            f"  SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) as wins "
            f"FROM print_outcomes{where} "
            f"GROUP BY printer_name "
            f"ORDER BY (CAST(wins AS REAL) / total) DESC, total DESC",
            params,
        ).fetchall()
        results = []
        for row in rows:
            results.append({
                "printer_name": row[0],
                "total_prints": row[1],
                "successes": row[2],
                "success_rate": round(row[2] / row[1], 2) if row[1] else 0.0,
            })
        return results

    def get_successful_settings(
        self,
        printer_name: str | None = None,
        material_type: str | None = None,
        file_hash: str | None = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return settings from successful print outcomes.

        Filters by printer, material, and/or file hash. Returns only
        outcomes where outcome='success' and settings is not NULL,
        ordered by quality_grade (excellent > good > acceptable > poor)
        then by created_at descending.
        """
        clauses: List[str] = ["outcome = 'success'", "settings IS NOT NULL"]
        params: List[Any] = []
        if printer_name:
            clauses.append("printer_name = ?")
            params.append(printer_name)
        if material_type:
            clauses.append("material_type = ?")
            params.append(material_type)
        if file_hash:
            clauses.append("file_hash = ?")
            params.append(file_hash)
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM print_outcomes{where} "
            "ORDER BY "
            "CASE quality_grade "
            "  WHEN 'excellent' THEN 0 "
            "  WHEN 'good' THEN 1 "
            "  WHEN 'acceptable' THEN 2 "
            "  WHEN 'poor' THEN 3 "
            "  ELSE 4 "
            "END, created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._outcome_row_to_dict(r) for r in rows]

    def _outcome_row_to_dict(self, row) -> Dict[str, Any]:
        """Convert a print_outcomes row to a dictionary."""
        d = dict(row)
        if d.get("settings"):
            d["settings"] = json.loads(d["settings"])
        if d.get("environment"):
            d["environment"] = json.loads(d["environment"])
        return d


    # ------------------------------------------------------------------
    # Model Cache
    # ------------------------------------------------------------------

    def save_cache_entry(self, entry) -> None:
        """Insert a model cache entry.

        Args:
            entry: A :class:`ModelCacheEntry` dataclass instance.
        """
        import json as _json
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO model_cache
                    (cache_id, file_name, file_path, file_hash,
                     file_size_bytes, source, source_id, prompt,
                     tags, dimensions, print_count, last_printed_at,
                     created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.cache_id,
                    entry.file_name,
                    entry.file_path,
                    entry.file_hash,
                    entry.file_size_bytes,
                    entry.source,
                    entry.source_id,
                    entry.prompt,
                    _json.dumps(entry.tags) if entry.tags else "[]",
                    _json.dumps(entry.dimensions) if entry.dimensions else None,
                    entry.print_count,
                    entry.last_printed_at,
                    entry.created_at,
                    _json.dumps(entry.metadata) if entry.metadata else "{}",
                ),
            )
            self._conn.commit()

    def _cache_row_to_entry(self, row):
        """Convert a model_cache row to a ModelCacheEntry."""
        from kiln.model_cache import ModelCacheEntry
        d = dict(row)
        tags = json.loads(d["tags"]) if d.get("tags") else []
        dimensions = json.loads(d["dimensions"]) if d.get("dimensions") else None
        metadata = json.loads(d["metadata"]) if d.get("metadata") else {}
        return ModelCacheEntry(
            cache_id=d["cache_id"],
            file_name=d["file_name"],
            file_path=d["file_path"],
            file_hash=d["file_hash"],
            file_size_bytes=d["file_size_bytes"],
            source=d["source"],
            source_id=d.get("source_id"),
            prompt=d.get("prompt"),
            tags=tags,
            dimensions=dimensions,
            print_count=d.get("print_count", 0),
            last_printed_at=d.get("last_printed_at"),
            created_at=d["created_at"],
            metadata=metadata,
        )

    def get_cache_entry(self, cache_id: str):
        """Return a ModelCacheEntry by cache_id, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM model_cache WHERE cache_id = ?", (cache_id,)
        ).fetchone()
        if row is None:
            return None
        return self._cache_row_to_entry(row)

    def get_cache_entry_by_hash(self, file_hash: str):
        """Return a ModelCacheEntry by file_hash, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM model_cache WHERE file_hash = ? LIMIT 1",
            (file_hash,),
        ).fetchone()
        if row is None:
            return None
        return self._cache_row_to_entry(row)

    def search_cache(
        self,
        *,
        query: Optional[str] = None,
        source: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 20,
    ):
        """Search cached models by name, source, tags, or prompt text."""
        clauses: List[str] = []
        params: List[Any] = []

        if query:
            like_q = f"%{query}%"
            clauses.append(
                "(file_name LIKE ? OR prompt LIKE ? OR tags LIKE ?)"
            )
            params.extend([like_q, like_q, like_q])

        if source:
            clauses.append("source = ?")
            params.append(source)

        if tags:
            for tag in tags:
                clauses.append("tags LIKE ?")
                params.append(f"%{tag}%")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM model_cache{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._cache_row_to_entry(r) for r in rows]

    def list_cache_entries(self, *, limit: int = 50, offset: int = 0):
        """List all cached models, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM model_cache ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._cache_row_to_entry(r) for r in rows]

    def record_cache_print(self, cache_id: str) -> None:
        """Increment print_count and update last_printed_at."""
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                "UPDATE model_cache SET print_count = print_count + 1, "
                "last_printed_at = ? WHERE cache_id = ?",
                (now, cache_id),
            )
            self._conn.commit()

    def delete_cache_entry(self, cache_id: str) -> bool:
        """Delete a model cache entry by ID. Returns True if a row was deleted."""
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM model_cache WHERE cache_id = ?", (cache_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Cleanup & Maintenance
    # ------------------------------------------------------------------

    def cleanup(self, max_age_days: int = 90) -> Dict[str, int]:
        """Delete old completed/failed jobs and events, then VACUUM.

        Parameters:
            max_age_days: Records older than this many days are purged.

        Returns:
            A dict with ``"jobs_deleted"``, ``"events_deleted"`` counts.
        """
        cutoff = time.time() - (max_age_days * 86400)
        jobs_deleted = 0
        events_deleted = 0

        with self._write_lock:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE status IN ('completed', 'failed', 'cancelled') "
                "AND submitted_at < ?",
                (cutoff,),
            )
            jobs_deleted = cursor.rowcount

            cursor = self._conn.execute(
                "DELETE FROM events WHERE timestamp < ?",
                (cutoff,),
            )
            events_deleted = cursor.rowcount

            self._conn.commit()
            self._conn.execute("VACUUM")

        return {"jobs_deleted": jobs_deleted, "events_deleted": events_deleted}

    def db_size_bytes(self) -> int:
        """Return the current size of the database file in bytes."""
        try:
            return os.path.getsize(self._db_path)
        except OSError:
            return 0


    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    # Safety audit log
    # ------------------------------------------------------------------


    def _get_hmac_key(self) -> bytes:
        """Return the HMAC key for audit log signing.

        Uses ``KILN_AUDIT_HMAC_KEY`` env var if set, otherwise derives
        a per-installation key from the database path.
        """
        env_key = os.environ.get("KILN_AUDIT_HMAC_KEY", "")
        if env_key:
            return env_key.encode("utf-8")
        return hashlib.sha256(self._db_path.encode("utf-8")).digest()

    def _compute_audit_hmac(self, row_data: Dict[str, Any]) -> str:
        """Compute an HMAC-SHA256 signature for an audit log row.

        :param row_data: Dict with keys: timestamp, tool_name, safety_level,
            action, agent_id, printer_name, details.
        :returns: Hex-encoded HMAC digest.
        """
        key = self._get_hmac_key()
        # Build a canonical message from the row fields.
        parts = [
            str(row_data.get("timestamp", "")),
            str(row_data.get("tool_name", "")),
            str(row_data.get("safety_level", "")),
            str(row_data.get("action", "")),
            str(row_data.get("agent_id", "") or ""),
            str(row_data.get("printer_name", "") or ""),
            str(row_data.get("details", "") or ""),
        ]
        message = "|".join(parts).encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def log_audit(
        self,
        tool_name: str,
        safety_level: str,
        action: str,
        agent_id: Optional[str] = None,
        printer_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Record a safety audit event and return the row id.

        Args:
            tool_name: MCP tool name (e.g. ``"start_print"``).
            safety_level: Safety classification (``"safe"``, ``"guarded"``,
                ``"confirm"``, ``"emergency"``).
            action: What happened — ``"executed"``, ``"blocked"``,
                ``"rate_limited"``, ``"auth_denied"``, ``"preflight_failed"``,
                or ``"dry_run"``.
            agent_id: Optional identifier for the calling agent.
            printer_name: Optional printer name involved.
            details: Optional dict of extra context (args, error messages).
        """
        ts = time.time()
        with self._write_lock:
            details_json = json.dumps(details) if details else None
            hmac_sig = self._compute_audit_hmac({
                "timestamp": ts,
                "tool_name": tool_name,
                "safety_level": safety_level,
                "action": action,
                "agent_id": agent_id,
                "printer_name": printer_name,
                "details": details_json,
            })
            cur = self._conn.execute(
                """
                INSERT INTO safety_audit_log
                    (timestamp, tool_name, safety_level, action,
                     agent_id, printer_name, details, hmac_signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    tool_name,
                    safety_level,
                    action,
                    agent_id,
                    printer_name,
                    details_json,
                    hmac_sig,
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]


    def verify_audit_log(self) -> Dict[str, Any]:
        """Verify HMAC signatures on all audit log entries.

        :returns: Dict with ``total``, ``valid``, ``invalid`` counts
            and an ``integrity`` field (``"ok"`` or ``"compromised"``).
        """
        rows = self._conn.execute(
            "SELECT * FROM safety_audit_log ORDER BY id"
        ).fetchall()
        total = len(rows)
        valid = 0
        invalid = 0
        for row in rows:
            d = dict(row)
            stored_sig = d.get("hmac_signature")
            if stored_sig is None:
                # Legacy row without HMAC — count as invalid.
                invalid += 1
                continue
            expected = self._compute_audit_hmac({
                "timestamp": d["timestamp"],
                "tool_name": d["tool_name"],
                "safety_level": d["safety_level"],
                "action": d["action"],
                "agent_id": d.get("agent_id"),
                "printer_name": d.get("printer_name"),
                "details": d.get("details"),
            })
            if hmac.compare_digest(stored_sig, expected):
                valid += 1
            else:
                invalid += 1
        return {
            "total": total,
            "valid": valid,
            "invalid": invalid,
            "integrity": "ok" if invalid == 0 else "compromised",
        }

    def query_audit(
        self,
        action: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query the safety audit log, newest first.

        Args:
            action: Filter by action type (e.g. ``"blocked"``).
            tool_name: Filter by tool name.
            limit: Maximum rows to return.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if tool_name is not None:
            clauses.append("tool_name = ?")
            params.append(tool_name)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM safety_audit_log {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            if d.get("details"):
                d["details"] = json.loads(d["details"])
            results.append(d)
        return results

    def audit_summary(self, window_seconds: float = 3600.0) -> Dict[str, Any]:
        """Return a summary of audit activity within a time window.

        Args:
            window_seconds: Look-back window in seconds (default 1 hour).
        """
        cutoff = time.time() - window_seconds
        rows = self._conn.execute(
            "SELECT action, COUNT(*) as cnt FROM safety_audit_log "
            "WHERE timestamp > ? GROUP BY action",
            (cutoff,),
        ).fetchall()
        counts = {row["action"]: row["cnt"] for row in rows}

        recent_blocked = self._conn.execute(
            "SELECT tool_name, details, timestamp FROM safety_audit_log "
            "WHERE action IN ('blocked', 'rate_limited', 'auth_denied') "
            "AND timestamp > ? ORDER BY id DESC LIMIT 10",
            (cutoff,),
        ).fetchall()
        blocked_list = []
        for row in recent_blocked:
            entry = {
                "tool": row["tool_name"],
                "timestamp": row["timestamp"],
            }
            if row["details"]:
                entry["details"] = json.loads(row["details"])
            blocked_list.append(entry)

        return {
            "window_seconds": window_seconds,
            "counts": counts,
            "recent_blocked": blocked_list,
            "total": sum(counts.values()),
        }

    # ------------------------------------------------------------------
    # Snapshot persistence
    # ------------------------------------------------------------------

    def save_snapshot(
        self,
        printer_name: str,
        image_path: str,
        *,
        job_id: Optional[str] = None,
        phase: str = "unknown",
        image_size_bytes: Optional[int] = None,
        analysis: Optional[str] = None,
        agent_notes: Optional[str] = None,
        confidence: Optional[float] = None,
        completion_pct: Optional[float] = None,
    ) -> int:
        """Persist a snapshot record and return its row ID.

        :param printer_name: Printer that captured the snapshot.
        :param image_path: Filesystem path to the saved image file.
        :param job_id: Associated print job ID (if known).
        :param phase: Print phase at capture time (e.g. "first_layer", "mid_print", "final_layer").
        :param image_size_bytes: Size of the image file in bytes.
        :param analysis: JSON-encoded analysis result from vision model.
        :param agent_notes: Free-form notes from the monitoring agent.
        :param confidence: Vision model confidence score (0.0–1.0).
        :param completion_pct: Print completion percentage at capture time.
        :returns: The auto-incremented snapshot row ID.
        """
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO snapshots
                    (job_id, printer_name, phase, image_path, image_size_bytes,
                     analysis, agent_notes, confidence, completion_pct, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, printer_name, phase, image_path, image_size_bytes,
                    analysis, agent_notes, confidence, completion_pct, time.time(),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_snapshots(
        self,
        *,
        job_id: Optional[str] = None,
        printer_name: Optional[str] = None,
        phase: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query snapshot records with optional filters.

        :param job_id: Filter by job ID.
        :param printer_name: Filter by printer name.
        :param phase: Filter by capture phase.
        :param limit: Maximum number of records to return.
        :returns: List of snapshot dicts, newest first.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if printer_name is not None:
            clauses.append("printer_name = ?")
            params.append(printer_name)
        if phase is not None:
            clauses.append("phase = ?")
            params.append(phase)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM snapshots{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_snapshots(
        self,
        *,
        job_id: Optional[str] = None,
        older_than: Optional[float] = None,
    ) -> int:
        """Delete snapshot records matching filters. Returns count deleted.

        :param job_id: Delete snapshots for this job.
        :param older_than: Delete snapshots created before this Unix timestamp.
        :returns: Number of rows deleted.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if older_than is not None:
            clauses.append("created_at < ?")
            params.append(older_than)
        if not clauses:
            return 0
        where = " WHERE " + " AND ".join(clauses)
        with self._write_lock:
            cur = self._conn.execute(f"DELETE FROM snapshots{where}", params)
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Fulfillment order helpers
    # ------------------------------------------------------------------

    def list_active_fulfillment_orders(self) -> List[Dict[str, Any]]:
        """Return fulfillment orders that are not yet delivered/failed/cancelled."""
        rows = self._conn.execute(
            "SELECT * FROM fulfillment_orders "
            "WHERE status NOT IN ('delivered', 'completed', 'failed', 'cancelled', 'canceled') "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_fulfillment_order_status(self, order_id: str, status: str) -> None:
        """Update the status of a fulfillment order."""
        with self._write_lock:
            self._conn.execute(
                "UPDATE fulfillment_orders SET status = ?, updated_at = ? WHERE order_id = ?",
                (status, time.time(), order_id),
            )
            self._conn.commit()

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
