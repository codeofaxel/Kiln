"""Tests for kiln.material_safety — material compatibility and process settings."""

from __future__ import annotations

import pytest

from kiln.material_safety import (
    MATERIAL_PROCESS,
    abrasive_nozzle_floor,
    build_bambu_flush_matrix,
    check_material_compatibility,
    get_process_settings,
    is_abrasive_filament,
)

# ---------------------------------------------------------------------------
# check_material_compatibility — basic cases
# ---------------------------------------------------------------------------


class TestCheckMaterialCompatibility:
    def test_empty_list_returns_ok(self):
        result = check_material_compatibility([])
        assert result["safe"] is True
        assert result["level"] == "ok"
        assert result["pairs"] == []
        assert result["message"] == "No materials specified."

    def test_single_material_no_pairs(self):
        """One material → no cross-material pairs, just hardware checks."""
        result = check_material_compatibility(["PLA"])
        assert result["safe"] is True
        assert result["level"] == "ok"
        assert result["pairs"] == []

    def test_pla_pla_ok(self):
        result = check_material_compatibility(["PLA", "PLA"])
        assert result["safe"] is True
        assert result["level"] == "ok"
        assert "✅" in result["message"]

    def test_pla_pvs_ok(self):
        """PLA + PVA is a classic soluble support pairing — should be OK."""
        result = check_material_compatibility(["PLA", "PVA"])
        assert result["safe"] is True
        assert result["level"] == "ok"

    def test_pla_abs_incompatible(self):
        result = check_material_compatibility(["PLA", "ABS"])
        assert result["safe"] is False
        assert result["level"] == "incompatible"
        assert "⛔" in result["message"]
        assert "ABS" in result["message"]

    def test_pla_asa_incompatible(self):
        result = check_material_compatibility(["PLA", "ASA"])
        assert result["safe"] is False
        assert result["level"] == "incompatible"

    def test_pla_nylon_incompatible(self):
        result = check_material_compatibility(["PLA", "NYLON"])
        assert result["safe"] is False
        assert result["level"] == "incompatible"

    def test_pla_pc_incompatible(self):
        result = check_material_compatibility(["PLA", "PC"])
        assert result["safe"] is False
        assert result["level"] == "incompatible"

    def test_abs_tpu_incompatible(self):
        result = check_material_compatibility(["ABS", "TPU"])
        assert result["safe"] is False
        assert result["level"] == "incompatible"

    def test_pla_petg_conditional(self):
        result = check_material_compatibility(["PLA", "PETG"])
        assert result["safe"] is True  # conditional is still safe (printable w/ mitigations)
        assert result["level"] == "conditional"
        assert "⚠️" in result["message"]

    def test_pla_tpu_caution(self):
        result = check_material_compatibility(["PLA", "TPU"])
        assert result["safe"] is True
        assert result["level"] == "caution"

    def test_abs_hips_ok(self):
        """Classic ABS + HIPS (limonene-soluble support) should be OK."""
        result = check_material_compatibility(["ABS", "HIPS"])
        assert result["safe"] is True
        assert result["level"] == "ok"

    def test_abs_asa_ok(self):
        result = check_material_compatibility(["ABS", "ASA"])
        assert result["safe"] is True
        assert result["level"] == "ok"

    def test_case_insensitive(self):
        """Material names should be normalised before lookup."""
        lower = check_material_compatibility(["pla", "abs"])
        upper = check_material_compatibility(["PLA", "ABS"])
        assert lower["level"] == upper["level"]
        assert lower["safe"] == upper["safe"]

    def test_whitespace_stripped(self):
        result = check_material_compatibility(["  PLA  ", "  ABS  "])
        assert result["safe"] is False

    def test_empty_strings_ignored(self):
        result = check_material_compatibility(["PLA", "", "PLA"])
        assert result["safe"] is True
        assert result["level"] == "ok"

    def test_none_strings_ignored(self):
        """None-like empty values should be filtered out gracefully."""
        result = check_material_compatibility(["PLA", None, "PLA"])  # type: ignore[list-item]
        assert result["safe"] is True


# ---------------------------------------------------------------------------
# Pairs structure
# ---------------------------------------------------------------------------


class TestPairsStructure:
    def test_pairs_contains_level_warning_mitigations(self):
        result = check_material_compatibility(["PLA", "PETG"])
        assert len(result["pairs"]) == 1
        pair = result["pairs"][0]
        assert "level" in pair
        assert "warning" in pair
        assert "mitigations" in pair
        assert "purge_volume_mm3" in pair

    def test_pairs_symmetric_dedup(self):
        """PLA+PETG should appear once, not twice."""
        result = check_material_compatibility(["PLA", "PETG"])
        assert len(result["pairs"]) == 1

    def test_three_materials_two_pairs(self):
        """PLA + PETG + TPU → 3 unique unordered pairs."""
        result = check_material_compatibility(["PLA", "PETG", "TPU"])
        # Pairs: PLA↔PETG, PLA↔TPU, PETG↔TPU
        assert len(result["pairs"]) == 3

    def test_incompatible_pair_has_zero_purge(self):
        result = check_material_compatibility(["PLA", "ABS"])
        pair = result["pairs"][0]
        assert pair["purge_volume_mm3"] == 0


# ---------------------------------------------------------------------------
# Hardware warnings
# ---------------------------------------------------------------------------


class TestHardwareWarnings:
    def test_pla_cf_triggers_hardened_nozzle_warning(self):
        result = check_material_compatibility(["PLA", "PLA-CF"])
        assert any("hardened" in w.lower() for w in result["hardware_warnings"])

    def test_abs_triggers_enclosure_warning(self):
        result = check_material_compatibility(["ABS"])
        assert any("enclosure" in w.lower() for w in result["hardware_warnings"])

    def test_pure_pla_no_hardware_warnings(self):
        result = check_material_compatibility(["PLA"])
        assert result["hardware_warnings"] == []

    def test_duplicate_hardware_warnings_not_repeated(self):
        """Two ABS slots should not double the enclosure warning."""
        result = check_material_compatibility(["ABS", "ABS"])
        enclosure_count = sum(1 for w in result["hardware_warnings"] if "enclosure" in w.lower())
        assert enclosure_count == 1


# ---------------------------------------------------------------------------
# Purge matrix
# ---------------------------------------------------------------------------


class TestPurgeMatrix:
    def test_purge_matrix_shape(self):
        result = check_material_compatibility(["PLA", "ABS"])
        matrix = result["purge_matrix"]
        assert len(matrix) == 2
        assert all(len(row) == 2 for row in matrix)

    def test_purge_matrix_diagonal_is_zero(self):
        result = check_material_compatibility(["PLA", "PETG", "TPU"])
        matrix = result["purge_matrix"]
        for i in range(3):
            assert matrix[i][i] == 0.0

    def test_purge_matrix_values_are_floats(self):
        result = check_material_compatibility(["PLA", "PVA"])
        for row in result["purge_matrix"]:
            for val in row:
                assert isinstance(val, float)

    def test_incompatible_purge_volume_zero(self):
        """Incompatible pairs have purge_volume=0 (don't print at all)."""
        result = check_material_compatibility(["PLA", "ABS"])
        # Off-diagonal should be 0 for incompatible pair
        assert result["purge_matrix"][0][1] == 0.0
        assert result["purge_matrix"][1][0] == 0.0


# ---------------------------------------------------------------------------
# Worst-level propagation with mixed pairings
# ---------------------------------------------------------------------------


class TestWorstLevelPropagation:
    def test_incompatible_wins_over_caution(self):
        """Three materials: PLA+TPU (caution) + PLA+ABS (incompatible) → incompatible."""
        result = check_material_compatibility(["PLA", "TPU", "ABS"])
        assert result["safe"] is False
        assert result["level"] == "incompatible"

    def test_conditional_wins_over_caution(self):
        """PLA+PETG (conditional) + PLA+TPU (caution) → conditional."""
        result = check_material_compatibility(["PLA", "PETG", "TPU"])
        assert result["safe"] is True
        assert result["level"] == "conditional"

    def test_all_ok_stays_ok(self):
        result = check_material_compatibility(["PLA", "PLA-HF"])
        assert result["level"] == "ok"


# ---------------------------------------------------------------------------
# get_process_settings
# ---------------------------------------------------------------------------


class TestGetProcessSettings:
    def test_known_material(self):
        s = get_process_settings("PLA")
        assert s is not None
        assert "temp_range" in s
        assert "bed_temp" in s
        assert "speed_factor" in s
        assert "requires_hardened_nozzle" in s
        assert "requires_enclosure" in s

    def test_unknown_material_returns_none(self):
        assert get_process_settings("UNOBTANIUM") is None

    def test_case_insensitive(self):
        assert get_process_settings("pla") == get_process_settings("PLA")

    def test_all_known_materials_have_temp_range(self):
        for mat_name, info in MATERIAL_PROCESS.items():
            assert "temp_range" in info, f"{mat_name} missing temp_range"
            lo, hi = info["temp_range"]
            assert lo < hi, f"{mat_name} temp_range inverted"

    def test_pla_cf_requires_hardened_nozzle(self):
        s = get_process_settings("PLA-CF")
        assert s["requires_hardened_nozzle"] is True

    def test_abs_requires_enclosure(self):
        s = get_process_settings("ABS")
        assert s["requires_enclosure"] is True

    def test_pla_does_not_require_enclosure(self):
        s = get_process_settings("PLA")
        assert s["requires_enclosure"] is False


# ---------------------------------------------------------------------------
# build_bambu_flush_matrix
# ---------------------------------------------------------------------------


class TestBuildBambuFlushMatrix:
    def test_returns_string(self):
        result = build_bambu_flush_matrix(["PLA", "PLA"])
        assert isinstance(result, str)

    def test_default_4_slots_produces_16_values(self):
        result = build_bambu_flush_matrix(["PLA", "PLA"])
        values = result.split()
        assert len(values) == 16  # 4×4

    def test_diagonal_is_zero(self):
        """Slots flushing into themselves = 0."""
        result = build_bambu_flush_matrix(["PLA", "PETG"], n_slots=2)
        values = [int(v) for v in result.split()]
        # Matrix: [[0, PLA→PETG], [PETG→PLA, 0]]
        assert values[0] == 0  # slot 0 → slot 0
        assert values[3] == 0  # slot 1 → slot 1

    def test_off_diagonal_non_zero_for_different_materials(self):
        result = build_bambu_flush_matrix(["PLA", "PETG"], n_slots=2)
        values = [int(v) for v in result.split()]
        assert values[1] > 0
        assert values[2] > 0

    def test_unknown_slots_get_conservative_default(self):
        """Empty / unspecified slots should get 800mm³ conservative default."""
        result = build_bambu_flush_matrix(["PLA"], n_slots=4)
        values = [int(v) for v in result.split()]
        # Slot 0 (PLA) → slot 1 (empty) = 800
        assert values[1] == 800

    def test_n_slots_parameter(self):
        result = build_bambu_flush_matrix(["PLA", "ABS", "PETG"], n_slots=3)
        values = result.split()
        assert len(values) == 9  # 3×3


# ---------------------------------------------------------------------------
# is_abrasive_filament / abrasive_nozzle_floor
# ---------------------------------------------------------------------------


class TestAbrasiveFilament:
    """Filled-filament detection and the free nozzle-wear floor.

    Coverage: curated-table hits, name-rule hits for the filled grades
    the table does not carry, colourway names that must not trip it,
    and the floor's honesty about what it cannot establish.
    """

    @pytest.mark.parametrize(
        "material",
        [
            "PLA-CF",       # the one entry MATERIAL_PROCESS carries
            "PETG-CF",      # not in the table — name rule must catch it
            "PA-CF",
            "PAHT-CF",
            "PPS-CF",
            "PLA-GF",
            "ASA-GF",
            "PA6-CF15",     # grade number rides along
            "pla-cf",       # case-insensitive
            "PLA Wood",
            "Copperfill",
            "woodfill",
            "Metal Filled PLA",
            "PLA Carbon Fiber",
            "Glass Fiber Nylon",
            "glass-fibre PA",
        ],
    )
    def test_filled_filament_is_abrasive(self, material):
        assert is_abrasive_filament(material) is True

    @pytest.mark.parametrize(
        "material",
        [
            "PLA", "PETG", "ABS", "ASA", "TPU", "TPE", "PVA", "HIPS",
            "PA", "PC", "PLA+", "PLA-HF", "NYLON",
            "Polycarbonate",   # contains "carbon" — must not read as filled
            "Copper PLA",      # a colourway, not a filler
            "Bronze PETG",
            "Sea Glass PLA",
            "Redwood PLA",
            "", "   ",
        ],
    )
    def test_unfilled_filament_is_not_abrasive(self, material):
        assert is_abrasive_filament(material) is False

    def test_no_catalogued_material_trips_the_rule(self):
        # Every material in MATERIAL_PROCESS must agree with its own
        # requires_hardened_nozzle flag, so the name rule can never
        # contradict the curated table it extends.
        for name, settings in MATERIAL_PROCESS.items():
            assert is_abrasive_filament(name) is bool(
                settings["requires_hardened_nozzle"]
            ), name

    def test_floor_names_the_material_and_the_fix(self):
        note = abrasive_nozzle_floor("PETG-CF")
        assert "PETG-CF" in note
        assert "hardened steel" in note.lower()

    def test_floor_does_not_claim_to_know_the_nozzle(self):
        # Public Kiln holds no nozzle state.  Claiming otherwise would be
        # the false-safe direction: the reader would trust a reading that
        # was never taken.
        note = abrasive_nozzle_floor("PLA-CF").lower()
        assert "cannot see which nozzle" in note
        assert "caution and not a measurement" in note

    def test_floor_is_silent_for_unfilled_filament(self):
        assert abrasive_nozzle_floor("PLA") == ""
        assert abrasive_nozzle_floor("") == ""
