"""Slicer tools plugin.

Extracts slicer-domain MCP tools from server.py into a focused plugin
module.  All tools delegate to helpers and singletons defined in
server.py via lazy ``import kiln.server as _srv``.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from kiln import plate_state as _plate_state
from kiln.plate_state import pretty_job_name as _pretty_job_name
from kiln.print_start_verdict import resolve_print_start
from kiln.tool_args import parse_json_object
from kiln.tool_results import unwrap_tool_result

_logger = logging.getLogger(__name__)


def _try_orient_to_fit(input_path: str, printer_id: str) -> str | None:
    """Rotate an oversized STL to try to make it fit the bed (free + local).

    Returns the path to a rotated temp copy that fits, or None if no tried
    orientation fits.  Uses the PUBLIC orientation helper + the printer's
    datasheet bed size only (no curated SME).  We try the two axis-aligned
    re-orientations that change the bounding box (lay the part on its other
    faces) — this rescues the common "modelled tall, fits lying down" /
    "long-in-X fits long-in-Y" cases without disturbing a part already fine.
    """
    try:
        from kiln.auto_orient import apply_orientation
        from kiln.printers.bed_fit import validate_mesh_for_printer
    except Exception:  # noqa: BLE001
        return None
    import tempfile

    stem = os.path.splitext(os.path.basename(input_path))[0]
    for rx, ry, rz in ((90.0, 0.0, 0.0), (0.0, 90.0, 0.0)):
        try:
            tmp_dir = tempfile.mkdtemp(prefix="kiln_orient_")
            oriented = os.path.join(tmp_dir, f"{stem}_oriented.stl")
            apply_orientation(input_path, rx, ry, rz, output_path=oriented)
            if validate_mesh_for_printer(oriented, printer_id).get("ok"):
                _logger.info(
                    "Auto-oriented %s (rot %g/%g/%g) to fit the %s bed.",
                    os.path.basename(input_path), rx, ry, rz, printer_id,
                )
                return oriented
        except Exception:  # noqa: BLE001
            _logger.debug("orient-to-fit candidate failed", exc_info=True)
    return None


def _material_temp_block(
    printer_id: str | None, material_id: str | None,
) -> dict | None:
    """Bed-fit-gate-shaped temp-ceiling block dict, or None.

    Delegates to the print_gate single-source-of-truth check (which reads only
    PUBLIC datasheet/safety-floor data — printer rated max-temp + material
    safety-floor range, never the curated SME) and maps the verdict to this
    gate's ``{error_code, error_message}`` shape.
    """
    if not material_id:
        return None
    try:
        from kiln.printers.print_gate import check_material_temp

        v = check_material_temp(printer_id, material_id)
    except Exception:  # noqa: BLE001
        return None
    if v is None:
        return None
    return {
        "error_code": v["code"],
        "error_message": (v["reason"] + " " + v.get("override_hint", "")).strip(),
        # Carry the Pro+ enrichment (material swap) the gate attached, so the
        # slice-layer block surfaces "here's what to print it in instead".
        "enrichment": v.get("enrichment"),
    }


def _attach_fit_enrichment(
    fit: dict,
    input_path: str,
    printer_id: str | None,
    material_id: str | None,
) -> dict:
    """Attach Pro+ enrichment to an EXCEEDS_BED bed-fit block, in place.

    No-op for any other error code (OFF_BED is auto-centerable, not a split
    case).  The enrichment (real usable envelope + split plan) lives in
    kiln-pro; this just hands the gate's enricher the bbox the bed-fit
    validator already computed.  Never raises.
    """
    if not isinstance(fit, dict) or fit.get("error_code") != "EXCEEDS_BED":
        return fit
    try:
        from kiln.printers.print_gate import _maybe_enrich_block

        enrichment = _maybe_enrich_block(
            {"blocked": True, "code": "EXCEEDS_BED", "fit": fit},
            job_path=input_path,
            printer_id=printer_id,
            material_id=material_id,
        )
        if enrichment is not None:
            fit["enrichment"] = enrichment
    except Exception:  # noqa: BLE001 — enrichment must never break slicing
        pass
    return fit


def _gate_error_response(gate_err: dict) -> dict:
    """Build the slice tool's error response, carrying any Pro+ enrichment.

    Single chokepoint for the three slice-tool bed-fit-gate callsites so the
    enrichment block (when present) rides out alongside the standard error,
    without each callsite re-implementing the merge.
    """
    from kiln.server import _error_dict

    resp = _error_dict(
        gate_err.get("error_message", "Bed-fit check failed."),
        code=gate_err.get("error_code", "BED_FIT_ERROR"),
    )
    enrichment = gate_err.get("enrichment")
    if enrichment:
        resp["enrichment"] = enrichment
    return resp


def _apply_bed_fit_gate(
    input_path: str,
    effective_printer_id: str | None,
    auto_center: bool,
    material_id: str | None = None,
) -> tuple[str, dict | None, dict]:
    """Pre-slice safety gate: verify the mesh fits within the printer's
    build volume and hasn't been placed off-bed (origin-centered
    geometry crashed a Bambu A1 nozzle into the purge tool on 2026-04-15
    — incident #0).

    When ``auto_center=True`` (default) and the mesh is off-bed but
    physically fits, we translate it to a bed-centered copy in a temp
    directory and return that path.  The original file is not modified.

    When the mesh exceeds the build volume, we return an error dict
    even with ``auto_center=True`` — translation can't fix that.

    Returns ``(effective_input_path, error_dict_or_None, bed_fit_info)``.
    The caller uses ``effective_input_path`` for slicing.  If
    ``error_dict_or_None`` is not None, the caller should return it
    immediately instead of slicing.
    """
    from kiln.printers.bed_fit import (
        apply_translation_to_stl,
        validate_mesh_for_printer,
    )

    if not effective_printer_id:
        # No printer context — skip the gate.  The caller probably knows
        # what they're doing (e.g. generic slice without a target printer).
        return input_path, None, {"gate": "skipped_no_printer"}

    # Material temperature ceiling — independent of fit.  Refuse a material the
    # printer's hotend physically cannot reach (e.g. PC's 270C floor on a 260C
    # Ender).  Authoritative here because the material is resolved at slice time.
    temp_block = _material_temp_block(effective_printer_id, material_id)
    if temp_block is not None:
        return input_path, temp_block, temp_block

    if not input_path.lower().endswith(".stl"):
        # Only validate + translate STLs for now.  3MF/STEP/OBJ are out
        # of scope for the translate path — we'd need format-specific
        # rewriters.  Still run a bbox validation but can't auto-fix.
        fit = validate_mesh_for_printer(input_path, effective_printer_id)
        fit["approval_carries"] = True  # nothing on this branch moves the mesh
        if not fit["ok"] and fit["error_code"] in ("OFF_BED_GEOMETRY", "EXCEEDS_BED"):
            _attach_fit_enrichment(fit, input_path, effective_printer_id, material_id)
            return input_path, fit, fit
        return input_path, None, fit

    fit = validate_mesh_for_printer(input_path, effective_printer_id)
    # Whether a yes given on the DESIGN mesh still describes what will print.
    # The gate slices a moved or rotated COPY when it has to, and the ledger
    # records the copy — so the design's approval silently never reached the
    # print file.  Silent was the bug: the two transforms below say so, and
    # by how much, and the stage on the slice result shows the plate as it
    # will print so that is what gets approved.
    fit["approval_carries"] = True
    if fit["ok"]:
        return input_path, None, fit
    if fit["error_code"] == "EXCEEDS_BED":
        # Auto-orient before giving up: a part too tall/wide as-modelled often
        # fits once rotated. (Free: public orientation helper + datasheet bed.)
        oriented = _try_orient_to_fit(input_path, effective_printer_id)
        if oriented is not None:
            ofit = validate_mesh_for_printer(oriented, effective_printer_id)
            ofit["auto_oriented"] = True
            ofit["oriented_input_path"] = oriented
            ofit["approval_carries"] = False
            ofit["approval_note"] = (
                "rotated to fit the bed, so a yes given on the design mesh "
                "does not carry; the stage on this result shows the plate as "
                "it will print — approve from here"
            )
            return oriented, None, ofit
        _attach_fit_enrichment(fit, input_path, effective_printer_id, material_id)
        return input_path, fit, fit
    if fit["error_code"] == "OFF_BED_GEOMETRY":
        if auto_center and fit.get("suggested_translate"):
            # Auto-center: translate STL into a temp copy and use it.
            import tempfile
            stem = os.path.splitext(os.path.basename(input_path))[0]
            tmp_dir = tempfile.mkdtemp(prefix="kiln_bedfit_")
            centered_path = os.path.join(tmp_dir, f"{stem}_bedcentered.stl")
            try:
                apply_translation_to_stl(
                    input_path, fit["suggested_translate"], centered_path,
                )
                _logger.info(
                    "Auto-centered off-bed mesh for %s: translate %s -> %s",
                    effective_printer_id, fit["suggested_translate"],
                    centered_path,
                )
                fit["auto_centered"] = True
                fit["centered_input_path"] = centered_path
                fit["approval_carries"] = False
                _dx, _dy = (list(fit["suggested_translate"]) + [0.0, 0.0])[:2]
                fit["approval_note"] = (
                    f"moved {math.hypot(float(_dx), float(_dy)):.0f} mm to fit "
                    "the bed, so a yes given on the design mesh does not carry; "
                    "the stage on this result shows the plate as it will print "
                    "— approve from here"
                )
                return centered_path, None, fit
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Auto-center failed: %s", exc)
                fit["error_message"] = (
                    f"{fit['error_message']} "
                    f"(auto-center also failed: {exc})"
                )
                return input_path, fit, fit
        # auto_center disabled or translation path unavailable — block.
        return input_path, fit, fit
    # Unknown warn-only states (BBOX_UNKNOWN, VOLUME_UNKNOWN) — pass through.
    return input_path, None, fit


# ---------------------------------------------------------------------------
# Placement on an occupied plate
# ---------------------------------------------------------------------------
#
# A slice used to move the new part to the centre of the plate whatever was
# already there -- onto the part the last print left behind.  The plate
# record (kiln.plate_state) says when that part is still there; from here
# every slice door takes a ``placement`` and, on an occupied plate, refuses
# until a spot is named, asks kiln-pro whether that spot is safe
# (kiln._pro_placement_bridge), moves the part there in a temp copy, slices,
# and looks at the sliced file once more before it is handed on.  Nothing
# here computes clearance: the verdict is the engine's; the doors, the
# translation and every refusal's wording are public Kiln's.
#
# Two failure directions, on purpose.  An OCCUPIED plate with no verdict
# fails CLOSED: nothing is sliced onto it.  An UNRECORDED plate (status
# ``unknown``) fails OPEN at the door: it slices exactly as it always did.
# That is defensible because the record is written only by Kiln's own starts
# and its print-ended hook -- "no record" means Kiln never put a part there,
# not that a part might be there -- and a person's own manual print is
# theirs to clear.

_PLACEMENT_TIER_NOTE = (
    "The clearance verdict is free; placing and starting a second print on an "
    "occupied plate is a kiln-pro feature (https://kiln3d.com/pricing)."
)
#: The note where the verdict does not say a quiet start would stand on this
#: plate: no tier is named on a plate where paying would start nothing.
_PLACEMENT_FREE_NOTE = "The clearance verdict is free on every tier."


def _quiet_start_would_stand(verdict: Any) -> bool:
    """Whether the verdict says a print could start beside what is on this
    plate on the plan's tier (``record.quiet_start``).  A verdict that does
    not say -- no verdict, no record, a service older than the field --
    reads as no: a tier is named only where it would start the print."""
    record = verdict.get("record") if isinstance(verdict, dict) else None
    return isinstance(record, dict) and record.get("quiet_start") is True


#: The nine regions a person can name: thirds of the bed in X and Y.  Front
#: is low Y and left is low X, the way the printer's own screen draws the
#: plate.  ``center`` spellings are accepted too.
_REGION_ROWS = ("front", "middle", "back")
_REGION_COLUMNS = ("left", "centre", "right")
PLACEMENT_REGIONS = tuple(
    "centre" if (r, c) == (1, 1) else f"{_REGION_ROWS[r]}-{_REGION_COLUMNS[c]}"
    for r in range(3)
    for c in range(3)
)
_PROFILE_NUMBER_KEYS = ("layer_height", "skirts", "skirt_distance", "brim_width")
_PLACEABLE_EXTENSIONS = (".stl", ".3mf")


def _plate_holds_sentence(state: Any) -> str:
    """The record's own opening (:meth:`kiln.plate_state.PlateState.holds_sentence`)."""
    return state.holds_sentence()


def _no_verdict_sentence(state: Any, miss: Any) -> str:
    """The fail-closed refusal, in the shared voice of every served door.

    What is on the line is the plate record's own opening; the cause and
    the fix are :func:`kiln.served_answer.sentence`'s, so this door says
    "this computer is offline" in exactly the words the motion and blade
    doors say it, and a refusal in the server's own words is appended
    whole.  *miss* is the bridge's :class:`~kiln.served_answer.Miss`; a
    bare cause string or nothing at all reads as "didn't answer".
    """
    from kiln import served_answer

    if not isinstance(miss, served_answer.Miss):
        cause = miss if isinstance(miss, str) and miss in served_answer.CAUSES else "unanswered"
        miss = served_answer.Miss(cause)
    return served_answer.sentence(
        miss,
        feature="clearance check",
        on_the_line=_plate_holds_sentence(state),
        cannot="check whether a second part fits safely beside it",
        wont="won't slice onto this plate",
        safe_remedy="clear the plate and say so",
    )


def _region_cell(name: str) -> tuple[int, int] | None:
    """``"back-left"`` -> ``(2, 0)``; ``None`` for anything that is not a region."""
    n = name.strip().lower().replace("_", "-").replace(" ", "-").replace("center", "centre")
    if n in ("centre", "middle", "middle-centre", "centre-centre"):
        return (1, 1)
    parts = n.split("-")
    if len(parts) != 2 or parts[0] not in _REGION_ROWS or parts[1] not in _REGION_COLUMNS:
        return None
    return (_REGION_ROWS.index(parts[0]), _REGION_COLUMNS.index(parts[1]))


def _region_name(cell: tuple[int, int]) -> str:
    return PLACEMENT_REGIONS[cell[0] * 3 + cell[1]]


def _cell_of(x: float, y: float, bed: list[float]) -> tuple[int, int]:
    """Which third-by-third region of *bed* the point ``(x, y)`` falls in."""
    col = min(2, max(0, int(x // (float(bed[0]) / 3.0))))
    row = min(2, max(0, int(y // (float(bed[1]) / 3.0))))
    return (row, col)


def _spot_centre(spot: dict[str, Any], part: dict[str, Any] | None) -> tuple[float, float] | None:
    """The footprint centre of a verdict spot: its rect when it names one,
    else its origin plus half the part's size (``at_mm`` is the min corner)."""
    rect = spot.get("footprint_mm")
    if isinstance(rect, (list, tuple)) and len(rect) == 4:
        return ((float(rect[0]) + float(rect[2])) / 2.0, (float(rect[1]) + float(rect[3])) / 2.0)
    at = spot.get("at_mm")
    if not (isinstance(at, (list, tuple)) and len(at) == 2):
        return None
    size = (part or {}).get("size_mm") or [0.0, 0.0]
    return (float(at[0]) + float(size[0]) / 2.0, float(at[1]) + float(size[1]) / 2.0)


def _regions_with_room(spots: list[dict[str, Any]], part: dict[str, Any] | None, bed: list[float]) -> list[str]:
    cells: set[tuple[int, int]] = set()
    for spot in spots:
        centre = _spot_centre(spot, part)
        if centre is not None:
            cells.add(_cell_of(centre[0], centre[1], bed))
    return [_region_name(cell) for cell in sorted(cells)]


def _parse_placement(placement: Any) -> tuple[str, Any, str | None]:
    """``(kind, value, problem)`` -- kind is ``auto`` / ``keep`` / ``spot`` / ``region``.

    A JSON-encoded ``"[x, y]"`` is read as a spot, since a host may hand a
    list through as its text.  *problem* is the sentence for anything else.
    """
    accepted = ", ".join(PLACEMENT_REGIONS)
    if placement is None:
        return "auto", "auto", None
    if isinstance(placement, str):
        text = placement.strip()
        if text.startswith("["):
            try:
                placement = json.loads(text)
            except ValueError:
                return "auto", None, f"placement {placement!r} is not a spot; give [x, y] in mm"
        else:
            low = text.lower()
            if low in ("", "auto"):
                return "auto", "auto", None
            if low == "keep":
                return "keep", "keep", None
            cell = _region_cell(low)
            if cell is not None:
                return "region", cell, None
            return "auto", None, f'placement {placement!r} is not a spot ([x, y] in mm), "keep", or a region ({accepted})'
    if isinstance(placement, (list, tuple)) and len(placement) == 2:
        try:
            return "spot", [float(placement[0]), float(placement[1])], None
        except (TypeError, ValueError):
            pass
    return "auto", None, f'placement {placement!r} is not a spot ([x, y] in mm), "keep", or a region ({accepted})'


def _profile_numbers(profile_path: str | None) -> dict[str, float]:
    """``layer_height`` / ``skirts`` / ``skirt_distance`` / ``brim_width`` from a
    slicer profile, when they are trivially readable; ``{}`` otherwise."""
    out: dict[str, float] = {}
    if not profile_path:
        return out
    try:
        text = Path(profile_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    if str(profile_path).lower().endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError:
            return out
        for key in _PROFILE_NUMBER_KEYS:
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, list) and value:
                value = value[0]
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
        return out
    for key in _PROFILE_NUMBER_KEYS:
        m = re.search(rf"^\s*{key}\s*=\s*([-+]?\d+(?:\.\d+)?)", text, re.MULTILINE)
        if m:
            out[key] = float(m.group(1))
    return out


def _part_envelope(input_path: str, profile_path: str | None) -> tuple[dict[str, Any] | None, dict[str, float] | None]:
    """``(part, bbox)`` for the request: the part's size from its bbox, the
    layer height and the widest thing the slicer draws around it (skirt or
    brim) from the profile -- the slicer's own defaults when the profile is
    silent -- and a tower when the 3MF asks for more than one filament.
    Colour changes are unknown before slicing.  ``(None, None)`` when the
    geometry cannot be read."""
    from kiln.printers.bed_fit import compute_mesh_bbox

    try:
        bbox = compute_mesh_bbox(input_path)
    except Exception:  # noqa: BLE001 -- unreadable geometry is "no envelope"
        bbox = None
    if not bbox:
        return None, None
    numbers = _profile_numbers(profile_path)
    skirts = numbers.get("skirts", 1.0)
    skirt_distance = numbers.get("skirt_distance", 6.0)
    skirt = max(numbers.get("brim_width", 0.0), skirt_distance if skirts > 0 else 0.0)
    tower: list[float] | None = None
    if input_path.lower().endswith(".3mf"):
        try:
            tower = [30.0, 30.0] if _detect_3mf_multicolor(input_path) else None
        except Exception:  # noqa: BLE001
            tower = None
    part = {
        "size_mm": [
            round(bbox["x_max"] - bbox["x_min"], 3),
            round(bbox["y_max"] - bbox["y_min"], 3),
            round(bbox["z_max"] - bbox["z_min"], 3),
        ],
        "layer_height_mm": numbers.get("layer_height", 0.2),
        "tower_mm": tower,
        "colour_changes_at_mm": [],
        "skirt_mm": skirt,
    }
    return part, bbox


def _place_copy(input_path: str, bbox: dict[str, float], target_min: list[float]) -> tuple[str | None, str | None]:
    """A temp copy of *input_path* with its footprint origin at *target_min*,
    or ``(None, sentence)`` for a format Kiln cannot move without re-exporting."""
    import tempfile

    ext = os.path.splitext(input_path)[1].lower()
    if ext not in _PLACEABLE_EXTENSIONS:
        return None, (
            f"Kiln can place an STL or a 3MF beside a part on the plate, not {ext or 'this format'}; "
            "convert it first (import_external_mesh) or clear the plate and say so."
        )
    dx = float(target_min[0]) - float(bbox["x_min"])
    dy = float(target_min[1]) - float(bbox["y_min"])
    stem = os.path.splitext(os.path.basename(input_path))[0]
    dst = os.path.join(tempfile.mkdtemp(prefix="kiln_placement_"), f"{stem}_placed{ext}")
    if ext == ".stl":
        from kiln.printers.bed_fit import apply_translation_to_stl

        apply_translation_to_stl(input_path, [dx, dy, 0.0], dst)
    else:
        from kiln.threemf_placement import translate_3mf

        translate_3mf(input_path, dx, dy, dst)
    return dst, None


def _spots_clause(verdict: dict[str, Any] | list[dict[str, Any]] | None) -> str:
    """What the refusal says about room beside the part.

    With the places in hand (the plan's own tier), the best few are named.
    Without them, the COUNT still is -- the verdict carries ``spots_found``
    on every tier -- so a person is told room exists, rather than being
    handed a list they cannot print at.  The plan's tier is named beside
    the count only where the verdict says a print could start there on it
    (:func:`_quiet_start_would_stand`); elsewhere the count is said plainly,
    because paying would buy no start on that plate.
    """
    if isinstance(verdict, list):
        verdict = {"spots": verdict, "spots_found": len(verdict)}
    if not isinstance(verdict, dict):
        return ""
    spots = verdict.get("spots") or []
    named: list[str] = []
    for spot in spots[:3]:
        at = spot.get("at_mm") if isinstance(spot, dict) else None
        if isinstance(at, (list, tuple)) and len(at) == 2:
            clear = spot.get("clearance_mm")
            named.append(
                f"[{float(at[0]):g}, {float(at[1]):g}]"
                + (f" ({float(clear):g} mm clear)" if isinstance(clear, (int, float)) else "")
            )
    if named:
        return f" Spots with room: {', '.join(named)}."
    found = verdict.get("spots_found")
    if isinstance(found, int) and found > 0:
        count = f" {found} spot{'s' if found != 1 else ''} beside it would fit"
        if _quiet_start_would_stand(verdict):
            return count + "; printing around what is on the plate is a kiln-pro feature (https://kiln3d.com/pricing)."
        return count + "."
    return ""


def _refusal_sentences(verdict: dict[str, Any]) -> str:
    sentences = [
        str(r.get("sentence") or "").strip()
        for r in (verdict.get("refusals") or [])
        if isinstance(r, dict) and r.get("sentence")
    ]
    return " ".join(s if s.endswith(".") else s + "." for s in sentences) or "the engine named no safe way to place it there."


def _placement_refusal(
    message: str, code: str, *, state: Any, bed: list[float] | None, verdict: dict[str, Any] | None = None,
    miss: Any = None,
) -> dict[str, Any]:
    """The error dict every placement refusal shares: the record, the spots
    that would work, the plate as the engine (or the record) sees it, and
    -- when the refusal is a miss -- the shared voice's ``why`` fields
    beside the sentence (never inside it)."""
    from kiln import served_answer
    from kiln.server import _error_dict

    resp = _error_dict(message, code=code)
    if isinstance(miss, served_answer.Miss):
        resp.update(served_answer.fields(miss))
    resp["plate"] = state.to_dict()
    resp["spots"] = list(verdict.get("spots") or []) if isinstance(verdict, dict) else []
    resp["occupancy"] = (verdict.get("occupancy") if isinstance(verdict, dict) else None) or state.occupancy(bed)
    if isinstance(verdict, dict):
        resp["placement"] = verdict
    resp["regions"] = list(PLACEMENT_REGIONS)
    resp["tier_note"] = _PLACEMENT_TIER_NOTE if _quiet_start_would_stand(verdict) else _PLACEMENT_FREE_NOTE
    return resp


def _apply_plate_placement(
    input_path: str,
    *,
    effective_printer_id: str | None,
    printer_name: str | None,
    placement: Any,
    profile_path: str | None = None,
    adapter: Any | None = None,
) -> tuple[str, dict | None, dict]:
    """Pre-slice gate for a plate that still holds the last print.

    Same return shape as :func:`_apply_bed_fit_gate` --
    ``(effective_input_path, error_dict_or_None, info)`` -- and called by
    every door that reaches the slicer BEFORE it, so a part is placed
    beside the occupant first and checked against the bed second.  A door
    that already holds the target's *adapter* passes it; otherwise the
    machine is resolved from *printer_name* the way every tool does.

    * plate ``clear`` or ``unknown`` (no printer, no record, the hosted
      process): the input passes through unchanged, ``info["plate"]`` says
      which.  ``unknown`` fails OPEN here, deliberately: the record is
      written only by Kiln's own starts and print-ended hook, so no record
      means Kiln never put a part on this plate -- not that one might be
      there -- and a manual print of the person's own is theirs to clear;
    * plate ``occupied`` and no *placement*: refuse
      (``PLACEMENT_PLATE_OCCUPIED``) with what is there and, when a verdict
      is obtainable, the spots that would work;
    * a spot ``[x, y]``, ``"keep"`` or a named region: ask the bridge.  A
      region is resolved here, never in the engine -- the spots of an
      ``"auto"`` verdict whose footprint centre falls in that third of the
      bed, best clearance first -- and refused by name when none does;
    * refused: the verdict's own sentences, spots and occupancy ride the
      error;
    * ok: the part is moved to the verdict's spot in a temp copy (an STL by
      its vertices, a 3MF by its build items, anything else refused) and
      that copy is the effective input, with ``info["placement"]`` the
      verdict and ``approval_carries`` false;
    * no verdict at all (offline, signed out, nothing answered): REFUSE.
      The plate holds a part and Kiln cannot check clearance, so nothing is
      sliced onto it; the sentence says why and what to do.

    The caller runs :func:`_verify_plate_placement` on the sliced file and
    :func:`_attach_placement` on its success response.
    """
    import kiln.server as _srv
    from kiln import _pro_placement_bridge as bridge
    from kiln import plate_state

    kind, value, problem = _parse_placement(placement)
    if problem:
        return input_path, _srv._error_dict(problem, code="PLACEMENT_INVALID"), {"plate": "unknown"}
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return input_path, None, {"plate": "unknown", "gate": "skipped_hosted"}
    except Exception:  # noqa: BLE001
        pass
    if adapter is None:
        try:
            adapter = _srv._resolve_adapter(printer_name)
        except Exception:  # noqa: BLE001 -- no printer means no plate record
            adapter = None
    if adapter is None:
        return input_path, None, {"plate": "unknown", "gate": "skipped_no_printer"}
    state = plate_state.read(adapter)
    if not state.occupied:
        return input_path, None, {"plate": state.status}

    from kiln.printers.bed_fit import get_build_volume

    volume = get_build_volume(effective_printer_id)
    bed = [float(volume[0]), float(volume[1])] if volume else None
    part, bbox = _part_envelope(input_path, profile_path)
    pid = effective_printer_id or plate_state.declared_model_of(adapter) or ""
    holds = _plate_holds_sentence(state)
    occupied_info = {"plate": "occupied"}

    if kind == "auto":
        probe, _reason = bridge.ask(bridge.request_for(adapter, pid, placement="auto", part=part))
        message = (
            f"{holds} Slicing now would put the new part on top of it. Name a spot beside it "
            f'(placement=[x, y] in mm, or a region such as "front-left"), or clear the plate and say so.'
            + (_spots_clause(probe) if isinstance(probe, dict) else "")
        )
        return input_path, _placement_refusal(message, "PLACEMENT_PLATE_OCCUPIED", state=state, bed=bed, verdict=probe), occupied_info

    if kind == "region":
        if bed is None:
            return input_path, _placement_refusal(
                f"{holds} Kiln does not know this printer's bed size, so it cannot resolve a region; "
                "name the spot as [x, y] in mm instead.",
                "PLACEMENT_INVALID", state=state, bed=bed,
            ), occupied_info
        probe, reason = bridge.ask(bridge.request_for(adapter, pid, placement="auto", part=part))
        if probe is None:
            return input_path, _placement_refusal(_no_verdict_sentence(state, reason), "PLACEMENT_NO_VERDICT", state=state, bed=bed, miss=reason), occupied_info
        spots = [s for s in (probe.get("spots") or []) if isinstance(s, dict)]
        in_region = []
        for spot in spots:
            centre = _spot_centre(spot, part)
            if centre is not None and _cell_of(centre[0], centre[1], bed) == value:
                in_region.append(spot)
        if not in_region:
            rooms = _regions_with_room(spots, part, bed)
            if not rooms:
                where = "there is no safe spot anywhere beside it"
            elif len(rooms) == 1:
                where = f"there is room {rooms[0]}"
            else:
                where = f"there is room {', '.join(rooms[:-1])} and {rooms[-1]}"
            job_name = _pretty_job_name(state.job.file if state.job else "")
            message = f"There is no safe spot {_region_name(value)} with {job_name} on the plate; {where}."
            return input_path, _placement_refusal(message, "PLACEMENT_NO_ROOM_IN_REGION", state=state, bed=bed, verdict=probe), occupied_info
        best = max(in_region, key=lambda s: float(s.get("clearance_mm") or 0.0))
        request = bridge.request_for(adapter, pid, placement=list(best["at_mm"]), part=part, placed_by="human")
    elif kind == "keep":
        if bbox is None:
            return input_path, _placement_refusal(
                f"{holds} Kiln could not read where this file puts the part, so it cannot keep it there; "
                "name a spot as [x, y] in mm or clear the plate and say so.",
                "PLACEMENT_UNPLACEABLE", state=state, bed=bed,
            ), occupied_info
        request = bridge.request_for(adapter, pid, placement="keep", part=part, keep_at=[bbox["x_min"], bbox["y_min"]])
    else:
        request = bridge.request_for(adapter, pid, placement=value, part=part, placed_by="agent")

    verdict, reason = bridge.ask(request)
    if verdict is None:
        return input_path, _placement_refusal(_no_verdict_sentence(state, reason), "PLACEMENT_NO_VERDICT", state=state, bed=bed, miss=reason), occupied_info
    if not verdict.get("ok"):
        spots_clause = _spots_clause(verdict)
        message = (
            f"Kiln won't slice onto this plate there: {_refusal_sentences(verdict)}"
            + (spots_clause or " No spot on the plate is safe beside it; clear the plate and say so.")
        )
        return input_path, _placement_refusal(message, "PLACEMENT_REFUSED", state=state, bed=bed, verdict=verdict), occupied_info

    if kind == "keep":
        placed = input_path
    else:
        rect = verdict.get("footprint_mm")
        target = list(rect[:2]) if isinstance(rect, (list, tuple)) and len(rect) == 4 else verdict.get("at_mm")
        if bbox is None or not (isinstance(target, (list, tuple)) and len(target) == 2):
            return input_path, _placement_refusal(
                f"{holds} Kiln could not move the part to the spot the check approved, so it won't slice onto this plate.",
                "PLACEMENT_UNPLACEABLE", state=state, bed=bed, verdict=verdict,
            ), occupied_info
        try:
            placed, problem = _place_copy(input_path, bbox, [float(target[0]), float(target[1])])
        except Exception as exc:  # noqa: BLE001 -- a copy that failed is a part that stays unplaced
            _logger.warning("Placement copy failed for %s: %s", input_path, exc)
            placed, problem = None, f"{holds} Kiln could not move the part to the approved spot ({exc}), so it won't slice onto this plate."
        if placed is None:
            return input_path, _placement_refusal(problem or "", "PLACEMENT_UNPLACEABLE", state=state, bed=bed, verdict=verdict), occupied_info
        _logger.info("Placed %s beside %s at %s -> %s", os.path.basename(input_path), state.job.file if state.job else "a part", target, placed)

    job_name = _pretty_job_name(state.job.file if state.job else "")
    info: dict[str, Any] = {
        "plate": "occupied",
        "placement": verdict,
        "placed_input_path": placed,
        "approval_carries": False,
        "approval_note": (
            f"placed beside {job_name}, which is still on the plate, so a yes given on the design "
            "mesh does not carry; the stage on this result shows the plate as it will print — approve from here"
        ),
        "_verify": {"request": request, "state": state, "bed": bed},
        "_state": state,
        "_machine": _contract_machine_id(adapter),
    }
    return placed, None, info


def _contract_machine_id(adapter: Any) -> str:
    """The identity a quiet-start file is bound to: the same one the
    pre-print gate reads back (:func:`kiln.printers.print_gate.same_bed_machine_id`)."""
    try:
        from kiln.printers.print_gate import same_bed_machine_id

        return same_bed_machine_id(adapter)
    except Exception:  # noqa: BLE001
        return ""


def _quiet_start_plan(info: dict[str, Any]) -> dict[str, Any] | None:
    """The verdict's start plan, bound to this machine, when the verdict
    carries one that is ok and available; else ``None``."""
    verdict = info.get("placement") if isinstance(info, dict) else None
    start = verdict.get("start") if isinstance(verdict, dict) else None
    if not isinstance(start, dict) or not start.get("ok") or not start.get("available"):
        return None
    machine = str(info.get("_machine") or "")
    if not machine or not start.get("plate_fingerprint"):
        return None
    return {**start, "planned_for_machine": machine}


def _lift_floor_of(info: dict[str, Any]) -> float | None:
    """The lift floor the wrap raises every lift to, on every tier."""
    verdict = info.get("placement") if isinstance(info, dict) else None
    start = verdict.get("start") if isinstance(verdict, dict) else None
    floor = start.get("lift_floor_mm") if isinstance(start, dict) else None
    try:
        return float(floor) if floor is not None else None
    except (TypeError, ValueError):
        return None


def _verify_plate_placement(gcode_path: str | None, info: dict | None) -> tuple[dict | None, dict]:
    """Post-slice pass: the file that was just sliced goes back to the engine.

    Called by every slice door right after ``slice_file`` and before any
    wrap or upload.  The pre-slice verdict was about an envelope; this one
    is about the real toolpath -- its skirt, its tower, its colour changes.
    A verdict that is not ok, or no verdict at all, refuses the result: the
    door returns the error dict and never hands the file on.  Returns
    ``(error_dict_or_None, info)`` with ``info["placement"]`` replaced by
    the verified verdict.  A clear-plate *info* passes straight through.
    """
    if not isinstance(info, dict) or info.get("plate") != "occupied":
        return None, info
    from kiln import _pro_placement_bridge as bridge

    ctx = info.pop("_verify", None) or {}
    state, bed, request = ctx.get("state"), ctx.get("bed"), ctx.get("request")
    if state is None or not isinstance(request, dict) or not gcode_path:
        from kiln.server import _error_dict

        return _error_dict(
            "Kiln could not check the sliced file against the plate, so it won't hand it on.",
            code="PLACEMENT_UNVERIFIED",
        ), info
    verdict, reason = bridge.ask({**request, "sliced_gcode": {"path": str(gcode_path)}})
    if verdict is None:
        return _placement_refusal(_no_verdict_sentence(state, reason), "PLACEMENT_NO_VERDICT", state=state, bed=bed, miss=reason), info
    if not verdict.get("ok"):
        message = (
            f"Kiln checked the sliced file against the plate and won't hand it on: {_refusal_sentences(verdict)}"
            + (_spots_clause(verdict) or " Clear the plate and say so.")
        )
        return _placement_refusal(message, "PLACEMENT_REFUSED", state=state, bed=bed, verdict=verdict), info
    info["placement"] = verdict
    info["verified_sliced_file"] = True
    return None, info


def _attach_placement(response: dict, info: dict | None) -> None:
    """Stamp a success response with the verdict and the approval note, on an
    occupied plate only.  The bed-fit gate's own ``approval_carries`` inside
    ``bed_fit`` stays as it is for the clear-plate case."""
    if not isinstance(info, dict) or info.get("plate") != "occupied":
        return
    info.pop("_verify", None)
    info.pop("_machine", None)
    state = info.pop("_state", None)
    response["placement"] = info.get("placement")
    response["approval_carries"] = False
    response["approval_note"] = info.get("approval_note")
    # How this file starts beside the occupant, said on the slice result so
    # an agent knows before it reaches for a start: the quiet way when the
    # verdict's plan is this account's to use, else why not -- the plan's
    # own sentence (the tier, an unverified printer, a lift that does not
    # fit) or the record's, that the file's start sequence crosses the plate.
    verdict = info.get("placement") if isinstance(info.get("placement"), dict) else {}
    start = verdict.get("start") if isinstance(verdict, dict) else None
    if isinstance(start, dict) and start.get("ok") and start.get("available"):
        response["start"] = {
            "allowed": True, "mode": "quiet_start",
            "why": (
                "This file starts the quiet way: no Z home, no probe and no purge line across the plate; the head "
                f"lifts to {float(start['clear_z_mm']):g} mm clear of what is there before it moves, and the printer "
                "is asked whether it is homed and idle at the moment of the start."
            ),
        }
        return
    if isinstance(start, dict) and start.get("refusals"):
        response["start"] = {"allowed": False, "mode": "quiet_start", "why": _refusal_sentences(start)}
        return
    if state is not None:
        response["start"] = {"allowed": False, "why": state.start_refusal_sentence()}


def _placed_slice(
    input_path: str,
    *,
    effective_printer_id: str | None,
    printer_name: str | None,
    placement: Any,
    profile_path: str | None = None,
    adapter: Any | None = None,
    auto_center: bool | None = None,
    material_id: str | None = None,
    slicer: Any | None = None,
    plate_gate: bool = True,
    **slice_kwargs: Any,
) -> tuple[Any | None, dict | None, dict[str, Any]]:
    """The slice every door shares: plate gate, bed-fit gate, slicer, second verdict.

    *plate_gate* ``False`` skips the plate gate and its second verdict, and
    is for a door whose output is never printed -- an estimate.  The plate
    gate exists because a file sliced onto an occupied plate would be
    started onto it; a number about how long a part takes is started onto
    nothing.  The bed-fit gate is untouched by it: *auto_center* decides that
    gate exactly as before, and a part that does not fit the bed has no
    honest estimate on any plate.

    Returns ``(result, error_dict_or_None, info)``.  *auto_center* ``None``
    means the door never had a bed-fit gate and keeps not having one; a
    bool runs :func:`_apply_bed_fit_gate` with it, which never re-centres a
    part placed beside an occupant.  *slicer* is ``kiln.slicer.slice_file``
    unless a door slices another way (the CLI's multi-colour copies); it is
    called as ``slicer(path, profile=profile_path, **slice_kwargs)`` and
    must return a ``SliceResult``.  ``info`` carries ``placement`` (the
    plate gate's info, for :func:`_attach_placement`), ``bed_fit`` (the bed
    gate's block, or ``None``), ``effective_input`` (the file that was
    sliced), and -- on an occupied plate -- ``quiet_start`` (the start plan
    the wrap writes into the file, or ``None``) and ``lift_floor_mm`` (the
    height every lift in the file rises to).  Raises whatever the slicer
    raises; each door words that.
    """
    if plate_gate:
        placed, err, place_info = _apply_plate_placement(
            input_path, effective_printer_id=effective_printer_id, printer_name=printer_name,
            placement=placement, profile_path=profile_path, adapter=adapter,
        )
    else:
        placed, err, place_info = input_path, None, {"plate": "not_checked"}
    info: dict[str, Any] = {"placement": place_info, "bed_fit": None, "effective_input": input_path}
    if err is not None:
        return None, err, info
    effective_input = placed
    if auto_center is not None:
        effective_input, gate_err, bed_fit = _apply_bed_fit_gate(
            placed, effective_printer_id, auto_center and place_info.get("plate") != "occupied",
            material_id=material_id,
        )
        info["bed_fit"] = bed_fit
        if gate_err is not None:
            return None, _gate_error_response(gate_err), info
    info["effective_input"] = effective_input
    if slicer is None:
        from kiln.slicer import slice_file

        result = slice_file(effective_input, profile=profile_path, **slice_kwargs)
    else:
        result = slicer(effective_input, profile=profile_path, **slice_kwargs)
    verify_err, place_info = _verify_plate_placement(result.output_path, place_info)
    info["placement"] = place_info
    if verify_err is not None:
        return None, verify_err, info
    # The quiet start's plan and the lift floor, for whichever door wraps
    # the file next: the plan when the verdict carries one this account may
    # use, the floor whenever there is one (it is a safety number, and the
    # wrap honours it on every tier).
    info["quiet_start"] = _quiet_start_plan(place_info)
    info["lift_floor_mm"] = _lift_floor_of(place_info)
    return result, None, info


def _auto_wrap_bambu_3mf(
    gcode_path: str,
    effective_printer_id: str | None,
    stl_path: str | None,
    *,
    quiet_start: dict[str, Any] | None = None,
    lift_floor_mm: float | None = None,
) -> tuple[str | None, str | None]:
    """If the effective printer is a Bambu Lab, repackage the sliced
    G-code into a 3MF so it can actually start (Bambu firmware ignores
    raw ``.gcode`` via the ``gcode_file`` MQTT command; only
    ``project_file`` works, and that requires ``.3mf``).

    ``stl_path`` is routed by extension — see
    :func:`~kiln.printers.bambu_3mf.thumbnail_inputs_for_model`, shared
    with the other wrap doors — into either a mesh to render or a 3MF to
    copy an existing preview from.  Any other extension is ignored: the
    wrap still succeeds, without a preview.

    Returns ``(threemf_path, warning)``.  When no wrap happens, both
    are ``None``.  Failure is non-fatal — the original gcode_path is
    still usable for non-Bambu printers or manual wrapping.
    """
    if not effective_printer_id or not effective_printer_id.startswith("bambu"):
        return (None, None)
    try:
        # CRITICAL: use build_bambu_3mf (adds BambuStudio start-gcode —
        # G28 homing, M620 AMS load, purge line, bed leveling) rather
        # than repackage_gcode_as_bambu_3mf (only zips gcode that ALREADY
        # has Bambu init).  PrusaSlicer-native output never has Bambu
        # init, so repackage_* produced 3MFs that caused nozzle crashes
        # because the printer tried to execute G1 moves without ever
        # homing — incident #0 (2026-04-15).  Route through the adapter's
        # wrap_gcode_as_3mf method which wires to build_bambu_3mf with
        # the correct start-gcode for the printer model.
        from pathlib import Path as _Path

        from kiln.printers.bambu_3mf import (
            BambuPrintSettings,
            build_bambu_3mf,
            thumbnail_inputs_for_model,
        )

        stem = gcode_path.rsplit(".", 1)[0]
        if quiet_start is not None and not stem.endswith("_quiet"):
            stem += "_quiet"
        threemf_path = stem + ".gcode.3mf"
        # Shared with every other door that wraps gcode, so a format one
        # of them learns to preview is previewable from all of them.
        stl_paths, source_3mf = thumbnail_inputs_for_model(stl_path)

        gcode_body = _Path(gcode_path).read_text(encoding="utf-8")
        # This door builds its own settings rather than going through the
        # adapter, so it has to ask the machine what colour is loaded the
        # same way the adapter does — otherwise the everyday slice keeps
        # declaring white at a printer holding red, drawing a white
        # preview and then warning about the mismatch it just created.
        # Best-effort and cached-only: an unreachable printer costs the
        # colour, never the wrap.
        _loaded_color: str | None = None
        try:
            import kiln.server as _s

            _adapter = _s._get_adapter()
            if hasattr(_adapter, "active_filament_color"):
                _loaded_color = _adapter.active_filament_color()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Filament colour unavailable for wrap: %s", exc)

        settings = BambuPrintSettings(
            model_name=_Path(gcode_path).stem,
            # Type and temperatures are left unsaid on purpose: the build
            # reads them off the G-code -- the material the slice was
            # weighed as and the temperatures it heats to -- so the start
            # sequence and the tile agree with the toolpath.
            filament_colors=[_loaded_color] if _loaded_color else None,
        )
        wrap = build_bambu_3mf(
            gcode_body,
            threemf_path,
            settings=settings,
            source_3mf_path=source_3mf,
            stl_paths=stl_paths,
            # A part is on the plate: the plan becomes the file's prologue and
            # contract, and the floor lifts every colour change and the end.
            quiet_start=quiet_start,
            lift_floor_mm=lift_floor_mm,
            # The profile id the caller asked for, or the configured
            # printer_model when they did not — a declaration either way, and
            # already what chose the slicer profile.  Selects the per-model
            # end gcode; an id with no template of its own keeps the A1 files.
            printer_model=effective_printer_id,
        )
        _logger.info(
            "Auto-wrapped %s as Bambu 3MF (with Bambu init) at %s",
            os.path.basename(gcode_path), threemf_path,
        )
        # The printer will know this job by the WRAP's name.  Join it to the
        # slice in the ledger, as the adapter's own wrap door does — without
        # this, the design mesh's approval never reached the file that was
        # uploaded, and the stage had no slice to dress the wrap in (0 of 8
        # ledger rows carried ``wrapped``, measured 2026-09-21).
        try:
            from kiln.monitor_twin import note_wrapped

            note_wrapped(gcode_path, threemf_path)
        except Exception:  # noqa: BLE001 — bookkeeping never blocks a wrap
            _logger.debug("monitor-twin wrap note failed", exc_info=True)
        # A successful wrap can still hand back a file whose startup sequence
        # belongs to another machine.  That used to be a log line only, which
        # is invisible to the agent holding the 3MF — and it stopped being a
        # rare case the moment the relative-E fix let the other Bambu models
        # slice at all.  The wrap succeeded, so this is a warning, not a
        # failure: same return shape, second field populated.
        #
        # Read defensively on purpose.  The 3MF is already written by this
        # point, and raw gcode does not start on Bambu firmware at all, so an
        # unexpected builder return must cost the warning and never the print.
        # The warning itself is covered against the real builder in
        # TestStartGcodeSubstitutionIsAudible.
        return (threemf_path, getattr(wrap, "start_gcode_warning", None))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Bambu auto-wrap failed: %s — leaving as raw gcode", exc)
        return (None, f"Bambu auto-wrap failed: {exc}")


def _steer_to_wrapped_upload(
    response: dict,
    threemf_path: str,
    effective_printer_id: str | None,
) -> None:
    """STEER, don't just validate — name the file the printer can print.

    This tool was TOLD the target printer, so it must not hand back two
    files and let the caller guess: on a Bambu the raw gcode carries no
    start block, so uploading it is refused three steps later by the
    homing gate (incident #0 class).  ``output_path`` already points at
    the 3MF, but the human-readable message is built back in
    ``kiln.slicer`` BEFORE the wrap exists and still names the gcode —
    so the prose and the field disagreed, and prose is what an agent
    reads.  Measured 2026-08-17: an agent holding both files picked the
    unprintable one, because the message and ``upload_file``'s docstring
    both pointed at it.
    """
    # The recommended file is one the printer's screen can draw.  A slicer
    # that wrote its own plate (Orca, Studio) may have left the tile slots
    # out or drawn them grey; completion renders them from the plate's own
    # model in its declared colours and never touches the G-code.
    try:
        from kiln.printers.bambu_3mf import bambu_archive_problems, complete_bambu_archive

        if bambu_archive_problems(threemf_path):
            complete_bambu_archive(threemf_path)
            # The 3MF's thumbnail tiles were filled in.  Not "a preview
            # was seen" — the stage's ``shown`` says that, and only that.
            response["thumbnails_completed"] = True
    except Exception as exc:  # noqa: BLE001 — the upload door still refuses an incomplete file
        _logger.warning("Bambu preview completion failed for %s: %s", threemf_path, exc)
        response.setdefault("warnings", []).append(f"Preview completion failed: {exc}")
    response["recommended_upload_path"] = threemf_path
    response["recommended_upload_reason"] = (
        f"{effective_printer_id or 'This printer'} starts prints "
        f"from a .3mf project file; the raw .gcode has no start "
        f"block (no G28 homing) and will be refused at upload."
    )
    response["raw_gcode_note"] = (
        "Kept for inspection and for printers that take bare "
        "gcode — NOT for this printer."
    )
    wrapped_name = os.path.basename(threemf_path)
    response["message"] = (
        f"{response.get('message', 'Sliced')} "
        f"Upload {wrapped_name}."
    ).strip()


def _steer_to_complete_gcode(
    response: dict,
    gcode_path: str,
    effective_printer_id: str | None,
    model_path: str | None,
) -> None:
    """The raw-G-code sibling of :func:`_steer_to_wrapped_upload`.

    A Bambu gets a wrapped archive; everybody else gets the G-code itself,
    and the file this tool recommends must already be one the printer's
    surface can draw and weigh.  Kiln's profiles name no filament, so
    PrusaSlicer writes ``0.00 g``, and its CLI draws no thumbnail at all —
    so Mainsail, Fluidd, OctoPrint, PrusaLink and Duet Web Control were all
    handed a file with a placeholder tile and no weight.  The completion
    puts both in, from the mesh that was sliced and the file's own moves,
    and never touches a move.  The printer id is passed along because the
    printer's OWN screen is a property of the machine, not of the software
    in front of it: a Prusa MK4 / MINI / XL / Core One draws a QOI block
    its firmware asks for by exact size, and the slice door is the one
    door that knows the model without knowing the adapter.

    Best-effort by contract: a picture that cannot be drawn costs the
    picture, never the slice.  The upload door still refuses a file that
    leaves here incomplete.
    """
    try:
        from kiln.printers.gcode_complete import complete_gcode_for_printer

        complete_gcode_for_printer(
            gcode_path, printer_model=effective_printer_id, model_path=model_path,
        )
        response["thumbnails_completed"] = True
    except Exception as exc:  # noqa: BLE001 — the upload door still refuses an incomplete file
        _logger.warning("G-code completion failed for %s: %s", gcode_path, exc)
        response.setdefault("warnings", []).append(f"Preview completion failed: {exc}")
    response["recommended_upload_path"] = gcode_path
    response["recommended_upload_reason"] = (
        f"{effective_printer_id or 'This printer'} prints the G-code itself; "
        f"it now carries the preview its file list draws and the weight its "
        f"screen shows."
    )


def _maybe_auto_assembly_manual(metadata: dict) -> dict | None:
    """Optional plugin hook: route ``slice_and_print`` metadata through
    kiln-pro's assembly-manual generator if it's installed.

    Returns ``None`` when kiln-pro isn't installed — public Kiln keeps
    no mandatory dependency on it.  When installed it returns a
    JSON-friendly dict the caller can pass through to the user
    verbatim (cached PDF path, pending status, or an upsell hint).
    All errors are caught — never raises out to the print pipeline.

    Multi-language manuals and co-brand wordmarks are kiln-pro
    Business+ features (https://kiln3d.com/pricing); the metadata
    keys for them are accepted at every tier and ignored where the
    tier doesn't allow.
    """
    try:
        from kiln_pro.manuals.auto_trigger import (
            maybe_generate_for_print_job,
        )
    except ImportError:
        return None

    try:
        result = maybe_generate_for_print_job(
            metadata["assembly_json"],
            output_dir=metadata.get("manual_output_dir"),
            design_name=metadata.get("manual_design_name"),
            branding=metadata.get("manual_branding"),
            co_brand_name=metadata.get("manual_co_brand_name"),
            languages=metadata.get("manual_languages"),
            cover_language=metadata.get("manual_cover_language"),
        )
    except Exception as exc:  # noqa: BLE001 — never block slice_and_print
        _logger.info("auto_assembly_manual integration failed: %s", exc)
        return None

    return {
        "skipped": result.skipped,
        "reason": result.reason,
        "parts_count": result.parts_count,
        "fingerprint": result.fingerprint,
        "cached_path": result.cached_path,
        "expected_path": result.expected_path,
        "pending": result.pending,
        "upsell_text": result.upsell_text,
        "first_time_notice": result.first_time_notice,
    }


# Canonical slicer identities, and the token that names each one.
# Ordered fork-before-upstream: an OrcaSlicer build may mention Bambu
# Studio (it is a fork of it) in its own version banner, so "orca" has
# to be answered first for that build to be called what it is.
_SLICER_IDENTITY_TOKENS: tuple[tuple[str, str], ...] = (
    ("orca", "orcaslicer"),
    ("bambu", "bambustudio"),
    ("prusa", "prusaslicer"),
)


def _loaded_material_for(printer_name: str | None, material: str | None) -> str | None:
    """The spool the target printer reports loaded, when nothing was declared.

    One reading for every slicing door: the density the slicer is handed
    (:mod:`kiln.slicer_filament`) and the material hint a door uses for
    adhesion and validation come from the same answer.  A declared material
    makes the question moot, so the printer is not asked.  Never raises —
    an unreachable or unit-less printer reads as ``None``, and the slice
    resolves PLA and says so.
    """
    if material:
        return None
    try:
        import kiln.server as _srv
        from kiln.slicer_filament import loaded_filament_type

        return loaded_filament_type(_srv._resolve_adapter(printer_name))
    except Exception:  # noqa: BLE001 — a slice must never fail on a status query
        _logger.debug("Loaded-spool read for %r failed", printer_name, exc_info=True)
        return None


def _resolve_slicer_name(slicer_path: str | None = None) -> str | None:
    """Name the slicer that will actually run this job.

    Resolves through :func:`kiln.slicer.find_slicer` — the same call
    :func:`kiln.slicer.slice_file` makes with the same argument — so the
    answer names the binary that runs, rather than guessing from a
    parallel detection of our own.  The binary's file name, its
    directory-independent basename and its ``--version`` banner are all
    consulted, so a renamed or bundled build still identifies itself.

    :param slicer_path: Explicit slicer binary, or ``None`` to use the
        same auto-detection the slice itself will use.
    :returns: One of ``"orcaslicer"``, ``"bambustudio"``,
        ``"prusaslicer"``, or ``None`` when no slicer is installed or
        the binary isn't one of those.  Never raises: an unidentifiable
        slicer is a missing fact, not an error.
    """
    try:
        from kiln.slicer import find_slicer

        info = find_slicer(slicer_path)
        haystack = " ".join((
            info.name or "",
            os.path.basename(info.path or ""),
            info.version or "",
        )).lower()
    except Exception as exc:  # noqa: BLE001 — a missing name is not an error
        _logger.debug("Slicer identity unresolved: %s", exc)
        return None

    for token, identity in _SLICER_IDENTITY_TOKENS:
        if token in haystack:
            return identity
    return None


def _maybe_overlay_calibration(
    parsed_overrides: dict[str, str],
    printer_id: str,
    *,
    material: str | None = None,
    input_path: str | None = None,
    slicer_name: str | None = None,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Inject a Pro+ user's calibrated slicer values into ``parsed_overrides``.

    Lazy-imports ``kiln_pro.engineering.calibration_coach`` so this is
    a no-op for free-tier installs that don't have kiln-pro present.
    When kiln-pro is installed AND the user has a HIGH/MEDIUM-tier
    calibrated slicer profile for ``printer_id`` (with material
    matched when supplied; fallback to most-recent across all
    materials when ``material`` is ``None``), the helper:

    - Fills in keys the caller did NOT already set in ``parsed_overrides``
      with the user's calibrated values (extrusion_multiplier,
      filament_max_volumetric_speed, pressure_advance, xy_size_compensation,
      filament_retraction_length).  User-supplied overrides ALWAYS win;
      calibration only fills gaps.  Idempotent.
    - Returns the standard ``calibration_used`` block (same shape as
      every other wire-up site) so the slicer tool's response can
      surface what was applied.
    - If ``input_path`` is supplied AND the calibration overlay produced
      a non-None block, ALSO records a slice event into kiln-pro's
      per-design slice-history artifact via the bridge (best-effort,
      never raises).  Centralising the recording hook here means any
      slice-flow caller of this helper automatically participates;
      future tools that apply calibration overlay don't need to know
      about slice history at all.

    ``slicer_name`` names the slicer this job is about to be handed to
    (see :func:`_resolve_slicer_name`).  A calibrated value is expressed
    against the base profile of the slicer it was tuned in, so which
    slicer runs decides which stored profile applies to this job; pass
    ``None`` when it can't be determined and the lookup answers without
    that fact, as it did before this parameter existed.

    Returns ``(modified_overrides, calibration_used_block_or_None)``.
    The calibration block is ``None`` when kiln-pro isn't installed
    OR ``calibration_for`` raised; never raises out of this helper.
    """
    try:
        from kiln_pro.engineering.calibration_coach import (
            apply_calibration_to_slicer_args,
            calibration_for,
            calibration_used_block,
        )
    except ImportError:
        return parsed_overrides, None

    try:
        merged = apply_calibration_to_slicer_args(
            parsed_overrides, printer_id, material,
            slicer_name=slicer_name,
        )
        # Same identity on both calls, so the block that reports what
        # calibration was used describes the same profile the overlay
        # above resolved against.
        verdict = calibration_for(printer_id, material, prefer_slicer=slicer_name)
        cal_used = calibration_used_block(verdict, printer_id=printer_id)
    except Exception as exc:  # noqa: BLE001 — never block slicing
        _logger.debug(
            "calibration overlay skipped for printer %r: %s",
            printer_id, exc,
        )
        return parsed_overrides, None

    # Record the slice event for design-anchored explanation if we have
    # an input path AND the overlay actually produced a calibration
    # block.  Wrapped in try/except so any kiln-pro hiccup never blocks
    # a successful slice.
    if input_path and cal_used is not None:
        try:
            from kiln_pro.bridge import pro_features
            pro_features.record_slice_for_input(
                input_path=input_path,
                printer_id=printer_id,
                material=cal_used.get("material") or material or "",
            )
        except Exception:
            pass  # never block slicing on telemetry

    # First-time-use UX (added inside kiln-pro's calibration_used_block
    # itself — see kiln_pro.engineering.calibration_coach).  Public-side
    # has no knowledge of the marker mechanism; that lives entirely
    # inside kiln-pro so the kiln-pro side can grow the UX without a
    # corresponding public-side change.

    return merged, cal_used


# ---------------------------------------------------------------------------
# Multicolor-flatten advisory
#
# A multicolor 3MF (per-object extruder assignments, or a painted
# single object) sliced through a single-filament configuration prints
# ENTIRELY in one filament — PrusaSlicer honors the geometry and drops
# the color story (measured: two-color file, default single-extruder
# config → 0 tool changes, second filament 0.00 mm).  Before this
# advisory the tool returned a bare green success and the user found
# out at the printer.  Warn, never block: slicing has already
# succeeded when the advisory runs, and a detection failure of any
# kind reads as "not multicolor".
# ---------------------------------------------------------------------------

# A tool-select command at line start in G-code (``T0``, ``T1`` ...).
# Word boundary so ``T1`` matches but a hypothetical ``T1x`` doesn't;
# mid-line forms like ``M104 T0 S200`` are heater targeting, not tool
# changes, and correctly don't match.
_GCODE_TOOL_RE = re.compile(r"T(\d+)\b")


def _detect_3mf_multicolor(input_path: str) -> dict[str, Any] | None:
    """Does this 3MF ask for more than one filament?

    The detection itself lives with the format —
    :func:`kiln.multicolor_3mf.detect_3mf_multicolor` — because the
    slicing engine now acts on it (expanding filament presets on the
    Orca dialect) and the engine cannot import from the tool layer.
    This wrapper keeps the tool layer's advisory wiring and its tests
    pointed at one name.
    """
    from kiln.multicolor_3mf import detect_3mf_multicolor

    return detect_3mf_multicolor(input_path)


def _profile_filament_slots(profile_path: str | None) -> int:
    """How many filament slots the effective slicer config can express.

    One implementation, in the engine
    (:func:`kiln.slicer.profile_filament_slots`), because the engine now
    branches on the same number when it decides whether to expand
    filament presets.  Two copies is how the advisory ends up reporting
    a limit the backend no longer has.
    """
    from kiln.slicer import profile_filament_slots

    return profile_filament_slots(profile_path)


def _count_gcode_tools(gcode_path: str | None) -> tuple[int, int] | None:
    """``(tool_changes, distinct_tools)`` from one pass over the G-code.

    Counts line-start ``T<n>`` commands — the form PrusaSlicer emits at
    every filament change.  A tool CHANGE is a T command selecting a
    different tool than the active one (the first selection arms, it
    doesn't change).  Returns ``None`` when the file can't be read, so
    the caller treats "couldn't measure" as no evidence either way.
    """
    if not gcode_path:
        return None
    try:
        tools: set[int] = set()
        changes = 0
        active: int | None = None
        with open(gcode_path, errors="replace") as fh:
            for line in fh:
                m = _GCODE_TOOL_RE.match(line)
                if not m:
                    continue
                n = int(m.group(1))
                tools.add(n)
                if active is not None and n != active:
                    changes += 1
                active = n
        return changes, len(tools)
    except OSError:
        return None


def _multicolor_flatten_advisory(
    input_path: str,
    profile_path: str | None,
    gcode_path: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Advisory for a multicolor 3MF that just sliced down to one filament.

    Returns ``(info_block, warning_text)`` when the input 3MF carries a
    multicolor story the effective configuration cannot express (single
    filament slot, and the produced G-code — when readable — confirms no
    second tool was used).  Returns ``(None, None)`` when the input isn't
    a multicolor 3MF, the config expresses two-plus filaments, or the
    G-code measurably uses two-plus tools.  Never raises and never
    blocks — the slice already succeeded; this only makes the result
    honest.
    """
    try:
        if not input_path.lower().endswith(".3mf"):
            return None, None
        evidence = _detect_3mf_multicolor(input_path)
        if evidence is None:
            return None, None

        # How many filaments the FILE asks for vs how many the slice could
        # express.  PrusaSlicer clamps gracefully (measured: 6 paint states
        # on a 2-extruder config slices exit-0 with the excess states folded
        # onto the available tools), so partial flattening is silent too —
        # 6 colors quietly become 2.  Warn whenever colors are LOST, not
        # only when everything collapses to one.
        # The SAME number the engine expands filament presets to, so the
        # advisory can never disagree with the slice about what the file
        # asked for.  It is a highest-slot-index, not a distinct count:
        # parts on extruders 1 and 3 need three slots, and a two-slot
        # config genuinely cannot print them.
        colors_needed = int(
            evidence.get("filament_slots_needed")
            or max(
                len(evidence.get("extruders") or ()),
                int(evidence.get("paint_filaments") or 0),
                int(evidence.get("palette_colors") or 0),
            )
        )
        slots = _profile_filament_slots(profile_path)
        counted = _count_gcode_tools(gcode_path)
        expressed = max(slots, counted[1] if counted is not None else 0)
        if expressed >= colors_needed:
            return None, None

        block: dict[str, Any] = {
            "multicolor_input": True,
            "colors_flattened": True,
            "colors_in_file": colors_needed,
            "profile_filament_slots": slots,
            **evidence,
        }
        if counted is not None:
            block["tool_changes"] = counted[0]
            block["distinct_tools"] = counted[1]

        if "extruders" in evidence:
            carries = (
                f"parts assigned to {len(evidence['extruders'])} different "
                "filaments"
            )
        elif "paint_attribute" in evidence:
            carries = f"painted-on colors in {colors_needed} filaments"
        else:
            carries = f"a {evidence['palette_colors']}-color palette"
        measured = (
            f" (measured: {counted[0]} tool changes in the G-code)"
            if counted is not None
            else ""
        )
        if expressed <= 1:
            lost = f"the whole object will print in ONE filament{measured}"
        else:
            lost = (
                f"only {expressed} filaments were available, so its "
                f"{colors_needed} colors were folded together{measured}"
            )
        warning = (
            f"Multicolor flattened: this 3MF carries {carries}, but {lost}. "
            "Kiln keeps the colors automatically when it slices through an "
            "OrcaSlicer or BambuStudio binary — the color-to-filament "
            "assignments are already in the file.  Install one of those "
            "slicers (or point slicer_path / KILN_SLICER_PATH at its "
            "binary) and slice again; no slicer GUI involved.  "
            "Alternatively, rebuild the plate with the "
            "multi_material_print tool to assign a material per part."
        )
        return block, warning
    except Exception:  # noqa: BLE001 — advisory only, never break slicing
        return None, None


class _SlicerToolsPlugin:
    """Slicer tools: slice, reslice, find slicer, list/get profiles.

    Tools:
        - slice_model
        - slice_and_print
        - reslice_with_overrides
        - find_slicer  (name override)
        - list_slicer_profiles  (name override)
        - get_slicer_profile  (name override)
    """

    @property
    def name(self) -> str:
        return "slicer_tools"

    @property
    def description(self) -> str:
        return "Slicer tools: slice, reslice, find slicer, list/get profiles"

    def register(self, mcp: Any) -> None:  # noqa: C901, PLR0915
        """Register slicer tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # slice_model
        # ------------------------------------------------------------------

        @mcp.tool()
        def slice_model(
            input_path: str,
            output_dir: str | None = None,
            profile: str | None = None,
            printer_id: str | None = None,
            slicer_path: str | None = None,
            auto_center: bool = True,
            printer_name: str | None = None,
            material: str | None = None,
            placement: str | list[float] | None = None,
        ) -> dict:
            """Slice a 3D model (STL/3MF/STEP) to G-code using PrusaSlicer or OrcaSlicer.

            Args:
                input_path: Path to the input file (STL, 3MF, STEP, OBJ, AMF).
                output_dir: Directory for the output G-code.  Defaults to
                    the system temp directory.
                profile: Path to a slicer profile/config file (.ini or .json).
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"prusa_mini"``).
                slicer_path: Explicit path to the slicer binary.  Auto-detected
                    if omitted.
                material: Filament material for this slice (``"PLA"``,
                    ``"PETG"``, ``"ABS"``, …).  Its density is what the
                    slicer weighs the print with.  Omitted, the spool the
                    printer reports loaded answers, then PLA; the response's
                    ``filament`` block says which.
                auto_center: When True (default), off-bed STLs are translated
                    to a bed-centered copy before slicing.  This prevents the
                    class of crash where origin-centered meshes (common from
                    compose_part_from_primitives / OpenSCAD output) produce
                    sliced gcode with negative X/Y moves that drive the
                    nozzle into the printer frame.  Set False only if you've
                    verified the input is already correctly positioned.
                printer_name: Registered printer this slice is FOR.  Omit for
                    the default printer.  Naming a second machine resolves
                    its profile, its bed and its safety limits — without it,
                    a multi-printer install slices everything for whichever
                    printer is the default.
                placement: Where the part goes when the plate still holds the
                    last print.  ``[x, y]`` in mm (where the part's footprint
                    origin lands), a named region (``"front-left"``,
                    ``"centre"``, ``"back-right"``, …), or ``"keep"`` to leave
                    it where the file puts it.  Omitted, an occupied plate
                    refuses and lists the spots that would work; a clear
                    plate slices as before.  The response's ``placement``
                    block is the clearance verdict, and the sliced file is
                    checked against the plate once more before it is handed
                    on.  The clearance verdict is free; placing and starting
                    a second print on an occupied plate is a kiln-pro feature
                    (https://kiln3d.com/pricing).

            Returns a JSON object with the output G-code path.  The output file
            can then be uploaded to a printer with ``upload_file`` and printed
            with ``start_print``.
            """
            if err := _srv._check_auth("slicer"):
                return err

            try:
                from kiln.slicer import SlicerError, SlicerNotFoundError
                from kiln.slicer_profiles import validate_profile_for_printer

                effective_printer_id, effective_profile = _srv._resolve_slice_profile_context(
                    profile=profile,
                    printer_id=printer_id,
                    printer_name=printer_name,
                )

                # Bed-fit safety gate (Layer 1).  Blocks off-bed / oversized
                # geometry before it hits the slicer.  May auto-translate
                # an origin-centered STL into a bed-centered temp copy.
                # One shared step: the plate gate (a plate that still holds
                # the last print refuses until a spot is named, and the part
                # is moved beside the occupant), the bed-fit gate (which
                # never re-centres a placed part), the slicer, and the
                # second verdict on the sliced file.
                result, slice_err, sinfo = _placed_slice(
                    input_path, effective_printer_id=effective_printer_id,
                    printer_name=printer_name, placement=placement,
                    profile_path=effective_profile, auto_center=auto_center,
                    output_dir=output_dir, slicer_path=slicer_path, material=material,
                    loaded_material=_loaded_material_for(printer_name, material),
                )
                if slice_err is not None:
                    return slice_err
                effective_input, gate_info, place_info = (
                    sinfo["effective_input"], sinfo["bed_fit"], sinfo["placement"],
                )
                response: dict[str, Any] = {
                    "success": True,
                    **result.to_dict(),
                }
                if effective_printer_id:
                    response["printer_id"] = effective_printer_id
                if effective_profile:
                    response["profile_path"] = effective_profile

                # Bambu auto-wrap: Bambu firmware ignores gcode_file MQTT
                # commands and only starts via project_file on .3mf.  Wrap
                # here so callers don't have to know the Bambu-specific
                # convention.  Failure is non-fatal — raw gcode still usable.
                # Use the (possibly centered) STL for thumbnail generation
                # so the LCD preview matches the sliced geometry.
                _gcode_path = result.to_dict().get("output_path")
                if _gcode_path:
                    threemf_path, warning = _auto_wrap_bambu_3mf(
                        _gcode_path, effective_printer_id, effective_input,
                        quiet_start=sinfo.get("quiet_start"), lift_floor_mm=sinfo.get("lift_floor_mm"),
                    )
                    if threemf_path:
                        response["output_3mf_path"] = threemf_path
                        response["output_path"] = threemf_path
                        response["raw_gcode_path"] = _gcode_path
                        # POST-WRAP VERIFICATION: ensure the final 3MF has
                        # both a valid bbox AND a homing sequence before
                        # handing it to the caller.  Incident #0 showed
                        # that a dormant bug in the wrap function could
                        # produce a 3MF without G28 — the safety check
                        # catches that regression class.
                        try:
                            from kiln.printers.bed_fit import (
                                verify_3mf_is_safe_to_print,
                            )
                            safety = verify_3mf_is_safe_to_print(
                                threemf_path, effective_printer_id,
                            )
                            response["safety_verification"] = safety
                            if not safety["ok"]:
                                return _srv._error_dict(
                                    f"Produced 3MF failed safety verification: "
                                    f"{safety.get('error_message', 'unknown issue')}. "
                                    f"Failed checks: {', '.join(safety['failed'])}. "
                                    f"The slicer or wrapper produced unsafe output. "
                                    f"Do NOT upload this file to the printer.",
                                    code=safety.get("error_code", "UNSAFE_3MF"),
                                )
                        except Exception as _exc:
                            _logger.warning(
                                "Post-wrap safety verification skipped: %s",
                                _exc,
                            )
                    if warning:
                        response.setdefault("warnings", []).append(warning)

                if threemf_path:
                    _steer_to_wrapped_upload(
                        response, threemf_path, effective_printer_id,
                    )
                elif _gcode_path:
                    # Every other printer prints this G-code as it stands,
                    # so it leaves here carrying its preview and its weight.
                    _steer_to_complete_gcode(
                        response, _gcode_path, effective_printer_id, effective_input,
                    )

                # Surface the bed-fit result so callers can see if we
                # auto-centered + the translation applied.
                if gate_info.get("gate") != "skipped_no_printer":
                    response["bed_fit"] = gate_info

                # Cross-check slicer profile against printer safety limits.
                # The limits belong to the machine this slice is FOR: checking
                # an aimed slice against the default printer's hotend either
                # invents an incompatibility or misses a real one.
                _target_model = _srv._resolve_target_printer_model(printer_name)
                if _target_model and effective_profile:
                    # Extract profile_id from the profile path or use printer model
                    _profile_id = effective_printer_id or os.path.basename(effective_profile).split("_")[0]
                    if _profile_id:
                        validation = validate_profile_for_printer(_profile_id, _target_model)
                        if validation["warnings"] or validation["errors"]:
                            response["profile_validation"] = validation
                            if validation["errors"]:
                                response["profile_validation_warning"] = (
                                    f"Slicer profile may be incompatible with {_target_model}: "
                                    + "; ".join(validation["errors"])
                                )
                            elif validation["warnings"]:
                                response["profile_validation_warning"] = "Profile compatibility note: " + "; ".join(
                                    validation["warnings"]
                                )

                # Honest-messaging advisory: a multicolor 3MF sliced with
                # a single-filament config prints entirely in one filament.
                # Say so instead of returning a bare green success.
                mc_block, mc_warning = _multicolor_flatten_advisory(
                    input_path, effective_profile, _gcode_path,
                )
                if mc_warning:
                    response["multicolor_flattened"] = mc_block
                    response.setdefault("warnings", []).append(mc_warning)

                # (Slice telemetry is recorded inside slicer.slice_file —
                # the chokepoint every slicing path shares — so no
                # in-body count here: it would double-count this tool
                # while every other path stayed at zero.)

                _attach_placement(response, place_info)
                return response
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(f"Failed to slice model: {exc}", code="SLICER_ERROR")
            except FileNotFoundError as exc:
                return _srv._error_dict(f"Failed to slice model: {exc}", code="FILE_NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in slice_model")
                return _srv._error_dict(f"Unexpected error in slice_model: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # reslice_with_overrides
        # ------------------------------------------------------------------

        _SLICER_INPUT_EXTENSIONS = {".stl", ".3mf", ".step", ".stp", ".obj", ".amf"}

        @mcp.tool()
        def reslice_with_overrides(
            input_path: str,
            printer_id: str | None = None,
            overrides: str | dict[str, Any] | None = None,
            output_dir: str | None = None,
            slicer_path: str | None = None,
            auto_center: bool = True,
            printer_name: str | None = None,
            material: str | None = None,
            placement: str | list[float] | None = None,
        ) -> dict[str, Any]:
            """Reslice a 3D model with custom slicer parameter overrides.

            Accepts a base printer profile and a JSON dict of overrides to customize
            the slice. Common override keys (PrusaSlicer INI format):

              Adhesion: brim_width (mm), skirts (count), skirt_distance (mm)
              Temperature: temperature, first_layer_temperature, bed_temperature
              Speed: perimeter_speed, infill_speed, external_perimeter_speed, first_layer_speed, travel_speed (mm/s)
              Structure: fill_density (e.g. "25%"), fill_pattern (gyroid/grid/honeycomb), layer_height
              Support: support_material (0/1), support_material_buildplate_only (0/1)
              Retraction: retract_length, retract_speed

            Example overrides JSON: {"brim_width": "8", "perimeter_speed": "30", "fill_density": "25%"}

            Use this tool when a print failed due to adhesion, wobble, or quality issues
            and you need to reslice with adjusted settings. Pair with rotate_model to
            also change part orientation before reslicing.

            Requires PrusaSlicer or OrcaSlicer installed locally.
            Use kiln find-slicer or the find_slicer MCP tool to verify.

            Args:
                input_path: Path to the input file (STL, 3MF, STEP, OBJ, AMF).
                printer_id: Printer model ID for bundled profile selection
                    (e.g. ``"prusa_mini"``, ``"bambu_a1"``).
                overrides: Slicer keys to override, as a JSON object or its
                    string form (e.g. ``{"brim_width": "8", "fill_density": "25%"}``).
                output_dir: Directory for the output G-code.  Defaults to the
                    system temp directory.
                slicer_path: Explicit path to the slicer binary.  Auto-detected
                    if omitted.
                printer_name: Registered printer this reslice is FOR.  Omit
                    for the default printer.  Naming a second machine
                    resolves its profile, its bed and its temperature
                    ceilings instead of the default printer's.
                material: Filament material for this slice (``"PLA"``,
                    ``"PETG"``, …); its density is what the slicer weighs
                    the print with.  Omitted, the loaded spool answers,
                    then PLA — the response's ``filament`` block says which.
                placement: Where the part goes when the plate still holds the
                    last print: ``[x, y]`` in mm (the part's footprint
                    origin), a named region (``"front-left"``, ``"centre"``,
                    ``"back-right"``, …), or ``"keep"``.  Omitted, an
                    occupied plate refuses and lists the spots that would
                    work; a clear plate reslices as before.  The response's
                    ``placement`` block is the clearance verdict, checked
                    again on the sliced file.  The clearance verdict is free;
                    placing and starting a second print on an occupied plate
                    is a kiln-pro feature (https://kiln3d.com/pricing).
            """
            if err := _srv._check_auth("slicer"):
                return err


            from kiln.slicer_profiles import (
                profile_with_overrides,
                resolve_slicer_profile,
                validate_profile_for_printer,
            )

            # -- Validate input file --
            input_abs = os.path.abspath(input_path)
            if not os.path.isfile(input_abs):
                return _srv._error_dict(
                    f"Input file not found: {os.path.basename(input_abs)}",
                    code="FILE_NOT_FOUND",
                )

            ext = Path(input_abs).suffix.lower()
            if ext not in _SLICER_INPUT_EXTENSIONS:
                return _srv._error_dict(
                    f"Unsupported input format '{ext}'. Supported: {', '.join(sorted(_SLICER_INPUT_EXTENSIONS))}",
                    code="UNSUPPORTED_FORMAT",
                )

            # -- Parse overrides: a JSON object, its string form, or nothing --
            _parsed, _arg_err = parse_json_object(overrides, "overrides")
            if _arg_err is not None:
                return _arg_err
            parsed_overrides: dict[str, str] = {
                str(k): str(v) for k, v in (_parsed or {}).items()
            }

            try:
                from kiln.slicer import SlicerError, SlicerNotFoundError

                # -- Resolve profile with overrides --
                effective_printer_id = _srv._resolve_printer_profile_id(
                    printer_id, printer_name,
                )

                # -- Calibration overlay: when kiln-pro is installed and the
                # user has a calibrated slicer profile for (printer, material),
                # inject those values into parsed_overrides BEFORE resolve so
                # the slicer's print-time / cost estimates use values the
                # user has personally verified.  No-op for free users.
                # User-supplied overrides ALWAYS win — the helper only fills
                # gaps.
                cal_used: dict[str, Any] | None = None
                if effective_printer_id:
                    # Passing input_path here triggers Pro+ slice-history
                    # recording inside the helper itself — any future
                    # caller of _maybe_overlay_calibration automatically
                    # participates without a separate hook.  The slicer
                    # identity resolves from the same slicer_path the
                    # slice below uses, so the values that get overlaid
                    # belong to the slicer that receives them.
                    parsed_overrides, cal_used = _maybe_overlay_calibration(
                        parsed_overrides, effective_printer_id,
                        input_path=input_path,
                        slicer_name=_resolve_slicer_name(slicer_path),
                    )

                effective_profile: str | None = None
                if effective_printer_id:
                    try:
                        effective_profile = resolve_slicer_profile(
                            effective_printer_id,
                            overrides=parsed_overrides or None,
                        )
                    except Exception as exc:
                        _logger.debug(
                            "Profile resolution failed for %s: %s",
                            effective_printer_id,
                            exc,
                        )

                # Overrides with NO resolvable printer profile used to be
                # dropped on the floor while the response still stamped
                # applied_overrides — a receipt for work that never happened
                # (measured 2026-08-06: layer_height 0.2 vs 0.4 both sliced
                # at the default 0.3).  One shared helper now guarantees the
                # overrides reach the slicer whatever the base profile is.
                effective_profile = profile_with_overrides(
                    effective_profile, parsed_overrides,
                )

                # -- Safety-validate temperature overrides --
                validation_result: dict[str, Any] | None = None
                _temp_keys = {
                    "temperature",
                    "first_layer_temperature",
                    "bed_temperature",
                    "first_layer_bed_temperature",
                }
                has_temp_overrides = bool(parsed_overrides and _temp_keys & parsed_overrides.keys())

                # Temperature ceilings belong to the TARGET machine — an
                # override checked against the default printer's hotend is
                # the wrong ceiling for the machine that will heat it.
                _target_model = _srv._resolve_target_printer_model(printer_name)
                if has_temp_overrides and effective_printer_id and _target_model:
                    validation_result = validate_profile_for_printer(effective_printer_id, _target_model)

                # -- Plate gate, bed-fit gate, slice, second verdict: the
                # one shared step (see _placed_slice) --
                result, slice_err, sinfo = _placed_slice(
                    input_abs, effective_printer_id=effective_printer_id,
                    printer_name=printer_name, placement=placement,
                    profile_path=effective_profile, auto_center=auto_center,
                    output_dir=output_dir, slicer_path=slicer_path, material=material,
                    loaded_material=_loaded_material_for(printer_name, material),
                )
                if slice_err is not None:
                    return slice_err
                effective_input, gate_info, place_info = (
                    sinfo["effective_input"], sinfo["bed_fit"], sinfo["placement"],
                )

                response: dict[str, Any] = {
                    "success": True,
                    **result.to_dict(),
                }
                if effective_printer_id:
                    response["printer_id"] = effective_printer_id
                if effective_profile:
                    response["profile_path"] = effective_profile
                if parsed_overrides:
                    response["applied_overrides"] = parsed_overrides
                if gate_info.get("gate") != "skipped_no_printer":
                    response["bed_fit"] = gate_info
                if cal_used is not None:
                    response["calibration_used"] = cal_used

                # Bambu auto-wrap (same logic as slice_model) so callers
                # don't have to know raw gcode won't start on Bambu.
                _gcode_path = result.to_dict().get("output_path")
                _wrap_path: str | None = None
                if _gcode_path:
                    threemf_path, warning = _auto_wrap_bambu_3mf(
                        _gcode_path, effective_printer_id, effective_input,
                        quiet_start=sinfo.get("quiet_start"), lift_floor_mm=sinfo.get("lift_floor_mm"),
                    )
                    _wrap_path = threemf_path
                    if threemf_path:
                        response["output_3mf_path"] = threemf_path
                        response["output_path"] = threemf_path
                        response["raw_gcode_path"] = _gcode_path
                        # Post-wrap safety verification — parity with slice_model
                        try:
                            from kiln.printers.bed_fit import (
                                verify_3mf_is_safe_to_print,
                            )
                            safety = verify_3mf_is_safe_to_print(
                                threemf_path, effective_printer_id,
                            )
                            response["safety_verification"] = safety
                            if not safety["ok"]:
                                return _srv._error_dict(
                                    f"Produced 3MF failed safety verification: "
                                    f"{safety.get('error_message', 'unknown issue')}. "
                                    f"Failed checks: {', '.join(safety['failed'])}. "
                                    f"Do NOT upload this file.",
                                    code=safety.get("error_code", "UNSAFE_3MF"),
                                )
                        except Exception as _exc:
                            _logger.warning(
                                "Post-wrap safety verification skipped: %s", _exc,
                            )
                    else:
                        # Not a Bambu: the G-code itself is the file that
                        # goes to the printer, so it leaves this door
                        # carrying its preview and its weight, the same as
                        # the one slice_model recommends.
                        _steer_to_complete_gcode(
                            response, _gcode_path, effective_printer_id, effective_input,
                        )
                    if warning:
                        response.setdefault("warnings", []).append(warning)

                # Attach validation warnings/errors when present
                if validation_result and (validation_result["warnings"] or validation_result["errors"]):
                    response["profile_validation"] = validation_result
                    if validation_result["errors"]:
                        response["profile_validation_warning"] = (
                            f"Temperature overrides may be unsafe for {_target_model}: "
                            + "; ".join(validation_result["errors"])
                        )
                    elif validation_result["warnings"]:
                        response["profile_validation_warning"] = "Profile compatibility note: " + "; ".join(
                            validation_result["warnings"]
                        )

                # Multicolor-flatten advisory — same wire as slice_model.
                mc_block, mc_warning = _multicolor_flatten_advisory(
                    input_abs, effective_profile, _gcode_path,
                )
                if mc_warning:
                    response["multicolor_flattened"] = mc_block
                    response.setdefault("warnings", []).append(mc_warning)

                _attach_placement(response, place_info)
                return response
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to reslice model: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(
                    f"Failed to reslice model: {exc}",
                    code="SLICER_ERROR",
                )
            except FileNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to reslice model: {exc}",
                    code="FILE_NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in reslice_with_overrides")
                return _srv._error_dict(
                    f"Unexpected error in reslice_with_overrides: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # find_slicer
        # ------------------------------------------------------------------

        @mcp.tool(name="find_slicer")
        def find_slicer_tool() -> dict:
            """Check if a slicer (PrusaSlicer/OrcaSlicer) is available on the system.

            Returns the slicer path, name, and version if found.
            """
            try:
                from kiln.slicer import SlicerNotFoundError
                from kiln.slicer import find_slicer as _find_slicer

                info = _find_slicer()
                return {
                    "success": True,
                    **info.to_dict(),
                }
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to find slicer: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in find_slicer_tool")
                return _srv._error_dict(f"Unexpected error in find_slicer_tool: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # slice_and_print
        # ------------------------------------------------------------------

        @mcp.tool()
        def slice_and_print(
            input_path: str,
            printer_name: str | None = None,
            profile: str | None = None,
            printer_id: str | None = None,
            material: str | None = None,
            auto_center: bool = True,
            metadata: dict | None = None,
            skip_validation: bool = False,
            preview_token: str | None = None,
            placement: str | list[float] | None = None,
        ) -> dict:
            """Slice a 3D model (STL/3MF) + upload + print in one step (basic pipeline).

            For a more comprehensive pipeline with validation and profile auto-detection,
            use ``run_quick_print``. For custom slicer overrides, use ``run_reslice_and_print``.
            Automatically analyzes bed adhesion and adds brim/raft when needed
            based on model geometry, material warp tendency, and printer type.
            This adhesion intelligence only activates when no custom profile is
            supplied.

            Pre-print validation gate: mesh inputs (.stl/.obj/.3mf/.step/.glb)
            run through Kiln's full validation pipeline before slicing —
            format check, watertight check, auto-repair, printability scoring
            (0-100), bed-fit, and material checks.  Designs that fail the gate
            are blocked before reaching the printer; auto-repaired meshes are
            sliced from the repaired path.  Pass ``skip_validation=True`` to
            bypass (e.g. for already-validated meshes or pre-sliced 3MFs).

            Args:
                input_path: Path to the 3D model file (STL, 3MF, STEP, etc.).
                printer_name: Target printer name.  Omit for the default printer.
                profile: Path to a slicer profile/config file.
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"prusa_mini"``).
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``).  Its
                    density is what the slicer weighs the print with, and it
                    steers the automatic brim/raft decision.  Omitted, the
                    spool the printer reports loaded answers, then PLA — the
                    response's ``slice.filament`` says which.
                metadata: Optional dict of pass-through fields.  When
                    kiln-pro (https://kiln3d.com) is installed it
                    consumes keys here to generate a printable
                    assembly manual alongside the print, surfacing it
                    under ``response["assembly_manual"]``.  Without
                    kiln-pro the metadata is silently ignored.
                    Recognised keys (all optional):
                    ``assembly_json``, ``manual_output_dir``,
                    ``manual_design_name``, ``manual_branding``,
                    ``manual_co_brand_name``, ``manual_languages``,
                    ``manual_cover_language``.  Multi-language and
                    co-brand are kiln-pro Business+ features
                    (https://kiln3d.com/pricing).
                skip_validation: Bypass the pre-print validation gate.
                    Defaults to False — designs are pre-tested for
                    printability before they reach the printer.  Set to
                    True only when the caller has already validated the
                    mesh (e.g. ``validate_and_prepare`` was just called)
                    or when the input is a pre-sliced 3MF the validator
                    can't introspect.
                placement: Where the part goes when the plate still holds the
                    last print: ``[x, y]`` in mm (the part's footprint
                    origin), a named region (``"front-left"``, ``"centre"``,
                    ``"back-right"``, …), or ``"keep"``.  Omitted, an
                    occupied plate refuses before anything is sliced and
                    lists the spots that would work; a clear plate prints as
                    before.  The response's ``placement`` block is the
                    clearance verdict, checked again on the sliced file
                    before upload.  The clearance verdict is free; placing
                    and starting a second print on an occupied plate is a
                    kiln-pro feature (https://kiln3d.com/pricing).

            Combines ``slice_model``, ``upload_file``, and ``start_print`` into
            a single action.

            Branch on ``print_start`` — one field, three values, and the
            nested ``print`` block carries the identical pair so the two
            halves cannot disagree:

            - ``"started"``: the printer, asked after the command, is printing.
            - ``"accepted"``: the command was sent and not refused, and the
              machine has not confirmed it is running.  Normal during the
              start-up transient (homing, AMS load, calibration).  Call
              ``printer_status()`` to watch it start.
            - ``"failed"``: the printer, asked after the command, is idle or
              errored — it did not take the job.

            ``success`` is ``False`` only for ``"failed"``.
            """
            if err := _srv._check_auth("print"):
                return err
            try:
                from kiln.printers import PrinterError
                from kiln.registry import PrinterNotFoundError
                from kiln.slicer import SlicerError, SlicerNotFoundError
                from kiln.slicer_profiles import (
                    profile_with_overrides,
                    resolve_slicer_profile,
                    start_gcode_override_from_printer,
                )

                effective_printer_id, effective_profile = _srv._resolve_slice_profile_context(
                    profile=profile,
                    printer_id=printer_id,
                    printer_name=printer_name,
                )

                # The as-given input, for the multicolor-flatten advisory:
                # the validation gate below may swap input_path for a
                # repaired copy that no longer carries the color story.
                _multicolor_source = input_path

                # --- Pro+ calibration overlay ---
                # Mirror slice_model: when kiln-pro is installed AND the user
                # has a HIGH/MEDIUM-tier calibrated profile for this printer,
                # overlay calibrated values (extrusion_multiplier,
                # filament_max_volumetric_speed, pressure_advance,
                # xy_size_compensation, filament_retraction_length) onto the
                # slicer args.  Free tier no-op.  Re-resolves the profile so
                # the slicer call actually uses the calibrated values.
                parsed_overrides: dict[str, str] = {}
                cal_used = None
                if effective_printer_id:
                    # This tool takes no explicit slicer path, so the
                    # identity comes from the same auto-detection
                    # slice_file performs further down.
                    parsed_overrides, cal_used = _maybe_overlay_calibration(
                        parsed_overrides, effective_printer_id,
                        material=material,
                        input_path=input_path,
                        slicer_name=_resolve_slicer_name(),
                    )
                    if parsed_overrides:
                        try:
                            effective_profile = resolve_slicer_profile(
                                effective_printer_id,
                                overrides=parsed_overrides,
                            )
                        except Exception as _exc:
                            _logger.debug(
                                "Calibration profile re-resolution failed for %s: %s",
                                effective_printer_id, _exc,
                            )

                # --- Auto-material from AMS if not specified ---
                # The active tray when the unit names one, else the first
                # LOADED tray (the A1 / AMS Lite keeps tray_now="255" with
                # trays loaded) — read through the one helper every slicing
                # door uses, so the adhesion/validation hint here and the
                # density the slicer is handed come from the same reading.
                # Declared and loaded are kept apart: the slice below is
                # told both, and its response says which one weighed the
                # print.  (Routing is handled separately by
                # _resolve_use_ams below — this only sets the material
                # string.)
                declared_material = material
                loaded_material: str | None = None
                if material is None:
                    loaded_material = _loaded_material_for(printer_name, material)
                    if loaded_material:
                        material = loaded_material
                        _logger.debug("Auto-detected material from AMS: %s", material)

                # --- Pre-print validation gate ---
                # Mesh inputs are pre-tested for printability (manifold,
                # walls, overhangs, bridges, bed-fit, material) before they
                # reach the printer.  Auto-repair on non-manifold; blocks
                # designs that fail with a clear next_action.  Bypass with
                # skip_validation=True (e.g. pre-sliced 3MFs).
                validation_summary: dict | None = None
                if not skip_validation:
                    try:
                        from kiln.plugins._validation_pipeline_internals import (
                            _SUPPORTED_FORMATS,
                        )
                        from kiln.plugins.validation_pipeline_tools import (
                            run_full_validation_pipeline,
                        )

                        _ext = os.path.splitext(input_path)[1].lower()
                        if _ext in _SUPPORTED_FORMATS:
                            val_report = run_full_validation_pipeline(
                                input_path,
                                printer_id=effective_printer_id or "",
                                material=material or "",
                            )
                            if not val_report.get("ready_to_print", True):
                                score = val_report.get("printability_score", 0)
                                summary = val_report.get("summary", "Validation failed")
                                err_resp = _srv._error_dict(
                                    f"Mesh failed pre-print validation "
                                    f"(score {score}/100): {summary} "
                                    f"Pass skip_validation=True to bypass.",
                                    code="VALIDATION_FAILED",
                                )
                                err_resp["validation"] = val_report
                                return err_resp

                            # Slice the (possibly repaired/scaled) validated mesh.
                            validated_path = val_report.get("validated_path") or input_path
                            if validated_path and validated_path != input_path:
                                _logger.info(
                                    "slice_and_print: using validated path %s (repaired=%s)",
                                    validated_path,
                                    val_report.get("repaired", False),
                                )
                                input_path = validated_path

                            validation_summary = {
                                "printability_score": val_report.get("printability_score"),
                                "ready_to_print": val_report.get("ready_to_print"),
                                "repaired": val_report.get("repaired"),
                                "summary": val_report.get("summary"),
                            }
                    except ImportError:
                        _logger.debug(
                            "Validation pipeline unavailable, proceeding without",
                            exc_info=True,
                        )
                    except Exception:
                        # An infrastructure-side bug in validation must not
                        # block users from printing.  Log and proceed.
                        _logger.warning(
                            "Validation pipeline raised — proceeding without gate",
                            exc_info=True,
                        )

                # --- Auto-adhesion: analyse model and inject brim/raft if needed ---
                adhesion_rec = None
                adhesion_overrides: dict[str, str] = {}
                if profile is None and input_path.lower().endswith((".stl", ".obj", ".3mf")):
                    try:
                        from kiln.printability import (
                            analyze_printability as _analyze_printability,
                        )
                        from kiln.printability import (
                            is_bedslinger,
                            recommend_adhesion,
                        )

                        report = _analyze_printability(
                            input_path,
                            material=material or "pla",
                            printer_id=effective_printer_id or None,
                        )
                        if report.bed_adhesion:
                            has_enclosure = False
                            is_bs = False
                            if effective_printer_id:
                                is_bs = is_bedslinger(effective_printer_id)
                                try:
                                    from kiln.printer_intelligence import get_printer_intel

                                    intel = get_printer_intel(effective_printer_id)
                                    if intel:
                                        has_enclosure = intel.get("has_enclosure", False)
                                except Exception:
                                    pass

                            rec = recommend_adhesion(
                                report.bed_adhesion,
                                material=material or "PLA",
                                has_enclosure=has_enclosure,
                                is_bedslinger_printer=is_bs,
                                model_height_mm=report.model_height_mm,
                            )
                            if rec.brim_width_mm > 0 or rec.use_raft:
                                adhesion_rec = rec.to_dict()
                                adhesion_overrides = dict(rec.slicer_overrides)
                                _logger.info(
                                    "Auto-adhesion: brim=%dmm raft=%s (%s)",
                                    rec.brim_width_mm,
                                    rec.use_raft,
                                    rec.rationale,
                                )
                    except Exception:
                        _logger.debug("Auto-adhesion analysis failed, proceeding without", exc_info=True)

                # Bambu printers: wrap_gcode_as_3mf expects M83 (relative extrusion)
                # and provides its own start/end gcode, so override PrusaSlicer defaults.
                #
                # Read the TARGET's type, not the default connection's.  The
                # wrap decision below is made per-adapter; deciding this half
                # from a global meant an aimed slice could have its start
                # block emptied for a machine that is never wrapped (no
                # homing, no heat-up), or be wrapped for a Bambu while still
                # carrying absolute E and PrusaSlicer's own start gcode.
                target_type = _srv._resolve_target_printer_type(printer_name)
                if target_type == "bambu":
                    adhesion_overrides["use_relative_e_distances"] = "1"
                    adhesion_overrides["start_gcode"] = ""
                    adhesion_overrides["end_gcode"] = ""

                # Prefer per-model speeds when printer_id is available
                if effective_printer_id:
                    try:
                        from kiln.printer_intelligence import get_slicer_speed_overrides

                        model_speeds = get_slicer_speed_overrides(effective_printer_id)
                        if model_speeds:
                            for k, v in model_speeds.items():
                                if k not in adhesion_overrides:
                                    adhesion_overrides[k] = v
                    except (ImportError, Exception):
                        pass  # fall through to per-type defaults below

                # Inject printer-aware speed overrides — again for the target:
                # a Bambu's 250mm/s infill is not a speed to hand an Ender 3
                # just because a Bambu happens to be the default printer.
                if target_type in _srv._PRINTER_SPEED_OVERRIDES:
                    for k, v in _srv._PRINTER_SPEED_OVERRIDES[target_type].items():
                        if k not in adhesion_overrides:  # don't override explicit user settings
                            adhesion_overrides[k] = v

                # --- Printer's own start routine (kiln-pro handoff) ---
                # When the registered machine's Klipper config defines a
                # PRINT_START / START_PRINT macro whose parameters can be
                # supplied safely, the start block becomes a call to that
                # macro — its chamber heat, bed mesh and purge included —
                # instead of the generic warm-up floor.  Free-tier no-op;
                # declines (keeping the floor) on any doubt.  The Bambu
                # block above already stated start_gcode explicitly, and a
                # stated value outranks the handoff by contract.
                start_handoff: str | None = None
                try:
                    _sg_adapter = _srv._resolve_adapter(printer_name)
                except Exception:
                    _sg_adapter = None
                if _sg_adapter is not None:
                    _sg_patch, _sg_reason = start_gcode_override_from_printer(
                        _sg_adapter,
                        effective_printer_id,
                        {**parsed_overrides, **adhesion_overrides},
                    )
                    if _sg_patch:
                        adhesion_overrides.update(_sg_patch)
                        start_handoff = _sg_reason.removeprefix("handoff:")
                    else:
                        _logger.debug("start-gcode handoff declined: %s", _sg_reason)

                # Re-resolve profile with adhesion overrides merged in.
                # The printer-id path is the richer merge (bundled profile +
                # overrides); the helper is the floor for everything else.
                # That floor is load-bearing, not tidiness: a Bambu whose
                # MODEL is unset or unmappable ("bambu", "my-printer") has a
                # known printer TYPE and no profile id, and this block used
                # to drop its overrides — including the three settings
                # wrap_gcode_as_3mf requires (relative extrusion, empty
                # start/end gcode).  The slice then came out with absolute E
                # and PrusaSlicer's own start gcode, and got wrapped into a
                # Bambu 3MF that assumes the opposite.  Wrong file, not
                # untuned settings.
                if adhesion_overrides:
                    # Seeded with the calibration overrides, because this
                    # re-resolve REPLACES the profile resolved above rather
                    # than patching it: resolving from the bundled profile
                    # with only adhesion_overrides silently dropped every
                    # calibrated value (pressure_advance,
                    # extrusion_multiplier, ...) whenever this block fired —
                    # while the response's calibration_used block still
                    # claimed they were applied.  Adhesion and the explicit
                    # Bambu-wrap keys win any conflict; calibration only
                    # fills the keys they did not set, same precedence it
                    # had at the first resolve.
                    final_overrides = {**parsed_overrides, **adhesion_overrides}
                    merged: str | None = None
                    if effective_printer_id:
                        try:
                            merged = resolve_slicer_profile(
                                effective_printer_id, overrides=final_overrides,
                            )
                        except Exception:
                            _logger.debug("Profile override injection failed", exc_info=True)
                    effective_profile = merged or profile_with_overrides(
                        effective_profile, final_overrides,
                    )

                # --- Bed-fit safety gate (Layer 1) ---
                # Blocks off-bed / oversized geometry before slicing (auto-
                # orients to fit if it can), and refuses a material the printer
                # physically can't melt.  May auto-translate an origin-centered
                # STL to bed-centered.
                # Plate gate, bed-fit gate, slice, second verdict: the one
                # shared step (see _placed_slice).
                result, slice_err, sinfo = _placed_slice(
                    input_path, effective_printer_id=effective_printer_id,
                    printer_name=printer_name, placement=placement,
                    profile_path=effective_profile, auto_center=auto_center,
                    material_id=material, material=declared_material,
                    loaded_material=loaded_material,
                )
                if slice_err is not None:
                    return slice_err
                effective_input, place_info = sinfo["effective_input"], sinfo["placement"]

                adapter = _srv._resolve_adapter(printer_name)

                # A plate that still holds the last print is never started
                # onto: the file carries the maker's own start sequence,
                # which drives the head across the plate.  Refused here,
                # before the upload, with the slice and its verdict attached
                # so the work is not lost.
                quiet_plan = sinfo.get("quiet_start")
                # A placed slice with a plan is not refused here: the file it
                # becomes carries the plan, and the start gate judges it
                # against the printer at the moment of the start.
                if quiet_plan is None and (block := _plate_state.start_refusal(adapter)):
                    block["slice"] = result.to_dict()
                    _attach_placement(block, place_info)
                    return block

                # Bambu printers need PrusaSlicer output wrapped in a 3MF with
                # the proprietary BambuStudio start/end gcode.  The adapter
                # exposes wrap_gcode_as_3mf() for this.  Pass the (possibly
                # bed-centered) STL so the LCD thumbnail matches the sliced
                # geometry.
                from kiln.printers.upload_prep import prepare_upload_for_adapter

                upload_path, _wrapped = prepare_upload_for_adapter(
                    adapter,
                    result.output_path,
                    stl_paths=(
                        [effective_input] if effective_input.lower().endswith(".stl") else None
                    ),
                    quiet_start=quiet_plan,
                    lift_floor_mm=sinfo.get("lift_floor_mm"),
                )
                if quiet_plan is not None and (
                    block := _plate_state.start_refusal(adapter, file_name=os.path.basename(upload_path), local_path=upload_path)
                ):
                    block["slice"] = result.to_dict()
                    _attach_placement(block, place_info)
                    return block

                # Post-wrap safety verification — refuse to upload a 3MF
                # that has no homing sequence or off-bed coordinates.
                # Last gate before bytes reach the printer via FTPS.
                try:
                    from kiln.printers.bed_fit import verify_3mf_is_safe_to_print
                    if upload_path.lower().endswith(".3mf"):
                        safety = verify_3mf_is_safe_to_print(
                            upload_path, effective_printer_id,
                        )
                        if not safety["ok"]:
                            return _srv._error_dict(
                                f"Sliced 3MF failed safety verification before upload: "
                                f"{safety.get('error_message', 'unknown issue')}. "
                                f"Failed checks: {', '.join(safety['failed'])}. "
                                f"This would have been the incident #0 class of crash.",
                                code=safety.get("error_code", "UNSAFE_3MF"),
                            )
                except Exception as _exc:
                    _logger.warning("slice_and_print safety check skipped: %s", _exc)

                upload = adapter.upload_file(upload_path)
                file_name = upload.file_name or os.path.basename(upload_path)

                # Mandatory pre-flight safety gate before starting print.
                safety_printer = _srv._resolve_effective_printer_name(printer_name)
                if block := _srv._emergency_latch_error("slice_and_print", safety_printer):
                    return block
                pf = unwrap_tool_result(_srv.preflight_check(printer_name=printer_name))
                if not pf.get("ready", False):
                    _srv._audit(
                        "slice_and_print",
                        "preflight_failed",
                        details={
                            "file": file_name,
                            "summary": pf.get("summary", ""),
                        },
                    )
                    return _srv._error_dict(
                        pf.get("summary", "Pre-flight checks failed"),
                        code="PREFLIGHT_FAILED",
                    )

                # --- AMS auto-routing for Bambu printers ---
                # Silent fallthrough to the external-spool feed path caused
                # production failures (error 0300-8015 "filament on external
                # spool has run out") when users had AMS trays loaded but
                # nothing on the external spool.  Delegate to the shared
                # ``_resolve_use_ams`` helper so this matches the behaviour
                # of the ``start_print`` MCP tool exactly.
                print_kwargs: dict[str, Any] = {}
                ams_routing: dict[str, Any] | None = None
                ams_routing_warnings: list[str] = []
                # Ask the adapter that will receive the job, whatever it is.
                # This block used to run only for a Bambu, which made the
                # colour-mismatch refusal below VENDOR-gated: a four-colour
                # file at a Klipper MMU sailed through.  ``_resolve_use_ams``
                # now reads every kind of unit (and says when it cannot), so
                # the refusal is about the hardware, not the brand.
                ams_decision = _srv._resolve_use_ams(
                    "auto", None, adapter, material=material,
                    # The sliced file says which colours it wants, so
                    # each extruder routes to the tray of that colour.
                    file_path=upload_path,
                )
                ams_routing_warnings = list(ams_decision.get("warnings") or [])
                if ams_decision.get("blocked"):
                    return _srv._error_dict(
                        " ".join(ams_routing_warnings) or "AMS routing blocked.",
                        code="AMS_COLOR_MISMATCH",
                        retryable=False,
                        extra={"ams_plan": ams_decision.get("plan")},
                    )
                # Refuse to silent-route when AMS state is ambiguous
                # (hardware bits say AMS present but no tray state, or
                # probe errored out).  Returning an error envelope
                # here blocks the print BEFORE upload instead of
                # silently routing to the wrong filament feed path.
                # Memory rule: "always route to AMS when printer has
                # one — never silent external-spool fallthrough".
                if ams_decision.get("ambiguous") and not ams_decision.get("use_ams"):
                    return _srv._error_dict(
                        "AMS routing is ambiguous — hardware reports AMS "
                        "present but no tray state is available.  Refusing "
                        "to silently route to external spool (which would "
                        "fail with Bambu error 0300-8015 if nothing is "
                        "loaded there).  Retry in a few seconds for the "
                        "MQTT cache to refresh, or call start_print() "
                        "directly with use_ams='true' and an explicit "
                        "ams_mapping=[<tray id>]. "
                        + " ".join(ams_routing_warnings),
                        code="AMS_STATE_AMBIGUOUS",
                    )
                if ams_decision.get("use_ams"):
                    print_kwargs["use_ams"] = True
                    mapping = ams_decision.get("ams_mapping")
                    if mapping is not None:
                        print_kwargs["ams_mapping"] = mapping
                    ams_routing = {
                        "routed": "ams",
                        "ams_mapping": mapping,
                        "warnings": ams_routing_warnings,
                    }
                    if ams_decision.get("plan"):
                        ams_routing["plan"] = ams_decision["plan"]
                elif ams_decision.get("multi_material"):
                    # A unit Kiln reads but does not drive: say what it saw.
                    ams_routing = {
                        "routed": "printer_owned_unit",
                        "multi_material": ams_decision["multi_material"],
                        "warnings": ams_routing_warnings,
                    }
                elif _srv._resolve_target_printer_type(printer_name, adapter) == "bambu":
                    ams_routing = {
                        "routed": "external_spool",
                        "warnings": ams_routing_warnings,
                    }

                # Pass local 3MF path so bambu.py can compute MD5 + detect
                # multi-material plates (supersedes single-tray routing above
                # when the 3MF explicitly declares multiple filaments).
                if upload_path.lower().endswith(".3mf") and os.path.isfile(upload_path):
                    print_kwargs["local_file_path"] = upload_path
                if quiet_plan is not None:
                    # Every switch the plan names, off -- the adapter template
                    # sends them off from the file's contract anyway; said
                    # here too so the command the tool audits is the one sent.
                    print_kwargs.update({name: False for name in (quiet_plan.get("flags") or {})})
                    for how in (quiet_plan.get("switched_off") or {}).values():
                        flag = str(how).split("=", 1)[0].strip()
                        if flag:
                            print_kwargs[flag] = False

                # ``sent_at`` is what lets the verdict below tell a reading
                # about THIS command from the printer's last word about the
                # previous job.  Capture it before the command, not after.
                # You chose a file; Kiln chose how it sits on the plate.
                # Validated against the INPUT model, which is what a user can
                # actually preview before calling this.
                if block := _srv._preview_gate_error(
                    "slice_and_print", input_path, preview_token,
                    printer_name=printer_name,
                ):
                    return block
                sent_at = time.monotonic()
                print_result = adapter.start_print(file_name, **print_kwargs)
                _srv._note_print_started(adapter)

                base_name = os.path.basename(input_path)
                verdict = resolve_print_start(
                    adapter, print_result, sent_at=sent_at, file_name=base_name,
                    vendor_start_block=quiet_plan is None,
                )
                if verdict.confirmed:
                    outer_message = f"Sliced, uploaded, and started printing {base_name}."
                elif verdict.ok:
                    outer_message = (
                        f"Sliced, uploaded, and sent the print command for "
                        f"{base_name}. The printer has not confirmed it is "
                        f"running yet — call printer_status() to watch it start."
                    )
                else:
                    outer_message = (
                        f"Sliced and uploaded {base_name}, but the printer did "
                        f"not start it. {verdict.message}"
                    )

                resp: dict[str, Any] = {
                    "success": verdict.ok,
                    "print_start": verdict.state,
                    "slice": result.to_dict(),
                    "upload": upload.to_dict(),
                    "print": verdict.to_dict(),
                    "printer_id": effective_printer_id,
                    "profile_path": effective_profile,
                    "message": outer_message,
                }
                # Top-level beside the message, as start_print carries it:
                # this door composes its own message, so the reader would
                # otherwise have to open the nested print block to learn
                # that the coming slam is the filament cutter.
                if verdict.what_you_will_see:
                    resp["what_you_will_see"] = list(verdict.what_you_will_see)
                if validation_summary is not None:
                    resp["validation"] = validation_summary
                if adhesion_rec:
                    resp["adhesion"] = adhesion_rec
                if start_handoff:
                    resp["start_gcode_source"] = (
                        f"{start_handoff} — the printer's own start routine"
                    )
                if ams_routing is not None:
                    resp["ams_routing"] = ams_routing
                if ams_routing_warnings:
                    resp["warnings"] = ams_routing_warnings
                if cal_used is not None:
                    resp["calibration_used"] = cal_used
                _attach_placement(resp, place_info)

                # Multicolor-flatten advisory — same wire as slice_model.
                # The print already started (warn, never block); the user
                # can still cancel before wasting a spool.
                mc_block, mc_warning = _multicolor_flatten_advisory(
                    _multicolor_source, effective_profile, result.output_path,
                )
                if mc_warning:
                    resp["multicolor_flattened"] = mc_block
                    resp.setdefault("warnings", []).append(mc_warning)

                # kiln-pro hook: when installed, generate an assembly
                # manual alongside the print and add it to the
                # response.  No-op when kiln-pro isn't installed.
                if metadata and metadata.get("assembly_json"):
                    auto_manual = _maybe_auto_assembly_manual(metadata)
                    if auto_manual is not None:
                        resp["assembly_manual"] = auto_manual

                # Nozzle capacity advisory — when kiln-pro is installed
                # AND the slice produced a filament-grams estimate, run
                # the wear-envelope projection and attach an advisory
                # block.  Sibling of the preflight_check wire — slice_and_print
                # is the entry point users hit when bypassing preflight.
                # Free tier silently skips.
                try:
                    from kiln import _pro_nozzle_bridge

                    _planned_grams = 0.0
                    _slicer_estimate = resp.get("filament_grams") or resp.get("filament_weight_g")
                    if _slicer_estimate:
                        _planned_grams = float(_slicer_estimate)
                    _printer_for_nozzle = printer_name or printer_id or ""
                    if _printer_for_nozzle and _planned_grams > 0:
                        _nozzle_verdict = _pro_nozzle_bridge.consult_capacity(
                            printer_id=_printer_for_nozzle,
                            planned_grams=_planned_grams,
                            filament_material=material or "",
                        )
                        from kiln.nozzle_milestones import is_flagged, notice_for

                        if _nozzle_verdict is not None:
                            # Once per rung per nozzle, never per slice.
                            _nz_notice = notice_for(_printer_for_nozzle, _nozzle_verdict)
                            if _nz_notice is not None:
                                resp["nozzle_capacity_advisory"] = {**_nz_notice, "advisory": True}
                        elif is_flagged(_printer_for_nozzle):
                            # Kiln could not ask, and this nozzle was already
                            # named as wearing: say so rather than reading as
                            # a nozzle with life to spare.
                            _nz_gap = _pro_nozzle_bridge.nozzle_unchecked(_printer_for_nozzle)
                            if _nz_gap is not None:
                                resp["nozzle_check"] = _nz_gap
                except Exception:
                    pass  # Nozzle bridge unavailable — silently skip.

                return resp
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice and print: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(f"Failed to slice and print: {exc}", code="SLICER_ERROR")
            except PrinterNotFoundError:
                return _srv._error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
            except (PrinterError, RuntimeError, FileNotFoundError) as exc:
                return _srv._error_dict(
                    f"Failed to slice and print: {exc}. Check the input file and printer connection."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in slice_and_print")
                return _srv._error_dict(f"Unexpected error in slice_and_print: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # list_slicer_profiles
        # ------------------------------------------------------------------

        @mcp.tool(name="list_slicer_profiles")
        def list_slicer_profiles_tool() -> dict:
            """List all bundled slicer profiles for supported printers.

            Returns profile IDs, display names, recommended slicer, and the
            minimum license tier required for each.  Free-tier profiles can be
            used by everyone; PRO profiles require a Kiln Pro license.

            Use with ``get_slicer_profile`` to see full settings, or
            ``slice_model`` with printer_id for auto-profile selection.
            """
            if err := _srv._check_auth("slicer"):
                return err
            try:
                from kiln.slicer_profiles import get_slicer_profile, list_slicer_profiles

                ids = list_slicer_profiles()
                profiles = []
                for pid in ids:
                    try:
                        p = get_slicer_profile(pid)
                        profiles.append(
                            {
                                "id": p.id,
                                "display_name": p.display_name,
                                "slicer": p.slicer,
                                "tier": p.tier,
                            }
                        )
                    except KeyError:
                        continue
                return {"success": True, "count": len(profiles), "profiles": profiles}
            except Exception as exc:
                _logger.exception("Unexpected error in list_slicer_profiles_tool")
                return _srv._error_dict(f"Unexpected error in list_slicer_profiles_tool: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # get_slicer_profile
        # ------------------------------------------------------------------

        @mcp.tool(name="get_slicer_profile")
        def get_slicer_profile_tool(printer_id: str) -> dict:
            """Get the full bundled slicer profile for a printer model.

            Returns all INI settings (layer height, speeds, temps, retraction, etc.)
            and the recommended slicer.  Free-tier profiles (default, ender3,
            prusa_mk3s, klipper_generic) are available to all users.  Premium
            profiles require a Kiln Pro license.

            Args:
                printer_id: Printer model identifier (e.g. ``"ender3"``,
                    ``"bambu_x1c"``, ``"creality_k1_max"``).
            """
            if err := _srv._check_auth("slicer"):
                return err
            try:
                from kiln.slicer_profiles import get_slicer_profile, slicer_profile_to_dict

                profile = get_slicer_profile(printer_id)

                # Gate premium profiles behind PRO license
                if profile.tier == "pro":
                    ok, message = _srv.check_tier(_srv.LicenseTier.PRO)
                    if not ok:
                        from kiln.tiers_and_terms import (
                            signin_hint_fields,
                            tier_required_message,
                        )

                        return {
                            "success": False,
                            "error": tier_required_message(
                                f"The '{profile.display_name}' slicer profile",
                                "pro",
                                "Free-tier profiles available: default, "
                                "ender3, prusa_mk3s, klipper_generic",
                            ),
                            "code": "LICENSE_REQUIRED",
                            "required_tier": "pro",
                            "upgrade_url": "https://kiln3d.com/pricing",
                            **signin_hint_fields(),
                        }

                return {"success": True, "profile": slicer_profile_to_dict(profile)}
            except KeyError:
                return _srv._error_dict(
                    f"No slicer profile for '{printer_id}' and no default available.",
                    code="NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in get_slicer_profile_tool")
                return _srv._error_dict(f"Unexpected error in get_slicer_profile_tool: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered slicer tools")


plugin = _SlicerToolsPlugin()
