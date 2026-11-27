"""A piece the part holds in is made; a piece in open air is refused.

The floating-piece check refuses a make with a piece above the bed that is
joined to nothing.  A piece the part holds in on every side (a link of a
chain, a ring on a capped post, a ball in a cage of bars or in a cube with
holes smaller than itself) is a print-in-place mechanism, not scrap, at any
clearance.  The refusal says the rule, where each floating piece is in the
file's own coordinates, and never tells someone to fuse a piece that is
meant to move.

The parts are the shapes an agent's OpenSCAD makes, each source kept here.
Each is built in code with manifold3d, as OpenSCAD's own engine builds it,
so the tests need no OpenSCAD; where OpenSCAD is installed the sources are
compiled too and judged the same way.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
manifold3d = pytest.importorskip("manifold3d")

from kiln.fusion_check import FLOATING_REFUSAL_CODE, NEAR_GAP_MM, attach_fusion_report  # noqa: E402

M = manifold3d.Manifold


# ---------------------------------------------------------------------------
# OpenSCAD's shapes, built as OpenSCAD's engine builds them
# ---------------------------------------------------------------------------


def _cube(size, center=False):
    return M.cube(tuple(float(s) for s in size), center)


def _cylinder(h, r, fn, r2=None):
    return M.cylinder(float(h), float(r), float(r if r2 is None else r2), int(fn))


def _sphere(r, fn):
    """OpenSCAD's sphere: (fn + 1) // 2 rings of fn corners, the first and
    last half a ring from the poles, so a coarse one is flat at both ends."""
    rings = (fn + 1) // 2
    points = []
    for i in range(rings):
        phi = math.pi * (i + 0.5) / rings
        for j in range(fn):
            theta = 2 * math.pi * j / fn
            points.append((r * math.sin(phi) * math.cos(theta), r * math.sin(phi) * math.sin(theta), r * math.cos(phi)))
    return M.hull_points(np.asarray(points, dtype=np.float64))


def _torus(ring, tube, fn):
    """``rotate_extrude() translate([ring, 0]) circle(tube)``."""
    return manifold3d.CrossSection.circle(float(tube), int(fn)).translate((float(ring), 0.0)).revolve(int(fn))


def _polygon_prism(r, sides, h):
    """``linear_extrude(h) circle(r, $fn=sides)``."""
    return manifold3d.CrossSection.circle(float(r), int(sides)).extrude(float(h))


def _union(*parts):
    return M.batch_boolean([p for p in parts], manifold3d.OpType.Add)


def _letters(z, height=1.5):
    """Four block letters where ``text("KILN", size=10)`` stands at x=5, y=5."""
    return [_cube((8, 10, height)).translate((5 + 13 * i, 5, z)) for i in range(4)]


def _open_box():
    """``difference(){ cube([40,40,25]); translate([2,2,2]) cube([36,36,30]); }``."""
    return _cube((40, 40, 25)) - _cube((36, 36, 30)).translate((2, 2, 2))


def _link(ring=8.0, tube=1.6, fn=40):
    return _torus(ring, tube, fn)


def _chain(step):
    """Three links: the two ends stand on the bed, the middle one lies flat
    through both, ``step`` mm along x apart."""
    up = ring = 8.0 + 1.6
    return _union(
        _link().rotate((90, 0, 0)).translate((0, 0, up)),
        _link().translate((step, 0, ring)),
        _link().rotate((90, 0, 0)).translate((2 * step, 0, up)),
    )


def _ball_in_holed_cube(clearance):
    s, ball, hole = 30.0, 12.0, 9.0
    cube = _cube((s, s, s), center=True) - _sphere(ball + clearance, 64)
    for turn in ((0, 0, 0), (90, 0, 0), (0, 90, 0)):
        cube = cube - _cylinder(s + 2, hole, 64).translate((0, 0, -(s + 2) / 2)).rotate(turn)
    return _union(cube, _sphere(ball, 64))


def _ball_in_bar_cage():
    s, b = 30.0, 3.0
    bars = []
    for x in (0, s - b):
        for y in (0, s - b):
            bars.append(_cube((b, b, s)).translate((x, y, 0)))
    for x in (0, s - b):
        for z in (0, s - b):
            bars.append(_cube((b, s, b)).translate((x, 0, z)))
    for y in (0, s - b):
        for z in (0, s - b):
            bars.append(_cube((s, b, b)).translate((0, y, z)))
    return _union(*bars, _sphere(13, 48).translate((s / 2, s / 2, s / 2)))


def _ring_on_capped_post():
    ring = _cylinder(4, 12, 64) - _cylinder(6, 5.2, 64).translate((0, 0, -1))
    return _union(
        _cylinder(3, 20, 64), _cylinder(30, 4, 64), _cylinder(3, 7, 64).translate((0, 0, 30)),
        ring.translate((0, 0, 14)),
    )


def _mug(handle_at):
    wall = _cylinder(90, 40, 96) - _cylinder(90, 37, 96).translate((0, 0, 3))
    handle = _torus(12, 4, 96).rotate((90, 0, 0)).translate((handle_at, 0, 45))
    return _union(wall, handle)


def _hinge_pin_fused():
    r, pin, cl, length, kn = 4.0, 1.8, 0.4, 30.0, 6.0

    def knuckle(y):
        return _cylinder(kn - 0.4, r, 48).rotate((-90, 0, 0)).translate((0, y, r))

    leaf_a = _union(
        _cube((25 - r - 0.5, length, 2)).translate((-25, 0, 0)),
        *[knuckle(i * kn) for i in (0, 2, 4)],
        _cylinder(length, pin, 48).rotate((-90, 0, 0)).translate((0, 0, r)),
        _cube((2, length, 2)).translate((-r - 1, 0, 0)),
    )
    leaf_b = _union(
        _cube((25, length, 2)).translate((r + 0.5, 0, 0)),
        *[knuckle(i * kn) for i in (1, 3)],
        _cube((2, kn, 2)).translate((r - 1, kn, 0)),
        _cube((2, kn, 2)).translate((r - 1, 3 * kn, 0)),
    ) - _cylinder(length + 2, pin + cl, 48).rotate((-90, 0, 0)).translate((0, -1, r))
    return _union(leaf_a, leaf_b)


def _hinge_pin_loose():
    r, pin, cl, length = 5.0, 2.0, 1.0, 30.0
    bore = _cylinder(40, pin + cl, 48).rotate((-90, 0, 0)).translate((0, -1, r))
    block_a = _union(
        _cube((15, 14, 2 * r)).translate((-15, 0, 0)), _cylinder(14, r, 48).rotate((-90, 0, 0)).translate((0, 0, r)),
    ) - bore
    block_b = _union(
        _cube((15, 14, 2 * r)).translate((0, 16, 0)), _cylinder(14, r, 48).rotate((-90, 0, 0)).translate((0, 16, r)),
    ) - bore
    return _union(block_a, block_b, _cylinder(length, pin, 48).rotate((-90, 0, 0)).translate((0, 0, r)))


def _head_on_neck_coarse():
    eyes = [
        _sphere(3, 16).translate((22, 0, 0)).rotate((0, -10, 0)).rotate((0, 0, s * 20)).translate((0, 0, 40))
        for s in (-1, 1)
    ]
    return _union(_cylinder(20, 12, 8), _sphere(20, 8).translate((0, 0, 40)), *eyes)


def _text_on_cylinder():
    letters = [
        _cube((1.5, 6, 8)).translate((30, -3, 15)).rotate((0, 0, i * 12)) for i in range(5)
    ]
    return _union(_cylinder(40, 30, 30), *letters)


def _lid_seated():
    lip = _cube((35.6, 35.6, 4)) - _cube((32.6, 32.6, 6)).translate((1.5, 1.5, -1))
    return _union(_open_box(), _cube((40, 40, 2)).translate((0, 0, 25)), lip.translate((2.2, 2.2, 21)))


def _lid_beside_box():
    lip = _cube((35.6, 35.6, 4)) - _cube((32.6, 32.6, 6)).translate((1.5, 1.5, -1))
    return _union(_open_box(), _union(_cube((40, 40, 2)), lip.translate((2.2, 2.2, 2))).translate((50, 0, 0)))


def _token_in_closed_box():
    box = _cube((40, 40, 25)) - _cube((36, 36, 21)).translate((2, 2, 2))
    return _union(box, _cube((20, 20, 3)).translate((10, 10, 8)))


def _gear_on_axle():
    gear = _cylinder(6, 18, 48) - _cylinder(8, 4.4, 48).translate((0, 0, -1))
    return _union(
        _cylinder(3, 25, 48), _cylinder(12, 4, 48), _cylinder(2, 6, 48).translate((0, 0, 12)),
        gear.translate((0, 0, 3.4)),
    )


def _honeycomb_stand():
    """A base on the bed and three hexagonal cells hanging 2 mm over it."""
    cells = [_polygon_prism(3, 6, 3).translate((-12 + 12 * i, 0, 6)) for i in range(3)]
    return _union(_cube((40, 30, 4), center=True).translate((0, 0, 2)), *cells)


#: name: (the OpenSCAD source an agent wrote, the same shape built in code,
#: what Kiln does with it).  "held" is made and says the piece is captive.
PARTS: dict[str, tuple[str, object, str]] = {
    "text flush on its plate": (
        'cube([60,20,3]); translate([5,5,3]) linear_extrude(1.5) text("KILN", size=10);',
        lambda: _union(_cube((60, 20, 3)), *_letters(3)), "made",
    ),
    "text 0.2 mm over its plate": (
        'cube([60,20,3]); translate([5,5,3.2]) linear_extrude(1.5) text("KILN", size=10);',
        lambda: _union(_cube((60, 20, 3)), *_letters(3.2)), "made",
    ),
    "text 1 mm over its plate": (
        'cube([60,20,3]); translate([5,5,4]) linear_extrude(1.5) text("KILN", size=10);',
        lambda: _union(_cube((60, 20, 3)), *_letters(4)), "refused",
    ),
    "lid printed beside its box": (
        "difference(){ cube([40,40,25]); translate([2,2,2]) cube([36,36,30]); }\n"
        "translate([50,0,0]) union(){ cube([40,40,2]); translate([2.2,2.2,2]) difference(){ "
        "cube([35.6,35.6,4]); translate([1.5,1.5,-1]) cube([32.6,32.6,6]); } }",
        _lid_beside_box, "made",
    ),
    "lid seated on its box": (
        "difference(){ cube([40,40,25]); translate([2,2,2]) cube([36,36,30]); }\n"
        "translate([0,0,25]) cube([40,40,2]);\n"
        "translate([2.2,2.2,21]) difference(){ cube([35.6,35.6,4]); translate([1.5,1.5,-1]) cube([32.6,32.6,6]); }",
        _lid_seated, "made",
    ),
    "lid shown 10 mm over its box": (
        "difference(){ cube([40,40,25]); translate([2,2,2]) cube([36,36,30]); }\n"
        "translate([0,0,35]) cube([40,40,2]);",
        lambda: _union(_open_box(), _cube((40, 40, 2)).translate((0, 0, 35))), "refused",
    ),
    "a part centred on z beside one on the bed": (
        "difference(){ cube([40,40,25]); translate([2,2,2]) cube([36,36,30]); }\n"
        "translate([70,20,0]) cube([40,40,3], center=true);",
        lambda: _union(_open_box(), _cube((40, 40, 3), center=True).translate((70, 20, 0))), "refused",
    ),
    "hinge with its pin fused": (
        "$fn=48; r=4; pin=1.8; cl=0.4; L=30; kn=6;\n"
        "module knuckle(y){ translate([0,y,r]) rotate([-90,0,0]) cylinder(r=r,h=kn-0.4); }\n"
        "union(){ translate([-25,0,0]) cube([25-r-0.5,L,2]); for(i=[0,2,4]) knuckle(i*kn); "
        "translate([0,0,r]) rotate([-90,0,0]) cylinder(r=pin,h=L); translate([-r-1,0,0]) cube([2,L,2]); }\n"
        "difference(){ union(){ translate([r+0.5,0,0]) cube([25,L,2]); for(i=[1,3]) knuckle(i*kn); "
        "translate([r-1,kn,0]) cube([2,kn,2]); translate([r-1,3*kn,0]) cube([2,kn,2]);} "
        "translate([0,-1,r]) rotate([-90,0,0]) cylinder(r=pin+cl,h=L+2); }",
        _hinge_pin_fused, "made",
    ),
    "loose pin in its knuckles, 1 mm clearance": (
        "$fn=48; r=5; pin=2; cl=1.0; L=30;\n"
        "difference(){ union(){ translate([-15,0,0]) cube([15,14,2*r]); translate([0,0,r]) rotate([-90,0,0]) "
        "cylinder(r=r,h=14); } translate([0,-1,r]) rotate([-90,0,0]) cylinder(r=pin+cl,h=40); }\n"
        "difference(){ union(){ translate([0,16,0]) cube([15,14,2*r]); translate([0,16,r]) rotate([-90,0,0]) "
        "cylinder(r=r,h=14); } translate([0,-1,r]) rotate([-90,0,0]) cylinder(r=pin+cl,h=40); }\n"
        "translate([0,0,r]) rotate([-90,0,0]) cylinder(r=pin,h=L);",
        _hinge_pin_loose, "held",
    ),
    "ball in a cube with holes, 1 mm clearance": (
        "$fn=64; s=30; R=12; cl=1.0; hole=9;\n"
        "difference(){ cube(s,center=true); sphere(R+cl); for(a=[[0,0,0],[90,0,0],[0,90,0]]) rotate(a) "
        "cylinder(r=hole,h=s+2,center=true); }\nsphere(R);",
        lambda: _ball_in_holed_cube(1.0), "held",
    ),
    "ball in a cube with holes, 0.5 mm clearance": (
        "$fn=64; s=30; R=12; cl=0.5; hole=9;\n"
        "difference(){ cube(s,center=true); sphere(R+cl); for(a=[[0,0,0],[90,0,0],[0,90,0]]) rotate(a) "
        "cylinder(r=hole,h=s+2,center=true); }\nsphere(R);",
        lambda: _ball_in_holed_cube(0.5), "made",
    ),
    "ball in a cage of bars": (
        "$fn=48; s=30; b=3; R=13;\n"
        "module edges(){ for(x=[0,s-b],y=[0,s-b]) translate([x,y,0]) cube([b,b,s]); for(x=[0,s-b],z=[0,s-b]) "
        "translate([x,0,z]) cube([b,s,b]); for(y=[0,s-b],z=[0,s-b]) translate([0,y,z]) cube([s,b,b]); }\n"
        "edges();\ntranslate([s/2,s/2,s/2]) sphere(R);",
        _ball_in_bar_cage, "held",
    ),
    "chain of three links, 1 mm clearance": (
        "$fn=40; R=8; r=1.6;\nmodule link(){ rotate_extrude() translate([R,0]) circle(r); }\n"
        "translate([0,0,R+r]) rotate([90,0,0]) link();\ntranslate([2*R-2*r-1,0,R+r]) link();\n"
        "translate([2*(2*R-2*r-1),0,R+r]) rotate([90,0,0]) link();",
        lambda: _chain(2 * 8 - 2 * 1.6 - 1), "held",
    ),
    "chain of three links, 0.5 mm clearance": (
        "$fn=40; R=8; r=1.6; s=12.3;\nmodule link(){ rotate_extrude() translate([R,0]) circle(r); }\n"
        "translate([0,0,R+r]) rotate([90,0,0]) link();\ntranslate([s,0,R+r]) link();\n"
        "translate([2*s,0,R+r]) rotate([90,0,0]) link();",
        lambda: _chain(12.3), "made",
    ),
    "ring on a capped post, 1.2 mm clearance": (
        "$fn=64;\ncylinder(r=20,h=3); cylinder(r=4,h=30); translate([0,0,30]) cylinder(r=7,h=3);\n"
        "translate([0,0,14]) difference(){ cylinder(r=12,h=4); translate([0,0,-1]) cylinder(r=5.2,h=6); }",
        _ring_on_capped_post, "held",
    ),
    "mug handle sunk into the wall": (
        "$fn=96; R=40; H=90;\ndifference(){ cylinder(r=R,h=H); translate([0,0,3]) cylinder(r=R-3,h=H); }\n"
        "translate([R+1+12,0,H/2]) rotate([90,0,0]) rotate_extrude(angle=360) translate([12,0]) circle(4);",
        lambda: _mug(40 + 1 + 12), "made",
    ),
    "mug handle 1 mm off the wall": (
        "$fn=96; R=40; H=90;\ndifference(){ cylinder(r=R,h=H); translate([0,0,3]) cylinder(r=R-3,h=H); }\n"
        "translate([R+1+12+4,0,H/2]) rotate([90,0,0]) rotate_extrude(angle=360) translate([12,0]) circle(4);",
        lambda: _mug(40 + 1 + 12 + 4), "refused",
    ),
    "mug handle fused": (
        "$fn=96; R=40; H=90;\ndifference(){ cylinder(r=R,h=H); translate([0,0,3]) cylinder(r=R-3,h=H); }\n"
        "translate([R-2+12,0,H/2]) rotate([90,0,0]) rotate_extrude(angle=360) translate([12,0]) circle(4);",
        lambda: _mug(40 - 2 + 12), "made",
    ),
    "head on a neck, coarse facets leave a gap": (
        "$fn=8;\ncylinder(r=12,h=20); translate([0,0,40]) sphere(20);\n"
        "for(s=[-1,1]) translate([0,0,40]) rotate([0,0,s*20]) rotate([0,-10,0]) translate([20+3-1,0,0]) "
        "sphere(3, $fn=16);",
        _head_on_neck_coarse, "refused",
    ),
    "letters touching a round wall": (
        '$fn=0; r=30; cylinder(r=r,h=40);\nt="HELLO"; for(i=[0:len(t)-1]) rotate([0,0,i*12]) translate([r,0,15]) '
        'rotate([90,0,90]) linear_extrude(1.5) text(t[i], size=8, halign="center");',
        _text_on_cylinder, "made",
    ),
    "token loose inside a closed box": (
        "difference(){ cube([40,40,25]); translate([2,2,2]) cube([36,36,21]); }\n"
        "translate([10,10,8]) cube([20,20,3]);",
        _token_in_closed_box, "held",
    ),
    "star 1 mm off a box's side": (
        "cube([40,40,25]);\ntranslate([41,20,12]) rotate([0,90,0]) linear_extrude(2) circle(6,$fn=5);",
        lambda: _union(_cube((40, 40, 25)), _polygon_prism(6, 5, 2).rotate((0, 90, 0)).translate((41, 20, 12))),
        "refused",
    ),
    "a plate of pegs": (
        "for(i=[0:4],j=[0:3]) translate([i*12,j*12,0]) cylinder(r=4,h=10,$fn=24);",
        lambda: _union(*[_cylinder(10, 4, 24).translate((i * 12, j * 12, 0)) for i in range(5) for j in range(4)]),
        "made",
    ),
    "gear on its axle, 0.4 mm clearance": (
        "$fn=48;\ncylinder(r=25,h=3); cylinder(r=4,h=12); translate([0,0,12]) cylinder(r=6,h=2);\n"
        "translate([0,0,3.4]) difference(){ cylinder(r=18,h=6); translate([0,0,-1]) cylinder(r=4.4,h=8); }",
        _gear_on_axle, "made",
    ),
    "honeycomb cells hanging over a stand": (
        "translate([-20,-15,0]) cube([40,30,4]);\n"
        "for(i=[0:2]) translate([-12+12*i,0,6]) cylinder(r=3,h=3,$fn=6);",
        _honeycomb_stand, "refused",
    ),
}

#: The parts with a piece the part holds in on every side while leaving it
#: open above or below in places: held by standing in its way, not by a cover.
TRAPPED = {
    "ball in a cube with holes, 1 mm clearance",
    "ball in a cage of bars",
    "chain of three links, 1 mm clearance",
    "ring on a capped post, 1.2 mm clearance",
}


def _save(solid, path: Path, shift=(0.0, 0.0, 0.0)) -> Path:
    mesh = solid.to_mesh()
    vertices = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3] + np.asarray(shift, dtype=np.float64)
    trimesh.Trimesh(vertices=vertices, faces=np.asarray(mesh.tri_verts), process=False).export(path)
    return path


def _judge(path: Path) -> dict:
    return attach_fusion_report({"success": True, "message": "Compiled."}, str(path), refuse_floating=True)


def _verdict(response: dict) -> str:
    if response.get("code") == FLOATING_REFUSAL_CODE:
        return "refused"
    fusion = response.get("fusion") or {}
    assert response["success"] is True, response
    return "held" if fusion.get("captive_bodies") else "made"


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PARTS))
def test_each_part_gets_its_verdict(tmp_path, name):
    _source, build, expected = PARTS[name]
    response = _judge(_save(build(), tmp_path / "part.stl"))
    assert _verdict(response) == expected, (name, response.get("error") or response.get("warnings"))


@pytest.mark.parametrize("name", sorted(TRAPPED))
def test_a_piece_the_part_holds_in_on_every_side_is_made_and_said_as_captive(tmp_path, name):
    _source, build, _expected = PARTS[name]
    response = _judge(_save(build(), tmp_path / "part.stl"))
    fusion = response["fusion"]
    assert response["success"] is True and "code" not in response
    assert fusion["floating_bodies"] == 0 and fusion["captive_bodies"] >= 1
    assert any(w.startswith("captive pieces:") for w in response["warnings"])


def test_pieces_that_only_hold_one_another_in_the_air_are_still_refused(tmp_path):
    """Two links linked to each other, both hanging 6 mm over a plate that
    holds neither: each stands in the other's way, and still nothing joins
    them to what stands on the bed."""
    up = 6 + 8 + 1.6
    pair = _union(
        _link().rotate((90, 0, 0)).translate((0, 0, up)),
        _link().translate((2 * 8 - 2 * 1.6 - 1, 0, up)),
    )
    response = _judge(_save(_union(_cube((80, 40, 3), center=True).translate((6, 0, 1.5)), pair), tmp_path / "p.stl"))
    assert response.get("code") == FLOATING_REFUSAL_CODE
    assert response["fusion"]["floating_bodies"] == 2


def test_the_next_link_of_a_chain_held_by_a_held_link_is_held_too(tmp_path):
    """A chain standing straight up, 1 mm between links: the first link
    stands on the bed, the second hangs through it, and the third is held
    only by the second, with open air above it."""
    step = 2 * 8 - 2 * 1.6 - 1
    up = 8 + 1.6
    links = _union(
        _link().rotate((90, 0, 0)).translate((0, 0, up)),
        _link().rotate((0, 90, 0)).translate((0, 0, up + step)),
        _link().rotate((90, 0, 0)).translate((0, 0, up + 2 * step)),
    )
    response = _judge(_save(links, tmp_path / "upright.stl"))
    assert response["success"] is True, response.get("error")
    assert response["fusion"]["captive_bodies"] == 2 and response["fusion"]["floating_bodies"] == 0


# ---------------------------------------------------------------------------
# What a refusal says
# ---------------------------------------------------------------------------


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[.;:]\s", text) if c.strip()]


def test_the_refusal_says_the_rule_and_where_the_piece_is_in_the_files_own_coordinates(tmp_path):
    """A lid shown over its box, written far from the origin: the box the
    refusal names is the file's own, not one moved to the bed."""
    _source, build, _ = PARTS["lid shown 10 mm over its box"]
    response = _judge(_save(build(), tmp_path / "lid.stl", shift=(100, -50, 20)))
    assert response["code"] == FLOATING_REFUSAL_CODE
    error = response["error"]
    finding = next(f for f in response["fusion"]["findings"] if f["relation"] == "floating")

    assert "x 100.0 to 140.0, y -50.0 to -10.0, z 55.0 to 57.0 mm" in error
    assert finding["boxes"] == [{"body": finding["bodies"][0], "min_mm": [100.0, -50.0, 55.0], "max_mm": [140.0, -10.0, 57.0]}]
    assert "the part standing on the bed is 10.0 mm away" in error
    # The rule, as it is: joined, within the clearance, or held in.
    assert f"within {NEAR_GAP_MM} mm of the part" in error
    assert "the part holds it in" in error
    assert "envelope" not in error


def test_a_piece_meant_to_move_is_never_told_to_be_fused(tmp_path):
    for name in ("chain of three links, 1 mm clearance", "mug handle 1 mm off the wall"):
        _source, build, _ = PARTS[name]
        response = _judge(_save(build(), tmp_path / "part.stl"))
        said = " ".join([response.get("error") or "", *response.get("warnings", [])])
        moving = [c for c in _clauses(said) if "meant to move" in c]
        assert moving, (name, said)
        for clause in moving:
            assert "extend" not in clause and "overlap" not in clause, (name, clause)


def test_many_pieces_name_four_and_count_the_rest(tmp_path):
    cells = [_polygon_prism(3, 6, 3).translate((-30 + 12 * i, 0, 6)) for i in range(6)]
    response = _judge(_save(_union(_cube((80, 30, 4), center=True).translate((0, 0, 2)), *cells), tmp_path / "s.stl"))
    finding = next(f for f in response["fusion"]["findings"] if f["relation"] == "floating")
    assert len(finding["boxes"]) == 6
    assert response["error"].count(" spans x ") == 4
    assert "and 2 more" in response["error"]


# ---------------------------------------------------------------------------
# The same sources, compiled by OpenSCAD where it is installed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")
@pytest.mark.parametrize("name", sorted(PARTS))
def test_the_openscad_sources_get_the_same_verdicts(tmp_path, name):
    source, _build, expected = PARTS[name]
    scad, made = tmp_path / "part.scad", tmp_path / "part.stl"
    scad.write_text(source)
    run = subprocess.run(
        [shutil.which("openscad"), "-o", str(made), str(scad)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0 and made.exists(), run.stderr[-400:]
    response = _judge(made)
    assert _verdict(response) == expected, (name, response.get("error") or response.get("warnings"))
