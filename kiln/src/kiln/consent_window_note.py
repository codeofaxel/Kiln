"""The line every print result carries while a standing window is open.

A window a person cannot see the edge of is a trap: they said "yes, for
the next two hours" in a dialog, and from then on prints start without
asking.  The dialog does not come back to remind them — that is what they
asked for — so the reminder rides the thing they DO see: the result of
every print-starting tool, while a window covers the printer that call
was aimed at.  It says which printer, until when, and how to close it
early, and it tells the agent to pass that on rather than keep it.

Mechanics mirror :mod:`kiln.update_nudge`: the tool-manager hook the
telemetry counters use runs with ``convert_result=True``, so a dict
mutation there is silently lost; only the lowlevel handler (via
:func:`kiln.mcp_compat.wrap_call_tool_result`) sees the real result
object.  One hook covers every door the MCP side has — ``start_print``,
``slice_and_print``, ``run_quick_print``, the queue, the plate tools —
because it keys on the same table the consent wrapper keys on
(``_CONSENT_FILE_ARG``), so a tool that is asked for consent is a tool
that carries the note.  The CLI's doors print their own line
(``cli_gate``), and ``kiln consent status`` shows the same facts.

Attached on success and on failure alike: the window is a fact about
the machine's standing permission, not about this one call, and a person
whose print failed still has a window open.  On the hosted server the
account's store answers (``kiln.consent_windows.window_store``), so a web
user is reminded the same way.  Never raises.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The structured key agents read.
RESULT_KEY = "standing_window"


def note_for(printer_name: str | None) -> dict[str, Any] | None:
    """The block for this result, or ``None``.

    Two things it can say.  A window the person asked for on this very
    call that did NOT open (an unreadable length, one past the cap, a
    store that could not be written): said first, in the moment, because
    the next print asking again is the wrong way to find out.  Otherwise
    the live window covering *printer_name*, read straight from the
    store, so a window opened by the dialog on this call is reported on
    this result — the moment the person most needs to be told what they
    just opened.
    """
    from kiln import consent_windows
    from kiln.print_consent import take_window_outcome

    outcome = take_window_outcome()
    if outcome is not None and not outcome.get("opened", False):
        asked_for = str(outcome.get("asked_for") or "a standing window")
        reason = str(outcome.get("reason") or "it could not be opened")
        return {
            "opened": False,
            "asked_for": asked_for,
            "reason": reason,
            "note": (
                f"The person asked for a standing window ({asked_for}) and it was NOT opened: "
                f"{reason}. This print went ahead on their yes; the next print will ask again. "
                "Tell them, and what to answer next time (a length like 45m, 3h or 1d, 24 hours at most)."
            ),
        }
    window = consent_windows.covering(printer_name)
    if window is None:
        return None
    return block_for_window(window)


def block_for_window(window: Any, *, close_hint: str | None = None) -> dict[str, Any]:
    """The block for one open window — the same shape at every door.  The
    MCP result names the tool that closes it; a CLI door passes the
    command instead."""
    from kiln import consent_windows

    facts = consent_windows.describe(window)
    close = close_hint or f"call revoke_consent_window(window_id=\"{facts['id']}\")"
    return {
        "opened": True,
        "id": facts["id"],
        "printer": facts["scope"],
        "until": facts["until"],
        "remaining_minutes": facts["remaining_minutes"],
        "opened_via": facts["opened_via"],
        "note": (
            f"A standing window is open: prints may start on {facts['scope']} until "
            f"{facts['until_clock']} without asking each time (each one still previewed "
            f"first). Tell the person this. If they want it closed, {close} — closing is "
            "always allowed; opening is theirs alone."
        ),
    }


def _attach(inner: Any, ctx: Any, name: str | None, arguments: dict | None) -> None:
    """Mutate one tool result in place; body must never raise outward."""
    try:
        if not name:
            return
        import kiln.server as _srv

        if name not in _srv._CONSENT_FILE_ARG:
            return
        args = arguments if isinstance(arguments, dict) else {}
        printer_name = args.get("printer_name") or None
        try:
            aimed = printer_name or _srv._resolve_effective_printer_name(None)
        except Exception:  # noqa: BLE001
            aimed = printer_name or "default"
        block = note_for(aimed)
        if block is None:
            return

        from kiln.local_stage import _result_as_dict

        sc = getattr(inner, "structuredContent", None)
        if not isinstance(sc, dict):
            # Seed from the tool's own output — a host that prefers
            # structuredContent shows THIS and nothing else, so seeding
            # with only the note would hide the result it rides on.
            sc = _result_as_dict(inner) or {}
        else:
            sc = dict(sc)
        if not sc:
            return
        sc[RESULT_KEY] = block
        inner.structuredContent = sc
    except Exception:  # noqa: BLE001 -- a note must never break a result
        logger.debug("standing window note not attached", exc_info=True)


def install(mcp: Any) -> bool:
    """Wrap the lowlevel handler.  Returns whether the hook landed."""
    try:
        from kiln.mcp_compat import wrap_call_tool_result

        return bool(wrap_call_tool_result(mcp, _attach))
    except Exception:  # noqa: BLE001 -- optional surface, never fatal
        logger.debug("standing window note hook not installed", exc_info=True)
        return False
