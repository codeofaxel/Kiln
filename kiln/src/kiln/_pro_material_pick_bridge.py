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
door words with :func:`kiln.material_basics.basic_pick_line`, never a pick.
A plan that includes Kiln Pro, handed the pick every plan gets, reads one
line saying so (:func:`kiln.material_basics.pro_pick_missed_line`).  The pick is asked for
on the way to a make, so the served ask waits briefly and, after the servers
fail to answer, is left alone for a while.  Nothing here raises.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error

logger = logging.getLogger(__name__)

#: The served tool that answers Kiln Pro's pick for one part.
WIRE_TOOL = "material_pick"
#: The served tool that answers the pick on every other plan.
FLOOR_WIRE_TOOL = "material_floor_pick"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.material_floor_pick"
#: What a person reads about a miss, filled in by :mod:`kiln.served_answer`.
FEATURE = "material pick"
#: The same for Kiln Pro's own pick, when it is the one that missed.
PRO_FEATURE = "Pro pick"
#: A pick is asked on the way to a make; the miss is said if this passes.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the servers fail to answer, how long the pick is not asked for; a
#: miss said again meanwhile says when Kiln asks again.
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


def _already_missed() -> Miss | None:
    """The miss of a call Kiln's servers were already asked for and did not
    answer (``kiln.served_public`` runs it here after that), so they are not
    asked a second time; ``None`` otherwise."""
    try:
        from kiln import served_public

        if served_public.already_missed():
            _answer, miss = served_public.answer_or_missed(FLOOR_WIRE_TOOL, {})
            return miss if isinstance(miss, Miss) else Miss("unanswered")
    except Exception:  # noqa: BLE001 -- unreadable: ask as usual
        logger.debug("served_public state unreadable", exc_info=True)
    return None


def _served(requirements_text: str, printer: dict[str, Any]) -> tuple[Any, Miss | None]:
    held = _already_missed()
    if held is not None:
        return None, held
    wait = _service_down_until - time.monotonic()
    if wait > 0:
        # Said again, not asked: when the servers last went unanswered, and
        # how long until Kiln asks them again.
        last = _last_miss or Miss("unanswered", code="SERVER_UNREACHABLE")
        return None, Miss(last.cause, last.code, last.detail, retry_after_s=wait)
    pro_note: str | None = None
    if _plan_includes_the_pick():
        rec, miss = _ask(WIRE_TOOL, requirements_text, printer)
        # Kiln Pro's pick, or a miss asking again would only repeat; a part
        # it has nothing for, or a plan the servers do not count as Kiln Pro,
        # is asked as every other plan is, and the answer says so once.
        if rec is not None or (miss is not None and miss.cause != "refused"):
            return rec, miss
        from kiln.material_basics import pro_pick_missed_line

        pro_note = pro_pick_missed_line(_why_not_kiln_pros(miss), feature=PRO_FEATURE)
    rec, miss = _ask(FLOOR_WIRE_TOOL, requirements_text, printer)
    if rec is None and miss is None:
        miss = Miss("unanswered", detail="no pick in the answer")
    if rec is not None and pro_note is not None:
        rec.warnings.insert(0, pro_note)
    return rec, miss


#: What fixes a plan the servers do not count as Kiln Pro while this
#: computer's record of the sign-in does.
_PLAN_RECORD_FIX = (
    "Kiln's servers do not count this computer's sign-in as Kiln Pro; signing in "
    "again brings this computer's record of the plan up to date"
)


#: Words that offer a plan: the pricing page, or "needs Kiln <plan>".
_OFFERS_A_PLAN = re.compile(r"kiln3d\.com/pricing|\bneeds Kiln (?:Pro|Business|Enterprise)\b")


def _why_not_kiln_pros(miss: Miss | None) -> Miss | None:
    """Why Kiln Pro's pick did not come, for a caller this computer counts as
    holding Kiln Pro: the servers' own words, except words that offer the
    plan, which are never said to someone who holds it.  ``None`` when Kiln
    Pro's pick answered and had nothing for the part."""
    if miss is None:
        return None
    if _OFFERS_A_PLAN.search(miss.detail or ""):
        return Miss(miss.cause, miss.code, _PLAN_RECORD_FIX)
    return miss


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
    """The servers did not answer: leave the pick alone for a while so a make
    does not wait on it again and again, and meanwhile say that miss again
    with the time left.  Only a server that did not answer; an offline
    computer, a sign-in and a refusal are asked again at once, since each
    gets the same miss until something changes and the change is the
    person's to make."""
    global _service_down_until, _last_miss
    if miss.cause == "unanswered":
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


__all__ = ["FEATURE", "FLOOR_WIRE_TOOL", "LOCAL_MODULE", "PRO_FEATURE", "WIRE_TOOL", "pick"]
