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

The same door answers a print whose filament is not loaded
(:func:`stand_in_fit`): whether each filament that is loaded suits the
part's saved goal, asked whenever the part has one.  Every plan is told when
a loaded filament would be unsafe for what the part is for; whether the
others suit it is Kiln Pro's, and a plan without it is told so once.  With
no goal, nothing is asked and the print door offers the loaded filament with
its own hedge.
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


#: The served-miss sentence's parts for :func:`stand_in_fit`.
_STAND_IN_MISS_WORDS = {
    "on_the_line": "Whether a loaded filament suits what this part is for is checked by Kiln's servers",
    "cannot": "check it",
    "wont": "offers the loaded filament without saying whether it suits the part",
}
#: Why nothing was checked, when no check could run.
NO_GOAL = (
    "Kiln has no saved goal for this part, so it doesn't say whether a loaded "
    "filament suits what the part is for."
)
NO_NEEDS = (
    "This part's saved goal doesn't say what it has to stand up to, so Kiln "
    "doesn't say whether a loaded filament suits it."
)
#: What the plan that includes it adds, said once to a plan without it, the
#: account's own sentence after it (:func:`kiln.account_plan.check_tier`).
STAND_IN_PRO_ADDS = "Kiln can also check a swap like this against what your goal says the part is for."


def stand_in_fit(
    material_id: str | None,
    stand_ins: list[str],
    *,
    goal_id: str | None = None,
    version_id: str | None = None,
    job_path: str | None = None,
) -> dict[str, Any] | None:
    """Whether each loaded filament in *stand_ins* suits the saved goal of
    the part being printed in place of *material_id*.

    ``{"verdicts": {filament: {"suits", "why", "offer", "ask"}}}``: ``suits``
    True, False or ``None`` (not said), ``why`` a finished sentence, ``offer``
    whether the filament is still offered, ``ask`` a question when ``why`` is
    a condition.  Beside it, ``pro_adds`` (the line for a plan without Kiln
    Pro, once) and ``missed`` (why nothing, or only part, was checked).  The
    goal is named by *goal_id*, the kept *version_id*, or the version
    *job_path* is a print of; ``None`` when there are no stand-ins.  Never
    raises.
    """
    if not material_id or not stand_ins:
        return None
    if not (goal_id or version_id) and job_path:
        try:
            from kiln.auto_record_hook import version_of_print_file

            version_id = version_of_print_file(job_path)
        except Exception:  # noqa: BLE001 -- a version that cannot be read names no goal
            version_id = None
    if not (goal_id or version_id):
        return {"missed": NO_GOAL}
    local = _local_stand_ins(material_id, stand_ins, goal_id, version_id)
    if local is not _NOT_HERE:
        return local
    try:
        from kiln.server import _pro_api_call

        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            _asked_by_user=False,
            blocked_material=str(material_id),
            stand_ins=[str(s) for s in stand_ins],
            goal_id=str(goal_id or ""),
            version_id=str(version_id or ""),
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        return _stand_in_missed(classify_transport_error(exc))
    if not isinstance(answer, dict):
        return None
    miss = classify_answer(answer)
    if miss is not None:
        return _stand_in_missed(miss)
    if answer.get("known") is not True:
        return {"missed": NO_GOAL}
    return _verdicts(answer.get("stand_ins"), answer.get("purpose_known"))


def _local_stand_ins(material_id: str, stand_ins: list[str], goal_id: str | None, version_id: str | None) -> Any:
    try:
        from kiln_pro.print_fix.stand_in import stand_in_fit as engine_fit
    except ImportError:
        return _NOT_HERE
    try:
        fit = engine_fit(material_id, stand_ins, goal_id=goal_id, version_id=version_id)
    except Exception as exc:  # noqa: BLE001 -- the print door offers with its own hedge
        logger.warning("kiln-pro stand-in check failed: %s", exc)
        return None
    if not isinstance(fit, dict):
        return {"missed": NO_GOAL}
    return _verdicts(fit.get("rows"), fit.get("purpose_known"))


def _verdicts(rows: Any, purpose_known: Any) -> dict[str, Any] | None:
    """The door's answer from the served rows, or ``None`` for none."""
    if not isinstance(rows, list):
        return None
    verdicts: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("material"), str):
            continue
        suits = row.get("suits")
        verdicts[row["material"]] = {
            "suits": suits if isinstance(suits, bool) else None,
            "why": row.get("why") if isinstance(row.get("why"), str) else None,
            "offer": row.get("offer") is not False,
            **({"ask": row["ask"]} if isinstance(row.get("ask"), str) else {}),
        }
    if not verdicts:
        return None
    out: dict[str, Any] = {"verdicts": verdicts}
    if purpose_known is not True:
        out["missed"] = NO_NEEDS
    elif any(v["suits"] is None and v["offer"] and not v["why"] for v in verdicts.values()):
        note = _stand_in_plan_note()
        if note:
            out["pro_adds"] = note
    return out


def _stand_in_plan_note() -> str | None:
    """:data:`STAND_IN_PRO_ADDS` and the account's own sentence, for a plan
    without Kiln Pro; ``None`` when it is covered or cannot be read."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, why = check_tier(LicenseTier.PRO)
    except Exception:  # noqa: BLE001 -- an unreadable plan is never a pitch
        logger.debug("stand-in check: plan not read", exc_info=True)
        return None
    if covered:
        return None
    return f"{STAND_IN_PRO_ADDS} {why}".strip() if why else STAND_IN_PRO_ADDS


def _stand_in_missed(miss: Miss | None) -> dict[str, Any] | None:
    if miss is None:
        return None
    return {"missed": sentence(miss, feature="servers", then="ask to print again", **_STAND_IN_MISS_WORDS)}


__all__ = [
    "LOCAL_MODULE", "NO_GOAL", "NO_NEEDS", "PRO_ADDS", "STAND_IN_PRO_ADDS", "WIRE_TOOL", "material_fix",
    "stand_in_fit",
]
