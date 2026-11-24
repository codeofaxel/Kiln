"""Copies of a part, each in its own colour, come back as one print.

Read from the file each installed slicer writes: the head is sent home, the
heater switched off and the motors released no more than once each, every
layer is there once and in order, and a filament is called for each copy.  A slicer that prints
the plate in one filament is refused in plain words, with no file left.
Skipped where no slicer is installed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.slicer import SlicerError, slice_multicolor_copies  # noqa: E402
from kiln.slicer_profiles import resolve_slicer_profile  # noqa: E402

_PRUSA = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
_EACH_ITS_OWN = {
    "OrcaSlicer": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "Bambu Studio": "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
}
_INSTALLED = {name: path for name, path in _EACH_ITS_OWN.items() if os.access(path, os.X_OK)}
#: A 20 mm square 5 mm tall at 0.2 mm layers over a first layer of 0.2 or 0.25 mm.
_LAYERS = (24, 25)


@pytest.fixture
def _cube(tmp_path, monkeypatch) -> str:
    from kiln import monitor_twin

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    cube = trimesh.creation.box(extents=(20, 20, 5))
    cube.apply_translation((0, 0, 2.5))
    path = tmp_path / "cube.stl"
    cube.export(str(path))
    return str(path)


def _count(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, re.M))


@pytest.mark.skipif(not _INSTALLED, reason="neither OrcaSlicer nor Bambu Studio is installed")
@pytest.mark.parametrize("slicer", list(_INSTALLED) or ["none installed"])
def test_two_copies_in_two_colours_are_one_print(tmp_path, _cube, slicer):
    result = slice_multicolor_copies(
        _cube, 2, printer_id="bambu_a1", profile=resolve_slicer_profile("bambu_a1"),
        slicer_path=_INSTALLED[slicer], output_dir=str(tmp_path / "out"),
    )
    assert result.success, result.message
    text = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
    assert _count(text, r"^M84\b") <= 1, "the motors are released more than once"
    assert _count(text, r"^M104 S0\b") <= 1, "the heater is switched off more than once"
    assert _count(text, r"^G28\s*(?:;.*)?$") <= 1, "the head is sent home more than once"
    assert _count(text, r"^;\s*(?:LAYER_CHANGE|CHANGE_LAYER)\s*$") in _LAYERS, "a layer is there twice, or missing"
    heights = [float(z) for z in re.findall(r"^;\s*(?:Z|Z_HEIGHT)\s*:\s*([\d.]+)", text, re.M)]
    assert heights == sorted(heights), "the layers are out of order"
    assert set(re.findall(r"^T[01]\b", text, re.M)) == {"T0", "T1"}, "a copy has no filament of its own"
    assert _count(text, r"^; kiln_lineage = ") == 1, "the file carries more than one record of itself"


@pytest.mark.skipif(not os.access(_PRUSA, os.X_OK), reason="PrusaSlicer not installed")
def test_a_slicer_that_prints_the_plate_in_one_filament_is_refused(tmp_path, _cube):
    with pytest.raises(SlicerError) as refused:
        slice_multicolor_copies(
            _cube, 2, printer_id="bambu_a1", profile=resolve_slicer_profile("bambu_a1"),
            slicer_path=_PRUSA, output_dir=str(tmp_path / "out"),
        )
    said = str(refused.value)
    assert "prints every copy in one filament, so it is not handed over" in said
    assert "slice with one of them (`--slicer`)" in said
    assert not list((tmp_path / "out").glob("*.gcode")), "a file was left behind"
