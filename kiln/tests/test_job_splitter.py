"""Tests for kiln.job_splitter.

Coverage areas:
- SplitJob, SplitPlan, SplitProgress dataclasses
- plan_multi_copy_split with various printer/copy counts
- plan_assembly_split with various file counts
- Time savings calculation
- Progress tracking
- Cancel plan
- Edge cases: single printer, single copy, no printers
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from kiln.job_splitter import (
    SplitJob,
    SplitPlan,
    SplitProgress,
    cancel_split_plan,
    get_split_progress,
    plan_assembly_split,
    plan_multi_copy_split,
)


class TestSplitJobDataclass:
    """SplitJob to_dict() returns a plain dict."""

    def test_to_dict_returns_all_fields(self):
        sj = SplitJob(
            part_id="copy_1",
            file_path="/tmp/test.gcode",
            printer_name="ender3",
            printer_model="Ender 3",
            estimated_time_seconds=3600,
            material="pla",
            settings={"speed": 60},
            status="pending",
            job_id=None,
        )
        d = sj.to_dict()
        assert d["part_id"] == "copy_1"
        assert d["printer_name"] == "ender3"
        assert d["status"] == "pending"
        assert d["job_id"] is None

    def test_to_dict_with_job_id(self):
        sj = SplitJob(
            part_id="p1",
            file_path="/tmp/a.gcode",
            printer_name="p",
            printer_model="m",
            estimated_time_seconds=100,
            material="abs",
            settings={},
            status="printing",
            job_id="job-123",
        )
        assert sj.to_dict()["job_id"] == "job-123"


class TestSplitPlanDataclass:
    """SplitPlan to_dict() nests SplitJob dicts correctly."""

    def test_to_dict_serialises_parts(self):
        parts = [
            SplitJob("p1", "/a", "pr1", "m1", 100, "pla", {}, "pending"),
            SplitJob("p2", "/b", "pr2", "m2", 200, "pla", {}, "pending"),
        ]
        sp = SplitPlan(
            original_file="/a",
            split_type="multi_copy",
            parts=parts,
            total_printers=2,
            estimated_total_time_seconds=200,
            estimated_sequential_time_seconds=300,
            time_savings_percentage=33.3,
            assembly_instructions=None,
        )
        d = sp.to_dict()
        assert len(d["parts"]) == 2
        assert d["parts"][0]["part_id"] == "p1"
        assert d["time_savings_percentage"] == 33.3


class TestSplitProgressDataclass:
    """SplitProgress to_dict() returns plain dict."""

    def test_to_dict(self):
        sp = SplitProgress(
            plan_id="plan-1",
            total_parts=4,
            completed_parts=2,
            failed_parts=0,
            in_progress_parts=1,
            pending_parts=1,
            overall_progress=0.5,
            estimated_remaining_seconds=1800,
            part_statuses=[],
        )
        d = sp.to_dict()
        assert d["plan_id"] == "plan-1"
        assert d["overall_progress"] == 0.5


class TestPlanMultiCopySplit:
    """plan_multi_copy_split distributes copies across printers."""

    def test_single_copy_single_printer(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n" * 100)
            path = f.name
        try:
            plan = plan_multi_copy_split(
                path, 1,
                available_printers=[{"name": "p1", "model": "Ender 3"}],
            )
            assert plan.split_type == "multi_copy"
            assert len(plan.parts) == 1
            assert plan.total_printers == 1
            assert plan.time_savings_percentage == 0.0
        finally:
            os.unlink(path)

    def test_multiple_copies_multiple_printers(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n" * 100)
            path = f.name
        try:
            printers = [
                {"name": "p1", "model": "Ender 3"},
                {"name": "p2", "model": "Prusa MK3"},
                {"name": "p3", "model": "Bambu X1"},
            ]
            plan = plan_multi_copy_split(path, 6, available_printers=printers)
            assert plan.total_printers == 3
            assert len(plan.parts) == 6
            # Each printer gets 2 copies
            printer_counts = {}
            for p in plan.parts:
                printer_counts[p.printer_name] = printer_counts.get(p.printer_name, 0) + 1
            assert printer_counts["p1"] == 2
            assert printer_counts["p2"] == 2
            assert printer_counts["p3"] == 2
        finally:
            os.unlink(path)

    def test_more_printers_than_copies(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n" * 100)
            path = f.name
        try:
            printers = [
                {"name": f"p{i}", "model": "test"} for i in range(5)
            ]
            plan = plan_multi_copy_split(path, 2, available_printers=printers)
            assert plan.total_printers == 2
            assert len(plan.parts) == 2
        finally:
            os.unlink(path)

    def test_time_savings_with_parallelism(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n" * 100)
            path = f.name
        try:
            printers = [
                {"name": "p1", "model": "test"},
                {"name": "p2", "model": "test"},
            ]
            plan = plan_multi_copy_split(path, 4, available_printers=printers)
            # 4 copies, 2 printers -> 2 copies each -> 50% savings
            assert plan.time_savings_percentage == 50.0
        finally:
            os.unlink(path)

    def test_material_assignment(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n")
            path = f.name
        try:
            plan = plan_multi_copy_split(
                path, 2,
                material="abs",
                available_printers=[{"name": "p1", "model": "test"}],
            )
            assert all(p.material == "abs" for p in plan.parts)
        finally:
            os.unlink(path)

    def test_no_available_printers_uses_default(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n")
            path = f.name
        try:
            plan = plan_multi_copy_split(path, 3, available_printers=[])
            assert plan.total_printers == 1
            assert plan.parts[0].printer_name == "default"
        finally:
            os.unlink(path)

    def test_nonexistent_file_uses_default_estimate(self):
        plan = plan_multi_copy_split(
            "/nonexistent/file.gcode", 2,
            available_printers=[{"name": "p1", "model": "test"}],
        )
        assert len(plan.parts) == 2
        # Default 1 hour estimate when file doesn't exist
        assert plan.parts[0].estimated_time_seconds == 3600


class TestPlanAssemblySplit:
    """plan_assembly_split distributes files across printers."""

    def test_single_file_single_printer(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n")
            path = f.name
        try:
            plan = plan_assembly_split(
                [path],
                available_printers=[{"name": "p1", "model": "test"}],
            )
            assert plan.split_type == "assembly"
            assert len(plan.parts) == 1
            assert plan.assembly_instructions is not None
        finally:
            os.unlink(path)

    def test_multiple_files_multiple_printers(self):
        files = []
        for i in range(3):
            with tempfile.NamedTemporaryFile(suffix=f"_part{i}.gcode", delete=False) as f:
                f.write(b"G28\n" * 100)
                files.append(f.name)
        try:
            printers = [
                {"name": "p1", "model": "test"},
                {"name": "p2", "model": "test"},
                {"name": "p3", "model": "test"},
            ]
            plan = plan_assembly_split(files, available_printers=printers)
            assert plan.total_printers == 3
            assert len(plan.parts) == 3
            # Each file assigned to a different printer
            printer_names = {p.printer_name for p in plan.parts}
            assert len(printer_names) == 3
        finally:
            for fp in files:
                os.unlink(fp)

    def test_assembly_instructions_generated(self):
        files = []
        for i in range(2):
            with tempfile.NamedTemporaryFile(suffix=f"_part{i}.gcode", delete=False) as f:
                f.write(b"G28\n")
                files.append(f.name)
        try:
            plan = plan_assembly_split(
                files,
                available_printers=[{"name": "p1", "model": "test"}],
            )
            assert plan.assembly_instructions is not None
            assert len(plan.assembly_instructions) == 3  # 2 collect steps + 1 assemble
        finally:
            for fp in files:
                os.unlink(fp)

    def test_time_savings_calculation(self):
        files = []
        for i in range(2):
            with tempfile.NamedTemporaryFile(suffix=f"_p{i}.gcode", delete=False) as f:
                f.write(b"G28\n" * 100)
                files.append(f.name)
        try:
            printers = [
                {"name": "p1", "model": "test"},
                {"name": "p2", "model": "test"},
            ]
            plan = plan_assembly_split(files, available_printers=printers)
            # With 2 files of similar size across 2 printers, savings ~ 50%
            assert plan.time_savings_percentage >= 0.0
            assert plan.estimated_total_time_seconds <= plan.estimated_sequential_time_seconds
        finally:
            for fp in files:
                os.unlink(fp)


class TestSplitPlanPersistence:
    """Tests for submit, progress, and cancel with mock DB."""

    def _make_mock_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE split_plans (
                id TEXT PRIMARY KEY,
                original_file TEXT,
                split_type TEXT NOT NULL,
                parts TEXT NOT NULL,
                total_printers INTEGER,
                created_at REAL NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)
        conn.commit()
        db = MagicMock()
        db._conn = conn
        return db

    @patch("kiln.persistence.get_db")
    def test_get_split_progress_not_found(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        progress = get_split_progress("nonexistent")
        assert progress.total_parts == 0
        assert progress.overall_progress == 0.0

    @patch("kiln.persistence.get_db")
    def test_get_split_progress_with_data(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        parts = [
            {"part_id": "p1", "status": "completed", "estimated_time_seconds": 100},
            {"part_id": "p2", "status": "printing", "estimated_time_seconds": 200},
            {"part_id": "p3", "status": "pending", "estimated_time_seconds": 300},
        ]
        db._conn.execute(
            "INSERT INTO split_plans (id, original_file, split_type, parts, total_printers, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("plan-1", "/test.gcode", "multi_copy", json.dumps(parts), 3, 1000.0),
        )
        db._conn.commit()

        progress = get_split_progress("plan-1")
        assert progress.total_parts == 3
        assert progress.completed_parts == 1
        assert progress.in_progress_parts == 1
        assert progress.pending_parts == 1
        assert progress.overall_progress == pytest.approx(0.33, abs=0.01)

    @patch("kiln.persistence.get_db")
    def test_cancel_split_plan_not_found(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        result = cancel_split_plan("nonexistent")
        assert result["success"] is False
