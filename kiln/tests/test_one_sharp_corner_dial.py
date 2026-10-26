"""One sharp-corner threshold, every caller: the tools leave it to Kiln, and
no overlay moves it.

How far the surface must turn inward across an inside edge for the corner to
count as sharp is one value (``kiln.design_reasoning._SHARP_ANGLE_THRESHOLD_DEG_PUBLIC``).
Until 2026-10-24 three tools passed their own number, pinning the dial for
every caller below the engine's floor, and an overlay could carry a second
number that -- under the check's own definition, where a lower value is
stricter -- could only quiet or sharpen a warning the floor had already
decided.  The minimum cross-section the same tools passed was likewise their
own number, looser than the engine's floor, so a plain install was told less
than the engine would have said.

The parts are joined the way a real part is (a boolean union) and read
through the tools a person calls, as a plain install answers them.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d", reason="the parts are joined with a boolean union")

from tests import _real_exports as rx  # noqa: E402
from tests.test_sharp_corners_are_inside_corners import _box, _call, _save, _union  # noqa: E402

PLATE = ((0, 0, 0), (60, 60, 4))


def _leaning_rib(lean_deg, base_mm=12.0, height_mm=8.0):
    """A rib whose sides lean ``lean_deg`` in from vertical, running the whole
    length of the plate so its ends are outside faces: the surface turns
    ``90 - lean_deg`` degrees across each foot edge, and nowhere else."""
    z0, z1 = 3.0, 4.0 + height_mm  # the foot sits 1 mm into the plate, so the union is one solid
    half_top = base_mm / 2.0 - (z1 - z0) * math.tan(math.radians(lean_deg))
    assert half_top > 0.2
    x0, x1, xt0, xt1, y0, y1 = 30 - base_mm / 2, 30 + base_mm / 2, 30 - half_top, 30 + half_top, 0, 60
    v = [(x0, y0, z0), (x1, y0, z0), (xt1, y0, z1), (xt0, y0, z1), (x0, y1, z0), (x1, y1, z0), (xt1, y1, z1), (xt0, y1, z1)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    rib = trimesh.Trimesh(vertices=v, faces=f)
    rib.fix_normals()
    return _union(_box(*PLATE), rib)


def _polygon_pocket(sides, radius_mm=10.0):
    """A pocket with ``sides`` straight walls cut right through a block, so
    its only inside edges are the corners between walls, which turn
    ``360 / sides`` degrees."""
    angles = np.linspace(0, 2 * math.pi, sides, endpoint=False)
    ring = np.stack([30 + radius_mm * np.cos(angles), 30 + radius_mm * np.sin(angles)], axis=1)
    bottom = [(x, y, -1.0) for x, y in ring]
    top = [(x, y, 13.0) for x, y in ring]
    verts = bottom + top
    faces = []
    for i in range(sides):
        j = (i + 1) % sides
        faces += [(i, j, sides + j), (i, sides + j, sides + i)]
    for i in range(1, sides - 1):
        faces += [(0, i + 1, i), (sides, sides + i, sides + i + 1)]
    cutter = trimesh.Trimesh(vertices=verts, faces=faces)
    cutter.fix_normals()
    return trimesh.boolean.difference([_box((0, 0, 0), (60, 60, 12)), cutter], engine="manifold")


def _corners(path, **kw):
    reply = _call("analyze_structural_risks", file_path=path, **kw)
    assert reply.get("success"), reply
    return [r for r in reply["risks"] if r["risk_type"] == "sharp_corner"]


def test_the_tools_leave_the_threshold_to_kiln(tmp_path):
    """A foot edge turning 62 degrees is below Kiln's threshold and is not a
    corner through any door; one turning 70 is."""
    gentle = _save(_leaning_rib(28.0), tmp_path, "gentle")  # turns 62
    assert _corners(gentle) == []
    assert "sharp" not in _call("design_improvement_plan", file_path=gentle)["summary"]
    steep = _save(_leaning_rib(20.0), tmp_path, "steep")  # turns 70
    corners = _corners(steep)  # the two foot edges, 24 mm apart: two corners
    assert len(corners) == 2
    assert all(c["severity"] == "warning" and c["metric_value"] == pytest.approx(70.0, abs=0.5) for c in corners)
    # A caller may still pin another value.
    assert len(_corners(gentle, sharp_angle_threshold_deg=55.0)) == 2


def test_a_hexagonal_pocket_is_not_a_sharp_corner_and_a_pentagonal_one_is(tmp_path):
    assert _corners(_save(_polygon_pocket(6), tmp_path, "hex")) == []
    pentagon = _corners(_save(_polygon_pocket(5), tmp_path, "pent"))
    assert pentagon and all(c["metric_value"] == pytest.approx(72.0, abs=0.5) for c in pentagon)


def test_no_overlay_moves_the_threshold(tmp_path, monkeypatch):
    """A second number for the dial, from anywhere, changes nothing: an inside
    corner that counts is a warning for every caller, never a note."""
    import kiln.design_intelligence as di

    steep = _save(_leaning_rib(20.0), tmp_path, "steep")
    for pretend in (30.0, 80.0):
        monkeypatch.setattr(di, "load_pro_overlay_or_empty", lambda kind, p=pretend: {"sharp_angle_threshold_deg": p})
        from kiln import server

        with rx.without_kiln_pro():
            found = [r for r in rx.call(server.mcp, "analyze_structural_risks", {"file_path": steep})["risks"]
                     if r["risk_type"] == "sharp_corner"]
        assert [r["severity"] for r in found] == ["warning", "warning"], (pretend, found)
        assert not any("Borderline" in r["description"] for r in found)


def test_the_tools_leave_the_minimum_section_to_kiln(tmp_path):
    """A rod of 4.5 mm2 is below Kiln's floor and is a thin neck through every
    door that reads sections."""
    rod = _save(_box((0, 0, 0), (2.12, 2.12, 30)), tmp_path, "rod")
    for door in ("analyze_structural_risks", "design_improvement_plan"):
        risks = _call(door, file_path=rod)["risks"]
        assert [r for r in risks if r["risk_type"] == "thin_neck"], door
    recs = _call("recommend_design_reinforcements", file_path=rod)["reinforcements"]
    assert [r for r in recs if r["addresses_risk"] == "thin_neck"]
