"""A mesh edit is judged by the mesh it makes, before anyone is told it worked.

Measured 2026-10-01 on a clean CAD enclosure -- 80 x 55 x 28 mm, walls
1.2-2.0 mm, eight screw holes -- through the three geometry-repair tools:

* ``thicken_mesh_walls(0.4)`` left the thinnest wall at 0.096 mm, lost all
  eight holes and creased the floor;
* ``add_mesh_fillet`` lost seven of the holes, opened the surface, and made
  the part 1.4 mm BIGGER -- rounding an edge only ever removes material;
* ``add_mesh_chamfer`` opened the surface and made the part 1 mm bigger.

All three reported success.  Their tests checked that a file was written.

So each of them now measures its input and its output the same way -- the
thinnest wall, every hole, the outside size, whether the surface is closed
(:func:`measure_mesh`) -- and :func:`judge_edit` names, in a sentence, every
way the result is worse than the edit promised.  A result with any problem is
refused: the caller's file is untouched and the reply says what happened and
what would work instead.  The measuring is the printability analyzer's own,
so a refused edit is refused in the numbers the rest of Kiln reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Two measurements of the same surface agree to this, in mm: faceting and
#: the analyzer's own sampling, not an edit.
_SAME_MM = 0.05
#: A hole kept "at size" is within this of its old diameter, in mm.  The hole
#: detector fits a circle to facets; a re-tessellated hole lands within a few
#: hundredths, a hole an offset closed by 2 x 0.4 mm is far outside it.
_HOLE_SAME_MM = 0.1


@dataclass(frozen=True)
class MeshMeasure:
    """What a mesh edit is judged on."""

    extents_mm: tuple[float, float, float]
    watertight: bool
    volume_mm3: float
    min_wall_mm: float | None
    hole_diameters_mm: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "size_mm": [round(e, 2) for e in self.extents_mm],
            "closed_surface": self.watertight,
            "volume_mm3": round(self.volume_mm3, 1),
            "thinnest_wall_mm": None if self.min_wall_mm is None else round(self.min_wall_mm, 3),
            "holes_mm": [round(d, 2) for d in self.hole_diameters_mm],
        }


def measure_mesh(path: str) -> MeshMeasure:
    """Measure *path* the way every edit is judged.  Raises on an unreadable mesh."""
    from kiln.mesh_frame import load_mesh
    from kiln.printability import analyze_printability

    # Kiln's frame, as every other reader of the same file sees it: a glTF
    # measured raw lies on its side, and its extents disagree with the
    # printability report taken beside them.
    mesh = load_mesh(path, force="mesh")
    report = analyze_printability(path, include_hole_detection=True)
    holes = sorted(float(h.get("diameter_mm") or 0.0) for h in report.holes or [] if h.get("diameter_mm"))
    wall = report.thin_walls.min_wall_thickness_mm if report.thin_walls is not None else None
    return MeshMeasure(
        extents_mm=tuple(float(e) for e in mesh.extents),
        watertight=bool(mesh.is_watertight),
        volume_mm3=float(abs(mesh.volume)) if mesh.is_watertight else 0.0,
        min_wall_mm=float(wall) if wall is not None else None,
        hole_diameters_mm=tuple(holes),
    )


@dataclass(frozen=True)
class EditVerdict:
    """Whether an edit kept its promise, and the measurements that decided it."""

    ok: bool
    problems: tuple[str, ...]
    before: MeshMeasure
    after: MeshMeasure

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


def _unmatched_holes(before: tuple[float, ...], after: tuple[float, ...]) -> list[float]:
    """Diameters in *before* with no hole of the same size left in *after*."""
    left = list(after)
    missing: list[float] = []
    for d in before:
        match = next((a for a in left if abs(a - d) <= _HOLE_SAME_MM), None)
        if match is None:
            missing.append(d)
        else:
            left.remove(match)
    return missing


def judge_edit(
    before: MeshMeasure,
    after: MeshMeasure,
    *,
    grows_by_mm: float = 0.0,
    wall_grows_by_mm: float | None = None,
    holes_keep_size: bool = True,
) -> EditVerdict:
    """Name every way *after* is worse than the edit promised.

    *grows_by_mm* is how far the edit moves the surface outward (a
    thickening's amount; 0 for an edit that only removes material), so the
    part may grow by at most twice it across.  *wall_grows_by_mm* is the
    least the thinnest wall must gain, when the edit is meant to thicken.
    *holes_keep_size* ``False`` lets holes change size -- an offset that was
    told to leave them -- but never lets one disappear.
    """
    problems: list[str] = []
    if before.watertight and not after.watertight:
        problems.append("its surface is no longer closed, so a slicer would have to guess what is inside it")

    allowed = 2 * grows_by_mm + _SAME_MM
    growth = max(a - b for a, b in zip(after.extents_mm, before.extents_mm, strict=True))
    if growth > allowed:
        if grows_by_mm:
            problems.append(f"it grew {growth:.2f} mm across, where moving every surface {grows_by_mm:g} mm grows it {2 * grows_by_mm:g}")
        else:
            problems.append(f"it grew {growth:.2f} mm across, and this edit only ever takes material away")

    if holes_keep_size:
        for d in _unmatched_holes(before.hole_diameters_mm, after.hole_diameters_mm):
            problems.append(f"the {d:.1f} mm hole is gone or no longer {d:.1f} mm")
    elif len(after.hole_diameters_mm) < len(before.hole_diameters_mm):
        lost = len(before.hole_diameters_mm) - len(after.hole_diameters_mm)
        problems.append(f"{lost} of its {len(before.hole_diameters_mm)} holes closed")

    if before.min_wall_mm is not None and after.min_wall_mm is not None:
        if wall_grows_by_mm is not None and after.min_wall_mm < before.min_wall_mm + 0.9 * wall_grows_by_mm:
            problems.append(
                f"its thinnest wall is {after.min_wall_mm:.2f} mm, from {before.min_wall_mm:.2f} mm -- "
                f"it should have gained at least {wall_grows_by_mm:g} mm"
            )
        elif wall_grows_by_mm is None and after.min_wall_mm < before.min_wall_mm - _SAME_MM:
            problems.append(f"its thinnest wall went from {before.min_wall_mm:.2f} mm to {after.min_wall_mm:.2f} mm")

    return EditVerdict(ok=not problems, problems=tuple(problems), before=before, after=after)


def refusal_sentence(edit: str, verdict: EditVerdict, *, instead: str) -> str:
    """One sentence a person reads: what the edit would have done, and what to do."""
    listed = "; ".join(verdict.problems)
    return f"Kiln did not {edit}: the result was worse than the part you gave it -- {listed}. Your file is unchanged. {instead}"


#: The code a refused edit carries, whichever tool refused it.
EDIT_REFUSED = "EDIT_WOULD_DAMAGE"


def guarded_edit(
    file_path: str,
    output_path: str,
    engine: Any,
    *,
    edit: str,
    instead: str,
    grows_by_mm: float = 0.0,
    wall_grows_by_mm: float | None = None,
    holes_keep_size: bool = True,
) -> dict[str, Any]:
    """Run *engine* into a scratch file and hand the result back only if it kept its promise.

    *engine* is called as ``engine(scratch_path)`` and returns the edit's own
    statistics.  The result is measured against *file_path*; when it is
    worse (:func:`judge_edit`), the scratch file is deleted, nothing is
    written to *output_path*, and the reply says why and what to do instead
    (*instead*).  Either way the reply carries both measurements.
    """
    import os
    import shutil
    import tempfile

    before = measure_mesh(file_path)
    fd, scratch = tempfile.mkstemp(suffix=os.path.splitext(output_path)[1] or ".stl")
    os.close(fd)
    try:
        stats = engine(scratch)
        verdict = judge_edit(
            before,
            measure_mesh(scratch),
            grows_by_mm=grows_by_mm,
            wall_grows_by_mm=wall_grows_by_mm,
            holes_keep_size=holes_keep_size,
        )
        if not verdict.ok:
            return {
                "success": False,
                "code": EDIT_REFUSED,
                "message": refusal_sentence(edit, verdict, instead=instead),
                "measured": verdict.to_dict(),
                **({"engine": stats} if stats else {}),
            }
        shutil.move(scratch, output_path)  # across filesystems, unlike os.replace
        return {"success": True, "path": output_path, "measured": verdict.to_dict(), **(stats or {})}
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
