"""A part that needs supports is charged for it once, by area.

Until 2026-10-24 one flat underside paid three times: ``overhangs`` charged
the share of the mesh's TRIANGLES that hang over (so the same mushroom lost
4 points at one mesh resolution and 8 at another), ``bridging`` charged the
same underside again whenever any region needed supports (a sphere lost 5
points for a 7 mm "bridge" under the 10 mm a slicer spans unaided), and
``supports`` charged it a third time from a volume estimate that clamps at
100 % under any wide ceiling.  Up to 60 points for one fact, so a part that
only needs supports, standing well on the plate, read F.

Now the ``supports`` block charges the share of the part's underside, seen
from below, that supports must hold up: nothing for the first tenth, then in
a straight line to 30 points for all of it.  The ``bridging`` block charges
only a bridge longer than the self-support limit that the area did not
already pay for.  Every other charge is unchanged, and no part scores lower.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

#: The blocks a part's need for supports is charged under.
_SUPPORT_TERMS = ("overhangs", "bridging", "supports")

#: Scores measured on the public path at 3bbda204, before this change, for
#: the parts below.  The change only removes double charges, so no part may
#: score lower than it did.  The ABS plate was 80 there and is 77 on main
#: 18f407fd with this change reverted (its warping charge grew to -20 when
#: ABS stopped being vouched for on a standard bed, a change of main's own,
#: measured 2026-10-25); this change still moves it by nothing.
_BEFORE = {
    "block": 100, "mushroom_x0": 26, "mushroom_x4": 22, "t_shape": 54, "sphere": 58,
    "bridge_part": 68, "chamfer_45": 70, "tab_3mm": 100, "cantilever": 49, "column": 84,
    "abs_plate": 77,
}


def _block_kiln_pro(monkeypatch) -> None:
    """The public score: no paid package in reach, its submodules included."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


def _mushroom(subdivisions: int):
    """An 80 mm cap on a 3 mm stem, the cap's mesh split *subdivisions* times."""
    cap = _box((-40, -40, 25), (40, 40, 27))
    for _ in range(subdivisions):
        cap = cap.subdivide()
    return trimesh.util.concatenate([_box((-1.5, -1.5, 0), (1.5, 1.5, 25)), cap])


def _parts() -> dict[str, tuple[object, str]]:
    sphere = trimesh.creation.icosphere(subdivisions=3, radius=15)
    sphere.apply_translation((0, 0, -sphere.bounds[0][2]))
    corners = [(x, y, z) for z, h in ((0, 8), (2, 10), (10, 10)) for x in (-h, h) for y in (-h, h)]
    chamfer = trimesh.convex.convex_hull(np.array(corners, dtype=float))
    chamfer.apply_translation((10, 10, 0))
    column = trimesh.creation.cylinder(radius=3, height=60, sections=48)
    column.apply_translation([120, 120, 30])
    return {
        "block": (_box((0, 0, 0), (20, 20, 10)), "pla"),
        "mushroom_x0": (_mushroom(0), "pla"),
        "mushroom_x4": (_mushroom(4), "pla"),
        # A 40 x 10 x 10 bar on a 10 x 10 x 20 post.
        "t_shape": (trimesh.util.concatenate([_box((15, 0, 0), (25, 10, 20)),
                                              _box((0, 0, 20), (40, 10, 30))]), "pla"),
        "sphere": (sphere, "pla"),
        # Two 10 x 10 x 20 posts 30 mm apart under a 50 x 10 x 5 beam.
        "bridge_part": (trimesh.util.concatenate([_box((0, 0, 0), (10, 10, 20)),
                                                  _box((40, 0, 0), (50, 10, 20)),
                                                  _box((0, 0, 20), (50, 10, 25))]), "pla"),
        # A 20 x 20 x 10 block with a 2 mm, 45-degree chamfer round its base.
        "chamfer_45": (chamfer, "pla"),
        # A 3 mm tab on the side of a block: a lip a slicer reaches sideways.
        "tab_3mm": (trimesh.boolean.union([_box((0, 0, 0), (20, 20, 10)),
                                           _box((20, 8.5, 4), (23, 11.5, 6))]), "pla"),
        "cantilever": (trimesh.util.concatenate([_box((95, 95, 0), (105, 105, 30)),
                                                 _box((95, 95, 30), (165, 105, 35))]), "pla"),
        "column": (column, "pla"),
        "abs_plate": (_box((28, 28, 0), (228, 228, 2)), "abs"),
    }


@pytest.fixture(scope="module")
def reports(tmp_path_factory) -> dict[str, object]:
    from kiln.printability import analyze_printability

    folder = tmp_path_factory.mktemp("support_charges")
    out = {}
    with pytest.MonkeyPatch.context() as mp:
        _block_kiln_pro(mp)
        for name, (mesh, material) in _parts().items():
            path = folder / f"{name}.stl"
            mesh.export(str(path))
            out[name] = analyze_printability(str(path), material=material)
    return out


def _support_cost(report) -> int:
    return -sum(getattr(report, key).score_deduction for key in _SUPPORT_TERMS)


def _split(mesh, times: int):
    for _ in range(times):
        mesh = mesh.subdivide()
    return mesh


#: Three parts whose underside needs supports, built with the overhanging
#: piece's mesh split ``k`` times.  Measured at 3bbda204, by triangle count,
#: the three support charges together went 34-38, 34-38 and 32-38 points
#: across k = 0..4, and the beam read 68 at k=0 and 62 at k=4.
_SPLIT = {
    "mushroom": lambda k: trimesh.util.concatenate([_box((-1.5, -1.5, 0), (1.5, 1.5, 25)),
                                                    _split(_box((-40, -40, 25), (40, 40, 27)), k)]),
    "t_shape": lambda k: trimesh.util.concatenate([_box((15, 0, 0), (25, 10, 20)),
                                                   _split(_box((0, 0, 20), (40, 10, 30)), k)]),
    "bridge_part": lambda k: trimesh.util.concatenate([_box((0, 0, 0), (10, 10, 20)),
                                                       _box((40, 0, 0), (50, 10, 20)),
                                                       _split(_box((0, 0, 20), (50, 10, 25)), k)]),
}


@pytest.mark.parametrize("name", sorted(_SPLIT))
def test_the_charge_does_not_move_with_the_mesh(name, tmp_path, monkeypatch):
    """The same part split finer and finer is the same part: its supports
    are charged the same, to the point, however many triangles carry it."""
    from kiln.printability import analyze_printability

    _block_kiln_pro(monkeypatch)
    costs, shares, scores = [], [], []
    for k in range(5):
        path = tmp_path / f"{name}_{k}.stl"
        _SPLIT[name](k).export(str(path))
        report = analyze_printability(str(path), material="pla")
        costs.append(_support_cost(report))
        shares.append(report.supports.support_area_percentage)
        scores.append(report.score)
    assert max(costs) - min(costs) <= 1, costs
    assert max(shares) - min(shares) < 0.5, shares
    assert max(scores) - min(scores) <= 1, scores


@pytest.mark.parametrize("name", ["t_shape", "bridge_part"])
def test_a_part_that_only_needs_supports_still_prints(reports, name):
    """A bar on a post and a beam across two posts stand well on the plate
    and need supports under one face each.  Charged three times for that
    face they read 54 and 68; charged once they clear the printable line."""
    from kiln.printability import _PRINTABLE_SCORE_MIN

    report = reports[name]
    assert report.overhangs.needs_supports
    assert report.score >= _PRINTABLE_SCORE_MIN, (report.score, _support_cost(report))


def test_a_bridge_a_slicer_spans_costs_nothing(reports):
    """The sphere's bottom flat reads a 7 mm span, under the 10 mm a slicer
    bridges unaided, and was charged 5 points as a bridge on top of the
    overhang it is part of."""
    from kiln.printability import _MAX_SELF_SUPPORTING_BRIDGE_MM

    report = reports["sphere"]
    assert report.bridging.max_bridge_length_mm <= _MAX_SELF_SUPPORTING_BRIDGE_MM
    assert report.bridging.score_deduction == 0
    assert report.supports.score_deduction < 0  # the overhang is still charged, once


@pytest.mark.parametrize("name", sorted(_parts()))
def test_needing_supports_never_costs_more_than_30(reports, name):
    assert _support_cost(reports[name]) <= 30, {k: getattr(reports[name], k).score_deduction
                                                 for k in _SUPPORT_TERMS}


@pytest.mark.parametrize("name", ["block", "column", "abs_plate"])
def test_a_part_with_no_overhang_scores_what_it_did(reports, name):
    assert _support_cost(reports[name]) == 0
    assert reports[name].score == _BEFORE[name]


@pytest.mark.parametrize("name", sorted(_BEFORE))
def test_no_part_scores_lower_than_before(reports, name):
    assert reports[name].score >= _BEFORE[name], (reports[name].score, _BEFORE[name])


def test_the_charge_is_the_share_of_the_underside_held_up(reports):
    """The supports block states the area it charges for, so a reader can
    check the points: the mushroom's cap is nearly its whole underside, the
    chamfer ring about a third of the block's."""
    mushroom = reports["mushroom_x0"].supports
    assert mushroom.support_area_percentage > 95
    assert mushroom.score_deduction in (-30, -29)
    chamfer = reports["chamfer_45"].supports
    assert chamfer.support_area_mm2 == pytest.approx(144, rel=0.02)
    assert chamfer.underside_area_mm2 == pytest.approx(400, rel=0.02)
    assert chamfer.support_area_percentage == pytest.approx(36, abs=0.5)
    assert chamfer.score_deduction == -8  # (36 - 10) / 90 of 30 points


def test_a_small_share_held_up_costs_nothing(tmp_path, monkeypatch):
    """A cylinder lying on its side needs supports under a strip either side
    of the line it rests on, a tenth of its underside, and was charged
    nothing for it before (its share of the faces rounded to 0 points and its
    support volume was too small to count).  It still costs nothing."""
    from kiln.printability import analyze_printability

    _block_kiln_pro(monkeypatch)
    lying = trimesh.creation.cylinder(radius=10, height=60, sections=64)
    lying.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    lying.apply_translation((0, 0, -lying.bounds[0][2]))
    path = tmp_path / "lying.stl"
    lying.export(str(path))
    report = analyze_printability(str(path), material="pla")
    assert report.overhangs.needs_supports
    assert 5 < report.supports.support_area_percentage < 10
    assert _support_cost(report) == 0
    assert report.score == 85  # what it scored at 3bbda204
