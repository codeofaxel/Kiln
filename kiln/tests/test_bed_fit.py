"""Bed-fit safety tests.

Regression coverage for incident #0 (2026-04-15, Bambu A1 nozzle crash
into purge tool).  Tests the `kiln.printers.bed_fit` module + the
downstream gates in slicer_tools and upload_file.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kiln.printers.bed_fit import (
    apply_translation_to_stl,
    check_bed_fit,
    check_gcode_has_homing,
    compute_gcode_bbox,
    compute_mesh_bbox,
    get_build_volume,
    validate_gcode_for_printer,
    validate_mesh_for_printer,
)

# ---------------------------------------------------------------------------
# Helpers to build test STL/gcode files
# ---------------------------------------------------------------------------

_STL_HEADER_SIZE = 80


def _write_cube_stl(
    path: Path,
    x: tuple[float, float],
    y: tuple[float, float],
    z: tuple[float, float],
) -> None:
    """Write a minimal binary STL for a cube with the given bounds."""
    x_min, x_max = x
    y_min, y_max = y
    z_min, z_max = z
    # 12 triangles (2 per face × 6 faces) — use a simple winding
    v = {
        "NNN": (x_min, y_min, z_min), "PNN": (x_max, y_min, z_min),
        "NPN": (x_min, y_max, z_min), "PPN": (x_max, y_max, z_min),
        "NNP": (x_min, y_min, z_max), "PNP": (x_max, y_min, z_max),
        "NPP": (x_min, y_max, z_max), "PPP": (x_max, y_max, z_max),
    }
    faces = [
        (v["NNN"], v["PNN"], v["PPN"]), (v["NNN"], v["PPN"], v["NPN"]),  # bottom
        (v["NNP"], v["PPP"], v["PNP"]), (v["NNP"], v["NPP"], v["PPP"]),  # top
        (v["NNN"], v["NPN"], v["NPP"]), (v["NNN"], v["NPP"], v["NNP"]),  # -X
        (v["PNN"], v["PNP"], v["PPP"]), (v["PNN"], v["PPP"], v["PPN"]),  # +X
        (v["NNN"], v["NNP"], v["PNP"]), (v["NNN"], v["PNP"], v["PNN"]),  # -Y
        (v["NPN"], v["PPN"], v["PPP"]), (v["NPN"], v["PPP"], v["NPP"]),  # +Y
    ]
    with open(path, "wb") as fh:
        fh.write(b"test cube".ljust(_STL_HEADER_SIZE, b"\x00"))
        fh.write(struct.pack("<I", len(faces)))
        for tri in faces:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))  # normal
            for vx in tri:
                fh.write(struct.pack("<3f", *vx))
            fh.write(struct.pack("<H", 0))  # attribute byte count


# ---------------------------------------------------------------------------
# Printer intelligence lookup
# ---------------------------------------------------------------------------

class TestBuildVolumeLookup:
    def test_bambu_a1(self):
        vol = get_build_volume("bambu_a1")
        assert vol == (256.0, 256.0, 256.0)

    def test_creality_k1_max_alias(self):
        assert get_build_volume("creality_k1_max") == (300.0, 300.0, 300.0)

    def test_ender3_v3_ke(self):
        assert get_build_volume("ender3_v3_ke") == (220.0, 220.0, 240.0)

    def test_sparkx_i7(self):
        assert get_build_volume("sparkx_i7") == (260.0, 260.0, 255.0)

    def test_ender3_v4(self):
        assert get_build_volume("ender3_v4") == (220.0, 220.0, 235.0)

    def test_unknown_returns_none(self):
        assert get_build_volume("this_printer_does_not_exist") is None

    def test_none_id_returns_none(self):
        # defensive: None printer_id shouldn't crash — get_build_volume
        # isn't called with None but we check the None path in the full
        # validators
        assert get_build_volume("") is None


# ---------------------------------------------------------------------------
# Mesh bbox
# ---------------------------------------------------------------------------

class TestComputeMeshBbox:
    def test_stl_origin_centered_cube(self, tmp_path):
        stl = tmp_path / "cube.stl"
        _write_cube_stl(stl, (-10, 10), (-10, 10), (0, 5))
        bbox = compute_mesh_bbox(str(stl))
        assert bbox is not None
        assert bbox["x_min"] == pytest.approx(-10)
        assert bbox["x_max"] == pytest.approx(10)
        assert bbox["y_min"] == pytest.approx(-10)
        assert bbox["y_max"] == pytest.approx(10)
        assert bbox["z_min"] == pytest.approx(0)
        assert bbox["z_max"] == pytest.approx(5)

    def test_missing_file(self):
        assert compute_mesh_bbox("/nonexistent/path.stl") is None


# ---------------------------------------------------------------------------
# The fit check — this is the core incident regression
# ---------------------------------------------------------------------------

class TestCheckBedFit:
    def test_origin_centered_disc_on_bambu_a1_is_rejected(self):
        """This is the exact incident #0 geometry."""
        bbox = {
            "x_min": -12.5, "x_max": 12.5,
            "y_min": -12.475, "y_max": 12.475,
            "z_min": 0.0, "z_max": 5.0,
        }
        result = check_bed_fit(bbox, (256, 256, 256), source="mesh")
        assert result["ok"] is False
        assert result["error_code"] == "OFF_BED_GEOMETRY"
        assert result["suggested_translate"] is not None
        # Translation should move x and y centers to bed center
        tx, ty, tz = result["suggested_translate"]
        assert tx == pytest.approx(128.0)
        assert ty == pytest.approx(128.0)
        assert tz == pytest.approx(0.0)  # z_min already 0

    def test_properly_placed_model_passes(self):
        bbox = {
            "x_min": 100, "x_max": 150,
            "y_min": 100, "y_max": 150,
            "z_min": 0, "z_max": 20,
        }
        result = check_bed_fit(bbox, (256, 256, 256))
        assert result["ok"] is True
        assert result["error_code"] is None

    def test_model_too_big_cannot_be_translated(self):
        bbox = {
            "x_min": 0, "x_max": 300,  # wider than 256 bed
            "y_min": 0, "y_max": 100,
            "z_min": 0, "z_max": 50,
        }
        result = check_bed_fit(bbox, (256, 256, 256))
        assert result["ok"] is False
        assert result["error_code"] == "EXCEEDS_BED"
        # No translation could fix this
        assert result["suggested_translate"] is None

    def test_unknown_printer_does_not_block(self):
        bbox = {
            "x_min": -500, "x_max": 500,  # clearly bad
            "y_min": -500, "y_max": 500,
            "z_min": 0, "z_max": 5,
        }
        result = check_bed_fit(bbox, None)  # unknown volume
        assert result["ok"] is True
        assert result["error_code"] == "VOLUME_UNKNOWN"

    def test_missing_bbox_does_not_block(self):
        result = check_bed_fit(None, (256, 256, 256))
        assert result["ok"] is True
        assert result["error_code"] == "BBOX_UNKNOWN"

    def test_floating_point_epsilon_tolerance(self):
        """A model that's 0.0001mm past the edge should NOT be rejected."""
        bbox = {
            "x_min": 0.0, "x_max": 256.0001,  # 0.0001mm past bed
            "y_min": 0.0, "y_max": 256.0,
            "z_min": 0.0, "z_max": 10.0,
        }
        result = check_bed_fit(bbox, (256, 256, 256))
        assert result["ok"] is True

    def test_negative_z_rejected(self):
        """Model dipping below the bed (z_min < -epsilon) IS dangerous —
        nozzle would grind into the bed.  Distinct from z_min > 0
        (hovering), which is wasteful but not dangerous."""
        bbox = {
            "x_min": 100, "x_max": 150,
            "y_min": 100, "y_max": 150,
            "z_min": -3.0, "z_max": 10.0,  # dipping below bed
        }
        result = check_bed_fit(bbox, (256, 256, 256))
        assert result["ok"] is False
        assert result["error_code"] == "OFF_BED_GEOMETRY"
        # suggested z translation should lift z_min to 0
        assert result["suggested_translate"][2] == pytest.approx(3.0)

    def test_hovering_model_is_not_a_crash(self):
        """z_min > 0 (hovering above bed) is wasteful but not dangerous.
        Don't block it — the slicer may have intentionally raft-lifted."""
        bbox = {
            "x_min": 100, "x_max": 150,
            "y_min": 100, "y_max": 150,
            "z_min": 5.0, "z_max": 10.0,  # hovering
        }
        result = check_bed_fit(bbox, (256, 256, 256))
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Gcode bbox scan
# ---------------------------------------------------------------------------

class TestComputeGcodeBbox:
    def test_scan_positive_moves(self, tmp_path):
        """Only moves with E (extrusion) count as 'print moves'.
        Non-extruding travel moves are skipped because printers
        legitimately travel through off-bed positions for parking/wipe."""
        gcode = tmp_path / "t.gcode"
        gcode.write_text(
            "; start\n"
            "G28\n"
            "G1 X100 Y100 Z0.2 F3000 E0.5\n"
            "G1 X120 Y120 E0.5\n"
            "G1 X150 Y80 E0.7\n"
        )
        bbox = compute_gcode_bbox(str(gcode))
        assert bbox is not None
        assert bbox["x_min"] == 100
        assert bbox["x_max"] == 150
        assert bbox["y_min"] == 80
        assert bbox["y_max"] == 120

    def test_scan_catches_negative_moves(self, tmp_path):
        """The incident #0 scenario — negative X EXTRUSION moves."""
        gcode = tmp_path / "bad.gcode"
        gcode.write_text(
            "; start\n"
            "G28\n"
            ";LAYER_CHANGE\n"
            "G1 X-12.5 Y-10.0 Z0.2 F3000 E0.3\n"
            "G1 X12.5 Y12.5 E0.5\n"
        )
        bbox = compute_gcode_bbox(str(gcode))
        assert bbox is not None
        assert bbox["x_min"] == -12.5
        assert bbox["y_min"] == -10.0

    def test_non_extruding_off_bed_travel_is_ignored(self, tmp_path):
        """Bambu A1 parks at X=-48 for wipe (no extrusion).
        That's SAFE — don't flag it as an off-bed crash risk."""
        gcode = tmp_path / "with_park.gcode"
        gcode.write_text(
            ";LAYER_CHANGE\n"
            "G1 X128 Y128 E0.5\n"   # safe print move
            "G1 X-48.2 F3000\n"     # non-extruding park move (safe)
            "G1 X128 Y128 E0.7\n"   # safe print move
        )
        bbox = compute_gcode_bbox(str(gcode))
        assert bbox["x_min"] == 128
        assert bbox["x_max"] == 128  # park move ignored


class TestValidateGcodeForPrinter:
    def test_incident_reproduction_negative_x_is_rejected(self, tmp_path):
        """Extruding move at negative X is rejected (off-bed print)."""
        gcode = tmp_path / "incident.gcode"
        gcode.write_text(
            "G28\n"
            ";LAYER_CHANGE\n"
            "G1 X-12.5 Y-10.0 Z0.2 F3000 E0.3\n"
            "G1 X12.5 Y12.5 E0.5\n"
        )
        result = validate_gcode_for_printer(str(gcode), "bambu_a1")
        assert result["ok"] is False
        assert result["error_code"] == "OFF_BED_GEOMETRY"

    def test_valid_gcode_passes(self, tmp_path):
        gcode = tmp_path / "good.gcode"
        gcode.write_text(
            "G28\n"
            ";LAYER_CHANGE\n"
            "G1 X128 Y128 Z0.2 F3000 E0.3\n"
            "G1 X140 Y140 E0.5\n"
        )
        result = validate_gcode_for_printer(str(gcode), "bambu_a1")
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Full mesh-file validation
# ---------------------------------------------------------------------------

class TestValidateMeshForPrinter:
    def test_incident_reproduction_from_stl(self, tmp_path):
        """STL with the exact bbox that caused incident #0."""
        stl = tmp_path / "disc.stl"
        _write_cube_stl(stl, (-12.5, 12.5), (-12.5, 12.5), (0, 5))
        result = validate_mesh_for_printer(str(stl), "bambu_a1")
        assert result["ok"] is False
        assert result["error_code"] == "OFF_BED_GEOMETRY"
        # Translation should bring it on-bed
        tx, ty, _tz = result["suggested_translate"]
        assert tx == pytest.approx(128.0)
        assert ty == pytest.approx(128.0)

    def test_valid_stl_passes(self, tmp_path):
        stl = tmp_path / "placed.stl"
        _write_cube_stl(stl, (120, 140), (120, 140), (0, 5))
        result = validate_mesh_for_printer(str(stl), "bambu_a1")
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Auto-translate fixes off-bed geometry
# ---------------------------------------------------------------------------

class TestCheckGcodeHasHoming:
    """Regression for the REAL root cause of incident #0: a 3MF with
    no G28 homing sequence caused the Bambu A1 nozzle to drive to
    an uninitialized position and slam into the purge tool."""

    def test_no_g28_is_rejected(self, tmp_path):
        """PrusaSlicer-native output without Bambu start-gcode has no
        G28 — this must be rejected."""
        gcode = tmp_path / "no_home.gcode"
        gcode.write_text(
            "M190 S65\n"
            "M104 S220\n"
            "M109 S220\n"
            "G21\n"
            "G90\n"
            "M83\n"
            "G1 Z0.4 F24000\n"
            "G1 X114 Y114 E0.5\n"  # print move without homing!
        )
        result = check_gcode_has_homing(str(gcode))
        assert result["ok"] is False
        assert result["error_code"] == "NO_HOMING_SEQUENCE"

    def test_g28_before_print_passes(self, tmp_path):
        gcode = tmp_path / "homed.gcode"
        gcode.write_text(
            "M190 S65\n"
            "M109 S220\n"
            "G28 ; home all axes\n"
            "G1 Z0.4 F24000\n"
            "G1 X114 Y114 E0.5\n"
        )
        result = check_gcode_has_homing(str(gcode))
        assert result["ok"] is True

    def test_g28_after_first_print_move_is_rejected(self, tmp_path):
        """Unusual: homing AFTER a print move is also dangerous."""
        gcode = tmp_path / "late_home.gcode"
        gcode.write_text(
            "M109 S220\n"
            "G1 X114 Y114 E0.5\n"  # print move first
            "G28\n"                  # homing after — doesn't save us
        )
        result = check_gcode_has_homing(str(gcode))
        assert result["ok"] is False

    def test_retraction_without_homing_is_not_a_print_move(self, tmp_path):
        """G1 E-0.8 F1800 is retraction only, no X/Y movement — should
        not trigger the homing requirement on its own."""
        gcode = tmp_path / "retract_only.gcode"
        gcode.write_text(
            "M109 S220\n"
            "G1 E-0.8 F1800\n"  # retract — no X/Y, not a print move
        )
        result = check_gcode_has_homing(str(gcode))
        assert result["ok"] is True  # no print moves, not blocked

    def test_incident_0_3mf_would_be_rejected(self, tmp_path):
        """Reproduces the exact shape of the 3MF that caused incident #0:
        bed-centered print moves but no G28, no Bambu start-gcode."""
        import zipfile
        threemf = tmp_path / "incident_like.gcode.3mf"
        bad_gcode = (
            "; generated by PrusaSlicer 2.9.4\n"
            "M190 S65\n"
            "M104 S220\n"
            "M109 S220\n"
            "G21\n"
            "G90\n"
            "M83\n"
            "G1 Z0.4 F24000\n"          # z move without homing
            "G1 X114.658 Y114.939\n"    # print pos, no home
            "G1 X116.4 Y113.37 E0.07\n"
        )
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", bad_gcode)
        result = check_gcode_has_homing(str(threemf), source="3mf")
        assert result["ok"] is False
        assert result["error_code"] == "NO_HOMING_SEQUENCE"


class TestApplyTranslationToStl:
    def test_translation_fixes_origin_centered_disc(self, tmp_path):
        """Full loop: bad STL → compute translation → apply → re-verify."""
        stl = tmp_path / "before.stl"
        _write_cube_stl(stl, (-12.5, 12.5), (-12.5, 12.5), (0, 5))
        # Validate → get translation
        result = validate_mesh_for_printer(str(stl), "bambu_a1")
        assert result["ok"] is False
        translation = result["suggested_translate"]
        # Apply
        out = tmp_path / "after.stl"
        apply_translation_to_stl(str(stl), translation, str(out))
        # Re-validate — should now pass
        result2 = validate_mesh_for_printer(str(out), "bambu_a1")
        assert result2["ok"] is True
