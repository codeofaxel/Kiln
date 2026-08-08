"""Analytic facts about a STEP file — the truth behind a tessellated preview.

No viewer renders B-rep directly; every stage shows triangles.  What makes a
STEP display honest is carrying the ANALYTIC facts alongside the tessellation,
so the stage can say "1 solid, 4 true cylinders, r=45.000 exact" instead of
passing triangles off as CAD.  This module is the one place those facts are
measured — the same census serves the import flow, the inline stage, the
hosted viewer, and kiln-pro's B-rep emitter, so every surface reports the
same numbers about the same file.

Two measurement paths, one census:

* :func:`facts_from_shape` — kernel-side, called with a live ``TopoDS_Shape``.
  Runs ONLY inside child interpreters (the OCCT discipline of
  :mod:`kiln.step_import`: compiled C++ that Python cannot interrupt must die
  by timeout, never wedge a server worker).  The emitter calls it on the
  shape it just built; the reader child below calls it on a shape it read.
* :func:`read_step_facts` — parent-side.  Spawns a child that reads the file
  and runs the same :func:`facts_from_shape`, so read-side and emit-side
  facts can never drift.

When the kernel is not installed the answer degrades HONESTLY:
:func:`unavailable_facts` still carries what a text parse of the ISO header
can prove (name, schema, the Kiln stamp) with ``available: False`` and a
reason — never invented numbers.

The facts dict (``kind: "kiln.step_facts.v1"``)::

    {
      "kind": "kiln.step_facts.v1",
      "available": true,
      "source": "emitted" | "read",   # emitted = censused as built (exact by
                                      # construction); read = re-measured from
                                      # the file's own B-rep
      "solids": 1,
      "faces": 7,
      "edges": 15,
      "surfaces": {"plane": 3, "cylinder": 4},   # census by analytic type
      "cylinders": {"count": 4, "radii_mm": [3.1, 45.0]},  # unique, sorted
      "spheres":   {"count": 0, "radii_mm": []},
      "bbox_mm": {"min": [..], "max": [..], "size": [..]},  # tight analytic
      "header": {"name": ..., "schema": ..., "originating_system": ...,
                 "stamped_by_kiln": true},
    }

``bbox_mm`` is computed with ``BRepBndLib.AddOptimal_s`` over the exact
geometry (triangulation excluded) — a padded tessellation box would betray
the "exact" claim the readout makes.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Versioned discriminator for the facts dict.
FACTS_KIND = "kiln.step_facts.v1"

#: The originating_system stamp every Kiln-born STEP carries in its ISO
#: 10303-21 header.  Owned here (public — the string is readable in every
#: emitted file); kiln-pro's emitter and re-stamper write it.
KILN_STAMP = "Kiln - kiln3d.com"

#: Unique radii reported per surface family before the list is capped.  The
#: census COUNTS stay exact; the radii list is display material and a knurled
#: part with hundreds of distinct fillet radii should not ship them all.
MAX_REPORTED_RADII = 12


def facts_from_shape(shape: Any, *, source: str = "read") -> dict[str, Any]:
    """Census a live ``TopoDS_Shape`` into the facts dict (sans header).

    KERNEL-SIDE: imports OCP, so call this only where OCP is already loaded —
    inside a child interpreter (the reader child below, kiln-pro's emitter
    child).  Never import it into a serving process.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def _unique(kind: Any) -> Any:
        found = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, found)
        return found

    solids = _unique(TopAbs_ShapeEnum.TopAbs_SOLID)
    faces = _unique(TopAbs_ShapeEnum.TopAbs_FACE)
    edges = _unique(TopAbs_ShapeEnum.TopAbs_EDGE)

    _TYPE_NAMES = {
        GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
        GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
        GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
        GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
        GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
        GeomAbs_SurfaceType.GeomAbs_BezierSurface: "bezier",
        GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "bspline",
        GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "revolution",
        GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "extrusion",
        GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "offset",
    }

    census: dict[str, int] = {}
    cylinder_radii: set[float] = set()
    sphere_radii: set[float] = set()
    for i in range(1, faces.Extent() + 1):
        face = TopoDS.Face_s(faces.FindKey(i))
        adaptor = BRepAdaptor_Surface(face)
        surface_type = adaptor.GetType()
        name = _TYPE_NAMES.get(surface_type, "other")
        census[name] = census.get(name, 0) + 1
        # Exact radii, deduplicated at micron precision: one physical
        # cylinder split into several faces reports one radius.
        if name == "cylinder":
            cylinder_radii.add(round(adaptor.Cylinder().Radius(), 6))
        elif name == "sphere":
            sphere_radii.add(round(adaptor.Sphere().Radius(), 6))

    # Tight analytic bounds: AddOptimal over the exact geometry.  The default
    # Add pads by shape tolerance and falls back to triangulation, and a
    # padded box under an "exact" label is the lie this module exists to
    # prevent.
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

    def _radii(values: set[float]) -> list[float]:
        return sorted(values)[:MAX_REPORTED_RADII]

    return {
        "kind": FACTS_KIND,
        "available": True,
        "source": source,
        "solids": solids.Extent(),
        "faces": faces.Extent(),
        "edges": edges.Extent(),
        "surfaces": census,
        "cylinders": {
            "count": census.get("cylinder", 0),
            "radii_mm": _radii(cylinder_radii),
        },
        "spheres": {
            "count": census.get("sphere", 0),
            "radii_mm": _radii(sphere_radii),
        },
        "bbox_mm": {
            "min": [xmin, ymin, zmin],
            "max": [xmax, ymax, zmax],
            "size": [xmax - xmin, ymax - ymin, zmax - zmin],
        },
    }


# Runs in a CHILD interpreter — same discipline as step_import's converters.
# The child imports THIS module for the census, so read-side facts are the
# same code path emit-side facts use; PYTHONPATH below guarantees the child
# resolves the same ``kiln`` the parent is running.
_FACTS_CHILD_TEMPLATE = r'''
import json, sys

from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPControl import STEPControl_Reader

from kiln.step_facts import facts_from_shape

step_path = {step_path!r}

reader = STEPControl_Reader()
if reader.ReadFile(step_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
    sys.stderr.write("OCCT could not read the STEP file\n")
    raise SystemExit(3)
reader.TransferRoots()
shape = reader.OneShape()
if shape.IsNull():
    sys.stderr.write("the STEP file contains no transferable shape\n")
    raise SystemExit(3)

print("KILN_FACTS:" + json.dumps(facts_from_shape(shape, source="read")))
'''


def header_facts(step_path: str | Path) -> dict[str, Any]:
    """Header block from a text parse alone — no kernel required.

    Reads the ISO 10303-21 header via :func:`kiln.step_import.get_step_metadata`
    (the existing lightweight reader) plus the ``originating_system`` field it
    now extracts, and answers the one question a viewer chrome asks first:
    is this file Kiln-stamped?
    """
    from kiln.step_import import get_step_metadata

    try:
        meta = get_step_metadata(str(step_path))
    except (FileNotFoundError, ValueError, OSError):
        return {
            "name": None,
            "schema": None,
            "originating_system": None,
            "stamped_by_kiln": False,
        }
    originating = meta.get("originating_system")
    products = meta.get("products") or []
    # FILE_NAME's own name field is the header's claim about the part; the
    # PRODUCT entities carry kernel-suffixed spellings ("part 1").
    name = (
        meta.get("original_file_name")
        or (products[0] if products else None)
        or meta.get("description")
    )
    return {
        "name": name,
        "schema": meta.get("schema"),
        "originating_system": originating,
        "stamped_by_kiln": originating == KILN_STAMP,
    }


def unavailable_facts(
    step_path: str | Path, reason: str
) -> dict[str, Any]:
    """The honest degraded answer: header truth, no invented geometry.

    A stage receiving this shows its mesh preview with "CAD facts unavailable"
    — it never fills the census in from the triangles, which would be the
    exact mesh-in-CAD-clothing dishonesty the facts exist to prevent.
    """
    return {
        "kind": FACTS_KIND,
        "available": False,
        "source": "read",
        "reason": reason,
        "header": header_facts(step_path),
    }


def read_step_facts(
    step_path: str | Path,
    *,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Measure a STEP file's analytic facts, or degrade honestly.

    Runs the kernel in a child interpreter (timeout + clean kill — OCCT can
    not be interrupted in-process).  Returns the facts dict with ``available``
    telling the truth about which path answered:

    * kernel present, file read → the full census, ``source: "read"``.
    * kernel absent / child failed / timed out → :func:`unavailable_facts`
      with the reason, header facts still filled from the text parse.

    Never raises for a missing kernel or an unreadable file — a facts readout
    is display material and must not take the surface down with it.  Path
    validation errors (traversal, wrong extension) do raise, mirroring
    :func:`kiln.step_import.convert_step_to_stl`.
    """
    from kiln.step_import import (
        SUBPROCESS_TIMEOUT_S,
        _ocp_available,
        _validate_step_path,
    )

    validated = _validate_step_path(str(step_path))
    if not _ocp_available():
        return unavailable_facts(
            validated,
            "CAD facts unavailable on this install (no OCCT kernel). "
            "The preview below is a display tessellation.",
        )

    script = _FACTS_CHILD_TEMPLATE.format(step_path=str(validated))
    # The child must resolve the SAME ``kiln`` package as this parent —
    # editable installs and test worktrees otherwise skew the census import
    # to whatever tree sys.executable finds first.
    import os

    src_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        src_root + os.pathsep + existing if existing else src_root
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout_s or SUBPROCESS_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return unavailable_facts(
            validated,
            f"CAD facts measurement timed out after "
            f"{timeout_s or SUBPROCESS_TIMEOUT_S}s and was stopped cleanly.",
        )

    line = next(
        (
            ln
            for ln in (proc.stdout or "").splitlines()
            if ln.startswith("KILN_FACTS:")
        ),
        None,
    )
    if proc.returncode != 0 or line is None:
        stderr = (proc.stderr or "").strip()
        logger.debug("step facts child failed: %s", stderr[-500:])
        return unavailable_facts(
            validated,
            "The CAD kernel could not measure this file.",
        )

    try:
        facts = json.loads(line[len("KILN_FACTS:"):])
    except json.JSONDecodeError:
        return unavailable_facts(
            validated, "The CAD kernel returned an unreadable measurement."
        )
    facts["header"] = header_facts(validated)
    return facts
