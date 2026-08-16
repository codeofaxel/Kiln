"""Attach the update offer to the first tool result of a session.

The nudge already exists on three surfaces — the CLI banner, the MCP
server instructions, and the ``update`` block on ``get_started`` /
``kiln_health`` — and yet ``upgrade_kiln`` has never been recorded
firing.  The plausible reason: the instructions surface is one sentence
inside a long preamble an agent reads once on connect, and a session
that never calls ``get_started`` sees nothing else.  A structured field
riding the FIRST tool result of the session is in the agent's working
context at the exact moment it is composing a reply, which is as close
to unmissable as a hosted-agent surface gets.

Mechanics mirror :mod:`kiln.local_stage`, the one attach path measured
to actually work: the tool-manager hook the telemetry counters use runs
with ``convert_result=True``, so a dict mutation there is silently lost;
only the lowlevel handler (via :func:`kiln.mcp_compat.wrap_call_tool_result`)
sees the real result object.

Behaviour contract:

* at most ONE attach per process — a nudge, not a nag;
* never on a failure-shaped result (an error is a bad moment to upsell
  an upgrade, and the agent should relay the error undistracted);
* never on a result that already carries an update block (``get_started``
  / ``kiln_health`` own that surface);
* never on the hosted multi-tenant server — its package version is the
  SERVER's, and telling a web user to pip-upgrade is nonsense;
* payload is exactly :func:`kiln.version_check.check_for_update`'s dict
  (one contract, defined once) plus the agent-contract sentence the
  server instructions already carry;
* every attach records ``shown_tool_result`` in the daily stats, so the
  aggregate reports can finally show whether the nudge converts —
  attaches with no recorded upgrade attempts and attempts with no
  successes are different product failures needing different fixes.

Never raises: an upgrade hint that breaks a tool result would be the
tail wagging the dog.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# The structured key agents read.  Distinct from get_started's "update"
# key so the two surfaces can't shadow each other in one payload.
RESULT_KEY = "kiln_update"

# Mirrors the server-instructions wording — the agent is told to OFFER,
# not to paste a command at the user.
_AGENT_NOTE = (
    "A newer Kiln is available. Offer to handle it: ask 'want me to "
    "update Kiln for you now?' and, on yes, call the upgrade_kiln tool "
    "(never while a print is active). Don't just tell them to run a "
    "command."
)

# Appended when the release published what it's worth updating FOR — the
# offer should lead with the gain, not the procedure.
_HIGHLIGHTS_NOTE = (
    "Lead with what's new: mention an item or two from 'highlights' when "
    "you offer, so the user hears what they gain."
)

# One attach per process.  Module-level on purpose: the nudge's whole
# contract is "once per session", and a server process IS a session.
_attached = False


def _hosted_multitenant() -> bool:
    """True on the shared hosted server, where this nudge is nonsense.

    Read straight from the env the hosted deploy sets process-wide
    (fly.toml ``KILN_HOSTED_MULTITENANT``) — public Kiln can't import
    kiln-pro's guard, but it doesn't need to: the flag is the contract.
    """
    return os.environ.get("KILN_HOSTED_MULTITENANT", "").strip() in (
        "1", "true", "yes", "on",
    )


def _result_seems_failed(inner: Any, payload: dict | None) -> bool:
    if getattr(inner, "isError", False):
        return True
    return bool(payload) and payload.get("success") is False


def _attach(inner: Any, ctx: Any, name: str | None) -> None:
    """Mutate one tool result in place; body must never raise outward."""
    global _attached
    try:
        if _attached or _hosted_multitenant():
            return
        from kiln.version_check import check_for_update

        info = check_for_update()
        if not info:
            return

        from kiln.local_stage import _result_as_dict

        sc = getattr(inner, "structuredContent", None)
        if not isinstance(sc, dict):
            # Seed from the tool's own output — a host that prefers
            # structuredContent shows THIS and nothing else, so seeding
            # with only the nudge would hide the result it rides on
            # (the lesson local_stage already paid for).
            sc = _result_as_dict(inner) or {}
        else:
            sc = dict(sc)
        if not sc:
            # Nothing to seed from — attaching would REPLACE the visible
            # result with just the nudge on structured-content hosts.
            # Wait for a later result this session that can carry it.
            return
        if _result_seems_failed(inner, sc):
            return
        if "update" in sc or RESULT_KEY in sc:
            _attached = True  # get_started/kiln_health already said it
            return

        note = _AGENT_NOTE
        if info.get("highlights"):
            note = f"{_AGENT_NOTE} {_HIGHLIGHTS_NOTE}"
        sc[RESULT_KEY] = {**info, "note": note}
        inner.structuredContent = sc
        _attached = True

        from kiln.daily_stats import record_update_nudge

        record_update_nudge("shown_tool_result")
    except Exception:  # noqa: BLE001 -- a nudge must never break a result
        logger.debug("update nudge not attached", exc_info=True)


def install(mcp: Any) -> bool:
    """Wrap the lowlevel handler.  Returns whether the hook landed."""
    if not _update_checks_on():
        return False
    try:
        from kiln.mcp_compat import wrap_call_tool_result

        return bool(wrap_call_tool_result(mcp, _attach))
    except Exception:  # noqa: BLE001 -- optional capability, never fatal
        logger.debug("update nudge hook not installed", exc_info=True)
        return False


def _update_checks_on() -> bool:
    try:
        from kiln.version_check import update_check_enabled

        return update_check_enabled()
    except Exception:  # noqa: BLE001
        return False


def _reset_for_tests() -> None:
    global _attached
    _attached = False
