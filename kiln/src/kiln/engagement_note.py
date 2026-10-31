"""The line a result carries when a look made Kiln start working with a printer.

Below the fleet tier Kiln works with one printer at a time, and a look at a
printer that is mid-print is watching it, so the look takes the free slot
(:func:`kiln.printers.engagement.observe`).  That is the rule working, but
nobody asked for it in so many words, so the result of the call that did it
says which printer Kiln is now working with and how to move it on.  Without
this line the person learns it from the next refusal, as a surprise.

Mechanics mirror :mod:`kiln.consent_window_note`: only the lowlevel handler
(:func:`kiln.mcp_compat.wrap_call_tool_result`) sees the real result object.
The claim is written down with the engagement itself rather than held in a
context variable, because a synchronous tool runs on a worker thread and
nothing it sets there survives the return.  One hook covers every MCP tool,
so a look reached from any tool carries the line.  Said once; never raises.

Honest bound: the CLI and the local REST door do not pass this hook, so a
look made there takes the slot without the line, and the next refusal is
where the person reads it.  A claim left unsaid is dropped after
``_ANNOUNCE_WITHIN_S`` rather than announced on a later, unrelated result.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The structured key agents read.
RESULT_KEY = "working_with"


def _attach(inner: Any, ctx: Any, name: str | None) -> None:
    """Mutate one tool result in place; body must never raise outward."""
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            # One process serves every account there, and no printer is
            # driven from it: a record in its ~/.kiln is nobody's to announce.
            return
        from kiln.local_stage import _result_as_dict
        from kiln.mcp_compat import result_structured_content, set_result_structured_content

        sc = result_structured_content(inner)
        if not isinstance(sc, dict):
            # Seed from the tool's own output -- a host that prefers
            # structuredContent shows THIS and nothing else, so seeding
            # with only the line would hide the result it rides on.
            sc = _result_as_dict(inner) or {}
        else:
            sc = dict(sc)
        if not sc:
            return
        from kiln.printers.engagement import take_announcement

        block = take_announcement()
        if block is None:
            return
        sc[RESULT_KEY] = block
        set_result_structured_content(inner, sc)
    except Exception:  # noqa: BLE001 -- a note must never break a result
        logger.debug("engagement note not attached", exc_info=True)


def install(mcp: Any) -> bool:
    """Wrap the lowlevel handler.  Returns whether the hook landed."""
    try:
        from kiln.mcp_compat import wrap_call_tool_result

        return bool(wrap_call_tool_result(mcp, _attach))
    except Exception:  # noqa: BLE001 -- optional surface, never fatal
        logger.debug("engagement note hook not installed", exc_info=True)
        return False
