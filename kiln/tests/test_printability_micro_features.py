"""Regression tests: self-supporting micro-features must not tank the
printability grade.

Background (2026-08-25 threaded-jar audit): Kiln's own ``threaded_jar``
template — a hollow cylinder plus a ring lid any printer handles with no
supports — graded F (52/100).  Three engine defects stacked:

1. ``max_bridge_length_mm`` was the bridge region's XY-bounding-box max
   dimension, so a narrow band (a debossed logo's 1 mm-deep recess
   ceiling, a helical relief) reported its LENGTH (34 mm) instead of the
   distance a slicer actually bridges (~1 mm), tripping the public
   >10 mm deduction (-15) AND the pro overlay's per-material bridging
   penalty (-8) for the same phantom span.
2. Flat micro-undersides attached to a wall (thread reliefs, flange
   lips) were counted as support-needing overhangs even though they
   bridge or laterally close over a nozzle-width or two.
3. The thermal-stress analyzer called a cross-section step "critical"
   even when nothing but a sliver of print remained above it (the
   template's old spike rim), deducting -15 on a trivially printable
   part.

The fixes are general (supported-chord span measurement, per-region
self-supporting exemption, remaining-structure significance floor) —
these tests pin them with synthetic meshes, no OpenSCAD needed.
"""

from __future__ import annotations

import math
import os
import struct

from kiln.printability import analyze_printability


# ---------------------------------------------------------------------------
# Mesh builders
# ---------------------------------------------------------------------------


def _make_binary_stl(triangles: list[tuple]) -> bytes:
    out = bytearray(b"\x00" * 80)
    out += struct.pack("<I", len(triangles))
    for tri in triangles:
        out += struct.pack("<fff", 0.0, 0.0, 0.0)
        for v in tri:
            out += struct.pack("<fff", *v)
        out += struct.pack("<H", 0)
    return bytes(out)


def _write_stl(tmpdir, name: str, triangles: list[tuple]) -> str:
    path = os.path.join(str(tmpdir), name)
    with open(path, "wb") as fh:
        fh.write(_make_binary_stl(triangles))
    return path


def _hollow_cylinder_triangles(
    *,
    outer_r: float = 27.5,
    wall: float = 2.4,
    height: float = 55.0,
    segments: int = 48,
) -> list[tuple]:
    """Open-top hollow cylinder (a plain jar body): outer wall, inner
    wall, solid floor, flat rim."""
    inner_r = outer_r - wall
    floor = wall
    tris: list[tuple] = []

    def ring(r: float, z: float) -> list[tuple[float, float, float]]:
        return [
            (r * math.cos(2 * math.pi * i / segments),
             r * math.sin(2 * math.pi * i / segments), z)
            for i in range(segments)
        ]

    ob, ot = ring(outer_r, 0.0), ring(outer_r, height)
    ib, it = ring(inner_r, floor), ring(inner_r, height)
    ctr_bottom = (0.0, 0.0, 0.0)
    ctr_floor = (0.0, 0.0, floor)
    for i in range(segments):
        j = (i + 1) % segments
        # outer wall (outward)
        tris.append((ob[i], ob[j], ot[j]))
        tris.append((ob[i], ot[j], ot[i]))
        # inner wall (inward)
        tris.append((ib[i], it[j], ib[j]))
        tris.append((ib[i], it[i], it[j]))
        # bottom disc (down)
        tris.append((ctr_bottom, ob[j], ob[i]))
        # floor top disc (up)
        tris.append((ctr_floor, ib[i], ib[j]))
        # rim annulus (up)
        tris.append((ot[i], ot[j], it[j]))
        tris.append((ot[i], it[j], it[i]))
    return tris


def _extruded_profile_triangles(
    profile: list[tuple[float, float]],
    cap_triangles: list[tuple[int, int, int]],
    depth: float = 12.0,
) -> list[tuple]:
    """Extrude a closed 2D XZ profile along +Y with pre-triangulated
    caps.  ``profile`` is counter-clockwise viewed from -Y."""
    front = [(x, 0.0, z) for x, z in profile]
    back = [(x, depth, z) for x, z in profile]
    tris: list[tuple] = []
    n = len(profile)
    for i in range(n):
        j = (i + 1) % n
        tris.append((front[i], front[j], back[j]))
        tris.append((front[i], back[j], back[i]))
    for a, b, c in cap_triangles:
        tris.append((front[a], front[c], front[b]))
        tris.append((back[a], back[b], back[c]))
    return tris


# 2 mm-thick, 30 mm-tall wall with a 1 mm bump protruding at z 15–17:
# the bump's flat 1 mm underside is a classic self-supporting flange lip.
_BUMP_WALL_PROFILE = [
    (0.0, 0.0), (2.0, 0.0), (2.0, 15.0), (3.0, 15.0),
    (3.0, 17.0), (2.0, 17.0), (2.0, 30.0), (0.0, 30.0),
]
_BUMP_WALL_CAPS = [
    (0, 1, 2), (0, 2, 6), (0, 6, 7), (2, 3, 4), (2, 4, 5),
]

# Same wall with a 1 mm-deep pocket (recess) at z 15–17 instead — the
# debossed-logo case.  The pocket ceiling at z=17 spans 1 mm radially
# but the full extrusion depth tangentially.
_POCKET_WALL_PROFILE = [
    (0.0, 0.0), (2.0, 0.0), (2.0, 15.0), (1.0, 15.0),
    (1.0, 17.0), (2.0, 17.0), (2.0, 30.0), (0.0, 30.0),
]
_POCKET_WALL_CAPS = [
    (0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 5), (0, 5, 6), (0, 6, 7),
]

# 6 mm-wide, 20 mm-tall wall with a 1 mm-wide, 3 mm-tall spike on top —
# the cross-section collapses 6:1 at z=20 with only the spike above.
_SPIKE_TOP_PROFILE = [
    (0.0, 0.0), (6.0, 0.0), (6.0, 20.0), (4.0, 20.0),
    (4.0, 23.0), (3.0, 23.0), (3.0, 20.0), (0.0, 20.0),
]
_SPIKE_TOP_CAPS = [
    (0, 1, 2), (0, 2, 7), (7, 2, 3), (7, 3, 6), (3, 4, 5), (3, 5, 6),
]


def _box_triangles(
    x0: float, y0: float, z0: float,
    x1: float, y1: float, z1: float,
) -> list[tuple]:
    v = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


# ---------------------------------------------------------------------------
# The headline regression: trivially printable parts are not graded F.
# ---------------------------------------------------------------------------


class TestPlainHollowCylinder:
    def test_not_graded_f(self, tmp_path):
        """A plain open-top hollow cylinder — the most printable shape
        in FDM — must grade well.  This is the threaded_jar body minus
        the thread; it graded F (52) before the 2026-08-25 fixes."""
        p = _write_stl(tmp_path, "cylinder.stl", _hollow_cylinder_triangles())
        r = analyze_printability(p, material="pla")
        assert r.grade != "F"
        assert r.printable
        assert r.score >= 80, (
            f"plain hollow cylinder scored {r.score} ({r.grade}); "
            f"recommendations: {r.recommendations}"
        )
        assert not r.overhangs.needs_supports
        assert not r.bridging.needs_supports_for_bridges


class TestSelfSupportingMicroFeatures:
    def test_flange_lip_is_not_a_support_case(self, tmp_path):
        """A 1 mm bump on a wall has a flat underside, but it closes by
        lateral reach from the wall below — no supports, no deduction."""
        p = _write_stl(
            tmp_path, "bump.stl",
            _extruded_profile_triangles(_BUMP_WALL_PROFILE, _BUMP_WALL_CAPS),
        )
        r = analyze_printability(p, material="pla")
        assert not r.overhangs.needs_supports
        assert not r.bridging.needs_supports_for_bridges
        assert r.score >= 80

    def test_recess_ceiling_reports_depth_not_length(self, tmp_path):
        """A 1 mm-deep pocket in a wall (a debossed logo) has a ceiling
        that is 12 mm LONG but only 1 mm deep.  The pre-fix XY-bbox
        measure reported 12 mm and tripped the >10 mm long-bridge
        deduction (this exact mechanism scored the threaded-jar case at
        34.08 mm); the honest span is the pocket depth."""
        p = _write_stl(
            tmp_path, "pocket.stl",
            _extruded_profile_triangles(
                _POCKET_WALL_PROFILE, _POCKET_WALL_CAPS,
            ),
        )
        r = analyze_printability(p, material="pla")
        assert r.bridging.bridge_count > 0  # the ceiling IS detected
        assert r.bridging.max_bridge_length_mm < 5.0, (
            f"recess ceiling measured {r.bridging.max_bridge_length_mm} mm "
            f"— bbox-length regression?"
        )
        assert not r.bridging.needs_supports_for_bridges
        assert not r.overhangs.needs_supports

    def test_feature_flush_on_floor_is_supported(self, tmp_path):
        """A boolean seam: a block starting a hair above a floor slab is
        topologically unanchored but rests on solid material — printable
        as an ordinary layer bond."""
        tris = _box_triangles(0, 0, 0, 20, 20, 2)
        tris += _box_triangles(8, 8, 2.05, 11, 11, 5)
        p = _write_stl(tmp_path, "flush.stl", tris)
        r = analyze_printability(p, material="pla")
        assert not r.bridging.needs_supports_for_bridges
        assert not r.overhangs.needs_supports

    def test_floating_island_still_flagged(self, tmp_path):
        """Counterpart to the flush-on-floor case: the same block
        floating mid-air with nothing below must keep its support
        verdict — the exemption must not swallow genuine islands."""
        tris = _box_triangles(0, 0, 0, 20, 20, 2)
        tris += _box_triangles(8, 8, 10.0, 11, 11, 13)
        p = _write_stl(tmp_path, "island.stl", tris)
        r = analyze_printability(p, material="pla")
        assert r.bridging.needs_supports_for_bridges
        assert r.overhangs.needs_supports


class TestThermalSignificanceFloor:
    def test_spike_rim_is_not_critical(self, tmp_path):
        """A thin decorative spike atop a wall collapses the
        cross-section 6:1, but with only a sliver of print above the
        step there is nothing for contraction stress to crack.  The old
        analyzer called this critical (-15) — the threaded_jar's spiky
        rim artifact."""
        p = _write_stl(
            tmp_path, "spike.stl",
            _extruded_profile_triangles(
                _SPIKE_TOP_PROFILE, _SPIKE_TOP_CAPS,
            ),
        )
        r = analyze_printability(p, material="pla")
        assert r.thermal_stress is not None
        assert r.thermal_stress.risk_level in ("low", "moderate")
        assert r.thermal_stress.score_deduction == 0


class TestThreadedJarTemplate:
    """The template itself: a real swept helical thread on both parts,
    not the old string-of-cones (which built a sawtooth rim, overhang
    spikes past the body height, and a lid with no thread at all)."""

    def _template(self):
        import json
        from pathlib import Path
        data = Path(__file__).parent.parent / "src" / "kiln" / "data"
        return json.loads(
            (data / "design_templates.json").read_text()
        )["threaded_jar"]

    def test_swept_thread_replaces_cone_string(self):
        scad = self._template()["scad_template"]
        assert "helical_thread" in scad
        # The cone-string signature: pointed cylinders (d2=0) stacked
        # along the helix.
        assert "d2=0" not in scad.replace(" ", "")

    def test_lid_gets_internal_thread(self):
        scad = self._template()["scad_template"]
        lid_body = scad.split("module lid()")[1]
        assert "helical_thread" in lid_body

    def test_thread_stays_flush_with_body(self):
        """The sweep is inset so max Z equals the body height — the old
        cones overshot the rim by 1.5 mm and read as a sawtooth."""
        scad = self._template()["scad_template"]
        assert "length - 2 * half" in scad
