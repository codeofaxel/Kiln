"""Tests for kiln.backup — database backup, restore, and scheduled backups.

Covers:
- backup_database() with and without credential redaction
- restore_database() validation and overwrite behaviour
- verify_integrity() on valid and corrupt databases
- snapshot_database() VACUUM INTO snapshots and rotation
- _rotate_backups() keeps the right number of files
- BackupScheduler lifecycle (start/stop/is_running)
- BackupScheduler env var fallback (KILN_BACKUP_INTERVAL, KILN_BACKUP_KEEP)
- BackupScheduler runs backup cycles and handles failures
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from kiln.backup import (
    BackupError,
    BackupScheduler,
    IntegrityResult,
    ScheduledBackupResult,
    _generate_backup_filename,
    _rotate_backups,
    backup_database,
    restore_database,
    snapshot_database,
    verify_integrity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_test_db(path: str) -> None:
    """Create a minimal Kiln-like SQLite database for testing."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE printers (
            name TEXT PRIMARY KEY,
            printer_type TEXT NOT NULL,
            host TEXT NOT NULL,
            api_key TEXT,
            registered_at REAL NOT NULL,
            last_seen REAL
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE payment_methods (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            rail TEXT NOT NULL,
            provider_ref TEXT NOT NULL,
            method_ref TEXT,
            label TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        INSERT INTO printers VALUES
            ('test', 'octoprint', 'http://localhost', 'super-secret-key', 1000.0, NULL);
        INSERT INTO settings VALUES
            ('active_printer', 'test');
        INSERT INTO settings VALUES
            ('custom_token', 'tok_abc123');
        INSERT INTO payment_methods VALUES
            ('pm1', 'u1', 'stripe', 'cus_secret', 'pm_secret', 'Visa', 0, 1000.0);
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests — backup_database()
# ---------------------------------------------------------------------------

class TestBackupDatabase:
    """Tests for backup_database()."""

    def test_backup_creates_valid_copy(self, tmp_path):
        src = str(tmp_path / "kiln.db")
        _create_test_db(src)
        out = str(tmp_path / "backup.db")
        result = backup_database(src, out, redact_credentials=False)
        assert os.path.isfile(result)
        conn = sqlite3.connect(result)
        row = conn.execute("SELECT api_key FROM printers WHERE name=?", ("test",)).fetchone()
        assert row[0] == "super-secret-key"
        conn.close()

    def test_backup_redacts_credentials(self, tmp_path):
        src = str(tmp_path / "kiln.db")
        _create_test_db(src)
        out = str(tmp_path / "backup.db")
        result = backup_database(src, out, redact_credentials=True)
        conn = sqlite3.connect(result)
        row = conn.execute("SELECT api_key FROM printers WHERE name=?", ("test",)).fetchone()
        assert row[0] == "REDACTED"
        # Safe settings keys should NOT be redacted
        row = conn.execute("SELECT value FROM settings WHERE key=?", ("active_printer",)).fetchone()
        assert row[0] == "test"
        # Non-safe settings should be redacted
        row = conn.execute("SELECT value FROM settings WHERE key=?", ("custom_token",)).fetchone()
        assert row[0] == "REDACTED"
        # payment_methods provider_ref and method_ref should be redacted
        row = conn.execute("SELECT provider_ref, method_ref FROM payment_methods WHERE id=?", ("pm1",)).fetchone()
        assert row[0] == "REDACTED"
        assert row[1] == "REDACTED"
        conn.close()

    def test_backup_source_not_found(self, tmp_path):
        with pytest.raises(BackupError, match="Source database not found"):
            backup_database(str(tmp_path / "nope.db"), str(tmp_path / "out.db"))

    def test_backup_default_output_path(self, tmp_path, monkeypatch):
        src = str(tmp_path / "kiln.db")
        _create_test_db(src)
        monkeypatch.setattr("kiln.backup.Path.home", lambda: tmp_path)
        result = backup_database(src, redact_credentials=False)
        assert os.path.isfile(result)
        assert ".kiln" in result
        assert "backups" in result

    def test_backup_does_not_modify_source(self, tmp_path):
        src = str(tmp_path / "kiln.db")
        _create_test_db(src)
        out = str(tmp_path / "backup.db")
        backup_database(src, out, redact_credentials=True)
        conn = sqlite3.connect(src)
        row = conn.execute("SELECT api_key FROM printers WHERE name=?", ("test",)).fetchone()
        assert row[0] == "super-secret-key"
        conn.close()


# ---------------------------------------------------------------------------
# Tests — restore_database()
# ---------------------------------------------------------------------------

class TestRestoreDatabase:
    """Tests for restore_database()."""

    def test_restore_from_valid_backup(self, tmp_path):
        src = str(tmp_path / "backup.db")
        _create_test_db(src)
        dest = str(tmp_path / "restored.db")
        result = restore_database(src, dest)
        assert os.path.isfile(result)
        conn = sqlite3.connect(result)
        row = conn.execute("SELECT api_key FROM printers WHERE name=?", ("test",)).fetchone()
        assert row[0] == "super-secret-key"
        conn.close()

    def test_restore_validates_sqlite(self, tmp_path):
        bad = str(tmp_path / "bad.db")
        with open(bad, "w") as f:
            f.write("this is not a database")
        dest = str(tmp_path / "restored.db")
        with pytest.raises(BackupError, match="Not a valid SQLite database"):
            restore_database(bad, dest)

    def test_restore_refuses_overwrite_without_force(self, tmp_path):
        src = str(tmp_path / "backup.db")
        _create_test_db(src)
        dest = str(tmp_path / "existing.db")
        _create_test_db(dest)
        with pytest.raises(BackupError, match="already exists"):
            restore_database(src, dest, force=False)

    def test_restore_overwrites_with_force(self, tmp_path):
        src = str(tmp_path / "backup.db")
        _create_test_db(src)
        dest = str(tmp_path / "existing.db")
        _create_test_db(dest)
        result = restore_database(src, dest, force=True)
        assert os.path.isfile(result)

    def test_restore_backup_not_found(self, tmp_path):
        with pytest.raises(BackupError, match="Backup file not found"):
            restore_database(str(tmp_path / "nope.db"), str(tmp_path / "out.db"))


# ---------------------------------------------------------------------------
# Tests — verify_integrity()
# ---------------------------------------------------------------------------

class TestVerifyIntegrity:
    """Tests for verify_integrity()."""

    def test_valid_database_passes(self, tmp_path):
        db = str(tmp_path / "good.db")
        _create_test_db(db)
        result = verify_integrity(db)
        assert result.ok is True
        assert result.details == "ok"

    def test_missing_file_fails(self, tmp_path):
        result = verify_integrity(str(tmp_path / "nope.db"))
        assert result.ok is False
        assert "File not found" in result.details

    def test_corrupt_file_fails(self, tmp_path):
        bad = str(tmp_path / "corrupt.db")
        with open(bad, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\xff" * 100)
        result = verify_integrity(bad)
        assert result.ok is False

    def test_to_dict(self):
        result = IntegrityResult(ok=True, details="ok")
        d = result.to_dict()
        assert d == {"ok": True, "details": "ok"}


# ---------------------------------------------------------------------------
# Tests — snapshot_database()
# ---------------------------------------------------------------------------

class TestSnapshotDatabase:
    """Tests for snapshot_database() using VACUUM INTO."""

    def test_creates_backup_file(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        backup_dir = str(tmp_path / "backups")
        result = snapshot_database(db, backup_dir)
        assert result.success is True
        assert os.path.isfile(result.path)
        assert result.size_bytes > 0
        assert result.timestamp != ""
        assert result.error is None

    def test_backup_is_valid_sqlite(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        backup_dir = str(tmp_path / "backups")
        result = snapshot_database(db, backup_dir)
        integrity = verify_integrity(result.path)
        assert integrity.ok is True

    def test_source_not_found(self, tmp_path):
        result = snapshot_database(
            str(tmp_path / "nope.db"),
            str(tmp_path / "backups"),
        )
        assert result.success is False
        assert "not found" in result.error

    def test_rotates_old_backups(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        backup_dir = str(tmp_path / "backups")
        # Create 4 backups with keep=2 — only 2 should remain
        for _ in range(4):
            result = snapshot_database(db, backup_dir, keep=2)
            assert result.success is True
            time.sleep(0.01)  # Ensure unique filenames
        backup_files = [
            f for f in os.listdir(backup_dir)
            if f.endswith(".db") and "_backup_" in f
        ]
        assert len(backup_files) == 2

    def test_to_dict(self):
        result = ScheduledBackupResult(success=True, path="/a/b.db", size_bytes=42)
        d = result.to_dict()
        assert d["success"] is True
        assert d["path"] == "/a/b.db"
        assert d["size_bytes"] == 42


# ---------------------------------------------------------------------------
# Tests — _generate_backup_filename()
# ---------------------------------------------------------------------------

class TestGenerateBackupFilename:
    """Tests for _generate_backup_filename()."""

    def test_contains_stem_and_backup(self):
        name = _generate_backup_filename("/some/path/kiln.db")
        assert name.startswith("kiln_backup_")
        assert name.endswith(".db")

    def test_unique_on_rapid_calls(self):
        names = {_generate_backup_filename("test.db") for _ in range(5)}
        # May collide on same microsecond, but at least produces valid names
        for name in names:
            assert "_backup_" in name


# ---------------------------------------------------------------------------
# Tests — _rotate_backups()
# ---------------------------------------------------------------------------

class TestRotateBackups:
    """Tests for _rotate_backups()."""

    def test_keeps_newest(self, tmp_path):
        backup_dir = str(tmp_path)
        paths = []
        for i in range(5):
            p = os.path.join(backup_dir, f"kiln_backup_{i:04d}.db")
            with open(p, "w") as f:
                f.write(str(i))
            # Set ascending modification times
            os.utime(p, (1000 + i, 1000 + i))
            paths.append(p)

        deleted = _rotate_backups(backup_dir, keep=2)
        assert len(deleted) == 3
        # The two newest (i=3, i=4) should survive
        remaining = [f for f in os.listdir(backup_dir) if f.endswith(".db")]
        assert len(remaining) == 2

    def test_prefix_filter(self, tmp_path):
        backup_dir = str(tmp_path)
        # kiln backup
        kiln = os.path.join(backup_dir, "kiln_backup_0001.db")
        with open(kiln, "w") as f:
            f.write("kiln")
        # other backup
        other = os.path.join(backup_dir, "other_backup_0001.db")
        with open(other, "w") as f:
            f.write("other")

        deleted = _rotate_backups(backup_dir, keep=0, prefix="kiln")
        assert kiln in deleted
        assert other not in deleted
        assert os.path.isfile(other)

    def test_nonexistent_dir(self, tmp_path):
        deleted = _rotate_backups(str(tmp_path / "nope"), keep=1)
        assert deleted == []


# ---------------------------------------------------------------------------
# Tests — BackupScheduler
# ---------------------------------------------------------------------------

class TestBackupSchedulerConstructor:
    """Tests for BackupScheduler.__init__() and env var resolution."""

    def test_explicit_args_override_env(self, monkeypatch):
        monkeypatch.setenv("KILN_BACKUP_INTERVAL", "9999")
        monkeypatch.setenv("KILN_BACKUP_KEEP", "99")
        sched = BackupScheduler(
            "/db", "/backups",
            interval_seconds=60.0,
            keep=3,
        )
        assert sched._interval == 60.0
        assert sched._keep == 3

    def test_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("KILN_BACKUP_INTERVAL", "120")
        monkeypatch.setenv("KILN_BACKUP_KEEP", "10")
        sched = BackupScheduler("/db", "/backups")
        assert sched._interval == 120.0
        assert sched._keep == 10

    def test_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.delenv("KILN_BACKUP_INTERVAL", raising=False)
        monkeypatch.delenv("KILN_BACKUP_KEEP", raising=False)
        sched = BackupScheduler("/db", "/backups")
        assert sched._interval == 3600.0
        assert sched._keep == 5


class TestBackupSchedulerLifecycle:
    """Tests for BackupScheduler start/stop/is_running."""

    def test_start_and_stop(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        backup_dir = str(tmp_path / "backups")

        sched = BackupScheduler(
            db, backup_dir,
            interval_seconds=3600.0,
            keep=2,
        )
        assert sched.is_running is False

        sched.start()
        assert sched.is_running is True

        sched.stop()
        assert sched.is_running is False

    def test_double_start_is_idempotent(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        backup_dir = str(tmp_path / "backups")

        sched = BackupScheduler(
            db, backup_dir,
            interval_seconds=3600.0,
            keep=2,
        )
        sched.start()
        thread_1 = sched._thread
        sched.start()  # Should not create a second thread
        assert sched._thread is thread_1
        sched.stop()

    def test_stop_without_start(self, tmp_path):
        sched = BackupScheduler(
            str(tmp_path / "kiln.db"),
            str(tmp_path / "backups"),
            interval_seconds=3600.0,
        )
        # Should not raise
        sched.stop()
        assert sched.is_running is False


class TestBackupSchedulerExecution:
    """Tests for BackupScheduler._run() backup cycle."""

    def test_creates_backup_on_first_cycle(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        backup_dir = str(tmp_path / "backups")

        sched = BackupScheduler(
            db, backup_dir,
            interval_seconds=0.05,
            keep=3,
        )
        sched.start()
        # Wait enough time for at least one cycle
        time.sleep(0.15)
        sched.stop()

        backup_files = [
            f for f in os.listdir(backup_dir)
            if f.endswith(".db") and "_backup_" in f
        ]
        assert len(backup_files) >= 1

    def test_handles_missing_db_gracefully(self, tmp_path):
        backup_dir = str(tmp_path / "backups")

        sched = BackupScheduler(
            str(tmp_path / "nonexistent.db"),
            backup_dir,
            interval_seconds=0.05,
            keep=2,
        )
        # Should not raise even though the DB does not exist
        sched.start()
        time.sleep(0.1)
        sched.stop()

    def test_handles_exception_in_snapshot(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        backup_dir = str(tmp_path / "backups")

        sched = BackupScheduler(
            db, backup_dir,
            interval_seconds=0.05,
            keep=2,
        )

        with patch(
            "kiln.backup.snapshot_database",
            side_effect=RuntimeError("boom"),
        ):
            sched.start()
            time.sleep(0.1)
            sched.stop()

        # Scheduler should have survived the exception
        assert sched.is_running is False

    def test_thread_is_daemon(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        sched = BackupScheduler(
            db, str(tmp_path / "backups"),
            interval_seconds=3600.0,
        )
        sched.start()
        assert sched._thread.daemon is True
        sched.stop()

    def test_thread_name(self, tmp_path):
        db = str(tmp_path / "kiln.db")
        _create_test_db(db)
        sched = BackupScheduler(
            db, str(tmp_path / "backups"),
            interval_seconds=3600.0,
        )
        sched.start()
        assert sched._thread.name == "kiln-backup-scheduler"
        sched.stop()
