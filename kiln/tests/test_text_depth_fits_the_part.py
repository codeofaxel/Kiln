"""Text asked for with no depth is fitted to a part too thin for the legibility depth.

The engine cuts text at its legibility depth and refuses a cut that would
leave less than one nozzle width of floor.  A caller that names no depth
asks for lettering that reads, not for a number; on a part too thin for
the legibility depth and its floor, the deepest cut that keeps the floor is
cut instead, down to two nozzle widths, and the depth it was cut at is said.
A depth the caller named is never changed, and below two nozzle widths the
cut is still refused with the depth that would fit.
"""

from __future__ import annotations

import shutil

import pytest

from kiln.emboss_generator import DebossTooDeepError
from tests.test_emboss_never_cuts_through import (
    LEGIBLE_DEPTH,
    NOZZLE,
    _box,
    _recess_floor_mm,
    _stl,
)

needs_openscad = pytest.mark.skipif(shutil.which("openscad") is None, reason="needs OpenSCAD")

FITTED_FLOOR = 2 * NOZZLE  # the shallowest cut the engine fits down to on its own


def _read(body_stl: str, tmp_path, **kw) -> tuple[str, list[str]]:
    from kiln.decoration_helpers import emboss_text_lines_on_face

    notes: list[str] = []
    out = emboss_text_lines_on_face(
        body_stl, ["Read"], face_name="top", mode="deboss", nozzle_diameter_mm=NOZZLE,
        line_scale=0.65, output_dir=str(tmp_path / "decorated"), collect_warnings=notes, **kw,
    )
    return out, notes


def _solids(stl_path: str) -> int:
    import trimesh

    from kiln.mesh_bodies import read_mesh

    return read_mesh(trimesh.load(stl_path, force="mesh")).solid_count


def test_the_fitted_depth_is_the_deepest_cut_that_keeps_the_floor():
    from kiln.decoration_helpers import fitted_deboss_depth_mm

    assert fitted_deboss_depth_mm(1.2 - NOZZLE, NOZZLE) == pytest.approx(0.8)
    assert fitted_deboss_depth_mm(1.5 - NOZZLE, NOZZLE) == pytest.approx(1.1)
    assert fitted_deboss_depth_mm(0.83, NOZZLE) == pytest.approx(0.8)  # cut in 0.05 mm steps, never past the reading
    assert fitted_deboss_depth_mm(1.0 - NOZZLE, NOZZLE) is None  # 0.6: shallower than two nozzle widths
    assert fitted_deboss_depth_mm(0.0, NOZZLE) is None


@needs_openscad
@pytest.mark.parametrize("thickness", [1.2, 1.5])
def test_text_on_a_thin_part_is_cut_as_deep_as_the_part_allows_and_says_so(tmp_path, thickness):
    body = _stl(_box(0, 30, 0, 60, 0, thickness), tmp_path / "body.stl")
    out, notes = _read(body, tmp_path)
    expected = round(thickness - NOZZLE, 2)
    assert _solids(out) == 1
    assert _recess_floor_mm(out, thickness) == pytest.approx(NOZZLE, abs=1e-3)
    assert notes == [f"Text cut {expected:g} mm deep, the most this {thickness:g} mm part can take and stay whole."]


@needs_openscad
def test_a_part_with_room_for_the_legibility_depth_is_cut_at_it_and_says_nothing(tmp_path):
    body = _stl(_box(0, 30, 0, 60, 0, 3.0), tmp_path / "body.stl")
    out, notes = _read(body, tmp_path)
    assert _solids(out) == 1
    assert _recess_floor_mm(out, 3.0) == pytest.approx(3.0 - LEGIBLE_DEPTH, abs=1e-3)
    assert not any("cut" in n and "deep" in n for n in notes), notes


@needs_openscad
def test_a_part_too_thin_for_two_nozzle_widths_is_still_refused(tmp_path):
    body = _stl(_box(0, 30, 0, 60, 0, 1.0), tmp_path / "body.stl")
    with pytest.raises(DebossTooDeepError) as caught:
        _read(body, tmp_path)
    assert caught.value.max_depth_mm == pytest.approx(1.0 - NOZZLE, abs=1e-6)
    assert caught.value.max_depth_mm < FITTED_FLOOR


@needs_openscad
def test_a_depth_the_caller_named_is_never_fitted(tmp_path):
    body = _stl(_box(0, 30, 0, 60, 0, 1.2), tmp_path / "body.stl")
    with pytest.raises(DebossTooDeepError):
        _read(body, tmp_path, depth_mm=LEGIBLE_DEPTH)
