"""Two print-in-place mechanisms, built in code for the tests that read them.

A lazy tongs (eight flat bars in two layers, ten waisted captive pins) and a
robot claw (a handle, a slider, two links and two fingers on six pins and a
rod in a tunnel).  Every running gap is drawn across every face, so one gap
can be set per joint: the tongs take a gap for each joint, the claw one gap
for every pin and one for the rod.  The geometry follows the OpenSCAD
sources the parts were first drawn in, built with manifold3d so a test needs
no OpenSCAD.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_EF = 0.5  # the relief where a moving gap meets the bed
_H = 5.0  # the height of what lies on the bed
_ZB = 7.5  # the underside of the upper layer
_ZT = 10.5  # the top
_RO = 7.0  # an eye's outer radius
_PIN = [
    (0.0, 0.0), (3.6 - _EF, 0.0), (3.6, _EF), (2.4, 2.5), (3.6, 5.0), (3.6, 5.6),
    (5.5, _ZB), (5.5, _ZT), (0.0, _ZT),
]

#: The tongs' joint pitch along the arms and across them, mm.
_X = 40.9576
_Y = 14.3394


def _m():
    import manifold3d

    return manifold3d


def _cylinder(r: float, h: float, fn: int, at: tuple[float, float, float] = (0.0, 0.0, 0.0)):
    return _m().Manifold.cylinder(h, r, r, fn).translate(at)


def _hull(*parts):
    return _m().Manifold.batch_hull(list(parts))


def _revolve(points, fn: int):
    return _m().CrossSection([points]).revolve(fn)


def _union(parts):
    return _m().Manifold.batch_boolean(list(parts), _m().OpType.Add)


def _save(solid, path: Path) -> Path:
    """*solid* written to *path*, each separate piece's triangles in one run."""
    import trimesh

    pieces = []
    for piece in solid.decompose():
        mesh = piece.to_mesh()
        pieces.append(trimesh.Trimesh(np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts), process=False))
    trimesh.util.concatenate(pieces).export(path)
    return path


def runs(path: Path) -> list[tuple[int, int]]:
    """Each separate piece's run of triangles in the file at *path*, as
    ``(first, count)``: the spans a motion note names a part by."""
    from kiln.mesh_bodies import read_triangles
    from kiln.printability import _parse_mesh_file

    triangles, _vertices = _parse_mesh_file(Path(path), [])
    piece = np.asarray(read_triangles(triangles).face_piece).copy()
    # A sliver the weld collapsed belongs to no piece: it rides with the run
    # it sits in.
    for i in np.flatnonzero(piece < 0):
        piece[i] = piece[i - 1] if i else piece[piece >= 0][0]
    starts = np.flatnonzero(np.r_[True, piece[1:] != piece[:-1]])
    ends = np.r_[starts[1:], len(piece)]
    return [(int(a), int(b - a)) for a, b in zip(starts, ends, strict=True)]


def _tongs_pin(fn: int):
    return _revolve(_PIN, fn)


def _tongs_bore(gap: float, fn: int):
    cs = _m().CrossSection
    profile = [(0.0, -1.0), (3.6 - _EF, -1.0), *_PIN[1:]]
    grown = cs([profile]).offset(gap, _m().JoinType.Miter, 100.0) ^ cs.square((20.0, 7.0)).translate((0.0, -1.0))
    foot = cs([[(0.0, -1.0), (3.6 + gap + _EF, -1.0), (3.6 + gap + _EF, 0.0), (3.6 + gap, _EF), (0.0, _EF)]])
    return (grown + foot).revolve(fn)


def tongs(
    path: Path, *, gaps: dict[tuple[float, float], float] | None = None, ring_wall: float = 2.5, fn: int = 64,
) -> Path:
    """The lazy tongs at *path*: every joint 0.3 mm across every face, but
    those *gaps* names by the joint's place (x, y), mm.  *ring_wall* is the
    wall of the lower handle's finger ring, mm."""
    gaps = gaps or {}
    ends = {0.0, 4 * _X}

    def joint_gap(x: float, y: float) -> float:
        return next((g for (gx, gy), g in gaps.items() if abs(gx - x) < 1e-3 and abs(gy - y) < 1e-3), 0.3)

    parts = []
    for k in range(4):
        x0, x1 = k * _X, (k + 1) * _X
        # A lower bar runs from (x0, -Y) to (x1, Y) on the bed.
        eyes = [(x0 + _X / 2, 0.0)] + [(x, y) for x, y in ((x0, -_Y), (x1, _Y)) if x not in ends]
        bar = [_hull(_cylinder(5, _H, fn, (x0, -_Y, 0)), _cylinder(5, _H, fn, (x1, _Y, 0)))]
        bar += [_cylinder(_RO, _H, fn, (x, y, 0)) for x, y in eyes]
        if k == 0:
            bar.append(_hull(_cylinder(5, _H, fn, (0, -_Y, 0)), _cylinder(5, _H, fn, (-18.0213, -26.9581, 0))))
            ring = _cylinder(9 - ring_wall, _H + 2, fn, (-21.2980, -29.2524, -1))
            bar.append(_cylinder(9, _H, fn, (-21.2980, -29.2524, 0)) - ring)
        solid = _union(bar)
        for x, y in eyes:
            solid -= _tongs_bore(joint_gap(x, y), fn).translate((x, y, 0))
        parts.append(solid)
        # An upper bar runs from (x0, Y) to (x1, -Y), above.
        eyes = [(x0 + _X / 2, 0.0)] + [(x, y) for x, y in ((x0, _Y), (x1, -_Y)) if x not in ends]
        h = _ZT - _ZB
        bar = [_hull(_cylinder(5, h, fn, (x0, _Y, _ZB)), _cylinder(5, h, fn, (x1, -_Y, _ZB)))]
        bar += [_cylinder(_RO, h, fn, (x, y, _ZB)) for x, y in eyes]
        if k == 0:
            bar.append(_hull(_cylinder(5, h, fn, (0, _Y, _ZB)), _cylinder(5, h, fn, (-18.0213, 26.9581, _ZB))))
            bar.append(_cylinder(9, _ZT, fn, (-21.2980, 29.2524, 0)) - _cylinder(6.5, _ZT + 2, fn, (-21.2980, 29.2524, -1)))
        parts.append(_union(bar))
    joints = [(k * _X + _X / 2, 0.0) for k in range(4)] + [(k * _X, s * _Y) for k in (1, 2, 3) for s in (1, -1)]
    parts += [_tongs_pin(fn).translate((x, y, 0)) for x, y in joints]
    return _save(_union(parts), path)


#: The joint the tight tongs close up: the middle of the third pair of bars.
TONGS_MIDDLE_JOINT = (2 * _X + _X / 2, 0.0)
#: Where the first pair of bars crosses the second.
TONGS_FIRST_CROSSING = (_X, _Y)


def claw(path: Path, *, gap: float = 0.3, rod_gap: float = 0.3, fn: int = 72) -> Path:
    """The robot claw at *path*: each pin *gap* from its bore across every
    face, and the rod *rod_gap* from its tunnel each side."""
    m = _m()
    cs = m.CrossSection
    px, qx, sy = 14.0, 38.0, -26.0

    def pin():
        return _revolve(_PIN, fn)

    def bore():
        g = gap
        return _revolve([(0, -1), (3.6 + g + _EF, -1), (3.6 + g + _EF, 0), (3.6 + g, _EF), (2.4 + g, 2.5),
                         (3.6 + g, 5), (4.6 + g, 6), (0, 6)], fn)

    def along_y(section, length: float):
        """*section* extruded *length* mm from y=0 toward -y, its own y up."""
        return section.extrude(length).rotate((90.0, 0.0, 0.0))

    rod = cs([[(-4 + _EF, 0), (4 - _EF, 0), (4, _EF), (4, 3.5), (0, 7.5), (-4, 3.5), (-4, _EF)]])
    gs = rod_gap
    tunnel = cs([[(-4 - gs, -1), (4 + gs, -1), (4 + gs, 3.5 + gs * 0.4142), (0, 7.5 + gs * 1.4142),
                  (-4 - gs, 3.5 + gs * 0.4142)]])
    tunnel += cs([[(-4 - gs - _EF, -1), (4 + gs + _EF, -1), (4 + gs + _EF, 0), (4 + gs, _EF), (-4 - gs, _EF),
                   (-4 - gs - _EF, 0)]])

    handle = _union([
        _hull(_cylinder(_RO, _H, fn, (-px, 0, 0)), _cylinder(_RO, _H, fn, (px, 0, 0))),
        along_y(rod, 82.0).translate((0, 0.01, 0)),
        _hull(_cylinder(11, 8, fn, (0, -82, 0)), _cylinder(11, 8, fn, (0, -106, 0))),
    ])
    for s in (-1, 1):
        handle -= bore().translate((s * px, 0, 0))
    handle -= _cylinder(4.5, 12, fn, (0, -104, -1))

    def slider_half():
        return _union([
            m.Manifold.cube((34.01, 10, 8)).translate((11 - 0.01, -67, 0)),
            m.Manifold.cube((14, 29, _ZT)).translate((31, -67, 0)),
            _hull(m.Manifold.cube((14, 2, _ZT - _ZB)).translate((31, -40, _ZB)),
                  _cylinder(5.5, _ZT - _ZB, fn, (qx, sy, _ZB))),
            pin().translate((qx, sy, 0)),
        ])

    body = _union([m.Manifold.cube((22, 22, 12)).translate((-11, -67, 0))]
                  + [m.Manifold.cube((22, 2, 1.5)).translate((-11, y, 12 - 0.01)) for y in (-63, -58, -53)])
    body -= along_y(tunnel, 30.0).translate((0, -40, 0))
    half = slider_half()
    slider = _union([body, half, half.mirror((1, 0, 0))])

    link = _hull(_cylinder(_RO, _H, fn, (qx, sy, 0)), _cylinder(_RO, _H, fn, (qx, 0, 0)))
    link = link - bore().translate((qx, sy, 0)) - bore().translate((qx, 0, 0))

    joints = [(16, 13.2), (23, 26), (25.5, 44), (25.5, 64)]
    finger = _union([
        pin().translate((px, 0, 0)),
        pin().translate((qx, 0, 0)),
        _hull(_cylinder(5.5, _ZT - _ZB, fn, (px, 0, _ZB)), _cylinder(5.5, _ZT - _ZB, fn, (qx, 0, _ZB))),
        _hull(_cylinder(5.5, _ZT - _ZB, fn, (px, 0, _ZB)), _cylinder(4.5, _ZT - _ZB, fn, (*joints[0], _ZB))),
        *(_hull(_cylinder(4.5, _ZT, fn, (*joints[i], 0)), _cylinder(4.5, _ZT, fn, (*joints[i + 1], 0)))
          for i in range(3)),
    ])
    return _save(_union([handle, slider, link, link.mirror((1, 0, 0)), finger, finger.mirror((1, 0, 0))]), path)
