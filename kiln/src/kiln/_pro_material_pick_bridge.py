"""Public-Kiln → Kiln's servers: Kiln Pro's material pick for one part.

``kiln.design_intelligence.recommend_material_for_design`` answers "what
material should this part be?".  Every install answers it with the safety
floor (thermal, chemical and process limits and their warnings).  Kiln Pro
(https://kiln3d.com/pricing) answers with Kiln's own pick, and this module is
where that pick is asked for: the sentence describing the part and the
printer's few capabilities go in, the pick comes back with its reasons,
warnings and runners-up.  Nothing is kept on either side.

Asked in one of two places, in order:

1. **kiln-pro on this computer** (the hosted server, and an install with
   kiln-pro), imported by name in :func:`_local`, never by a string, so a
   static reader of the call graph sees the crossing.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked
   through the door every served tool uses (:func:`kiln.server._pro_api_call`)
   -- only when the signed-in plan includes Kiln Pro, so a free install
   never waits on a network call for a pick its plan does not include.

A miss (offline, signed out, no answer, refused) answers ``None`` and the
safety floor answers instead, with every warning the floor gives.  The pick
is asked for on the way to a make, so the served ask waits briefly and,
after the servers fail to answer, is left alone for a while.  Nothing here
raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error

logger = logging.getLogger(__name__)

#: The served tool that answers the pick for one part.
WIRE_TOOL = "material_pick"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.material_recommendation"
#: A pick is asked on the way to a make; the floor answers if this passes.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the servers fail to answer, how long the pick is not asked for.
SERVICE_BACKOFF_S: float = 300.0

_service_down_until: float = 0.0


def recommend(requirements_text: str, printer: dict[str, Any]) -> Any:
    """Kiln Pro's pick as public Kiln's ``MaterialRecommendation``, or
    ``None`` for the safety floor.  *printer* is the four capability fields
    ``recommend_material_for_design`` takes.  Never raises."""
    local = _local(requirements_text, printer)
    if local is not _NOT_HERE:
        return local
    if not _plan_includes_the_pick():
        return None
    return _served(requirements_text, printer)


#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def _local(requirements_text: str, printer: dict[str, Any]) -> Any:
    try:
        from kiln_pro.material_recommendation import recommend_material
    except ImportError:
        return _NOT_HERE
    try:
        rec = recommend_material(requirements_text, **printer)
    except Exception as exc:  # noqa: BLE001 -- the safety floor still answers
        logger.warning("kiln-pro material pick failed, answering with the safety floor: %s", exc)
        return None
    from kiln.design_intelligence import MaterialRecommendation

    return rec if isinstance(rec, MaterialRecommendation) else None


def _plan_includes_the_pick() -> bool:
    """The signed-in account's plan covers Kiln Pro (read on this computer,
    no network).  Anything unreadable is the free plan."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, _why = check_tier(LicenseTier.PRO)
        return bool(covered)
    except Exception:  # noqa: BLE001
        return False


def _served(requirements_text: str, printer: dict[str, Any]) -> Any:
    global _service_down_until
    if time.monotonic() < _service_down_until:
        return None
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # The person asked what material to use; the pick is what they
            # reached for.
            _asked_by_user=True,
            requirements_text=str(requirements_text or ""),
            printer_has_enclosure=bool(printer.get("printer_has_enclosure", False)),
            printer_has_direct_drive=bool(printer.get("printer_has_direct_drive", True)),
            max_hotend_temp_c=int(printer.get("max_hotend_temp_c", 300)),
            supported_materials=printer.get("supported_materials"),
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        _note_miss(classify_transport_error(exc))
        return None
    if not (isinstance(answer, dict) and answer.get("success") is True and answer.get("status") == "ok"):
        _note_miss(classify_answer(answer) if isinstance(answer, dict) else None)
        return None
    if answer.get("known") is not True:
        return None
    return _rebuild(answer.get("pick"))


def _note_miss(miss: Miss | None) -> None:
    """The servers did not answer (not a refusal): leave the pick alone for a
    while so a make does not wait on it again and again."""
    global _service_down_until
    if miss is not None and miss.cause in ("offline", "unanswered"):
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
    logger.debug("material pick not served (%s); answering with the safety floor", miss and miss.cause)


def _rebuild(pick: Any) -> Any:
    """The served pick as public Kiln's ``MaterialRecommendation``, or
    ``None`` when it is not one."""
    from kiln.design_intelligence import MaterialRecommendation, get_material_profile

    if not isinstance(pick, dict) or not isinstance(pick.get("material_id"), str):
        return None
    profile = get_material_profile(pick["material_id"])
    if profile is None:
        return None

    def _strings(value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    def _dicts(value: Any) -> list[dict[str, Any]]:
        return [dict(v) for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    try:
        score = float(pick.get("score"))
    except (TypeError, ValueError):
        return None
    return MaterialRecommendation(
        material=profile,
        score=score,
        reasons=_strings(pick.get("reasons")),
        warnings=_strings(pick.get("warnings")),
        design_limits_summary=profile.design_limits,
        alternatives=_dicts(pick.get("alternatives")),
        recommended_brands=_dicts(pick.get("recommended_brands")),
    )


__all__ = ["LOCAL_MODULE", "WIRE_TOOL", "recommend"]
