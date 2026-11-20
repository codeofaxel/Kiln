"""Public-Kiln -> kiln-pro bridge for the guided session's second lesson:
what one printer can really print.

``printer_bench`` teaches Kiln how steep, how far and how thin a printer
prints in one plastic from a test print the person prints and reads.  Each
step of that lesson (make the test print, read its sliced files, keep the
person's readings, say back what is on record) is answered by Kiln's servers
(https://kiln3d.com), one step per ask, and kept in the person's own Kiln
account.  This module is the courier: it sends the step, sends the sliced
files up with it, brings the test print's files back to this computer, and
says why when no answer came.  Every step carries this install's printers
(their names, models and which is the default: the same ``printer_facts``
reading every served printer tool sends), so the servers file what is learned
under the printer's own name and find it again by name or by model.  Nothing
here works anything out; a caller that finds itself doing so is on the wrong
side of this file.

The lesson is free with a Kiln account.  What a kept test print changes is
the servers' answer to give, and their reply says it in the person's words.

A step comes back as one of two things:

* the servers' own reply (``status: "ok"``, or a refusal the lesson itself
  gives, whose ``code`` is in :data:`LESSON_CODES` and whose sentence the
  session says as written), or
* a :class:`kiln.served_answer.Miss`: offline, signed out, no answer, or
  refused for a reason that is not the lesson's.  Nothing was kept, and the
  session words it with :func:`miss_sentence`.

Never raises.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln import served_answer

logger = logging.getLogger(__name__)

#: The served wire this bridge asks.
WIRE_TOOL = "limits_lesson"
#: A test print is drawn and compiled on the servers, and a sliced file can
#: be large on a home connection: a person is waiting, so the wait is long
#: but bounded.
TIMEOUT_S: float = 180.0
#: The parameter the sliced files travel under: a list of files sent up
#: with the call, in plate order.
GCODE_PARAM = "gcode_paths"
#: Refusals the lesson itself gives: answers the session acts on (ask for the
#: plastic again, the nozzle, a reading), never a miss.
LESSON_CODES = frozenset({
    "INVALID_INPUT", "UNKNOWN_MATERIAL", "NOZZLE_NEEDED", "LAYER_HEIGHT_NEEDED", "BED_TOO_SMALL",
    "OPENSCAD_NOT_FOUND", "COMPILE_TIMEOUT", "COMPILE_FAILED", "TEST_NOT_FOUND", "TEST_MISMATCH",
    "INVALID_READING", "GCODE_NOT_THIS_PRINT", "PATH_NOT_AVAILABLE",
})
#: The reading every step carries: this install's printers, as plain facts.
_READS = [{"kind": "printer_facts"}]
#: What each step sends beside the step's name, and nothing else.
_ARGS = (
    "printer_id", "material", "coupon", "test_id", "readings", "nozzle_mm", "layer_height_mm", "fan_percent",
    "enclosed", "spool_dried", "firmware", "printer_model", "bed_mm",
)

__all__ = ["GCODE_PARAM", "LESSON_CODES", "TIMEOUT_S", "WIRE_TOOL", "ask", "miss_sentence", "signed_in"]


def signed_in() -> bool:
    """Whether this install can ask Kiln's servers as an account now."""
    try:
        import kiln.server as _srv

        bearer, _expired = _srv._served_bearer()
        return bool(bearer)
    except Exception:  # noqa: BLE001 -- an unreadable sign-in is no sign-in
        return False


def ask(step: str, *, gcode_paths: list[str] | None = None, **args: Any) -> tuple[dict[str, Any] | None, Any]:
    """One step of the lesson: ``(reply, None)`` or ``(None, miss)``.

    *gcode_paths* (``sliced`` only) are files on this computer, sent up with
    the call.  A reply that names the test print's files has them saved on
    this computer and their paths put where the servers' were.
    """
    try:
        import kiln.server as _srv
        from kiln import served_makes

        bearer, expired = _srv._served_bearer()
        if not bearer:
            code = served_answer.SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
            return None, served_answer.Miss("signed_out", code)
        from kiln import served_steps

        kwargs: dict[str, Any] = {"step": step}
        kwargs.update({k: v for k, v in args.items() if k in _ARGS and v is not None})
        printers, refusal = served_steps.read_first(
            WIRE_TOOL, _READS, printer_name=str(args.get("printer_id") or ""), call=kwargs,
        )
        if refusal is not None:
            return None, served_answer.classify_answer(refusal) or served_answer.Miss("unanswered")
        kwargs["step_results"] = printers
        if gcode_paths:
            kwargs[GCODE_PARAM] = list(gcode_paths)
            kwargs, refusal = served_makes.send_inputs(WIRE_TOOL, kwargs, {"files": {GCODE_PARAM: "many"}})
            if refusal is not None:
                return None, served_answer.classify_answer(refusal) or served_answer.Miss("unanswered")
        answer = _srv._pro_api_call(WIRE_TOOL, _timeout=TIMEOUT_S, _asked_by_user=True, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        import kiln.server as _srv

        return None, served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
    if not isinstance(answer, dict):
        return None, served_answer.Miss("unanswered", detail="not an answer")
    if answer.get("status") == "ok" and answer.get("success") is not False:
        try:
            from kiln import served_makes

            answer = served_makes.arrive(WIRE_TOOL, answer, printer_name=args.get("printer_id"), record_resumes=False)
        except Exception:  # noqa: BLE001
            logger.debug("%s files not brought home", WIRE_TOOL, exc_info=True)
        if isinstance(answer, dict) and answer.get("status") == "ok":
            return answer, None
        return None, served_answer.classify_answer(answer) or served_answer.Miss("unanswered")
    if str(answer.get("code") or "") in LESSON_CODES:
        return answer, None
    return None, served_answer.classify_answer(answer) or served_answer.Miss("unanswered")


def miss_sentence(miss: Any, *, cannot: str) -> str:
    """The one sentence a session says for *miss*: what was not done, why,
    and the fix.  Nothing was kept."""
    return served_answer.sentence(
        miss,
        feature="servers",
        on_the_line="A test print and what you read off it are kept in your Kiln account",
        cannot=cannot,
        wont="has kept nothing",
    )
