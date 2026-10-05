"""Thicken a part's walls: exactly from its CAD file, on a mesh only when it measures better.

The one door every caller of "thicken the walls" goes through --
``thicken_mesh_walls`` and the reinforcement step of
``apply_design_reinforcements`` alike -- so both get the same two routes:

* **The CAD file**, when there is one: the part's STEP file passed directly,
  or the one a Kiln-converted mesh came from, sitting beside it under the same
  name and converting to that same mesh.  Every surface moves out by the
  amount, exactly, and round holes keep their size (:mod:`kiln.cad_offset`).
* **The mesh**, otherwise, through a true offset -- the part grown by a ball,
  round holes cut back to size (:mod:`kiln.mesh_offset`) -- handed back only
  when it measures better than it went in (:mod:`kiln.mesh_edit_check`).  A
  tilted hole the detector cannot see, or a mesh with no closed inside, is
  refused rather than handed back damaged, and the reply points at the CAD
  route.

Either way the result is measured against the input: the thinnest wall must
gain at least the amount, the part may grow by at most twice it, the surface
stays closed, and every hole is still there (at its size, unless the caller
let holes move).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kiln.mesh_edit_check import EDIT_REFUSED, guarded_edit

#: More than this per surface is a redesign, not a thickening: it fills
#: slots, swallows small features, and grows the part past its fit.
MAX_AMOUNT_MM = 5.0

_INSTEAD_ON_A_MESH = (
    "Pass the part's STEP file instead: Kiln moves every surface of the CAD exactly and keeps "
    "round holes at size. Or raise the wall thickness in the design that made it."
)
_INSTEAD_FROM_CAD = "Raise the wall thickness in the CAD itself, where the kernel can rebuild the part."


def thicken_part(
    file_path: str,
    *,
    amount_mm: float = 0.5,
    output_path: str | None = None,
    keep_hole_size: bool = True,
) -> dict[str, Any]:
    """Thicken *file_path*'s walls by *amount_mm* per surface; see the module docstring.

    Returns a reply dict: ``success`` with ``path``, ``method`` (``cad`` or
    ``mesh``), the measurements before and after and a ``note`` -- or
    ``success`` False with ``code`` :data:`kiln.mesh_edit_check.EDIT_REFUSED`
    and a ``message`` that says what the edit would have done.  Raises
    ``ValueError`` for an amount out of range.
    """
    from kiln.step_import import ensure_mesh_path, is_step_file, step_converted_from

    if not 0 < amount_mm <= MAX_AMOUNT_MM:
        raise ValueError(f"amount_mm must be above 0 and at most {MAX_AMOUNT_MM:g} mm")
    source = file_path if is_step_file(file_path) else step_converted_from(file_path)
    mesh_in = ensure_mesh_path(file_path)[0] if is_step_file(file_path) else file_path
    if output_path is None:
        base = Path(file_path)
        output_path = str(base.with_name(f"{base.stem}_thickened.stl"))

    promise = {"grows_by_mm": amount_mm, "wall_grows_by_mm": amount_mm, "holes_keep_size": keep_hole_size}
    cad_failure = ""
    if source is not None:
        import shutil
        import tempfile

        from kiln.cad_offset import CadOffsetError, offset_step
        from kiln.step_import import keep_step_beside

        scratch_dir = tempfile.mkdtemp(prefix="kiln_thicken_")
        scratch_step = os.path.join(scratch_dir, "thickened.step")
        try:
            reply = guarded_edit(
                mesh_in, output_path,
                lambda scratch: offset_step(
                    source, scratch, amount_mm=amount_mm, keep_hole_size=keep_hole_size, output_step=scratch_step,
                ),
                edit="thicken the walls", instead=_INSTEAD_FROM_CAD, **promise,
            )
        except CadOffsetError as exc:
            cad_failure = str(exc)
        else:
            reply.update(method="cad", cad_source=source)
            if reply.get("success") and Path(output_path).suffix.lower() == ".stl":
                # The thickened CAD beside the thickened mesh: the next edit
                # of this part starts from CAD again.
                reply["step_path"], beside = keep_step_beside(scratch_step, output_path, source)
                reply = _worded(reply, amount_mm, keep_hole_size)
                if reply["step_path"]:
                    reply["note"] += f" The thickened CAD is {os.path.basename(reply['step_path'])}, beside the mesh.{beside}"
                return reply
            return _worded(reply, amount_mm, keep_hole_size)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    from kiln.mesh_offset import MeshOffsetError, offset_mesh

    try:
        reply = guarded_edit(
            mesh_in, output_path,
            lambda scratch: offset_mesh(mesh_in, scratch, amount_mm=amount_mm, keep_hole_size=keep_hole_size),
            edit="thicken the walls", instead=_INSTEAD_ON_A_MESH, **promise,
        )
    except MeshOffsetError as exc:
        reply = {
            "success": False,
            "code": EDIT_REFUSED,
            "message": f"Kiln did not thicken the walls: {exc}. Your file is unchanged. {_INSTEAD_ON_A_MESH}",
        }
    reply["method"] = "mesh"
    if cad_failure:
        reply["cad_failure"] = f"The CAD route was tried first and could not be used: {cad_failure}."
    return _worded(reply, amount_mm, keep_hole_size)


def _worded(reply: dict[str, Any], amount_mm: float, keep_hole_size: bool) -> dict[str, Any]:
    """Add the plain-English note a successful thickening carries."""
    if not reply.get("success"):
        reply.setdefault("code", EDIT_REFUSED)
        return reply
    before, after = reply["measured"]["before"], reply["measured"]["after"]
    source = os.path.basename(reply["cad_source"]) if reply.get("method") == "cad" else ""
    how = f"from its CAD file {source}" if source else "on the mesh"
    holes = before["holes_mm"]
    if not holes:
        kept = "It has no round holes."
    elif keep_hole_size:
        kept = f"All {len(holes)} round holes kept their size."
    else:
        kept = f"Its {len(holes)} round holes are each {2 * amount_mm:g} mm narrower, as asked."
    reply["note"] = (
        f"Every surface moved out {amount_mm:g} mm, {how}: the thinnest wall went from "
        f"{before['thinnest_wall_mm']} to {after['thinnest_wall_mm']} mm, and the part is now "
        f"{' x '.join(f'{v:g}' for v in after['size_mm'])} mm "
        f"(was {' x '.join(f'{v:g}' for v in before['size_mm'])}). {kept} "
        f"Openings that are not round holes -- slots, vents, cutouts -- are {2 * amount_mm:g} mm narrower."
    )
    return reply
