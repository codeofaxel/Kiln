"""Rebuild a saved design from its recipe — the engine behind ``rebuild_design``.

A design recipe records what a design is MADE of: its part STLs, their
colors and filament slots, the slicer profile — and, when the design was
born parametric, the OpenSCAD source and the parameter values that
produced the geometry.  This module re-executes that build.

Two modes, and which one runs is a property of the recipe, not a flag:

* **Parametric** — the recipe carries ``source_scad``.  The parameters are
  applied to the source and the geometry is RECOMPILED.  This is the whole
  point of keeping the source: to make a part bigger you change the
  parameter and re-derive, so a 3mm wall stays 3mm and an M3 clearance
  hole stays an M3 clearance hole.  Scaling the mesh instead scales every
  feature with the body — a 3mm wall becomes 3.6mm, and a 3.4mm clearance
  hole becomes 4.08mm, which no longer holds an M3 screw the way it was
  dimensioned to (scale down instead and the screw stops fitting at all).
  Either way the fits are gone: the same object, quietly ruined.
* **Mesh** — no source, so the geometry on disk IS the design and the
  recorded meshes are re-sliced exactly as they are.

Every parameter is reported as applied, absent from the source, or skipped
with a reason: an edit this engine cannot honor is said out loud, never
silently dropped.

The pipeline calls Kiln's own engines (:mod:`kiln.slicer`,
:mod:`kiln.parametric`).  The one adapter-bound step is the Bambu 3MF
wrap; where no Bambu adapter is registered the artifact is the merged
G-code and the result says so rather than naming a file that was never
written.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

_COLOR_ALIASES: dict[str, str] = {
    "grey": "gray",
    "colour": "color",
}


def normalize_color(color: str) -> str:
    """Normalize a color string — strip whitespace, apply aliases."""
    color = color.strip().lower()
    return _COLOR_ALIASES.get(color, color)


# ---------------------------------------------------------------------------
# Recipe resolution
# ---------------------------------------------------------------------------


def resolve_recipe(recipe_path: str) -> tuple[str, str] | None:
    """Resolve a caller-supplied path to ``(recipe_file, design_dir)``.

    The ONE resolver every door calls.  Callers hand us either the design
    directory (the documented contract) or the recipe file itself;
    ``save_recipe`` wants the directory while ``load_recipe`` historically
    wanted the file, and letting each caller reconcile that asymmetry is
    exactly how two tools once shipped raising IsADirectoryError on every
    call while a third, twelve lines away, had been fixed.

    :returns: ``(absolute recipe file, absolute design directory)``, or
        ``None`` when no recipe exists at *recipe_path*.
    """
    from kiln.design_recipe import find_recipe

    if os.path.isdir(recipe_path):
        recipe_file = find_recipe(recipe_path)
        if recipe_file is None:
            return None
        return recipe_file, os.path.abspath(recipe_path)
    if os.path.isfile(recipe_path):
        recipe_file = os.path.abspath(recipe_path)
        return recipe_file, os.path.dirname(recipe_file)
    return None


def no_recipe_error(recipe_path: str) -> dict[str, Any]:
    """The shared 'nothing here' envelope, so every door words it alike."""
    return {
        "status": "error",
        "error": f"No recipe found at: {recipe_path}",
        "code": "RECIPE_NOT_FOUND",
    }


def load_recipe_or_error(recipe_path: str) -> tuple[Any, str] | dict[str, Any]:
    """Load the recipe behind *recipe_path*, or return the error envelope.

    :returns: ``(recipe, design_dir)`` on success, an error dict otherwise.
    """
    from kiln.design_recipe import load_recipe

    resolved = resolve_recipe(recipe_path)
    if resolved is None:
        return no_recipe_error(recipe_path)
    recipe_file, design_dir = resolved
    try:
        recipe = load_recipe(recipe_file)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to load recipe: {exc}",
            "code": "RECIPE_LOAD_ERROR",
        }
    return recipe, design_dir


# ---------------------------------------------------------------------------
# Pipeline — slice / merge / wrap
# ---------------------------------------------------------------------------


def part_stl_path(part: Any, design_dir: str) -> str:
    """A part's STL path, resolving a recipe-relative path against the design
    DIRECTORY (never the recipe file — joining onto the file was a real bug)."""
    stl_path = part.stl_path
    if not os.path.isabs(stl_path):
        stl_path = os.path.join(design_dir, stl_path)
    return stl_path


def slice_stl(stl_path: str, profile: str | None) -> str:
    """Slice one STL through the real slicing engine; return the gcode path.

    :raises RuntimeError: When the slicer fails or emits nothing — callers
        convert that to their own error envelope.
    """
    from kiln.slicer import slice_file

    result = slice_file(stl_path, profile=profile or None)
    if not getattr(result, "success", False) or not result.output_path:
        raise RuntimeError(
            getattr(result, "message", "") or "slicer produced no output"
        )
    return result.output_path


def merge_part_gcodes(recipe: Any, design_dir: str) -> str:
    """Merge the recipe's per-part gcode into one multi-tool gcode file.

    ``merge_order`` is an ORDER, never a filter: parts it names take its
    positions and parts it omits (a stale order after a rename, a hand
    edit) follow in recipe order rather than vanishing from the print — a
    dropped part is the quietest failure this pipeline can have, found at
    assembly rather than on screen.  Sorting the PARTS rather than walking
    the names also means a repeated name cannot print one part twice, and
    two parts sharing a name cannot collapse into one.

    Each part's tool index is its ``filament_slot``, falling back to list
    position so a recipe that never assigned slots still merges
    deterministically.

    :raises RuntimeError: When no part has gcode or the merge engine refuses.
    """
    from kiln.slicer import merge_multipart_gcode

    position = {name: i for i, name in enumerate(recipe.merge_order or [])}
    tail = len(position)
    indexed = sorted(
        enumerate(recipe.parts),
        key=lambda item: (position.get(item[1].name, tail + item[0]), item[0]),
    )
    entries: list[dict[str, Any]] = []
    for index, part in indexed:
        if not part.gcode_path:
            continue
        gcode = part.gcode_path
        if not os.path.isabs(gcode):
            gcode = os.path.join(design_dir, gcode)
        entries.append({
            "gcode_path": gcode,
            "tool_index": part.filament_slot if part.filament_slot is not None else index,
            "name": part.name,
        })
    if not entries:
        raise RuntimeError("no sliced parts to merge")
    merged = merge_multipart_gcode(entries)
    output_path = merged.get("output_path") if isinstance(merged, dict) else None
    if not output_path:
        raise RuntimeError("gcode merge produced no output")
    return output_path


def _looks_hex(color: str | None) -> bool:
    return bool(color) and color.startswith("#") and len(color) in (7, 9)


def wrap_or_gcode(
    gcode_path: str,
    recipe: Any,
    *,
    stl_path: str | None = None,
) -> dict[str, Any]:
    """Package *gcode_path* for printing, degrading honestly.

    Bambu printers need the G-code wrapped in a 3MF, and that wrap is
    adapter-bound (it writes the printer's own start/end sequences), so it
    can only run where a Bambu adapter is registered.  Where it is not, the
    artifact IS the merged G-code — printable everywhere else via
    ``upload_file`` — and the result says which of the two the caller got
    and why.  Reporting a 3MF that was never written, or silently renaming
    the gcode, are both lies this helper exists to prevent.

    :param stl_path: The single mesh behind this gcode, when there is one —
        the wrap renders it into the thumbnail shown on the printer's screen.
    """
    parts = list(recipe.parts or [])
    slots = {
        (p.filament_slot if p.filament_slot is not None else i)
        for i, p in enumerate(parts)
    }
    num_filaments = max(len(slots), 1)
    colors = [p.color for p in parts]
    wrap_kwargs: dict[str, Any] = {
        "gcode_path": gcode_path,
        "num_filaments": num_filaments,
    }
    if stl_path and os.path.isfile(stl_path):
        wrap_kwargs["stl_path"] = stl_path
    if parts and all(_looks_hex(c) for c in colors):
        wrap_kwargs["filament_colors"] = colors

    try:
        from kiln.server import wrap_gcode_as_3mf

        wrapped = wrap_gcode_as_3mf(**wrap_kwargs)
        if isinstance(wrapped, dict) and wrapped.get("output_path"):
            return {"output_3mf": wrapped["output_path"], "wrapped": True}
        if isinstance(wrapped, dict):
            err = wrapped.get("error")
            # The server's error envelope nests {code, message}; a bare
            # string is also possible.  Either way the note carries prose,
            # never a dict repr.
            if isinstance(err, dict):
                note = err.get("message") or err.get("code") or "3MF wrap failed"
            else:
                note = err or "3MF wrap unavailable"
        else:
            note = f"unexpected wrap result: {type(wrapped).__name__}"
    except Exception as exc:  # noqa: BLE001 — degrade, never derail
        note = f"3MF wrap unavailable: {exc}"

    return {
        "output_gcode": gcode_path,
        "wrapped": False,
        "wrap_note": (
            f"{note} — returning merged G-code instead; send it with "
            f"upload_file(), or rebuild on a machine with a Bambu printer "
            f"registered for a print-ready 3MF."
        ),
    }


# ---------------------------------------------------------------------------
# Parametric re-derivation
# ---------------------------------------------------------------------------


def _flat_parameter_values(recipe: Any) -> dict[str, Any]:
    """The recipe's caller-editable parameter map, flattened.

    Generator-born recipes nest the user-facing values under
    ``parameters["values"]`` beside bookkeeping like ``product_type`` and
    the brep plan; hand-built and version-rail recipes keep a flat dict.
    Bookkeeping keys are not SCAD parameters and never reach the source.
    """
    params = recipe.parameters if isinstance(recipe.parameters, dict) else {}
    values = params.get("values")
    if isinstance(values, dict):
        return values
    return {
        k: v for k, v in params.items()
        if k not in ("product_type", "brep")
    }


def _apply_parameters_to_scad(
    scad_code: str,
    values: dict[str, Any],
) -> tuple[str, dict[str, Any], list[str], dict[str, str]]:
    """Apply recipe parameter values onto the SCAD source, out loud.

    Every value lands in exactly one of three buckets — applied, absent
    from the source, or skipped with a reason — so an edit the rebuild
    cannot honor is REPORTED.  A predecessor of this code reported a
    successful "scale" while writing nothing at all; that is the failure
    this three-bucket split exists to make impossible.

    :returns: ``(updated_code, applied, not_in_source, skipped)``
    """
    from kiln.parametric import update_openscad_parameter

    applied: dict[str, Any] = {}
    not_in_source: list[str] = []
    skipped: dict[str, str] = {}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            skipped[name] = (
                "non-numeric — the parametric rebuild can only re-derive "
                "numeric parameters; edit source_scad directly for this one"
            )
            continue
        try:
            scad_code = update_openscad_parameter(scad_code, name, float(value))
            applied[name] = value
        except ValueError:
            not_in_source.append(name)
    return scad_code, applied, not_in_source, skipped


def _rebuild_parametric(recipe: Any, design_dir: str) -> dict[str, Any]:
    """Re-derive geometry from the recipe's OpenSCAD source, then rebuild.

    Apply the recipe's numeric parameters to the source, recompile, keep
    the fresh mesh as the design's primary STL, slice it, and package for
    print.  Because the geometry is re-derived rather than stretched, wall
    thicknesses and fastener holes come out exactly as parameterized.
    """
    from kiln.design_recipe import save_recipe
    from kiln.parametric import compile_scad_code

    values = _flat_parameter_values(recipe)
    scad_code, applied, not_in_source, skipped = _apply_parameters_to_scad(
        recipe.source_scad, values,
    )

    try:
        compiled_stl = compile_scad_code(scad_code)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"OpenSCAD re-derivation failed: {exc}",
            "code": "COMPILE_ERROR",
            "parameters_applied": applied,
            "parameters_not_in_source": not_in_source,
            "parameters_skipped": skipped,
        }

    try:
        gcode_path = slice_stl(compiled_stl, None)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Slicing failed for re-derived model: {exc}",
            "code": "SLICE_ERROR",
        }

    artifact = wrap_or_gcode(gcode_path, recipe, stl_path=compiled_stl)

    # Persist once, at the end, so a failed pipeline never half-writes the
    # recipe: the re-derived mesh becomes the primary STL, the updated
    # source records the parameters really applied, and the 3MF lands on
    # the recipe's own field.
    recipe.source_scad = scad_code
    recipe.stl_path = compiled_stl
    if artifact.get("output_3mf"):
        recipe.final_3mf = artifact["output_3mf"]
    try:
        save_recipe(recipe, design_dir)
    except Exception as exc:
        _logger.warning("Failed to save updated recipe: %s", exc)

    return {
        "status": "success",
        "mode": "parametric",
        "message": (
            f"Design re-derived from OpenSCAD source — "
            f"{len(applied)} parameter(s) applied, geometry recompiled, "
            f"sliced and packaged."
        ),
        "parameters_applied": applied,
        "parameters_not_in_source": not_in_source,
        "parameters_skipped": skipped,
        "compiled_stl": recipe.stl_path,
        **artifact,
    }


def _rebuild_mesh(recipe: Any, design_dir: str) -> dict[str, Any]:
    """Re-slice the recipe's recorded meshes exactly as they are.

    No parametric source, so the geometry on disk IS the design.  Slices
    every part (or the primary STL for a part-less recipe), merges
    multi-part gcode, and packages for print.
    """
    from kiln.design_recipe import save_recipe

    part_results: list[dict[str, Any]] = []
    if recipe.parts:
        for part in recipe.parts:
            stl_path = part_stl_path(part, design_dir)
            if not os.path.exists(stl_path):
                return {
                    "status": "error",
                    "error": f"STL not found for part '{part.name}': {stl_path}",
                    "code": "FILE_NOT_FOUND",
                }
            try:
                part.gcode_path = slice_stl(stl_path, part.slicer_profile)
            except Exception as exc:
                return {
                    "status": "error",
                    "error": f"Slicing failed for part '{part.name}': {exc}",
                    "code": "SLICE_ERROR",
                }
            part_results.append({
                "part": part.name,
                "color": part.color,
                "filament_slot": part.filament_slot,
                "gcode_path": part.gcode_path,
            })

        if len(recipe.parts) == 1:
            merged_gcode = recipe.parts[0].gcode_path
        else:
            try:
                merged_gcode = merge_part_gcodes(recipe, design_dir)
            except Exception as exc:
                return {
                    "status": "error",
                    "error": f"Gcode merge failed: {exc}",
                    "code": "MERGE_ERROR",
                }
    else:
        stl_path = recipe.stl_path
        if not stl_path or not os.path.exists(stl_path):
            return {
                "status": "error",
                "error": (
                    "Nothing to rebuild: the recipe has no parts, no primary "
                    "STL on disk, and no OpenSCAD source to re-derive from."
                ),
                "code": "REBUILD_EMPTY",
            }
        try:
            merged_gcode = slice_stl(stl_path, None)
        except Exception as exc:
            return {
                "status": "error",
                "error": f"Slicing failed: {exc}",
                "code": "SLICE_ERROR",
            }

    # A single mesh (a lone part, or the primary STL) can also become the
    # printer-screen thumbnail; a multi-part merge has no one mesh to show.
    single_stl: str | None = None
    if recipe.parts and len(recipe.parts) == 1:
        single_stl = part_stl_path(recipe.parts[0], design_dir)
    elif not recipe.parts:
        single_stl = recipe.stl_path
    artifact = wrap_or_gcode(merged_gcode, recipe, stl_path=single_stl)

    if artifact.get("output_3mf"):
        recipe.final_3mf = artifact["output_3mf"]
    try:
        save_recipe(recipe, design_dir)
    except Exception as exc:
        _logger.warning("Failed to save updated recipe: %s", exc)

    n = len(part_results) or 1
    result = {
        "status": "success",
        "mode": "mesh",
        "message": f"Design rebuilt — {n} part(s) sliced and packaged.",
        "parts": part_results,
        **artifact,
    }
    if recipe.source_scad and recipe.parts:
        # Both shapes present: the parts are meshes born from steps this
        # pipeline cannot replay (splits, decorations), so they were
        # re-sliced as they are.  Say so — a parameter edit does NOT reach
        # this build, and silence here is exactly the lie the parametric
        # mode exists to end.
        result["note"] = (
            "This recipe also carries OpenSCAD source, but its part meshes "
            "were derived by steps rebuild cannot replay, so they were "
            "re-sliced unchanged. Parameter changes do not reach this "
            "build; re-derive the base model with update_scad_parameter + "
            "compile_scad, then re-split."
        )
    return result


def apply_brief_to_recipe(recipe_path: str, brief_id: str) -> None:
    """Persist *brief_id* on the recipe at *recipe_path* before a rebuild.

    Surfaces a caller-supplied saved-goal id onto the recipe so the rebuild
    output carries the goal in its provenance.  ``brief_id`` is a recipe
    field (see :class:`kiln.design_recipe.DesignRecipe`); resolving what it
    POINTS AT is kiln-pro's design_session, and nothing here needs it.

    An empty ``brief_id`` is a no-op, so the recipe's existing goal is
    preserved and iterating a brief-attached design keeps it automatically.

    Best-effort by design: a missing recipe, an unloadable one, or any IO
    failure logs at DEBUG and returns.  The rebuild is the user's actual
    intent — never derail it over an annotation.
    """
    if not brief_id:
        return
    try:
        from kiln.design_recipe import load_recipe, save_recipe

        resolved = resolve_recipe(recipe_path)
        if resolved is None:
            _logger.debug(
                "apply_brief_to_recipe: no recipe at %s; skipped", recipe_path,
            )
            return
        recipe_file, design_dir = resolved
        recipe = load_recipe(recipe_file)
        recipe.brief_id = brief_id
        save_recipe(recipe, design_dir)
    except Exception:
        _logger.debug(
            "apply_brief_to_recipe: best-effort brief attach failed",
            exc_info=True,
        )


def rebuild_design_from_recipe(recipe_path: str) -> dict[str, Any]:
    """Re-execute the full build pipeline from a saved recipe.

    Parametric recipes (those carrying OpenSCAD source and no split parts)
    re-derive their geometry from source + parameters; every other recipe
    re-slices its recorded meshes.  The result names which mode ran.

    :param recipe_path: The design directory, or the recipe file itself.
    :returns: Dict with the print artifact, the mode used, and per-part or
        per-parameter detail.
    """
    loaded = load_recipe_or_error(recipe_path)
    if isinstance(loaded, dict):
        return loaded
    recipe, design_dir = loaded

    if recipe.source_scad and not recipe.parts:
        return _rebuild_parametric(recipe, design_dir)
    return _rebuild_mesh(recipe, design_dir)
