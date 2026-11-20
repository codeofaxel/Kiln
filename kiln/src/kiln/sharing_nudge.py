"""Ask once whether the person wants to share, on an install that waits for it.

An ``ask`` install (:mod:`kiln.sharing_consent`) sends nothing until the person
turns sharing on.  This puts the question in front of the agent once, as a
structured block on a successful tool result, with the exact command that turns
it on here.  The agent asks in its own words at a natural pause; the person
turns it on themselves, confirming at their own terminal (``kiln sharing on``
asks for that confirmation and refuses when there is no terminal to ask at, so
a program running Kiln cannot quietly turn it on).  The question is recorded as asked in ``~/.kiln/sharing.json``, so it is
asked once per computer, not once per session.

Same attach path as :mod:`kiln.update_nudge`: never on a failure, never on an
empty result, never on the hosted server, never raises.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

RESULT_KEY = "kiln_sharing"

QUESTION = (
    "Kiln can learn from how prints turn out on this computer to make "
    "everyone's prints better. It would share anonymous print results (the "
    "printer model, the material, the nozzle size and how each print went) "
    "and a daily report: an anonymous install number, your computer's system "
    "type, Kiln's version, your printer models and firmware, which AI app you "
    "use, whether you are signed in, and counts of what Kiln did. Never your "
    "files, their shape, their names, your addresses or who you are. It is "
    "off until you turn it on."
)

_NOTE = (
    "Ask the person once, in your own words, at a natural pause (for example "
    "after they have seen their part). If they want it, they turn it on "
    "themselves by running person_runs in their own terminal, which asks them "
    "to confirm; do not run it for them. If they say no or do not answer, do "
    "nothing: it stays off, and Kiln will not ask again."
)

#: The question about printer routines, asked once on its own, after the
#: first print whose file carried them.
ROUTINES_KEY = "kiln_printer_routines"
ROUTINES_QUESTION = (
    "Kiln can learn your printer's own routines from your print files: how it "
    "starts and ends a print, pauses and changes filament, exactly as your "
    "slicer wrote them for your printer. Never the print itself, its name or "
    "your settings. Share them, so every owner of your printer gets a Kiln "
    "that knows how it moves? Kiln works the same either way."
)
_ROUTINES_NOTE = (
    "Ask the person once, in your own words, at a natural pause. If they want "
    "it, they turn it on themselves by running person_runs in their own "
    "terminal, which asks them to confirm; do not run it for them. If they say "
    "no or do not answer, do nothing: nothing is sent, and Kiln will not ask "
    "again."
)

_attached = False
_routines_attached = False


def _result_seems_failed(inner: Any, payload: dict | None) -> bool:
    from kiln.mcp_compat import result_is_error

    if result_is_error(inner):
        return True
    return bool(payload) and payload.get("success") is False


def _attach(inner: Any, ctx: Any, name: str | None) -> None:
    """Mutate one tool result in place; body must never raise outward."""
    global _attached
    try:
        from kiln.sharing_consent import mark_asked, waiting_for_an_answer, was_asked
        from kiln.update_nudge import _hosted_multitenant

        if _attached or _hosted_multitenant() or not waiting_for_an_answer() or was_asked():
            return

        from kiln.cli_command import kiln_command
        from kiln.local_stage import _result_as_dict
        from kiln.mcp_compat import result_structured_content, set_result_structured_content

        sc = result_structured_content(inner)
        sc = dict(sc) if isinstance(sc, dict) else (_result_as_dict(inner) or {})
        # Nothing to seed from would REPLACE the visible result with the
        # question on structured-content hosts; wait for a later result.
        if not sc or _result_seems_failed(inner, sc) or RESULT_KEY in sc:
            return

        sc[RESULT_KEY] = {
            "question": QUESTION,
            "person_runs": kiln_command("sharing on"),
            "note": _NOTE,
        }
        set_result_structured_content(inner, sc)
        _attached = True
        mark_asked()
    except Exception:  # noqa: BLE001 -- a question must never break a result
        logger.debug("sharing question not attached", exc_info=True)


def _routines_due() -> bool:
    """The routines question has not been answered or asked on this computer."""
    from kiln.sharing_consent import routines_answer, routines_asked

    return routines_answer() is None and not routines_asked()


def _attach_routines(inner: Any, ctx: Any, name: str | None) -> None:
    """The routines question on one result, once, after a print file has
    carried routines here.  Never raises outward."""
    global _routines_attached
    try:
        from kiln.sharing_consent import mark_routines_asked
        from kiln.sliced_routines import any_kept
        from kiln.update_nudge import _hosted_multitenant

        if _routines_attached or _hosted_multitenant() or not _routines_due() or not any_kept():
            return
        from kiln.cli_command import kiln_command
        from kiln.local_stage import _result_as_dict
        from kiln.mcp_compat import result_structured_content, set_result_structured_content

        sc = result_structured_content(inner)
        sc = dict(sc) if isinstance(sc, dict) else (_result_as_dict(inner) or {})
        if not sc or _result_seems_failed(inner, sc) or ROUTINES_KEY in sc or RESULT_KEY in sc:
            return
        sc[ROUTINES_KEY] = {
            "question": ROUTINES_QUESTION,
            "person_runs": kiln_command("sharing routines on"),
            "note": _ROUTINES_NOTE,
        }
        set_result_structured_content(inner, sc)
        _routines_attached = True
        mark_routines_asked()
    except Exception:  # noqa: BLE001 -- a question must never break a result
        logger.debug("routines question not attached", exc_info=True)


def install(mcp: Any) -> bool:
    """Wrap the lowlevel handler on an install that waits for an answer, or
    where the routines question is still to be asked."""
    try:
        from kiln.mcp_compat import wrap_call_tool_result
        from kiln.sharing_consent import waiting_for_an_answer

        wrapped = False
        if waiting_for_an_answer():
            wrapped = bool(wrap_call_tool_result(mcp, _attach)) or wrapped
        if _routines_due():
            wrapped = bool(wrap_call_tool_result(mcp, _attach_routines)) or wrapped
        return wrapped
    except Exception:  # noqa: BLE001 -- optional capability, never fatal
        logger.debug("sharing question not installed", exc_info=True)
        return False


def _reset_for_tests() -> None:
    global _attached, _routines_attached
    _attached = False
    _routines_attached = False
