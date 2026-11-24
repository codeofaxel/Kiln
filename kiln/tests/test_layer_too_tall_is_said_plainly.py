"""A layer as tall as the nozzle is refused in the person's words.

Each slicer refuses it in its own log line; the answer is one plain sentence
with what to change, whichever slicer gave it.  The cases with a slicer's
name run it and are skipped where it is not installed.
"""

from __future__ import annotations

import os

import pytest

from kiln.slicer import REFUSED_LAYER_TOO_TALL, SlicerRefusal, _orca_refusal

_SAID = (
    "refused to slice: the layer height asked for is too tall for the nozzle. A layer has to be thinner than "
    "the line the nozzle lays. Choose a lower layer height and slice again."
)


@pytest.mark.parametrize("log", [
    "[2026-11-24 12:19:11.258748] [0x01] [error]   got error when validate: Layer height cannot exceed nozzle diameter.",
    "[BambuStudio.cpp:7542 run] got error when validate: Too small line width\nrun found error, exit",
])
def test_the_orca_familys_log_line_becomes_one_plain_sentence(log):
    refusal = _orca_refusal("OrcaSlicer", 1, log)
    assert isinstance(refusal, SlicerRefusal)
    assert (refusal.kind, refusal.code) == (REFUSED_LAYER_TOO_TALL, "LAYER_TOO_TALL")
    assert str(refusal) == f"OrcaSlicer {_SAID}"


_SLICERS = {
    "PrusaSlicer": "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
    "OrcaSlicer": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "Bambu Studio": "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
}
_INSTALLED = {name: path for name, path in _SLICERS.items() if os.access(path, os.X_OK)}


@pytest.mark.skipif(not _INSTALLED, reason="no slicer installed: public CI has none")
@pytest.mark.parametrize("slicer", list(_INSTALLED) or ["none installed"])
def test_a_layer_taller_than_the_nozzle_is_refused_in_plain_words(tmp_path, monkeypatch, slicer):
    trimesh = pytest.importorskip("trimesh")
    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import profile_with_overrides, resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    cube = trimesh.creation.box(extents=(20, 20, 5))
    cube.apply_translation((0, 0, 2.5))
    cube.export(str(tmp_path / "cube.stl"))
    profile = profile_with_overrides(resolve_slicer_profile("bambu_a1"), {"layer_height": "0.5"})
    with pytest.raises(SlicerRefusal) as refused:
        slice_file(str(tmp_path / "cube.stl"), output_dir=str(tmp_path / "out"), profile=profile,
                   slicer_path=_INSTALLED[slicer], timeout=300)
    said = str(refused.value)
    assert said.endswith(_SAID), said
    assert "[error]" not in said and "validate" not in said and "It said" not in said
    assert not list((tmp_path / "out").glob("*.gcode"))
