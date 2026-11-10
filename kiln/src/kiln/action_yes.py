"""A person's yes to an order or to making a design public, typed as the
code Kiln shows on this machine's screen.

Kiln's servers hold both actions until the person says yes, and say so with
``APPROVAL_REQUIRED``, the question, the person's page (``approve_at``) and
a request id (``challenge_id``).  The page is always a door.  On the machine
Kiln is installed on there is a second: :func:`offer` shows a code beside
the question (:mod:`kiln.screen_code`), the person types it in the chat,
the agent relays it through ``give_print_code``, and :func:`redeem` tells
the servers the person answered.  Then the same call goes through.

What it proves is what a print's code proves: someone who could see this
screen typed it.  The servers take this install's word for that, and record
the yes as given on its screen.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from kiln import screen_code

logger = logging.getLogger(__name__)

APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

#: What each door calls again once the person has answered.
_AGAIN = {
    "fulfillment_order": "call fulfillment_order again with the same arguments",
    "set_design_public": "call set_design_public again with the same preview_token",
}


def _key(tool: str, challenge: dict[str, Any]) -> str:
    """One name per thing asked about, so asking twice shows one code."""
    about = str(challenge.get("approve_at") or challenge.get("question") or "")
    return f"{tool}-{hashlib.sha256(about.encode('utf-8')).hexdigest()[:16]}"


def waiting_ask(reply: Any) -> dict[str, Any] | None:
    """The servers' ask inside *reply* (``question``, ``approve_at``,
    ``challenge_id``), or ``None`` when *reply* is not a wait for the
    person's yes or carries no request a code could answer."""
    if not isinstance(reply, dict):
        return None
    said = reply.get("error")
    code = reply.get("code") or (said.get("code") if isinstance(said, dict) else None)
    if code != APPROVAL_REQUIRED:
        return None
    inner = reply.get("details") if isinstance(reply.get("details"), dict) else {}
    ask = {k: reply.get(k) or inner.get(k) for k in ("question", "approve_at", "challenge_id")}
    return ask if ask.get("challenge_id") else None


def offer(tool: str, ask: dict[str, Any], *, host: str = "") -> dict[str, Any]:
    """Show the person a code for *ask* and return what the door adds to its
    reply: ``code_shown`` and ``code_next`` (what the agent does with the
    code).  ``{}`` when no code could be shown: the page is then the door,
    and the reply already names it.  The code is never in the result."""
    from kiln.runtime_env import is_hosted_multitenant

    if tool not in screen_code.ACTION_TOOLS or is_hosted_multitenant() or not ask.get("challenge_id"):
        return {}
    question = str(ask.get("question") or "").strip().splitlines()[0:1]
    shown = screen_code.issue(
        tool=tool, file_name=_key(tool, ask), file_sha256="", printer_name=screen_code.ACTION_SCOPE,
        host=host, subject=question[0] if question else "", ticket=str(ask["challenge_id"]),
    )
    outcome = shown.get("outcome")
    if outcome == screen_code.SHOWN:
        return {
            "code_shown": outcome,
            "code_next": (
                "Or, at this computer: a code was just shown on the person's screen beside the question. "
                "Ask them to type it, pass their exact words to give_print_code, then "
                f"{_AGAIN[tool]}. Nothing you can call reveals the code."
            ),
        }
    if outcome == screen_code.COOLDOWN:
        return {"code_shown": outcome}
    return {}


def with_offer(tool: str, reply: Any, *, host: str = "") -> Any:
    """*reply* as a door hands it back: unchanged, or, when it is the
    servers waiting for the person's yes, with the code offer beside it."""
    ask = waiting_ask(reply)
    if ask is None:
        return reply
    extra = offer(tool, ask, host=host)
    return {**reply, **extra} if extra else reply


def redeem(issued: screen_code.Issued) -> dict[str, Any]:
    """Tell the servers the person answered the code shown for *issued*.
    ``{"success": True, "next": ...}``, or ``{"success": False, "code",
    "error"}`` saying why the yes was not recorded and what to do."""
    from kiln import bridge_client

    ok, code, said = bridge_client.confirm_on_screen(issued.ticket)
    again = _AGAIN.get(issued.tool, "call the tool again")
    if ok:
        return {"success": True, "next": f"The person said yes. Now {again}."}
    if code == "offline":
        error = "The code was right, but Kiln's servers could not be reached to record the yes. "
    elif code == "signed_out":
        error = "The code was right, but this Kiln is not signed in, so the yes could not be recorded. "
    elif code in ("challenge_unknown", "challenge_spent"):
        error = "The code was right, but the question it answered has run out. "
    else:
        error = (said + " ") if said else "The code was right, but Kiln's servers did not record the yes. "
    logger.info("action yes: %s not recorded (%s)", issued.tool, code)
    return {
        "success": False, "code": "YES_NOT_RECORDED", "why": code,
        "error": error + f"To ask again, {again}; a new code is shown. The person's page works too.",
    }
