"""The CAD kernel calls Kiln's kernel children share, under names both kernel versions answer to.

Kiln's kernel children -- thickening (:mod:`kiln.cad_offset`), edge rounding
and bevelling (:mod:`kiln.cad_edge`) -- read one solid from a STEP file,
walk its faces and edges, and write a mesh.  Those few calls live here once.

Kernel 8.0 renamed the calls that narrow a shape to its kind
(``TopoDS.Face_s`` became ``TopoDS.Face``), moved the shape collections into
``OCP.collections`` under generated names, and returns a bounding box's
limits as a struct its bindings cannot hand to Python.  A child written
against 7.9 alone stops at its first import on 8.0, so every such call goes
through this module and either version works.

Imports nothing at module level: the kernel is an optional install, loaded
only inside the child interpreter that uses it.
"""

from __future__ import annotations

import math
from typing import Any


def _narrow(kind: str, shape: Any) -> Any:
    from OCP.TopoDS import TopoDS

    cast = getattr(TopoDS, kind + "_s", None) or getattr(TopoDS, kind)
    return cast(shape)


def as_solid(shape: Any) -> Any:
    return _narrow("Solid", shape)


def as_face(shape: Any) -> Any:
    return _narrow("Face", shape)


def as_edge(shape: Any) -> Any:
    return _narrow("Edge", shape)


def as_vertex(shape: Any) -> Any:
    return _narrow("Vertex", shape)


def shape_map() -> Any:
    """An empty indexed map of shapes, for ``TopExp.MapShapes_s``."""
    try:
        from OCP.TopTools import TopTools_IndexedMapOfShape as Map
    except ImportError:
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as Map
    return Map()


def ancestor_map() -> Any:
    """An empty map from a shape to the shapes it bounds, for ``TopExp.MapShapesAndAncestors_s``."""
    try:
        from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape as Map
    except ImportError:
        from OCP.collections import IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as Map
    return Map()


_KINDS = {"solid": as_solid, "face": as_face, "edge": as_edge, "vertex": as_vertex}


def indexed(shape: Any, kind: str) -> Any:
    """The indexed map of *shape*'s sub-shapes of *kind*; index 1 is the first."""
    from OCP import TopAbs
    from OCP.TopExp import TopExp

    found = shape_map()
    TopExp.MapShapes_s(shape, getattr(TopAbs, "TopAbs_" + kind.upper()), found)
    return found


def subshapes(shape: Any, kind: str) -> list[Any]:
    """*shape*'s sub-shapes of *kind* (``"solid"``, ``"face"``, ``"edge"``, ``"vertex"``), each once."""
    found = indexed(shape, kind)
    return [_KINDS[kind](found.FindKey(i)) for i in range(1, found.Extent() + 1)]


def box(shape: Any) -> tuple[float, float, float, float, float, float]:
    """``(xmin, ymin, zmin, xmax, ymax, zmax)`` of *shape*, from its exact geometry."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    limits = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, limits, False, False)
    # The two corners, not Get(): kernel 8.0 returns Get() as a struct its
    # bindings cannot hand to Python.
    lo, hi = limits.CornerMin(), limits.CornerMax()
    return lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()


def volume(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def area(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return float(props.Mass())


def read_one_solid(step_path: str, *, verb: str) -> tuple[Any, str | None]:
    """The single solid in *step_path*, or ``(None, why not)``.

    *verb* finishes the sentence a file holding several solids is refused
    with: "Kiln thickens one part at a time".
    """
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    if reader.ReadFile(step_path) != IFSelect_RetDone:
        return None, "the STEP file could not be read"
    reader.TransferRoots()
    solids = subshapes(reader.OneShape(), "solid")
    if len(solids) != 1:
        return None, f"the file holds {len(solids)} solids; Kiln {verb} one part at a time"
    return solids[0], None


def outward_normal(face: Any, u: float, v: float) -> tuple[Any, Any]:
    """The point and the unit normal pointing out of the solid at ``(u, v)`` on *face*."""
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec

    point, normal = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(u, v, point, normal)  # the face's orientation is applied
    if normal.Magnitude() > 0:
        normal.Normalize()
    return point, normal


def full_cylinders(solid: Any, *, concave: bool) -> list[dict[str, Any]]:
    """Cylinders whose faces close a full circle around one axis line.

    *concave* picks which: a bore (the outward normal points at the axis) or a
    boss (it points away).  A partial arc -- an inner-corner rounding, the end
    of a slot, a rounded outside corner -- is neither.  Each answer carries its
    ``faces``, ``radius``, a point on the axis (``origin``), the axis direction
    (``axis``, sign-normalised) and the ``span`` of its first face in degrees.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.gp import gp_Vec

    groups: dict[tuple, list[tuple[Any, dict[str, Any]]]] = {}
    for face in subshapes(solid, "face"):
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Cylinder:
            continue
        cylinder = surface.Cylinder()
        origin, axis = cylinder.Axis().Location(), cylinder.Axis().Direction()
        u0, u1 = surface.FirstUParameter(), surface.LastUParameter()
        v0, v1 = surface.FirstVParameter(), surface.LastVParameter()
        point, normal = outward_normal(face, 0.5 * (u0 + u1), 0.5 * (v0 + v1))
        to_point = gp_Vec(origin, point)
        along = gp_Vec(axis)
        radial = to_point - along.Multiplied(to_point.Dot(along))
        if radial.Magnitude() == 0:
            continue
        points_at_axis = normal.Dot(radial) < 0
        if points_at_axis != concave:
            continue
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


def write_stl(shape: Any, path: str, *, linear: float, angular: float) -> bool:
    """Tessellate *shape* to the given bounds and write it as a closed binary STL."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.StlAPI import StlAPI_Writer

    # Mesh first: an untriangulated shape writes an empty file, not an error.
    BRepMesh_IncrementalMesh(shape, linear, False, angular, True)
    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    if not writer.Write(shape, path):
        return False
    drop_collapsed_facets(path)
    return True


#: Two corners of a facet are the same point when they are this close, mm:
#: far below any tessellation bound, far above float32 noise at part scale.
_SAME_POINT_MM = 1e-7
#: Facets read per pass.  A finely rounded part's mesh runs to hundreds of
#: megabytes; it is cleaned a slice at a time, never held whole.
_FACETS_PER_PASS = 200_000


def drop_collapsed_facets(stl_path: str) -> int:
    """Remove facets with two corners at one point from a binary STL; returns how many.

    Where a surface closes to a point -- the pole of a ball-rounded corner,
    the tip of a cone -- the kernel's mesher writes a facet with two of its
    corners at that point.  It has no area, and it is not harmless: a mesh
    reader sees an edge from a point to itself, counts the surface as open,
    and every check downstream calls a sound solid "not closed" (measured
    2026-10-04: a block with rounded corners, eight such facets, volume
    read as zero).  Without them the surface is the same surface, closed.

    A file that is not a well-formed binary STL is left exactly as it is,
    and so is one with nothing to remove.
    """
    import os

    import numpy as np

    facet = np.dtype([("normal", "<f4", (3,)), ("corners", "<f4", (3, 3)), ("attribute", "<u2")])
    try:
        size = os.path.getsize(stl_path)
        with open(stl_path, "rb") as fh:
            head = fh.read(84)
    except OSError:
        return 0
    if len(head) < 84:
        return 0
    count = int.from_bytes(head[80:84], "little")
    if size != 84 + facet.itemsize * count:
        return 0

    def passes(fh: Any) -> Any:
        fh.seek(84)
        while True:
            raw = fh.read(facet.itemsize * _FACETS_PER_PASS)
            if not raw:
                return
            facets = np.frombuffer(raw, dtype=facet)
            a, b, c = (facets["corners"][:, i] for i in range(3))
            collapsed = np.zeros(len(facets), dtype=bool)
            for p, q in ((a, b), (b, c), (a, c)):
                collapsed |= np.abs(p - q).max(axis=1) <= _SAME_POINT_MM
            yield facets, collapsed

    with open(stl_path, "rb") as fh:
        dropped = sum(int(collapsed.sum()) for _, collapsed in passes(fh))
    if not dropped:
        return 0
    cleaned = stl_path + ".cleaning"
    try:
        with open(stl_path, "rb") as src, open(cleaned, "wb") as dst:
            dst.write(head[:80] + (count - dropped).to_bytes(4, "little"))
            for facets, collapsed in passes(src):
                dst.write(facets[~collapsed].tobytes())
        os.replace(cleaned, stl_path)
    finally:
        if os.path.exists(cleaned):
            os.unlink(cleaned)
    return dropped


#: What a STEP written by Kiln names as the system that made it.
STEP_MADE_BY = "Kiln | kiln3d.com"


def write_step(shape: Any, path: str) -> bool:
    """Write *shape* as a STEP file whose header names Kiln as the system that made it."""
    from OCP.APIHeaderSection import APIHeaderSection_MakeHeader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TCollection import TCollection_HAsciiString

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    APIHeaderSection_MakeHeader(writer.Model()).SetOriginatingSystem(TCollection_HAsciiString(STEP_MADE_BY))
    return writer.Write(path) == IFSelect_RetDone


def step_made_by_kiln(path: str) -> bool:
    """True when the STEP file at *path* carries the header :func:`write_step` writes."""
    try:
        with open(path, "rb") as fh:
            return STEP_MADE_BY.encode("ascii") in fh.read(2048)
    except OSError:
        return False
