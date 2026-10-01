"""Tests for adhesion intelligence and print failure diagnosis.

Covers:
    - recommend_adhesion() decision matrix (all branches)
    - diagnose_from_signals() priority-ordered diagnosis
    - is_bedslinger() lookup
    - failure_symptom_queries() helper
    - AdhesionRecommendation / PrintFailureDiagnosis dataclasses
"""

from __future__ import annotations

from kiln.printability import (
    AdhesionRecommendation,
    BedAdhesionAnalysis,
    PrintFailureDiagnosis,
    _adhesion_risk_for_contact,
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


def _measured(contact_percentage: float) -> BedAdhesionAnalysis:
    """A bed-adhesion reading labelled by the engine's own bands."""
    return _adhesion(
        contact_percentage=contact_percentage,
        adhesion_risk=_adhesion_risk_for_contact(contact_percentage),
    )


class TestTheBrimAgreesWithTheRisk:
    """The brim decision and the risk label describe the same part.

    2026-10-01: an enclosure base printed open side down touched the bed
    over 7.1% of its footprint.  The report labelled it ``high`` adhesion
    risk and, in the same block, said "Good bed contact (7.1%), no brim
    needed" -- the table had rows for under 5% and for 10-30% and none for
    the band between, so a part there fell through to the row written for
    a part that sits flat.
    """

    def test_the_part_from_the_report(self):
        rec = recommend_adhesion(_measured(7.1), material="PLA", is_bedslinger_printer=True)
        assert rec.adhesion_risk == "high"
        assert rec.brim_width_mm == 5
        assert "7.1%" in rec.rationale and "brim" in rec.rationale
        assert "Good bed contact" not in rec.rationale
        assert rec.slicer_overrides["brim_width"] == "5"

    def test_a_warping_material_in_the_band_gets_the_wide_brim(self):
        for enclosed in (True, False):
            rec = recommend_adhesion(_measured(7.1), material="ABS", has_enclosure=enclosed)
            assert rec.brim_width_mm == 8 and rec.use_raft is False

    @staticmethod
    def _sweep():
        tenths = [t / 10.0 for t in range(0, 1001)]
        for material in ("PLA", "PETG", "ABS", "ASA"):
            for enclosed in (False, True):
                for bedslinger in (False, True):
                    for height in (20.0, 80.0):
                        yield material, enclosed, bedslinger, height, [
                            (pct, recommend_adhesion(
                                _measured(pct), material=material, has_enclosure=enclosed,
                                is_bedslinger_printer=bedslinger, model_height_mm=height,
                            ))
                            for pct in tenths
                        ]

    def test_no_brim_is_only_ever_said_of_a_part_at_low_risk(self):
        for material, enclosed, bedslinger, height, recs in self._sweep():
            for pct, rec in recs:
                where = f"{material} enclosed={enclosed} bedslinger={bedslinger} h={height} at {pct}%"
                if rec.adhesion_risk != "low":
                    assert rec.brim_width_mm > 0 or rec.use_raft, f"no brim at {rec.adhesion_risk} risk: {where}"
                    assert "Good bed contact" not in rec.rationale, where
                if rec.brim_width_mm == 0 and not rec.use_raft:
                    assert rec.slicer_overrides == {}, where

    def test_less_contact_never_gets_less_help(self):
        for material, enclosed, bedslinger, height, recs in self._sweep():
            previous = None
            for pct, rec in recs:
                strength = (rec.use_raft, rec.brim_width_mm)
                if previous is not None:
                    assert strength <= previous[1], (
                        f"{material} enclosed={enclosed} bedslinger={bedslinger} h={height}: "
                        f"{pct}% contact gets {strength}, more than {previous[0]}% got ({previous[1]})"
                    )
                previous = (pct, strength)


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
# failure_symptom_queries() -- the one builder every diagnosis door uses
# ---------------------------------------------------------------------------


class TestBuildSymptomQueries:
    """Tests for failure_symptom_queries, shared by every diagnosis door."""

    def test_high_adhesion_risk(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({"adhesion_risk": "high"})
        assert any("adhesion" in q for q in queries)

    def test_medium_adhesion_risk(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({"adhesion_risk": "medium"})
        assert any("adhesion" in q for q in queries)

    def test_thermal_delta(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({"tool_temp_actual": 180.0, "tool_temp_target": 210.0},
        )
        assert any("temperature" in q or "thermal" in q for q in queries)

    def test_print_error(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({"print_error": "nozzle_clog"})
        assert "nozzle_clog" in queries

    def test_overhang(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({"overhang_pct": 40.0})
        assert any("overhang" in q for q in queries)

    def test_bridge(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({"max_bridge_mm": 20.0})
        assert any("bridge" in q for q in queries)

    def test_warp_material_no_enclosure(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({"material": "ABS", "printer_has_enclosure": False},
        )
        assert any("warp" in q for q in queries)

    def test_empty_signals_fallback(self):
        from kiln.printability import failure_symptom_queries

        queries = failure_symptom_queries({})
        assert queries == ["print failure"]
