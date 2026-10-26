"""A support region is trapped only when it lies on a sealed hollow.

Until 2026-10-24 the support assessment decided "trapped" by casting one
ray up and one down from a point 0.1 mm under each overhang, and called the
region trapped when both rays crossed an odd number of surfaces.  Odd and
odd means the point is inside plastic, not inside a hollow, so it got both
cases backwards:

* A sealed void was missed.  The start point sits in the void's air, both
  rays cross an even number of surfaces, and a 40 mm box with a 30 x 30 x 20
  hollow read 0 trapped regions.
* An open part was refused.  The 1.5.0 honeycomb pen cup carries a sliver of
  its inside floor wound facing down; the point under it sits inside the
  2 mm floor, reads odd/odd, and every print door refused the cup as an
  "enclosed cavity".

And on any mesh over 5,000 triangles the ray budget let only the first
region be tested at all.

The rule now: split the mesh into its closed shells, read each shell's
winding the way most of its surface faces, and call a shell a sealed void
when it faces inward and lies inside a shell that faces outward.  A region
is trapped when its triangles lie on such a shell.  Finding one is said as
a warning and never refuses: the old test never refused a real hollow, so
refusing one now would make the gate stricter than it has ever been.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import trimesh
from trimesh.creation import box, cylinder

from kiln.plugins.validation_pipeline_tools import gate_for_print
from kiln.support_assessment import assess_support_feasibility

# The 400,000-triangle cup the diagnosis was run on.  It is 20 MB, too big
# for the fixtures folder, so this case runs where the file is and skips
# where it is not; the reduced twin below runs everywhere.
_REAL_CUP = os.environ.get(
    "KILN_REAL_CUP_STL",
    "/private/tmp/claude-501/-Users-adamarreola-Kiln-pro/"
    "6a0c6be8-17b0-4e8e-a9ac-e9992b88e4e4/scratchpad/cup_cavity/cup_honeycomb.stl",
)


def _placed_box(extents: tuple[float, float, float], centre: tuple[float, float, float]) -> trimesh.Trimesh:
    mesh = box(extents=list(extents))
    mesh.apply_translation(list(centre))
    return mesh


def _sealed_hollow_box() -> trimesh.Trimesh:
    """A 40 x 40 x 30 block with a 30 x 30 x 20 hollow sealed inside it.

    Built as two shells, the inner one turned to face into the hollow,
    which is how a sealed void is written in an STL.
    """
    outer = _placed_box((40, 40, 30), (0, 0, 15))
    hollow = _placed_box((30, 30, 20), (0, 0, 15))
    hollow.invert()
    return trimesh.util.concatenate([outer, hollow])


def _open_cup() -> trimesh.Trimesh:
    """A 70 mm cup, open at the top, with a 3 mm floor."""
    outer = cylinder(radius=35, height=60, sections=96)
    outer.apply_translation([0, 0, 30])
    inner = cylinder(radius=33, height=60, sections=96)
    inner.apply_translation([0, 0, 33])
    return outer.difference(inner)


def _cup_with_pockets() -> trimesh.Trimesh:
    """The open cup with six 1 mm pockets cut into its outside wall.

    Each pocket's roof faces down and needs supports, which come away
    through the pocket's open mouth.
    """
    cup = _open_cup()
    cuts = []
    for k in range(6):
        angle = k * np.pi / 3
        x, y = 35.5 * np.cos(angle), 35.5 * np.sin(angle)
        pocket = _placed_box((2, 6, 6), (x, y, 20 + (k % 2) * 15))
        pocket.apply_transform(trimesh.transformations.rotation_matrix(angle, [0, 0, 1], point=[x, y, 0]))
        cuts.append(pocket)
    return cup.difference(trimesh.util.concatenate(cuts))


def _cup_with_flipped_floor(count: int = 2) -> trimesh.Trimesh:
    """The open cup with *count* triangles of its inside floor wound facing down.

    The reduced twin of the real cup: the sliver at the floor that faces
    the wrong way is exactly what made the old test call the cup sealed.
    """
    cup = _open_cup()
    up = np.where((cup.face_normals[:, 2] > 0.99) & (np.abs(cup.triangles_center[:, 2] - 3.0) < 1e-6))[0][:count]
    assert len(up) == count
    faces = cup.faces.copy()
    faces[up] = faces[up][:, ::-1]
    return trimesh.Trimesh(cup.vertices, faces, process=False)


def _write(tmp_path: Path, mesh: trimesh.Trimesh, name: str) -> str:
    path = tmp_path / f"{name}.stl"
    mesh.export(path)
    return str(path)


def _support_check(report: dict) -> dict:
    checks = [c for c in report["checks"] if c["name"] == "support_assessment"]
    assert len(checks) == 1, report["checks"]
    return checks[0]


def _winding_notes(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "inconsistent winding" in w]


# ---------------------------------------------------------------------------
# A real sealed hollow is found, and said
# ---------------------------------------------------------------------------


def test_a_sealed_hollow_box_is_found_and_said_but_not_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, _sealed_hollow_box(), "sealed")

    assessed = assess_support_feasibility(stl_path=path, material="PLA")
    assert assessed.trapped_regions, "the hollow's ceiling needs supports that can never come out"
    assert all(not region.accessible for region in assessed.trapped_regions)
    # The ceiling of the hollow is at z = 25.
    assert all(abs(region.centroid[2] - 25.0) < 1e-6 for region in assessed.trapped_regions)
    assert assessed.removal_difficulty == "impossible"
    assert not _winding_notes(assessed.warnings)

    gate = gate_for_print(path, material="PLA")
    assert gate.reason is None, gate.reason
    check = _support_check(gate.report)
    assert check["passed"] is False
    assert check["severity"] == "warning"
    assert "sealed hollow" in check["details"]
    assert gate.report["ready_to_print"] is True
    assert gate.report["model_info"]["support_assessment"]["trapped_regions"]


def test_a_hollow_cut_by_a_boolean_is_found_too() -> None:
    """The same hollow made the way a CAD tool would make it."""
    sealed = _placed_box((40, 40, 30), (0, 0, 15)).difference(_placed_box((30, 30, 20), (0, 0, 15)))
    assessed = assess_support_feasibility(triangles=sealed.triangles.tolist(), material="PLA")
    assert assessed.trapped_regions
    assert assessed.removal_difficulty == "impossible"


def test_a_hollow_left_unchecked_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the containment budget runs out, the shells it never reached
    are named, never passed over as if they had been checked."""
    import kiln.support_assessment as sa

    monkeypatch.setattr(sa, "_MAX_CONTAINMENT_TESTS", 0)
    assessed = assess_support_feasibility(triangles=_sealed_hollow_box().triangles.tolist(), material="PLA")
    assert assessed.trapped_regions == []
    assert any("not checked for a sealed hollow" in w for w in assessed.warnings), assessed.warnings


# ---------------------------------------------------------------------------
# An open part is not a sealed hollow, whatever its winding says
# ---------------------------------------------------------------------------


def test_an_open_cup_with_two_floor_triangles_flipped_is_not_trapped(tmp_path: Path) -> None:
    path = _write(tmp_path, _cup_with_flipped_floor(2), "flipped")

    assessed = assess_support_feasibility(stl_path=path, material="PLA")
    assert assessed.trapped_regions == []
    assert assessed.removal_difficulty != "impossible"
    # The wrong-way triangles are a fault in the file, and said as one.
    notes = _winding_notes(assessed.warnings)
    assert len(notes) == 1 and notes[0].startswith("Mesh defect: 2 triangles"), assessed.warnings

    gate = gate_for_print(path, material="PLA")
    assert gate.reason is None, gate.reason
    assert "sealed hollow" not in _support_check(gate.report)["details"]


@pytest.mark.skipif(not Path(_REAL_CUP).is_file(), reason="the 400,000-triangle cup is not on this machine")
def test_the_real_honeycomb_cup_is_not_trapped_and_prints() -> None:
    gate = gate_for_print(_REAL_CUP, material="PLA")
    assert gate.reason is None, gate.reason
    assessed = gate.report["model_info"]["support_assessment"]
    assert assessed["trapped_regions"] == []
    assert assessed["removal_difficulty"] != "impossible"
    assert _winding_notes(assessed["warnings"])


@pytest.mark.parametrize("name, build", [("open_cup", _open_cup), ("cup_with_pockets", _cup_with_pockets)])
def test_a_clean_open_cup_stays_clean(tmp_path: Path, name: str, build) -> None:
    path = _write(tmp_path, build(), name)

    assessed = assess_support_feasibility(stl_path=path, material="PLA")
    assert assessed.trapped_regions == []
    assert not _winding_notes(assessed.warnings)

    gate = gate_for_print(path, material="PLA")
    assert gate.reason is None, gate.reason
    assert "sealed hollow" not in _support_check(gate.report)["details"]


# ---------------------------------------------------------------------------
# Every region is judged, however many triangles the part has
# ---------------------------------------------------------------------------


def test_every_region_is_judged_on_a_mesh_over_five_thousand_triangles() -> None:
    """Three sealed hollows in a 12,288-triangle block, and a block floating
    above it whose underside needs supports that come away normally.

    The old ray budget added two rays per triangle against 10,000, so on a
    mesh this size only the first region was ever tested.
    """
    block = _placed_box((100, 40, 20), (0, 0, 10))
    vertices, faces = trimesh.remesh.subdivide(block.vertices, block.faces)
    for _ in range(4):
        vertices, faces = trimesh.remesh.subdivide(vertices, faces)
    block = trimesh.Trimesh(vertices, faces)
    hollows = []
    for x in (-30.0, 0.0, 30.0):
        hollow = _placed_box((10, 10, 8), (x, 0, 10))
        hollow.invert()
        hollows.append(hollow)
    floating = _placed_box((10, 10, 5), (0, 30, 32.5))
    mesh = trimesh.util.concatenate([block, *hollows, floating])
    assert len(mesh.faces) > 5_000

    assessed = assess_support_feasibility(triangles=mesh.triangles.tolist(), material="PLA")

    trapped_x = sorted(region.centroid[0] for region in assessed.trapped_regions)
    assert trapped_x == pytest.approx([-30.0, 0.0, 30.0])
    # Every hollow's ceiling, at z = 14, and nothing else.
    assert all(abs(region.centroid[2] - 14.0) < 1e-6 for region in assessed.trapped_regions)


# ---------------------------------------------------------------------------
# Thousands of tiny shells: the work is bounded, and the bound is said
# ---------------------------------------------------------------------------


def _field_of_cubes(count: int = 3_000, nested: bool = False) -> trimesh.Trimesh:
    """*count* 1 mm cubes on a 3 mm grid, every other one wound inward, so
    half are inward-facing shells and half outward, and none sits inside
    another.  With *nested*, one more inward cube is placed inside one
    4 mm outward cube, off the grid: the one real sealed hollow."""
    parts = []
    for i in range(count):
        cube = box(extents=[1, 1, 1])
        cube.apply_translation([(i % 60) * 3, (i // 60) * 3, 0.5])
        if i % 2:
            cube.invert()
        parts.append(cube)
    if nested:
        body = _placed_box((4, 4, 4), (-20, -20, 2))
        hollow = _placed_box((2, 2, 2), (-20, -20, 2))
        hollow.invert()
        parts += [body, hollow]
    return trimesh.util.concatenate(parts)


def test_thousands_of_tiny_shells_are_judged_in_bounded_time() -> None:
    """2026-10-24, from an automated review: asking every inward shell about
    every outward shell was a Python loop over shells squared, seconds on a
    part made of thousands of pieces and minutes on more.  The pairs are
    now swept as arrays, and the one nested hollow is still the one found."""
    import time

    from kiln.support_assessment import _read_shells

    tris = _field_of_cubes(3_000).triangles.tolist()
    started = time.perf_counter()
    _read_shells(tris)
    took = time.perf_counter() - started
    assert took < 1.0, f"{took:.2f} s for 3,000 shells"

    # The sweep still finds the one hollow that really is sealed.
    assessed = assess_support_feasibility(
        triangles=_field_of_cubes(600, nested=True).triangles.tolist(), material="PLA",
    )
    assert [tuple(round(c) for c in r.centroid) for r in assessed.trapped_regions] == [(-20, -20, 3)]
    assert not any("not checked" in w for w in assessed.warnings), assessed.warnings


def test_every_pair_of_shells_counts_against_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair that the bounding boxes rule out still cost a look.  Only ray
    tests used to be charged, so a field of shells that never nest spent
    nothing and the loop ran unbounded; past the budget the shells not
    reached are said, never passed over."""
    import re

    import kiln.support_assessment as sa

    monkeypatch.setattr(sa, "_MAX_CONTAINMENT_TESTS", 10_000)
    assessed = assess_support_feasibility(triangles=_field_of_cubes(1_200).triangles.tolist(), material="PLA")

    assert assessed.trapped_regions == []
    unchecked = [w for w in assessed.warnings if "not checked for a sealed hollow" in w]
    assert unchecked, assessed.warnings
    # 600 inward shells against 600 outward ones is 360,000 pairs.  Each
    # look costs 600 of a 10,000 budget: the seventeenth look starts with
    # 400 left and ends it, so 583 inward shells go unreached.
    assert int(re.search(r"(\d+) inward-facing", unchecked[0]).group(1)) == 600 - 17, unchecked
