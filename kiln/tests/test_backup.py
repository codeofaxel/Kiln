"""Tests for kiln.backup — database backup and restore."""

from __future__ import annotations

import os
import sqlite3

import pytest

from kiln.backup import BackupError, backup_database, restore_database


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
