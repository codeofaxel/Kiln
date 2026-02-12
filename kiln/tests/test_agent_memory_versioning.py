"""Tests for agent memory versioning and TTL (time-to-live).

Covers:
- save_memory auto-increments version on overwrite
- save_memory with ttl_seconds sets expires_at
- save_memory without ttl_seconds leaves expires_at NULL
- list_memory filters out expired entries
- list_memory includes version in results
- get_memory filters out expired entries
- clean_expired_notes deletes expired rows
- clean_expired_notes returns correct count
- clean_expired_notes ignores non-expired entries
- _migrate_agent_memory adds columns to existing tables
- MCP tool save_agent_note accepts ttl_seconds
- MCP tool get_agent_context returns version info
- MCP tool clean_agent_memory calls clean_expired_notes
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from kiln.persistence import KilnDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    db_path = str(tmp_path / "test_agent_memory.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# Version auto-increment
# ---------------------------------------------------------------------------


class TestAgentMemoryVersioning:

    def test_first_save_version_is_one(self, db):
        db.save_memory("agent1", "global", "key1", "value1")
        entries = db.list_memory("agent1")
        assert len(entries) == 1
        assert entries[0]["version"] == 1

    def test_overwrite_increments_version(self, db):
        db.save_memory("agent1", "global", "key1", "v1")
        db.save_memory("agent1", "global", "key1", "v2")
        entries = db.list_memory("agent1")
        assert len(entries) == 1
        assert entries[0]["version"] == 2
        assert entries[0]["value"] == "v2"

    def test_multiple_overwrites_increment_sequentially(self, db):
        for i in range(5):
            db.save_memory("agent1", "global", "key1", f"v{i}")
        entries = db.list_memory("agent1")
        assert entries[0]["version"] == 5

    def test_different_keys_have_independent_versions(self, db):
        db.save_memory("agent1", "global", "key1", "v1")
        db.save_memory("agent1", "global", "key1", "v2")
        db.save_memory("agent1", "global", "key2", "v1")
        entries = db.list_memory("agent1")
        versions = {e["key"]: e["version"] for e in entries}
        assert versions["key1"] == 2
        assert versions["key2"] == 1

    def test_overwrite_preserves_created_at(self, db):
        db.save_memory("agent1", "global", "key1", "v1")
        entries_v1 = db.list_memory("agent1")
        created_at_v1 = entries_v1[0]["created_at"]

        # Small delay to ensure different timestamps
        time.sleep(0.01)
        db.save_memory("agent1", "global", "key1", "v2")
        entries_v2 = db.list_memory("agent1")
        assert entries_v2[0]["created_at"] == created_at_v1
        assert entries_v2[0]["updated_at"] > created_at_v1


# ---------------------------------------------------------------------------
# TTL / Expiration
# ---------------------------------------------------------------------------


class TestAgentMemoryTTL:

    def test_save_with_ttl_sets_expires_at(self, db):
        db.save_memory("agent1", "global", "key1", "value1", ttl_seconds=3600)
        row = db._conn.execute(
            "SELECT expires_at FROM agent_memory WHERE key = ?", ("key1",),
        ).fetchone()
        assert row["expires_at"] is not None
        # Should be approximately now + 3600
        assert abs(row["expires_at"] - (time.time() + 3600)) < 5

    def test_save_without_ttl_has_null_expires(self, db):
        db.save_memory("agent1", "global", "key1", "value1")
        row = db._conn.execute(
            "SELECT expires_at FROM agent_memory WHERE key = ?", ("key1",),
        ).fetchone()
        assert row["expires_at"] is None

    def test_list_memory_excludes_expired(self, db):
        # Save one expired and one active entry
        db.save_memory("agent1", "global", "expired_key", "old_value", ttl_seconds=1)
        db.save_memory("agent1", "global", "active_key", "active_value")

        # Manually expire the first entry
        db._conn.execute(
            "UPDATE agent_memory SET expires_at = ? WHERE key = ?",
            (time.time() - 10, "expired_key"),
        )
        db._conn.commit()

        entries = db.list_memory("agent1")
        keys = [e["key"] for e in entries]
        assert "active_key" in keys
        assert "expired_key" not in keys

    def test_list_memory_includes_non_expired_ttl(self, db):
        db.save_memory("agent1", "global", "future_key", "value", ttl_seconds=3600)
        entries = db.list_memory("agent1")
        assert len(entries) == 1
        assert entries[0]["key"] == "future_key"

    def test_get_memory_excludes_expired(self, db):
        db.save_memory("agent1", "global", "key1", "value1", ttl_seconds=1)
        # Manually expire
        db._conn.execute(
            "UPDATE agent_memory SET expires_at = ? WHERE key = ?",
            (time.time() - 10, "key1"),
        )
        db._conn.commit()

        result = db.get_memory("agent1", "global", "key1")
        assert result is None

    def test_get_memory_includes_non_expired(self, db):
        db.save_memory("agent1", "global", "key1", "value1", ttl_seconds=3600)
        result = db.get_memory("agent1", "global", "key1")
        assert result == "value1"

    def test_list_memory_with_scope_excludes_expired(self, db):
        db.save_memory("agent1", "fleet", "key1", "v1", ttl_seconds=1)
        db.save_memory("agent1", "fleet", "key2", "v2")
        # Expire key1
        db._conn.execute(
            "UPDATE agent_memory SET expires_at = ? WHERE key = ?",
            (time.time() - 10, "key1"),
        )
        db._conn.commit()

        entries = db.list_memory("agent1", scope="fleet")
        keys = [e["key"] for e in entries]
        assert "key2" in keys
        assert "key1" not in keys


# ---------------------------------------------------------------------------
# clean_expired_notes
# ---------------------------------------------------------------------------


class TestCleanExpiredNotes:

    def test_deletes_expired_entries(self, db):
        db.save_memory("agent1", "global", "key1", "v1", ttl_seconds=1)
        db.save_memory("agent1", "global", "key2", "v2", ttl_seconds=1)
        # Expire both
        db._conn.execute(
            "UPDATE agent_memory SET expires_at = ?",
            (time.time() - 10,),
        )
        db._conn.commit()

        deleted = db.clean_expired_notes()
        assert deleted == 2

    def test_preserves_non_expired_entries(self, db):
        db.save_memory("agent1", "global", "permanent", "value1")
        db.save_memory("agent1", "global", "future", "value2", ttl_seconds=3600)
        db.save_memory("agent1", "global", "expired", "value3", ttl_seconds=1)
        # Expire only the third
        db._conn.execute(
            "UPDATE agent_memory SET expires_at = ? WHERE key = ?",
            (time.time() - 10, "expired"),
        )
        db._conn.commit()

        deleted = db.clean_expired_notes()
        assert deleted == 1

        entries = db.list_memory("agent1")
        keys = {e["key"] for e in entries}
        assert "permanent" in keys
        assert "future" in keys
        assert "expired" not in keys

    def test_returns_zero_when_nothing_to_clean(self, db):
        db.save_memory("agent1", "global", "key1", "v1")
        deleted = db.clean_expired_notes()
        assert deleted == 0

    def test_returns_zero_on_empty_table(self, db):
        deleted = db.clean_expired_notes()
        assert deleted == 0


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


class TestAgentMemoryMigration:

    def test_version_column_exists(self, db):
        columns = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(agent_memory)").fetchall()
        }
        assert "version" in columns
        assert "expires_at" in columns

    def test_migrate_idempotent(self, db):
        # Calling migrate again should not raise
        db._migrate_agent_memory()
        columns = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(agent_memory)").fetchall()
        }
        assert "version" in columns
        assert "expires_at" in columns


# ---------------------------------------------------------------------------
# MCP tool integration
# ---------------------------------------------------------------------------


class TestAgentMemoryMCPTools:

    @patch("kiln.server.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_save_agent_note_with_ttl(self, mock_auth, mock_get_db):
        from kiln.server import save_agent_note

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        result = save_agent_note(key="test_key", value="test_val", ttl_seconds=600)
        assert result["success"] is True
        assert result["ttl_seconds"] == 600

        mock_db.save_memory.assert_called_once()
        call_kwargs = mock_db.save_memory.call_args
        assert call_kwargs.kwargs["ttl_seconds"] == 600

    @patch("kiln.server.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_save_agent_note_without_ttl(self, mock_auth, mock_get_db):
        from kiln.server import save_agent_note

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        result = save_agent_note(key="test_key", value="test_val")
        assert result["success"] is True
        assert result["ttl_seconds"] is None

        call_kwargs = mock_db.save_memory.call_args
        assert call_kwargs.kwargs["ttl_seconds"] is None

    @patch("kiln.server.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_get_agent_context_returns_entries(self, mock_auth, mock_get_db):
        from kiln.server import get_agent_context

        mock_db = MagicMock()
        mock_db.list_memory.return_value = [
            {"key": "k1", "value": "v1", "version": 3, "expires_at": None},
        ]
        mock_get_db.return_value = mock_db

        result = get_agent_context()
        assert result["success"] is True
        assert result["count"] == 1
        assert result["entries"][0]["version"] == 3

    @patch("kiln.server.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_clean_agent_memory_returns_count(self, mock_auth, mock_get_db):
        from kiln.server import clean_agent_memory

        mock_db = MagicMock()
        mock_db.clean_expired_notes.return_value = 5
        mock_get_db.return_value = mock_db

        result = clean_agent_memory()
        assert result["success"] is True
        assert result["deleted_count"] == 5
        mock_db.clean_expired_notes.assert_called_once()
