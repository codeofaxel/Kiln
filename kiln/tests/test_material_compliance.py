"""Tests for kiln.material_compliance -- MaterialComplianceDatabase and friends.

Covers:
- All pre-populated materials return data
- Compliance check with met and unmet requirements
- Warning retrieval
- Unknown material handling
- Case insensitivity
- ComplianceCheckResult and MaterialCompliance serialization
- Multiple requirement checks
"""

from __future__ import annotations

import pytest

from kiln.material_compliance import (
    ComplianceCheckResult,
    MaterialCompliance,
    MaterialComplianceDatabase,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> MaterialComplianceDatabase:
    return MaterialComplianceDatabase()


# All materials that must be in the default database.
_ALL_MATERIALS = [
    "PLA",
    "ABS",
    "PETG",
    "TPU",
    "NYLON",
    "ASA",
    "PC",
    "PVA",
    "HIPS",
    "PP",
    "RESIN_STANDARD",
    "RESIN_TOUGH",
    "RESIN_DENTAL",
]


# ---------------------------------------------------------------------------
# MaterialCompliance dataclass
# ---------------------------------------------------------------------------


class TestMaterialCompliance:
    """Tests for the MaterialCompliance dataclass."""

    def test_to_dict(self):
        mc = MaterialCompliance(
            material_type="TEST",
            food_safe=True,
            warnings=["Caution"],
        )
        d = mc.to_dict()
        assert d["material_type"] == "TEST"
        assert d["food_safe"] is True
        assert d["warnings"] == ["Caution"]
        assert d["reach_compliant"] is False

    def test_default_values(self):
        mc = MaterialCompliance(material_type="X")
        assert mc.food_safe is False
        assert mc.reach_compliant is False
        assert mc.rohs_compliant is False
        assert mc.uv_resistant is False
        assert mc.flame_retardant is False
        assert mc.biocompatible is False
        assert mc.max_continuous_temp_c is None
        assert mc.warnings == []


# ---------------------------------------------------------------------------
# ComplianceCheckResult dataclass
# ---------------------------------------------------------------------------


class TestComplianceCheckResult:
    """Tests for the ComplianceCheckResult dataclass."""

    def test_to_dict(self):
        r = ComplianceCheckResult(
            compliant=False,
            failures=["Not food safe"],
            warnings=["Handle with care"],
        )
        d = r.to_dict()
        assert d["compliant"] is False
        assert d["failures"] == ["Not food safe"]
        assert d["warnings"] == ["Handle with care"]


# ---------------------------------------------------------------------------
# Pre-populated database
# ---------------------------------------------------------------------------


class TestDatabasePopulation:
    """Tests that all expected materials are present with valid data."""

    @pytest.mark.parametrize("material", _ALL_MATERIALS)
    def test_material_exists(self, db, material):
        info = db.get_compliance(material)
        assert info is not None
        assert info.material_type == material

    @pytest.mark.parametrize("material", _ALL_MATERIALS)
    def test_material_has_warnings(self, db, material):
        info = db.get_compliance(material)
        assert isinstance(info.warnings, list)
        # Every material in our database should have at least one warning
        assert len(info.warnings) >= 1

    @pytest.mark.parametrize("material", _ALL_MATERIALS)
    def test_material_has_max_temp(self, db, material):
        info = db.get_compliance(material)
        assert info.max_continuous_temp_c is not None
        assert info.max_continuous_temp_c > 0


class TestDatabaseSpecificMaterials:
    """Spot-check specific material properties."""

    def test_pla_is_food_safe(self, db):
        info = db.get_compliance("PLA")
        assert info.food_safe is True

    def test_abs_is_not_food_safe(self, db):
        info = db.get_compliance("ABS")
        assert info.food_safe is False

    def test_pc_is_flame_retardant(self, db):
        info = db.get_compliance("PC")
        assert info.flame_retardant is True

    def test_resin_dental_is_biocompatible(self, db):
        info = db.get_compliance("RESIN_DENTAL")
        assert info.biocompatible is True

    def test_asa_is_uv_resistant(self, db):
        info = db.get_compliance("ASA")
        assert info.uv_resistant is True

    def test_petg_is_food_safe(self, db):
        info = db.get_compliance("PETG")
        assert info.food_safe is True

    def test_resin_standard_not_reach_compliant(self, db):
        info = db.get_compliance("RESIN_STANDARD")
        assert info.reach_compliant is False


# ---------------------------------------------------------------------------
# get_compliance
# ---------------------------------------------------------------------------


class TestGetCompliance:
    """Tests for MaterialComplianceDatabase.get_compliance()."""

    def test_returns_none_for_unknown_material(self, db):
        assert db.get_compliance("UNOBTANIUM") is None

    def test_case_insensitive_lookup(self, db):
        info = db.get_compliance("pla")
        assert info is not None
        assert info.material_type == "PLA"

    def test_mixed_case_lookup(self, db):
        info = db.get_compliance("Petg")
        assert info is not None
        assert info.material_type == "PETG"


# ---------------------------------------------------------------------------
# check_job_compliance
# ---------------------------------------------------------------------------


class TestCheckJobCompliance:
    """Tests for MaterialComplianceDatabase.check_job_compliance()."""

    def test_pla_meets_food_safe(self, db):
        result = db.check_job_compliance("PLA", requirements=["food_safe"])
        assert result.compliant is True
        assert result.failures == []
        assert len(result.warnings) > 0

    def test_abs_fails_food_safe(self, db):
        result = db.check_job_compliance("ABS", requirements=["food_safe"])
        assert result.compliant is False
        assert "ABS is not food_safe" in result.failures

    def test_multiple_requirements_all_met(self, db):
        result = db.check_job_compliance("PLA", requirements=["food_safe", "reach_compliant", "rohs_compliant"])
        assert result.compliant is True
        assert result.failures == []

    def test_multiple_requirements_some_unmet(self, db):
        result = db.check_job_compliance("PLA", requirements=["food_safe", "uv_resistant"])
        assert result.compliant is False
        assert len(result.failures) == 1
        assert "PLA is not uv_resistant" in result.failures

    def test_unknown_material_fails(self, db):
        result = db.check_job_compliance("UNOBTANIUM", requirements=["food_safe"])
        assert result.compliant is False
        assert "Unknown material 'UNOBTANIUM'" in result.failures

    def test_unknown_requirement_fails(self, db):
        result = db.check_job_compliance("PLA", requirements=["waterproof"])
        assert result.compliant is False
        assert "Unknown requirement 'waterproof'" in result.failures

    def test_empty_requirements_is_compliant(self, db):
        result = db.check_job_compliance("ABS", requirements=[])
        assert result.compliant is True
        assert result.failures == []

    def test_case_insensitive_material(self, db):
        result = db.check_job_compliance("pla", requirements=["food_safe"])
        assert result.compliant is True

    def test_warnings_included_in_result(self, db):
        result = db.check_job_compliance("ABS", requirements=["reach_compliant"])
        assert result.compliant is True
        assert len(result.warnings) > 0


# ---------------------------------------------------------------------------
# get_warnings
# ---------------------------------------------------------------------------


class TestGetWarnings:
    """Tests for MaterialComplianceDatabase.get_warnings()."""

    def test_returns_warnings_for_known_material(self, db):
        warnings = db.get_warnings("ABS")
        assert len(warnings) > 0
        assert any("ventilation" in w.lower() for w in warnings)

    def test_returns_empty_for_unknown_material(self, db):
        assert db.get_warnings("UNOBTANIUM") == []

    def test_case_insensitive(self, db):
        w1 = db.get_warnings("PLA")
        w2 = db.get_warnings("pla")
        assert w1 == w2

    def test_returns_copy_not_reference(self, db):
        w1 = db.get_warnings("PLA")
        w1.append("modified")
        w2 = db.get_warnings("PLA")
        assert "modified" not in w2
