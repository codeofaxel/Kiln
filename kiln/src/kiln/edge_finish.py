"""Round or bevel a part's edges -- on its CAD file, where the faces are rebuilt exactly.

The one door ``add_mesh_fillet``, ``add_mesh_chamfer`` and the reinforcement
step of ``apply_design_reinforcements`` go through.  What it does depends on
what the part is made of:

* **A CAD file** -- a STEP passed directly, or the one a Kiln-converted mesh
  still sits beside (:func:`kiln.step_import.step_converted_from`).  The
  kernel reads every edge (:mod:`kiln.cad_edge`), a plan picks the edges and
  sizes for the printer the part is for (:mod:`kiln.edge_plan`), and the
  kernel rebuilds the solid.  The result is measured against the part
  (:mod:`kiln.mesh_edit_check`) and comes back as a mesh AND a STEP file
  beside it, so the next edit starts from CAD again.  Every selected edge
  that stayed sharp is listed with its reason.
* **An OpenSCAD design** -- a ``.scad`` file, or a mesh whose design recipe
  keeps its source.  The script is the part; a mesh patched after the fact is
  lost the next time the script is rebuilt.  The reply names the script, and
  the mesh is not touched.
* **A mesh with nothing behind it.**  Refused, in a sentence.  Measured
  2026-10-01: rounding or bevelling on the mesh left an open surface on every
  part tried, a plain 20 mm cube included, and made the part bigger.  Until a
  mesh route measures as clean as the CAD one there is none.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiln.edge_plan import CHAMFER, FILLET, Choice, EdgePlan, PrintFrame, plan_edges
from kiln.mesh_edit_check import EDIT_REFUSED, TOO_LARGE_TO_CHECK, guarded_edit

#: The part is a mesh with no CAD file or script behind it.
NEEDS_CAD = "EDGE_FINISH_NEEDS_CAD"
#: The part has an OpenSCAD script; its edges are finished there.
IN_THE_SCRIPT = "EDGE_FINISH_IN_SCRIPT"
#: Every selected edge stayed sharp; the reply lists why.
NOTHING_FINISHED = "NO_EDGE_FINISHED"
#: The CAD kernel is not installed on this machine.
NO_KERNEL = "CAD_KERNEL_MISSING"

_VERB = {FILLET: "round", CHAMFER: "bevel"}
_DONE = {FILLET: "rounded", CHAMFER: "bevelled"}
_SUFFIX = {FILLET: "filleted", CHAMFER: "chamfered"}

_INSTEAD_FROM_CAD = "Finish these edges in the CAD itself, or ask for a smaller size or fewer edges."


def print_frame(
    printer_id: str | None = None,
    *,
    nozzle_mm: float | None = None,
    layer_height_mm: float | None = None,
    material: str | None = None,
) -> tuple[PrintFrame, dict[str, Any]]:
    """The printer a plan is sized for, and where each figure came from.

    The nozzle is :func:`kiln.assumed_nozzle.assumed_nozzle`'s answer.  The
    layer height is the one stated, else the bundled slicing profile's for
    that printer, else Kiln's default profile's.  The overhang limit is the
    printability analyzer's own for the material
    (:func:`kiln.printability._resolve_overhang_threshold`).
    """
    from kiln.assumed_nozzle import assumed_nozzle, stock_setting
    from kiln.design_intelligence import load_pro_overlay_or_empty
    from kiln.printability import _resolve_overhang_threshold
    from kiln.slicer_profiles import get_slicer_profile

    nozzle = assumed_nozzle(printer_id or None, stated=nozzle_mm, or_only_printer=True)

    def _layer(raw: Any) -> float | None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if 0 < value <= nozzle.diameter_mm else None

    layer, layer_from = _layer(layer_height_mm), "the layer height it was given"
    if layer is None and nozzle.printer_id:
        layer, layer_from = _layer(stock_setting(nozzle.printer_id, "layer_height")), f"the slicing profile for {nozzle.printer_id}"
    if layer is None:
        layer = _layer(get_slicer_profile("default").settings.get("layer_height"))
        layer_from = "Kiln's default slicing profile"
    if layer is None:  # a nozzle finer than the default profile's layers
        layer, layer_from = nozzle.diameter_mm / 2.0, "half the nozzle width"

    overhang = _resolve_overhang_threshold(None, material or None, load_pro_overlay_or_empty("printability_judgment"))
    frame = PrintFrame(nozzle_mm=nozzle.diameter_mm, layer_mm=layer, overhang_deg=overhang)
    said = {
        "nozzle": nozzle.to_dict(),
        "layer_height_mm": layer,
        "layer_height_from": layer_from,
        "overhang_limit_deg": overhang,
        "note": (
            f"{nozzle.sentence('Sized')} Layers of {layer:g} mm ({layer_from}); "
            f"overhangs to {overhang:g} degrees print without support"
            + (f" in {material}." if material else ".")
        ),
    }
    return frame, said


def fillet_part(file_path: str, *, radius_mm: float = 1.0, **options: Any) -> dict[str, Any]:
    """Round edges of *file_path*; see :func:`finish_part` for the options and the reply."""
    return finish_part(file_path, kind=FILLET, size_mm=radius_mm, **options)


def chamfer_part(file_path: str, *, distance_mm: float = 0.5, **options: Any) -> dict[str, Any]:
    """Bevel edges of *file_path*; see :func:`finish_part` for the options and the reply."""
    return finish_part(file_path, kind=CHAMFER, size_mm=distance_mm, **options)


def finish_part(
    file_path: str,
    *,
    kind: str,
    size_mm: float,
    edges: str | list[str] | None = None,
    angle_threshold_deg: float = 60.0,
    output_path: str | None = None,
    printer_id: str | None = None,
    nozzle_mm: float | None = None,
    layer_height_mm: float | None = None,
    material: str | None = None,
    plan_only: bool = False,
    choose: Callable[[list[dict[str, Any]], PrintFrame], dict[int, Choice]] | None = None,
) -> dict[str, Any]:
    """Round (``kind="fillet"``) or bevel (``"chamfer"``) the selected edges of *file_path*.

    *edges* selects them (:func:`kiln.edge_plan.parse_selector`); an edge
    counts as sharp when the surface turns at least *angle_threshold_deg*
    across it.  *printer_id*, *nozzle_mm*, *layer_height_mm* and *material*
    say what the part prints on (:func:`print_frame`).  *plan_only* returns
    the plan without building it.  *choose* may change the finish chain by
    chain (:func:`kiln.edge_plan.plan_edges`).

    Returns ``success`` with ``path`` (the mesh), ``step_path`` (the CAD
    beside it), ``edges`` (finished, left sharp with reasons, cautions),
    ``sized_for``, ``nozzle``, the exact ``volume_mm3`` and round ``holes_mm``
    before and after as the CAD has them, and the ``measured`` check of the
    mesh -- or ``success`` False with a ``code`` and a
    ``message`` that says what to do.  Raises ``ValueError`` for a size or a
    selection it cannot read.
    """
    from kiln.step_import import is_step_file, step_converted_from

    if not size_mm > 0:
        raise ValueError("the size must be above 0 mm")
    if not 0 < angle_threshold_deg < 180:
        raise ValueError("angle_threshold_deg must be between 0 and 180")
    if not os.path.isfile(file_path):
        raise ValueError(f"File not found: {file_path}")

    frame, sized_for = print_frame(printer_id, nozzle_mm=nozzle_mm, layer_height_mm=layer_height_mm, material=material)

    script = script_behind(file_path)
    if script is not None:
        return _in_the_script(kind, size_mm, script, frame, sized_for)
    source = file_path if is_step_file(file_path) else step_converted_from(file_path)
    if source is None:
        return {
            "success": False,
            "code": NEEDS_CAD,
            "message": (
                f"Kiln did not {_VERB[kind]} the edges: this is a mesh with no CAD file behind it. Kiln finishes edges on "
                "a part's CAD file, where each face is rebuilt exactly; doing it on a mesh left an open surface and a "
                "bigger part on every part measured, so there is no mesh route. Your file is unchanged. Pass the "
                "part's STEP file, or finish the edges in the design that made it."
            ),
        }
    return _on_the_cad(
        file_path, source, kind=kind, size_mm=size_mm, edges=edges, angle_threshold_deg=angle_threshold_deg,
        output_path=output_path, frame=frame, sized_for=sized_for, plan_only=plan_only, choose=choose,
    )


# ---------------------------------------------------------------------------
# The CAD route
# ---------------------------------------------------------------------------


def _on_the_cad(
    file_path: str,
    source: str,
    *,
    kind: str,
    size_mm: float,
    edges: str | list[str] | None,
    angle_threshold_deg: float,
    output_path: str | None,
    frame: PrintFrame,
    sized_for: dict[str, Any],
    plan_only: bool,
    choose: Callable[[list[dict[str, Any]], PrintFrame], dict[int, Choice]] | None,
) -> dict[str, Any]:
    from kiln.cad_edge import CadEdgeError, finish_step, survey_step
    from kiln.step_import import _ocp_available, ensure_mesh_path, install_help, is_step_file, keep_step_beside

    if not _ocp_available():
        return {
            "success": False,
            "code": NO_KERNEL,
            "message": f"Kiln did not {_VERB[kind]} the edges: the CAD kernel that does it is not installed. {install_help()}",
        }
    if output_path is None:
        base = Path(file_path)
        output_path = str(base.with_name(f"{base.stem}_{_SUFFIX[kind]}.stl"))
    if Path(output_path).suffix.lower() != ".stl":
        raise ValueError("output_path must end in .stl; the finished CAD is written beside it as .step")

    try:
        survey = survey_step(source)
    except CadEdgeError as exc:
        return {
            "success": False,
            "code": EDIT_REFUSED,
            "message": f"Kiln did not {_VERB[kind]} the edges: {exc}. Your file is unchanged.",
        }
    plan = plan_edges(
        survey, kind=kind, size_mm=size_mm, frame=frame, edges=edges, min_turn_deg=angle_threshold_deg, choose=choose,
    )
    common = {"method": "cad", "cad_source": source, "sized_for": sized_for, "nozzle": sized_for["nozzle"]}
    if plan_only:
        return {
            "success": True, "plan_only": True, **common, "edges": plan.to_dict(),
            "all_edges": [_edge_line(e, survey["box"][2]) for e in survey["edges"] if e["corner"] != "smooth"],
            "note": _plan_note(plan, kind),
        }
    if not plan.treatments:
        return {
            "success": False, "code": NOTHING_FINISHED, **common, "edges": plan.to_dict(),
            "message": f"Kiln did not {_VERB[kind]} any edge: {_why_none(plan)} Your file is unchanged.",
        }

    mesh_in = ensure_mesh_path(file_path)[0] if is_step_file(file_path) else file_path
    scratch_dir = tempfile.mkdtemp(prefix="kiln_edge_")
    scratch_step = os.path.join(scratch_dir, "finished.step")

    def build(scratch: str) -> dict[str, Any]:
        """The kernel's build, refused here when it cost the part a hole.

        Holes are judged on the CAD, where a hole is a face with a radius,
        not on the mesh: the mesh hole reader loses a hole whose rim was
        bevelled, and would call a sound lead-in a closed hole.
        """
        kernel = finish_step(source, scratch, treatments=plan.treatments, output_step=scratch_step)
        lost = list(kernel["holes_before_mm"])
        for size in kernel["holes_after_mm"]:
            if size in lost:
                lost.remove(size)
        if lost:
            raise CadEdgeError(
                "the result lost or resized " + ", ".join(f"the {d:g} mm hole" for d in lost)
            )
        return kernel

    try:
        try:
            reply = guarded_edit(
                mesh_in, output_path, build,
                edit=f"{_VERB[kind]} the edges", instead=_INSTEAD_FROM_CAD, judge_holes=False,
            )
        except CadEdgeError as exc:
            return {
                "success": False, "code": EDIT_REFUSED, **common, "edges": plan.to_dict(),
                "message": f"Kiln did not {_VERB[kind]} the edges: {exc}. Your file is unchanged. {_INSTEAD_FROM_CAD}",
            }
        kernel = reply.pop("engine", None) or {k: reply.pop(k) for k in list(reply) if k in _KERNEL_KEYS}
        _fold_in_dropped(plan, kernel)
        reply.update(common, edges=plan.to_dict())
        if kernel:
            reply["volume_mm3"] = {"before": kernel["volume_before_mm3"], "after": kernel["volume_after_mm3"]}
            reply["holes_mm"] = {"before": kernel["holes_before_mm"], "after": kernel["holes_after_mm"]}
        if not reply.get("success"):
            if reply.get("code") == TOO_LARGE_TO_CHECK and kernel:
                # Every rounded edge is thousands of small triangles; fewer
                # edges is a smaller mesh.
                reply["message"] += " Select fewer edges with 'edges' and it will be."
            return reply
        reply["step_path"], beside = keep_step_beside(scratch_step, output_path, source)
        reply["note"] = _done_note(plan, kind, reply, beside)
        return reply
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


_KERNEL_KEYS = frozenset({
    "applied", "dropped", "volume_before_mm3", "volume_after_mm3", "box_before", "box_after",
    "valid_solid", "holes_before_mm", "holes_after_mm", "step_written", "builds", "seconds",
})


def _fold_in_dropped(plan: EdgePlan, kernel: dict[str, Any] | None) -> None:
    """Move the chains the kernel could not build from the plan's finished list to its sharp one."""
    if not kernel:
        return
    for dropped in kernel.get("dropped", []):
        gone = set(dropped["edges"])
        for treatment in [t for t in plan.treatments if set(t["edges"]) == gone]:
            plan.treatments.remove(treatment)
            plan.left_sharp.append({
                "edges": treatment["edges"], "place": treatment["place"], "corner": treatment["corner"],
                "reason": f"the CAD kernel could not build it: {dropped['reason']}",
            })
        plan.cautions[:] = [c for c in plan.cautions if not set(c["edges"]) <= gone]


def _edge_line(edge: dict[str, Any], bed_z: float) -> dict[str, Any]:
    from kiln.edge_plan import place_of

    line = {
        "id": f"e{edge['id']}", "place": place_of(edge, bed_z), "corner": edge["corner"],
        "length_mm": edge["length_mm"], "at": edge["mid"], "turn_deg": edge["turn_deg"],
    }
    for key in ("hole_mm", "hole_edge", "post_mm"):
        if key in edge:
            line[key] = edge[key]
    return line


def _count(n: int, what: str) -> str:
    return f"{n} {what}" if n == 1 else f"{n} {what}s"


def _why_none(plan: EdgePlan) -> str:
    if not plan.left_sharp:
        return "no sharp edge matched the selection."
    reasons = sorted({s["reason"] for s in plan.left_sharp})
    return f"the {_count(len(plan.left_sharp), 'edge')} selected stayed sharp -- " + "; ".join(reasons) + "."


def _plan_note(plan: EdgePlan, kind: str) -> str:
    return (
        f"Plan only, nothing built: {_count(len(plan.treatments), 'edge chain')} would be {_DONE[kind]}, "
        f"{len(plan.left_sharp)} would stay sharp, {_count(len(plan.cautions), 'caution')}. "
        "all_edges lists every sharp edge with its id."
    )


def _done_note(plan: EdgePlan, kind: str, reply: dict[str, Any], beside: str) -> str:
    done = len(plan.treatments)
    finishes = {t["kind"] for t in plan.treatments}
    what = _DONE[kind] if finishes == {kind} else "finished"
    said = f"{_count(done, 'edge chain')} {what} on the CAD file {os.path.basename(reply['cad_source'])}."
    if plan.left_sharp:
        said += f" {_count(len(plan.left_sharp), 'selected edge')} stayed sharp; edges.left_sharp says why."
    if plan.cautions:
        said += f" {_count(len(plan.cautions), 'caution')} about how it prints; read edges.cautions."
    if reply.get("step_path"):
        said += f" The finished CAD is {os.path.basename(reply['step_path'])}, beside the mesh.{beside}"
    return said


# ---------------------------------------------------------------------------
# The script route
# ---------------------------------------------------------------------------


def script_behind(file_path: str) -> dict[str, Any] | None:
    """The OpenSCAD script *file_path* is made from, or ``None``.

    A ``.scad`` file is its own script.  A mesh is script-made when the
    design recipe in its folder keeps OpenSCAD source and lists this mesh
    among its parts (or lists none, the single-part case).
    """
    path = Path(file_path)
    if path.suffix.lower() == ".scad":
        return {"scad_path": str(path), "recipe": None}
    try:
        from kiln.design_rebuild import part_stl_path
        from kiln.design_recipe import find_recipe, load_recipe

        recipe_file = find_recipe(str(path.parent))
        if recipe_file is None:
            return None
        recipe = load_recipe(recipe_file)
        if not recipe.source_scad:
            return None
        parts = [part_stl_path(part, str(path.parent)) for part in recipe.parts]
        if parts and not any(os.path.exists(p) and os.path.samefile(p, path) for p in parts):
            return None
        return {"scad_path": None, "recipe": recipe_file}
    except Exception:  # noqa: BLE001 -- an unreadable recipe is no script
        return None


def _in_the_script(kind: str, size_mm: float, script: dict[str, Any], frame: PrintFrame, sized_for: dict[str, Any]) -> dict[str, Any]:
    """The refusal a script-made part gets: where its script is, and that the edit belongs there."""
    where = (
        f"its OpenSCAD file {os.path.basename(script['scad_path'])}" if script["scad_path"]
        else "the OpenSCAD source kept in its design recipe (rebuild_design recompiles it)"
    )
    what = "round" if kind == FILLET else "bevel"
    return {
        "success": False,
        "code": IN_THE_SCRIPT,
        "script": script,
        "sized_for": sized_for,
        "nozzle": sized_for["nozzle"],
        "message": (
            f"Kiln did not {_VERB[kind]} the edges on the mesh: this part is made from an OpenSCAD script, and the script "
            f"is the part -- a mesh changed after the fact is lost the next time it is rebuilt. Write the {size_mm:g} mm "
            f"{what} into {where}, then compile it again. Your files are unchanged. On this printer a {what} shows from "
            f"{frame.smallest_shown_mm('vertical'):g} mm on an edge that runs up the part and from "
            f"{frame.smallest_shown_mm('top'):g} mm on a level one."
        ),
    }


__all__ = [
    "IN_THE_SCRIPT",
    "NEEDS_CAD",
    "NOTHING_FINISHED",
    "NO_KERNEL",
    "chamfer_part",
    "fillet_part",
    "finish_part",
    "print_frame",
    "script_behind",
]
