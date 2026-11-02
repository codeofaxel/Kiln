"""Public-Kiln → Kiln's servers: Kiln Pro's filament for a refused print.

When a print or a slice is refused because the printer cannot melt the
filament (:func:`kiln.printers.print_gate.check_material_temp`), every plan
is told why and which filaments the printer runs.  Kiln Pro
(https://kiln3d.com/pricing) also names the filaments closest to the refused
one that this printer can melt, checked against the saved goal the part was
made for when Kiln knows it.  This module asks for that answer.

Asked in one of two places, in order:

1. **kiln-pro on this computer** (the hosted server, and an install with
   kiln-pro), imported by name in :func:`_local`.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL` with
   ``blocked_material``, asked through the door every served tool uses
   (:func:`kiln.server._pro_api_call`), only when the signed-in plan
   includes Kiln Pro.  The part's file stays here: the goal is named by the
   kept version being printed, read off this computer's own records.

Below Kiln Pro the refusal gets one line saying what the plan adds, in the
account's own words for where it stands (signed out, a lower plan, a plan
that has ended).  A miss (offline, signed out, no answer, refused) is said
in one sentence and the refusal stands as it is.  Nothing here raises.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, sentence

logger = logging.getLogger(__name__)

#: The served tool that answers it (the material pick, refused-print mode).
WIRE_TOOL = "material_pick"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.print_fix.engine"
#: Asked on the way to a refusal; the refusal stands if this passes.
_CONSULT_TIMEOUT_S: float = 4.0

#: What the plan that includes it adds, said once to a plan without it.  It
#: names no plan: the account's own sentence after it does, once
#: (:func:`kiln.account_plan.check_tier`).
PRO_ADDS = (
    "Kiln can also name the filaments closest to {material} that this "
    "printer can melt, checked against what your goal says the part is for."
)
#: The served-miss sentence's parts (:func:`kiln.served_answer.sentence`).
_MISS_WORDS = {
    "on_the_line": "Kiln Pro's pick of a filament this printer can melt is worked out by Kiln's servers",
    "cannot": "ask for it",
    "wont": "names no filament beyond the ones this printer runs",
}

_NOT_HERE = object()


def material_fix(
    printer_id: str | None,
    material_id: str | None,
    *,
    version_id: str | None = None,
) -> dict[str, Any]:
    """What a refusal over *material_id* on *printer_id* carries beyond the
    free answer, as one of:

    * ``{"enrichment": {...}}`` -- the Pro block, the same shape a refusal
      carries where kiln-pro runs;
    * ``{"pro_adds": "..."}`` -- a plan without it: what it adds, then the
      account's own sentence for where it stands;
    * ``{"pro_fix_missed": "..."}`` -- the plan includes it and the servers
      did not give it: why, in one sentence;
    * ``{}`` -- nothing to add.  Never raises.
    """
    if not (printer_id and material_id):
        return {}
    local = _local(printer_id, material_id, version_id)
    if local is not _NOT_HERE and local is not None:
        return {"enrichment": local}
    note = _plan_note(material_id)
    if note is not None:
        return {"pro_adds": note}
    if local is not _NOT_HERE:
        return {}
    return _served(printer_id, material_id, version_id)


def _local(printer_id: str, material_id: str, version_id: str | None) -> Any:
    try:
        from kiln_pro.print_fix.engine import material_enrichment
    except ImportError:
        return _NOT_HERE
    try:
        return material_enrichment(printer_id, material_id, version_id=version_id)
    except Exception as exc:  # noqa: BLE001 -- the refusal stands without it
        logger.warning("kiln-pro filament fix failed; the refusal stands: %s", exc)
        return None


def _plan_note(material_id: str) -> str | None:
    """The line for a plan without Kiln Pro, or ``None`` when it is covered.
    A plan that cannot be read is no plan, and names none."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, why = check_tier(LicenseTier.PRO)
    except Exception:  # noqa: BLE001 -- an unreadable plan is never a pitch
        logger.debug("filament fix: plan not read", exc_info=True)
        return None
    if covered:
        return None
    adds = PRO_ADDS.format(material=material_id.upper())
    return f"{adds} {why}".strip() if why else adds


def _served(printer_id: str, material_id: str, version_id: str | None) -> dict[str, Any]:
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return {}
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # The person asked to print, not for this pick: it is fetched on
            # the way to the refusal they are given.
            _asked_by_user=False,
            blocked_material=str(material_id),
            printer_model=str(printer_id),
            version_id=str(version_id or ""),
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        return _said(classify_transport_error(exc))
    if not isinstance(answer, dict):
        return {}
    miss = classify_answer(answer)
    if miss is not None:
        return _said(miss)
    enrichment = answer.get("enrichment")
    if answer.get("known") is not True or not isinstance(enrichment, dict):
        return {}
    return {"enrichment": enrichment}


def _said(miss: Miss | None) -> dict[str, Any]:
    if miss is None:
        return {}
    return {"pro_fix_missed": sentence(miss, feature="servers", then="ask to print again", **_MISS_WORDS)}


__all__ = ["LOCAL_MODULE", "PRO_ADDS", "WIRE_TOOL", "material_fix"]
