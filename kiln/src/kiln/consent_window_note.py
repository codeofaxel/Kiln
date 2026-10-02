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

Always allow — the window with no end, for one printer — rides the same
block with its own line: every print started under it says it was
started without asking, on which printer, and what to say to turn it
off.  For the day after Kiln turned it off itself (a different machine
under the name), the block says that instead, so the person learns why
prints ask again.
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
        closed = consent_windows.turned_itself_off(printer_name)
        return turned_off_block(closed) if closed is not None else None
    return block_for_window(window)


def for_outcome(block: dict[str, Any], outcome: str, printer_name: str | None) -> dict[str, Any]:
    """An always-allow block, worded for what the call did: the line says
    the print was started (or queued) without asking, and the block
    carries what the bed was known to be — the look taken for this call,
    with its picture.  Any other block, and a call that started nothing,
    comes back as it was."""
    if not (outcome and block.get("always") and block.get("opened")):
        return block
    from kiln.print_consent import unasked_look_noted

    worded = {**block, "note": always_line(block["printer"], outcome)}
    look = unasked_look_noted(printer_name)
    if look is not None:
        worded["bed_check"] = {**look.evidence(), "note": bed_check_line(look.evidence())}
    return worded


#: What the call did, for the first words of the always-allow line.
STARTED = "started"
QUEUED = "queued"


def always_line(printer: str, outcome: str = "") -> str:
    """The line a person reads on a print that rested on always allow."""
    lead = {STARTED: "Started without asking. ", QUEUED: "Queued without asking. "}.get(outcome, "")
    return f'{lead}Always allow is on for {printer}. Say "ask me first" to turn it off.'


def turned_off_line(printer: str) -> str:
    """What a person is told when always allow closed itself."""
    return (
        f"Always allow is off for {printer}. A different printer is now set up under that name, "
        "so Kiln turned it off. Kiln will ask before each print."
    )


def could_not_see_line(printer: str, why: str) -> str:
    """What a person is told when always allow is on and they are asked
    anyway, because the camera could not show the bed."""
    reason = f" ({why})" if why else ""
    return (
        f"Always allow is on for {printer}, but Kiln could not see the bed through the camera{reason}, "
        "so it is asking you this time."
    )


def bed_check_line(evidence: dict[str, Any] | None) -> str:
    """One sentence on what the bed was known to be for a print nobody
    was asked about; ``""`` when nothing is known."""
    if not evidence:
        return ""
    if evidence.get("checked") and not evidence.get("frame"):
        return "Before it started, you said the bed was clear."
    if evidence.get("checked"):
        who = "you" if evidence.get("judged_by") == "human" else "your assistant"
        return f"Before it started, {who} checked the bed through the camera. The picture: {evidence.get('frame')}"
    if evidence.get("camera") is None:
        return "This printer has no camera Kiln can use, so the bed was not checked first."
    return ""


def turned_off_block(window: Any) -> dict[str, Any]:
    """The block for an always-allow entry Kiln closed itself."""
    from kiln import consent_windows

    facts = consent_windows.describe(window)
    return {
        "opened": False,
        "always": True,
        "id": facts["id"],
        "printer": facts["scope"],
        "turned_off": "machine_changed",
        "note": turned_off_line(facts["scope"]),
        "for_the_assistant": (
            "Show the person that line as written. Only they can turn always allow on again, at a "
            f"terminal: `kiln consent window --always --printer {facts['scope']}`."
        ),
    }


def block_for_window(window: Any, *, close_hint: str | None = None, outcome: str = "") -> dict[str, Any]:
    """The block for one open window — the same shape at every door.  The
    MCP result names the tool that closes it; a CLI door passes the
    command instead.  *outcome* is what the call did (:data:`STARTED`,
    :data:`QUEUED`, or nothing known), which only the always-allow line
    words."""
    from kiln import consent_windows

    facts = consent_windows.describe(window)
    close = close_hint or f"call revoke_consent_window(window_id=\"{facts['id']}\")"
    if facts["always"]:
        return {
            "opened": True,
            "always": True,
            "id": facts["id"],
            "printer": facts["scope"],
            "until": None,
            "remaining_minutes": None,
            "opened_via": facts["opened_via"],
            "opened_by": facts["set_by"],
            "opened_at": facts["set_at"],
            "note": always_line(facts["scope"], outcome),
            "for_the_assistant": (
                "Show the person that line as written. When they say \"ask me first\", or ask to "
                f"turn always allow off, {close} — turning it off is always allowed; turning it "
                "on is theirs alone, at a terminal."
            ),
        }
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


#: The doors that put a job on the queue rather than on the machine.
_QUEUE_DOORS = frozenset({"submit_job", "fleet_submit_job"})


def _outcome_of(name: str, args: dict, result: dict) -> str:
    """What this call did, as far as its own result says: :data:`STARTED`,
    :data:`QUEUED`, or ``""`` for a call that failed, was a dry run, or
    says nothing either way — the line then claims no start."""
    if result.get("success") is not True or args.get("dry_run"):
        return ""
    # A start result can read as accepted while the adapter itself turned
    # the print away (its safety gate refused and the printer has not been
    # heard from since).  "Started" is said only when the adapter said so.
    evidence = result.get("evidence")
    if isinstance(evidence, dict) and evidence.get("adapter_reported_success") is False:
        return ""
    return QUEUED if name in _QUEUE_DOORS else STARTED


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
        from kiln.mcp_compat import result_structured_content, set_result_structured_content

        sc = result_structured_content(inner)
        if not isinstance(sc, dict):
            # Seed from the tool's own output — a host that prefers
            # structuredContent shows THIS and nothing else, so seeding
            # with only the note would hide the result it rides on.
            sc = _result_as_dict(inner) or {}
        else:
            sc = dict(sc)
        if not sc:
            return
        sc[RESULT_KEY] = for_outcome(block, _outcome_of(name, args, sc), aimed)
        set_result_structured_content(inner, sc)
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
