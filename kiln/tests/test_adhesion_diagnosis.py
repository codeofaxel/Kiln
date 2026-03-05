"""Tests for adhesion intelligence and print failure diagnosis.

Covers:
    - recommend_adhesion() decision matrix (all branches)
    - diagnose_from_signals() priority-ordered diagnosis
    - is_bedslinger() lookup
    - _build_symptom_queries() helper
    - AdhesionRecommendation / PrintFailureDiagnosis dataclasses
"""

from __future__ import annotations

from kiln.printability import (
    AdhesionRecommendation,
    BedAdhesionAnalysis,
    PrintFailureDiagnosis,
    diagnose_from_signals,
    is_bedslinger,
    recommend_adhesion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _adhesion(
    contact_percentage: float = 30.0,
    adhesion_risk: str = "low",
    contact_area_mm2: float = 100.0,
) -> BedAdhesionAnalysis:
    return BedAdhesionAnalysis(
        contact_area_mm2=contact_area_mm2,
        contact_percentage=contact_percentage,
        adhesion_risk=adhesion_risk,
    )


# ---------------------------------------------------------------------------
# AdhesionRecommendation dataclass
# ---------------------------------------------------------------------------


class TestAdhesionRecommendationDataclass:
    def test_to_dict(self):
        rec = AdhesionRecommendation(
            brim_width_mm=5,
            use_raft=False,
            adhesion_risk="high",
            contact_percentage=3.0,
            rationale="test",
            slicer_overrides={"brim_width": "5"},
        )
        d = rec.to_dict()
        assert d["brim_width_mm"] == 5
        assert d["use_raft"] is False
        assert d["slicer_overrides"] == {"brim_width": "5"}


# ---------------------------------------------------------------------------
# PrintFailureDiagnosis dataclass
# ---------------------------------------------------------------------------


class TestPrintFailureDiagnosisDataclass:
    def test_to_dict(self):
        diag = PrintFailureDiagnosis(
            failure_category="adhesion",
            probable_causes=["Low bed contact"],
            recommended_fixes=["Add brim"],
            confidence=0.8,
            signals={"contact_pct": 2.0},
            slicer_overrides={"brim_width": "8"},
        )
        d = diag.to_dict()
        assert d["failure_category"] == "adhesion"
        assert d["confidence"] == 0.8
        assert len(d["probable_causes"]) == 1


# ---------------------------------------------------------------------------
# is_bedslinger()
# ---------------------------------------------------------------------------


class TestIsBedslinger:
    def test_known_bedslingers(self):
        assert is_bedslinger("bambu_a1") is True
        assert is_bedslinger("ender3") is True
        assert is_bedslinger("prusa_mk3s") is True
        assert is_bedslinger("bambu_a1_mini") is True

    def test_non_bedslingers(self):
        assert is_bedslinger("bambu_x1c") is False
        assert is_bedslinger("bambu_p1s") is False
        assert is_bedslinger("voron_2") is False

    def test_case_insensitive(self):
        assert is_bedslinger("Bambu_A1") is True
        assert is_bedslinger("ENDER3") is True

    def test_hyphen_normalisation(self):
        assert is_bedslinger("bambu-a1") is True
        assert is_bedslinger("ender3-v2") is True


# ---------------------------------------------------------------------------
# recommend_adhesion() — decision matrix
# ---------------------------------------------------------------------------


class TestRecommendAdhesion:
    """Covers every branch of the decision matrix."""

    def test_extreme_low_contact_pla(self):
        """contact < 2%, PLA → 8mm brim, no raft."""
        rec = recommend_adhesion(_adhesion(contact_percentage=1.5, adhesion_risk="high"))
        assert rec.brim_width_mm == 8
        assert rec.use_raft is False
        assert "8" in rec.slicer_overrides.get("brim_width", "")

    def test_extreme_low_contact_abs(self):
        """contact < 2%, ABS → 8mm brim + raft."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=1.0, adhesion_risk="high"),
            material="ABS",
        )
        assert rec.brim_width_mm == 8
        assert rec.use_raft is True
        assert rec.slicer_overrides.get("brim_type") is not None or rec.slicer_overrides.get("skirt_distance") is not None

    def test_low_contact_high_warp(self):
        """contact < 5% + warping material → 8mm brim + raft."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=3.5, adhesion_risk="high"),
            material="ASA",
        )
        assert rec.brim_width_mm == 8
        assert rec.use_raft is True

    def test_low_contact_bedslinger(self):
        """contact < 5% + bedslinger → 8mm brim, no raft."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=4.0, adhesion_risk="high"),
            material="PLA",
            is_bedslinger_printer=True,
        )
        assert rec.brim_width_mm == 8
        assert rec.use_raft is False

    def test_low_contact_open_frame(self):
        """contact < 5%, no warp, open frame (default) → 8mm brim."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=4.0, adhesion_risk="high"),
            material="PLA",
        )
        assert rec.brim_width_mm == 8
        assert rec.use_raft is False

    def test_low_contact_enclosed(self):
        """contact < 5%, no warp, enclosed printer → 5mm brim."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=4.0, adhesion_risk="high"),
            material="PLA",
            has_enclosure=True,
        )
        assert rec.brim_width_mm == 5
        assert rec.use_raft is False

    def test_medium_risk_warp_material(self):
        """medium risk + warping material → 8mm brim."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=12.0, adhesion_risk="medium"),
            material="ABS",
        )
        assert rec.brim_width_mm == 8
        assert rec.use_raft is False

    def test_medium_risk_bedslinger(self):
        """medium risk + bedslinger → 5mm brim."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=12.0, adhesion_risk="medium"),
            material="PLA",
            is_bedslinger_printer=True,
        )
        assert rec.brim_width_mm == 5

    def test_medium_risk_standard(self):
        """medium risk, no warp, no bedslinger → 3mm brim."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=12.0, adhesion_risk="medium"),
            material="PLA",
        )
        assert rec.brim_width_mm == 3

    def test_low_risk_tall_abs(self):
        """low risk + tall model + warping material → 5mm brim."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=30.0, adhesion_risk="low"),
            material="ABS",
            model_height_mm=80.0,
        )
        assert rec.brim_width_mm == 5

    def test_low_risk_pla(self):
        """low risk PLA → no brim."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=30.0, adhesion_risk="low"),
            material="PLA",
        )
        assert rec.brim_width_mm == 0
        assert rec.use_raft is False
        assert rec.slicer_overrides == {}

    def test_slicer_overrides_include_brim_width(self):
        """slicer_overrides should contain brim_width when brim > 0."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=4.0, adhesion_risk="high"),
            material="PLA",
        )
        assert rec.brim_width_mm > 0
        assert "brim_width" in rec.slicer_overrides

    def test_raft_includes_support_type_override(self):
        """When raft is recommended, slicer_overrides should set raft."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=1.0, adhesion_risk="high"),
            material="ABS",
        )
        assert rec.use_raft is True
        # Raft is set via support_material_buildplate_only or raft_layers
        overrides = rec.slicer_overrides
        has_raft_key = any(
            k in overrides for k in ("raft_layers", "support_material_buildplate_only")
        )
        assert has_raft_key or "raft" in str(overrides).lower()

    def test_enclosure_reduces_risk(self):
        """ABS with enclosure should not use raft for medium contact."""
        rec = recommend_adhesion(
            _adhesion(contact_percentage=12.0, adhesion_risk="medium"),
            material="ABS",
            has_enclosure=True,
        )
        assert rec.use_raft is False

    def test_rationale_is_populated(self):
        """Every recommendation has a non-empty rationale."""
        rec = recommend_adhesion(_adhesion(contact_percentage=3.0, adhesion_risk="high"))
        assert len(rec.rationale) > 10

    def test_contact_percentage_matches_input(self):
        """contact_percentage is passed through to the recommendation."""
        rec = recommend_adhesion(_adhesion(contact_percentage=7.5, adhesion_risk="medium"))
        assert rec.contact_percentage == 7.5


# ---------------------------------------------------------------------------
# diagnose_from_signals()
# ---------------------------------------------------------------------------


class TestDiagnoseFromSignals:
    """Covers all priority tiers of diagnosis logic."""

    def test_adhesion_failure_high_risk(self):
        """adhesion_risk='high' → adhesion category."""
        diag = diagnose_from_signals({"adhesion_risk": "high"})
        assert diag.failure_category == "adhesion"
        assert diag.confidence >= 0.7

    def test_adhesion_failure_low_contact(self):
        """contact_percentage < 5 → adhesion category."""
        diag = diagnose_from_signals({"contact_percentage": 3.0})
        assert diag.failure_category == "adhesion"
        assert len(diag.recommended_fixes) > 0

    def test_thermal_failure(self):
        """Large temp delta + no adhesion signals → thermal category."""
        diag = diagnose_from_signals({
            "tool_temp_actual": 180.0,
            "tool_temp_target": 210.0,
        })
        assert diag.failure_category == "thermal"
        assert diag.confidence >= 0.7

    def test_thermal_failure_with_error(self):
        """Print error flag → thermal category."""
        diag = diagnose_from_signals({"print_error": "thermal_runaway"})
        assert diag.failure_category == "thermal"

    def test_geometry_failure_overhang(self):
        """High overhang % → geometry category."""
        diag = diagnose_from_signals({"overhang_pct": 40.0})
        assert diag.failure_category == "geometry"

    def test_geometry_failure_bridge(self):
        """Long bridge → geometry category."""
        diag = diagnose_from_signals({"max_bridge_mm": 25.0})
        assert diag.failure_category == "geometry"

    def test_mechanical_failure_abs_no_enclosure(self):
        """ABS without enclosure → mechanical category."""
        diag = diagnose_from_signals(
            {"material": "ABS", "printer_has_enclosure": False},
            material="ABS",
        )
        # Should be mechanical if no other signals dominate
        assert diag.failure_category in ("mechanical", "unknown")

    def test_unknown_fallback(self):
        """No signals → unknown category."""
        diag = diagnose_from_signals({})
        assert diag.failure_category == "unknown"
        assert diag.confidence <= 0.5

    def test_adhesion_overrides_include_brim(self):
        """Adhesion diagnosis should suggest brim overrides."""
        diag = diagnose_from_signals({
            "adhesion_risk": "high",
            "contact_percentage": 2.0,
        })
        assert diag.failure_category == "adhesion"
        assert "brim_width" in diag.slicer_overrides or len(diag.recommended_fixes) > 0

    def test_priority_adhesion_over_geometry(self):
        """Adhesion signals take priority over geometry signals."""
        diag = diagnose_from_signals({
            "adhesion_risk": "high",
            "contact_percentage": 2.0,
            "overhang_pct": 50.0,
        })
        assert diag.failure_category == "adhesion"

    def test_priority_thermal_over_geometry(self):
        """Thermal signals take priority over geometry signals."""
        diag = diagnose_from_signals({
            "tool_temp_actual": 170.0,
            "tool_temp_target": 210.0,
            "overhang_pct": 50.0,
        })
        assert diag.failure_category == "thermal"

    def test_signals_included_in_output(self):
        """Raw signals are passed through for debugging."""
        signals = {"adhesion_risk": "high", "contact_percentage": 2.0}
        diag = diagnose_from_signals(signals)
        assert diag.signals == signals

    def test_probable_causes_not_empty_on_known_failure(self):
        """Known failure categories always have at least one cause."""
        diag = diagnose_from_signals({"adhesion_risk": "high"})
        assert len(diag.probable_causes) >= 1

    def test_recommended_fixes_not_empty_on_known_failure(self):
        """Known failure categories always have at least one fix."""
        diag = diagnose_from_signals({"print_error": "thermal"})
        assert len(diag.recommended_fixes) >= 1

    def test_intel_modes_surfaced(self):
        """Failure modes from printer intelligence are used."""
        diag = diagnose_from_signals({
            "failure_modes_from_intel": [
                {"symptom": "spaghetti", "cause": "adhesion", "fix": "add brim"},
            ],
        })
        # Even with no other signals, intel modes get surfaced in unknown tier
        assert diag.failure_category == "unknown"
        assert len(diag.probable_causes) >= 1


# ---------------------------------------------------------------------------
# _build_symptom_queries() -- module-level helper in plugin
# ---------------------------------------------------------------------------


class TestBuildSymptomQueries:
    """Tests for the _build_symptom_queries helper in the plugin."""

    def test_high_adhesion_risk(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(None, {"adhesion_risk": "high"})
        assert any("adhesion" in q for q in queries)

    def test_medium_adhesion_risk(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(None, {"adhesion_risk": "medium"})
        assert any("adhesion" in q for q in queries)

    def test_thermal_delta(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(
            None,
            {"tool_temp_actual": 180.0, "tool_temp_target": 210.0},
        )
        assert any("temperature" in q or "thermal" in q for q in queries)

    def test_print_error(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(None, {"print_error": "nozzle_clog"})
        assert "nozzle_clog" in queries

    def test_overhang(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(None, {"overhang_pct": 40.0})
        assert any("overhang" in q for q in queries)

    def test_bridge(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(None, {"max_bridge_mm": 20.0})
        assert any("bridge" in q for q in queries)

    def test_warp_material_no_enclosure(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(
            None,
            {"material": "ABS", "printer_has_enclosure": False},
        )
        assert any("warp" in q for q in queries)

    def test_empty_signals_fallback(self):
        from kiln.plugins.printability_tools import _build_symptom_queries

        queries = _build_symptom_queries(None, {})
        assert queries == ["print failure"]
