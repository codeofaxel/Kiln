"""Tests for the recommend_settings MCP tool and persistence layer."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from kiln.persistence import KilnDB


@pytest.fixture()
def db(tmp_path: Path) -> KilnDB:
    """Fresh database for each test."""
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
    base: Dict[str, Any] = {
        "job_id": job_id,
        "printer_name": printer_name,
        "file_name": "benchy.gcode",
        "file_hash": "abc123",
        "material_type": "PLA",
        "outcome": outcome,
        "quality_grade": "good",
        "failure_mode": None,
        "settings": {"temp_tool": 210, "temp_bed": 60, "speed": 50},
        "environment": None,
        "notes": None,
        "agent_id": "claude",
        "created_at": time.time(),
    }
    base.update(kwargs)
    return base


class TestGetSuccessfulSettings:
    """Test the persistence layer method."""

    def test_returns_only_successes(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", outcome="success"))
        db.save_print_outcome(_outcome(job_id="j2", outcome="failed"))
        results = db.get_successful_settings()
        assert len(results) == 1
        assert results[0]["outcome"] == "success"

    def test_returns_only_with_settings(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", settings={"temp_tool": 210}))
        db.save_print_outcome(_outcome(job_id="j2", settings=None))
        results = db.get_successful_settings()
        assert len(results) == 1

    def test_filter_by_printer(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", printer_name="voron"))
        db.save_print_outcome(_outcome(job_id="j2", printer_name="ender3"))
        results = db.get_successful_settings(printer_name="voron")
        assert len(results) == 1
        assert results[0]["printer_name"] == "voron"

    def test_filter_by_material(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", material_type="PLA"))
        db.save_print_outcome(_outcome(job_id="j2", material_type="PETG"))
        results = db.get_successful_settings(material_type="PLA")
        assert len(results) == 1

    def test_filter_by_file_hash(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", file_hash="aaa"))
        db.save_print_outcome(_outcome(job_id="j2", file_hash="bbb"))
        results = db.get_successful_settings(file_hash="aaa")
        assert len(results) == 1

    def test_ordered_by_quality_then_recency(self, db: KilnDB) -> None:
        db.save_print_outcome(_outcome(job_id="j1", quality_grade="good", created_at=1000.0))
        db.save_print_outcome(_outcome(job_id="j2", quality_grade="excellent", created_at=900.0))
        db.save_print_outcome(_outcome(job_id="j3", quality_grade="poor", created_at=2000.0))
        results = db.get_successful_settings()
        assert results[0]["quality_grade"] == "excellent"
        assert results[1]["quality_grade"] == "good"
        assert results[2]["quality_grade"] == "poor"

    def test_limit(self, db: KilnDB) -> None:
        for i in range(10):
            db.save_print_outcome(_outcome(job_id=f"j{i}"))
        results = db.get_successful_settings(limit=3)
        assert len(results) == 3

    def test_empty_results(self, db: KilnDB) -> None:
        results = db.get_successful_settings()
        assert results == []


class TestRecommendSettingsTool:
    """Test the MCP tool layer."""

    @patch("kiln.persistence.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_recommends_median_settings(self, mock_auth, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_successful_settings.return_value = [
            {"settings": {"temp_tool": 200, "temp_bed": 60, "speed": 50}, "quality_grade": "good", "printer_name": "v", "material_type": "PLA", "notes": None},
            {"settings": {"temp_tool": 210, "temp_bed": 60, "speed": 55}, "quality_grade": "good", "printer_name": "v", "material_type": "PLA", "notes": None},
            {"settings": {"temp_tool": 220, "temp_bed": 65, "speed": 60}, "quality_grade": "excellent", "printer_name": "v", "material_type": "PLA", "notes": None},
        ]

        from kiln.plugins.learning_tools import recommend_settings
        result = recommend_settings(printer_name="voron", material_type="PLA")

        assert result["success"] is True
        assert result["has_data"] is True
        assert result["recommended_settings"]["temp_tool"] == 210.0
        assert result["sample_size"] == 3

    @patch("kiln.server._check_auth", return_value=None)
    def test_requires_at_least_one_filter(self, mock_auth):
        from kiln.plugins.learning_tools import recommend_settings
        result = recommend_settings()
        assert "error" in result

    @patch("kiln.persistence.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_no_data_returns_gracefully(self, mock_auth, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_successful_settings.return_value = []

        from kiln.plugins.learning_tools import recommend_settings
        result = recommend_settings(printer_name="voron")

        assert result["success"] is True
        assert result["has_data"] is False

    @patch("kiln.persistence.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_confidence_scales_with_sample_size(self, mock_auth, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        # 2 outcomes = low confidence
        mock_db.get_successful_settings.return_value = [
            {"settings": {"temp_tool": 210}, "quality_grade": "good", "printer_name": "v", "material_type": "PLA", "notes": None},
            {"settings": {"temp_tool": 210}, "quality_grade": "good", "printer_name": "v", "material_type": "PLA", "notes": None},
        ]
        from kiln.plugins.learning_tools import recommend_settings
        result = recommend_settings(printer_name="voron")
        assert result["confidence"] == "low"

    @patch("kiln.persistence.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_includes_safety_notice(self, mock_auth, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_successful_settings.return_value = [
            {"settings": {"temp_tool": 210}, "quality_grade": "good", "printer_name": "v", "material_type": "PLA", "notes": None},
        ]
        from kiln.plugins.learning_tools import recommend_settings
        result = recommend_settings(material_type="PLA")
        assert "safety_notice" in result

    @patch("kiln.persistence.get_db")
    @patch("kiln.server._check_auth", return_value=None)
    def test_slicer_profile_mode(self, mock_auth, mock_get_db):
        """Most common slicer profile should be recommended."""
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_successful_settings.return_value = [
            {"settings": {"slicer_profile": "fast", "temp_tool": 210}, "quality_grade": "good", "printer_name": "v", "material_type": "PLA", "notes": None},
            {"settings": {"slicer_profile": "fast", "temp_tool": 210}, "quality_grade": "good", "printer_name": "v", "material_type": "PLA", "notes": None},
            {"settings": {"slicer_profile": "quality", "temp_tool": 200}, "quality_grade": "excellent", "printer_name": "v", "material_type": "PLA", "notes": None},
        ]
        from kiln.plugins.learning_tools import recommend_settings
        result = recommend_settings(printer_name="voron")
        assert result["recommended_settings"]["slicer_profile"] == "fast"
