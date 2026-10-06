"""Public-Kiln -> kiln-pro bridge for one question: does this material meet
the rules a part must meet before it prints (food contact, REACH, RoHS,
flame retardancy, UV)?

The record and its reasoning are kiln-pro's (https://kiln3d.com).  With
kiln-pro installed here the answer is read locally; otherwise this asks Kiln's
servers for the one material and the requirements named, and relays the
block it gets back as it is.  Every caller gets the material's warnings; the
per-requirement verdict is a Kiln Business feature
(https://kiln3d.com/pricing), decided by the servers from the account asking.

A call that gets no answer -- offline, signed out, the servers silent or
saying no -- is said in the shared voice (:mod:`kiln.served_answer`) and
never reads as a requirement met: ``checked`` is ``False`` and nothing is
judged.  Nothing is kept on this computer.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The hosted door, asked when kiln-pro is not installed here.
WIRE_TOOL = "material_rule_check"
#: Where the same answer is read when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.material_rules.engine"

__all__ = ["LOCAL_MODULE", "WIRE_TOOL", "rule_checks"]


def rule_checks(material: str, requirements: list[str]) -> dict[str, Any]:
    """The rule-check block for *material* against *requirements*.

    Read locally when kiln-pro is installed, else served.  Never raises:
    a miss comes back as ``{"success": False, "checked": False, ...}``
    with the sentence and its cause.
    """
    try:
        check = importlib.import_module(LOCAL_MODULE).rule_check
    except (ImportError, AttributeError):
        return _served(material, requirements)
    try:
        return check(material, requirements)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        logger.debug("local material rule check failed", exc_info=True)
        from kiln.served_answer import Miss

        return _miss(Miss("unanswered", detail=str(exc)[:200]), requirements)


def _served(material: str, requirements: list[str]) -> dict[str, Any]:
    from kiln import served_answer

    try:
        import kiln.server as _srv

        answer = _srv._pro_api_call(WIRE_TOOL, material=material, requirements=list(requirements))
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        import kiln.server as _srv

        host = getattr(_srv, "_HOSTED_KILN_API_URL", None)
        return _miss(served_answer.classify_transport_error(exc, host=host), requirements)
    if isinstance(answer, dict) and (
        (answer.get("success") is True and isinstance(answer.get("requirements"), list))
        # The server's own ruling on a request it could not read, relayed as it is.
        or (answer.get("success") is False and answer.get("code") == "INVALID_INPUT")
    ):
        return answer
    miss = served_answer.classify_answer(answer) or served_answer.Miss(
        "unanswered", detail="no rule check in the answer"
    )
    return _miss(miss, requirements)


def _miss(miss: Any, requirements: list[str]) -> dict[str, Any]:
    """The block when the check could not be made: what Kiln could not do,
    why, and that no requirement was judged."""
    from kiln import served_answer

    text = served_answer.sentence(
        miss,
        feature="material rule check",
        on_the_line="The rule check says whether this material meets what the part must meet",
        cannot="check it against those rules",
        wont="won't call any of them met",
        safe_remedy="treat every requirement as unconfirmed and ask the filament's maker for its declarations",
    )
    out: dict[str, Any] = {
        "success": False,
        "checked": False,
        "requirements": list(requirements),
        "error": text,
        "code": "RULE_CHECK_UNAVAILABLE",
        "retryable": miss.cause != "refused",
    }
    out.update(served_answer.fields(miss))
    return out
