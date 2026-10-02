"""Thicken a part from its CAD file: every surface moved out, holes kept at size.

A mesh does not know which of its triangles are a wall and which are a screw
hole; a STEP file does.  So when the part's CAD file is at hand, walls are
thickened there -- the OpenCascade kernel moves every face of the solid out by
the asked amount, exactly -- and the result is tessellated once.  Measured
2026-10-01 on an 80 x 55 x 28 mm enclosure (walls 1.2-2.0 mm, eight screw
holes) at 0.4 mm: the part grew exactly 0.8 mm on every axis and every hole
kept its size; the thinnest wall went from 1.2 to 1.8 mm, because a wall
beside a held hole grows on its outer side only (2.0 mm when the holes may
close in).  The same request through the old mesh path had left a 0.096 mm
wall and no holes.

Moving every face also shrinks every hole by twice the amount (2.2 -> 1.4 mm
on that part), which breaks the screw that was meant to go in it.  So a hole --
a concave cylinder whose faces close a full circle around one axis -- keeps its
size:

* a hole whose rims are sharp edges is held exactly: its faces are given an
  offset of zero, and the kernel moves everything else around it;
* a hole with a rounded rim cannot be held that way -- the kernel carries one
  offset along every chain of tangent faces, so holding the bore would hold
  the rounding and the face behind it -- so it moves with the rest and is cut
  back to its own diameter afterwards.  Each end is probed on the original
  solid: a closed end (a blind floor) stops the cut where that floor moved to;
  an open end carries it out past the moved surface.

Slots, vents and cutouts that are not round are moved like any other face, and
the reply says so with the outside size: thickening a part changes its fit
somewhere, and the honest answer names where.

The kernel runs in a child interpreter (:mod:`kiln.child_interpreter`), as
Kiln's STEP conversion does: a C++ call cannot be timed out from Python, and a
pathological solid must not take the server's memory with it.  This file is
that child's script too, so the part below the line imports nothing of Kiln.
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

#: The kernel's tolerance for the offset itself, mm.
_OFFSET_TOLERANCE_MM = 1e-4
#: Tessellation of the result: the same bounds Kiln converts a STEP file with,
#: so a thickened part is as smooth as the part it came from.  Read from
#: step_import by the parent and handed to the child.
_CHILD_TIMEOUT_S = 180
#: An outward offset grows the box by exactly twice the amount on every axis;
#: more than this off means some outer face did not move.
_GROWTH_TOLERANCE_MM = 1e-3
#: How far past a hole's end the probe looks to tell open from closed, mm.
_END_PROBE_MM = 0.2
#: Extra length an open-ended re-cut carries past the moved surface, mm.
_RECUT_MARGIN_MM = 0.1
#: A re-cut bore is this much over its old radius, mm.
_RECUT_OVERSIZE_MM = 0.005


class CadOffsetError(RuntimeError):
    """The kernel could not thicken this solid; the message says why."""


def offset_step(
    step_path: str,
    output_stl: str,
    *,
    amount_mm: float,
    keep_hole_size: bool = True,
) -> dict[str, Any]:
    """Thicken the solid in *step_path* by *amount_mm* per surface; write *output_stl*.

    Returns the kernel's own account: holes held and re-cut (diameters, mm),
    the growth of each axis, whether the result is a valid solid.  Raises
    :class:`CadOffsetError` when the kernel fails, times out, or produces a
    solid whose outside did not move as asked.
    """
    from kiln.child_interpreter import run_in_child
    from kiln.step_import import _OCP_ANGULAR_DEFLECTION, _OCP_LINEAR_DEFLECTION

    request = {
        "step": os.path.abspath(step_path),
        "out": os.path.abspath(output_stl),
        "amount": float(amount_mm),
        "keep_holes": bool(keep_hole_size),
        "linear": _OCP_LINEAR_DEFLECTION,
        "angular": _OCP_ANGULAR_DEFLECTION,
    }
    return run_in_child(__file__, request, timeout_s=_CHILD_TIMEOUT_S, what="the CAD kernel", error=CadOffsetError)


# ---------------------------------------------------------------------------
# The child: everything below runs in its own interpreter, OpenCascade only.
# ---------------------------------------------------------------------------


def _child(request: dict[str, Any]) -> dict[str, Any]:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepOffset import BRepOffset_MakeOffset, BRepOffset_Skin
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.GeomAbs import GeomAbs_Intersection
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    reader = STEPControl_Reader()
    if reader.ReadFile(request["step"]) != IFSelect_RetDone:
        return {"refused": "the STEP file could not be read"}
    reader.TransferRoots()
    shape = reader.OneShape()
    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
    if solids.Extent() != 1:
        return {"refused": f"the file holds {solids.Extent()} solids; Kiln thickens one part at a time"}
    solid = TopoDS.Solid_s(solids.FindKey(1))

    amount = request["amount"]
    holes = _find_holes(solid) if request["keep_holes"] else []
    edges_to_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, edges_to_faces)
    held = [h for h in holes if _rims_are_sharp(h, edges_to_faces)]
    recut = [h for h in holes if h not in held]

    maker = BRepOffset_MakeOffset()
    maker.Initialize(solid, amount, _OFFSET_TOLERANCE_MM, BRepOffset_Skin, False, False, GeomAbs_Intersection, False, False)
    for hole in held:
        for face in hole["faces"]:
            maker.SetOffsetOnFace(face, 0.0)
    maker.MakeOffsetShape()
    if not maker.IsDone():
        return {"refused": f"the CAD kernel could not offset this solid (error {maker.Error()})"}
    result = maker.Shape()

    for hole in recut:
        start, end = _recut_span(solid, hole, amount)
        axis_point = gp_Pnt(*hole["origin"]).Translated(gp_Vec(*hole["axis"]).Multiplied(start))
        # A hair over the old radius: cut at exactly it, the rim keeps slivers
        # that hide the hole from Kiln's own hole detector.
        bore = BRepPrimAPI_MakeCylinder(
            gp_Ax2(axis_point, gp_Dir(*hole["axis"])), hole["radius"] + _RECUT_OVERSIZE_MM, end - start,
        ).Shape()
        cut = BRepAlgoAPI_Cut(result, bore)
        cut.Build()
        result = cut.Shape()

    before, after = _box(solid), _box(result)
    growth = [round((after[i + 3] - after[i]) - (before[i + 3] - before[i]), 4) for i in range(3)]
    if any(abs(g - 2 * amount) > _GROWTH_TOLERANCE_MM for g in growth):
        return {"refused": f"the outside grew {growth} mm instead of {2 * amount:g} on every axis: a face did not move"}

    # Seated where the part was: the offset moved its underside down by the
    # amount, and a part below the plate is a part the bed-fit gate moves.
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf

    lift = gp_Trsf()
    lift.SetTranslation(gp_Vec(0.0, 0.0, before[2] - after[2]))
    result = BRepBuilderAPI_Transform(result, lift, True).Shape()

    BRepMesh_IncrementalMesh(result, request["linear"], False, request["angular"], True)
    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    if not writer.Write(result, request["out"]):
        return {"refused": "the thickened part could not be written"}
    return {
        "holes_held_mm": sorted(round(2 * h["radius"], 3) for h in held),
        "holes_recut_mm": sorted(round(2 * h["radius"], 3) for h in recut),
        "growth_mm": growth,
        "valid_solid": bool(BRepCheck_Analyzer(result).IsValid()),
    }


def _box(shape: Any) -> tuple[float, ...]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    # The two corners, not Get(): kernel 8.0 returns Get() as a struct its
    # bindings cannot hand to Python (the same fix step_import carries).
    lo, hi = box.CornerMin(), box.CornerMax()
    return lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()


def _faces(shape: Any) -> list[Any]:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, found)
    return [TopoDS.Face_s(found.FindKey(i)) for i in range(1, found.Extent() + 1)]


def _outward_normal(face: Any, u: float, v: float) -> tuple[Any, Any]:
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec

    point, normal = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(u, v, point, normal)  # the face's orientation is applied
    if normal.Magnitude() > 0:
        normal.Normalize()
    return point, normal


def _find_holes(solid: Any) -> list[dict[str, Any]]:
    """Concave cylinders whose faces close a full circle around one axis line.

    Concave: the outward normal points at the axis, as a bore's does and a
    boss's does not.  A partial concave arc -- an inner-corner rounding, the
    end of a slot -- is not a hole: holding it while its tangent neighbours
    move would tear the surface.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.gp import gp_Vec

    groups: dict[tuple, list[tuple[Any, dict[str, Any]]]] = {}
    for face in _faces(solid):
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Cylinder:
            continue
        cylinder = surface.Cylinder()
        origin, axis = cylinder.Axis().Location(), cylinder.Axis().Direction()
        u0, u1 = surface.FirstUParameter(), surface.LastUParameter()
        v0, v1 = surface.FirstVParameter(), surface.LastVParameter()
        point, normal = _outward_normal(face, 0.5 * (u0 + u1), 0.5 * (v0 + v1))
        to_point = gp_Vec(origin, point)
        along = gp_Vec(axis)
        radial = to_point - along.Multiplied(to_point.Dot(along))
        if radial.Magnitude() == 0 or normal.Dot(radial) >= 0:
            continue  # convex: a boss, not a bore
        d = (axis.X(), axis.Y(), axis.Z())
        if d < (0.0, 0.0, 0.0):
            d = (-d[0], -d[1], -d[2])
        o = gp_Vec(origin.X(), origin.Y(), origin.Z())
        dv = gp_Vec(*d)
        foot = o - dv.Multiplied(o.Dot(dv))
        key = (*(round(c, 4) for c in d), round(foot.X(), 3), round(foot.Y(), 3), round(foot.Z(), 3),
               round(cylinder.Radius(), 4))
        groups.setdefault(key, []).append((face, {
            "radius": cylinder.Radius(),
            "origin": (origin.X(), origin.Y(), origin.Z()),
            "axis": d,
            "span": math.degrees(u1 - u0),
        }))
    return [
        {"faces": [f for f, _ in members], **members[0][1]}
        for members in groups.values()
        if sum(info["span"] for _, info in members) >= 359.0
    ]


def _rims_are_sharp(hole: dict[str, Any], edges_to_faces: Any, tangent_deg: float = 2.0) -> bool:
    """True when every edge of the hole meets its neighbour at an angle (no rounding)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def normal_at(face: Any, point: Any) -> Any:
        u, v = GeomAPI_ProjectPointOnSurf(point, BRep_Tool.Surface_s(face)).LowerDistanceParameters()
        return _outward_normal(face, u, v)[1]

    for face in hole["faces"]:
        edges = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(face, TopAbs_EDGE, edges)
        for i in range(1, edges.Extent() + 1):
            edge = TopoDS.Edge_s(edges.FindKey(i))
            if BRep_Tool.Degenerated_s(edge):
                continue
            others = [
                TopoDS.Face_s(f) for f in edges_to_faces.FindFromKey(edge)
                if not any(f.IsSame(h) for h in hole["faces"])
            ]
            curve = BRepAdaptor_Curve(edge)
            for t in (0.25, 0.5, 0.75):
                point = curve.Value(curve.FirstParameter() + t * (curve.LastParameter() - curve.FirstParameter()))
                mine = normal_at(face, point)
                for other in others:
                    cosine = max(-1.0, min(1.0, mine.Dot(normal_at(other, point))))
                    if math.degrees(math.acos(cosine)) < tangent_deg:
                        return False
    return True


def _recut_span(solid: Any, hole: dict[str, Any], amount: float) -> tuple[float, float]:
    """Where along its axis a moved hole is cut back to size: (start, end)."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Dir, gp_Pnt, gp_Vec
    from OCP.TopAbs import TopAbs_IN

    origin, axis = gp_Pnt(*hole["origin"]), gp_Vec(*hole["axis"])
    along = []
    for face in hole["faces"]:
        x0, y0, z0, x1, y1, z1 = _box(face)
        along += [gp_Vec(origin, gp_Pnt(x, y, z)).Dot(axis) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    t0, t1 = min(along), max(along)
    radius = hole["radius"]
    reference = gp_Dir(1, 0, 0) if abs(axis.X()) < 0.9 else gp_Dir(0, 1, 0)
    u = axis.Crossed(gp_Vec(reference)).Normalized()
    w = axis.Crossed(u)

    def inside(t: float, r: float) -> bool:
        centre = origin.Translated(axis.Multiplied(t))
        ring = [centre] + [
            centre.Translated(u.Multiplied(r * math.cos(a)) + w.Multiplied(r * math.sin(a)))
            for a in (2 * math.pi * k / 12 for k in range(12))
        ]
        return any(BRepClass3d_SolidClassifier(solid, p, 1e-6).State() == TopAbs_IN for p in ring)

    ends = []
    for t, sign in ((t0, -1.0), (t1, 1.0)):
        if inside(t + sign * _END_PROBE_MM, 0.9 * radius):
            ends.append(t - sign * (amount - 0.01))  # a floor: it moved `amount` into the hole
        else:
            k = 0.01
            while k < 30.0 and inside(t + sign * k, radius + amount):
                k += 0.05
            ends.append(t + sign * (k + amount + _RECUT_MARGIN_MM))
    return ends[0], ends[1]


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.dumps(_child(json.load(fh))))
