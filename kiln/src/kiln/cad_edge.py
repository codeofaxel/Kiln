"""Round or bevel a part's edges on its CAD file: the kernel rebuilds the faces, exactly.

A mesh does not know where its edges are; a STEP file does.  The kernel
replaces each chosen edge with a rolled surface (a fillet) or a flat one (a
chamfer) and re-trims the faces beside it, so the result is a closed solid
whose volume is the one geometry gives: a 30 x 20 x 10 mm block with all
twelve edges rounded at 2 mm comes out at 5,804.6961 mm3, the closed form to
the fourth decimal.

Two calls, both in a child interpreter (:mod:`kiln.child_interpreter`): a C++
call cannot be timed out from Python, and a pathological solid must not take
the server's memory with it.

* :func:`survey_step` reads what every edge IS -- which two faces meet there
  and at what angle, whether the corner is an outside or an inside one, how
  much room each face has beside it, whether it rims a hole or the foot of a
  post, and which edges the kernel will round together because they run on
  smoothly into each other.  Facts only; what to do with them is decided by
  :mod:`kiln.edge_plan`.
* :func:`finish_step` applies a plan.  Rounds go first and bevels second, so
  a bevel follows a rounded corner instead of mitring across it.  When the
  kernel cannot build the whole plan, the edges it fails on are dropped and
  named in the answer; the rest are still done.

This file is the child's script too.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any

_CHILD_TIMEOUT_S = 180
#: The share of the timeout the child may spend finding which edges to drop
#: before it stops looking and reports the rest as not tried.
_SEARCH_SHARE = 0.6
#: Faces that meet at less than this are one smooth surface, not an edge, degrees.
_SMOOTH_DEG = 1.0
#: How far off an edge the corner probe steps to tell an outside corner from
#: an inside one, mm.  Far below any printable feature, far above the kernel's
#: own tolerance.
_CORNER_PROBE_MM = 1e-3
#: Rounding or bevelling an edge never makes the part's box bigger; more
#: growth than this means a face went somewhere it should not, mm.
_BOX_TOLERANCE_MM = 1e-4
#: A survey and the plan made from it are two reads of one file; an edge whose
#: midpoint moved more than this between them is not the edge that was planned.
_SAME_EDGE_MM = 1e-5


class CadEdgeError(RuntimeError):
    """The kernel could not read or finish this solid; the message says why."""


def survey_step(step_path: str) -> dict[str, Any]:
    """Every edge of the solid in *step_path*, as facts.

    Returns ``box`` (the part's limits), ``volume_mm3``, ``holes_mm`` and
    ``edges``: one dict per real edge with its ``id``, the ``chain`` the
    kernel rounds it with, ``curve`` (``line``, ``circle`` or ``curve``),
    ``length_mm``, ``mid``, ``tangent``, ``z`` (lowest and highest point),
    ``turn_deg`` (how far the surface turns across it), ``corner``
    (``outside``, ``inside`` or ``smooth``), the two ``faces`` with the
    outward ``normals`` at three points along the edge, the corners it
    ``ends`` at (edges that share one meet there), the ``room_mm`` each
    face has beside it and the edge ``across`` that room, ``hole_mm`` with
    ``hole_edge`` (``mouth`` or ``floor``) when it bounds a round hole, and
    ``post_mm`` when it bounds a round post.
    Raises :class:`CadEdgeError`.
    """
    return _run({"do": "survey", "step": os.path.abspath(step_path)})


def finish_step(
    step_path: str,
    output_stl: str,
    *,
    treatments: list[dict[str, Any]],
    output_step: str | None = None,
) -> dict[str, Any]:
    """Round and bevel the edges *treatments* names; write *output_stl* (and *output_step*).

    Each treatment is ``{"edges": [ids], "mids": [[x, y, z], ...], "kind":
    "fillet" | "chamfer", "size_mm": r}``: one chain of edges and the size it
    gets.  Returns ``applied`` and ``dropped`` (each dropped chain with its
    ``reason``), the volume, box and round holes before and after, and
    ``valid_solid``.
    Raises :class:`CadEdgeError` when the kernel fails outright, times out,
    or could finish none of the edges.
    """
    from kiln.step_import import _OCP_ANGULAR_DEFLECTION, _OCP_LINEAR_DEFLECTION

    return _run({
        "do": "finish",
        "step": os.path.abspath(step_path),
        "out": os.path.abspath(output_stl),
        "out_step": os.path.abspath(output_step) if output_step else None,
        "treatments": treatments,
        "linear": _OCP_LINEAR_DEFLECTION,
        "angular": _OCP_ANGULAR_DEFLECTION,
        "search_s": _SEARCH_SHARE * _CHILD_TIMEOUT_S,
    })


def _run(request: dict[str, Any]) -> dict[str, Any]:
    from kiln.child_interpreter import run_in_child

    return run_in_child(__file__, request, timeout_s=_CHILD_TIMEOUT_S, what="the CAD kernel", error=CadEdgeError)


# ---------------------------------------------------------------------------
# The child: everything below runs in its own interpreter.
# ---------------------------------------------------------------------------


def _child(request: dict[str, Any]) -> dict[str, Any]:
    from kiln import cad_kernel as k

    solid, why_not = k.read_one_solid(request["step"], verb="finishes the edges of")
    if solid is None:
        return {"refused": why_not}
    if request["do"] == "survey":
        return _survey(solid)
    return _finish(solid, request)


def _xyz(v: Any) -> list[float]:
    return [round(v.X(), 6), round(v.Y(), 6), round(v.Z(), 6)]


def _edge_table(solid: Any) -> tuple[Any, Any, dict[int, list[Any]]]:
    """The solid's edges and faces by index, and each real edge's two faces.

    An edge is real when exactly two different faces meet along it: a seam
    (one face meeting itself around a cylinder) and a degenerate edge (the
    tip of a cone) are not places a part can be rounded.
    """
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp

    from kiln import cad_kernel as k

    edges, faces = k.indexed(solid, "edge"), k.indexed(solid, "face")
    owners = k.ancestor_map()
    TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, owners)
    real: dict[int, list[Any]] = {}
    for i in range(1, edges.Extent() + 1):
        edge = k.as_edge(edges.FindKey(i))
        if BRep_Tool.Degenerated_s(edge):
            continue
        beside: list[Any] = []
        for face in owners.FindFromKey(edge):
            if not any(face.IsSame(seen) for seen in beside):
                beside.append(k.as_face(face))
        if len(beside) == 2:
            real[i] = beside
    return edges, faces, real


def _normal_on(face: Any, edge: Any, t: float, point: Any) -> Any:
    """*face*'s outward normal at the edge's parameter *t* (the point *point*)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve2d
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

    from kiln import cad_kernel as k

    try:
        uv = BRepAdaptor_Curve2d(edge, face).Value(t)
        u, v = uv.X(), uv.Y()
    except Exception:  # noqa: BLE001 -- no curve on this face: project the point instead
        u, v = GeomAPI_ProjectPointOnSurf(point, BRep_Tool.Surface_s(face)).LowerDistanceParameters()
    return k.outward_normal(face, u, v)[1]


def _corner(solid: Any, point: Any, n_a: Any, n_b: Any) -> str:
    """``outside`` or ``inside``: which side of the edge the material is on.

    Step off the edge along the difference of the two normals, both ways.
    On an outside corner both steps leave the part; on an inside corner both
    land in it.
    """
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN

    step = n_a - n_b
    if step.Magnitude() == 0:
        return "smooth"
    step.Normalize()
    inside = 0
    for sign in (1.0, -1.0):
        probe = point.Translated(step.Multiplied(sign * _CORNER_PROBE_MM))
        if BRepClass3d_SolidClassifier(solid, probe, 1e-7).State() == TopAbs_IN:
            inside += 1
    return "inside" if inside == 2 else "outside"


def _shares_a_vertex(a: Any, b: Any) -> bool:
    from kiln import cad_kernel as k

    mine = k.subshapes(a, "vertex")
    return any(v.IsSame(w) for v in k.subshapes(b, "vertex") for w in mine)


def _room(edge: Any, face: Any, edges: Any, closed: bool, length: float) -> tuple[float, int | None]:
    """How far *face* runs away from *edge* before its next boundary, and which edge that is.

    The nearest edge of the face that does not touch this one: the far side
    of a wall's top, the rim of a hole beside it.  A face whose only boundary
    is this edge (the flat end of a round post) has its own width as room,
    across to this same edge; a face every edge of which touches this one (a
    triangle) has its mean depth.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    from kiln import cad_kernel as k

    best, across = math.inf, None
    for other in k.subshapes(face, "edge"):
        if other.IsSame(edge) or BRep_Tool.Degenerated_s(other) or _shares_a_vertex(edge, other):
            continue
        gap = BRepExtrema_DistShapeShape(edge, other)
        if gap.IsDone() and gap.Value() < best:
            best, across = gap.Value(), edges.FindIndex(other)
    if across is not None:
        return best, across
    face_area = k.area(face)
    if closed:
        return 2.0 * math.sqrt(face_area / math.pi), edges.FindIndex(edge)
    return (2.0 * face_area / length if length > 0 else 0.0), None


def _chains(solid: Any, edges: Any, sharp: list[int]) -> dict[int, int]:
    """Edge id -> chain id, as the kernel itself groups them.

    The kernel rounds an edge together with every edge that runs on smoothly
    from it, so a size is given to a chain, never to one edge of it.  Asked
    here rather than re-derived: a second opinion on what "smoothly" means is
    a plan the kernel would not build.
    """
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet

    from kiln import cad_kernel as k

    maker = BRepFilletAPI_MakeFillet(solid)
    chain_of: dict[int, int] = {}
    for i in sharp:
        if i in chain_of:
            continue
        edge = k.as_edge(edges.FindKey(i))
        try:
            maker.Add(edge)
            contour = maker.Contour(edge)
        except Exception:  # noqa: BLE001 -- the kernel will not take this edge: a chain of one
            contour = 0
        if contour <= 0:
            chain_of[i] = i
            continue
        members = [edges.FindIndex(maker.Edge(contour, j)) for j in range(1, maker.NbEdges(contour) + 1)]
        lead = min([m for m in members if m > 0] + [i])
        for m in members:
            if m > 0:
                chain_of.setdefault(m, lead)
        chain_of.setdefault(i, lead)
    return chain_of


def _rims(solid: Any, edges: Any, *, concave: bool) -> tuple[dict[int, float], set[int]]:
    """Edges that rim a full round bore (or post): id -> diameter, and the edges inside one."""
    from OCP.BRep import BRep_Tool

    from kiln import cad_kernel as k

    rims: dict[int, float] = {}
    within: set[int] = set()
    for feature in k.full_cylinders(solid, concave=concave):
        counts: dict[int, int] = {}
        for face in feature["faces"]:
            for edge in k.subshapes(face, "edge"):
                if not BRep_Tool.Degenerated_s(edge):
                    counts[edges.FindIndex(edge)] = counts.get(edges.FindIndex(edge), 0) + 1
        for i, n in counts.items():
            if n == 1:
                rims[i] = round(2.0 * feature["radius"], 4)
            else:
                within.add(i)
    return rims, within


def _survey(solid: Any) -> dict[str, Any]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line
    from OCP.gp import gp_Pnt, gp_Vec

    from kiln import cad_kernel as k

    edges, faces, real = _edge_table(solid)
    corners = k.indexed(solid, "vertex")
    holes, in_hole = _rims(solid, edges, concave=True)
    posts, _in_post = _rims(solid, edges, concave=False)

    table: list[dict[str, Any]] = []
    for i, (face_a, face_b) in real.items():
        if i in in_hole:
            continue
        edge = k.as_edge(edges.FindKey(i))
        curve = BRepAdaptor_Curve(edge)
        t0, t1 = curve.FirstParameter(), curve.LastParameter()
        kind = {GeomAbs_Line: "line", GeomAbs_Circle: "circle"}.get(curve.GetType(), "curve")
        length = GCPnts_AbscissaPoint.Length_s(curve)
        normals, zs = [], []
        for share in (0.0, 0.25, 0.5, 0.75, 1.0):
            t = t0 + share * (t1 - t0)
            point = curve.Value(t)
            zs.append(point.Z())
            if share in (0.25, 0.5, 0.75):
                normals.append([_xyz(_normal_on(face_a, edge, t, point)), _xyz(_normal_on(face_b, edge, t, point))])
        t_mid = 0.5 * (t0 + t1)
        mid, along = gp_Pnt(), gp_Vec()
        curve.D1(t_mid, mid, along)
        if along.Magnitude() > 0:
            along.Normalize()
        n_a, n_b = gp_Vec(*normals[1][0]), gp_Vec(*normals[1][1])
        turn = math.degrees(math.acos(max(-1.0, min(1.0, n_a.Dot(n_b)))))
        corner = "smooth" if turn < _SMOOTH_DEG else _corner(solid, mid, n_a, n_b)
        closed = bool(curve.IsClosed())
        rooms = [_room(edge, face, edges, closed, length) for face in (face_a, face_b)]
        entry: dict[str, Any] = {
            "id": i,
            "curve": kind,
            "length_mm": round(length, 4),
            "mid": _xyz(mid),
            "tangent": _xyz(along),
            "z": [round(min(zs), 6), round(max(zs), 6)],
            "turn_deg": round(turn, 3),
            "corner": corner,
            "faces": [faces.FindIndex(face_a), faces.FindIndex(face_b)],
            "ends": sorted({corners.FindIndex(v) for v in k.subshapes(edge, "vertex")}),
            "normals": normals,
            "room_mm": [round(r, 4) for r, _ in rooms],
            "across": [a for _, a in rooms],
        }
        if i in holes:
            # Where the bore meets the outside is its mouth; where it meets
            # its own blind end is its floor.
            entry["hole_mm"] = holes[i]
            entry["hole_edge"] = "mouth" if corner == "outside" else "floor"
        if i in posts:
            entry["post_mm"] = posts[i]
        table.append(entry)

    chain_of = _chains(solid, edges, [e["id"] for e in table if e["corner"] != "smooth"])
    for entry in table:
        entry["chain"] = chain_of.get(entry["id"], entry["id"])
    return {
        "box": [round(v, 6) for v in k.box(solid)],
        "volume_mm3": round(k.volume(solid), 4),
        "holes_mm": sorted({d for d in holes.values()}),
        "edges": table,
    }


def _build(solid: Any, edges: Any, plan: list[dict[str, Any]]) -> tuple[Any, str]:
    """The solid with every treatment in *plan* applied, or ``(None, why not)``.

    Rounds first, bevels second, on the rounded shape: a bevel then follows
    the rounded corner along its chain.  The answer must be one valid solid
    no bigger than the part.
    """
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet

    from kiln import cad_kernel as k

    shape = solid
    rounds = [t for t in plan if t["kind"] == "fillet"]
    bevels = [t for t in plan if t["kind"] == "chamfer"]
    try:
        rounder = None
        if rounds:
            rounder = BRepFilletAPI_MakeFillet(shape)
            for t in rounds:
                for i in t["edges"]:
                    rounder.Add(float(t["size_mm"]), k.as_edge(edges.FindKey(i)))
            rounder.Build()
            if not rounder.IsDone():
                return None, "the kernel could not round these edges together"
            shape = rounder.Shape()
        if bevels:
            beveller = BRepFilletAPI_MakeChamfer(shape)
            now_edges = k.subshapes(shape, "edge") if rounder is not None else []
            for t in bevels:
                taken = 0
                for i in t["edges"]:
                    edge = k.as_edge(edges.FindKey(i))
                    for piece in (_what_is_left_of(edge, now_edges) if rounder is not None else [edge]):
                        beveller.Add(float(t["size_mm"]), piece)
                        taken += 1
                if not taken:
                    return None, "the rounding beside it used the edge up"
            beveller.Build()
            if not beveller.IsDone():
                return None, "the kernel could not bevel these edges together"
            shape = beveller.Shape()
    except Exception as exc:  # noqa: BLE001 -- the kernel signals a failed build by raising
        return None, f"the kernel failed on these edges ({type(exc).__name__})"
    if shape is None or shape.IsNull():
        return None, "the kernel returned nothing"
    if len(k.subshapes(shape, "solid")) != 1 or not BRepCheck_Analyzer(shape).IsValid():
        return None, "the result was not one valid solid"
    before, after = k.box(solid), k.box(shape)
    if any(after[i] < before[i] - _BOX_TOLERANCE_MM or after[i + 3] > before[i + 3] + _BOX_TOLERANCE_MM for i in range(3)):
        return None, "the result came out bigger than the part"
    return shape, ""


def _hole_sizes(shape: Any) -> list[float]:
    """The diameter of every round hole in *shape*, smallest first, as the kernel has them."""
    from kiln import cad_kernel as k

    return sorted(round(2.0 * hole["radius"], 4) for hole in k.full_cylinders(shape, concave=True))


#: A point of a rounded shape's edge is on an original edge when it is this close, mm.
_ON_THE_EDGE_MM = 1e-6


def _what_is_left_of(edge: Any, now_edges: list[Any]) -> list[Any]:
    """The edges of the rounded shape that lie along *edge* of the original.

    Found by where they are, not by the kernel's record of what it changed:
    rounding one rim of a face rebuilds the face, and the record then calls
    the face's other, untouched edges deleted (measured: the rim of a hole in
    the top of a post, once the post's outer rim was rounded).  What is left
    of an edge is every edge of the new shape whose quarter points all lie on
    it -- the whole edge when nothing touched it, the shortened middle when
    its corners were rounded, nothing when the rounding consumed it.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    from kiln import cad_kernel as k

    x0, y0, z0, x1, y1, z1 = k.box(edge)
    left = []
    for candidate in now_edges:
        curve = BRepAdaptor_Curve(candidate)
        t0, t1 = curve.FirstParameter(), curve.LastParameter()
        on_it = True
        for share in (0.25, 0.5, 0.75):
            point = curve.Value(t0 + share * (t1 - t0))
            if not (x0 - _ON_THE_EDGE_MM <= point.X() <= x1 + _ON_THE_EDGE_MM
                    and y0 - _ON_THE_EDGE_MM <= point.Y() <= y1 + _ON_THE_EDGE_MM
                    and z0 - _ON_THE_EDGE_MM <= point.Z() <= z1 + _ON_THE_EDGE_MM):
                on_it = False
                break
            gap = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(point).Vertex(), edge)
            if not gap.IsDone() or gap.Value() > _ON_THE_EDGE_MM:
                on_it = False
                break
        if on_it:
            left.append(candidate)
    return left


def _finish(solid: Any, request: dict[str, Any]) -> dict[str, Any]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    from kiln import cad_kernel as k

    edges, _faces, real = _edge_table(solid)
    plan: list[dict[str, Any]] = []
    for t in request["treatments"]:
        for i, mid in zip(t["edges"], t["mids"], strict=True):
            if i not in real:
                return {"refused": f"edge {i} is not an edge of this file; plan the edges again"}
            curve = BRepAdaptor_Curve(k.as_edge(edges.FindKey(i)))
            here = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
            if max(abs(a - b) for a, b in zip(_xyz(here), mid, strict=True)) > _SAME_EDGE_MM:
                return {"refused": f"edge {i} is not where the plan found it; plan the edges again"}
        plan.append({"edges": list(t["edges"]), "kind": t["kind"], "size_mm": float(t["size_mm"])})
    if not plan:
        return {"refused": "no edge was asked for"}

    started = time.monotonic()
    builds = 0
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    shape = None

    def attempt(trial: list[dict[str, Any]]) -> tuple[Any, str]:
        nonlocal builds
        builds += 1
        return _build(solid, edges, trial)

    def grow(pending: list[dict[str, Any]]) -> None:
        """Keep as many of *pending* as build together with what is kept already."""
        nonlocal shape
        if not pending:
            return
        if time.monotonic() - started > request["search_s"]:
            dropped.extend({**t, "reason": "not tried: the kernel ran out of time finding which edges it can finish"}
                           for t in pending)
            return
        built, why_not = attempt(kept + pending)
        if built is not None:
            kept.extend(pending)
            shape = built
            return
        if len(pending) == 1:
            dropped.append({**pending[0], "reason": why_not})
            return
        half = len(pending) // 2
        grow(pending[:half])
        grow(pending[half:])

    # Rounds as one block, then bevels on top of them: rounds that meet at a
    # corner are tried together first, and a bevel the kernel cannot build is
    # found without re-testing them.
    grow([t for t in plan if t["kind"] == "fillet"])
    grow([t for t in plan if t["kind"] == "chamfer"])
    if shape is None:
        reasons = sorted({d["reason"] for d in dropped})
        return {"refused": "the CAD kernel could not finish any of these edges: " + "; ".join(reasons)}

    if not k.write_stl(shape, request["out"], linear=request["linear"], angular=request["angular"]):
        return {"refused": "the finished part could not be written"}
    wrote_step = bool(request.get("out_step")) and k.write_step(shape, request["out_step"])
    return {
        "applied": kept,
        "dropped": dropped,
        "volume_before_mm3": round(k.volume(solid), 4),
        "volume_after_mm3": round(k.volume(shape), 4),
        "box_before": [round(v, 6) for v in k.box(solid)],
        "box_after": [round(v, 6) for v in k.box(shape)],
        "valid_solid": True,
        "holes_before_mm": _hole_sizes(solid),
        "holes_after_mm": _hole_sizes(shape),
        "step_written": wrote_step,
        "builds": builds,
        "seconds": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.dumps(_child(json.load(fh))))
