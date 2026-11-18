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

import copy
import logging
import math
import re
import time
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, plan_record_fix

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
#: What the sign-in sentence calls each pick.
SIGN_IN_NAME = f"Kiln's {FEATURE}"
PRO_SIGN_IN_NAME = "Kiln Pro's material pick"
#: A pick is asked on the way to a make; the miss is said if this passes.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the servers fail to answer, how long the pick is not asked for; a
#: miss said again meanwhile says when Kiln asks again.
SERVICE_BACKOFF_S: float = 300.0
#: The longest description of one part the pick reads, on every path.
MAX_REQUIREMENTS_CHARS = 2000
#: What a longer description is told, once, with nothing asked.
TOO_LONG = f"Describe the part in {MAX_REQUIREMENTS_CHARS:,} characters or fewer."
#: The hotend ceiling a printer is taken to have when none is given.
_DEFAULT_CEILING_C = 300
#: What a hotend ceiling that is not a number is told, with nothing asked.
BAD_CEILING = "Give the printer's hotend ceiling as a number of degrees C."

_service_down_until: float = 0.0
_last_miss: Miss | None = None


def pick(requirements_text: str, printer: dict[str, Any]) -> tuple[Any, Miss | None]:
    """Kiln's pick for one part as public Kiln's ``MaterialRecommendation``,
    or ``(None, miss)`` saying why there is none.  *printer* is the four
    capability fields ``recommend_material_for_design`` takes.  Never
    raises.

    A description longer than :data:`MAX_REQUIREMENTS_CHARS` is refused here
    with :data:`TOO_LONG`, and a hotend ceiling that is not a finite number
    with :data:`BAD_CEILING`, nothing asked.  Within one tool call
    (:func:`kiln.tool_context.call_memo`) the same part on the same printer
    is asked for once: a door whose helpers each read the analysis gets one
    answer, each helper its own copy."""
    if isinstance(requirements_text, str) and len(requirements_text) > MAX_REQUIREMENTS_CHARS:
        return None, Miss("refused", code="INVALID_ARGUMENT", detail=TOO_LONG)
    if _hotend_ceiling(printer) is None:
        return None, Miss("refused", code="INVALID_ARGUMENT", detail=BAD_CEILING)
    memo, key = _call_memo(), _memo_key(requirements_text, printer)
    if memo is not None and key is not None and key in memo:
        return copy.deepcopy(memo[key])
    answer = _local(requirements_text, printer)
    if answer is _NOT_HERE:
        answer = _served(requirements_text, printer)
    if memo is not None and key is not None:
        memo[key] = copy.deepcopy(answer)
    return answer


def _hotend_ceiling(printer: dict[str, Any]) -> int | None:
    """The printer's hotend ceiling in whole degrees, or ``None`` when what
    was given is not a finite number (``None``, NaN, infinity, a yes/no,
    words).  Left out, it is :data:`_DEFAULT_CEILING_C`."""
    raw = printer.get("max_hotend_temp_c", _DEFAULT_CEILING_C)
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def _call_memo() -> dict[Any, Any] | None:
    try:
        from kiln.tool_context import call_memo

        return call_memo()
    except Exception:  # noqa: BLE001 -- no memo: ask as usual
        return None


def _memo_key(requirements_text: Any, printer: dict[str, Any]) -> Any:
    """The memo key for one part on one printer, or ``None`` when it cannot
    be one."""
    supported = printer.get("supported_materials")
    key = (
        "material_pick",
        requirements_text,
        printer.get("printer_has_enclosure"),
        printer.get("printer_has_direct_drive"),
        printer.get("max_hotend_temp_c"),
        tuple(supported) if isinstance(supported, list) else supported,
    )
    try:
        hash(key)
    except TypeError:
        return None
    return key


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

        return None, local_fault(exc, where="Kiln's material pick")
    from kiln.design_intelligence import MaterialRecommendation

    if isinstance(rec, MaterialRecommendation):
        return rec, None
    return None, Miss("unanswered", detail="Kiln's material pick gave no pick")


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
        # This computer counts the caller as holding Kiln Pro: the pick every
        # plan gets keeps its load caution and says nothing about the plan.
        rec.warnings = [pro_note, *_without_plan_line(rec.warnings)]
    return rec, miss


def _without_plan_line(warnings: list[str]) -> list[str]:
    """*warnings* with the plan sentence the load caution may carry removed,
    the caution itself kept word for word."""
    from kiln.design_intelligence import _PICK_LOAD_CAUTION

    return [
        _PICK_LOAD_CAUTION if w.startswith(_PICK_LOAD_CAUTION) else w
        for w in warnings
    ]


#: Words that offer a plan: the pricing page, or "needs Kiln <plan>".
_OFFERS_A_PLAN = re.compile(r"kiln3d\.com/pricing|\bneeds Kiln (?:Pro|Business|Enterprise)\b")


def _why_not_kiln_pros(miss: Miss | None) -> Miss | None:
    """Why Kiln Pro's pick did not come, for a caller this computer counts as
    holding Kiln Pro: the servers' own words, except words that offer the
    plan, which are never said to someone who holds it (a plan wall in any
    words is read that way for every door, in :mod:`kiln.served_answer`).
    ``None`` when Kiln Pro's pick answered and had nothing for the part."""
    if miss is None:
        return None
    if _OFFERS_A_PLAN.search(miss.detail or ""):
        return Miss(miss.cause, miss.code, plan_record_fix("pro"))
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
            _feature=PRO_SIGN_IN_NAME if tool == WIRE_TOOL else SIGN_IN_NAME,
            requirements_text=str(requirements_text or ""),
            printer_has_enclosure=bool(printer.get("printer_has_enclosure", False)),
            printer_has_direct_drive=bool(printer.get("printer_has_direct_drive", True)),
            max_hotend_temp_c=_hotend_ceiling(printer),
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


def _named_on_the_wire(pick: dict[str, Any]) -> Any:
    """The picked material as the servers named it (its name and family),
    for a material this computer's catalogue does not record: a newer one
    than this install knows still shows, with no heat or chemistry beside it
    that this computer cannot vouch for.  Unnamed, its id is its name."""
    from kiln.design_intelligence import MaterialProfile

    name = pick.get("display_name")
    category = pick.get("category")
    return MaterialProfile(
        material_id=pick["material_id"],
        display_name=name if isinstance(name, str) and name.strip() else pick["material_id"],
        category=category if isinstance(category, str) else "",
        thermal={},
        chemical={},
    )


def _rebuild(pick: Any) -> Any:
    """Kiln Pro's served pick as public Kiln's ``MaterialRecommendation``, or
    ``None`` when it is not one."""
    from kiln.design_intelligence import MaterialRecommendation, get_material_profile

    if not isinstance(pick, dict) or not isinstance(pick.get("material_id"), str):
        return None
    score = _score(pick)
    if score is None:
        return None
    profile = get_material_profile(pick["material_id"]) or _named_on_the_wire(pick)
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
    design limits as the summary.  A material the catalogue does not record
    is shown as the servers named it."""
    from kiln.design_intelligence import (
        MaterialProfile,
        MaterialRecommendation,
        get_public_material_profile,
    )

    if not isinstance(pick, dict) or not isinstance(pick.get("material_id"), str):
        return None
    score = _score(pick)
    if score is None:
        return None
    record = get_public_material_profile(pick["material_id"])
    if record is None:
        material, limits = _named_on_the_wire(pick), {}
    else:
        material = MaterialProfile(
            material_id=record.material_id,
            display_name=record.display_name,
            category=record.category,
            thermal=record.thermal,
            chemical=record.chemical,
        )
        limits = record.design_limits if pick.get("fallback") is True else {}
    return MaterialRecommendation(
        material=material,
        score=score,
        reasons=_strings(pick.get("reasons")),
        warnings=_strings(pick.get("warnings")),
        design_limits_summary=limits,
        alternatives=_dicts(pick.get("alternatives")),
        recommended_brands=_dicts(pick.get("recommended_brands")),
    )


__all__ = [
    "BAD_CEILING", "FEATURE", "FLOOR_WIRE_TOOL", "LOCAL_MODULE", "MAX_REQUIREMENTS_CHARS", "PRO_FEATURE",
    "PRO_SIGN_IN_NAME", "SIGN_IN_NAME", "TOO_LONG", "WIRE_TOOL",
    "pick",
]
