"""Parts sliced one at a time merge into one print, whichever slicer wrote them.

The merged file is read here as a printer would run it: every layer of every
part is there, the heater is switched off and the motors released once, at
the end, and nothing is printed after them.  The cases with a slicer's name
slice two real parts with it and are skipped where it is not installed; the
rest run everywhere on files written in each slicer's own layer marks.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from kiln.slicer import merge_multipart_gcode

#: How each slicer marks a layer and names its height.
_MARKS = {
    "PrusaSlicer and OrcaSlicer": (";LAYER_CHANGE", ";Z:{z}"),
    "Bambu Studio": ("; CHANGE_LAYER", "; Z_HEIGHT: {z}"),
}


def _part(path: Path, marks: tuple[str, str], x: float, layers: int, *, ending: bool = True) -> str:
    """One sliced part: a start, *layers* layers at *x*, and its ending."""
    change, height = marks
    lines = ["; a sliced part", "M83", "G28", "M104 S210"]
    for n in range(1, layers + 1):
        z = round(0.2 * n, 2)
        lines += [change, height.format(z=z), f"G1 Z{z} F600", f"G1 X{x} Y{10 + n} E0.5", "G1 E-0.8 F1800"]
    if ending:
        lines += ["M107", "M104 S0 ; heater off", "G28 X0", "M84 ; motors off", "; filament used [mm] = 12.50"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _count(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, re.M))


def _laid_after(text: str, pattern: str) -> int:
    """Lines that lay plastic after the first line matching *pattern*."""
    found = re.search(pattern, text, re.M)
    return _count(text[found.end():], r"^G1 X[\d.]+ Y[\d.]+ E0\.5") if found else 0


@pytest.mark.parametrize("slicer", list(_MARKS))
def test_two_parts_merge_into_one_print(tmp_path, slicer):
    """Mutation pin: with each part's ending left behind its last layer,
    the heater is switched off before the second part is printed."""
    marks = _MARKS[slicer]
    parts = [
        {"gcode_path": _part(tmp_path / "body.gcode", marks, 10, 5), "tool_index": 0, "name": "body"},
        {"gcode_path": _part(tmp_path / "badge.gcode", marks, 60, 3), "tool_index": 1, "name": "badge"},
    ]
    merged = merge_multipart_gcode(parts, output_path=str(tmp_path / "merged.gcode"))
    text = Path(merged["output_path"]).read_text(encoding="utf-8")

    assert merged["total_layers"] == 5
    assert _count(text, r"^G1 X10 Y\d+ E0\.5") == 5 and _count(text, r"^G1 X60 Y\d+ E0\.5") == 3
    assert _count(text, r"^M104 S0\b") == 1 and _count(text, r"^M84\b") == 1
    assert _laid_after(text, r"^M104 S0\b") == 0, "plastic is laid after the heater is switched off"
    assert _count(text, r"^G28\s*$") == 1, "the head is sent home again part-way through"
    assert re.findall(r"^T\d", text, re.M) == ["T0", "T1", "T0"]


def test_a_file_with_no_layer_marks_is_refused_in_plain_words(tmp_path):
    bare = tmp_path / "bare.gcode"
    bare.write_text("G28\nG1 X10 Y10 E0.5\n", encoding="utf-8")
    parts = [{"gcode_path": str(bare), "tool_index": 0, "name": "body"}]
    with pytest.raises(ValueError) as refused:
        merge_multipart_gcode(parts, output_path=str(tmp_path / "merged.gcode"))
    said = str(refused.value)
    assert said.startswith("Kiln couldn't find any layers in the G-code for body, so the parts are not merged.")
    assert "LAYER_CHANGE" not in said and "Ensure" not in said
    assert not (tmp_path / "merged.gcode").exists()


def test_a_merge_that_would_stop_part_way_through_is_not_handed_over(tmp_path):
    """A part whose own layers switch the heater off cannot be made one
    print by cutting its ending: the merged file is refused and not written."""
    marks = _MARKS["Bambu Studio"]
    stops = Path(_part(tmp_path / "stops.gcode", marks, 10, 4))
    stops.write_text(stops.read_text().replace("G1 Z0.4 F600", "M104 S0\nG1 Z0.4 F600"), encoding="utf-8")
    parts = [
        {"gcode_path": str(stops), "tool_index": 0, "name": "body"},
        {"gcode_path": _part(tmp_path / "badge.gcode", marks, 60, 2), "tool_index": 1, "name": "badge"},
    ]
    with pytest.raises(ValueError, match="stops or starts over part-way through.*so it is not handed over"):
        merge_multipart_gcode(parts, output_path=str(tmp_path / "merged.gcode"))
    assert not (tmp_path / "merged.gcode").exists()


_SLICERS = {
    "PrusaSlicer": "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
    "OrcaSlicer": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "Bambu Studio": "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
}
_INSTALLED = {name: path for name, path in _SLICERS.items() if os.access(path, os.X_OK)}


@pytest.mark.skipif(not _INSTALLED, reason="no slicer installed: public CI has none")
@pytest.mark.parametrize("slicer", list(_INSTALLED) or ["none installed"])
def test_two_real_slices_merge_with_every_layer_and_one_ending(tmp_path, monkeypatch, slicer):
    trimesh = pytest.importorskip("trimesh")
    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")

    layer = r"^;\s*(?:LAYER_CHANGE|CHANGE_LAYER)\s*$"
    ending = r"^; (?:prusaslicer_config = begin|CONFIG_BLOCK_START)"
    parts, layers = [], []
    for tool, (name, height) in enumerate((("body", 6.0), ("badge", 3.0))):
        box = trimesh.creation.box(extents=(15, 15, height))
        box.apply_translation((0, 0, height / 2))
        stl = tmp_path / f"{name}.stl"
        box.export(str(stl))
        sliced = slice_file(str(stl), output_dir=str(tmp_path / name), profile=resolve_slicer_profile("bambu_a1"),
                            slicer_path=_INSTALLED[slicer], timeout=600)
        own = Path(sliced.output_path).read_text(encoding="utf-8", errors="replace")
        assert _count(own, ending) == 1, f"{slicer} wrote no settings block to tell an ending by"
        layers.append(_count(own, layer))
        parts.append({"gcode_path": sliced.output_path, "tool_index": tool, "name": name})

    merged = merge_multipart_gcode(parts, output_path=str(tmp_path / "merged.gcode"))
    text = Path(merged["output_path"]).read_text(encoding="utf-8", errors="replace")
    assert min(layers) > 5 and _count(text, layer) == sum(layers), "a layer is missing or there twice"
    assert _count(text, ending) == 1, "more than one part's ending rides in the merged file"
    assert _count(text, r"^M84\b") <= 1 and _count(text, r"^M104 S0\b") <= 1
    assert merged["total_layers"] == max(layers)
