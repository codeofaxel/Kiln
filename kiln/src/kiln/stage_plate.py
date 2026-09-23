"""The plate the 3D stage draws under a model.

WHY THIS EXISTS
---------------
Kiln's stage stands every design on a print bed.  The bed is not decoration:
it is the only thing in the frame with a known size, so it is what tells a
person whether the part in front of them is a coaster or a shelf — and, when
the part hangs off the edge, that it will not print in one piece.

The stage had no way to know how big the bed was, so it drew a 256 mm square
for everyone.  On a 350 mm machine that understates the room the user has; on
a 450 mm part it draws a small dark square that disappears under the model and
reads as an artifact rather than a plate.  This module answers the one
question the stage could not: **how big is this install's bed, and whose is
it?**

WHAT IT RETURNS
---------------
A plain dict, ready to ride the ``kiln.mesh.v1`` payload as ``plate``::

    {"x_mm": 256.0, "y_mm": 256.0, "z_mm": 256.0,
     "printer_id": "bambu_a1", "label": "Bambu Lab A1", "source": "printer"}

``source`` is the honesty field, and every consumer keys off it:

* ``"printer"`` — these are a real machine's dimensions, from
  ``printer_intelligence.json`` via the printer model in ``config.yaml``.
  The stage may etch the name on the plate and may draw the machine's build
  envelope, because both are claims about a bed we actually know.
* ``"default"`` — nobody told us which printer this is, so the stage draws a
  reference plate.  It says nothing about anyone's machine, and the stage
  must not decorate it with a name or a volume.

TWO PLACES IT DELIBERATELY STAYS QUIET
--------------------------------------
* **No printer model configured.**  ``config.yaml`` carries ``printer_model``
  or it does not; this module never infers one from a serial prefix or a
  hostname (see :mod:`kiln.printer_model_resolver` for why that inference was
  removed).  Unknown means the reference plate, not a guess.
* **The hosted server.**  One process there serves every customer out of one
  ``~/.kiln``, so that file's ``printer_model`` is not the caller's — it is
  whatever the box happens to have.  Resolution is skipped entirely, and
  every hosted caller gets the reference plate.

Never raises: a stage that cannot name the bed still has to draw one.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Edge length of the reference plate, in millimetres.  Sits near the middle
#: of consumer FDM beds (Ender 235, Prusa 250, Bambu 256) so an unknown
#: machine is neither flattered nor shortchanged.  The stage carries the same
#: number as its own fallback — this one is what the payload states when it
#: states anything at all.
DEFAULT_PLATE_MM = 256.0


def _display_name(printer_id: str) -> str | None:
    """Catalogue name for an already-canonical printer id, or ``None``.

    Exact match only.  The fuzzy lookups elsewhere fall back to a ``default``
    profile, which is fine for settings advice and wrong here: a plate etched
    with the wrong printer's name is worse than a plate etched with nothing.
    """
    try:
        from kiln.printers.bed_fit import get_printer_display_name

        return get_printer_display_name(printer_id)
    except Exception:  # noqa: BLE001 — a missing name is not a failure
        return None


def default_stage_plate() -> dict[str, Any]:
    """The reference plate — a square of :data:`DEFAULT_PLATE_MM`, unattributed."""
    return {
        "x_mm": DEFAULT_PLATE_MM,
        "y_mm": DEFAULT_PLATE_MM,
        "z_mm": None,
        "printer_id": None,
        "label": None,
        "source": "default",
    }


def resolve_stage_plate(printer_id: str | None = None) -> dict[str, Any]:
    """Resolve the plate for this install (or for an explicit *printer_id*).

    Falls back to :func:`default_stage_plate` for every unknown: no printer
    model configured, a model the catalogue does not carry, a hosted process,
    or any error at all along the way.
    """
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if printer_id is None and is_hosted_multitenant():
            # One shared ~/.kiln, many customers — its printer is nobody's.
            return default_stage_plate()

        if printer_id is None:
            from kiln.printer_model_resolver import resolve_printer_model

            printer_id = resolve_printer_model()
        if not printer_id:
            return default_stage_plate()

        from kiln.printers.bed_fit import resolve_build_volume

        resolved = resolve_build_volume(printer_id)
        if not resolved:
            return default_stage_plate()
        canonical, (x, y, z) = resolved
        return {
            "x_mm": float(x),
            "y_mm": float(y),
            "z_mm": float(z),
            "printer_id": canonical,
            "label": _display_name(canonical),
            "source": "printer",
        }
    except Exception:  # noqa: BLE001 — the stage must never fail on furniture
        logger.debug("stage plate not resolved", exc_info=True)
        return default_stage_plate()


def attach_stage_plate(
    payload: dict[str, Any] | None,
    printer_id: str | None = None,
    *,
    mesh_path: str | None = None,
    gcode_path: str | None = None,
    occupancy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Stand a ``kiln.mesh.v1`` *payload* on the plate, stamp the plate on,
    lay the slicer's own additions around the part, and show what the plate
    already holds.

    The single call every payload-producing door makes, so a door added later
    cannot ship a stage with no bed under it — or, the same mistake from the
    other side, a correct bed with the part parked in a corner of it.  The
    stage draws the bed centred on the origin; geometry arrives wherever its
    file put it (a slicer-placed 3MF sits at the bed's centre in ITS bed
    coordinates, 128 mm from the origin).  Measured 2026-09-01 on the hosted
    view: a painted jar centred on the printer's plate rendered at the far
    corner of the stage's, because that door stamped no plate and centred
    nothing.  Centring lives here so the plate and the placement cannot
    come apart again.

    The same reasoning puts the slicer-added geometry here — skirt, brim,
    prime tower, supports, the things the printer prints that the model
    never contained (:mod:`kiln.slicer_geometry`).  They share the plate's
    coordinate frame and must move with the same centring the part gets,
    so the one door that decides placement is the one that attaches them.
    When the slice says the slicer turned the part, the part is turned the
    same way here, about its own centre, before the extras are laid
    around it (:func:`kiln.slicer_geometry.apply_pose_to_payload`) — the
    stage shows what will print, and ``payload["pose"]`` says it did.
    A door passes ``gcode_path`` when it holds the slice (the print twin),
    or ``mesh_path`` so the slice can be looked up in the machine's own
    ledger (:func:`kiln.monitor_twin.sliced_output_for`); a door that
    passes neither, or a mesh nobody sliced, attaches nothing — the model-
    only payload is byte-identical to one built before extras existed.

    The plate's contents ride here for the same reason.  When the plate
    record (:mod:`kiln.plate_state`) says the last print is still on the
    plate, the payload carries an ``occupancy`` block
    (:data:`kiln.plate_state.OCCUPANCY_KIND`): the occupant's footprint box
    and height from the record, or — when a door holds a placement verdict
    and passes its block as *occupancy* — the engine's own reading, with the
    proposed spot.  A clear or unrecorded plate
    carries none.  Rects are in bed millimetres with ``bed_mm`` alongside;
    the part's own positions have been centred by :func:`stand_on_plate`,
    so a drawing places the occupant against ``proposed.rect_mm`` when the
    block has one and against the bed otherwise.

    A ``None`` payload (no geometry to show) passes straight through.
    """
    if not isinstance(payload, dict):
        return payload
    stand_on_plate(payload)
    payload["plate"] = resolve_stage_plate(printer_id)
    attach_slicer_geometry(payload, mesh_path=mesh_path, gcode_path=gcode_path)
    block = occupancy if isinstance(occupancy, dict) else occupancy_for_plate(payload["plate"])
    if block:
        attach_occupant_prints(block)
        payload["occupancy"] = block
    return payload


def occupancy_for_plate(plate: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The record-box ``occupancy`` block for this install's printer, or ``None``.

    Read from the plate record of the default adapter — the machine whose
    model named the plate — and only on a person's own machine: the hosted
    process serves every customer out of one ``~/.kiln``, so its record is
    nobody's, exactly as its ``printer_model`` is.  *plate* (the resolved
    plate dict) supplies ``bed_mm``.  ``None`` for a clear or unrecorded
    plate, and for anything that goes wrong: a stage that cannot say what
    is on the plate still draws the part.  Never raises.
    """
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return None
        from kiln.server import _get_adapter

        adapter = _get_adapter()
        from kiln.plate_state import read

        state = read(adapter)
        if not state.occupied:
            return None
        bed = None
        if isinstance(plate, dict) and plate.get("x_mm") and plate.get("y_mm"):
            bed = [plate["x_mm"], plate["y_mm"]]
        return state.occupancy(bed)
    except Exception:  # noqa: BLE001 — what the plate holds is furniture to the stage
        logger.debug("plate occupancy not resolved", exc_info=True)
        return None


#: A print on the plate is drawn from its own files -- the model it was
#: printed from, and the slicer's additions from its G-code -- never from
#: the clearance engine's height grid, which is sized for checking where a
#: head can go and reads as blocks when drawn.  Capped per print so a busy
#: plate cannot swell the stage past what a panel will carry; a model over
#: the cap is thinned by the mesh builder, never dropped silently.
OCCUPANT_MAX_TRIANGLES = 40_000
_OCCUPANT_MAX_PRINTS = 4
_MODEL_SUFFIXES = (".3mf", ".stl", ".obj")
_OCCUPANT_CACHE: dict[tuple[Any, ...], dict[str, Any] | None] = {}
_OCCUPANT_CACHE_MAX = 8


def attach_occupant_prints(block: dict[str, Any]) -> dict[str, Any]:
    """Add ``prints`` to an occupancy *block*: each print still on the plate,
    drawn from the files it was printed from, where Kiln has them.

    Read from this install's own plate record and the files Kiln kept of
    each print (:func:`occupant_prints`), and only on a person's own machine
    (the hosted process's record is nobody's).  A
    print is attached only when its footprint on the bed overlaps an
    occupant the block already names, so a stage for one machine can never
    draw another machine's part.  An occupant with no files keeps the
    block's record box.  Never raises; returns *block*.
    """
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return block
        from kiln.plate_state import read
        from kiln.server import _get_adapter

        state = read(_get_adapter())
        if not state.occupied:
            return block
        prints = occupant_prints(state.jobs, block)
        if prints:
            block["prints"] = prints
    except Exception:  # noqa: BLE001 -- a drawing of the plate is furniture to the stage
        logger.debug("occupant prints not attached", exc_info=True)
    return block


def occupant_prints(jobs: Any, block: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The prints standing on the plate, each from its own files.

    For every job the plate record holds, the files come from
    :func:`kiln.monitor_twin.printed_files_for` -- the same join the
    placement verdict reads the G-code through, so the drawing and the
    verdict are always of the same print.  The model comes from the wrapped
    3MF (the printed file carries the model it printed) or, failing that,
    the model that was sliced; its place on the bed comes from the G-code, by
    the same checked alignment the stage uses for a part's own skirt and
    tower (:func:`kiln.slicer_geometry.slicer_features_block`) -- a model
    whose size does not match what was printed is not drawn at all.  The
    slicer's additions (prime tower, skirt, brim, supports) ride as their
    own classified block so a stage can show them with its extras and
    never mistake them for the part.

    Each entry::

        {"name": str, "top_mm": float, "footprint_mm": [x0, y0, x1, y1],
         "place_mm": [dx, dy, dz],          # mesh coords + place = bed coords
         "mesh": {"positions", "indices", "vertex_colors"?, "normals"?},
         "slicer": <kiln.slicer_features.v1, viewer frame>}

    Never raises.
    """
    out: list[dict[str, Any]] = []
    occupied = [o for o in (block or {}).get("occupied") or [] if isinstance(o, dict)]
    for job in list(jobs or [])[:_OCCUPANT_MAX_PRINTS]:
        try:
            entry = _occupant_print(job)
        except Exception:  # noqa: BLE001
            logger.debug("occupant print not built for %r", getattr(job, "file", job), exc_info=True)
            entry = None
        if entry is None:
            continue
        if block is not None:
            # Drawn only where the block says something stands, and called
            # by the block's own name for it -- the name the verdict uses in
            # "74 mm from ...", so a label and a sentence never disagree.
            best = max(occupied, key=lambda o: _overlap_area(entry["footprint_mm"], o.get("rect_mm")), default=None)
            if best is None or _overlap_area(entry["footprint_mm"], best.get("rect_mm")) <= 0.0:
                logger.debug("occupant print %s does not stand where the block says; not drawn", entry["name"])
                continue
            if isinstance(best.get("name"), str) and best["name"]:
                entry["name"] = best["name"]
        out.append(entry)
    return out


def _overlap_area(a: Any, b: Any) -> float:
    try:
        w = min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0]))
        h = min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1]))
    except (TypeError, ValueError, IndexError):
        return 0.0
    return w * h if w > 0 and h > 0 else 0.0


def _file_key(path: str | None) -> tuple[Any, ...] | None:
    import os

    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)


def _occupant_print(job: Any) -> dict[str, Any] | None:
    from kiln.monitor_twin import printed_files_for
    from kiln.plate_state import pretty_job_name

    file_name = getattr(job, "file", None)
    files = printed_files_for(file_name)
    if not files:
        return None
    gcode = files["gcode"]
    models = [m for m in files["models"] if m.lower().endswith(_MODEL_SUFFIXES)]
    for model in models:
        key = (_file_key(model), _file_key(gcode))
        if key in _OCCUPANT_CACHE:
            built = _OCCUPANT_CACHE[key]
        else:
            built = _build_occupant(model, gcode)
            if len(_OCCUPANT_CACHE) >= _OCCUPANT_CACHE_MAX:
                _OCCUPANT_CACHE.pop(next(iter(_OCCUPANT_CACHE)))
            _OCCUPANT_CACHE[key] = built
        if built is not None:
            return {"name": pretty_job_name(file_name), **built}
    return None


def _build_occupant(model: str, gcode: str) -> dict[str, Any] | None:
    """The drawing of one print: its model placed where its G-code printed
    it, and its slicer additions.  ``None`` when the two do not agree."""
    from kiln.mesh_payload import mesh_to_viewer_payload
    from kiln.slicer_geometry import slicer_features_block, to_viewer_frame

    mesh = mesh_to_viewer_payload(model, max_triangles=OCCUPANT_MAX_TRIANGLES)
    if mesh.get("downgraded") or not mesh.get("positions"):
        return None
    lo = [float(v) for v in mesh["bbox"]["min"]]
    hi = [float(v) for v in mesh["bbox"]["max"]]
    feats = slicer_features_block(gcode, tuple(lo), tuple(hi))
    if not feats.get("available"):
        logger.debug("occupant %s not drawn: %s", model, feats.get("reason"))
        return None
    ox, oy, oz = (float(v) for v in feats["offset_mm"])  # bed + offset = mesh
    place = [-ox, -oy, -oz]
    geometry = {k: mesh[k] for k in ("positions", "indices", "vertex_colors", "normals") if mesh.get(k)}
    return {
        "top_mm": round(hi[2] + place[2], 2),
        "footprint_mm": [
            round(lo[0] + place[0], 2),
            round(lo[1] + place[1], 2),
            round(hi[0] + place[0], 2),
            round(hi[1] + place[1], 2),
        ],
        "place_mm": [round(v, 4) for v in place],
        "mesh": geometry,
        "slicer": to_viewer_frame(feats),
    }


def resolve_sliced_gcode(
    mesh_path: str | None, gcode_path: str | None = None
) -> str | None:
    """The sliced G-code to draw around *mesh_path*, or ``None``.

    An explicit *gcode_path* is honoured as given — the door that holds a
    slice knows more than any ledger.  Otherwise the machine's own slice
    ledger answers, EXCEPT on the hosted server: one process there serves
    every customer out of one ``~/.kiln``, so its ledger is nobody's, and
    a hosted caller's mesh must never be dressed in another tenant's
    skirt.  Same rule the plate follows.  Never raises.
    """
    try:
        if gcode_path:
            return str(gcode_path)
        if not mesh_path:
            return None
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return None
        from kiln.monitor_twin import sliced_output_for

        return sliced_output_for(mesh_path)
    except Exception:  # noqa: BLE001 — no slice is the ordinary case
        logger.debug("sliced gcode not resolved", exc_info=True)
        return None


def attach_slicer_geometry(
    payload: dict[str, Any] | None,
    *,
    mesh_path: str | None = None,
    gcode_path: str | None = None,
) -> dict[str, Any] | None:
    """Stamp the ``slicer`` block onto *payload*, in place, when a slice of
    this mesh exists.  Aligned to the payload's bbox as it stands — call
    AFTER :func:`stand_on_plate`, as :func:`attach_stage_plate` does.
    Never raises."""
    try:
        if not isinstance(payload, dict):
            return payload
        gcode = resolve_sliced_gcode(mesh_path, gcode_path)
        if not gcode:
            return payload
        from kiln.slicer_geometry import attach_to_payload

        attach_to_payload(payload, gcode)
    except Exception:  # noqa: BLE001 — extras never break the stage
        logger.debug("slicer geometry skipped", exc_info=True)
    return payload


def stand_on_plate(payload: dict | None) -> dict | None:
    """Slide the geometry to the middle of the plate in X and Y, in place.

    WHY THIS IS HERE.  Geometry arrives in whatever coordinates its source
    wrote, and a parametric model's origin is almost always a CORNER of the
    part rather than its middle — a 120 x 150 SCAD panel occupies x 0..120,
    y 0..150.  The stage, meanwhile, draws the print bed CENTERED on the
    origin.  Left alone, most of Kiln's templates render parked in one
    quadrant of the plate or hanging off its edge, which reads as a part
    that will not print rather than as the coordinate convention it is.

    Z is deliberately untouched.  The part rests ON the bed; lifting or
    sinking it would be a claim about the print that isn't true.

    ``positions`` (viewer space, where x = mesh x and z = -mesh y) and
    ``bbox`` (mesh space) move by the SAME offset, so the payload can never
    describe the part somewhere its vertices are not.  A downgraded payload
    carries a bbox and no geometry — there is nothing to move, and moving
    the bbox alone would invent exactly that disagreement — so it passes
    through untouched.

    Never raises: an off-centre part on the plate beats a tool call that
    died over furniture.
    """
    try:
        if not isinstance(payload, dict) or payload.get("downgraded"):
            return payload
        positions = payload.get("positions")
        bbox = payload.get("bbox")
        if not isinstance(positions, str) or not isinstance(bbox, dict):
            return payload
        lo, hi = bbox.get("min"), bbox.get("max")
        if not (isinstance(lo, list) and isinstance(hi, list)):
            return payload
        if len(lo) != 3 or len(hi) != 3:
            return payload
        dx = -(float(lo[0]) + float(hi[0])) / 2.0
        dy = -(float(lo[1]) + float(hi[1])) / 2.0
        if not dx and not dy:
            return payload  # already centred — nothing to re-encode

        import numpy as np

        xyz = (
            np.frombuffer(base64.b64decode(positions), dtype="<f4")
            .reshape(-1, 3)
            .copy()
        )
        xyz[:, 0] += dx  # viewer x IS mesh x
        xyz[:, 2] -= dy  # viewer z is -mesh y, so mesh +y moves viewer -z
        payload["positions"] = base64.b64encode(
            xyz.astype("<f4", copy=False).tobytes()
        ).decode("ascii")
        bbox["min"] = [round(float(lo[0]) + dx, 4), round(float(lo[1]) + dy, 4), lo[2]]
        bbox["max"] = [round(float(hi[0]) + dx, 4), round(float(hi[1]) + dy, 4), hi[2]]
        # The slicer's additions share this frame and ride the same slide:
        # a skirt left behind by a centring is a skirt around empty plate.
        if isinstance(payload.get("slicer"), dict):
            from kiln.slicer_geometry import shift_block

            shift_block(payload["slicer"], dx, dy)
    except Exception:  # noqa: BLE001 — the stage may be off-centre, never broken
        logger.debug("stage centring skipped", exc_info=True)
    return payload


