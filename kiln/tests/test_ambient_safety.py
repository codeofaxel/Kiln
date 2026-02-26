"""Tests for ambient-aware safety warnings.

Covers material thermal profile checks, chamber temperature warnings,
cooldown advisories, thermal runaway detection, material normalization,
and edge cases.
"""

from __future__ import annotations

import pytest

from kiln.ambient_safety import (
    AmbientSafetyResult,
    AmbientWarning,
    _normalize_material,
    check_ambient_safety,
    get_supported_materials,
)


class TestNormalizeMaterial:
    def test_none(self) -> None:
        assert _normalize_material(None) is None

    def test_lowercase(self) -> None:
        assert _normalize_material("pla") == "PLA"

    def test_mixed_case(self) -> None:
        assert _normalize_material("Petg") == "PETG"

    def test_whitespace(self) -> None:
        assert _normalize_material("  ABS  ") == "ABS"

    def test_alias_polycarbonate(self) -> None:
        assert _normalize_material("polycarbonate") == "PC"

    def test_alias_pa6(self) -> None:
        assert _normalize_material("PA6") == "PA"

    def test_alias_pla_plus(self) -> None:
        assert _normalize_material("PLA+") == "PLA+"


class TestCheckAmbientSafetyNoChamber:
    def test_no_chamber_temp_is_safe(self) -> None:
        result = check_ambient_safety(chamber_temp_c=None, material="PLA")
        assert result.safe is True
        assert result.warnings == []
        assert result.chamber_temp_c is None


class TestPLATooHot:
    def test_pla_at_50c_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=50.0, material="PLA")
        assert result.safe is False
        assert any(w.category == "too_hot" for w in result.warnings)

    def test_pla_at_45c_is_safe(self) -> None:
        result = check_ambient_safety(chamber_temp_c=44.0, material="PLA")
        assert result.safe is True
        assert not any(w.category == "too_hot" for w in result.warnings)

    def test_pla_at_exact_max_is_safe(self) -> None:
        result = check_ambient_safety(chamber_temp_c=45.0, material="PLA")
        assert not any(w.category == "too_hot" for w in result.warnings)

    def test_pla_at_above_max_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=46.0, material="PLA")
        assert any(w.category == "too_hot" for w in result.warnings)


class TestABSTooCold:
    def test_abs_at_20c_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=20.0, material="ABS")
        assert result.safe is False
        assert any(w.category == "too_cold" for w in result.warnings)

    def test_abs_at_40c_is_safe(self) -> None:
        result = check_ambient_safety(chamber_temp_c=40.0, material="ABS")
        assert result.safe is True

    def test_abs_at_exact_min_is_safe(self) -> None:
        result = check_ambient_safety(chamber_temp_c=35.0, material="ABS")
        assert not any(w.category == "too_cold" for w in result.warnings)


class TestASATooCold:
    def test_asa_at_25c_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=25.0, material="ASA")
        assert result.safe is False
        assert any(w.category == "too_cold" for w in result.warnings)


class TestPCTooCold:
    def test_pc_at_30c_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=30.0, material="PC")
        assert result.safe is False
        assert any(w.category == "too_cold" for w in result.warnings)


class TestNylonTooCold:
    def test_nylon_at_20c_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=20.0, material="NYLON")
        assert result.safe is False
        assert any(w.category == "too_cold" for w in result.warnings)


class TestThermalRunaway:
    def test_over_max_chamber(self) -> None:
        result = check_ambient_safety(
            chamber_temp_c=85.0,
            material="ABS",
            max_chamber_temp_c=70.0,
        )
        assert any(w.category == "thermal_runaway" for w in result.warnings)
        assert any(w.severity == "critical" for w in result.warnings)

    def test_at_max_chamber_no_runaway(self) -> None:
        result = check_ambient_safety(
            chamber_temp_c=70.0,
            material="ABS",
            max_chamber_temp_c=70.0,
        )
        assert not any(w.category == "thermal_runaway" for w in result.warnings)


class TestCooldownAdvisory:
    def test_pla_warm_chamber_cooldown(self) -> None:
        # Chamber at 40C — above 35C default cooldown threshold but
        # below PLA max of 45C, so no "too_hot" but should get cooldown
        result = check_ambient_safety(chamber_temp_c=40.0, material="PLA")
        assert any(w.category == "cooldown_advisory" for w in result.warnings)

    def test_pla_cool_chamber_no_advisory(self) -> None:
        result = check_ambient_safety(chamber_temp_c=30.0, material="PLA")
        assert not any(w.category == "cooldown_advisory" for w in result.warnings)

    def test_pla_hot_chamber_no_double_warning(self) -> None:
        # Chamber at 50C — should get "too_hot" but NOT also "cooldown"
        result = check_ambient_safety(chamber_temp_c=50.0, material="PLA")
        assert any(w.category == "too_hot" for w in result.warnings)
        assert not any(w.category == "cooldown_advisory" for w in result.warnings)

    def test_abs_warm_chamber_no_cooldown(self) -> None:
        # ABS is not heat-sensitive — no cooldown advisory
        result = check_ambient_safety(chamber_temp_c=40.0, material="ABS")
        assert not any(w.category == "cooldown_advisory" for w in result.warnings)


class TestPETG:
    def test_petg_at_50c_is_safe(self) -> None:
        result = check_ambient_safety(chamber_temp_c=50.0, material="PETG")
        assert result.safe is True

    def test_petg_at_60c_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=60.0, material="PETG")
        assert result.safe is False
        assert any(w.category == "too_hot" for w in result.warnings)


class TestTPU:
    def test_tpu_at_55c_warns(self) -> None:
        result = check_ambient_safety(chamber_temp_c=55.0, material="TPU")
        assert result.safe is False
        assert any(w.category == "too_hot" for w in result.warnings)


class TestNoMaterial:
    def test_no_material_no_max_is_safe(self) -> None:
        result = check_ambient_safety(chamber_temp_c=60.0, material=None)
        assert result.safe is True

    def test_no_material_with_max_checks_runaway(self) -> None:
        result = check_ambient_safety(
            chamber_temp_c=80.0,
            material=None,
            max_chamber_temp_c=70.0,
        )
        assert not result.safe
        assert any(w.category == "thermal_runaway" for w in result.warnings)


class TestUnknownMaterial:
    def test_unknown_material_no_crash(self) -> None:
        result = check_ambient_safety(chamber_temp_c=50.0, material="EXOTIC_BLEND")
        # No thermal profile found — should not crash, no material warnings
        assert result.safe is True


class TestSerialization:
    def test_warning_to_dict(self) -> None:
        w = AmbientWarning("warning", "too_hot", "Test message")
        d = w.to_dict()
        assert d["severity"] == "warning"
        assert d["category"] == "too_hot"
        assert d["message"] == "Test message"

    def test_result_to_dict(self) -> None:
        result = AmbientSafetyResult(
            safe=False,
            chamber_temp_c=50.0,
            material="PLA",
            warnings=[AmbientWarning("warning", "too_hot", "Hot")],
        )
        d = result.to_dict()
        assert d["safe"] is False
        assert d["chamber_temp_c"] == 50.0
        assert len(d["warnings"]) == 1


class TestSupportedMaterials:
    def test_returns_sorted_list(self) -> None:
        materials = get_supported_materials()
        assert isinstance(materials, list)
        assert materials == sorted(materials)
        assert "PLA" in materials
        assert "ABS" in materials
        assert "PETG" in materials

    def test_at_least_10_materials(self) -> None:
        assert len(get_supported_materials()) >= 10
