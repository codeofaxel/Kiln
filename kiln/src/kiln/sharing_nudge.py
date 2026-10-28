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
    "Kiln can learn from this computer's prints to make everyone's prints "
    "better: anonymous print results (the part's shape signature, printer "
    "model, material, settings and how it went) and a daily report of Kiln's "
    "version, your printer models and counts of what Kiln did. No files, "
    "names, addresses or account details. It is off until you turn it on."
)

_NOTE = (
    "Ask the person once, in your own words, at a natural pause (for example "
    "after they have seen their part). If they want it, they turn it on "
    "themselves by running person_runs in their own terminal, which asks them "
    "to confirm; do not run it for them. If they say no or do not answer, do "
    "nothing: it stays off, and Kiln will not ask again."
)

_attached = False


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


def install(mcp: Any) -> bool:
    """Wrap the lowlevel handler on an install that waits for an answer."""
    try:
        from kiln.sharing_consent import waiting_for_an_answer

        if not waiting_for_an_answer():
            return False
        from kiln.mcp_compat import wrap_call_tool_result

        return bool(wrap_call_tool_result(mcp, _attach))
    except Exception:  # noqa: BLE001 -- optional capability, never fatal
        logger.debug("sharing question not installed", exc_info=True)
        return False


def _reset_for_tests() -> None:
    global _attached
    _attached = False
