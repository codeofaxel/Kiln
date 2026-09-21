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
               "job": {"file", "footprint_mm": [x0,y0,x1,y1]|null, "max_z_mm"}|null,
               "since": str|null},
     "occupant_gcode": {"path"} | {"name", "gz_b64"} | null,
     "part": {"size_mm": [x,y,z], "layer_height_mm", "tower_mm": [x,y]|null,
              "colour_changes_at_mm": [...], "skirt_mm"} | null,
     "sliced_gcode": {"path"} | {"name", "gz_b64"} | null,
     "placement": [x, y] | "keep" | "auto",
     "keep_at_mm": [x, y] | null,
     "placed_by": "auto"|"human"|"agent"|"keep",
     "suppress": [str] | null}

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
                   "reserved", "proposed", "source": "gcode"|"record_box"},
     "record": {"printer_id", "measured", "source"},
     "tier": {"verdict": "free", "plan": "pro"}}

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

logger = logging.getLogger(__name__)

SCHEMA = "placement_verdict/1"
REQUEST_SCHEMA = "placement_request/1"
OCCUPANCY_KIND = "kiln.plate_occupancy.v1"
TOOL = "placement_plan"

#: Why no verdict came back -- the three causes a refusal can name.
OFFLINE = "offline"
SIGNED_OUT = "signed_out"
NOT_ANSWERED = "not_answered"
REASONS = (OFFLINE, SIGNED_OUT, NOT_ANSWERED)

#: The hosted form carries a G-code body gzipped; over this it is dropped
#: (sent as ``null``) and the engine falls back to the record's box.
MAX_GZ_BYTES = 8 * 1024 * 1024

_OFFLINE_CODES = frozenset({"SERVER_UNREACHABLE"})
_SIGNED_OUT_CODES = frozenset({"KILN_ACCOUNT_NOT_PAIRED", "KILN_SIGNIN_REQUIRED", "KILN_SESSION_EXPIRED"})
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


def ask(request: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """``(verdict, None)`` when a source answered, else ``(None, reason)``.

    *reason* is one of :data:`REASONS`, read off the service's own answer
    the way the motion bridge reads it: a network fault is
    :data:`OFFLINE`; no sign-in, an unpaired account or an expired session
    is :data:`SIGNED_OUT`; anything else -- a refusal for this machine, a
    malformed document, an answer with no code -- is :data:`NOT_ANSWERED`.
    Never raises.
    """
    try:
        if not isinstance(request, dict) or not request.get("printer_id"):
            return None, NOT_ANSWERED
        pro = _local_pro()
        if pro is not None and hasattr(pro, "build_verdict"):
            try:
                doc = _call_local(pro.build_verdict, request)
                if is_verdict(doc):
                    return doc, None
            except Exception:  # noqa: BLE001 -- a local builder fault falls through to the service
                logger.debug("kiln_pro.placement.bridge.build_verdict raised; asking the service", exc_info=True)
        return _served(request)
    except Exception:  # noqa: BLE001 -- the bridge never raises into a slice door
        logger.debug("placement bridge failed", exc_info=True)
        return None, NOT_ANSWERED


def _call_local(build_verdict: Any, request: dict[str, Any]) -> Any:
    """The local builder takes the request document; a builder written to
    the motion bridge's keyword shape is met halfway."""
    try:
        return build_verdict(request)
    except TypeError:
        return build_verdict(**request)


def _served(request: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Ask the hosted service; ``(verdict, None)`` or ``(None, reason)``.

    Goes through the same door every served tool uses
    (``kiln.server._pro_api_call``): the user's sign-in, the device
    fingerprint header, the client version.  The service's own message is
    logged; the door words what the user sees.
    """
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None, NOT_ANSWERED
    try:
        answer = _pro_api_call(TOOL, **hosted_form(request))
    except Exception:  # noqa: BLE001 -- the network is a degrade, never a slice onto a part
        logger.debug("placement_plan request failed", exc_info=True)
        return None, OFFLINE
    if not isinstance(answer, dict):
        return None, NOT_ANSWERED
    doc = answer.get("verdict") if "verdict" in answer else answer
    if is_verdict(doc):
        return doc, None
    if answer.get("error") or answer.get("status") == "error":
        code = str(answer.get("code") or "")
        logger.info("placement_plan not served: %s", answer.get("error") or answer.get("message"))
        if code in _OFFLINE_CODES:
            return None, OFFLINE
        if code in _SIGNED_OUT_CODES:
            return None, SIGNED_OUT
    return None, NOT_ANSWERED


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

    The slice ledger (:func:`kiln.monitor_twin.sliced_entry_for`) joins the
    printer-side name to the G-code Kiln sliced; failing that, the twin's
    retained copy of the running job answers when it is the same file.
    ``None`` when Kiln did not slice it -- the engine then uses the record's
    footprint box.  Never raises.
    """
    base = os.path.basename(str(job_file or ""))
    if not base:
        return None
    try:
        from kiln.monitor_twin import active_twin, sliced_entry_for

        entry = sliced_entry_for(base)
        output = entry.get("output") if isinstance(entry, dict) else None
        if isinstance(output, str) and os.path.isfile(output):
            return {"path": output}
        twin = active_twin()
        if isinstance(twin, dict) and os.path.basename(str(twin.get("file_name") or "")) == base:
            retained = twin.get("gcode")
            if isinstance(retained, str) and os.path.isfile(retained):
                return {"path": retained}
    except Exception:  # noqa: BLE001 -- a ledger miss is "unknown", never a fault
        logger.debug("occupant gcode lookup failed", exc_info=True)
    return None


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

    state = plate_state.read(adapter)
    job = state.job
    plate: dict[str, Any] = {
        "status": state.status,
        "job": (
            {"file": job.file, "footprint_mm": list(job.footprint_mm) if job.footprint_mm else None, "max_z_mm": job.max_z_mm}
            if job is not None
            else None
        ),
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
    }
