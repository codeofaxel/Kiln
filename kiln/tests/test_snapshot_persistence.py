"""Tests for snapshot persistence in KilnDB.

Covers:
- save_snapshot (basic, all fields, defaults)
- get_snapshots (filters, ordering, limit)
- delete_snapshots (by job_id, by older_than, no filters)
- Edge cases (nullable fields, combined filters)
"""
from __future__ import annotations

import time

import pytest

from kiln.persistence import KilnDB


class TestSnapshotPersistence:
    """Snapshot save / query / delete operations."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path):
        self.db = KilnDB(db_path=str(tmp_path / "test.db"))
        yield
        self.db.close()

    def test_save_snapshot_returns_row_id(self):
        row_id = self.db.save_snapshot(
            printer_name="voron",
            image_path="/tmp/frame_0000.jpg",
        )
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_save_snapshot_with_all_fields(self):
        row_id = self.db.save_snapshot(
            printer_name="voron",
            image_path="/tmp/frame_0001.jpg",
            job_id="job-abc",
            phase="first_layer",
            image_size_bytes=54321,
            analysis='{"quality": "good"}',
            agent_notes="Looks fine",
            confidence=0.95,
            completion_pct=8.5,
        )
        snaps = self.db.get_snapshots(job_id="job-abc")
        assert len(snaps) == 1
        s = snaps[0]
        assert s["printer_name"] == "voron"
        assert s["phase"] == "first_layer"
        assert s["image_size_bytes"] == 54321
        assert s["confidence"] == 0.95
        assert s["completion_pct"] == 8.5
        assert s["agent_notes"] == "Looks fine"

    def test_get_snapshots_empty(self):
        result = self.db.get_snapshots()
        assert result == []

    def test_get_snapshots_filter_by_printer(self):
        self.db.save_snapshot(printer_name="voron", image_path="/tmp/a.jpg")
        self.db.save_snapshot(printer_name="bambu", image_path="/tmp/b.jpg")
        result = self.db.get_snapshots(printer_name="voron")
        assert len(result) == 1
        assert result[0]["printer_name"] == "voron"

    def test_get_snapshots_filter_by_phase(self):
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/a.jpg", phase="first_layer",
        )
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/b.jpg", phase="timelapse",
        )
        result = self.db.get_snapshots(phase="timelapse")
        assert len(result) == 1
        assert result[0]["phase"] == "timelapse"

    def test_get_snapshots_filter_by_job_id(self):
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/a.jpg", job_id="j1",
        )
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/b.jpg", job_id="j2",
        )
        result = self.db.get_snapshots(job_id="j1")
        assert len(result) == 1

    def test_get_snapshots_limit(self):
        for i in range(10):
            self.db.save_snapshot(
                printer_name="voron", image_path=f"/tmp/{i}.jpg",
            )
        result = self.db.get_snapshots(limit=3)
        assert len(result) == 3

    def test_get_snapshots_ordered_newest_first(self):
        self.db.save_snapshot(printer_name="voron", image_path="/tmp/old.jpg")
        time.sleep(0.01)
        self.db.save_snapshot(printer_name="voron", image_path="/tmp/new.jpg")
        result = self.db.get_snapshots()
        assert result[0]["image_path"] == "/tmp/new.jpg"

    def test_delete_snapshots_by_job_id(self):
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/a.jpg", job_id="j1",
        )
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/b.jpg", job_id="j2",
        )
        deleted = self.db.delete_snapshots(job_id="j1")
        assert deleted == 1
        remaining = self.db.get_snapshots()
        assert len(remaining) == 1
        assert remaining[0]["job_id"] == "j2"

    def test_delete_snapshots_by_older_than(self):
        self.db.save_snapshot(printer_name="voron", image_path="/tmp/a.jpg")
        cutoff = time.time() + 1
        deleted = self.db.delete_snapshots(older_than=cutoff)
        assert deleted == 1
        assert self.db.get_snapshots() == []

    def test_delete_snapshots_no_filters_returns_zero(self):
        self.db.save_snapshot(printer_name="voron", image_path="/tmp/a.jpg")
        deleted = self.db.delete_snapshots()
        assert deleted == 0
        assert len(self.db.get_snapshots()) == 1

    def test_default_phase_is_unknown(self):
        self.db.save_snapshot(printer_name="voron", image_path="/tmp/a.jpg")
        snap = self.db.get_snapshots()[0]
        assert snap["phase"] == "unknown"

    def test_nullable_fields_default_to_none(self):
        self.db.save_snapshot(printer_name="voron", image_path="/tmp/a.jpg")
        snap = self.db.get_snapshots()[0]
        assert snap["job_id"] is None
        assert snap["analysis"] is None
        assert snap["confidence"] is None
        assert snap["completion_pct"] is None

    def test_multiple_filters_combined(self):
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/a.jpg",
            job_id="j1", phase="first_layer",
        )
        self.db.save_snapshot(
            printer_name="voron", image_path="/tmp/b.jpg",
            job_id="j1", phase="timelapse",
        )
        self.db.save_snapshot(
            printer_name="bambu", image_path="/tmp/c.jpg",
            job_id="j1", phase="first_layer",
        )
        result = self.db.get_snapshots(printer_name="voron", phase="first_layer")
        assert len(result) == 1
        assert result[0]["image_path"] == "/tmp/a.jpg"
