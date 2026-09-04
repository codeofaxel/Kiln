"""Every machine-specific slicer value for the four H2-family printers is
pinned to a number read off that machine, not inherited from a sibling.

These four profiles were first written by copying the H2S's and overriding
the fields the author was thinking about.  Everything not thought about
inherited another machine's value while looking deliberate, and two of those
were physical: the bed rectangle and the maximum part height.  A part sized
against a 340x320 bed on a machine whose nozzles reach 300x320 gets placed
off the plate, and a 340 mm height ceiling on a 256 mm machine accepts a
part 84 mm too tall.

The lesson is narrow and worth keeping: a cloned profile is a set of
uncited claims about someone else's hardware.  This file makes the
machine-specific subset explicit, so adding a printer by copying one again
fails here rather than at somebody's nozzle.

Values below are read from the vendor's own shipped machine profiles
(BambuStudio ``resources/profiles/BBL/machine/<model> 0.4 nozzle.json``)
and from each model's published specifications.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "src" / "kiln" / "data"

# (bed_x, bed_y, max_print_height) -- the envelope Kiln slices against.
# Deliberately the SMALLEST supported mode, never the marketing figure: on a
# dual-nozzle machine a part that only the wider mode could print must be
# refused, not placed.
GEOMETRY = {
    "bambu_h2d": (300, 320, 320),
    "bambu_h2d_pro": (300, 320, 320),
    "bambu_h2c": (300, 320, 320),
    # The vendor's own profile says printable_height 261 and the main nozzle
    # reaches 256x256x260.  Kiln uses the AUXILIARY/dual envelope, which is
    # shorter and narrower, because the second nozzle cannot reach the rest.
    "bambu_x2d": (235, 256, 256),
}

MODELS = tuple(GEOMETRY)


@pytest.fixture(scope="module")
def slicer() -> dict:
    raw = json.loads((DATA / "slicer_profiles.json").read_text(encoding="utf-8"))
    return raw.get("profiles", raw)


@pytest.fixture(scope="module")
def catalogue() -> dict:
    return json.loads((DATA / "printer_intelligence.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("model", MODELS)
def test_bed_rectangle_is_this_machines_own(slicer, model: str):
    x, y, _ = GEOMETRY[model]
    assert slicer[model]["settings"]["bed_shape"] == f"0x0,{x}x0,{x}x{y},0x{y}"


@pytest.mark.parametrize("model", MODELS)
def test_height_ceiling_is_this_machines_own(slicer, model: str):
    assert int(slicer[model]["settings"]["max_print_height"]) == GEOMETRY[model][2]


@pytest.mark.parametrize("model", MODELS)
def test_slicer_geometry_agrees_with_the_catalogue(slicer, catalogue, model: str):
    """The two must never disagree: one guards placement, the other refuses
    an oversize part, and a part slipping between them reaches the printer."""
    bed = slicer[model]["settings"]["bed_shape"]
    match = re.fullmatch(r"0x0,(\d+)x0,\d+x(\d+),0x\d+", bed)
    assert match, f"{model} bed_shape is not a plain rectangle: {bed}"
    x, y = int(match.group(1)), int(match.group(2))
    z = int(slicer[model]["settings"]["max_print_height"])
    assert [x, y, z] == catalogue[model]["build_volume_mm"]


@pytest.mark.parametrize("model", MODELS)
def test_no_h2s_dimension_survived_the_clone(slicer, model: str):
    """The two values that were actually wrong, pinned by their wrong value.

    340x320 and 340 are the H2S's. Neither belongs to any of these four.
    """
    settings = slicer[model]["settings"]
    assert "340x0,340x320" not in settings["bed_shape"], (
        f"{model} still carries the H2S bed rectangle"
    )
    assert int(settings["max_print_height"]) != 340, (
        f"{model} still carries the H2S height ceiling"
    )


@pytest.mark.parametrize("model", MODELS)
def test_retraction_matches_the_machines_main_extruder(slicer, model: str):
    """0.8mm at 30mm/s, read from each machine's own shipped profile.

    Recorded because the X2D makes the point: its profile is
    ``['0.8','0.8','2','2']`` at ``['30','30','20','20']``, because its right
    hotend is fed by a REMOTE extruder on the rear panel that needs 2mm at
    20mm/s.  Kiln stores one value, and the one it stores is the main
    direct-drive nozzle's -- which is correct, and is a fact about this
    machine rather than a value inherited from a direct-drive sibling.
    """
    settings = slicer[model]["settings"]
    assert float(settings["retract_length"]) == 0.8
    assert float(settings["retract_speed"]) == 30


@pytest.mark.parametrize("model", MODELS)
def test_bambu_transport_invariants_hold(slicer, model: str):
    """Empty start/end G-code and relative E are required by the 3MF path."""
    settings = slicer[model]["settings"]
    assert settings["use_relative_e_distances"] == "1"
    assert settings["start_gcode"] == ""
    assert settings["end_gcode"] == ""
    assert float(settings["nozzle_diameter"]) == 0.4
