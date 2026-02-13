"""Database backup and restore for Kiln.

Provides functions to export the Kiln SQLite database with optional
credential redaction, and to restore from a backup file.  Includes a
:class:`BackupScheduler` for periodic automated backups via a daemon
thread.  Designed to be called from both the CLI and MCP tools.

Only stdlib modules are used.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


_CREDENTIAL_COLUMNS: list[tuple[str, str]] = [
    ("printers", "api_key"),
    ("payment_methods", "provider_ref"),
    ("payment_methods", "method_ref"),
    ("settings", "value"),
]

_SAFE_SETTINGS_KEYS: frozenset[str] = frozenset({
    "active_printer",
    "theme",
    "log_level",
    "units",
})


class BackupError(Exception):
    """Raised when a backup or restore operation fails."""


def backup_database(
    db_path: str,
    output_path: Optional[str] = None,
    *,
    redact_credentials: bool = True,
) -> str:
    """Create a backup copy of the Kiln database.

    :param db_path: Path to the source SQLite database.
    :param output_path: Destination path.  Defaults to
        ~/.kiln/backups/kiln-YYYYMMDD-HHMMSS.db.
    :param redact_credentials: If True, replace credential columns
        with REDACTED in the copy.
    :returns: The absolute path of the backup file.
    :raises BackupError: If the source DB does not exist or the copy fails.
    """
    if not os.path.isfile(db_path):
        raise BackupError(f"Source database not found: {db_path}")

    if output_path is None:
        backup_dir = os.path.join(str(Path.home()), ".kiln", "backups")
        ts = time.strftime("%Y%m%d-%H%M%S")
        output_path = os.path.join(backup_dir, f"kiln-{ts}.db")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        shutil.copy2(db_path, output_path)
    except OSError as exc:
        raise BackupError(f"Failed to copy database: {exc}") from exc

    if redact_credentials:
        _redact_credentials(output_path)

    return os.path.abspath(output_path)


def _redact_credentials(db_path: str) -> None:
    """Open a copied database and replace credential values with REDACTED."""
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        for table, column in _CREDENTIAL_COLUMNS:
            try:
                cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
            except sqlite3.OperationalError:
                continue

            if table == "settings" and column == "value":
                placeholders = ",".join("?" for _ in _SAFE_SETTINGS_KEYS)
                cur.execute(
                    f"UPDATE {table} SET {column} = 'REDACTED' "
                    f"WHERE key NOT IN ({placeholders})",
                    tuple(_SAFE_SETTINGS_KEYS),
                )
            else:
                cur.execute(
                    f"UPDATE {table} SET {column} = 'REDACTED' "
                    f"WHERE {column} IS NOT NULL AND {column} != ''",
                )

        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Failed to redact credentials: {exc}") from exc


def restore_database(
    backup_path: str,
    db_path: str,
    *,
    force: bool = False,
) -> str:
    """Restore a Kiln database from a backup file.

    :param backup_path: Path to the backup SQLite file.
    :param db_path: Destination path for the restored database.
    :param force: If False and db_path already exists, raise an error.
    :returns: The absolute path of the restored database.
    :raises BackupError: If the backup is not a valid SQLite file or the
        destination already exists (when force is False).
    """
    if not os.path.isfile(backup_path):
        raise BackupError(f"Backup file not found: {backup_path}")

    _validate_sqlite(backup_path)

    if os.path.isfile(db_path) and not force:
        raise BackupError(
            f"Database already exists at {db_path}. "
            "Use --force to overwrite."
        )

    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    try:
        shutil.copy2(backup_path, db_path)
    except OSError as exc:
        raise BackupError(f"Failed to restore database: {exc}") from exc

    return os.path.abspath(db_path)


def _validate_sqlite(path: str) -> None:
    """Check that path is a valid SQLite database file.

    :raises BackupError: If the file is not a valid SQLite database.
    """
    try:
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1")
        result = conn.execute("PRAGMA integrity_check(1)").fetchone()
        conn.close()
        if result is None or result[0] != "ok":
            raise BackupError(f"SQLite integrity check failed for {path}")
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Not a valid SQLite database: {path} ({exc})") from exc


# ---------------------------------------------------------------------------
# Data models for scheduled backups
# ---------------------------------------------------------------------------

@dataclass
class ScheduledBackupResult:
    """Outcome of a single scheduled backup cycle.

    :param success: Whether the backup completed successfully.
    :param path: Filesystem path to the backup file.
    :param size_bytes: Size of the backup in bytes.
    :param timestamp: ISO-formatted timestamp of the backup.
    :param error: Error message if the backup failed.
    """

    success: bool
    path: str = ""
    size_bytes: int = 0
    timestamp: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IntegrityResult:
    """Outcome of a database integrity check.

    :param ok: Whether the database passed the integrity check.
    :param details: Raw output from ``PRAGMA integrity_check``.
    """

    ok: bool
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Integrity verification
# ---------------------------------------------------------------------------

def verify_integrity(db_path: str) -> IntegrityResult:
    """Verify SQLite database integrity using ``PRAGMA integrity_check``.

    :param db_path: Path to the SQLite database file.
    :returns: :class:`IntegrityResult` indicating pass or fail.
    """
    if not os.path.isfile(db_path):
        return IntegrityResult(ok=False, details=f"File not found: {db_path}")

    try:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("PRAGMA integrity_check")
            rows = cursor.fetchall()
            result_text = "; ".join(row[0] for row in rows)
            is_ok = len(rows) == 1 and rows[0][0] == "ok"
            return IntegrityResult(ok=is_ok, details=result_text)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return IntegrityResult(ok=False, details=f"SQLite error: {exc}")


# ---------------------------------------------------------------------------
# VACUUM INTO snapshot helper
# ---------------------------------------------------------------------------

def _generate_backup_filename(db_path: str) -> str:
    """Generate a timestamped backup filename from the source database name."""
    base = Path(db_path).stem
    now = time.time()
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    micros = int((now % 1) * 1_000_000)
    return f"{base}_backup_{timestamp}_{micros:06d}.db"


def _rotate_backups(backup_dir: str, *, keep: int = 5, prefix: str = "") -> list[str]:
    """Delete old backups, keeping only the most recent *keep* files.

    :param backup_dir: Directory containing backup files.
    :param keep: Number of most recent backups to keep.
    :param prefix: Only consider files starting with this prefix.
    :returns: List of deleted file paths.
    """
    if not os.path.isdir(backup_dir):
        return []

    backups: list[str] = []
    for entry in os.listdir(backup_dir):
        if entry.endswith(".db") and "_backup_" in entry:
            if prefix and not entry.startswith(prefix):
                continue
            backups.append(os.path.join(backup_dir, entry))

    # Sort by modification time, newest first
    backups.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    deleted: list[str] = []
    for old_backup in backups[keep:]:
        try:
            os.remove(old_backup)
            deleted.append(old_backup)
            logger.debug("Rotated old backup: %s", old_backup)
        except OSError as exc:
            logger.warning("Failed to delete old backup %s: %s", old_backup, exc)

    return deleted


def snapshot_database(
    db_path: str,
    backup_dir: str,
    *,
    keep: int = 5,
) -> ScheduledBackupResult:
    """Create a consistent snapshot of a SQLite database using ``VACUUM INTO``.

    Unlike :func:`backup_database`, this does not redact credentials and uses
    ``VACUUM INTO`` for a safe, lock-free snapshot.  Intended for use by the
    :class:`BackupScheduler`.

    :param db_path: Path to the source SQLite database.
    :param backup_dir: Directory to store the backup.
    :param keep: Number of most recent backups to retain.
    :returns: :class:`ScheduledBackupResult` with the outcome.
    """
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    if not os.path.isfile(db_path):
        return ScheduledBackupResult(
            success=False,
            timestamp=timestamp,
            error=f"Source database not found: {db_path}",
        )

    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as exc:
        return ScheduledBackupResult(
            success=False,
            timestamp=timestamp,
            error=f"Cannot create backup directory: {exc}",
        )

    backup_filename = _generate_backup_filename(db_path)
    backup_path = os.path.join(backup_dir, backup_filename)

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f"VACUUM INTO '{backup_path}'")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return ScheduledBackupResult(
            success=False,
            path=backup_path,
            timestamp=timestamp,
            error=f"Backup failed: {exc}",
        )

    if not os.path.isfile(backup_path):
        return ScheduledBackupResult(
            success=False,
            path=backup_path,
            timestamp=timestamp,
            error="Backup file was not created",
        )

    size_bytes = os.path.getsize(backup_path)

    prefix = Path(db_path).stem
    _rotate_backups(backup_dir, keep=keep, prefix=prefix)

    logger.info(
        "Scheduled backup created: %s (%d bytes)", backup_path, size_bytes
    )

    return ScheduledBackupResult(
        success=True,
        path=backup_path,
        size_bytes=size_bytes,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Auto-backup scheduling
# ---------------------------------------------------------------------------

_DEFAULT_INTERVAL: float = 3600.0
_DEFAULT_KEEP: int = 5


class BackupScheduler:
    """Runs periodic database backups in a background daemon thread.

    Uses ``VACUUM INTO`` for consistent snapshots and rotates old backups
    by modification time.

    Configuration is read from environment variables at construction time:

    - ``KILN_BACKUP_INTERVAL`` — seconds between backups (default 3600).
    - ``KILN_BACKUP_KEEP`` — number of backups to retain (default 5).

    :param db_path: Path to the database to back up.
    :param backup_dir: Directory to store backups.
    :param interval_seconds: Seconds between backups.  Falls back to
        ``KILN_BACKUP_INTERVAL`` env var, then the default (3600).
    :param keep: Number of backups to retain.  Falls back to
        ``KILN_BACKUP_KEEP`` env var, then the default (5).
    """

    def __init__(
        self,
        db_path: str,
        backup_dir: str,
        *,
        interval_seconds: Optional[float] = None,
        keep: Optional[int] = None,
    ) -> None:
        self._db_path = db_path
        self._backup_dir = backup_dir

        # Resolve interval: explicit arg > env var > default
        if interval_seconds is not None:
            self._interval = float(interval_seconds)
        else:
            env_interval = os.environ.get("KILN_BACKUP_INTERVAL")
            self._interval = float(env_interval) if env_interval else _DEFAULT_INTERVAL

        # Resolve keep: explicit arg > env var > default
        if keep is not None:
            self._keep = int(keep)
        else:
            env_keep = os.environ.get("KILN_BACKUP_KEEP")
            self._keep = int(env_keep) if env_keep else _DEFAULT_KEEP

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- Public lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Start the background backup scheduler."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Backup scheduler already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="kiln-backup-scheduler",
        )
        self._thread.start()
        logger.info(
            "Backup scheduler started (interval=%ds, keep=%d)",
            self._interval, self._keep,
        )

    def stop(self) -> None:
        """Stop the background backup scheduler."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Backup scheduler stopped")

    @property
    def is_running(self) -> bool:
        """Whether the scheduler thread is currently active."""
        return self._thread is not None and self._thread.is_alive()

    # -- Internals -----------------------------------------------------------

    def _run(self) -> None:
        """Main scheduler loop — snapshot, then sleep until next cycle."""
        while not self._stop_event.is_set():
            try:
                result = snapshot_database(
                    self._db_path,
                    self._backup_dir,
                    keep=self._keep,
                )
                if result.success:
                    logger.debug(
                        "Scheduled backup completed: %s", result.path
                    )
                else:
                    logger.warning(
                        "Scheduled backup failed: %s", result.error
                    )
            except Exception:
                logger.exception("Unexpected error in backup scheduler")

            self._stop_event.wait(timeout=self._interval)
