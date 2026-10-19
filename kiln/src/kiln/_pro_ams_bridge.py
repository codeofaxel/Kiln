"""Public-Kiln -> kiln-pro bridge for one question: may this AMS dryer START
command be sent, and in exactly what shape?

The command's limits are kiln-pro's (https://kiln3d.com): the unit's own
drying ceiling, the cooling floor the firmware silently drops a command
under, the zero-duration guard.  With kiln-pro installed here they are read
locally; otherwise this asks Kiln's servers for the one command
(``ams_drying_command``), free at every tier.  A STOP never comes here: its
payload is public and fixed (:meth:`kiln.emergency.EmergencyCoordinator._build_ams_stop_command`),
so stopping a dryer works offline.

A start that gets no answer -- offline, signed out, the servers silent or
saying no -- is refused, and said in the shared voice
(:mod:`kiln.served_answer`): a heater is never switched on without the check
that keeps it under the unit's ceiling.  Nothing is kept on this computer.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The hosted door, asked when kiln-pro is not installed here.
WIRE_TOOL = "ams_drying_command"
#: Where the same answer is read when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.device_intelligence.bambu_ams"
#: A person is waiting on the dryer; a short wait.
CONSULT_TIMEOUT_S: float = 8.0

__all__ = ["CONSULT_TIMEOUT_S", "LOCAL_MODULE", "WIRE_TOOL", "start_command"]


def start_command(**request: Any) -> dict[str, Any]:
    """The checked START command, or why it may not be sent.

    *request* is ``ams_id``, ``mode``, ``duration_min``, ``temp_c``,
    ``cooling_temp_c``, ``humidity``, ``rotate_tray`` and the unit's own
    ``module_name``.  Answers ``{"success": True, "command": {...}}``, the
    limits' own refusal (``INVALID_DRYING_COMMAND`` with the reason), or a
    worded miss (``DRYING_CHECK_UNAVAILABLE``).  Never raises.
    """
    try:
        from kiln_pro.device_intelligence.bambu_ams import drying_command_for
    except ImportError:
        return _served(request)
    try:
        return drying_command_for(**request)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        logger.debug("local AMS drying check failed", exc_info=True)
        from kiln.served_answer import Miss

        return _miss(Miss("unanswered", detail=str(exc)[:200]))


def _served(request: dict[str, Any]) -> dict[str, Any]:
    from kiln import served_answer

    payload = {k: v for k, v in request.items() if v is not None and v != ""}
    try:
        import kiln.server as _srv

        answer = _srv._pro_api_call(WIRE_TOOL, _timeout=CONSULT_TIMEOUT_S, _asked_by_user=True, **payload)
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        import kiln.server as _srv

        return _miss(served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None)))
    if isinstance(answer, dict) and (
        (answer.get("success") is True and isinstance(answer.get("command"), dict))
        # The limits' own ruling on this command, relayed as it is.
        or (answer.get("success") is False and answer.get("code") == "INVALID_DRYING_COMMAND")
    ):
        return answer
    miss = served_answer.classify_answer(answer) or served_answer.Miss(
        "unanswered", detail="no drying command in the answer"
    )
    return _miss(miss)


def _miss(miss: Any) -> dict[str, Any]:
    """The refusal when the start could not be checked."""
    from kiln import served_answer

    text = served_answer.sentence(
        miss,
        feature="servers",
        on_the_line="Before an AMS dryer starts, Kiln checks the command against that unit's own temperature ceiling",
        cannot="check this drying start",
        wont="started nothing and the dryer is as it was",
        safe_remedy="Start the cycle from the printer's own screen",
    )
    out: dict[str, Any] = {
        "success": False,
        "status": "error",
        "code": "DRYING_CHECK_UNAVAILABLE",
        "error": text,
        "retryable": miss.cause != "refused",
    }
    out.update(served_answer.fields(miss))
    return out
