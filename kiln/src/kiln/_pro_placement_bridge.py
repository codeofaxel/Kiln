"""Public-Kiln → kiln-pro placement bridge: where a plate-clearance VERDICT comes from.

A slice used to ignore what was already on the plate: a part left behind by
the last print, and the new one centred by the bed-fit gate -- onto it.
The plate record (:mod:`kiln.plate_state`) now says when the plate still
holds a part; this bridge asks whether a second part can go beside it,
and where.

Public Kiln owns the doors and the floor: the ``placement`` argument on
every slice door, the record, the refusal wording, the translation of the
mesh to the spot that was named, the second look at the sliced file, and
the fail-closed default -- an occupied plate with no verdict is never
sliced onto.  What it does not own is the verdict: how far a head, its
fans and its purge reach around a part of that height, which rows the
machine reserves, how tall a tower or a skirt makes the new part's
footprint.  That is a reading of each machine kept by kiln-pro
(https://kiln3d.com) and handed over one verdict at a time.  Nothing here
computes clearance; a caller that finds itself doing so is on the wrong
side of this file.

A verdict reaches a door from one of two places, tried in order:

1. **kiln-pro importable** (a source-tree install with the overlay on disk):
   ``kiln_pro.placement.bridge.build_verdict`` answers from the local overlay.
2. **served**: the signed-in user's Kiln asks the hosted service
   (``POST /api/tools/placement_plan``) through the same door every served
   tool uses (:func:`kiln.server._pro_api_call`).  The clearance verdict is
   free; placing and starting a second print on an occupied plate is a
   kiln-pro feature (https://kiln3d.com/pricing), and the verdict's own
   ``tier`` block says so.

There is deliberately **no cache**.  A motion plan describes a machine and
can be kept; a placement verdict describes the plate as it stands NOW --
the part that is on it, its height, the spot being asked for -- and a
kept verdict would be an answer about a different plate.  When neither
source answers, the bridge says why (:data:`OFFLINE`, :data:`SIGNED_OUT`,
:data:`NOT_ANSWERED`) so the door can word one honest sentence, and the
door refuses: fail closed, never "no verdict, so proceed".

The contract both sides build to, exactly:

Request (``schema: "placement_request/1"``)::

    {"printer_id", "serial",
     "plate": {"status": "occupied"|"clear"|"unknown",
               "job": {"file", "footprint_mm": [x0,y0,x1,y1]|null, "max_z_mm",
                       "printer_id": str|null}|null,
               "jobs": [{...job, "gcode": {"path"}|{"name","gz_b64"}|null}],
               "fingerprint": str,
               "since": str|null},
     "occupant_gcode": {"path"} | {"name", "gz_b64"} | null,
     "part": {"size_mm": [x,y,z], "layer_height_mm", "tower_mm": [x,y]|null,
              "colour_changes_at_mm": [...], "skirt_mm"} | null,
     "sliced_gcode": {"path"} | {"name", "gz_b64"} | null,
     "placement": [x, y] | "keep" | "auto",
     "keep_at_mm": [x, y] | null,
     "placed_by": "auto"|"human"|"agent"|"keep",
     "suppress": [str] | null,
     "printer_observations": [<printer_motion_observation/1>] | null,   # kiln.bench
     "printer_settings": {"format": "klipper_motion_settings/1", "sections": {...},
                          "chip": str|null, "unit": str|null} | null}

``occupant_gcode`` is the local G-code of what is on the plate when Kiln
has it (the slice ledger joins the printer-side name to the file Kiln
sliced); ``null`` when unknown, and the engine falls back to the record's
footprint box.  ``sliced_gcode`` is the post-slice pass: the file that was
just sliced, so the verdict is about the real toolpath and not the
envelope.  The local form names a path; the hosted form carries the body
gzipped and base64-encoded, at most :data:`MAX_GZ_BYTES` after gzip, and
a file over the cap travels as ``null``.

Verdict (``schema: "placement_verdict/1"``)::

    {"ok", "placed_by", "at_mm": [x,y]|null, "tower_at_mm", "footprint_mm",
     "clearance_mm", "refusals": [{"code", "sentence"}], "conflicts",
     "switched_off": {name: how}, "spots": [{"at_mm", "clearance_mm"}],
     "occupancy": {"kind": "kiln.plate_occupancy.v1", "bed_mm", "occupied",
                   "proposed", "source": "gcode"|"record_box"},
     "record": {"printer_id", "measured", "source", "quiet_start",
                "machine_blocks": {block: "read" | why-not}},
     "start": {"mode": "quiet_start", "ok", "available", "refusals",
               "clear_z_mm", "lift_floor_mm", "travel_to_mm", "first_layer_z_mm",
               "approach_mm", "home_xy_gcode", "flags", "switched_off",
               "plate_fingerprint"} | null,
     "tier": {"verdict": "free", "plan": "pro"}}

``plate.jobs`` is every part on the plate (a second one started the quiet
way beside the first), each with its own file when the ledger has it;
``plate.fingerprint`` is the record's own hash of them.  ``start`` rides an
ok verdict on the sliced file: the quiet start's plan -- the paid half --
or, below its tier, ``available: false`` and the lift floor alone, with
the tier it needs when the plan would stand, and the plan's own refusal
and no tier when it would not.  The slicing door hands the plan to the
wrap, which writes it into the file under a contract the start gate
judges live.  ``record.quiet_start`` says whether a print could start
beside what is on this plate on the plan's tier; a door names that tier
only when it is true, and reads a verdict without it as false.

``at_mm`` is where the part's own footprint origin (its min corner) goes,
the same corner ``keep_at_mm`` names; ``footprint_mm`` is the placed rect.
A response carrying ``error`` / ``status: "error"``, or without the schema,
reads as "no verdict".  Every function here returns without raising.
"""

from __future__ import annotations

import base64
import gzip
import logging
import os
from typing import Any

from kiln import served_answer
from kiln.plate_state import OCCUPANCY_KIND

logger = logging.getLogger(__name__)

SCHEMA = "placement_verdict/1"
REQUEST_SCHEMA = "placement_request/1"
TOOL = "placement_plan"
__all__ = ["OCCUPANCY_KIND", "REQUEST_SCHEMA", "SCHEMA", "TOOL", "ask", "hosted_form", "job_envelope", "request_for", "verdict_for"]

#: Why no verdict came back: the causes a miss can have, in the shared
#: voice's own words (:data:`kiln.served_answer.CAUSES`).  Decided in ONE
#: place -- ``served_answer.classify_answer`` for an answer with no verdict
#: in it, ``classify_transport_error`` for a request that never got one --
#: and the door hands the :class:`~kiln.served_answer.Miss` straight to
#: ``served_answer.sentence``, so the code rides beside the sentence and
#: never inside it.
OFFLINE = "offline"
SIGNED_OUT = "signed_out"
UNANSWERED = "unanswered"
REFUSED = "refused"
REASONS = served_answer.CAUSES

#: The hosted form carries a G-code body gzipped; over this it is dropped
#: (sent as ``null``) and the engine falls back to the record's box.
MAX_GZ_BYTES = 8 * 1024 * 1024
_GCODE_FIELDS = ("occupant_gcode", "sliced_gcode")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _local_pro() -> Any | None:
    """``kiln_pro.placement.bridge`` when it is installed, else ``None``.

    Imported on every call rather than cached at import time, for the same
    reason the motion bridge does: the public package is imported before an
    embedding host has decided whether the overlay is present, and a test
    that installs or removes the pro package mid-process must see the change.
    """
    try:
        from kiln_pro.placement import bridge  # type: ignore[import-not-found]
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 -- a broken pro install degrades to the service
        logger.debug("kiln_pro.placement.bridge failed to import; asking the service", exc_info=True)
        return None
    return bridge


def available() -> bool:
    """True when kiln-pro is importable here.  Not "a verdict is obtainable":
    a served verdict needs no kiln-pro; ask :func:`ask`."""
    return _local_pro() is not None


def is_verdict(doc: Any) -> bool:
    """A well-formed verdict document: the schema, and ``ok`` a bool."""
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA and isinstance(doc.get("ok"), bool)


def verdict_for(request: dict[str, Any]) -> dict[str, Any] | None:
    """The verdict for *request*, or ``None`` when nothing answers.

    Local kiln-pro first, then the service -- see the module docstring.
    A verdict that says ``ok: false`` is still a verdict: the engine's own
    refusal, with its sentences, handed back for the door to relay.  A
    caller that needs to say WHY there is no verdict asks :func:`ask`.
    """
    return ask(request)[0]


def ask(request: dict[str, Any]) -> tuple[dict[str, Any] | None, served_answer.Miss | None]:
    """``(verdict, None)`` when a source answered, else ``(None, miss)``.

    *miss* is the shared voice's :class:`~kiln.served_answer.Miss` -- why
    there is no verdict (offline, signed out, unanswered, or a refusal in
    the server's own words), classified by ``served_answer`` the same way
    every served door classifies it, so the door words one sentence from
    it and the code rides beside that sentence, never inside.  Never raises.
    """
    try:
        if not isinstance(request, dict) or not request.get("printer_id"):
            return None, served_answer.Miss(UNANSWERED, detail="no printer declared")
        pro = _local_pro()
        if pro is not None and hasattr(pro, "build_verdict"):
            try:
                doc = _call_local(pro.build_verdict, request)
                if is_verdict(doc):
                    return doc, None
            except Exception:  # noqa: BLE001 -- a local builder fault falls through to the service
                logger.debug("kiln_pro.placement.bridge.build_verdict raised; asking the service", exc_info=True)
        return _served(request)
    except Exception as exc:  # noqa: BLE001 -- the bridge never raises into a slice door
        logger.debug("placement bridge failed", exc_info=True)
        return None, served_answer.Miss(UNANSWERED, detail=str(exc)[:200])


def _call_local(build_verdict: Any, request: dict[str, Any]) -> Any:
    """The local builder takes the request document; a builder written to
    the motion bridge's keyword shape is met halfway."""
    try:
        return build_verdict(request)
    except TypeError:
        return build_verdict(**request)


def _served(request: dict[str, Any]) -> tuple[dict[str, Any] | None, served_answer.Miss | None]:
    """Ask the hosted service; ``(verdict, None)`` or ``(None, miss)``.

    Goes through the same door every served tool uses
    (``kiln.server._pro_api_call``): the user's sign-in, the device
    fingerprint header, the client version.  A request that never got an
    answer is classified from the fault itself (no route is offline; a
    server that hung up did not answer); an answer with no verdict in it
    is classified from the envelope, in the shared voice's own terms.  The
    service's own message is logged; the door words what the user sees.
    """
    try:
        import kiln.server as srv
    except Exception:  # noqa: BLE001
        return None, served_answer.Miss(UNANSWERED, detail="the served door is not importable")
    try:
        answer = srv._pro_api_call(TOOL, **hosted_form(request))
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a slice onto a part
        logger.debug("placement_plan request failed", exc_info=True)
        return None, served_answer.classify_transport_error(exc, host=getattr(srv, "_HOSTED_KILN_API_URL", None))
    if not isinstance(answer, dict):
        return None, served_answer.Miss(UNANSWERED, detail="not an answer")
    doc = answer.get("verdict") if "verdict" in answer else answer
    if is_verdict(doc):
        return doc, None
    if answer.get("error") or answer.get("status") == "error":
        logger.info("placement_plan not served: %s", answer.get("error") or answer.get("message"))
    return None, served_answer.classify_answer(answer) or served_answer.Miss(UNANSWERED, detail="no verdict in the answer")


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def hosted_form(request: dict[str, Any]) -> dict[str, Any]:
    """*request* with every local G-code path turned into the wire form.

    ``{"path": p}`` becomes ``{"name": basename, "gz_b64": ...}``; a body
    over :data:`MAX_GZ_BYTES` after gzip, or a path that cannot be read,
    travels as ``null`` -- the engine then uses the record's box, which is
    the honest fallback, never a truncated toolpath that reads as a shorter
    part.  The request itself is not modified.
    """
    out = dict(request)
    for field in _GCODE_FIELDS:
        entry = out.get(field)
        if isinstance(entry, dict) and "path" in entry and "gz_b64" not in entry:
            out[field] = _gz_entry(str(entry.get("path") or ""))
    return out


def _gz_entry(path: str) -> dict[str, str] | None:
    try:
        with open(path, "rb") as handle:
            body = gzip.compress(handle.read(), compresslevel=6)
    except (OSError, ValueError):
        return None
    if len(body) > MAX_GZ_BYTES:
        logger.info("placement: %s is over the wire cap after gzip; the record's box stands in", os.path.basename(path))
        return None
    return {"name": os.path.basename(path), "gz_b64": base64.b64encode(body).decode("ascii")}


def _serial_of(adapter: Any) -> str:
    return str(getattr(adapter, "serial", "") or getattr(adapter, "_serial", "") or "").strip()


def occupant_gcode_for(job_file: str | None) -> dict[str, str] | None:
    """The local G-code of what is on the plate, when Kiln has it.

    Read through :func:`kiln.monitor_twin.printed_files_for`, the same join
    the stage draws the print from: the slice ledger, else the copy retained
    when that file started printing.  ``None`` when Kiln has neither -- the
    engine then uses the record's footprint box.  Never raises.
    """
    from kiln.monitor_twin import printed_files_for

    files = printed_files_for(job_file)
    return {"path": files["gcode"]} if files else None


def request_for(
    adapter: Any,
    printer_id: str | None,
    *,
    placement: Any,
    part: dict[str, Any] | None,
    sliced_gcode_path: str | None = None,
    keep_at: list[float] | None = None,
    placed_by: str | None = None,
    suppress: list[str] | None = None,
) -> dict[str, Any]:
    """The request document for *adapter*'s plate as it stands now.

    The plate block is the record (:func:`kiln.plate_state.read`); the
    occupant's G-code comes from the slice ledger when Kiln has it.
    *placement* is ``[x, y]``, ``"keep"`` or ``"auto"``; *placed_by* says
    who chose it and defaults from the placement itself (an exact spot is
    an agent's, ``"keep"`` and ``"auto"`` are their own).  Never raises.
    """
    from kiln import plate_state
    from kiln.bench import observations_for_request
    from kiln.machine_motion import motion_settings_of

    state = plate_state.read(adapter)
    job = state.job

    def _job_dict(j: Any) -> dict[str, Any]:
        return {
            "file": j.file,
            "footprint_mm": list(j.footprint_mm) if j.footprint_mm else None,
            "max_z_mm": j.max_z_mm,
            # The model the print was STARTED on, so the engine can refuse
            # a printer re-declared since -- as the motion planner does.
            "printer_id": j.printer_id,
        }

    # Every part on the plate, first to last, each with its own file when
    # the ledger has it; ``job`` and ``occupant_gcode`` beside them are the
    # last one, the shape the wire has always carried.  The fingerprint is
    # the record's own (:func:`kiln.plate_state.fingerprint`); the engine
    # echoes it into the start plan and the file carries it, so a start is
    # judged against the plate as it stands then.
    plate: dict[str, Any] = {
        "status": state.status,
        "job": _job_dict(job) if job is not None else None,
        "jobs": [
            {**_job_dict(j), "gcode": occupant_gcode_for(j.file) if state.occupied else None}
            for j in state.jobs
        ],
        "fingerprint": plate_state.fingerprint(state) if state.occupied else "",
        "since": state.since,
    }
    if isinstance(placement, (list, tuple)):
        placement_value: Any = [float(placement[0]), float(placement[1])]
        who = placed_by or "agent"
    else:
        placement_value = str(placement or "auto")
        who = placed_by or (placement_value if placement_value in ("keep", "auto") else "agent")
    return {
        "schema": REQUEST_SCHEMA,
        "printer_id": str(printer_id or "").strip().lower(),
        "serial": _serial_of(adapter),
        "plate": plate,
        "occupant_gcode": occupant_gcode_for(job.file) if (job is not None and state.occupied) else None,
        "part": dict(part) if isinstance(part, dict) else None,
        "sliced_gcode": {"path": str(sliced_gcode_path)} if sliced_gcode_path else None,
        "placement": placement_value,
        "keep_at_mm": [float(keep_at[0]), float(keep_at[1])] if keep_at else None,
        "placed_by": who,
        "suppress": list(suppress) if suppress else None,
        # A Klipper-family printer's own pause, cancel and homing macros and
        # axis limits, with nothing that identifies it or its owner
        # (:func:`kiln.machine_motion.motion_settings`), so the verdict can
        # be judged on how THIS unit moves; sent only while telemetry is on.
        "printer_settings": motion_settings_of(adapter),
        # What this unit's owner observed it do in a guided session
        # (:mod:`kiln.bench`), so a blank in the record is judged on the
        # owner's numbers for THIS printer at once.
        "printer_observations": observations_for_request(adapter),
    }


# ---------------------------------------------------------------------------
# The job envelope a fleet survey reads
# ---------------------------------------------------------------------------

#: Files that are machine code already: their toolpath is fixed, so a plate
#: is judged against the file as it will print, not against an envelope.
_SLICED_EXTENSIONS = frozenset({".gcode", ".gco", ".g"})
#: The slicer's own defaults when no profile says otherwise -- the numbers
#: the slice doors use for a part before it is sliced.
_DEFAULT_LAYER_HEIGHT_MM = 0.2
_DEFAULT_SKIRT_MM = 6.0


def job_envelope(file_path: str) -> dict[str, Any]:
    """The job as a fleet survey reads it: ``{"file", "part", "sliced_gcode_path"}``.

    ``part`` is the placement request's part block -- the part's size from
    its bounding box, the slicer's default layer height and skirt -- or
    ``None`` when the geometry cannot be read.  ``sliced_gcode_path`` is the
    file itself when it is already machine code, else ``None``.  Reads the
    file, never a printer; never raises.  Kiln-pro's fleet survey
    (``kiln.placement_fleet.for_fleet``) takes this and asks each plate.
    """
    path = str(file_path or "")
    name = os.path.basename(path) or path
    ext = os.path.splitext(path)[1].lower()
    sliced = path if ext in _SLICED_EXTENSIONS else None
    part: dict[str, Any] | None = None
    try:
        if sliced is None:
            from kiln.printers.bed_fit import compute_mesh_bbox

            bbox = compute_mesh_bbox(path)
            if bbox:
                part = {
                    "size_mm": [
                        round(float(bbox["x_max"]) - float(bbox["x_min"]), 3),
                        round(float(bbox["y_max"]) - float(bbox["y_min"]), 3),
                        round(float(bbox["z_max"]) - float(bbox["z_min"]), 3),
                    ],
                    "layer_height_mm": _DEFAULT_LAYER_HEIGHT_MM,
                    "tower_mm": None,
                    "colour_changes_at_mm": [],
                    "skirt_mm": _DEFAULT_SKIRT_MM,
                }
        if part is None:
            # A sliced file, or a mesh the mesh reader could not open: the
            # plate record's own reader gives the footprint and height it can.
            from kiln.plate_state import geometry_of

            footprint, max_z = geometry_of(path)
            if footprint and max_z is not None:
                x0, y0, x1, y1 = (float(v) for v in footprint)
                part = {
                    "size_mm": [round(x1 - x0, 3), round(y1 - y0, 3), round(float(max_z), 3)],
                    "layer_height_mm": _DEFAULT_LAYER_HEIGHT_MM,
                    "tower_mm": None,
                    "colour_changes_at_mm": [],
                    "skirt_mm": 0.0,
                }
    except Exception:  # noqa: BLE001 -- unreadable geometry is "no envelope", and the survey says so
        logger.debug("job envelope of %s not derivable", path, exc_info=True)
        part = None
    return {"file": name, "part": part, "sliced_gcode_path": sliced}
