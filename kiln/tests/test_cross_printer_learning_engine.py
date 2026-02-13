"""Tests for kiln.cross_printer_learning engine module.

Coverage areas:
- Input validation (all fields, boundary values, injection attempts)
- Rate limiting
- Outlier detection
- Material insights aggregation
- Printer insights aggregation
- Recommendation generation
- Network stats
- Thread safety
- Max outcomes eviction
- Module-level singleton
"""

from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

from kiln.cross_printer_learning import (
    CrossPrinterLearningEngine,
    LearningRateLimitError,
    LearningValidationError,
    MaterialInsight,
    PrinterModelInsight,
    PrintOutcome,
    get_learning_engine,
    _mean,
    _median,
    _std_dev,
    _validate_outcome,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_HASH = "a" * 64


def _make_outcome(**overrides: object) -> PrintOutcome:
    """Build a valid PrintOutcome with sensible defaults, overriding as needed."""
    defaults = {
        "printer_model": "Ender 3",
        "material": "PLA",
        "hotend_temp": 200.0,
        "bed_temp": 60.0,
        "success": True,
        "failure_mode": None,
        "print_time_s": 3600.0,
        "layer_count": 100,
        "file_hash": _VALID_HASH,
    }
    defaults.update(overrides)
    return PrintOutcome(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dataclass serialisation
# ---------------------------------------------------------------------------


class TestPrintOutcomeToDict:
    """PrintOutcome.to_dict() serialisation."""

    def test_round_trip(self) -> None:
        o = _make_outcome(failure_mode="warping")
        d = o.to_dict()
        assert d["printer_model"] == "Ender 3"
        assert d["material"] == "PLA"
        assert d["hotend_temp"] == 200.0
        assert d["bed_temp"] == 60.0
        assert d["success"] is True
        assert d["failure_mode"] == "warping"
        assert d["print_time_s"] == 3600.0
        assert d["layer_count"] == 100
        assert d["file_hash"] == _VALID_HASH
        assert d["is_outlier"] is False
        assert isinstance(d["recorded_at"], float)


class TestMaterialInsightToDict:
    """MaterialInsight.to_dict() serialisation."""

    def test_serialises_tuple_as_list(self) -> None:
        mi = MaterialInsight(
            material="PLA",
            recommended_hotend_temp_range=(195.0, 210.0),
            recommended_bed_temp_range=(55.0, 65.0),
            success_rate=0.95,
            sample_count=20,
            common_failures=[{"failure_mode": "stringing", "count": 2}],
        )
        d = mi.to_dict()
        assert d["recommended_hotend_temp_range"] == [195.0, 210.0]
        assert d["recommended_bed_temp_range"] == [55.0, 65.0]
        assert d["success_rate"] == 0.95


class TestPrinterModelInsightToDict:
    """PrinterModelInsight.to_dict() serialisation."""

    def test_serialises(self) -> None:
        pi = PrinterModelInsight(
            printer_model="Voron 2-4",
            best_materials=["PLA", "PETG"],
            worst_materials=["TPU"],
            common_failures=[],
            avg_success_rate=0.88,
            sample_count=50,
        )
        d = pi.to_dict()
        assert d["printer_model"] == "Voron 2-4"
        assert d["best_materials"] == ["PLA", "PETG"]
        assert d["avg_success_rate"] == 0.88


# ---------------------------------------------------------------------------
# Input validation — printer_model
# ---------------------------------------------------------------------------


class TestValidatePrinterModel:
    """Validation rules for PrintOutcome.printer_model."""

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="printer_model.*non-empty"):
            _validate_outcome(_make_outcome(printer_model=""))

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="printer_model.*non-empty"):
            _validate_outcome(_make_outcome(printer_model="   "))

    def test_too_long_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="max length"):
            _validate_outcome(_make_outcome(printer_model="A" * 101))

    def test_max_length_accepted(self) -> None:
        _validate_outcome(_make_outcome(printer_model="A" * 100))

    def test_special_chars_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="invalid characters"):
            _validate_outcome(_make_outcome(printer_model="Ender<script>3"))

    def test_sql_injection_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="invalid characters"):
            _validate_outcome(_make_outcome(printer_model="'; DROP TABLE --"))

    def test_unicode_injection_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="invalid characters"):
            _validate_outcome(_make_outcome(printer_model="Ender\u200b3"))

    def test_valid_with_spaces_hyphens_underscores(self) -> None:
        _validate_outcome(_make_outcome(printer_model="Prusa MK4-S_v2"))

    def test_non_string_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="printer_model.*non-empty"):
            _validate_outcome(_make_outcome(printer_model=123))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Input validation — material
# ---------------------------------------------------------------------------


class TestValidateMaterial:
    """Validation rules for PrintOutcome.material."""

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="material.*non-empty"):
            _validate_outcome(_make_outcome(material=""))

    def test_too_long_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="max length"):
            _validate_outcome(_make_outcome(material="M" * 51))

    def test_max_length_accepted(self) -> None:
        _validate_outcome(_make_outcome(material="M" * 50))

    def test_invalid_chars_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="invalid characters"):
            _validate_outcome(_make_outcome(material="PLA;DROP"))

    def test_underscores_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="invalid characters"):
            _validate_outcome(_make_outcome(material="PLA_Plus"))

    def test_valid_with_spaces_hyphens(self) -> None:
        _validate_outcome(_make_outcome(material="PLA-Plus Silk"))


# ---------------------------------------------------------------------------
# Input validation — temperatures
# ---------------------------------------------------------------------------


class TestValidateTemperatures:
    """Validation rules for hotend_temp and bed_temp."""

    def test_hotend_negative_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="hotend_temp.*0.*500"):
            _validate_outcome(_make_outcome(hotend_temp=-1))

    def test_hotend_over_500_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="hotend_temp.*0.*500"):
            _validate_outcome(_make_outcome(hotend_temp=501))

    def test_hotend_boundary_accepted(self) -> None:
        _validate_outcome(_make_outcome(hotend_temp=0))
        _validate_outcome(_make_outcome(hotend_temp=500))

    def test_bed_negative_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="bed_temp.*0.*200"):
            _validate_outcome(_make_outcome(bed_temp=-1))

    def test_bed_over_200_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="bed_temp.*0.*200"):
            _validate_outcome(_make_outcome(bed_temp=201))

    def test_bed_boundary_accepted(self) -> None:
        _validate_outcome(_make_outcome(bed_temp=0))
        _validate_outcome(_make_outcome(bed_temp=200))

    def test_non_numeric_hotend_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="hotend_temp.*number"):
            _validate_outcome(_make_outcome(hotend_temp="hot"))  # type: ignore[arg-type]

    def test_non_numeric_bed_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="bed_temp.*number"):
            _validate_outcome(_make_outcome(bed_temp="warm"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Input validation — success
# ---------------------------------------------------------------------------


class TestValidateSuccess:
    """Validation rules for PrintOutcome.success."""

    def test_non_bool_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="success.*boolean"):
            _validate_outcome(_make_outcome(success=1))  # type: ignore[arg-type]

    def test_true_accepted(self) -> None:
        _validate_outcome(_make_outcome(success=True))

    def test_false_accepted(self) -> None:
        _validate_outcome(_make_outcome(success=False))


# ---------------------------------------------------------------------------
# Input validation — failure_mode
# ---------------------------------------------------------------------------


class TestValidateFailureMode:
    """Validation rules for PrintOutcome.failure_mode."""

    def test_none_accepted(self) -> None:
        _validate_outcome(_make_outcome(failure_mode=None))

    def test_valid_string_accepted(self) -> None:
        _validate_outcome(_make_outcome(failure_mode="Bed adhesion failure"))

    def test_too_long_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="max length"):
            _validate_outcome(_make_outcome(failure_mode="F" * 201))

    def test_max_length_accepted(self) -> None:
        _validate_outcome(_make_outcome(failure_mode="F" * 200))

    def test_control_chars_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="control characters"):
            _validate_outcome(_make_outcome(failure_mode="fail\x00ure"))

    def test_newline_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="control characters"):
            _validate_outcome(_make_outcome(failure_mode="line1\nline2"))

    def test_non_string_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="failure_mode.*string"):
            _validate_outcome(_make_outcome(failure_mode=42))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Input validation — print_time_s
# ---------------------------------------------------------------------------


class TestValidatePrintTime:
    """Validation rules for PrintOutcome.print_time_s."""

    def test_negative_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="print_time_s.*>= 0"):
            _validate_outcome(_make_outcome(print_time_s=-1))

    def test_zero_accepted(self) -> None:
        _validate_outcome(_make_outcome(print_time_s=0))

    def test_over_7_days_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="7 days"):
            _validate_outcome(_make_outcome(print_time_s=604801))

    def test_exactly_7_days_accepted(self) -> None:
        _validate_outcome(_make_outcome(print_time_s=604800))

    def test_non_numeric_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="print_time_s.*number"):
            _validate_outcome(_make_outcome(print_time_s="long"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Input validation — layer_count
# ---------------------------------------------------------------------------


class TestValidateLayerCount:
    """Validation rules for PrintOutcome.layer_count."""

    def test_negative_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="layer_count.*>= 0"):
            _validate_outcome(_make_outcome(layer_count=-1))

    def test_zero_accepted(self) -> None:
        _validate_outcome(_make_outcome(layer_count=0))

    def test_over_max_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="layer_count.*100000"):
            _validate_outcome(_make_outcome(layer_count=100001))

    def test_max_accepted(self) -> None:
        _validate_outcome(_make_outcome(layer_count=100000))

    def test_float_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="layer_count.*integer"):
            _validate_outcome(_make_outcome(layer_count=5.5))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Input validation — file_hash
# ---------------------------------------------------------------------------


class TestValidateFileHash:
    """Validation rules for PrintOutcome.file_hash."""

    def test_valid_sha256_accepted(self) -> None:
        _validate_outcome(_make_outcome(file_hash="ab" * 32))

    def test_too_short_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="SHA-256"):
            _validate_outcome(_make_outcome(file_hash="ab" * 31))

    def test_too_long_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="SHA-256"):
            _validate_outcome(_make_outcome(file_hash="ab" * 33))

    def test_non_hex_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="SHA-256"):
            _validate_outcome(_make_outcome(file_hash="g" * 64))

    def test_empty_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="SHA-256"):
            _validate_outcome(_make_outcome(file_hash=""))

    def test_non_string_rejected(self) -> None:
        with pytest.raises(LearningValidationError, match="file_hash.*string"):
            _validate_outcome(_make_outcome(file_hash=12345))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Rate limit enforcement: max 100 outcomes per minute per model."""

    def test_within_limit_accepted(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=200)
        for i in range(100):
            engine.record_outcome(_make_outcome(file_hash=f"{i:064x}"))

    def test_exceeding_limit_raises(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=200)
        for i in range(100):
            engine.record_outcome(_make_outcome(file_hash=f"{i:064x}"))
        with pytest.raises(LearningRateLimitError, match="Rate limit"):
            engine.record_outcome(_make_outcome(file_hash=f"{100:064x}"))

    def test_different_models_independent(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=300)
        for i in range(100):
            engine.record_outcome(
                _make_outcome(printer_model="Ender 3", file_hash=f"{i:064x}")
            )
        # Different model should still work
        engine.record_outcome(
            _make_outcome(printer_model="Voron 24", file_hash=f"{200:064x}")
        )

    def test_limit_resets_after_window(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=200)

        # Fill rate bucket with old timestamps
        with engine._lock:
            old_ts = time.time() - 120  # 2 minutes ago
            engine._rate_buckets["Ender 3"] = [old_ts] * 100

        # Should succeed because old timestamps are outside the window
        engine.record_outcome(_make_outcome(file_hash=f"{999:064x}"))


# ---------------------------------------------------------------------------
# Outlier detection
# ---------------------------------------------------------------------------


class TestOutlierDetection:
    """Outlier detection: temps 3+ stddevs from mean."""

    def test_outlier_flagged(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)

        # Record 10 consistent outcomes at 200/60
        for i in range(10):
            engine.record_outcome(
                _make_outcome(hotend_temp=200, bed_temp=60, file_hash=f"{i:064x}")
            )

        # Record an extreme outlier
        outlier = _make_outcome(hotend_temp=450, bed_temp=60, file_hash=f"{99:064x}")
        engine.record_outcome(outlier)
        assert outlier.is_outlier is True

    def test_normal_not_flagged(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(10):
            engine.record_outcome(
                _make_outcome(hotend_temp=200, bed_temp=60, file_hash=f"{i:064x}")
            )
        normal = _make_outcome(hotend_temp=202, bed_temp=61, file_hash=f"{99:064x}")
        engine.record_outcome(normal)
        assert normal.is_outlier is False

    def test_not_enough_data_no_outlier(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(3):
            engine.record_outcome(
                _make_outcome(hotend_temp=200, bed_temp=60, file_hash=f"{i:064x}")
            )
        extreme = _make_outcome(hotend_temp=450, bed_temp=60, file_hash=f"{99:064x}")
        engine.record_outcome(extreme)
        assert extreme.is_outlier is False

    def test_bed_temp_outlier_flagged(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(10):
            engine.record_outcome(
                _make_outcome(hotend_temp=200, bed_temp=60, file_hash=f"{i:064x}")
            )
        outlier = _make_outcome(hotend_temp=200, bed_temp=190, file_hash=f"{99:064x}")
        engine.record_outcome(outlier)
        assert outlier.is_outlier is True

    def test_outliers_excluded_from_insights(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(10):
            engine.record_outcome(
                _make_outcome(hotend_temp=200, bed_temp=60, file_hash=f"{i:064x}")
            )
        outlier = _make_outcome(hotend_temp=450, bed_temp=60, file_hash=f"{99:064x}")
        engine.record_outcome(outlier)

        insight = engine.get_material_insights("PLA")
        assert insight.sample_count == 10
        assert insight.recommended_hotend_temp_range[1] <= 200


# ---------------------------------------------------------------------------
# Material insights
# ---------------------------------------------------------------------------


class TestMaterialInsights:
    """MaterialInsight aggregation across printers."""

    def test_empty_returns_zero_insight(self) -> None:
        engine = CrossPrinterLearningEngine()
        insight = engine.get_material_insights("PLA")
        assert insight.sample_count == 0
        assert insight.success_rate == 0.0
        assert insight.recommended_hotend_temp_range == (0.0, 0.0)
        assert insight.common_failures == []

    def test_aggregates_across_printers(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        engine.record_outcome(
            _make_outcome(printer_model="Ender 3", hotend_temp=200, bed_temp=60, file_hash=f"{0:064x}")
        )
        engine.record_outcome(
            _make_outcome(printer_model="Voron 24", hotend_temp=210, bed_temp=65, file_hash=f"{1:064x}")
        )

        insight = engine.get_material_insights("PLA")
        assert insight.sample_count == 2
        assert insight.recommended_hotend_temp_range == (200, 210)
        assert insight.recommended_bed_temp_range == (60, 65)

    def test_success_rate_computed(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(8):
            engine.record_outcome(
                _make_outcome(success=True, file_hash=f"{i:064x}")
            )
        for i in range(2):
            engine.record_outcome(
                _make_outcome(
                    success=False,
                    failure_mode="warping",
                    file_hash=f"{i + 100:064x}",
                )
            )

        insight = engine.get_material_insights("PLA")
        assert insight.success_rate == 0.8

    def test_common_failures_sorted(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(3):
            engine.record_outcome(
                _make_outcome(success=False, failure_mode="warping", file_hash=f"{i:064x}")
            )
        engine.record_outcome(
            _make_outcome(success=False, failure_mode="stringing", file_hash=f"{99:064x}")
        )

        insight = engine.get_material_insights("PLA")
        assert len(insight.common_failures) == 2
        assert insight.common_failures[0]["failure_mode"] == "warping"
        assert insight.common_failures[0]["count"] == 3
        assert insight.common_failures[1]["failure_mode"] == "stringing"
        assert insight.common_failures[1]["count"] == 1

    def test_empty_material_raises(self) -> None:
        engine = CrossPrinterLearningEngine()
        with pytest.raises(LearningValidationError, match="material.*non-empty"):
            engine.get_material_insights("")

    def test_temp_range_from_successes_only(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        engine.record_outcome(
            _make_outcome(success=True, hotend_temp=200, bed_temp=60, file_hash=f"{0:064x}")
        )
        engine.record_outcome(
            _make_outcome(success=False, hotend_temp=250, bed_temp=90, failure_mode="burn", file_hash=f"{1:064x}")
        )
        insight = engine.get_material_insights("PLA")
        assert insight.recommended_hotend_temp_range == (200, 200)
        assert insight.recommended_bed_temp_range == (60, 60)


# ---------------------------------------------------------------------------
# Printer model insights
# ---------------------------------------------------------------------------


class TestPrinterInsights:
    """PrinterModelInsight aggregation."""

    def test_empty_returns_zero_insight(self) -> None:
        engine = CrossPrinterLearningEngine()
        insight = engine.get_printer_insights("Ender 3")
        assert insight.sample_count == 0
        assert insight.avg_success_rate == 0.0
        assert insight.best_materials == []

    def test_best_and_worst_materials(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(5):
            engine.record_outcome(
                _make_outcome(material="PLA", success=True, file_hash=f"{i:064x}")
            )
        for i in range(4):
            engine.record_outcome(
                _make_outcome(material="ABS", success=False, failure_mode="warping", file_hash=f"{i + 10:064x}")
            )
        engine.record_outcome(
            _make_outcome(material="ABS", success=True, file_hash=f"{20:064x}")
        )

        insight = engine.get_printer_insights("Ender 3")
        assert "PLA" in insight.best_materials
        assert "ABS" in insight.worst_materials

    def test_common_failures(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(3):
            engine.record_outcome(
                _make_outcome(success=False, failure_mode="layer shift", file_hash=f"{i:064x}")
            )
        insight = engine.get_printer_insights("Ender 3")
        assert insight.common_failures[0]["failure_mode"] == "layer shift"
        assert insight.common_failures[0]["count"] == 3

    def test_empty_model_raises(self) -> None:
        engine = CrossPrinterLearningEngine()
        with pytest.raises(LearningValidationError, match="printer_model.*non-empty"):
            engine.get_printer_insights("")


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


class TestRecommendation:
    """Recommendation generation from aggregated data."""

    def test_no_data_returns_none_confidence(self) -> None:
        engine = CrossPrinterLearningEngine()
        rec = engine.get_recommendation("Ender 3", "PLA")
        assert rec["confidence"] == "none"
        assert rec["recommended_hotend_temp"] is None
        assert rec["sample_count"] == 0

    def test_specific_data_high_confidence(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(15):
            engine.record_outcome(
                _make_outcome(hotend_temp=205, bed_temp=62, file_hash=f"{i:064x}")
            )
        rec = engine.get_recommendation("Ender 3", "PLA")
        assert rec["confidence"] == "high"
        assert rec["recommended_hotend_temp"] == 205.0
        assert rec["recommended_bed_temp"] == 62.0
        assert rec["sample_count"] == 15

    def test_specific_data_medium_confidence(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(5):
            engine.record_outcome(
                _make_outcome(hotend_temp=205, bed_temp=62, file_hash=f"{i:064x}")
            )
        rec = engine.get_recommendation("Ender 3", "PLA")
        assert rec["confidence"] == "medium"

    def test_fallback_to_all_printers(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(5):
            engine.record_outcome(
                _make_outcome(
                    printer_model="Voron 24",
                    hotend_temp=210,
                    bed_temp=65,
                    file_hash=f"{i:064x}",
                )
            )
        rec = engine.get_recommendation("Ender 3", "PLA")
        assert rec["confidence"] == "low"
        assert rec["recommended_hotend_temp"] == 210.0

    def test_success_rate_included(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(8):
            engine.record_outcome(_make_outcome(success=True, file_hash=f"{i:064x}"))
        for i in range(2):
            engine.record_outcome(
                _make_outcome(success=False, failure_mode="fail", file_hash=f"{i + 100:064x}")
            )
        rec = engine.get_recommendation("Ender 3", "PLA")
        assert rec["success_rate"] == 0.8

    def test_empty_printer_model_raises(self) -> None:
        engine = CrossPrinterLearningEngine()
        with pytest.raises(LearningValidationError):
            engine.get_recommendation("", "PLA")

    def test_empty_material_raises(self) -> None:
        engine = CrossPrinterLearningEngine()
        with pytest.raises(LearningValidationError):
            engine.get_recommendation("Ender 3", "")

    def test_uses_median(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        temps = [195, 200, 200, 205, 210]
        for i, t in enumerate(temps):
            engine.record_outcome(
                _make_outcome(hotend_temp=t, bed_temp=60, file_hash=f"{i:064x}")
            )
        rec = engine.get_recommendation("Ender 3", "PLA")
        assert rec["recommended_hotend_temp"] == 200.0


# ---------------------------------------------------------------------------
# Network stats
# ---------------------------------------------------------------------------


class TestNetworkStats:
    """get_network_stats() aggregation."""

    def test_empty_network(self) -> None:
        engine = CrossPrinterLearningEngine()
        stats = engine.get_network_stats()
        assert stats["total_outcomes"] == 0
        assert stats["unique_printers"] == 0
        assert stats["unique_materials"] == 0
        assert stats["overall_success_rate"] == 0.0

    def test_populated_network(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        engine.record_outcome(
            _make_outcome(printer_model="Ender 3", material="PLA", success=True, file_hash=f"{0:064x}")
        )
        engine.record_outcome(
            _make_outcome(printer_model="Voron 24", material="ABS", success=False, failure_mode="warp", file_hash=f"{1:064x}")
        )
        engine.record_outcome(
            _make_outcome(printer_model="Ender 3", material="ABS", success=True, file_hash=f"{2:064x}")
        )

        stats = engine.get_network_stats()
        assert stats["total_outcomes"] == 3
        assert stats["unique_printers"] == 2
        assert stats["unique_materials"] == 2
        assert stats["overall_success_rate"] == round(2 / 3, 4)

    def test_includes_outliers_in_total(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=100)
        for i in range(10):
            engine.record_outcome(
                _make_outcome(hotend_temp=200, bed_temp=60, file_hash=f"{i:064x}")
            )
        outlier = _make_outcome(hotend_temp=450, bed_temp=60, file_hash=f"{99:064x}")
        engine.record_outcome(outlier)

        stats = engine.get_network_stats()
        assert stats["total_outcomes"] == 11


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Concurrent access does not corrupt state."""

    def test_concurrent_recording(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=5000)
        errors: list[Exception] = []

        def record_batch(thread_id: int, start: int) -> None:
            try:
                for i in range(50):
                    engine.record_outcome(
                        _make_outcome(
                            # Use per-thread printer model to stay under rate limit
                            printer_model=f"Printer {thread_id}",
                            file_hash=f"{start + i:064x}",
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=record_batch, args=(i, i * 50))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        stats = engine.get_network_stats()
        assert stats["total_outcomes"] == 500

    def test_concurrent_read_write(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=5000)
        errors: list[Exception] = []
        stop = threading.Event()

        def writer() -> None:
            try:
                for i in range(200):
                    # Spread across printer models to stay under rate limit
                    engine.record_outcome(
                        _make_outcome(
                            printer_model=f"Printer {i % 3}",
                            file_hash=f"{i:064x}",
                        )
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                stop.set()

        def reader() -> None:
            try:
                while not stop.is_set():
                    engine.get_material_insights("PLA")
                    engine.get_network_stats()
            except Exception as exc:
                errors.append(exc)

        writer_t = threading.Thread(target=writer)
        reader_t = threading.Thread(target=reader)
        writer_t.start()
        reader_t.start()
        writer_t.join()
        reader_t.join()

        assert not errors


# ---------------------------------------------------------------------------
# Max outcomes eviction
# ---------------------------------------------------------------------------


class TestMaxOutcomesEviction:
    """Bounded storage with FIFO eviction."""

    def test_evicts_oldest_when_full(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=5)
        for i in range(7):
            engine.record_outcome(
                _make_outcome(file_hash=f"{i:064x}")
            )
        stats = engine.get_network_stats()
        assert stats["total_outcomes"] == 5

    def test_oldest_evicted_first(self) -> None:
        engine = CrossPrinterLearningEngine(max_outcomes=3)
        engine.record_outcome(_make_outcome(printer_model="First", file_hash=f"{0:064x}"))
        engine.record_outcome(_make_outcome(printer_model="Second", file_hash=f"{1:064x}"))
        engine.record_outcome(_make_outcome(printer_model="Third", file_hash=f"{2:064x}"))
        engine.record_outcome(_make_outcome(printer_model="Fourth", file_hash=f"{3:064x}"))

        with engine._lock:
            models = [o.printer_model for o in engine._outcomes]
        assert "First" not in models
        assert "Fourth" in models

    def test_env_var_configures_max(self) -> None:
        with mock.patch.dict("os.environ", {"KILN_LEARNING_MAX_OUTCOMES": "42"}):
            engine = CrossPrinterLearningEngine()
            assert engine._max_outcomes == 42

    def test_invalid_env_var_uses_default(self) -> None:
        with mock.patch.dict("os.environ", {"KILN_LEARNING_MAX_OUTCOMES": "not_a_number"}):
            engine = CrossPrinterLearningEngine()
            assert engine._max_outcomes == 10000

    def test_explicit_kwarg_overrides_env(self) -> None:
        with mock.patch.dict("os.environ", {"KILN_LEARNING_MAX_OUTCOMES": "42"}):
            engine = CrossPrinterLearningEngine(max_outcomes=99)
            assert engine._max_outcomes == 99


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Module-level get_learning_engine() singleton."""

    def test_returns_same_instance(self) -> None:
        import kiln.cross_printer_learning as mod

        with mod._engine_lock:
            mod._engine = None

        a = get_learning_engine()
        b = get_learning_engine()
        assert a is b

        with mod._engine_lock:
            mod._engine = None


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


class TestStatisticsHelpers:
    """_mean, _std_dev, _median edge cases."""

    def test_mean_empty(self) -> None:
        assert _mean([]) == 0.0

    def test_mean_single(self) -> None:
        assert _mean([5.0]) == 5.0

    def test_mean_normal(self) -> None:
        assert _mean([1.0, 2.0, 3.0]) == 2.0

    def test_std_dev_empty(self) -> None:
        assert _std_dev([]) == 0.0

    def test_std_dev_single(self) -> None:
        assert _std_dev([5.0]) == 0.0

    def test_std_dev_uniform(self) -> None:
        assert _std_dev([5.0, 5.0, 5.0]) == 0.0

    def test_std_dev_known(self) -> None:
        assert abs(_std_dev([2, 4, 4, 4, 5, 5, 7, 9]) - 2.0) < 0.001

    def test_median_empty(self) -> None:
        assert _median([]) == 0.0

    def test_median_odd(self) -> None:
        assert _median([3.0, 1.0, 2.0]) == 2.0

    def test_median_even(self) -> None:
        assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5
