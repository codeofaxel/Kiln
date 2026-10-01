"""Round or bevel a part's sharp edges -- handed back only when the result measures better.

Measured 2026-10-01: the mesh fillet and chamfer engines
(:func:`kiln.generation.validation.add_fillet` / ``add_chamfer``) left an OPEN
surface on every part tried, a plain 20 mm cube included, and made the part
BIGGER -- the fillet by 1.41 mm across, the chamfer by 1.0 mm -- where rounding
or bevelling an edge only ever takes material away.  On a CAD enclosure the
fillet also lost seven of its eight screw holes.  Both tools reported success
every time.

So both go through one guarded door each, shared by the tools
(``add_mesh_fillet`` / ``add_mesh_chamfer``) and the reinforcement step of
``apply_design_reinforcements``: the result is measured against the input
(:mod:`kiln.mesh_edit_check`) -- the surface must stay closed, the part must not
grow, no hole may be lost, the thinnest wall must not shrink -- and a result
that fails is refused with the measurements, the caller's file untouched.

A STEP file is rounded and judged as Kiln's mesh of it (the shared
:func:`kiln.step_import.ensure_mesh_path` door), so it is refused in the same
words as its mesh rather than in a mesh reader's complaint about the format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiln.mesh_edit_check import guarded_edit

_INSTEAD = (
    "Round or bevel the edges in the part's design -- its OpenSCAD source or CAD file -- "
    "where the shape is rebuilt rather than patched."
)


def _output(file_path: str, output_path: str | None, suffix: str) -> str:
    if output_path:
        return output_path
    base = Path(file_path)
    return str(base.with_name(f"{base.stem}_{suffix}.stl"))


def fillet_part(
    file_path: str,
    *,
    radius_mm: float = 1.0,
    angle_threshold_deg: float = 60.0,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Round sharp edges; the reply carries the measurements, or a refusal saying why."""
    from kiln.generation.validation import add_fillet
    from kiln.step_import import ensure_mesh_path

    mesh = ensure_mesh_path(file_path)[0]
    return guarded_edit(
        mesh, _output(file_path, output_path, "filleted"),
        lambda scratch: add_fillet(
            mesh, radius_mm=radius_mm, angle_threshold_deg=angle_threshold_deg, output_path=scratch,
        ),
        edit="round the edges", instead=_INSTEAD,
    )


def chamfer_part(
    file_path: str,
    *,
    distance_mm: float = 0.5,
    angle_threshold_deg: float = 60.0,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Bevel sharp edges; the reply carries the measurements, or a refusal saying why."""
    from kiln.generation.validation import add_chamfer
    from kiln.step_import import ensure_mesh_path

    mesh = ensure_mesh_path(file_path)[0]
    return guarded_edit(
        mesh, _output(file_path, output_path, "chamfered"),
        lambda scratch: add_chamfer(
            mesh, distance_mm=distance_mm, angle_threshold_deg=angle_threshold_deg, output_path=scratch,
        ),
        edit="bevel the edges", instead=_INSTEAD,
    )
