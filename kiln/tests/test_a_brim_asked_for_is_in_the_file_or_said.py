"""A brim a slice asks for is in the sliced file, or the answer says it is not.

The part is a table top on four small feet: it needs supports under the top
and a brim round the feet.  Read from each slicer's own file: with supports
Kiln decided, the brim round the feet is as much as the same slice draws with
no supports at all; with the caller's own choice of supports that a slicer
drops the brim beside, the answer says the file has no brim and what keeps
it.  Skipped where the slicer is not installed; the last case runs everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiln.slicer import SliceResult, SlicerInfo, _say_brim_left_out

trimesh = pytest.importorskip("trimesh")

_ORCA_FAMILY = {
    "OrcaSlicer": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "Bambu Studio": "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
}
_INSTALLED = {name: path for name, path in _ORCA_FAMILY.items() if os.access(path, os.X_OK)}


def _box(low, high):
    box = trimesh.creation.box(extents=[h - lo for lo, h in zip(low, high, strict=True)])
    box.apply_translation([(lo + h) / 2 for lo, h in zip(low, high, strict=True)])
    return box


@pytest.fixture
def _table(tmp_path, monkeypatch) -> str:
    from kiln import monitor_twin

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    parts = [_box((0, 0, 40), (100, 100, 45))]
    parts += [_box((x, y, 0), (x + 4, y + 4, 40.01)) for x in (0, 96) for y in (0, 96)]
    path = tmp_path / "table.stl"
    trimesh.util.concatenate(parts).export(str(path))
    return str(path)


def _sliced(table: str, out: Path, slicer: str, overrides: dict[str, str]):
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import profile_with_overrides, resolve_slicer_profile

    profile = profile_with_overrides(resolve_slicer_profile("bambu_a1"), {"brim_width": "8", **overrides})
    return slice_file(table, output_dir=str(out), profile=profile, slicer_path=_INSTALLED[slicer],
                      material="PLA", timeout=600)


def _brim_lines(result) -> int:
    from kiln.slicer_geometry import parse_slicer_features

    bucket = parse_slicer_features(result.output_path).buckets.get("brim")
    return len(bucket.segments) // 6 if bucket is not None else 0


@pytest.mark.skipif(not _INSTALLED, reason="neither OrcaSlicer nor Bambu Studio is installed")
@pytest.mark.parametrize("slicer", list(_INSTALLED) or ["none installed"])
def test_supports_kiln_decides_keep_the_brim_whole(tmp_path, _table, slicer):
    held = _sliced(_table, tmp_path / "held", slicer, {})
    bare = _sliced(_table, tmp_path / "bare", slicer, {"support_material": "0"})
    assert held.supports.enabled and held.supports.decided_by == "analysis", held.supports.note
    assert _brim_lines(bare) > 1000, "the slice with no supports draws no brim to compare with"
    assert _brim_lines(held) > 0.9 * _brim_lines(bare), (
        f"{slicer}: {_brim_lines(held)} brim lines beside the supports, {_brim_lines(bare)} without them")
    assert held.brim is None and "no brim" not in held.message


@pytest.mark.skipif("Bambu Studio" not in _INSTALLED, reason="Bambu Studio is not installed")
def test_the_callers_own_supports_that_drop_the_brim_are_said(tmp_path, _table):
    stated = _sliced(_table, tmp_path / "stated", "Bambu Studio",
                     {"support_material": "1", "support_material_style": "grid"})
    assert stated.supports.decided_by == "stated"
    assert _brim_lines(stated) == 0, "Bambu Studio drew the brim beside its normal supports"
    assert stated.brim == {"asked_mm": 8.0, "drawn": False, "said": stated.brim["said"]}
    assert stated.brim["said"].startswith("The sliced file has no brim, though a 8 mm brim was asked for.")
    assert "Tree supports keep it" in stated.brim["said"] and stated.message.endswith(stated.brim["said"])
    assert stated.to_dict()["brim"]["drawn"] is False


_NO_BRIM = ";LAYER_CHANGE\n;Z:0.2\n;TYPE:Perimeter\nG1 X10 Y10 E0.5\nG1 X20 Y10 E0.5\n"


@pytest.mark.parametrize("gcode, said", [
    (_NO_BRIM, True),
    (_NO_BRIM.replace(";TYPE:Perimeter", ";TYPE:Brim"), False),
    ("G1 X10 Y10 E0.5\n", False),
], ids=["no brim in the file", "a brim in the file", "a file with no labels"])
def test_a_file_is_read_for_the_brim_it_was_asked_for(tmp_path, gcode, said):
    path = tmp_path / "part.gcode"
    path.write_text(gcode, encoding="utf-8")
    result = SliceResult(success=True, output_path=str(path), message="Sliced part.stl -> part.gcode")
    _say_brim_left_out(result, 5.0, SlicerInfo(path="/usr/bin/prusa-slicer", name="prusa-slicer"), None)
    assert (result.brim is not None) == said
    if said:
        assert result.message == (
            "Sliced part.stl -> part.gcode The sliced file has no brim, though a 5 mm brim was asked for. Look at "
            "the first layer in the slicer before printing: the part is held by its own footprint alone."
        )
    asked_none = SliceResult(success=True, output_path=str(path), message="Sliced")
    _say_brim_left_out(asked_none, 0.0, SlicerInfo(path="/usr/bin/prusa-slicer", name="prusa-slicer"), None)
    assert asked_none.brim is None and asked_none.message == "Sliced"
