"""Public-Kiln → Kiln's servers: Kiln's material pick for one part.

``kiln.design_intelligence.recommend_material_for_design`` answers "what
material should this part be?".  The pick is Kiln's (https://kiln3d.com),
and this module is where it is asked for: the sentence describing the part
and the printer's few capabilities go in, the pick comes back with its
reasons, warnings and runners-up.  Every signed-in plan gets a pick; Kiln Pro
(https://kiln3d.com/pricing) gets Kiln Pro's own.  Nothing is kept on either
side.

Asked in one of two places, in order:

1. **kiln-pro on this computer** (Kiln's servers), imported by name in
   :func:`_local`, never by a string, so a static reader of the call graph
   sees the crossing.
2. **served**: Kiln's servers answer through the door every served tool uses
   (:func:`kiln.server._pro_api_call`): :data:`WIRE_TOOL` for Kiln Pro's pick
   when the signed-in plan includes it, and :data:`FLOOR_WIRE_TOOL` for every
   other plan and for a part Kiln Pro's pick has nothing for.

Each call answers ``(pick, None)`` or ``(None, miss)``: a miss (offline,
signed out, no answer, refused) is a :class:`kiln.served_answer.Miss` the
door words with :func:`miss_sentence`, never a pick.  The pick is asked for
on the way to a make, so the served ask waits briefly and, after the servers
fail to answer, is left alone for a while.  Nothing here raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, sentence

logger = logging.getLogger(__name__)

#: The served tool that answers Kiln Pro's pick for one part.
WIRE_TOOL = "material_pick"
#: The served tool that answers the pick on every other plan.
FLOOR_WIRE_TOOL = "material_floor_pick"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.material_floor_pick"
#: What a person reads about a miss, filled in by :mod:`kiln.served_answer`.
FEATURE = "material pick"
#: A pick is asked on the way to a make; the miss is said if this passes.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the servers fail to answer, how long the pick is not asked for.
SERVICE_BACKOFF_S: float = 300.0

_service_down_until: float = 0.0
_last_miss: Miss | None = None


def pick(requirements_text: str, printer: dict[str, Any]) -> tuple[Any, Miss | None]:
    """Kiln's pick for one part as public Kiln's ``MaterialRecommendation``,
    or ``(None, miss)`` saying why there is none.  *printer* is the four
    capability fields ``recommend_material_for_design`` takes.  Never
    raises."""
    local = _local(requirements_text, printer)
    if local is not _NOT_HERE:
        return local
    return _served(requirements_text, printer)


def miss_sentence(miss: Miss | None) -> str:
    """The one line a door says when no pick came: Kiln did not pick a
    plastic for this part, why, and what fixes it."""
    if miss is None:
        miss = Miss("unanswered", detail="no pick")
    return sentence(
        miss,
        feature=FEATURE,
        on_the_line="Kiln did not pick a plastic for this part",
        cannot="pick one",
        wont="lists the plastics this printer can melt instead",
        then="Kiln picks the plastic for this part" if miss.cause == "signed_out" else "ask again",
    )


#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def _local(requirements_text: str, printer: dict[str, Any]) -> Any:
    try:
        from kiln_pro.material_floor_pick import pick_for_part
    except ImportError:
        return _NOT_HERE
    try:
        rec = pick_for_part(requirements_text, **printer)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        from kiln.served_answer import local_fault

        return None, local_fault(exc, where="the material pick on this computer")
    from kiln.design_intelligence import MaterialRecommendation

    if isinstance(rec, MaterialRecommendation):
        return rec, None
    return None, Miss("unanswered", detail="the material pick on this computer gave no pick")


def _plan_includes_the_pick() -> bool:
    """The signed-in account's plan covers Kiln Pro (read on this computer,
    no network).  Anything unreadable is the free plan."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, _why = check_tier(LicenseTier.PRO)
        return bool(covered)
    except Exception:  # noqa: BLE001
        return False


def _served(requirements_text: str, printer: dict[str, Any]) -> tuple[Any, Miss | None]:
    if time.monotonic() < _service_down_until:
        return None, _last_miss or Miss("unanswered", code="SERVER_UNREACHABLE", detail="not asked again yet")
    if _plan_includes_the_pick():
        rec, miss = _ask(WIRE_TOOL, requirements_text, printer)
        # Kiln Pro's pick, or a miss asking again would only repeat; a part
        # it has nothing for, or a plan the servers do not count as Kiln Pro,
        # is asked as every other plan is.
        if rec is not None or (miss is not None and miss.cause != "refused"):
            return rec, miss
    rec, miss = _ask(FLOOR_WIRE_TOOL, requirements_text, printer)
    if rec is None and miss is None:
        miss = Miss("unanswered", detail="no pick in the answer")
    return rec, miss


def _ask(tool: str, requirements_text: str, printer: dict[str, Any]) -> tuple[Any, Miss | None]:
    """One served ask: ``(pick, None)``, ``(None, miss)``, or ``(None, None)``
    when the servers answered and had no pick for the part."""
    try:
        import kiln.server as _srv
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", detail="the served door is not available")
    try:
        answer = _srv._pro_api_call(
            tool,
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
        logger.debug("%s request failed", tool, exc_info=True)
        return None, _note_miss(classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None)))
    if not (isinstance(answer, dict) and answer.get("success") is True and answer.get("status") == "ok"):
        return None, _note_miss(classify_answer(answer) or Miss("unanswered", detail="no pick in the answer"))
    if answer.get("known") is not True:
        return None, None
    rebuild = _rebuild if tool == WIRE_TOOL else _rebuild_floor
    rec = rebuild(answer.get("pick"))
    if rec is None:
        return None, Miss("unanswered", detail="the pick in the answer could not be read")
    return rec, None


def _note_miss(miss: Miss) -> Miss:
    """The servers did not answer (not a refusal): leave the pick alone for a
    while so a make does not wait on it again and again, and say the same
    miss meanwhile."""
    global _service_down_until, _last_miss
    if miss.no_answer:
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _last_miss = miss
    logger.debug("material pick not served (%s)", miss.cause)
    return miss


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [dict(v) for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _score(pick: dict[str, Any]) -> float | None:
    try:
        return float(pick.get("score"))
    except (TypeError, ValueError):
        return None


def _rebuild(pick: Any) -> Any:
    """Kiln Pro's served pick as public Kiln's ``MaterialRecommendation``, or
    ``None`` when it is not one."""
    from kiln.design_intelligence import MaterialRecommendation, get_material_profile

    if not isinstance(pick, dict) or not isinstance(pick.get("material_id"), str):
        return None
    profile = get_material_profile(pick["material_id"])
    score = _score(pick)
    if profile is None or score is None:
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


def _rebuild_floor(pick: Any) -> Any:
    """The served pick on every other plan as public Kiln's
    ``MaterialRecommendation``, or ``None`` when it is not one: the material
    as this computer's own catalogue records it (its name, family, heat and
    chemistry), and, when nothing met the constraints, that material's own
    design limits as the summary."""
    from kiln.design_intelligence import (
        MaterialProfile,
        MaterialRecommendation,
        get_public_material_profile,
    )

    if not isinstance(pick, dict) or not isinstance(pick.get("material_id"), str):
        return None
    record = get_public_material_profile(pick["material_id"])
    score = _score(pick)
    if record is None or score is None:
        return None
    return MaterialRecommendation(
        material=MaterialProfile(
            material_id=record.material_id,
            display_name=record.display_name,
            category=record.category,
            thermal=record.thermal,
            chemical=record.chemical,
        ),
        score=score,
        reasons=_strings(pick.get("reasons")),
        warnings=_strings(pick.get("warnings")),
        design_limits_summary=record.design_limits if pick.get("fallback") is True else {},
        alternatives=_dicts(pick.get("alternatives")),
        recommended_brands=_dicts(pick.get("recommended_brands")),
    )


__all__ = ["FEATURE", "FLOOR_WIRE_TOOL", "LOCAL_MODULE", "WIRE_TOOL", "miss_sentence", "pick"]
