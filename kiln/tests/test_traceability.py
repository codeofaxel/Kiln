"""Tests for kiln.traceability -- PartTraceabilityRecord and TraceabilityEngine.

Covers:
- Record creation with all fields
- Record completion (success, failure, cancel)
- Retrieval by ID, printer, and material
- Compliance report generation
- Chain-of-custody verification (valid and invalid cases)
- Edge cases: missing fields, unknown IDs, empty queries, duplicate handling
- Validation: invalid status, empty required fields
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kiln.traceability import (
    PartTraceabilityRecord,
    TraceabilityEngine,
    TraceabilityError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> TraceabilityEngine:
    return TraceabilityEngine()


def _make_record(engine: TraceabilityEngine, **overrides) -> PartTraceabilityRecord:
    """Helper to create a record with sensible defaults."""
    defaults = {
        "printer_id": "prusa-mk4-01",
        "file_name": "bracket.gcode",
        "file_hash": "sha256:aabbccdd",
        "material": "PLA",
        "safety_profile_version": "1.0.0",
    }
    defaults.update(overrides)
    return engine.start_record(**defaults)


# ---------------------------------------------------------------------------
# PartTraceabilityRecord dataclass
# ---------------------------------------------------------------------------


class TestPartTraceabilityRecord:
    """Tests for the PartTraceabilityRecord dataclass."""

    def test_to_dict_returns_all_fields(self, engine):
        rec = _make_record(engine)
        d = rec.to_dict()
        assert d["part_id"] == rec.part_id
        assert d["printer_id"] == "prusa-mk4-01"
        assert d["file_name"] == "bracket.gcode"
        assert d["file_hash"] == "sha256:aabbccdd"
        assert d["material"] == "PLA"
        assert d["status"] == "printing"
        assert d["safety_profile_version"] == "1.0.0"
        assert d["operator_id"] is None
        assert d["material_batch"] is None
        assert d["slicer_settings_hash"] is None
        assert d["completed_at"] is None
        assert d["quality_metrics"] is None

    def test_to_dict_with_optional_fields(self, engine):
        rec = _make_record(
            engine,
            operator_id="op-42",
            material_batch="LOT-2024-001",
            slicer_settings_hash="sha256:1234",
        )
        d = rec.to_dict()
        assert d["operator_id"] == "op-42"
        assert d["material_batch"] == "LOT-2024-001"
        assert d["slicer_settings_hash"] == "sha256:1234"


# ---------------------------------------------------------------------------
# Record creation
# ---------------------------------------------------------------------------


class TestStartRecord:
    """Tests for TraceabilityEngine.start_record()."""

    def test_creates_record_with_uuid(self, engine):
        rec = _make_record(engine)
        assert rec.part_id is not None
        assert len(rec.part_id) == 36  # UUID format

    def test_status_is_printing(self, engine):
        rec = _make_record(engine)
        assert rec.status == "printing"

    def test_started_at_is_iso8601(self, engine):
        rec = _make_record(engine)
        dt = datetime.fromisoformat(rec.started_at)
        assert dt.tzinfo is not None

    def test_record_is_stored(self, engine):
        rec = _make_record(engine)
        assert engine.get_record(rec.part_id) is rec

    def test_empty_printer_id_raises(self, engine):
        with pytest.raises(TraceabilityError, match="printer_id must not be empty"):
            engine.start_record(
                printer_id="",
                file_name="test.gcode",
                file_hash="sha256:abc",
                material="PLA",
                safety_profile_version="1.0.0",
            )

    def test_empty_file_name_raises(self, engine):
        with pytest.raises(TraceabilityError, match="file_name must not be empty"):
            engine.start_record(
                printer_id="p1",
                file_name="",
                file_hash="sha256:abc",
                material="PLA",
                safety_profile_version="1.0.0",
            )

    def test_empty_file_hash_raises(self, engine):
        with pytest.raises(TraceabilityError, match="file_hash must not be empty"):
            engine.start_record(
                printer_id="p1",
                file_name="test.gcode",
                file_hash="",
                material="PLA",
                safety_profile_version="1.0.0",
            )

    def test_empty_material_raises(self, engine):
        with pytest.raises(TraceabilityError, match="material must not be empty"):
            engine.start_record(
                printer_id="p1",
                file_name="test.gcode",
                file_hash="sha256:abc",
                material="",
                safety_profile_version="1.0.0",
            )

    def test_empty_safety_profile_version_raises(self, engine):
        with pytest.raises(TraceabilityError, match="safety_profile_version must not be empty"):
            engine.start_record(
                printer_id="p1",
                file_name="test.gcode",
                file_hash="sha256:abc",
                material="PLA",
                safety_profile_version="",
            )

    def test_multiple_records_get_unique_ids(self, engine):
        r1 = _make_record(engine)
        r2 = _make_record(engine)
        assert r1.part_id != r2.part_id


# ---------------------------------------------------------------------------
# Record completion
# ---------------------------------------------------------------------------


class TestCompleteRecord:
    """Tests for TraceabilityEngine.complete_record()."""

    def test_complete_sets_status_and_timestamp(self, engine):
        rec = _make_record(engine)
        updated = engine.complete_record(rec.part_id, status="completed")
        assert updated.status == "completed"
        assert updated.completed_at is not None
        dt = datetime.fromisoformat(updated.completed_at)
        assert dt.tzinfo is not None

    def test_complete_with_quality_metrics(self, engine):
        rec = _make_record(engine)
        metrics = {"temp_deviation_c": 1.2, "layer_adhesion": "good"}
        updated = engine.complete_record(rec.part_id, status="completed", quality_metrics=metrics)
        assert updated.quality_metrics == metrics

    def test_fail_status(self, engine):
        rec = _make_record(engine)
        updated = engine.complete_record(rec.part_id, status="failed")
        assert updated.status == "failed"

    def test_cancel_status(self, engine):
        rec = _make_record(engine)
        updated = engine.complete_record(rec.part_id, status="cancelled")
        assert updated.status == "cancelled"

    def test_unknown_part_id_raises(self, engine):
        with pytest.raises(TraceabilityError, match="No record found"):
            engine.complete_record("nonexistent-id", status="completed")

    def test_invalid_status_raises(self, engine):
        rec = _make_record(engine)
        with pytest.raises(TraceabilityError, match="Invalid status"):
            engine.complete_record(rec.part_id, status="exploded")

    def test_printing_status_on_complete_raises(self, engine):
        rec = _make_record(engine)
        with pytest.raises(TraceabilityError, match="Cannot complete"):
            engine.complete_record(rec.part_id, status="printing")


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class TestGetRecord:
    """Tests for TraceabilityEngine.get_record()."""

    def test_returns_record(self, engine):
        rec = _make_record(engine)
        assert engine.get_record(rec.part_id) is rec

    def test_returns_none_for_unknown_id(self, engine):
        assert engine.get_record("nonexistent") is None


class TestGetRecordsByPrinter:
    """Tests for TraceabilityEngine.get_records_by_printer()."""

    def test_returns_matching_records(self, engine):
        _make_record(engine, printer_id="p1")
        _make_record(engine, printer_id="p1")
        _make_record(engine, printer_id="p2")
        results = engine.get_records_by_printer("p1")
        assert len(results) == 2
        assert all(r.printer_id == "p1" for r in results)

    def test_empty_for_unknown_printer(self, engine):
        _make_record(engine, printer_id="p1")
        assert engine.get_records_by_printer("p999") == []

    def test_respects_limit(self, engine):
        for _ in range(5):
            _make_record(engine, printer_id="p1")
        results = engine.get_records_by_printer("p1", limit=3)
        assert len(results) == 3

    def test_sorted_newest_first(self, engine):
        _make_record(engine, printer_id="p1")
        r2 = _make_record(engine, printer_id="p1")
        results = engine.get_records_by_printer("p1")
        # r2 created after r1, should appear first
        assert results[0].part_id == r2.part_id


class TestGetRecordsByMaterial:
    """Tests for TraceabilityEngine.get_records_by_material()."""

    def test_returns_matching_records(self, engine):
        _make_record(engine, material="PLA")
        _make_record(engine, material="ABS")
        _make_record(engine, material="PLA")
        results = engine.get_records_by_material("PLA")
        assert len(results) == 2

    def test_case_insensitive(self, engine):
        _make_record(engine, material="PLA")
        results = engine.get_records_by_material("pla")
        assert len(results) == 1

    def test_empty_for_unknown_material(self, engine):
        assert engine.get_records_by_material("UNOBTANIUM") == []

    def test_respects_limit(self, engine):
        for _ in range(5):
            _make_record(engine, material="PETG")
        results = engine.get_records_by_material("PETG", limit=2)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Compliance report
# ---------------------------------------------------------------------------


class TestExportComplianceReport:
    """Tests for TraceabilityEngine.export_compliance_report()."""

    def test_report_structure(self, engine):
        rec = _make_record(engine)
        engine.complete_record(rec.part_id, status="completed")
        report = engine.export_compliance_report([rec.part_id])
        assert "generated_at" in report
        assert report["total"] == 1
        assert report["verified"] == 1
        assert report["unverified"] == 0
        assert len(report["parts"]) == 1
        assert report["parts"][0]["chain_of_custody_verified"] is True

    def test_report_multiple_parts(self, engine):
        r1 = _make_record(engine)
        r2 = _make_record(engine)
        engine.complete_record(r1.part_id, status="completed")
        engine.complete_record(r2.part_id, status="failed")
        report = engine.export_compliance_report([r1.part_id, r2.part_id])
        assert report["total"] == 2

    def test_report_unknown_part_raises(self, engine):
        with pytest.raises(TraceabilityError, match="no record for part_id"):
            engine.export_compliance_report(["unknown-id"])

    def test_report_empty_list(self, engine):
        report = engine.export_compliance_report([])
        assert report["total"] == 0
        assert report["parts"] == []


# ---------------------------------------------------------------------------
# Chain of custody verification
# ---------------------------------------------------------------------------


class TestVerifyChainOfCustody:
    """Tests for TraceabilityEngine.verify_chain_of_custody()."""

    def test_valid_completed_record(self, engine):
        rec = _make_record(engine)
        engine.complete_record(rec.part_id, status="completed")
        assert engine.verify_chain_of_custody(rec.part_id) is True

    def test_valid_printing_record(self, engine):
        rec = _make_record(engine)
        # A printing record without completed_at is valid
        assert engine.verify_chain_of_custody(rec.part_id) is True

    def test_unknown_id_returns_false(self, engine):
        assert engine.verify_chain_of_custody("nonexistent") is False

    def test_completed_without_completed_at_is_invalid(self, engine):
        rec = _make_record(engine)
        # Force status to completed without setting completed_at
        rec.status = "completed"
        rec.completed_at = None
        assert engine.verify_chain_of_custody(rec.part_id) is False

    def test_completed_at_before_started_at_is_invalid(self, engine):
        rec = _make_record(engine)
        rec.status = "completed"
        # Set completed_at to before started_at
        start = datetime.fromisoformat(rec.started_at)
        rec.completed_at = (start - timedelta(hours=1)).isoformat()
        assert engine.verify_chain_of_custody(rec.part_id) is False

    def test_invalid_started_at_returns_false(self, engine):
        rec = _make_record(engine)
        rec.started_at = "not-a-date"
        assert engine.verify_chain_of_custody(rec.part_id) is False

    def test_empty_required_field_returns_false(self, engine):
        rec = _make_record(engine)
        rec.file_hash = ""
        assert engine.verify_chain_of_custody(rec.part_id) is False
