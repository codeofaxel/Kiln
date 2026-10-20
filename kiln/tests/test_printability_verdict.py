"""The printability check says one of three things, and never "printable"
for a part that cannot be printed at all.

Measured 2026-10-20 on 51 parts with PrusaSlicer as the second opinion: a
cone on its tip (0 mm2 of bed contact; the slicer refuses it) and a box with
0.2 mm walls on a 0.4 mm nozzle (the slicer lays down almost nothing) both
came back "printable", while a T-shape that any slicer prints with supports
came back "Not recommended for printing".  Two floors close the first half,
the same way the placement floor does; the three-way verdict closes the
second: ready / prints with supports / won't print as it is.
"""

from __future__ import annotations

import pytest

from kiln.printability import analyze_printability, verdict

trimesh = pytest.importorskip("trimesh")


def _save(tmp_path, name, mesh):
    path = tmp_path / f"{name}.stl"
    mesh.export(path)
    return str(path)


def _cone_on_its_tip(tmp_path):
    cone = trimesh.creation.cone(radius=20.0, height=40.0, sections=64)
    cone.apply_transform(trimesh.transformations.rotation_matrix(3.141592653589793, [1, 0, 0]))
    cone.apply_translation([0, 0, -cone.bounds[0][2]])
    return _save(tmp_path, "cone_tip", cone)


def _cone_on_its_base(tmp_path):
    cone = trimesh.creation.cone(radius=20.0, height=40.0, sections=64)
    cone.apply_translation([0, 0, -cone.bounds[0][2]])
    return _save(tmp_path, "cone_base", cone)


def _box_with_walls(tmp_path, wall_mm):
    outer = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    inner = trimesh.creation.box(extents=(20.0 - 2 * wall_mm, 20.0 - 2 * wall_mm, 30.0))
    inner.apply_translation([0, 0, wall_mm + 5.0])
    box = outer.difference(inner)
    box.apply_translation([0, 0, -box.bounds[0][2]])
    return _save(tmp_path, f"box_w{wall_mm}", box)


def _t_shape(tmp_path):
    stem = trimesh.creation.box(extents=(10.0, 10.0, 30.0))
    bar = trimesh.creation.box(extents=(60.0, 10.0, 8.0))
    bar.apply_translation([0, 0, 19.0])
    t = trimesh.util.concatenate([stem, bar])
    t.apply_translation([0, 0, -t.bounds[0][2]])
    return _save(tmp_path, "t_shape", t)


def test_a_part_balanced_on_a_point_is_never_printable(tmp_path):
    report = analyze_printability(_cone_on_its_tip(tmp_path), nozzle_diameter=0.4, material="pla")
    assert report.bed_adhesion.contact_area_mm2 < 0.16
    assert report.printable is False
    assert verdict(report)[0] == "wont_print"
    assert "bed" in report.recommendations[0].lower()


def test_the_same_cone_on_its_base_stays_printable(tmp_path):
    report = analyze_printability(_cone_on_its_base(tmp_path), nozzle_diameter=0.4, material="pla")
    assert report.printable is True
    assert verdict(report)[0] == "ready"


def test_walls_thinner_than_the_nozzle_are_never_printable(tmp_path):
    report = analyze_printability(_box_with_walls(tmp_path, 0.2), nozzle_diameter=0.4, material="pla")
    assert report.thin_walls.min_wall_thickness_mm < 0.4
    assert report.printable is False
    assert verdict(report)[0] == "wont_print"
    assert "nozzle" in report.recommendations[0].lower()


def test_walls_the_nozzle_can_lay_down_stay_printable(tmp_path):
    report = analyze_printability(_box_with_walls(tmp_path, 1.2), nozzle_diameter=0.4, material="pla")
    assert report.printable is True
    assert verdict(report)[0] == "ready"


def test_a_part_that_needs_supports_says_so_instead_of_not_recommended(tmp_path):
    report = analyze_printability(_t_shape(tmp_path), nozzle_diameter=0.4, material="pla")
    key, sentence = verdict(report)
    assert key == "prints_with_supports"
    assert sentence.startswith("Prints with supports")
    assert "Not recommended" not in sentence


def test_the_tool_message_leads_with_the_verdict(tmp_path):
    from kiln.plugins.printability_tools import _PrintabilityToolsPlugin

    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    _PrintabilityToolsPlugin().register(_Mcp())
    out = captured["analyze_printability"](file_path=_t_shape(tmp_path), nozzle_diameter=0.4)
    assert out["success"] is True
    assert out["verdict"] == "prints_with_supports"
    assert out["message"].startswith("Prints with supports")
    assert "Not recommended for printing" not in out["message"]


def _arch(tmp_path, span_mm, subdivisions):
    """Two 5 mm posts joined by a 5 mm deck: a flat bridge of ``span_mm``."""
    left = trimesh.creation.box(extents=(5.0, 10.0, 20.0))
    left.apply_translation([-(span_mm + 5.0) / 2, 0, 10.0])
    right = left.copy()
    right.apply_translation([span_mm + 5.0, 0, 0])
    deck = trimesh.creation.box(extents=(span_mm + 10.0, 10.0, 5.0))
    deck.apply_translation([0, 0, 22.5])
    arch = trimesh.util.concatenate([left, right, deck])
    for _ in range(subdivisions):
        arch = arch.subdivide()
    arch.apply_translation([0, 0, -arch.bounds[0][2]])
    return _save(tmp_path, f"arch_{span_mm}_{subdivisions}", arch)


def test_a_bridge_scores_the_same_at_any_mesh_resolution(tmp_path):
    # 2026-10-20: the same 30 mm arch scored 71, 65 and 63 as its mesh was
    # subdivided, because bridging charged per flat triangle.
    scores = {
        sub: analyze_printability(_arch(tmp_path, 30.0, sub), nozzle_diameter=0.4, material="pla")
        for sub in (0, 1, 2)
    }
    assert scores[0].bridging.needs_supports_for_bridges
    assert len({r.bridging.score_deduction for r in scores.values()}) == 1, {
        s: (r.bridging.bridge_count, r.bridging.score_deduction) for s, r in scores.items()
    }


def _block_with_a_horizontal_hole(tmp_path):
    block = trimesh.creation.box(extents=(30.0, 30.0, 30.0))
    hole = trimesh.creation.cylinder(radius=5.0, height=40.0, sections=48)
    hole.apply_transform(trimesh.transformations.rotation_matrix(1.5707963267948966, [1, 0, 0]))
    part = block.difference(hole)
    part.apply_translation([0, 0, -part.bounds[0][2]])
    return _save(tmp_path, "block_hole", part)


def test_a_part_that_needs_no_supports_is_not_charged_for_support_volume(tmp_path):
    # Every slicer bridges a 10 mm hole's crown; the report says no supports
    # are needed, yet the naive volume estimate (about 19%) took 5 points.
    report = analyze_printability(_block_with_a_horizontal_hole(tmp_path), nozzle_diameter=0.4, material="pla")
    assert report.supports.support_percentage > 5
    assert report.overhangs.needs_supports is False
    assert report.bridging.needs_supports_for_bridges is False
    assert report.supports.score_deduction == 0
