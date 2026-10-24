"""Public-Kiln → Kiln's servers: Kiln Pro's plan for one failed print.

Every install spots a failure (``kiln.print_recovery``'s detectors, the
failure classifier) and answers with the free floor: what happened in one
line, the safety lines for it, and the safe thing to do now.  Kiln Pro
(https://kiln3d.com/pricing) answers with Kiln's own plan for saving the
print: the strategy, the layer to resume from, the G-code, the settings to
change.  This module is where that plan is asked for.  The failure's own
facts go in (its type, layers, heights, material and temperatures, or the
two printer abilities a fix plan reads), never a printer name, a job name or
a file path; nothing is kept on either side.

Asked in one of two places, in order:

1. **kiln-pro on this computer** (the hosted server, and an install with
   kiln-pro), imported by name in :func:`_local`, never by a string, so a
   static reader of the call graph sees the crossing.  It checks the
   caller's plan itself.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked
   through the door every served tool uses (:func:`kiln.server._pro_api_call`)
   -- only when the signed-in plan includes Kiln Pro, so a free install
   never waits on a network call for a plan it does not include.

No plan is the free floor, and :func:`floor_note` says why once: the plan
does not include it (with what Kiln Pro adds for this failure, or nothing
when it adds nothing), the sign-in ended, or a paid plan could not be
fetched (offline, signed out, no answer, refused).  After the servers fail
to answer, they are left alone for a while.  Nothing here raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, fields, local_fault, sentence

logger = logging.getLogger(__name__)

#: The served tool that answers the plan for one failure.
WIRE_TOOL = "recovery_plan"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.recovery.planner"
#: A plan is asked on the way to an answer about a failed print; the floor
#: answers if this passes.
_CONSULT_TIMEOUT_S: float = 6.0
#: After the servers fail to answer, how long a plan is not asked for.
SERVICE_BACKOFF_S: float = 300.0
#: The tool a person reaches for a plan through, for the pricing link's tag.
_PRICING_TOOL = "plan_failure_recovery"

_service_down_until: float = 0.0


@dataclass(frozen=True)
class Asked:
    """What came of asking for Kiln Pro's plan.

    ``answer`` is the served payload (``plan``, and ``gcode`` for a session)
    when Kiln Pro answered.  Otherwise ``included`` says whether the
    caller's plan includes Kiln Pro, and ``miss`` why it did not arrive."""

    answer: dict[str, Any] | None = None
    included: bool = False
    miss: Miss | None = None


def ask(kind: str, failure: dict[str, Any]) -> Asked:
    """Kiln Pro's ``kind`` plan (``"session"`` or ``"fix"``) for one failure
    described by *failure*'s own facts.  Never raises."""
    local = _local(kind, failure)
    if local is not None:
        return local
    if not _plan_includes_pro():
        return Asked()
    return _served(kind, failure)


def floor_note(asked: Asked, pro_adds: str) -> dict[str, Any]:
    """What a free-floor plan tells the person about Kiln Pro's plan, once.

    *pro_adds* is what Kiln Pro would add for THIS failure, or ``""`` when
    its plan is the same safe step (a thermal runaway, a spaghetti print):
    then the floor names no plan and no price.
    """
    note: dict[str, Any] = {"floor": True}
    if asked.included:
        # A paying caller whose plan did not arrive is told why, and never
        # pitched the plan they already hold.
        miss = asked.miss or Miss("unanswered", detail="no plan in the answer")
        note["pro_plan_missing"] = sentence(
            miss,
            on_the_line="This is the safe answer, not Kiln Pro's recovery plan",
            feature="recovery planner",
            cannot="plan the recovery",
            wont="gives the safe answer instead",
        )
        note.update(fields(miss))
        return note
    lapsed = _lapsed_signin()
    if lapsed:
        # The account may well hold the plan: sign back in, never "upgrade".
        note["sign_in_again"] = lapsed
        return note
    if pro_adds:
        note["pro_adds"] = pro_adds
        note["required_tier"] = "pro"
        note["upgrade_url"] = _upgrade_url()
    return note


def _local(kind: str, failure: dict[str, Any]) -> Asked | None:
    """kiln-pro's answer on this computer, or ``None`` when kiln-pro is not
    here (or predates the planner) and the servers are to be asked."""
    try:
        from kiln_pro.recovery.planner import answer as local_answer
    except ImportError:
        return None
    try:
        out = local_answer(kind, failure)
    except Exception as exc:  # noqa: BLE001 -- the floor still answers
        logger.warning("kiln-pro recovery plan failed, answering with the safe floor: %s", exc)
        return Asked(included=True, miss=local_fault(exc))
    if isinstance(out, dict) and out.get("success") is True and out.get("known") is True:
        return Asked(answer=out, included=True)
    if isinstance(out, dict) and out.get("required_tier"):
        # The caller's plan is below Kiln Pro (on the hosted server, the
        # person on this request): the floor, worded as for any free caller.
        return Asked()
    return Asked(included=True, miss=classify_answer(out) or Miss("unanswered", detail="no plan in the answer"))


def _plan_includes_pro() -> bool:
    """The signed-in account's plan covers Kiln Pro.  Anything unreadable is
    the free plan."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, _why = check_tier(LicenseTier.PRO)
        return bool(covered)
    except Exception:  # noqa: BLE001
        return False


def _served(kind: str, failure: dict[str, Any]) -> Asked:
    global _service_down_until
    if time.monotonic() < _service_down_until:
        return Asked(included=True, miss=Miss("unanswered", detail="Kiln's servers did not answer a moment ago"))
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return Asked(included=True, miss=Miss("unanswered", detail="this install could not open its connection"))
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # The person asked what to do about a failed print; the plan is
            # what they reached for.
            _asked_by_user=True,
            kind=kind,
            failure=dict(failure),
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        return _note_miss(classify_transport_error(exc))
    if isinstance(answer, dict) and answer.get("success") is True and answer.get("known") is True:
        return Asked(answer=answer, included=True)
    return _note_miss(
        classify_answer(answer) if isinstance(answer, dict) else None
    )


def _note_miss(miss: Miss | None) -> Asked:
    """The servers did not answer (not a refusal): leave the plan alone for a
    while so every answer about a failure does not wait on it again."""
    global _service_down_until
    miss = miss or Miss("unanswered", detail="an answer with no plan in it")
    if miss.cause in ("offline", "unanswered"):
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
    logger.debug("recovery plan not served (%s); answering with the safe floor", miss.cause)
    return Asked(included=True, miss=miss)


def _lapsed_signin() -> str:
    try:
        from kiln.auth_session import lapsed_signin_message

        return lapsed_signin_message()
    except Exception:  # noqa: BLE001
        return ""


def _upgrade_url() -> str:
    try:
        from kiln.tiers_and_terms import upgrade_link

        return upgrade_link(_PRICING_TOOL)
    except Exception:  # noqa: BLE001
        return "https://kiln3d.com/pricing"


__all__ = ["LOCAL_MODULE", "WIRE_TOOL", "Asked", "ask", "floor_note"]
