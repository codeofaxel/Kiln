"""Tests for kiln.failure_vocabulary — the boundary translator.

Three engines emit failure-type strings using three different
vocabularies, and three engines emit severity strings using three
different scales.  This module is the one source of truth that maps
all of them onto canonical forms.

These tests pin every value in every Python enum + every string in the
rerouter's safety set to its expected canonical form.  When a new
engine ships a new failure_type or severity value, the corresponding
enum-coverage test below fires immediately rather than waiting for a
silent vocabulary leak in production.
"""

from __future__ import annotations

import pytest

from kiln.failure_vocabulary import (
    ANTI_PATTERNS,
    CANONICAL_SEVERITIES,
    CLASSIFIER_TO_CANONICAL,
    MITIGATIONS,
    VALID_FAILURE_MODES,
    anti_pattern_for,
    mitigation_for,
    normalize_failure_type,
    normalize_severity,
    severity_at_least,
    to_canonical,
)


# ===================================================================
# normalize_failure_type — handles ALL three engine vocabularies
# ===================================================================


class TestNormalizeFailureType:
    """Every engine's failure_type strings resolve to a canonical mode."""

    def test_none_returns_none(self) -> None:
        assert normalize_failure_type(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_failure_type("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_failure_type("   ") is None

    def test_strips_whitespace(self) -> None:
        assert normalize_failure_type("  thermal_runaway  ") == "thermal_runaway"

    def test_unknown_value_returns_none(self) -> None:
        assert normalize_failure_type("not_a_real_failure") is None

    def test_canonical_passthrough(self) -> None:
        # Every value in VALID_FAILURE_MODES round-trips to itself.
        for mode in VALID_FAILURE_MODES:
            assert normalize_failure_type(mode) == mode, (
                f"canonical {mode!r} should round-trip"
            )

    @pytest.mark.parametrize(
        "engine_value,expected_canonical",
        [
            # kiln.failure_recovery.FailureType
            ("adhesion_loss", "adhesion"),
            ("nozzle_clog", "clog"),
            ("unknown", "other"),
            # kiln.print_recovery.FailureType
            ("adhesion_failure", "adhesion"),
            ("blob_detected", "spaghetti"),
            ("communication_loss", "other"),
            # kiln.recovery.FailureType
            ("bed_adhesion_failure", "adhesion"),
            ("first_layer_failure", "adhesion"),
            ("network_disconnect", "other"),
            ("printer_error", "mechanical"),
            ("software_crash", "other"),
            ("timeout", "other"),
            ("user_cancelled", "other"),
        ],
    )
    def test_engine_vocabularies_resolve(
        self,
        engine_value: str,
        expected_canonical: str,
    ) -> None:
        assert normalize_failure_type(engine_value) == expected_canonical

    def test_kiln_print_recovery_enum_coverage(self) -> None:
        """Every value in kiln.print_recovery.FailureType MUST normalize.

        This pin fires the moment a new failure type is added to the
        enum without a corresponding entry in CLASSIFIER_TO_CANONICAL —
        catches the "silent vocabulary leak" class of regression.
        """
        from kiln.print_recovery import FailureType

        for ft in FailureType:
            normalized = normalize_failure_type(ft.value)
            assert normalized is not None, (
                f"kiln.print_recovery.FailureType.{ft.name} (={ft.value!r}) "
                f"does not normalize.  Add an entry to "
                f"CLASSIFIER_TO_CANONICAL in failure_vocabulary.py."
            )

    def test_legacy_kiln_recovery_strings_still_normalize(self) -> None:
        """The deprecated kiln.recovery vocabulary still has CLASSIFIER_TO_CANONICAL
        entries — strings emitted by old code paths or saved data must
        still resolve to a canonical mode after the kiln.recovery
        module deletion (commit Option B).  Pin the values explicitly
        rather than reflecting the deleted enum.
        """
        legacy_values = (
            "bed_adhesion_failure",
            "first_layer_failure",
            "network_disconnect",
            "printer_error",
            "software_crash",
            "timeout",
            "user_cancelled",
        )
        for legacy in legacy_values:
            normalized = normalize_failure_type(legacy)
            assert normalized is not None, (
                f"legacy kiln.recovery value {legacy!r} no longer "
                f"normalizes — entries in CLASSIFIER_TO_CANONICAL "
                f"must remain so historical data survives the "
                f"module deletion."
            )

    def test_kiln_failure_recovery_enum_coverage(self) -> None:
        """Same pin for kiln.failure_recovery.FailureType."""
        from kiln.failure_recovery import FailureType

        for ft in FailureType:
            normalized = normalize_failure_type(ft.value)
            assert normalized is not None, (
                f"kiln.failure_recovery.FailureType.{ft.name} (={ft.value!r}) "
                f"does not normalize.  Add an entry to "
                f"CLASSIFIER_TO_CANONICAL in failure_vocabulary.py."
            )

    def test_rerouter_safety_set_all_normalize_to_adhesion_or_thermal(self) -> None:
        """Patent KILN-003 cl. 23: rerouter safety set covers adhesion + thermal.

        Every value in the rerouter's SAFETY_CRITICAL_FAILURE_TYPES set
        must normalize to either ``adhesion`` or ``thermal_runaway`` —
        those are the two safety-critical canonical modes.  This pin
        catches the kind of regression where a new safety string is
        added to the rerouter without adding it to the canonical
        vocabulary, which would let it accidentally auto-reroute.
        """
        # Import lazily so the test doesn't hard-fail when kiln-pro
        # isn't installed (free-tier CI).
        try:
            from kiln_pro.recovery.failure_rerouter import (
                SAFETY_CRITICAL_FAILURE_TYPES,
            )
        except ImportError:
            pytest.skip("kiln-pro not installed; rerouter safety set unavailable")

        expected_canonical = {"adhesion", "thermal_runaway"}
        for safety_value in SAFETY_CRITICAL_FAILURE_TYPES:
            normalized = normalize_failure_type(safety_value)
            assert normalized in expected_canonical, (
                f"rerouter safety value {safety_value!r} normalizes to "
                f"{normalized!r}, expected one of {expected_canonical}.  "
                f"Either add the value to CLASSIFIER_TO_CANONICAL with "
                f"the right target, or remove it from the rerouter set."
            )


# ===================================================================
# to_canonical — backward-compat wrapper (existing callers)
# ===================================================================


class TestToCanonical:
    """to_canonical preserves its existing contract."""

    def test_none_returns_none(self) -> None:
        assert to_canonical(None) is None

    def test_empty_returns_none(self) -> None:
        assert to_canonical("") is None

    def test_canonical_passes_through(self) -> None:
        assert to_canonical("adhesion") == "adhesion"

    def test_classifier_value_translates(self) -> None:
        assert to_canonical("adhesion_loss") == "adhesion"

    def test_unknown_returns_none(self) -> None:
        assert to_canonical("nope") is None


# ===================================================================
# normalize_severity — three engine scales onto one ladder
# ===================================================================


class TestNormalizeSeverity:
    """All severity engines map onto ok/info/low/medium/high/critical."""

    def test_none_returns_none(self) -> None:
        assert normalize_severity(None) is None

    def test_empty_returns_none(self) -> None:
        assert normalize_severity("") is None

    def test_unknown_returns_none(self) -> None:
        assert normalize_severity("urgent") is None

    def test_strips_and_lowercases(self) -> None:
        assert normalize_severity("  CRITICAL  ") == "critical"
        assert normalize_severity("WARNING") == "medium"

    @pytest.mark.parametrize(
        "engine_value,expected_canonical",
        [
            # kiln.print_health_monitor.HealthSeverity
            ("ok", "ok"),
            ("warning", "medium"),
            ("critical", "critical"),
            # kiln_pro.recovery.predictive RiskSignal
            ("info", "info"),
            ("amber", "medium"),
            ("red", "high"),
            ("clear", "ok"),
            # kiln.print_recovery.FailureReport.severity (free-form)
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
        ],
    )
    def test_engine_vocabularies_resolve(
        self,
        engine_value: str,
        expected_canonical: str,
    ) -> None:
        assert normalize_severity(engine_value) == expected_canonical

    def test_health_severity_enum_coverage(self) -> None:
        """Every kiln.print_health_monitor.HealthSeverity value normalizes."""
        from kiln.print_health_monitor import HealthSeverity

        for sev in HealthSeverity:
            normalized = normalize_severity(sev.value)
            assert normalized is not None, (
                f"HealthSeverity.{sev.name} (={sev.value!r}) does not "
                f"normalize.  Add to _SEVERITY_TO_CANONICAL."
            )


# ===================================================================
# severity_at_least — threshold check on the canonical ladder
# ===================================================================


class TestSeverityAtLeast:
    """Threshold check works regardless of source vocabulary."""

    def test_critical_at_least_high(self) -> None:
        assert severity_at_least("critical", "high") is True

    def test_low_not_at_least_high(self) -> None:
        assert severity_at_least("low", "high") is False

    def test_red_at_least_high(self) -> None:
        # "red" from predictive normalizes to "high"; threshold "high"
        # is satisfied (>=).
        assert severity_at_least("red", "high") is True

    def test_amber_at_least_medium(self) -> None:
        assert severity_at_least("amber", "medium") is True

    def test_warning_not_at_least_high(self) -> None:
        # "warning" -> "medium", threshold "high" -> not satisfied.
        assert severity_at_least("warning", "high") is False

    def test_unknown_value_returns_false(self) -> None:
        assert severity_at_least("blarg", "low") is False

    def test_unknown_threshold_raises(self) -> None:
        with pytest.raises(ValueError):
            severity_at_least("critical", "blarg")

    def test_none_value_returns_false(self) -> None:
        assert severity_at_least(None, "low") is False


# ===================================================================
# Existing helpers still work (mitigation_for, anti_pattern_for)
# ===================================================================


class TestMitigationsAndAntiPatterns:
    """Existing helpers are unchanged."""

    def test_mitigation_for_canonical(self) -> None:
        assert mitigation_for("adhesion") is not None
        assert "adhesion" in mitigation_for("adhesion").lower()

    def test_mitigation_for_classifier_value(self) -> None:
        # adhesion_loss should resolve to the same mitigation as adhesion.
        assert mitigation_for("adhesion_loss") == mitigation_for("adhesion")

    def test_anti_pattern_for_canonical(self) -> None:
        assert anti_pattern_for("warping") is not None

    def test_no_mitigation_for_hardware_failure(self) -> None:
        # Hardware failures (thermal_runaway, power_loss, etc.) have
        # no design mitigation by design.
        assert mitigation_for("thermal_runaway") is None
        assert mitigation_for("power_loss") is None
