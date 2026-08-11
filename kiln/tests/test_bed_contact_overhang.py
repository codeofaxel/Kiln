"""The face a model stands on is not an overhang.

The overhang convention scores a horizontal downward-facing face as 90°
— correct for a face in mid-air, wrong for the face resting on the build
plate.  Before 2026-08-11 every overhang door in Kiln counted a plain
box's own bottom as a 90° overhang: ``analyze_mesh`` returned
printability 80 with "Severe overhangs (90 degrees)" on a 20x10x5 box,
and ``auto_orient.estimate_supports`` returned the self-contradicting
pair ``needs_supports=True`` with ``estimated_support_volume_mm3=0.0``.

``kiln.printability._analyze_overhangs`` already had the right answer
via ``_is_bed_supported_triangle``; the other doors simply never called
it.  These tests pin every door to the shared helper, and — more
importantly — pin the safety floor: a real overhang must still be
caught.  A false negative here is a failed print, so the band that
counts as "on the plate" is deliberately tight (two layer heights).
"""

from __future__ import annotations

import math
import struct

import pytest

from kiln.auto_orient import estimate_supports
from kiln.generation.validation import (
    _bed_threshold_z,
    _is_bed_supported_triangle,
    _mesh_bed_z,
    analyze_mesh,
    estimate_support_volume,
    predict_print_failures,
)
from kiln.printability import _analyze_overhangs
from kiln.support_assessment import assess_support_feasibility

# ---------------------------------------------------------------------------
# Helpers — geometry generated inline so the intent stays next to the
# assertions (same convention as tests/regression/test_overhang_sweep.py).
# ---------------------------------------------------------------------------


def _write_stl(path: str, verts: list[tuple], faces: list[tuple]) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for face in faces:
            fh.write(struct.pack("<fff", 0.0, 0.0, 0.0))
            for vi in face:
                fh.write(struct.pack("<fff", *verts[vi]))
            fh.write(struct.pack("<H", 0))


def _box(path: str, x: float, y: float, z: float, z0: float = 0.0) -> None:
    """Axis-aligned box whose bottom face sits at ``z0``."""
    x2, y2 = x / 2.0, y / 2.0
    v = [
        (-x2, -y2, z0), (x2, -y2, z0), (x2, y2, z0), (-x2, y2, z0),
        (-x2, -y2, z0 + z), (x2, -y2, z0 + z),
        (x2, y2, z0 + z), (-x2, y2, z0 + z),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
        (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3),
    ]
    _write_stl(path, v, faces)


def _box_triangles(x: float, y: float, z: float) -> list[tuple]:
    """The same box as :func:`_box`, as in-memory triangles."""
    x2, y2 = x / 2.0, y / 2.0
    v = [
        (-x2, -y2, 0.0), (x2, -y2, 0.0), (x2, y2, 0.0), (-x2, y2, 0.0),
        (-x2, -y2, z), (x2, -y2, z), (x2, y2, z), (-x2, y2, z),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
        (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3),
    ]
    return [tuple(v[i] for i in face) for face in faces]


def _slope_wedge_triangles(
    overhang_deg: float, base: float = 30.0, height: float = 20.0
) -> list[tuple]:
    """Prism with two outward-leaning walls at ``overhang_deg``."""
    shift = height * math.tan(math.radians(overhang_deg))
    v = [
        (0.0, 0.0, 0.0), (base, 0.0, 0.0), (base, base, 0.0), (0.0, base, 0.0),
        (-shift, 0.0, height), (base + shift, 0.0, height),
        (base + shift, base, height), (-shift, base, height),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (3, 7, 6), (3, 6, 2),
        (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3),
    ]
    return [tuple(v[i] for i in face) for face in faces]


def _t_cantilever(path: str, post_h: float, z0: float = 0.0) -> None:
    """T-shape whose arm undersides are true 90° overhangs.

    ``post_h`` is the arm underside's height above the mesh's own lowest
    point (the post's foot), so it dials the distance from the plate
    directly.  The foot — not the arm — defines ``z_min``.
    """
    px2, ax2, arm_t, py2 = 4.0, 16.0, 4.0, 6.0
    z_top = post_h + arm_t
    section = [
        (px2, 0.0), (-px2, 0.0), (-px2, post_h), (-ax2, post_h),
        (-ax2, z_top), (ax2, z_top), (ax2, post_h), (px2, post_h),
    ]
    n = len(section)
    v: list[tuple] = [(x, -py2, z + z0) for (x, z) in section]
    v += [(x, py2, z + z0) for (x, z) in section]
    faces: list[tuple] = []
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, n + j))
        faces.append((i, n + j, n + i))
    for rect in ([1, 0, 7, 2], [3, 6, 5, 4]):
        faces.append((rect[0], rect[3], rect[2]))
        faces.append((rect[0], rect[2], rect[1]))
        faces.append((n + rect[0], n + rect[1], n + rect[2]))
        faces.append((n + rect[0], n + rect[2], n + rect[3]))
    _write_stl(path, v, faces)


# ---------------------------------------------------------------------------
# The bug: a plain box is the most printable object there is
# ---------------------------------------------------------------------------


def test_flat_box_has_no_overhangs(tmp_path):
    """The reported case: a 20x10x5 box scored 80 for its own bottom."""
    p = str(tmp_path / "box.stl")
    _box(p, 20, 10, 5)
    a = analyze_mesh(p)

    assert a.max_overhang_angle_deg == 0.0
    assert a.overhang_triangle_count == 0
    assert a.printability_score == 100
    assert not any("verhang" in issue for issue in a.printability_issues)


def test_flat_box_needs_no_supports(tmp_path):
    p = str(tmp_path / "box.stl")
    _box(p, 20, 10, 5)
    result = estimate_support_volume(p)

    assert result["needs_supports"] is False
    assert result["support_volume_mm3"] == 0.0
    assert result["overhang_triangle_count"] == 0


def test_flat_box_predicts_no_overhang_failure(tmp_path):
    p = str(tmp_path / "box.stl")
    _box(p, 20, 10, 5)
    failures = predict_print_failures(p)["failures"]

    assert not [f for f in failures if "overhang" in f["type"]]


def test_estimate_supports_never_contradicts_itself(tmp_path):
    """``needs_supports=True`` alongside ``0.0`` volume is incoherent.

    That pair was the symptom that ``_analyze_overhangs`` was counting
    the bed face while ``_analyze_supports`` — which got ``z_min`` —
    correctly excluded it.
    """
    p = str(tmp_path / "box.stl")
    _box(p, 20, 10, 5)
    est = estimate_supports(p)

    assert est.needs_supports is False
    assert est.estimated_support_volume_mm3 == 0.0
    if est.needs_supports:
        assert est.estimated_support_volume_mm3 > 0.0


def test_support_assessment_does_not_recommend_supports_for_a_box(tmp_path):
    """``support_assessment`` reaches the same rule via its own engine.

    It used to bill the box's 20x10 bottom as 200mm² of overhang and
    recommend tree supports for the most printable object there is.
    """
    p = str(tmp_path / "box.stl")
    _box(p, 20, 10, 5)
    result = assess_support_feasibility(stl_path=p, material="PLA")

    assert result.needs_supports is False
    assert result.overhang_area_mm2 == 0.0
    assert result.recommended_support_type == "none"
    # The bed face is still real surface — only its verdict changed.
    assert result.total_surface_area_mm2 > 0


def test_support_assessment_still_catches_a_real_overhang():
    """Safety floor for the same engine: a slab over a footing is caught."""
    slab = [
        ((0.0, 0.0, 10.0), (20.0, 20.0, 10.0), (20.0, 0.0, 10.0)),
        ((0.0, 0.0, 10.0), (0.0, 20.0, 10.0), (20.0, 20.0, 10.0)),
    ]
    footing = [
        ((0.0, 0.0, 0.0), (20.0, 20.0, 0.0), (20.0, 0.0, 0.0)),
        ((0.0, 0.0, 0.0), (0.0, 20.0, 0.0), (20.0, 20.0, 0.0)),
    ]
    result = assess_support_feasibility(triangles=slab + footing, material="PLA")

    assert result.needs_supports is True
    assert result.overhang_area_mm2 > 0


# ---------------------------------------------------------------------------
# The safety floor — a real overhang must still be caught
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("post_h", [30.0, 5.0, 1.0, 0.5])
def test_real_cantilever_still_flagged(tmp_path, post_h):
    """Arm undersides in the air stay 90° overhangs, down to 0.5mm up.

    0.5mm is just outside the two-layer band; it must not be swallowed.
    A false negative here means a failed print.
    """
    p = str(tmp_path / f"cant_{post_h}.stl")
    _t_cantilever(p, post_h)

    a = analyze_mesh(p)
    assert a.max_overhang_angle_deg == pytest.approx(90.0)
    assert a.overhang_triangle_count > 0

    est = estimate_support_volume(p)
    assert est["needs_supports"] is True
    assert est["support_volume_mm3"] > 0.0


def test_face_within_two_layers_counts_as_bed(tmp_path):
    """0.3mm above the plate is inside the band — the first layers pin it."""
    p = str(tmp_path / "cant_low.stl")
    _t_cantilever(p, 0.3)

    assert analyze_mesh(p).max_overhang_angle_deg == 0.0
    assert estimate_support_volume(p)["needs_supports"] is False


# ---------------------------------------------------------------------------
# The plate is the mesh's own minimum Z, not an absolute z=0
# ---------------------------------------------------------------------------


def test_scores_are_placement_invariant(tmp_path):
    """Where the exporter put the origin must not change the verdict."""
    grounded = str(tmp_path / "grounded.stl")
    lifted = str(tmp_path / "lifted.stl")
    _box(grounded, 20, 10, 5, z0=0.0)
    _box(lifted, 20, 10, 5, z0=25.0)

    a, b = analyze_mesh(grounded), analyze_mesh(lifted)
    assert a.printability_score == b.printability_score == 100
    assert a.max_overhang_angle_deg == b.max_overhang_angle_deg == 0.0

    sa = estimate_support_volume(grounded)
    sb = estimate_support_volume(lifted)
    assert sa["support_volume_mm3"] == sb["support_volume_mm3"] == 0.0
    assert sb["needs_supports"] is False


def test_lifted_cantilever_bills_only_the_gap(tmp_path):
    """Support height is measured from the plate, not from z=0.

    A model authored above the origin used to be billed for the empty
    space underneath it as well as its real overhang.
    """
    grounded = str(tmp_path / "cant.stl")
    lifted = str(tmp_path / "cant_lifted.stl")
    _t_cantilever(grounded, 30.0, z0=0.0)
    _t_cantilever(lifted, 30.0, z0=25.0)

    assert (
        estimate_support_volume(grounded)["support_volume_mm3"]
        == estimate_support_volume(lifted)["support_volume_mm3"]
    )


# ---------------------------------------------------------------------------
# The shared helper itself
# ---------------------------------------------------------------------------


def test_helper_requires_every_vertex_on_the_plate():
    """A face touching the plate along one edge is still an overhang.

    Testing the lowest vertex alone would erase real overhangs — a cone
    tip or a wedge touches down without resting flat.
    """
    flat = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    tilted = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 5.0))

    assert _is_bed_supported_triangle(flat, 0.0) is True
    assert _is_bed_supported_triangle(tilted, 0.0) is False


def test_helper_band_is_two_layer_heights():
    """The band scales with layer height and excludes anything above it."""
    inside = ((0.0, 0.0, 0.39), (1.0, 0.0, 0.39), (0.0, 1.0, 0.39))
    outside = ((0.0, 0.0, 0.41), (1.0, 0.0, 0.41), (0.0, 1.0, 0.41))

    assert _is_bed_supported_triangle(inside, 0.0, 0.2) is True
    assert _is_bed_supported_triangle(outside, 0.0, 0.2) is False
    # Same faces, thicker layers — the band grows with them.
    assert _is_bed_supported_triangle(outside, 0.0, 0.3) is True


def test_helper_is_relative_to_the_given_plate():
    """z_min moves the band; the helper never assumes z=0."""
    tri = ((0.0, 0.0, 25.0), (1.0, 0.0, 25.0), (0.0, 1.0, 25.0))

    assert _is_bed_supported_triangle(tri, 25.0) is True
    assert _is_bed_supported_triangle(tri, 0.0) is False


def test_band_helper_and_triangle_test_agree():
    """The band is defined once, so the two helpers cannot drift apart."""
    for layer_height in (0.1, 0.2, 0.3):
        top = _bed_threshold_z(0.0, layer_height)
        assert top == layer_height * 2.0
        at_top = ((0.0, 0.0, top), (1.0, 0.0, top), (0.0, 1.0, top))
        just_over = (
            (0.0, 0.0, top + 1e-6),
            (1.0, 0.0, top),
            (0.0, 1.0, top),
        )
        assert _is_bed_supported_triangle(at_top, 0.0, layer_height) is True
        assert _is_bed_supported_triangle(just_over, 0.0, layer_height) is False


# ---------------------------------------------------------------------------
# The plate comes from geometry, not from the parsed vertex list
# ---------------------------------------------------------------------------


_BOX_OBJ_FACES = """f 1 3 2
f 1 4 3
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 3 4 8
f 3 8 7
f 2 3 7
f 2 7 6
f 1 5 8
f 1 8 4
"""

_BOX_OBJ_VERTS = """v 0 0 0
v 20 0 0
v 20 10 0
v 0 10 0
v 0 0 5
v 20 0 5
v 20 10 5
v 0 10 5
"""


def test_unreferenced_vertex_does_not_move_the_plate(tmp_path):
    """An OBJ ``v`` line no face uses must not become the build plate.

    OBJ files routinely carry vertices no face references.  Taking the
    plate from the parsed vertex list let a single stray vertex below
    the model drop the plate out from under it, silently restoring the
    90° bottom-face overhang this module exists to prevent.
    """
    clean = tmp_path / "clean.obj"
    clean.write_text(_BOX_OBJ_VERTS + _BOX_OBJ_FACES)

    # Same box, plus one unreferenced vertex 50mm below it.  Face indices
    # are unchanged because the stray vertex is appended last.
    stray = tmp_path / "stray.obj"
    stray.write_text(_BOX_OBJ_VERTS + "v 0 0 -50\n" + _BOX_OBJ_FACES)

    a, b = analyze_mesh(str(clean)), analyze_mesh(str(stray))

    assert a.max_overhang_angle_deg == 0.0
    assert b.max_overhang_angle_deg == 0.0
    assert b.overhang_triangle_count == 0


def test_mesh_bed_z_reads_geometry_not_the_vertex_list():
    tri_low = ((0.0, 0.0, 2.0), (1.0, 0.0, 2.0), (0.0, 1.0, 2.0))
    tri_high = ((0.0, 0.0, 9.0), (1.0, 0.0, 9.0), (0.0, 1.0, 9.0))

    assert _mesh_bed_z([tri_low, tri_high]) == 2.0
    assert _mesh_bed_z([]) == 0.0


def test_a_sheet_thinner_than_the_band_has_no_overhangs(tmp_path):
    """A 0.3mm plate is one layer lying on the plate, not an overhang."""
    p = str(tmp_path / "sheet.stl")
    _box(p, 20, 10, 0.3)

    assert analyze_mesh(p).max_overhang_angle_deg == 0.0
    assert estimate_support_volume(p)["needs_supports"] is False


# ---------------------------------------------------------------------------
# Tier: the bed rule is physics, the angle threshold is the paid judgement
# ---------------------------------------------------------------------------


def test_bed_rule_is_identical_in_both_tiers():
    """Resting on the plate is geometry, so it must never be gated.

    ``_analyze_overhangs`` applies the bed test before it resolves any
    threshold, so a Pro overlay can raise or lower the angle a face must
    beat without changing which faces the bed is holding up.  If this
    ever fails, a safety-relevant fact has drifted behind the paywall.
    """
    box = _box_triangles(20, 10, 5)
    z_min = _mesh_bed_z(box)
    pro_overlay = {
        "overhangs": {
            "default_limit_deg": 45.0,
            "material_limits_deg": {"PLA": 55.0},
        }
    }

    free = _analyze_overhangs(box, z_min=z_min, material="PLA")
    pro = _analyze_overhangs(box, z_min=z_min, material="PLA", overlay=pro_overlay)

    assert free.needs_supports is pro.needs_supports is False
    assert free.max_overhang_angle == pro.max_overhang_angle == 0.0


def test_material_threshold_still_separates_the_tiers():
    """The seam the bed fix must NOT have flattened.

    A 50 degree slope beats the free tier's universal 45 degree floor but
    not the overlay's PLA value, so the two tiers must still disagree.
    """
    wedge = _slope_wedge_triangles(50.0)
    z_min = _mesh_bed_z(wedge)
    pro_overlay = {
        "overhangs": {
            "default_limit_deg": 45.0,
            "material_limits_deg": {"PLA": 55.0},
        }
    }

    free = _analyze_overhangs(wedge, z_min=z_min, material="PLA")
    pro = _analyze_overhangs(wedge, z_min=z_min, material="PLA", overlay=pro_overlay)

    assert free.needs_supports is True
    assert pro.needs_supports is False
