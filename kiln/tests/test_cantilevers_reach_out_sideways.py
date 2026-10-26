"""A cantilever is a member that reaches out SIDEWAYS from what holds it up,
with nothing under it: a shelf arm, a flange printed in the air, a hook's
reach, the rim of a cap on a stem.  A peg standing on a plate, or the upright
of an L-bracket, is held from below its whole height as it prints: a column,
not a cantilever.

Until 2026-10-23 the structural analysis read a cantilever off how far the
middle of each horizontal slice moved from the slice below, so wherever one
part of a shape ended and another began it saw one.  A plain pegboard got a
cantilever where its pegs leave the plate, an L-bracket standing on its base
got two, Blender's donut five (each placed in the hole, off the part), Kiln's
own nameplate twelve and its pegboard tool holder five -- while a T with a
20 mm arm each side, and a cap reaching 15 mm past its stem, got none.  Each
warning costs a part points in the design audit and turns supports on in the
slicer settings, and the reinforcement step puts a gusset where it points.

The parts here are joined the way a real part is (a boolean union), and each
is read through the tool a person calls, as a plain install answers it.
"""

from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d", reason="the parts are joined with a boolean union")

from tests import _real_exports as rx  # noqa: E402
from tests.test_sharp_corners_are_inside_corners import (  # noqa: E402
    _box,
    _call,
    _l_bracket,
    _pegboard,
    _post,
    _save,
    _union,
)


def _flange_in_air():
    """The L-bracket turned upside down: standing on the end of its upright,
    its 40 mm leg a flange in the air, 35 mm past the upright."""
    mesh = _l_bracket()
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi, (0, 1, 0)))
    mesh.apply_translation((0, 0, -mesh.bounds[0][2]))
    return mesh


def _t_shape():
    """A 10 mm post 30 tall, a 50 x 10 x 4 bar across its top: 20 mm of arm
    each side."""
    return _union(_box((20, 0, 0), (30, 10, 30)), _box((0, 0, 30), (50, 10, 34)))


def _hook():
    """A back plate standing up, an arm reaching 30 mm out of it at 10 mm up,
    and a lip rising at the arm's end."""
    return _union(_box((0, 0, 0), (5, 20, 60)), _box((5, 0, 10), (35, 20, 16)), _box((30, 0, 16), (35, 20, 26)))


def _mushroom(sections=96):
    """A cap 40 mm across on a stem 10 mm across: a rim reaching 15 mm all
    round."""
    return _union(_post(0, 0, 0, 20, 5, sections), _post(0, 0, 20, 26, 20, sections=2 * sections))


def _touching_shelf():
    """A column and a 40 mm arm off its top, written as two boxes that touch
    and are not joined, as many exports and generators write them."""
    return trimesh.util.concatenate([_box((0, 0, 0), (10, 10, 30)), _box((10, 0, 25), (50, 10, 30))])


def _braced_shelf():
    """The shelf with a 45-degree brace under the first 25 mm of its arm:
    the last 15 mm still reach out with nothing under them."""
    brace = trimesh.Trimesh(
        vertices=[(10, 0, 0), (10, 0, 25), (35, 0, 25), (10, 10, 0), (10, 10, 25), (35, 10, 25)],
        faces=[(0, 1, 2), (3, 5, 4), (0, 3, 4), (0, 4, 1), (1, 4, 5), (1, 5, 2), (2, 5, 3), (2, 3, 0)],
    )
    brace.fix_normals()
    return _union(_box((0, 0, 0), (10, 10, 30)), _box((10, 0, 25), (50, 10, 30)), brace)


def _deck_on_two_posts():
    """A deck on two posts 50 mm apart, running on 10 mm past each one."""
    return _union(_box((10, 0, 0), (16, 6, 20)), _box((60, 0, 0), (66, 6, 20)), _box((0, 0, 20), (76, 6, 24)))


def _cantilevers(path):
    reply = _call("analyze_structural_risks", file_path=path)
    assert reply.get("success"), reply
    return [r for r in reply["risks"] if r["risk_type"] == "cantilever"]


def _distance_to_surface(mesh, point):
    """How far ``point`` is from ``mesh``'s surface, to within the spacing of
    100,000 points spread over it."""
    samples, _ = trimesh.sample.sample_surface(mesh, 100_000, seed=0)
    return float(np.linalg.norm(samples - np.asarray(point), axis=1).min())


@pytest.mark.parametrize("build", [_pegboard, _l_bracket], ids=["pegboard", "l_bracket_on_its_base"])
def test_a_column_is_not_a_cantilever(build, tmp_path):
    """Pegs standing on a plate, the upright of an L-bracket on its base: each
    is held from below its whole height."""
    assert _cantilevers(_save(build(), tmp_path, "part")) == []


#: (part, [(reach mm, root x, root z, thickness mm)], each arm).  The root is
#: where the arm meets what holds it up, read to within a column (0.5 mm).
ARMS = {
    "flange_in_air": (_flange_in_air, [(35.0, -5.0, 35.0, 5.0)]),
    "t_shape": (_t_shape, [(20.0, 20.0, 30.0, 4.0), (20.0, 30.0, 30.0, 4.0)]),
    "hook": (_hook, [(30.0, 5.0, 10.0, 6.0)]),
    "touching_shelf": (_touching_shelf, [(40.0, 10.0, 25.0, 5.0)]),
    "braced_shelf": (_braced_shelf, [(15.0, 35.0, 25.0, 5.0)]),
}


@pytest.mark.parametrize("name", sorted(ARMS))
def test_a_member_reaching_out_sideways_is_one_cantilever_per_arm(name, tmp_path):
    build, arms = ARMS[name]
    mesh = build()
    found = _cantilevers(_save(mesh, tmp_path, name))
    assert len(found) == len(arms), found
    for risk, (reach, x, z, thick) in zip(sorted(found, key=lambda r: r["location_mm"][0]), arms, strict=True):
        assert risk["metric_name"] == "cantilever_reach_mm" and risk["severity"] == "warning"
        assert risk["metric_value"] == pytest.approx(reach, abs=1.0)
        assert (risk["location_mm"][0], risk["location_mm"][2]) == pytest.approx((x, z), abs=0.6)
        assert risk["region_size_mm"][2] == pytest.approx(thick, abs=0.3)
        assert f"reaches {risk['metric_value']:.0f} mm out from what holds it up" in risk["description"]
        assert _distance_to_surface(mesh, risk["location_mm"]) < 1.0  # on the part, never in the air


@pytest.mark.parametrize("sections", [24, 96, 400])
def test_the_rim_of_a_cap_is_one_cantilever_however_finely_it_is_faceted(sections, tmp_path):
    """A cap on a stem reaches out all round: one member, measured from the
    stem to the rim."""
    mesh = _mushroom(sections)
    (rim,) = _cantilevers(_save(mesh, tmp_path, "mushroom"))
    assert rim["metric_value"] == pytest.approx(15.0, abs=1.0)
    assert rim["location_mm"][2] == pytest.approx(20.0, abs=0.01)
    assert np.hypot(*rim["location_mm"][:2]) == pytest.approx(5.0, abs=0.6)  # on the stem's edge
    assert rim["region_size_mm"] == pytest.approx([40.0, 40.0, 6.0], abs=0.6)


def test_an_overhang_held_at_both_ends_is_a_bridge_not_a_cantilever(tmp_path):
    """The 44 mm of deck between the posts is held at both ends; only the
    10 mm past each post reaches out.  The roof of a slot and the top of a
    window are bridges too."""
    ends = _cantilevers(_save(_deck_on_two_posts(), tmp_path, "deck"))
    assert sorted(round(r["location_mm"][0]) for r in ends) == [10, 66]
    assert [r["metric_value"] for r in ends] == pytest.approx([10.0, 10.0], abs=1.0)

    slot = trimesh.boolean.difference([_box((0, 0, 0), (30, 20, 18)), _box((11, -1, 6), (19, 21, 12))],
                                      engine="manifold")
    window = trimesh.boolean.difference([_box((0, 0, 0), (80, 3, 60)), _box((20, -1, 15), (60, 4, 40))],
                                        engine="manifold")
    assert _cantilevers(_save(slot, tmp_path, "slot")) == []
    assert _cantilevers(_save(window, tmp_path, "window")) == []


def test_a_round_part_has_no_cantilever(tmp_path):
    """A donut's underside curves down to the bed with nothing holding it
    from beside: an overhang the printability check judges, and no member
    reaching out.  Blender's donut, as Blender exports it."""
    assert _cantilevers(str(rx.stage("donut_mm.stl", tmp_path / "donut"))) == []
    sphere = trimesh.creation.icosphere(subdivisions=4, radius=15)
    sphere.apply_translation((0, 0, 15))
    for name, mesh in {"sphere": sphere, "cylinder": trimesh.creation.cylinder(radius=10, height=20, sections=128),
                       "cube": _box((0, 0, 0), (20, 20, 20))}.items():
        assert _cantilevers(_save(mesh, tmp_path, name)) == [], name


def test_the_reading_is_the_part_as_it_sits_in_the_file(tmp_path):
    """The same L-bracket two ways up: on its base, nothing; on the end of its
    upright, its leg is a flange in the air.  And an inside-out mesh is read
    the right way out."""
    assert _cantilevers(_save(_l_bracket(), tmp_path, "on_base")) == []
    assert len(_cantilevers(_save(_flange_in_air(), tmp_path, "upside_down"))) == 1
    inside_out = _t_shape()
    inside_out.invert()
    assert len(_cantilevers(_save(inside_out, tmp_path, "inside_out"))) == 2


def test_every_structural_door_says_the_same(tmp_path):
    """The plan's summary, the reinforcements and the slicer settings all read
    the one detector: nothing on the pegboard, both arms of the T."""
    pegboard = _save(_pegboard(), tmp_path, "pegboard")
    t_shape = _save(_t_shape(), tmp_path, "t_shape")

    assert "cantilever" not in _call("design_improvement_plan", file_path=pegboard)["summary"]
    assert "2 cantilevers" in _call("design_improvement_plan", file_path=t_shape)["summary"]

    def gussets(path):
        recs = _call("recommend_design_reinforcements", file_path=path)["reinforcements"]
        return sorted(r["location_mm"][0] for r in recs if r["addresses_risk"] == "cantilever")

    assert gussets(pegboard) == []
    assert gussets(t_shape) == pytest.approx([20.0, 30.0], abs=0.6)  # at each arm's root

    settings = _call("infer_print_settings", file_path=pegboard)
    assert not any("antilever" in n for n in settings["special_notes"]) and "antilever" not in settings["support_reason"]
    t_settings = _call("infer_print_settings", file_path=t_shape)
    assert t_settings["support_enabled"] is True and t_settings["support_reason"].startswith("Prints with supports (")


@pytest.mark.parametrize("case", ["kilometre_flange", "nan_vertex", "inf_vertex", "far_face"])
def test_a_hostile_file_is_read_with_bounded_work_and_never_raises(case):
    """Files people upload reach this check on Kiln's servers: the work is
    bounded by the file's size, never by the distances written in it, and a
    face with no position, or one far off, never stops it."""
    import time
    import tracemalloc

    from kiln.design_reasoning import _find_cantilevers

    if case == "kilometre_flange":
        length = 2_000_000.0
        mesh = _union(_box((0, 0, 0), (40, length, 5)), _box((0, 0, 0), (5, length, 40)))
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi, (0, 1, 0)))
        mesh.apply_translation((0, 0, 40))
    else:
        mesh = _t_shape()
    triangles = [tuple(map(tuple, t)) for t in mesh.triangles.tolist()]
    poison = {"nan_vertex": float("nan"), "inf_vertex": float("inf"), "far_face": 9e8}.get(case)
    if poison is not None:
        triangles.append(((0.0, 0.0, 0.0), (poison, 0.0, 0.0), (0.0, poison if case == "far_face" else 1.0, 0.0)))

    tracemalloc.start()
    try:
        began = time.monotonic()
        found = _find_cantilevers(triangles)
        took = time.monotonic() - began
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 64e6, f"{peak / 1e6:.0f} MB"
    assert took < 10.0
    if case in ("nan_vertex", "inf_vertex"):
        # A face with no position is left out; the T's two arms are still read.
        assert [round(r.location_mm[0]) for r in sorted(found, key=lambda r: r.location_mm[0])] == [20, 30]
