"""Say what a real print taught Kiln about the person's printer, once.

When a print Kiln started pauses, is cancelled or ends, :mod:`kiln.head_moves`
may learn how that printer moves its head (:func:`kiln.bench.note_learned`).
This puts it in front of the agent on the next successful tool result, as a
structured block with one sentence per thing learned ("Kiln learned how your
Creality Ender-3 ends a print."), for the agent to say as written.  It names
what was learned from this printer, never what Kiln does not know.

After a printer's first print, the first lesson's offer rides the same block
when that print left something only a lesson can teach
(:func:`kiln.bench.offer_after_first_print`): worded here, on the reply, so
it is in the words of the caller's plan, and made through the one offer
every door makes (:func:`kiln.bench.offer`).

Same attach path as :mod:`kiln.update_nudge`: never on a failure, never on an
empty result, never on the hosted server, never raises.  Each sentence is said
once: it is forgotten when it is attached.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

RESULT_KEY = "kiln_learned"

_NOTE = "Tell the person in one line, as written. Nothing to ask and nothing to do."
_NOTE_OFFER = ("Tell the person what Kiln learned in one line, as written, if there is anything. Then say the offer "
               "once, as written, and take 'later' as an answer.")


def _result_seems_failed(inner: Any, payload: dict | None) -> bool:
    from kiln.mcp_compat import result_is_error

    if result_is_error(inner):
        return True
    return bool(payload) and payload.get("success") is False


def _attach(inner: Any, ctx: Any, name: str | None) -> None:
    """Mutate one tool result in place; body must never raise outward."""
    try:
        from kiln.update_nudge import _hosted_multitenant

        if _hosted_multitenant():
            return
        from kiln import bench

        if not bench._learned_path().exists():
            return
        from kiln.local_stage import _result_as_dict
        from kiln.mcp_compat import result_structured_content, set_result_structured_content

        sc = result_structured_content(inner)
        sc = dict(sc) if isinstance(sc, dict) else (_result_as_dict(inner) or {})
        # Nothing to seed from would REPLACE the visible result with the
        # sentence on structured-content hosts; wait for a later result.
        if not sc or _result_seems_failed(inner, sc) or RESULT_KEY in sc:
            return
        news = bench.take_news()
        offers = []
        for pending in news["offers"]:
            made = bench.offer(
                pending["unit"], pending["missing"], "first_print",
                model_name=bench._display_name(pending["printer_id"]) if pending.get("printer_id") else "printer",
                printer_name=pending.get("printer_name"),
            )
            if made is not None:
                offers.append(made)
        if not news["said"] and not offers:
            return
        block: dict[str, Any] = {"said": news["said"]} if news["said"] else {}
        if offers:
            block["offers"] = offers
        block["note"] = _NOTE_OFFER if offers else _NOTE
        sc[RESULT_KEY] = block
        set_result_structured_content(inner, sc)
    except Exception:  # noqa: BLE001 -- a sentence must never break a result
        logger.debug("what a print taught was not attached", exc_info=True)


def install(mcp: Any) -> bool:
    """Wrap the lowlevel handler.  Returns whether the hook landed."""
    try:
        from kiln.mcp_compat import wrap_call_tool_result

        return bool(wrap_call_tool_result(mcp, _attach))
    except Exception:  # noqa: BLE001 -- optional capability, never fatal
        logger.debug("what a print taught is not said", exc_info=True)
        return False
