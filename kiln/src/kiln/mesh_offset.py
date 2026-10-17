"""Thicken a mesh with no CAD file: a true outward offset, round holes kept at size.

The old mesh route pushed each vertex along its averaged normal.  On a corner
that moves a face by the amount over the square root of three, on a thin wall
it pushes the two sides into each other, and on a CAD enclosure it left a
0.096 mm wall and none of eight holes (measured 2026-10-01).  A true offset is
the Minkowski sum of the part with a ball: every point of the surface moves out
by the ball's radius, faces stay flat, convex edges round over, and nothing can
cross.  Measured on the same enclosure as a mesh: 0.33 GB at peak, 6.5 s on a
quiet machine and 18 s on a loaded one; the thinnest wall went from 1.2 to
1.76 mm with all eight holes kept, and the surface stayed closed.

* **The ball** has 8 segments.  Its vertices sit on the sphere, so a face lying
  between them moves up to 0.07 mm less than asked (walls aligned to an axis
  are exact).  16 segments halve that and cost five times the memory -- 1.6 GB
  on the enclosure, past what the hosted server can spare.
* **The sum is built in batches** of per-triangle hulls, each forced to
  evaluate: manifold3d is lazy, and one unforced union holds the whole tree.
* **Round holes are cut back** to their own diameter, 0.005 mm over (a cut at
  exactly the old size leaves slivers at the rim that hide the hole from Kiln's
  own detector).  Each end is judged on the original mesh: a floor stops the
  cut where the offset moved it; an open end carries it out past the moved
  surface.  Only holes on the x, y or z axis are found by the detector, so a
  tilted hole shrinks -- and the measured check that follows refuses the
  result rather than call that kept.
* **The result is welded at float32** before it is written: the union leaves
  sub-micron edges that every STL reader would otherwise see as non-manifold.

Runs in a child interpreter (:mod:`kiln.child_interpreter`) for the same
reasons as :mod:`kiln.cad_offset`; this file is the child's script too, so the
part below the line imports nothing of Kiln.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

#: Segments of the offset ball -- see the module docstring for the trade.
_BALL_SEGMENTS = 8
#: Per-triangle hulls unioned before each forced evaluation.
_BATCH = 1000
#: Above this many triangles the mesh is simplified first, by 0.01 mm: the sum
#: grows with the triangle count, and 0.01 mm is below what a nozzle prints.
_SIMPLIFY_ABOVE = 30_000
_SIMPLIFY_TOLERANCE_MM = 0.01
#: Past this many triangles (after simplifying) a mesh offset is refused: the
#: measured cost was 26 s and 0.7 GB at 82k triangles.
_MAX_TRIANGLES = 120_000
#: A re-cut hole is cut this much over its old radius, mm.
_RECUT_OVERSIZE_MM = 0.005
_CHILD_TIMEOUT_S = 180


class MeshOffsetError(RuntimeError):
    """The mesh could not be offset; the message says why."""


def offset_mesh(
    mesh_path: str,
    output_path: str,
    *,
    amount_mm: float,
    keep_hole_size: bool = True,
) -> dict[str, Any]:
    """Offset *mesh_path* outward by *amount_mm*; write *output_path*.

    Raises :class:`MeshOffsetError` when the mesh is not a closed solid, is too
    large, or the child fails.
    """
    from kiln.child_interpreter import run_in_child

    holes: list[dict[str, Any]] = []
    if keep_hole_size:
        from kiln.generation.validation import detect_holes

        holes = [h for h in detect_holes(mesh_path) if h.get("axis") in ("x", "y", "z")]
    request = {
        "mesh": os.path.abspath(mesh_path),
        "out": os.path.abspath(output_path),
        "amount": float(amount_mm),
        "holes": holes,
    }
    return run_in_child(__file__, request, timeout_s=_CHILD_TIMEOUT_S, what="the mesh offset", error=MeshOffsetError)


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------

_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


def _child(request: dict[str, Any]) -> dict[str, Any]:
    import manifold3d as mf
    import numpy as np
    import trimesh

    from kiln.mesh_frame import load_mesh

    # Offset the part as Kiln stands it: a glTF read raw would come back
    # thickened and lying on its side.
    original = load_mesh(request["mesh"], force="mesh")
    solid = mf.Manifold(mf.Mesh64(
        vert_properties=np.asarray(original.vertices, dtype=np.float64),
        tri_verts=np.asarray(original.faces, dtype=np.uint64),
    ))
    if solid.status() != mf.Error.NoError:
        return {"refused": "the mesh is not a closed solid, so it has no inside to grow (repair it first)"}
    simplified = solid.num_tri() > _SIMPLIFY_ABOVE
    if simplified:
        solid = solid.simplify(_SIMPLIFY_TOLERANCE_MM)
    if solid.num_tri() > _MAX_TRIANGLES:
        return {"refused": f"the mesh has {solid.num_tri()} triangles; a mesh offset stops at {_MAX_TRIANGLES}"}

    amount = request["amount"]
    ball = np.asarray(mf.Manifold.sphere(amount, _BALL_SEGMENTS).to_mesh64().vert_properties)[:, :3]
    flat = solid.to_mesh64()
    triangles = np.asarray(flat.vert_properties)[:, :3][np.asarray(flat.tri_verts)]
    grown = solid
    for start in range(0, len(triangles), _BATCH):
        hulls = [
            mf.Manifold.hull_points((tri[:, None, :] + ball[None]).reshape(-1, 3))
            for tri in triangles[start:start + _BATCH]
        ]
        grown = mf.Manifold.batch_boolean([grown, *hulls], mf.OpType.Add)
        grown.num_tri()  # force evaluation: manifold3d is lazy

    cutters = [_cutter(mf, np, original, hole, amount) for hole in request["holes"]]
    if cutters:
        grown = grown - mf.Manifold.batch_boolean(cutters, mf.OpType.Add)

    z_min = grown.bounding_box()[2]
    grown = grown.translate([0.0, 0.0, float(original.bounds[0][2] - z_min)])
    welded = mf.Manifold(grown.to_mesh())  # float32, as the STL will be
    if welded.status() != mf.Error.NoError:
        return {"refused": f"the offset could not be written as a closed solid ({welded.status()})"}
    mesh = welded.to_mesh()
    trimesh.Trimesh(
        vertices=np.asarray(mesh.vert_properties)[:, :3].astype(np.float64),
        faces=np.asarray(mesh.tri_verts),
        process=False,
    ).export(request["out"])
    return {
        "holes_recut_mm": sorted(round(float(h["diameter_mm"]), 3) for h in request["holes"]),
        "simplified": simplified,
        "ball_segments": _BALL_SEGMENTS,
    }


def _inside(np: Any, mesh: Any, points: Any) -> bool:
    """True when any point lies inside *mesh*, by its winding number."""
    tri = mesh.triangles
    for p in np.atleast_2d(points):
        a, b, c = tri[:, 0] - p, tri[:, 1] - p, tri[:, 2] - p
        la, lb, lc = (np.linalg.norm(x, axis=1) for x in (a, b, c))
        num = np.einsum("ij,ij->i", a, np.cross(b, c))
        den = la * lb * lc + np.einsum("ij,ij->i", a, b) * lc + np.einsum("ij,ij->i", b, c) * la + np.einsum("ij,ij->i", c, a) * lb
        if np.sum(np.arctan2(num, den)) / (2 * np.pi) >= 0.5:
            return True
    return False


def _ring(np: Any, centre: Any, axis: Any, radius: float, n: int = 12) -> Any:
    u = np.cross(axis, [1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.cross(axis, [0.0, 1.0, 0.0])
    u = u / np.linalg.norm(u)
    w = np.cross(axis, u)
    angles = 2 * np.pi * np.arange(n) / n
    return centre[None] + radius * (np.cos(angles)[:, None] * u[None] + np.sin(angles)[:, None] * w[None])


def _cutter(mf: Any, np: Any, original: Any, hole: dict[str, Any], amount: float) -> Any:
    """A cylinder that restores *hole* to its diameter, judged end by end on *original*."""
    axis = np.array(_AXES[hole["axis"]])
    p = hole["position"]
    centre = np.array([p["x_mm"], p["y_mm"], p["z_mm"]])
    radius = hole["diameter_mm"] / 2.0
    half = hole["depth_mm"] / 2.0
    ends = []
    for sign in (-1.0, 1.0):
        end = centre + sign * half * axis
        probe = end + sign * 0.2 * axis
        if _inside(np, original, np.vstack([_ring(np, probe, axis, 0.9 * radius, 8), probe[None]])):
            ends.append(end - sign * (amount - 0.01) * axis)  # a floor: it moved in by the amount
        else:
            t = 0.01
            while t < 30.0 and _inside(np, original, _ring(np, end + sign * t * axis, axis, radius + amount)):
                t += 0.05
            ends.append(end + sign * (t + amount + 0.1) * axis)
    start, stop = ends
    length = float(np.linalg.norm(stop - start))
    segments = 64
    r = (radius + _RECUT_OVERSIZE_MM) / np.cos(np.pi / segments)  # the polygon's inner circle at r + oversize
    cylinder = mf.Manifold.cylinder(length, r, r, segments)
    direction = (stop - start) / length
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(z, direction)
    s, c = np.linalg.norm(v), float(np.dot(z, direction))
    if s < 1e-12:
        rotation = np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
        rotation = np.eye(3) + vx + vx @ vx * ((1 - c) / s**2)
    return cylinder.transform(np.hstack([rotation, start.reshape(3, 1)]).tolist())


if __name__ == "__main__":
    import logging

    logging.disable(logging.INFO)
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.dumps(_child(json.load(fh))))
