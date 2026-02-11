"""Tests for cross-printer learning persistence layer.

Covers the print_outcomes table CRUD, aggregation queries,
printer insights, file outcomes, and printer suggestions.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

import pytest

from kiln.persistence import KilnDB


@pytest.fixture()
def db(tmp_path: Path) -> KilnDB:
    """Fresh in-memory database for each test."""
    db_path = str(tmp_path / "test.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


def _outcome(
    job_id: str = "job-1",
    printer_name: str = "voron",
    outcome: str = "success",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Helper to build an outcome dict with sensible defaults."""
    base: Dict[str, Any] = {
        "job_id": job_id,
        "printer_name": printer_name,
        "file_name": "benchy.gcode",
        "file_hash": "abc123",
        "material_type": "PLA",
        "outcome": outcome,
        "quality_grade": "good",
        "failure_mode": None,
        "settings": {"temp_tool": 210, "temp_bed": 60},
        "environment": {"ambient_temp": 22},
        "notes": None,
        "agent_id": "claude",
        "created_at": time.time(),
    }
    base.update(kwargs)
    return base


class TestSavePrintOutcome:
    def test_save_returns_row_id(self, db: KilnDB) -> None:
        row_id = db.save_print_outcome(_outcome())
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_save_multiple_returns_incrementing_ids(self, db: KilnDB) -> None:
        id1 = db.save_print_outcome(_outcome(job_id="j1"))
        id2 = db.save_print_outcome(_outcome(job_id="j2"))
        assert id2 > id1

    def test_save_minimal_fields(self, db: KilnDB) -> None:
        row_id = db.save_print_outcome({
            "job_id": "j1",
            "printer_name": "ender3",
            "outcome": "failed",
            "created_at": time.time(),
        })
        assert row_id >= 1

    def test_save_with_none_settings(self, db: KilnDB) -> None:
        row_id = db.save_print_outcome(_outcome(settings=None, environment=None))
        assert row_id >= 1


class TestGetPrintOutcome:
    def test_get_existing(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1"))
        result = db.get_print_outcome("j1")
        assert result is not None
        assert result["job_id"] == "j1"
        assert result["outcome"] == "success"

    def test_get_nonexistent(self, db: KilnDB) -> None:
        assert db.get_print_outcome("nope") is None

    def test_settings_deserialized(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", settings={"temp": 210}))
        result = db.get_print_outcome("j1")
        assert result["settings"] == {"temp": 210}

    def test_environment_deserialized(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", environment={"humidity": 45}))
        result = db.get_print_outcome("j1")
        assert result["environment"] == {"humidity": 45}

    def test_null_settings_returns_none(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", settings=None))
        result = db.get_print_outcome("j1")
        assert result["settings"] is None


class TestListPrintOutcomes:
    def test_list_all(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1"))
        db.save_print_outcome(_outcome(job_id="j2"))
        results = db.list_print_outcomes()
        assert len(results) == 2

    def test_filter_by_printer(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", printer_name="voron"))
        db.save_print_outcome(_outcome(job_id="j2", printer_name="ender3"))
        results = db.list_print_outcomes(printer_name="voron")
        assert len(results) == 1
        assert results[0]["printer_name"] == "voron"

    def test_filter_by_file_hash(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", file_hash="aaa"))
        db.save_print_outcome(_outcome(job_id="j2", file_hash="bbb"))
        results = db.list_print_outcomes(file_hash="aaa")
        assert len(results) == 1

    def test_filter_by_outcome(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", outcome="failed"))
        results = db.list_print_outcomes(outcome="failed")
        assert len(results) == 1
        assert results[0]["outcome"] == "failed"

    def test_combined_filters(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", printer_name="voron", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", printer_name="voron", outcome="failed"))
        db.save_print_outcome(_outcome(job_id="j3", printer_name="ender3", outcome="success"))
        results = db.list_print_outcomes(printer_name="voron", outcome="success")
        assert len(results) == 1

    def test_limit(self, db: KilnDB) -> None:
        for i in range(10):
            db.save_print_outcome(_outcome(job_id=f"j{i}"))
        results = db.list_print_outcomes(limit=3)
        assert len(results) == 3

    def test_empty_results(self, db: KilnDB) -> None:
        results = db.list_print_outcomes()
        assert results == []

    def test_ordered_by_created_at_desc(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="old", created_at=1000.0))
        db.save_print_outcome(_outcome(job_id="new", created_at=2000.0))
        results = db.list_print_outcomes()
        assert results[0]["job_id"] == "new"
        assert results[1]["job_id"] == "old"


class TestGetPrinterLearningInsights:
    def test_no_data(self, db: KilnDB) -> None:
        insights = db.get_printer_learning_insights("voron")
        assert insights["total_outcomes"] == 0
        assert insights["success_rate"] == 0.0

    def test_all_successes(self, db: KilnDB) -> None:
        for i in range(5):
            db.save_print_outcome(_outcome(job_id=f"j{i}", outcome="success"))
        insights = db.get_printer_learning_insights("voron")
        assert insights["total_outcomes"] == 5
        assert insights["success_rate"] == 1.0

    def test_mixed_outcomes(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j3", outcome="failed", failure_mode="warping"))
        insights = db.get_printer_learning_insights("voron")
        assert insights["total_outcomes"] == 3
        assert insights["success_rate"] == 0.67
        assert insights["failure_breakdown"] == {"warping": 1}

    def test_material_stats(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", material_type="PLA", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", material_type="PLA", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j3", material_type="PETG", outcome="failed"))
        insights = db.get_printer_learning_insights("voron")
        assert insights["material_stats"]["PLA"]["success_rate"] == 1.0
        assert insights["material_stats"]["PETG"]["success_rate"] == 0.0

    def test_ignores_other_printers(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", printer_name="voron", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", printer_name="ender3", outcome="failed"))
        insights = db.get_printer_learning_insights("voron")
        assert insights["total_outcomes"] == 1
        assert insights["success_rate"] == 1.0


class TestGetFileOutcomes:
    def test_single_printer(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", file_hash="abc", outcome="success"))
        result = db.get_file_outcomes("abc")
        assert result["best_printer"] == "voron"
        assert result["printers_tried"] == ["voron"]
        assert result["outcomes_by_printer"]["voron"]["success_rate"] == 1.0

    def test_multiple_printers(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", file_hash="abc", printer_name="voron", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", file_hash="abc", printer_name="voron", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j3", file_hash="abc", printer_name="ender3", outcome="failed"))
        result = db.get_file_outcomes("abc")
        assert result["best_printer"] == "voron"
        assert len(result["printers_tried"]) == 2

    def test_no_data(self, db: KilnDB) -> None:
        result = db.get_file_outcomes("nonexistent")
        assert result["printers_tried"] == []
        assert result["best_printer"] is None


class TestSuggestPrinter:
    def test_ranks_by_success_rate(self, db: KilnDB) -> None:
        # voron: 2/2 = 100%
        db.save_print_outcome(_outcome(job_id="j1", printer_name="voron", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", printer_name="voron", outcome="success"))
        # ender3: 1/3 = 33%
        db.save_print_outcome(_outcome(job_id="j3", printer_name="ender3", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j4", printer_name="ender3", outcome="failed"))
        db.save_print_outcome(_outcome(job_id="j5", printer_name="ender3", outcome="failed"))
        results = db.suggest_printer_for_outcome()
        assert results[0]["printer_name"] == "voron"
        assert results[0]["success_rate"] == 1.0
        assert results[1]["printer_name"] == "ender3"

    def test_filter_by_material(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", printer_name="voron", material_type="PLA", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", printer_name="ender3", material_type="PETG", outcome="success"))
        results = db.suggest_printer_for_outcome(material_type="PLA")
        assert len(results) == 1
        assert results[0]["printer_name"] == "voron"

    def test_filter_by_file_hash(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", file_hash="aaa", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", file_hash="bbb", outcome="success", printer_name="ender3"))
        results = db.suggest_printer_for_outcome(file_hash="aaa")
        assert len(results) == 1

    def test_empty_results(self, db: KilnDB) -> None:
        results = db.suggest_printer_for_outcome()
        assert results == []

    def test_tiebreak_by_volume(self, db: KilnDB) -> None:
        # Both 100% but voron has more prints
        for i in range(5):
            db.save_print_outcome(_outcome(job_id=f"v{i}", printer_name="voron", outcome="success"))
        db.save_print_outcome(_outcome(job_id="e1", printer_name="ender3", outcome="success"))
        results = db.suggest_printer_for_outcome()
        assert results[0]["printer_name"] == "voron"
        assert results[0]["total_prints"] == 5
