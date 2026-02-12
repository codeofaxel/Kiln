"""Database backup and restore for Kiln.

Provides functions to export the Kiln SQLite database with optional
credential redaction, and to restore from a backup file.  Designed to
be called from both the CLI and MCP tools.

Only stdlib modules are used.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional


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
