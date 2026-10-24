"""A multicolour slice keeps its colours on any printer profile, the generic one included.

Kiln slices a multicolour file with OrcaSlicer, which keeps every colour and
builds a prime tower beside the part.  A printer whose own profile is not on
this computer (a plain install, signed out or offline) slices with Kiln's
generic profile, and the generic profile extrudes in absolute distances,
which a prime tower is not built from: the slice failed, for every printer,
whatever it was.  A slice that places a prime tower now extrudes in relative
distances, reset every layer, whatever the profile says.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from kiln.slicer_orca import settings_to_orca_presets

_ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
needs_orca = pytest.mark.skipif(
    not (os.path.isfile(_ORCA) and os.access(_ORCA, os.X_OK)), reason="OrcaSlicer not installed",
)

_COLOURS = ["#F2F2F2", "#D32F2F", "#1A1A1A"]
#: A bed's middle, less half the part, so the skirt and the tower both land on it.
_MID_BED = (113.0, 113.0)
#: Absolute extrusion, as the generic profile has it: the key is simply absent.
_ABSOLUTE = {
    "nozzle_diameter": "0.4",
    "layer_height": "0.2",
    "bed_shape": "0x0,256x0,256x256,0x256",
    "temperature": "220",
    "bed_temperature": "60",
    "gcode_flavor": "marlin",
}


def _painted_3mf(tmp_path: Path) -> str:
    """A tetrahedron with one colour per side face, mid-bed: every layer
    touches all three colours, so a slice that keeps them must change tools."""
    from kiln.multicolor_3mf import compose_painted_3mf

    tris = [
        ((0, 0, 0), (30, 0, 0), (15, 30, 0)),
        ((0, 0, 0), (15, 30, 0), (15, 12, 24)),
        ((30, 0, 0), (15, 12, 24), (15, 30, 0)),
        ((0, 0, 0), (15, 12, 24), (30, 0, 0)),
    ]
    dx, dy = _MID_BED
    result = compose_painted_3mf(
        [tuple((x + dx, y + dy, z) for x, y, z in tri) for tri in tris],
        [_COLOURS[0], _COLOURS[0], _COLOURS[1], _COLOURS[2]],
        output_path=str(tmp_path / "painted.3mf"),
        name="tetra",
    )
    assert result["success"], result
    return result["output_path"]


def _what_the_gcode_does(gcode_path: str) -> tuple[list[str], int, bool, bool]:
    """The tools a slice used, how often it changed between them, whether it
    extrudes in relative distances, and whether it built a prime tower."""
    text = Path(gcode_path).read_text(errors="replace")
    tools = re.findall(r"^T(\d+)\b", text, re.M)
    changes = sum(1 for a, b in zip(tools, tools[1:], strict=False) if a != b)
    relative = bool(re.search(r"^M83\b", text, re.M))
    tower = bool(re.search(r"^;\s*TYPE:\s*Prime tower", text, re.M | re.I))
    return sorted(set(tools)), changes, relative, tower


@pytest.fixture
def no_catalogue(monkeypatch):
    """A plain install that cannot reach Kiln's printer catalogue, signed out
    or offline: a printer named by its id slices with the generic profile,
    laid on that printer's own bed."""
    import kiln.slicer_profiles as profiles

    monkeypatch.setattr(profiles, "_fetch_profile", lambda spelling: None)
    monkeypatch.setattr(profiles, "_catalogue_here", lambda: False)
    profiles._load()
    monkeypatch.delitem(profiles._cache, "bambu_a1", raising=False)
    monkeypatch.setattr(profiles, "_temp_cache", {})
    assert profiles.get_slicer_profile("bambu_a1").id == "default"  # control: really the generic profile


class TestThePresetsATowerIsBuiltFrom:
    def test_a_tower_on_an_absolute_profile_extrudes_in_relative_distances(self):
        presets = settings_to_orca_presets(_ABSOLUTE, filament_colors=_COLOURS, wipe_tower_xy=(150.0, 150.0))
        assert presets.machine["use_relative_e_distances"] == "1"
        assert "G92 E0" in presets.machine["layer_change_gcode"]

    def test_a_layer_gcode_of_its_own_is_kept(self):
        own = {**_ABSOLUTE, "layer_gcode": "M117 layer"}
        presets = settings_to_orca_presets(own, filament_colors=_COLOURS, wipe_tower_xy=(150.0, 150.0))
        assert presets.machine["layer_change_gcode"].splitlines() == ["G92 E0", "M117 layer"]

    def test_the_callers_settings_are_not_changed(self):
        settings = dict(_ABSOLUTE)
        settings_to_orca_presets(settings, filament_colors=_COLOURS, wipe_tower_xy=(150.0, 150.0))
        assert settings == _ABSOLUTE

    @pytest.mark.parametrize(
        ("colours", "tower"),
        [(_COLOURS, None), (["#F2F2F2"], (150.0, 150.0)), (None, None)],
        ids=["colours, no tower", "one colour", "single filament"],
    )
    def test_with_no_tower_the_profile_keeps_its_own_extrusion(self, colours, tower):
        presets = settings_to_orca_presets(_ABSOLUTE, filament_colors=colours, wipe_tower_xy=tower)
        assert presets.machine["use_relative_e_distances"] == "0"
        assert "layer_change_gcode" not in presets.machine


@needs_orca
def test_a_multicolour_file_slices_on_the_generic_profile(tmp_path, no_catalogue):
    """slice_file on the profile a Bambu Lab A1 gets when its own is not on
    this computer: three colours in, three tools out, the tower built from
    relative extrusion."""
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    profile = resolve_slicer_profile("bambu_a1")
    assert "bed_shape" in Path(profile).read_text()  # control: the bed is there, so a tower is placed
    result = slice_file(
        _painted_3mf(tmp_path),
        output_dir=str(tmp_path / "out"),
        profile=profile,
        slicer_path=_ORCA,
        timeout=600,
    )
    assert result.success, result.message
    tools, changes, relative, tower = _what_the_gcode_does(result.output_path)
    assert tools == ["0", "1", "2"] and changes > 0
    assert relative and tower


@needs_orca
def test_a_plain_install_that_cannot_reach_the_catalogue_slices_in_colour(tmp_path, no_catalogue):
    """Through slice_model, as a plain install signed out or offline answers
    it: the Bambu Lab A1 named, its profile not on this computer, so the
    generic one is used, and the colours still slice."""
    from kiln import server
    from tests import _real_exports as rx

    with rx.without_kiln_pro():
        reply = rx.call(server.mcp, "slice_model", {
            "input_path": _painted_3mf(tmp_path),
            "printer_id": "bambu_a1",
            "output_dir": str(tmp_path / "out"),
        })
    assert reply.get("success") is True, reply.get("error") or reply
    gcode = reply.get("gcode_path") or reply.get("output_path")
    if gcode and gcode.endswith(".gcode.3mf"):
        gcode = str(Path(gcode).with_suffix("").with_suffix(".gcode"))
    tools, changes, relative, tower = _what_the_gcode_does(gcode)
    assert tools == ["0", "1", "2"] and changes > 0
    assert relative and tower
