"""Tests for print history and agent memory persistence features.

Covers:
    - Print history: save, get, list (with filters and limits), stats, notes
    - Agent memory: save, get, overwrite, list (with scope), delete, JSON roundtrip
    - Edge cases: empty databases, non-existent records, zero stats

Uses temporary SQLite databases for full isolation.
"""

from __future__ import annotations

import json
import tempfile
import time

import pytest

from kiln.persistence import KilnDB


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture()
def db(tmp_path):
    """Return a KilnDB backed by a temporary file."""
    db_path = str(tmp_path / "test_history.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


def _make_print_record(**overrides) -> dict:
    """Return a minimal valid print history record with optional overrides."""
    now = time.time()
    defaults = {
        "job_id": "job-001",
        "printer_name": "voron",
        "file_name": "benchy.gcode",
        "status": "completed",
        "duration_seconds": 3600.0,
        "material_type": "PLA",
        "file_hash": "abc123",
        "slicer_profile": "0.2mm Standard",
        "notes": None,
        "agent_id": "claude-agent",
        "metadata": None,
        "started_at": now - 3600,
        "completed_at": now,
        "created_at": now,
    }
    defaults.update(overrides)
    return defaults


# ===================================================================
# Print History -- save and get roundtrip
# ===================================================================

class TestPrintHistory:
    """Tests for save_print_record and get_print_record."""

    def test_save_and_get_roundtrip(self, db) -> None:
        record = _make_print_record()
        row_id = db.save_print_record(record)
        assert isinstance(row_id, int)
        assert row_id >= 1

        result = db.get_print_record("job-001")
        assert result is not None
        assert result["job_id"] == "job-001"
        assert result["printer_name"] == "voron"
        assert result["file_name"] == "benchy.gcode"
        assert result["status"] == "completed"
        assert result["duration_seconds"] == 3600.0
        assert result["material_type"] == "PLA"
        assert result["file_hash"] == "abc123"
        assert result["slicer_profile"] == "0.2mm Standard"
        assert result["agent_id"] == "claude-agent"

    def test_get_nonexistent_returns_none(self, db) -> None:
        result = db.get_print_record("nonexistent-job")
        assert result is None

    def test_save_minimal_record(self, db) -> None:
        record = {
            "job_id": "minimal-001",
            "printer_name": "ender3",
            "status": "failed",
            "created_at": time.time(),
        }
        row_id = db.save_print_record(record)
        assert row_id >= 1

        result = db.get_print_record("minimal-001")
        assert result is not None
        assert result["printer_name"] == "ender3"
        assert result["status"] == "failed"
        assert result["file_name"] is None
        assert result["duration_seconds"] is None

    def test_save_with_metadata(self, db) -> None:
        metadata = {"layer_count": 150, "estimated_time": 7200}
        record = _make_print_record(
            job_id="meta-001",
            metadata=metadata,
        )
        db.save_print_record(record)

        result = db.get_print_record("meta-001")
        assert result is not None
        assert result["metadata"] == metadata
        assert result["metadata"]["layer_count"] == 150

    def test_save_returns_incrementing_ids(self, db) -> None:
        id1 = db.save_print_record(_make_print_record(job_id="j1"))
        id2 = db.save_print_record(_make_print_record(job_id="j2"))
        assert id2 > id1

    def test_multiple_records_same_job_id(self, db) -> None:
        """Multiple records with the same job_id are allowed (retries)."""
        db.save_print_record(_make_print_record(job_id="retry-job", status="failed"))
        db.save_print_record(_make_print_record(job_id="retry-job", status="completed"))

        # get_print_record returns the most recent (highest ID)
        result = db.get_print_record("retry-job")
        assert result is not None
        assert result["status"] == "completed"


# ===================================================================
# Print History -- list with filters and limits
# ===================================================================

class TestPrintHistoryList:
    """Tests for list_print_history with filtering and pagination."""

    def test_list_returns_records(self, db) -> None:
        now = time.time()
        for i in range(5):
            db.save_print_record(_make_print_record(
                job_id=f"list-{i}",
                completed_at=now + i,
            ))
        results = db.list_print_history()
        assert len(results) == 5

    def test_list_respects_limit(self, db) -> None:
        now = time.time()
        for i in range(10):
            db.save_print_record(_make_print_record(
                job_id=f"limit-{i}",
                completed_at=now + i,
            ))
        results = db.list_print_history(limit=3)
        assert len(results) == 3

    def test_list_filter_by_printer_name(self, db) -> None:
        now = time.time()
        db.save_print_record(_make_print_record(
            job_id="v1", printer_name="voron", completed_at=now,
        ))
        db.save_print_record(_make_print_record(
            job_id="e1", printer_name="ender3", completed_at=now + 1,
        ))
        db.save_print_record(_make_print_record(
            job_id="v2", printer_name="voron", completed_at=now + 2,
        ))

        voron_results = db.list_print_history(printer_name="voron")
        assert len(voron_results) == 2
        assert all(r["printer_name"] == "voron" for r in voron_results)

        ender_results = db.list_print_history(printer_name="ender3")
        assert len(ender_results) == 1
        assert ender_results[0]["printer_name"] == "ender3"

    def test_list_filter_by_status(self, db) -> None:
        now = time.time()
        db.save_print_record(_make_print_record(
            job_id="ok1", status="completed", completed_at=now,
        ))
        db.save_print_record(_make_print_record(
            job_id="fail1", status="failed", completed_at=now + 1,
        ))
        db.save_print_record(_make_print_record(
            job_id="ok2", status="completed", completed_at=now + 2,
        ))

        completed = db.list_print_history(status="completed")
        assert len(completed) == 2
        assert all(r["status"] == "completed" for r in completed)

        failed = db.list_print_history(status="failed")
        assert len(failed) == 1
        assert failed[0]["job_id"] == "fail1"

    def test_list_filter_by_printer_and_status(self, db) -> None:
        now = time.time()
        db.save_print_record(_make_print_record(
            job_id="vc", printer_name="voron", status="completed",
            completed_at=now,
        ))
        db.save_print_record(_make_print_record(
            job_id="vf", printer_name="voron", status="failed",
            completed_at=now + 1,
        ))
        db.save_print_record(_make_print_record(
            job_id="ec", printer_name="ender3", status="completed",
            completed_at=now + 2,
        ))

        results = db.list_print_history(printer_name="voron", status="completed")
        assert len(results) == 1
        assert results[0]["job_id"] == "vc"

    def test_list_empty_database(self, db) -> None:
        results = db.list_print_history()
        assert results == []

    def test_list_no_matching_filter(self, db) -> None:
        db.save_print_record(_make_print_record(
            job_id="x", printer_name="voron", completed_at=time.time(),
        ))
        results = db.list_print_history(printer_name="nonexistent")
        assert results == []

    def test_list_ordered_newest_first(self, db) -> None:
        now = time.time()
        db.save_print_record(_make_print_record(
            job_id="old", completed_at=now - 100,
        ))
        db.save_print_record(_make_print_record(
            job_id="new", completed_at=now,
        ))
        results = db.list_print_history()
        assert results[0]["job_id"] == "new"
        assert results[1]["job_id"] == "old"

    def test_list_metadata_deserialized(self, db) -> None:
        meta = {"quality": "high", "supports": True}
        db.save_print_record(_make_print_record(
            job_id="meta-list",
            metadata=meta,
            completed_at=time.time(),
        ))
        results = db.list_print_history()
        assert len(results) == 1
        assert results[0]["metadata"] == meta


# ===================================================================
# Printer Stats
# ===================================================================

class TestPrinterStats:
    """Tests for get_printer_stats()."""

    def test_basic_stats(self, db) -> None:
        now = time.time()
        db.save_print_record(_make_print_record(
            job_id="s1", printer_name="voron", status="completed",
            duration_seconds=3600.0, completed_at=now,
        ))
        db.save_print_record(_make_print_record(
            job_id="s2", printer_name="voron", status="completed",
            duration_seconds=1800.0, completed_at=now + 1,
        ))
        db.save_print_record(_make_print_record(
            job_id="s3", printer_name="voron", status="failed",
            duration_seconds=600.0, completed_at=now + 2,
        ))

        stats = db.get_printer_stats("voron")
        assert stats["printer_name"] == "voron"
        assert stats["total_prints"] == 3
        assert stats["success_rate"] == pytest.approx(2 / 3, rel=1e-3)
        assert stats["avg_duration_seconds"] == pytest.approx(2000.0, rel=1e-1)
        assert stats["total_print_hours"] == pytest.approx(
            (3600 + 1800 + 600) / 3600.0, rel=1e-2
        )

    def test_empty_stats(self, db) -> None:
        stats = db.get_printer_stats("nonexistent_printer")
        assert stats["printer_name"] == "nonexistent_printer"
        assert stats["total_prints"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["avg_duration_seconds"] is None
        assert stats["total_print_hours"] == 0.0

    def test_stats_all_successful(self, db) -> None:
        now = time.time()
        for i in range(5):
            db.save_print_record(_make_print_record(
                job_id=f"ok-{i}", printer_name="prusa",
                status="completed", duration_seconds=1000.0,
                completed_at=now + i,
            ))
        stats = db.get_printer_stats("prusa")
        assert stats["total_prints"] == 5
        assert stats["success_rate"] == 1.0

    def test_stats_all_failed(self, db) -> None:
        now = time.time()
        for i in range(3):
            db.save_print_record(_make_print_record(
                job_id=f"fail-{i}", printer_name="broken",
                status="failed", duration_seconds=100.0,
                completed_at=now + i,
            ))
        stats = db.get_printer_stats("broken")
        assert stats["total_prints"] == 3
        assert stats["success_rate"] == 0.0

    def test_stats_no_duration(self, db) -> None:
        db.save_print_record(_make_print_record(
            job_id="no-dur", printer_name="notime",
            status="completed", duration_seconds=None,
            completed_at=time.time(),
        ))
        stats = db.get_printer_stats("notime")
        assert stats["total_prints"] == 1
        assert stats["avg_duration_seconds"] is None
        assert stats["total_print_hours"] == 0.0

    def test_stats_only_count_target_printer(self, db) -> None:
        now = time.time()
        db.save_print_record(_make_print_record(
            job_id="a1", printer_name="alpha", status="completed",
            duration_seconds=1000.0, completed_at=now,
        ))
        db.save_print_record(_make_print_record(
            job_id="b1", printer_name="beta", status="completed",
            duration_seconds=2000.0, completed_at=now + 1,
        ))

        alpha_stats = db.get_printer_stats("alpha")
        assert alpha_stats["total_prints"] == 1

        beta_stats = db.get_printer_stats("beta")
        assert beta_stats["total_prints"] == 1


# ===================================================================
# Print Notes
# ===================================================================

class TestPrintNotes:
    """Tests for update_print_notes()."""

    def test_update_existing_record(self, db) -> None:
        db.save_print_record(_make_print_record(job_id="note-job"))
        result = db.update_print_notes("note-job", "First layer was rough")
        assert result is True

        record = db.get_print_record("note-job")
        assert record is not None
        assert record["notes"] == "First layer was rough"

    def test_update_overwrites_notes(self, db) -> None:
        db.save_print_record(_make_print_record(
            job_id="overwrite-job", notes="Original note",
        ))
        db.update_print_notes("overwrite-job", "Updated note")

        record = db.get_print_record("overwrite-job")
        assert record["notes"] == "Updated note"

    def test_update_nonexistent_returns_false(self, db) -> None:
        result = db.update_print_notes("no-such-job", "Some notes")
        assert result is False

    def test_update_empty_notes(self, db) -> None:
        db.save_print_record(_make_print_record(
            job_id="empty-note", notes="Had notes before",
        ))
        db.update_print_notes("empty-note", "")

        record = db.get_print_record("empty-note")
        assert record["notes"] == ""


# ===================================================================
# Agent Memory -- save and get roundtrip
# ===================================================================

class TestAgentMemory:
    """Tests for save_memory and get_memory."""

    def test_save_and_get_roundtrip(self, db) -> None:
        db.save_memory("agent-1", "global", "preferred_material", "PLA")
        result = db.get_memory("agent-1", "global", "preferred_material")
        assert result == "PLA"

    def test_get_nonexistent_returns_none(self, db) -> None:
        result = db.get_memory("agent-1", "global", "nonexistent_key")
        assert result is None

    def test_save_overwrites_same_key(self, db) -> None:
        db.save_memory("agent-1", "global", "pref", "PLA")
        assert db.get_memory("agent-1", "global", "pref") == "PLA"

        db.save_memory("agent-1", "global", "pref", "PETG")
        assert db.get_memory("agent-1", "global", "pref") == "PETG"

    def test_different_agents_independent(self, db) -> None:
        db.save_memory("agent-1", "global", "key", "value-1")
        db.save_memory("agent-2", "global", "key", "value-2")

        assert db.get_memory("agent-1", "global", "key") == "value-1"
        assert db.get_memory("agent-2", "global", "key") == "value-2"

    def test_different_scopes_independent(self, db) -> None:
        db.save_memory("agent-1", "printer", "key", "printer-value")
        db.save_memory("agent-1", "global", "key", "global-value")

        assert db.get_memory("agent-1", "printer", "key") == "printer-value"
        assert db.get_memory("agent-1", "global", "key") == "global-value"

    def test_numeric_value(self, db) -> None:
        db.save_memory("agent-1", "stats", "print_count", 42)
        result = db.get_memory("agent-1", "stats", "print_count")
        assert result == 42

    def test_boolean_value(self, db) -> None:
        db.save_memory("agent-1", "flags", "auto_level", True)
        result = db.get_memory("agent-1", "flags", "auto_level")
        assert result is True

    def test_none_value(self, db) -> None:
        db.save_memory("agent-1", "global", "cleared", None)
        result = db.get_memory("agent-1", "global", "cleared")
        assert result is None


# ===================================================================
# Agent Memory -- list_memory
# ===================================================================

class TestAgentMemoryList:
    """Tests for list_memory()."""

    def test_list_all_entries(self, db) -> None:
        db.save_memory("agent-1", "global", "key1", "val1")
        db.save_memory("agent-1", "global", "key2", "val2")
        db.save_memory("agent-1", "printer", "key3", "val3")

        results = db.list_memory("agent-1")
        assert len(results) == 3

    def test_list_with_scope_filter(self, db) -> None:
        db.save_memory("agent-1", "global", "g1", "gv1")
        db.save_memory("agent-1", "global", "g2", "gv2")
        db.save_memory("agent-1", "printer", "p1", "pv1")

        global_results = db.list_memory("agent-1", scope="global")
        assert len(global_results) == 2
        assert all(r["scope"] == "global" for r in global_results)

        printer_results = db.list_memory("agent-1", scope="printer")
        assert len(printer_results) == 1
        assert printer_results[0]["scope"] == "printer"

    def test_list_empty(self, db) -> None:
        results = db.list_memory("nonexistent-agent")
        assert results == []

    def test_list_only_target_agent(self, db) -> None:
        db.save_memory("agent-1", "global", "key", "val-1")
        db.save_memory("agent-2", "global", "key", "val-2")

        results = db.list_memory("agent-1")
        assert len(results) == 1
        assert results[0]["value"] == "val-1"

    def test_list_values_deserialized(self, db) -> None:
        db.save_memory("agent-1", "data", "config", {"threshold": 0.5})
        results = db.list_memory("agent-1")
        assert len(results) == 1
        assert results[0]["value"] == {"threshold": 0.5}

    def test_list_scope_filter_no_match(self, db) -> None:
        db.save_memory("agent-1", "global", "key", "value")
        results = db.list_memory("agent-1", scope="nonexistent_scope")
        assert results == []


# ===================================================================
# Agent Memory -- delete_memory
# ===================================================================

class TestAgentMemoryDelete:
    """Tests for delete_memory()."""

    def test_delete_existing(self, db) -> None:
        db.save_memory("agent-1", "global", "to_delete", "bye")
        assert db.get_memory("agent-1", "global", "to_delete") == "bye"

        result = db.delete_memory("agent-1", "global", "to_delete")
        assert result is True

        assert db.get_memory("agent-1", "global", "to_delete") is None

    def test_delete_nonexistent_returns_false(self, db) -> None:
        result = db.delete_memory("agent-1", "global", "no_such_key")
        assert result is False

    def test_delete_only_target_entry(self, db) -> None:
        db.save_memory("agent-1", "global", "keep", "kept")
        db.save_memory("agent-1", "global", "remove", "removed")

        db.delete_memory("agent-1", "global", "remove")

        assert db.get_memory("agent-1", "global", "keep") == "kept"
        assert db.get_memory("agent-1", "global", "remove") is None

    def test_delete_scoped_correctly(self, db) -> None:
        """Deleting from one scope should not affect the same key in another scope."""
        db.save_memory("agent-1", "global", "shared_key", "global-val")
        db.save_memory("agent-1", "printer", "shared_key", "printer-val")

        db.delete_memory("agent-1", "global", "shared_key")

        assert db.get_memory("agent-1", "global", "shared_key") is None
        assert db.get_memory("agent-1", "printer", "shared_key") == "printer-val"


# ===================================================================
# Agent Memory -- JSON value roundtrip
# ===================================================================

class TestAgentMemoryJSONRoundtrip:
    """Tests for storing complex JSON values in agent memory."""

    def test_dict_roundtrip(self, db) -> None:
        data = {"printer": "voron", "settings": {"speed": 100, "temp": 210}}
        db.save_memory("agent-1", "config", "profile", data)
        result = db.get_memory("agent-1", "config", "profile")
        assert result == data
        assert result["settings"]["speed"] == 100

    def test_list_roundtrip(self, db) -> None:
        data = [1, 2, 3, "four", {"five": 5}]
        db.save_memory("agent-1", "data", "sequence", data)
        result = db.get_memory("agent-1", "data", "sequence")
        assert result == data
        assert len(result) == 5
        assert result[4]["five"] == 5

    def test_nested_dict_roundtrip(self, db) -> None:
        data = {
            "printers": {
                "voron": {"prints": 42, "failures": 3},
                "ender3": {"prints": 100, "failures": 15},
            },
            "total": 142,
        }
        db.save_memory("agent-1", "stats", "fleet", data)
        result = db.get_memory("agent-1", "stats", "fleet")
        assert result == data
        assert result["printers"]["voron"]["prints"] == 42

    def test_string_roundtrip(self, db) -> None:
        db.save_memory("agent-1", "notes", "observation", "The bed leveling was off")
        result = db.get_memory("agent-1", "notes", "observation")
        assert result == "The bed leveling was off"

    def test_float_roundtrip(self, db) -> None:
        db.save_memory("agent-1", "calibration", "z_offset", -0.15)
        result = db.get_memory("agent-1", "calibration", "z_offset")
        assert result == pytest.approx(-0.15)

    def test_overwrite_preserves_created_at(self, db) -> None:
        """When overwriting, created_at should be preserved from original entry."""
        db.save_memory("agent-1", "global", "evolving", "v1")
        entries_v1 = db.list_memory("agent-1")
        assert len(entries_v1) == 1
        created_at_v1 = entries_v1[0]["created_at"]

        # Small delay to ensure different timestamps
        time.sleep(0.05)

        db.save_memory("agent-1", "global", "evolving", "v2")
        entries_v2 = db.list_memory("agent-1")
        assert len(entries_v2) == 1
        assert entries_v2[0]["value"] == "v2"
        assert entries_v2[0]["created_at"] == pytest.approx(created_at_v1, abs=0.01)
        assert entries_v2[0]["updated_at"] > created_at_v1
