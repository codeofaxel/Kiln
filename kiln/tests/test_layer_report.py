"""Tests for kiln.layer_report.analyze_layers.

Covers slicer-format compatibility (PrusaSlicer, Bambu Studio,
Cura, Marlin-implicit) and structural warnings.  Synthetic gcode
fixtures keep the tests self-contained — no real slicer runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info < (3, 10):
    pytest.skip("kiln requires Python 3.10+", allow_module_level=True)

from kiln.layer_report import (  # noqa: E402
    LayerReport,
    LayerSummary,
    analyze_layers,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _prusaslicer_gcode(
    path: Path, *, total_layers: int = 10, layer_height: float = 0.2,
    top_layers: int = 3, bottom_layers: int = 3,
) -> str:
    """Synthetic PrusaSlicer-flavoured gcode with ;LAYER_CHANGE + ;Z:
    + ;TYPE: markers — the fullest case the parser handles."""
    lines = [
        "M140 S60",
        "M104 S210",
        "M190 S60",
        "M109 S210",
        "M83",
    ]
    for i in range(1, total_layers + 1):
        z = i * layer_height
        lines.append(";LAYER_CHANGE")
        lines.append(f";Z:{z:.3f}")
        lines.append(f"G1 Z{z:.3f} F600")
        # Layer type
        if i <= bottom_layers:
            lines.append(";TYPE:Solid infill")
            lines.append(";WIDTH:0.450000")
        elif i > total_layers - top_layers:
            lines.append(";TYPE:Top solid infill")
        else:
            lines.append(";TYPE:Internal infill")
        # A perimeter on every layer for the NO_PERIMETER warning
        # suppression.
        lines.append(";TYPE:Perimeter")
        lines.append(f"G1 X10 Y10 E0.5 F1500")
        lines.append(f"G1 X90 Y10 E0.5 F1500")
        lines.append(f"G1 X90 Y90 E0.5 F1500")
        lines.append(f"G1 X10 Y90 E0.5 F1500")
        # One travel move between layers.
        lines.append("G1 X10 Y10 F6000")
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _bambu_gcode(path: Path) -> str:
    """Bambu-flavoured gcode — uses ``Outer wall``, ``Sparse infill``,
    ``Top surface`` typing instead of PrusaSlicer's tokens."""
    lines = [
        "M140 S65",
        "M104 S220",
    ]
    for i in range(1, 8):
        z = i * 0.2
        lines.append(";LAYER_CHANGE")
        lines.append(f";Z:{z:.3f}")
        lines.append(f"G1 Z{z:.3f} F600")
        lines.append(";TYPE:Outer wall")
        lines.append(f"G1 X20 Y20 E0.3 F2000")
        lines.append(f"G1 X80 Y80 E0.3 F2000")
        if i >= 6:
            lines.append(";TYPE:Top surface")
        else:
            lines.append(";TYPE:Sparse infill")
        lines.append(f"G1 X50 Y50 E0.3 F2000")
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _marlin_implicit_gcode(path: Path) -> str:
    """Gcode with NO ;LAYER_CHANGE markers — layer boundaries are
    inferred from Z-rising G1 moves only.  Tests the fallback path."""
    lines = ["M140 S60", "M104 S210"]
    for i in range(1, 6):
        z = i * 0.25
        lines.append(f"G1 Z{z:.3f} F600")
        lines.append(f"G1 X10 Y10 E0.5 F1500")
        lines.append(f"G1 X40 Y40 E0.5 F1500")
    path.write_text("\n".join(lines) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_prusaslicer_full_tagging_produces_complete_report(tmp_path):
    """Happy path — a fully-tagged PrusaSlicer file produces every
    flag populated + no missing-structure warnings."""
    gcode = _prusaslicer_gcode(
        tmp_path / "part.gcode",
        total_layers=10, layer_height=0.2,
        top_layers=3, bottom_layers=3,
    )
    report = analyze_layers(gcode)
    assert isinstance(report, LayerReport)
    assert report.total_layers == 10
    assert report.z_min_mm == pytest.approx(0.2)
    assert report.z_max_mm == pytest.approx(2.0)
    assert report.layer_height_mm == pytest.approx(0.2)
    assert report.hotend_set is True
    assert report.bed_set is True
    assert report.hotend_setpoint_c == pytest.approx(210)
    assert report.bed_setpoint_c == pytest.approx(60)
    assert report.has_perimeter is True
    assert report.has_top_solid is True
    assert report.z_continuous is True
    assert report.xy_bounds == (10.0, 10.0, 90.0, 90.0)
    # No structural warnings when the file is fully formed (only
    # possible warnings fire from missing setpoints / missing types).
    assert not any(
        w.startswith(("NO_HOTEND", "NO_BED", "NO_PERIMETER", "NO_TOP"))
        for w in report.warnings
    )


def test_bambu_slicer_type_aliases_normalise_correctly(tmp_path):
    """Bambu's ``Outer wall`` / ``Top surface`` / ``Sparse infill``
    should map to perimeter / top_solid / infill respectively."""
    gcode = _bambu_gcode(tmp_path / "bambu.gcode")
    report = analyze_layers(gcode)
    assert report.total_layers == 7
    assert report.has_perimeter is True
    assert report.has_top_solid is True
    # Last 2 layers should dominate as top_solid
    top_typed = [L for L in report.layers if L.type == "top_solid"]
    assert len(top_typed) >= 2


def test_marlin_implicit_layer_tracking_falls_back_to_z(tmp_path):
    """No ;LAYER_CHANGE markers — the parser derives layer count from
    rising G1 Z moves.  Also triggers NO_PERIMETER + NO_TOP_SOLID
    because there are no ;TYPE: tags at all."""
    gcode = _marlin_implicit_gcode(tmp_path / "marlin.gcode")
    report = analyze_layers(gcode)
    assert report.total_layers == 5
    assert report.layer_height_mm == pytest.approx(0.25)
    # No TYPE tags → all layers land as "other" + warnings fire.
    assert report.has_perimeter is False
    assert any("NO_PERIMETER" in w for w in report.warnings)
    assert any("NO_TOP_SOLID" in w for w in report.warnings)


def test_missing_setpoints_trigger_warnings(tmp_path):
    """A gcode without M104 / M140 should surface NO_HOTEND_SETPOINT
    and NO_BED_SETPOINT."""
    lines = ["M83"]
    for i in range(1, 4):
        z = i * 0.2
        lines.append(";LAYER_CHANGE")
        lines.append(f";Z:{z:.3f}")
        lines.append(f"G1 Z{z:.3f} F600")
        lines.append(";TYPE:Perimeter")
        lines.append(f"G1 X10 Y10 E0.5 F1500")
    (tmp_path / "cold.gcode").write_text("\n".join(lines) + "\n")
    report = analyze_layers(str(tmp_path / "cold.gcode"))
    assert report.hotend_set is False
    assert report.bed_set is False
    assert any("NO_HOTEND_SETPOINT" in w for w in report.warnings)
    assert any("NO_BED_SETPOINT" in w for w in report.warnings)


def test_z_gap_detected_and_warned(tmp_path):
    """A 0.2mm-layer file that jumps 1.0mm between two layers fires
    Z_GAP + sets z_continuous=False."""
    lines = ["M140 S60", "M104 S210", "M83"]
    zs = [0.2, 0.4, 1.4, 1.6]  # big gap between layer 2 and 3
    for i, z in enumerate(zs, 1):
        lines.append(";LAYER_CHANGE")
        lines.append(f";Z:{z:.3f}")
        lines.append(f"G1 Z{z:.3f} F600")
        lines.append(";TYPE:Perimeter")
        lines.append(f"G1 X10 Y10 E0.5 F1500")
    (tmp_path / "gap.gcode").write_text("\n".join(lines) + "\n")
    report = analyze_layers(str(tmp_path / "gap.gcode"))
    assert report.z_continuous is False
    assert any("Z_GAP" in w for w in report.warnings)


def test_missing_gcode_raises_file_not_found(tmp_path):
    """Non-existent path → FileNotFoundError (not a LayerReport)."""
    with pytest.raises(FileNotFoundError):
        analyze_layers(str(tmp_path / "does_not_exist.gcode"))


def test_no_g1_moves_raises_value_error(tmp_path):
    """A file with only comments / no G1 → ValueError, not a report."""
    (tmp_path / "empty.gcode").write_text("; header only\n; no moves\n")
    with pytest.raises(ValueError):
        analyze_layers(str(tmp_path / "empty.gcode"))


def test_layer_xy_bounds_track_per_layer_extrusions(tmp_path):
    """Each LayerSummary.xy_bounds should reflect only that layer's
    extrusion XY (not the whole file's envelope)."""
    lines = ["M140 S60", "M104 S210", "M83"]
    # Layer 1 at (10..20, 10..20); Layer 2 at (50..60, 50..60)
    lines += [";LAYER_CHANGE", ";Z:0.2", "G1 Z0.2", ";TYPE:Perimeter",
              "G1 X10 Y10 E0.5", "G1 X20 Y20 E0.5"]
    lines += [";LAYER_CHANGE", ";Z:0.4", "G1 Z0.4", ";TYPE:Perimeter",
              "G1 X50 Y50 E0.5", "G1 X60 Y60 E0.5"]
    (tmp_path / "per_layer.gcode").write_text("\n".join(lines) + "\n")
    report = analyze_layers(str(tmp_path / "per_layer.gcode"))
    assert report.total_layers == 2
    assert report.layers[0].xy_bounds == (10.0, 10.0, 20.0, 20.0)
    assert report.layers[1].xy_bounds == (50.0, 50.0, 60.0, 60.0)
    # File-level envelope spans both.
    assert report.xy_bounds == (10.0, 10.0, 60.0, 60.0)


def test_extrude_vs_travel_classification(tmp_path):
    """G1 with E>0 = extrude; G1 without E (or E<=0) = travel (not
    counted as extrude).  Retracts (E<0, no XY) don't count as either."""
    lines = [
        "M140 S60", "M104 S210", "M83",
        ";LAYER_CHANGE", ";Z:0.2", "G1 Z0.2",
        ";TYPE:Perimeter",
        "G1 X10 Y10 E0.5 F1500",       # extrude
        "G1 X20 Y20 E0.5 F1500",       # extrude
        "G1 E-0.8 F2400",              # retract — not travel, not extrude
        "G1 X30 Y30 F6000",            # travel
        "G1 E0.8 F2400",               # prime — same
        "G1 X40 Y40 E0.5 F1500",       # extrude
    ]
    (tmp_path / "mix.gcode").write_text("\n".join(lines) + "\n")
    report = analyze_layers(str(tmp_path / "mix.gcode"))
    L = report.layers[0]
    assert L.extrude_moves == 3
    assert L.travel_moves == 1
