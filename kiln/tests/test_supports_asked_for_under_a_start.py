"""A part whose one reason for supports is a piece that starts on nothing is
asked for them under that piece alone.

Where the file cannot be painted whole, the slice keeps supports on for the
whole part.
"""

from __future__ import annotations

import math
import zipfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from kiln import printability  # noqa: E402
from kiln.printability import judge_supports  # noqa: E402
from kiln.slicer_supports import (  # noqa: E402
    PAINTED_ONLY_KEY,
    SliceSupports,
    asked_under_input,
    decide_supports,
    witnessed,
)

#: The middle of a 256 mm bed, where a mesh is placed for a slicer told not to arrange it.
_BED = (128.0, 128.0)


def _hex_hole(centre_x: float, centre_z: float, across: float = 7.0):
    hole = trimesh.creation.cylinder(radius=across / 2.0, height=10.0, sections=6)
    hole.apply_transform(trimesh.transformations.rotation_matrix(math.pi / 2.0, (1, 0, 0)))
    hole.apply_translation((centre_x, 0.0, centre_z))
    return hole


def _toothed_wall(tmp_path: Path, holes=((-27.0, 20.0),), name: str = "wall.stl") -> str:
    """A 60 x 4 x 40 mm wall with hexagonal *holes* through it (centre x, centre z).
    A hole centred 3 mm in from an end leaves a tooth hanging from its top:
    the wall's end cuts the hexagon 0.5 mm short of its corner."""
    wall = trimesh.creation.box(extents=(60.0, 4.0, 40.0))
    wall.apply_translation((0.0, 0.0, 20.0))
    cut = trimesh.boolean.difference([wall, *(_hex_hole(x, z) for x, z in holes)], engine="manifold")
    cut.apply_translation((_BED[0], _BED[1], 0.0))
    path = tmp_path / name
    cut.export(str(path))
    return str(path)


def _judged(path: str) -> np.ndarray:
    facets, vertices = printability._parse_mesh(path)
    return np.asarray(printability._prepare_parsed(list(facets), vertices)[0], dtype=np.float64)


def _lean(tris: np.ndarray) -> np.ndarray:
    """Each face's lean from vertical, degrees, for a downward face; negative facing up."""
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    unit = normals[:, 2] / np.linalg.norm(normals, axis=1)
    return 90.0 - np.degrees(np.arccos(np.clip(-unit, -1.0, 1.0)))


# ---------------------------------------------------------------------------
# Which faces are asked for
# ---------------------------------------------------------------------------


def test_a_tooth_is_held_under_its_own_flanks_and_nothing_else(tmp_path: Path) -> None:
    """The tooth's two faces lean 30 degrees, short of the 45 the slice is
    kept off at, so with none of the piece leaning that far all of it is
    asked for.  Not the hole's flat top, and not the far slope of the same
    hole, which begins lower and hangs from the wall."""
    path = _toothed_wall(tmp_path)
    judged = judge_supports(path, material="pla")
    assert printability.support_verdict(judged) == "starts in mid-air at 21mm"
    tris = _judged(path)
    under = np.asarray(judged.support_under_faces, dtype=int)
    assert under.size == 2
    corners = tris[under].reshape(-1, 3)
    assert corners[:, 0].max() <= _BED[0] - 28.7 and corners[:, 0].min() == pytest.approx(_BED[0] - 30.0)
    assert corners[:, 2].min() == pytest.approx(20.866, abs=0.01) and corners[:, 2].max() == pytest.approx(23.031, abs=0.01)
    assert np.allclose(_lean(tris[under]), 30.0, atol=0.1)


def test_two_teeth_are_two_pieces_each_held_under_its_own(tmp_path: Path) -> None:
    path = _toothed_wall(tmp_path, holes=((-27.0, 12.0), (27.0, 30.0)))
    judged = judge_supports(path, material="pla")
    assert printability.support_verdict(judged) == "2 pieces start in mid-air, lowest at 13mm"
    tris = _judged(path)
    centres = tris[np.asarray(judged.support_under_faces, dtype=int)].mean(axis=1)
    assert len(centres) == 4
    assert (centres[:, 0] < _BED[0] - 28.0).sum() == 2 and (centres[:, 0] > _BED[0] + 28.0).sum() == 2


def _tower_with_a_chamfered_foot(tmp_path: Path, up: float = 0.3) -> str:
    """A tower on the plate, and beside it a foot *up* mm over it: a 10 mm
    sole widening to 16 mm over 6 mm (a chamfer leaning 27 degrees), then a
    post, joined to the tower by a strut leaning 39 degrees."""
    tower = trimesh.creation.cylinder(radius=4.0, height=30.0, sections=48)
    tower.apply_translation((0.0, 0.0, 15.0))
    profile = np.asarray([(0.0, 0.0), (5.0, 0.0), (8.0, 6.0), (8.0, 26.0), (0.0, 26.0)], dtype=float)
    foot = trimesh.creation.revolve(profile, sections=48)
    foot.apply_translation((20.0, 0.0, up))
    strut = trimesh.convex.convex_hull(np.asarray([
        (3.0, -2.0, 4.0), (3.0, 2.0, 4.0), (3.0, -2.0, 14.0), (3.0, 2.0, 14.0), (13.0, -2.0, 14.0), (13.0, 2.0, 14.0),
    ]))
    path = tmp_path / "foot.stl"
    body = trimesh.util.concatenate([tower, foot, strut])
    body.apply_translation((_BED[0], _BED[1], 0.0))
    body.export(str(path))
    return str(path)


def test_a_foot_is_held_under_its_sole_and_its_chamfer_prints_on_its_own(tmp_path: Path) -> None:
    path = _tower_with_a_chamfered_foot(tmp_path)
    judged = judge_supports(path, material="pla")
    assert printability.support_verdict(judged) == "starts in mid-air at 0.3mm"
    tris = _judged(path)
    under = np.asarray(judged.support_under_faces, dtype=int)
    assert under.size >= 40
    assert np.allclose(_lean(tris[under]), 90.0, atol=0.1), "the sole, every face level"
    assert np.allclose(tris[under][:, :, 2], 0.3, atol=1e-6)


def _round_hole_wall(tmp_path: Path) -> str:
    """A round hole, 6.4 mm across and drawn in 40 facets, whose rim passes
    0.165 mm beyond the wall's end: the end cuts the rim's lowest facet just
    short of its corner, and the face at the tooth's tip is 0.06 mm tall."""
    wall = trimesh.creation.box(extents=(60.0, 4.0, 36.0))
    wall.apply_translation((0.0, 0.0, 18.0))
    hole = trimesh.creation.cylinder(radius=3.2, height=10.0, sections=40)
    hole.apply_transform(trimesh.transformations.rotation_matrix(math.pi / 2.0, (1, 0, 0)))
    hole.apply_translation((-26.835, 0.0, 14.0))
    cut = trimesh.boolean.difference([wall, hole], engine="manifold")
    cut.apply_translation((_BED[0], _BED[1], 0.0))
    path = tmp_path / "round.stl"
    cut.export(str(path))
    return str(path)


def test_a_tooth_thinner_than_a_layer_is_held_with_its_first_two_layers(tmp_path: Path) -> None:
    path = _round_hole_wall(tmp_path)
    judged = judge_supports(path, material="pla")
    assert printability.support_verdict(judged) is not None and "mid-air" in printability.support_verdict(judged)
    tris = _judged(path)
    under = tris[np.asarray(judged.support_under_faces, dtype=int)]
    starts = tris[np.asarray(printability._starts_on_nothing(tris, 0.0, 0.2)[0], dtype=int)]
    tip = float(starts[:, :, 2].min())
    assert float(starts[:, :, 2].max()) - tip < 0.15, "the faces at the start alone are thinner than a layer"
    # What is asked for reaches up through the first two layers, and no further.
    assert float(under[:, :, 2].min()) == pytest.approx(tip, abs=1e-6)
    assert 0.3 < float(under[:, :, 2].max()) - tip < 0.9
    assert float(under[:, :, 0].max()) < _BED[0] - 30.0 + 1.5, "all of it at the end, none of it the far side of the hole"


def test_a_part_that_needs_supports_for_another_reason_asks_for_them_under_nothing(tmp_path: Path) -> None:
    """A shelf beside the tooth needs supports anyway: the slice carries them
    for the whole part, the hole's top kept clear, and the tooth is left to
    the slicer as before."""
    wall = trimesh.creation.box(extents=(60.0, 4.0, 40.0))
    wall.apply_translation((0.0, 0.0, 20.0))
    cut = trimesh.boolean.difference([wall, _hex_hole(-27.0, 20.0)], engine="manifold")
    shelf = trimesh.creation.box(extents=(20.0, 20.0, 3.0))
    shelf.apply_translation((40.0, 0.0, 30.0))
    path = tmp_path / "shelved.stl"
    trimesh.util.concatenate([cut, shelf]).export(str(path))
    judged = judge_supports(path, material="pla")
    assert "mid-air" not in (printability.support_verdict(judged) or "")
    assert judged.support_under_faces == [] and judged.support_kept_off_faces


# ---------------------------------------------------------------------------
# The decision and the paint
# ---------------------------------------------------------------------------


def test_the_decision_carries_the_faces_and_the_paint_asks_for_support_there(tmp_path: Path) -> None:
    path = _toothed_wall(tmp_path)
    decision = decide_supports(path)
    assert decision.enabled and decision.under and decision.kept_off
    assert "under" not in decision.to_dict() and "under_painted" not in decision.to_dict()

    painted = asked_under_input(path, decision, str(tmp_path), bed_centre=_BED)
    assert painted is not None
    out, count = painted
    assert count == len(decision.under)
    with zipfile.ZipFile(out) as archive:
        model = archive.read("3D/3dmodel.model").decode()
    assert model.count('paint_supports="4"') == count and 'slic3rpe:custom_supports="4"' in model
    assert 'supports="8"' not in model, "nothing is kept clear in a file that supports only what is painted"


def test_the_paint_is_all_or_nothing(tmp_path: Path) -> None:
    path = _toothed_wall(tmp_path)
    decision = decide_supports(path)
    short = replace(decision, under=(*decision.under, 10**6))
    assert asked_under_input(path, short, str(tmp_path), bed_centre=_BED) is None
    assert asked_under_input(path, replace(decision, under=()), str(tmp_path), bed_centre=_BED) is None
    assert asked_under_input(path, replace(decision, enabled=False), str(tmp_path), bed_centre=_BED) is None


def test_witnessing_keeps_what_was_asked_for() -> None:
    asked = SliceSupports(enabled=True, decided_by="analysis", reason="starts in mid-air at 3mm", under=(1, 2), under_painted=2)
    none = witnessed(asked, __file__)
    assert not none.enabled and none.under == (1, 2) and none.under_painted == 2


# ---------------------------------------------------------------------------
# Through the slice, with the real slicers
# ---------------------------------------------------------------------------


def _slicers() -> dict[str, str]:
    import shutil

    found = {}
    for name, path in (("PrusaSlicer", "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"),
                       ("Bambu Studio", "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
                       ("OrcaSlicer", "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")):
        if Path(path).is_file():
            found[name] = path
    if "PrusaSlicer" not in found and shutil.which("prusa-slicer"):
        found["PrusaSlicer"] = shutil.which("prusa-slicer")
    return found


_SLICERS = _slicers()


def _slice(tmp_path: Path, monkeypatch, slicer: str, path: str, out: str):
    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    return slice_file(path, output_dir=str(tmp_path / out), profile=resolve_slicer_profile("bambu_a1"),
                      slicer_path=_SLICERS[slicer], timeout=600,
                      extra_args=["--dont-arrange"] if slicer == "PrusaSlicer" else None)


def _settings_in(gcode: str) -> dict[str, str]:
    found = {}
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("; ") and " = " in line:
                key, _, value = line[2:].partition(" = ")
                found[key.strip()] = value.strip()
    return found


@pytest.mark.skipif(not _SLICERS, reason="no slicer installed: public CI has none")
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
def test_the_slice_supports_only_what_is_painted_and_the_tooth_is_held(tmp_path: Path, monkeypatch, slicer: str):
    """The real slicer, handed the painted file: it is told to support only
    what is painted, it lays support, and the reply reads as it did."""
    from kiln.slicer_supports import gcode_support_lines

    result = _slice(tmp_path, monkeypatch, slicer, _toothed_wall(tmp_path), "held")
    supports = result.supports
    assert supports.enabled and supports.under_painted == 2 and supports.kept_off_painted == 0
    assert supports.settings[PAINTED_ONLY_KEY] == "0"
    assert supports.note == "supports on: starts in mid-air at 21mm; none on the parts that print on their own"
    assert gcode_support_lines(result.output_path) > 0
    written = _settings_in(result.output_path)
    if slicer == "PrusaSlicer":
        assert written["support_material"] == "1" and written["support_material_auto"] == "0"
    else:
        assert written["enable_support"] == "1" and written["support_type"] == "normal(manual)"


@pytest.mark.skipif(not _SLICERS, reason="no slicer installed: public CI has none")
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
def test_a_file_that_cannot_be_painted_whole_keeps_supports_for_the_whole_part(tmp_path: Path, monkeypatch, slicer: str):
    from kiln import slicer as slicer_module
    from kiln.slicer_supports import gcode_support_lines

    monkeypatch.setattr(slicer_module, "asked_under_input", lambda *_a, **_k: None)
    result = _slice(tmp_path, monkeypatch, slicer, _toothed_wall(tmp_path), "whole")
    supports = result.supports
    assert supports.enabled and supports.under_painted == 0 and supports.kept_off_painted > 0
    assert PAINTED_ONLY_KEY not in supports.settings
    assert gcode_support_lines(result.output_path) > 0
    written = _settings_in(result.output_path)
    if slicer == "PrusaSlicer":
        assert written["support_material_auto"] == "1"
    else:
        assert written["support_type"] == "normal(auto)"
