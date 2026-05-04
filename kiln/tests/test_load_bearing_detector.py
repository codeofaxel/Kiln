"""Tests for the free-tier load-bearing detector.

Validates the trip rules from RESEARCH_normie_vocabulary.md §3:
- Single high-confidence noun fires on its own
- Single explicit "load-bearing" / "structural" adjective fires
- Mass >= 22 N (≈ 5 lb) fires
- Engineering material name shifts the prior
- Decoy patterns (phone stand, drink coaster, decorative) don't trip

The detector is open-source (lives in public Kiln) — these tests are
public.  The MOAT (real engineering math the upgrade nudge points to)
ships in kiln-pro and is tested separately there.
"""

from __future__ import annotations

import pytest

from kiln.load_bearing_detector import (
    LoadBearingVerdict,
    detect_load_bearing,
    extract_load_in_newtons,
)


class TestExtractLoadInNewtons:
    """Mass / force numeric extraction from free-text briefs."""

    def test_pounds(self):
        assert extract_load_in_newtons("holds 5 lbs") == pytest.approx(22.24, rel=0.01)

    def test_kilograms(self):
        assert extract_load_in_newtons("must support 2 kg") == pytest.approx(19.62, rel=0.01)

    def test_grams(self):
        assert extract_load_in_newtons("light 50 grams") == pytest.approx(0.4905, rel=0.01)

    def test_ounces(self):
        assert extract_load_in_newtons("16 oz weight") == pytest.approx(4.448, rel=0.02)

    def test_explicit_newtons(self):
        assert extract_load_in_newtons("rated 50 N") == 50.0

    def test_no_load_returns_none(self):
        assert extract_load_in_newtons("a small ornament") is None

    def test_picks_largest_load(self):
        # When multiple loads mentioned, take the worst-case
        assert extract_load_in_newtons("holds 1 lb usually but up to 10 lb") == pytest.approx(44.48, rel=0.01)


class TestDetectLoadBearingTrips:
    """Cases that SHOULD trip the detector."""

    def test_high_confidence_noun_bracket_trips(self):
        v = detect_load_bearing("print a bracket")
        assert v.is_load_bearing is True
        assert v.trip_score >= 50

    def test_mount_trips(self):
        v = detect_load_bearing("a wall mount for my monitor")
        assert v.is_load_bearing is True

    def test_shelf_trips(self):
        v = detect_load_bearing("shelf for my books")
        assert v.is_load_bearing is True

    def test_explicit_load_bearing_trips(self):
        v = detect_load_bearing("a load-bearing component")
        assert v.is_load_bearing is True
        assert v.confidence in ("high", "medium")

    def test_explicit_structural_trips(self):
        v = detect_load_bearing("structural part for my drone")
        assert v.is_load_bearing is True

    def test_5_pounds_trips(self):
        v = detect_load_bearing("must hold 5 lbs of weight")
        assert v.is_load_bearing is True
        assert v.load_n_extracted == pytest.approx(22.24, rel=0.01)

    def test_engineering_material_with_verb_trips(self):
        v = detect_load_bearing("PA6-CF arm that holds the camera", material="PA6-CF")
        assert v.is_load_bearing is True
        assert v.applies_engineering_material is True

    def test_explicit_load_n_param_trips(self):
        v = detect_load_bearing("simple part", applied_load_n=50.0)
        assert v.is_load_bearing is True

    def test_guitar_wall_mount_high_confidence(self):
        # The headline normie example
        v = detect_load_bearing("wall mount that holds my guitar")
        assert v.is_load_bearing is True
        assert v.confidence == "high"
        # Fires on multiple signals: noun, geometric tell, verb
        assert any("mount" in r.lower() for r in v.trip_reasons)
        assert any("hold" in r.lower() for r in v.trip_reasons)
        assert any("wall" in r.lower() for r in v.trip_reasons)

    def test_bookshelf_high_confidence(self):
        v = detect_load_bearing("bookshelf that holds 25 lbs of books")
        assert v.is_load_bearing is True
        assert v.confidence == "high"


class TestDetectLoadBearingDoesNotTrip:
    """Cases that should NOT trip — decoys, decorative, light hobby."""

    def test_phone_stand_does_not_trip(self):
        v = detect_load_bearing("a phone stand for my desk")
        assert v.is_load_bearing is False

    def test_drink_coaster_does_not_trip(self):
        v = detect_load_bearing("decorative drink coaster")
        assert v.is_load_bearing is False

    def test_decorative_bracket_does_not_trip(self):
        # "bracket" alone trips, but "decorative bracket" gets a -20 decoy
        # downgrade.  Net: 50 - 20 = 30 — below 50 threshold.
        v = detect_load_bearing("decorative bracket for the wall")
        # NOTE: "wall" alone doesn't trip (needs "wall mount"); "bracket"
        # +50 minus "decorative" -10 decoy minus "decorative bracket"
        # -20 decoy = +20 — should NOT trip.
        assert v.is_load_bearing is False, f"score was {v.trip_score}, reasons: {v.trip_reasons}"

    def test_lithophane_does_not_trip(self):
        v = detect_load_bearing("a lithophane of my dog")
        assert v.is_load_bearing is False

    def test_figurine_does_not_trip(self):
        v = detect_load_bearing("a figurine of a dragon")
        assert v.is_load_bearing is False

    def test_light_ornament_does_not_trip(self):
        v = detect_load_bearing("small christmas ornament 50 grams")
        # Even with a numeric mention, 0.5 N is way below threshold;
        # ornament adds -15 decoy
        assert v.is_load_bearing is False

    def test_empty_brief_does_not_trip(self):
        v = detect_load_bearing("")
        assert v.is_load_bearing is False
        assert v.trip_score == 0


class TestUpgradeRecommendation:
    """The Kiln Pro upgrade-nudge attached when the detector trips."""

    def test_trip_includes_upgrade_recommendation(self):
        v = detect_load_bearing("wall mount that holds 8 pounds")
        assert v.is_load_bearing is True
        assert v.upgrade_recommendation
        assert v.upgrade_recommendation["code"] == "LOAD_BEARING_DETECTED"
        assert v.upgrade_recommendation["engineering_grade"] == "heuristic"
        assert "kiln3d.com/pricing" in v.upgrade_recommendation["pro_upgrade"]["upgrade_url"]
        # User sees what the heuristic doesn't account for
        assert "fatigue" in v.upgrade_recommendation["warning"].lower()
        assert "creep" in v.upgrade_recommendation["warning"].lower()

    def test_no_trip_no_upgrade_recommendation(self):
        v = detect_load_bearing("a phone stand")
        assert v.is_load_bearing is False
        assert v.upgrade_recommendation == {}

    def test_upgrade_recommendation_lists_features(self):
        v = detect_load_bearing("structural drone arm")
        items = v.upgrade_recommendation["pro_upgrade"]["what_youd_get"]
        assert isinstance(items, list)
        assert len(items) >= 5
        # Concrete, not vague
        text = " ".join(items).lower()
        assert "iso 286" in text or "iso286" in text
        assert "fos" in text or "factor of safety" in text
        assert "fatigue" in text


class TestConfidenceLevels:
    """Confidence buckets — high (>=100), medium (>=70), low (>=50)."""

    def test_single_keyword_low_confidence(self):
        v = detect_load_bearing("a hook")
        assert v.is_load_bearing is True
        assert v.confidence in ("low", "medium")

    def test_multiple_signals_high_confidence(self):
        v = detect_load_bearing(
            "load-bearing wall mount bracket that holds 25 lbs",
        )
        assert v.confidence == "high"
        assert v.trip_score >= 100


class TestVerdictSerialization:
    def test_to_dict_round_trip(self):
        v = detect_load_bearing("guitar wall mount holds 8 lbs")
        d = v.to_dict()
        assert d["is_load_bearing"] is True
        assert isinstance(d["trip_reasons"], list)
        assert isinstance(d["upgrade_recommendation"], dict)
