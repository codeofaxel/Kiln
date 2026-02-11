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
                    job_id          TEXT NOT NULL,
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
        """Insert or replace a billing charge record."""
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO billing_charges
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
