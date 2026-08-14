"""Point a session that skipped onboarding at ``get_started``, once.

The connect-time instructions tell every agent to call ``get_started()``
first.  Agents skip it — measured in the wild: a whole working session
framed a server restart as "the user must reopen their app" because the
one tool built for it was only discoverable through onboarding that never
happened.  The preamble cannot be enforced; what CAN be done is noticing,
on the first tool result of the session, that onboarding was skipped, and
riding one structured hint on that result — in the agent's working
context at the exact moment it is composing a reply.

Mechanics mirror :mod:`kiln.update_nudge`, the attach path measured to
actually work (the tool-manager hook runs with ``convert_result=True``,
where dict mutations are silently lost; only the lowlevel handler via
:func:`kiln.mcp_compat.wrap_call_tool_result` sees the real result).

Behaviour contract:

* at most ONE attach per process — a pointer, not a nag;
* self-suppressing: a session that calls ``get_started`` or
  ``get_skill_manifest`` — before or as its first call — never sees it;
* never on a failure-shaped result (the agent should relay the error
  undistracted) and never on an empty one (attaching would REPLACE the
  visible result on structured-content hosts — the lesson
  ``local_stage`` already paid for);
* ``KILN_NO_ONBOARDING_NUDGE=1`` opts out entirely;
* every attach records ``onboarding_nudge_shown`` via the daily stats,
  so whether the pointer converts into ``get_started`` calls is
  measurable against the tallies that already count every tool.

Never raises: an onboarding hint that breaks a tool result would teach
exactly the wrong lesson about calling things.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: The structured key agents read.
RESULT_KEY = "kiln_onboarding"

#: Calling either of these IS onboarding — they suppress the nudge for
#: the life of the process, whether they come first or fifth.
ONBOARDING_TOOLS = frozenset({"get_started", "get_skill_manifest"})

_NOTE = (
    "This session hasn't called get_started() yet. Call it once: it "
    "returns the capability map, printer state, core workflows, and the "
    "session-maintenance tools (restart_server, trim_serve_processes, "
    "upgrade_kiln) that keep Kiln itself healthy — then discover "
    "anything else with ToolSearch(keyword)."
)

# One flag per concern: whether onboarding happened, and whether the
# nudge has already ridden a result.  A server process IS a session.
_onboarded = False
_attached = False


def _disabled() -> bool:
    return os.environ.get("KILN_NO_ONBOARDING_NUDGE", "").strip() in (
        "1", "true", "yes", "on",
    )


def _result_seems_failed(inner: Any, payload: dict | None) -> bool:
    if getattr(inner, "isError", False):
        return True
    return bool(payload) and payload.get("success") is False


def _attach(inner: Any, ctx: Any, name: str | None) -> None:
    """Mutate one tool result in place; body must never raise outward."""
    global _onboarded, _attached
    try:
        if name in ONBOARDING_TOOLS:
            _onboarded = True
            return
        if _onboarded or _attached or _disabled():
            return

        from kiln.local_stage import _result_as_dict

        sc = getattr(inner, "structuredContent", None)
        if not isinstance(sc, dict):
            sc = _result_as_dict(inner) or {}
        else:
            sc = dict(sc)
        if not sc:
            # Nothing to seed from — wait for a later result that can
            # carry the hint without replacing the tool's own output.
            return
        if _result_seems_failed(inner, sc):
            return
        if RESULT_KEY in sc:
            _attached = True
            return

        sc[RESULT_KEY] = {"note": _NOTE}
        inner.structuredContent = sc
        _attached = True

        from kiln.daily_stats import record_event

        record_event("onboarding_nudge_shown")
    except Exception:  # noqa: BLE001 — a hint must never break a result
        logger.debug("onboarding nudge not attached", exc_info=True)


def install(mcp: Any) -> bool:
    """Wrap the lowlevel handler.  Returns whether the hook landed."""
    if _disabled():
        return False
    try:
        from kiln.mcp_compat import wrap_call_tool_result

        return bool(wrap_call_tool_result(mcp, _attach))
    except Exception:  # noqa: BLE001 — optional capability, never fatal
        logger.debug("onboarding nudge hook not installed", exc_info=True)
        return False


def _reset_for_tests() -> None:
    global _onboarded, _attached
    _onboarded = False
    _attached = False
