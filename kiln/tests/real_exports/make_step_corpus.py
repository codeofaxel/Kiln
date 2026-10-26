"""Build the STEP half of the real-export corpus, and measure every STEP file.

Run with an interpreter that has build123d (any recent release; 0.11.1 made
the committed files):

    python3.12 kiln/tests/real_exports/make_step_corpus.py kiln/tests/real_exports

Two jobs:

1. Write ``assembly_3_bodies.step``: a small three-body assembly made with
   build123d (a base plate, a post standing on it, and a cap sitting on the
   post), each body its own solid, exported as one STEP the way a CAD program
   exports an assembly.  The bodies touch where they meet, as assembled parts
   do.
2. Measure every ``*.step`` in the directory, the Autodesk Fusion exports
   included, straight from the file's own B-rep through the OpenCASCADE
   kernel: how many solids, whether each solid's shells are closed, and the
   bounding box in the file's declared unit (millimetres for all three).
   The result is ``step_truth.json``.  Nothing in it comes from Kiln's mesh
   pipeline; it is read from the solids the file declares, before anything is
   turned into triangles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from build123d import Box, Compound, Cylinder, Location, export_step, import_step
from OCP.BRep import BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.TopAbs import TopAbs_EDGE, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def _build_assembly(path: Path) -> None:
    base = Box(40, 30, 4).moved(Location((0, 0, 2)))
    post = Cylinder(5, 16).moved(Location((0, 0, 4 + 8)))
    cap = Box(16, 16, 3).moved(Location((0, 0, 20 + 1.5)))
    base.label, post.label, cap.label = "Base", "Post", "Cap"
    assembly = Compound(label="Assembly", children=[base, post, cap])
    export_step(assembly, str(path))


def _shell_closed(shell) -> bool:
    """Closed when every edge is shared by two faces AND no free boundary
    wire is left: the kernel's own two readings of "sealed", both required."""
    free = ShapeAnalysis_FreeBounds(shell)
    open_edges = TopExp_Explorer(free.GetOpenWires(), TopAbs_EDGE)
    return bool(BRep_Tool.IsClosed_s(shell)) and not open_edges.More()


def _measure(path: Path) -> dict:
    shape = import_step(str(path))
    solids = shape.solids()
    pieces = []
    for i, solid in enumerate(solids):
        shells = []
        explorer = TopExp_Explorer(solid.wrapped, TopAbs_SHELL)
        while explorer.More():
            shells.append(TopoDS.Shell_s(explorer.Current()))
            explorer.Next()
        bb = solid.bounding_box()
        pieces.append({
            "index": i,
            "valid": bool(BRepCheck_Analyzer(solid.wrapped).IsValid()),
            "closed": bool(shells) and all(_shell_closed(s) for s in shells),
            "volume_mm3": round(solid.volume, 3),
            "size_mm": [round(bb.size.X, 4), round(bb.size.Y, 4), round(bb.size.Z, 4)],
        })
    bb = shape.bounding_box()
    lo = [bb.min.X, bb.min.Y, bb.min.Z]
    hi = [bb.max.X, bb.max.Y, bb.max.Z]
    return {
        "unit": "mm",
        "scale_to_mm": 1.0,
        "up_axis": "Z",
        "piece_count": len(solids),
        "pieces": pieces,
        "all_closed": all(p["closed"] for p in pieces),
        "size_mm": [round(top - bottom, 3) for bottom, top in zip(lo, hi, strict=True)],
        "min_z_mm": round(lo[2], 3),
        "rests_on_bed": abs(lo[2]) < 1e-6,
        "colours": 0,
        "pieces_touch": len(solids) > 1,
    }


#: How each STEP file was made.  The Fusion exports are our own
#: part, saved twice from Autodesk Fusion (File > Export > STEP) with the
#: second revision 8 mm taller.
MADE_BY = {
    "assembly_3_bodies.step": ("build123d", "export_step of a three-child Compound"),
    "enclosure_v1.step": ("Autodesk Fusion", "File > Export > STEP, revision 1"),
    "enclosure_v2.step": ("Autodesk Fusion", "File > Export > STEP, revision 2"),
}


if __name__ == "__main__":
    _build_assembly(OUT / "assembly_3_bodies.step")
    truth = {}
    for path in sorted(OUT.glob("*.step")):
        made_by, export = MADE_BY[path.name]
        truth[path.name] = {"made_by": made_by, "export": export, **_measure(path)}
    (OUT / "step_truth.json").write_text(json.dumps(truth, indent=1, sort_keys=True) + "\n")
    for name, t in truth.items():
        print(name, t["piece_count"], "solids", [p["closed"] for p in t["pieces"]], t["size_mm"], t["min_z_mm"])
