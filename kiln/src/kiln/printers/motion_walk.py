"""Walk a home or a park on a printer one confirmed step at a time.

A walk is a move Kiln's servers hand out a single step at a time for a
printer its owner is standing at.  This module is the hands and the floor:
it checks the machine before it asks, asks for exactly one step, reads the
step before it sends it, sends it, and says what the person should have
seen.  The next step is asked for only once the person says the last one
did what it described.

The floor, checked here on every step, before anything is asked or sent:

* the printer's own report names the model it is set up as;
* the printer is idle;
* the plate is empty by Kiln's plate record AND by the person's word;
* no heater is set, and the nozzle and bed read cold;
* the printer states its firmware version;
* the step asked for is the next one, and the person confirmed the last.

A step is sent only when it says a person at Kiln has read its sequence,
and only when every line in it is one a walk may carry, written exactly as
it will be sent; any other line is refused here whatever handed it over.
Nothing in a walk holds the printer: its own stop, pause, cancel and power
switch are never blocked, and no step waits on Kiln to be stopped.

Three doors, for the offer that sends a person into a walk:

* :func:`walk_offer` -- is a walk on offer for this printer and move;
* :func:`walk_step`  -- send one step, and describe it;
* :func:`walk_went_wrong` -- the person says a step did not do what it
  described.  A fault the printer raises during a step is reported the same
  way without being asked.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from kiln.printers.base import PrinterError, PrinterStatus
from kiln.printers.command_verdict import CommandVerdict

logger = logging.getLogger(__name__)

__all__ = [
    "COLD_MAX_C", "SENDER", "WALK_LARGEST", "WALK_LARGEST_MOTOR", "WALK_M221_SHAPES", "WALK_NEEDS_ONE_OF", "WALK_VERBS", "WALK_WORDS", "WalkRefused",
    "walk_offer", "walk_step", "walk_went_wrong",
]

#: This module's name in :data:`kiln.outbound_guard.VERSION_FIELDS`.
SENDER = "kiln/printers/motion_walk.py"

SCHEMA = "motion_walk/1"
WALK_VERBS = ("home", "park")

#: The warmest a nozzle or bed may read for a step to be sent, with no
#: heater set at all.
COLD_MAX_C: float = 45.0

#: How long a step is watched for a fault after it is sent, when the step
#: does not ask for longer; and the longest a step may ask for.
STEP_WATCH_S: float = 10.0
STEP_WATCH_MAX_S: float = 60.0

#: A walk nobody has advanced for this long starts again from its first step.
WALK_IDLE_S: float = 30 * 60

#: The only command words a walk step may carry, each with the only letters
#: it may carry them with.  Anything not named here is refused: a heater or
#: extruder word, a tool change, a macro, a second home, a word this list has
#: never seen.  A letter mapped to ``None`` takes a plain number; a tuple
#: names the only values it takes; ``""`` in a tuple is the letter alone.
_NUMBER = re.compile(r"-?(?:\d{1,5}(?:\.\d{1,4})?)")
#: The largest size a number may have, by word and letter: no desktop
#: printer travels further, moves faster or drives a motor harder.
WALK_LARGEST: dict[str, float] = {"X": 400.0, "Y": 400.0, "Z": 400.0, "F": 30000.0}
WALK_LARGEST_MOTOR: float = 2.0
_WORD = re.compile(r"[A-Z](?:-?\d+(?:\.\d+)?)?")
WALK_WORDS: dict[str, dict[str, tuple[str, ...] | None]] = {
    "G90": {},
    "G91": {},
    "G0": {"X": None, "Y": None, "Z": None, "F": None},
    "G1": {"X": None, "Y": None, "Z": None, "F": None},
    "G28": {"X": ("",), "Y": ("",)},
    "G380": {"S": ("2", "3"), "Z": None, "F": None},
    "M17": {"X": None, "Y": None, "Z": None},
    "M221": {"S": ("",), "R": ("",), "X": ("0",), "Y": ("0",), "Z": ("0",)},
}
#: Words that must carry at least one of these letters to mean anything here.
WALK_NEEDS_ONE_OF: dict[str, str] = {"G0": "XYZ", "G1": "XYZ", "G28": "XY", "G380": "Z", "M221": "SRXYZ"}
#: ``M221`` in exactly these shapes; with a number after ``S`` it is another command.
WALK_M221_SHAPES = (("S",), ("R",), ("X0", "Y0", "Z0"))


def _line_is_allowed(line: Any) -> bool:
    """Whether *line* is one a walk step may carry, written the one way it is sent.

    A line is read as it will be sent: capitals, single spaces, nothing else.
    A line number, a checksum, a comment, a tab, a carriage return, any
    control or non-ASCII character, words run together, or lowercase makes it
    a different line from the one that was read, and it is refused.
    """
    if not isinstance(line, str) or not line or len(line) > 64:
        return False
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in line):
        return False
    words = line.split(" ")
    if any(_WORD.fullmatch(word) is None for word in words):
        return False
    letters = WALK_WORDS.get(words[0])
    if letters is None:
        return False
    seen: set[str] = set()
    for word in words[1:]:
        letter, value = word[0], word[1:]
        if letter not in letters or letter in seen:
            return False
        seen.add(letter)
        takes = letters[letter]
        if takes is None:
            if _NUMBER.fullmatch(value) is None:
                return False
            largest = WALK_LARGEST_MOTOR if words[0] == "M17" else WALK_LARGEST[letter]
            if abs(float(value)) > largest or (letter == "F" and float(value) <= 0):
                return False
        elif value not in takes:
            return False
    needed = WALK_NEEDS_ONE_OF.get(words[0])
    if needed is not None and not seen & set(needed):
        return False
    return not (words[0] == "M221" and tuple(words[1:]) not in WALK_M221_SHAPES)


#: Where each walk stands in this process: ``{(machine, verb): (next step, when)}``.
_progress: dict[tuple[str, str], tuple[int, float]] = {}


class WalkRefused(PrinterError):
    """A walk step was not sent.  ``code`` names why; nothing moved."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# The floor
# ---------------------------------------------------------------------------


def _machine_reports(adapter: Any) -> str:
    """The catalogue model the printer's own report names, when every way it
    reports itself agrees with the model it is set up as; else refuse."""
    from kiln.printer_model_check import _row, check_printer_model, move_refusal

    disagreement = move_refusal(adapter)
    if disagreement:
        raise WalkRefused("PRINTER_MODEL_DISAGREES", disagreement)
    check = check_printer_model(adapter)
    channels_of = getattr(adapter, "get_identity_channels", None)
    channels = channels_of() if callable(channels_of) else {}
    agree = (
        check.status == "match" and bool(check.declared_row) and bool(channels)
        and all(_row(claim) == check.declared_row for claim in channels.values())
    )
    if not agree:
        raise WalkRefused(
            "PRINTER_MODEL_UNPROVEN",
            "Nothing was sent. Kiln could not confirm from the printer's own serial number and firmware name "
            "that it is the model it is set up as, and a walk is sent only to a printer that proves it.",
        )
    return str(check.declared_row)


def _reading(adapter: Any) -> dict[str, float]:
    """The printer's state as a walk needs it: idle, no heater set, cold."""
    state = adapter.get_state()
    if state.state is not PrinterStatus.IDLE:
        raise WalkRefused(
            "PRINTER_NOT_IDLE",
            f"Nothing was sent. The printer is {state.state.value}; a walk runs only on an idle printer.",
        )
    reading: dict[str, float] = {}
    for what, actual, target in (
        ("nozzle", state.tool_temp_actual, state.tool_temp_target),
        ("bed", state.bed_temp_actual, state.bed_temp_target),
    ):
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (actual, target)):
            raise WalkRefused(
                "NOT_COLD",
                f"Nothing was sent. Kiln could not read the {what}'s temperature and target, and a walk step "
                "is sent only to a printer it can see is cold.",
            )
        if target > 0:
            raise WalkRefused(
                "NOT_COLD",
                f"Nothing was sent. The {what} is set to heat to {target:g} °C; a walk is done cold. Switch "
                "the heater off, let it cool, then try again.",
            )
        if actual > COLD_MAX_C:
            raise WalkRefused(
                "NOT_COLD",
                f"Nothing was sent. The {what} reads {actual:g} °C; a walk is done cold, at or below "
                f"{COLD_MAX_C:g} °C. Let it cool, then try again.",
            )
        reading[f"{what}_c"] = float(actual)
        reading[f"{what}_target_c"] = float(target)
    return reading


def _plate_is_empty(adapter: Any, plate_clear: Any) -> None:
    from kiln.plate_state import plate_occupancy

    if not plate_occupancy(adapter).clear:
        raise WalkRefused(
            "PLATE_NOT_CLEAR",
            "Nothing was sent. Kiln's own record does not say this printer's plate is empty, and a walk runs "
            "only on an empty one. Take everything off the plate, tell Kiln it is clear, then try again.",
        )
    if plate_clear is not True:
        raise WalkRefused(
            "PLATE_CLEAR_REQUIRED",
            "Nothing was sent. Look at the plate now: a walk runs only once you have said it is empty "
            "(plate_clear=true).",
        )


def _firmware(adapter: Any) -> str:
    reader = getattr(adapter, "reported_firmware_version", None)
    version = reader() if callable(reader) else None
    if not isinstance(version, str) or not version.strip():
        raise WalkRefused(
            "FIRMWARE_UNKNOWN",
            "Nothing was sent. The printer has not stated its firmware version, and a walk counts only for "
            "the version it ran on. Check the printer is connected, then try again.",
        )
    return version.strip()


def _machine(adapter: Any) -> str:
    from kiln.plate_state import machine_id

    return machine_id(adapter) or f"object-{id(adapter)}"


def _in_order(adapter: Any, verb: str, step: int, saw_previous: Any) -> None:
    if isinstance(step, bool) or not isinstance(step, int) or step < 1:
        raise WalkRefused("INVALID_ARGUMENT", "Nothing was sent. A walk step is asked for by its number, from 1.")
    if step == 1:
        return
    key = (_machine(adapter), verb)
    expected, when = _progress.get(key, (0, 0.0))
    if expected != step or time.monotonic() - when > WALK_IDLE_S:
        _progress.pop(key, None)
        raise WalkRefused(
            "STEP_OUT_OF_ORDER",
            f"Nothing was sent. Step {step} follows step {step - 1}, sent a short while before it; start the "
            "walk again from step 1.",
        )
    if saw_previous is not True:
        raise WalkRefused(
            "STEP_NOT_CONFIRMED",
            f"Nothing was sent. Step {step} is sent only after you have said step {step - 1} did what it "
            "described (saw_previous=true). If it did not, say so with walk_went_wrong.",
        )


def _refuse_a_line_not_allowed(lines: list[str]) -> None:
    for line in lines:
        if not _line_is_allowed(line):
            raise WalkRefused(
                "LINE_REFUSED",
                "Nothing was sent. The step handed over carried a line a walk does not send: a walk moves "
                "the head and the bed, and never heats, extrudes or runs anything else.",
            )


# ---------------------------------------------------------------------------
# The asks
# ---------------------------------------------------------------------------


def _ask(adapter: Any, verb: str, walk: dict[str, Any], *, asked_by_person: bool) -> Any:
    """One ask of the served door.  What this install read off the machine
    goes through the outbound guard here, with the firmware version declared
    as one; no serial number is sent."""
    from kiln.outbound_guard import VERSION_FIELDS, scrub
    from kiln.server import _pro_api_call

    model = str(getattr(adapter, "_printer_model", "") or "").strip().lower()
    return _pro_api_call(
        "motion_plan", _asked_by_user=asked_by_person,
        printer_id=model, verb=verb, walk=scrub(walk, version_keys=VERSION_FIELDS[SENDER]),
    )


def _refusal_of(answer: Any) -> tuple[str, str] | None:
    if isinstance(answer, dict) and isinstance(answer.get("walk"), dict):
        return None
    if isinstance(answer, dict):
        return str(answer.get("code") or "NO_ANSWER"), str(answer.get("error") or "Kiln's servers gave no walk step.")
    return "NO_ANSWER", "Kiln's servers gave no walk step."


def _report(adapter: Any, verb: str, step: int, kind: str, code: str, reports: str | None, firmware: str | None) -> bool:
    try:
        answer = _ask(adapter, verb, {
            "op": "report", "machine_reports": reports or "", "firmware": firmware or "",
            "problem": {"step": int(step), "kind": kind, "code": code},
        }, asked_by_person=True)
    except Exception:  # noqa: BLE001 -- the report is owed; its failure is said, never raised over a fault
        logger.warning("walk: the report could not be sent", exc_info=True)
        return False
    return isinstance(answer, dict) and isinstance(answer.get("walk"), dict) and bool(answer["walk"].get("reported"))


def walk_offer(adapter: Any, verb: str) -> dict[str, Any]:
    """Whether a walk of *verb* is on offer for *adapter*'s printer.

    ``{"offered": True, "verb", "steps_total"}`` or ``{"offered": False}``.
    Sends nothing to the printer and never raises: an offer that cannot be
    confirmed is not made.
    """
    try:
        if verb not in WALK_VERBS:
            return {"offered": False}
        reports = _machine_reports(adapter)
        answer = _ask(adapter, verb, {"op": "offer", "machine_reports": reports}, asked_by_person=False)
        walk = answer.get("walk") if isinstance(answer, dict) else None
        if isinstance(walk, dict) and walk.get("offered") is True:
            return {"offered": True, "verb": verb, "steps_total": int(walk.get("steps_total") or 0)}
    except Exception:  # noqa: BLE001 -- an offer is a courtesy
        logger.debug("walk: no offer", exc_info=True)
    return {"offered": False}


def walk_step(adapter: Any, verb: str, step: int, *, plate_clear: Any = None, saw_previous: Any = None) -> dict[str, Any]:
    """Send step *step* of the walk of *verb* and describe it.

    :param plate_clear: ``True`` only when the person has looked and the
        plate is empty.  Asked on every step.
    :param saw_previous: ``True`` only when the person says the step before
        this one did what it described.  Read from the second step on.

    Returns ``{"success": True, "step_sent", "steps_total", "label",
    "you_should_see", "stops_when", "confirm", "if_not", "stop", "next_step"}``.
    Raises :class:`WalkRefused` (nothing was sent) when the floor, or Kiln's
    servers, say no.  A fault during the step comes back ``success: False``
    with what was reported; the walk is over.
    """
    from kiln.printers import motion_plan as _exec

    if verb not in WALK_VERBS:
        raise WalkRefused("INVALID_ARGUMENT", f"Nothing was sent. A walk is for one of: {', '.join(WALK_VERBS)}.")
    reports = _machine_reports(adapter)
    reading = _reading(adapter)
    _plate_is_empty(adapter, plate_clear)
    firmware = _firmware(adapter)
    _in_order(adapter, verb, step, saw_previous)
    answer = _ask(adapter, verb, {
        "op": "step", "machine_reports": reports, "firmware": firmware, "step": step,
        "confirmed_previous": saw_previous is True, "plate_record_clear": True,
        "plate_person_clear": plate_clear is True, **reading,
    }, asked_by_person=True)
    refusal = _refusal_of(answer)
    if refusal is not None:
        _exec.note_motion_outcome(adapter, verb, "refused", "walk_not_served")
        raise WalkRefused(refusal[0], f"Nothing was sent. {refusal[1]}")
    walk = answer["walk"]
    served = walk.get("step") if isinstance(walk.get("step"), dict) else {}
    lines = served.get("gcode")
    total = walk.get("steps_total")
    well_formed = (
        walk.get("schema") == SCHEMA and walk.get("verb") == verb and served.get("number") == step
        and walk.get("review") == "reviewed" and isinstance(walk.get("sequence"), str) and walk["sequence"]
        and isinstance(lines, list) and lines and all(isinstance(line, str) and line.strip() for line in lines)
        and isinstance(total, int) and not isinstance(total, bool) and step <= total
    )
    if not well_formed:
        raise WalkRefused("MALFORMED_STEP", "Nothing was sent. The step handed over was not one step of this walk.")
    _refuse_a_line_not_allowed(lines)
    key = (_machine(adapter), verb)
    _progress.pop(key, None)
    snapshot = getattr(adapter, "_snapshot_faults", None)
    faults_before = set(snapshot()) if callable(snapshot) else None
    verdict = CommandVerdict.coerce(adapter.send_gcode(list(lines)), what=verb)
    asked = served.get("watch_seconds")
    watch = STEP_WATCH_S
    if isinstance(asked, (int, float)) and not isinstance(asked, bool) and asked == asked:
        watch = min(max(float(asked), STEP_WATCH_S), STEP_WATCH_MAX_S)
    fault = None if not verdict.ok else _exec._watch_for_fault(adapter, faults_before, watch)
    if not verdict.ok or fault is not None:
        code = "rejected" if not verdict.ok else fault[0]
        _exec.note_motion_outcome(adapter, verb, "fault", code)
        reported = _report(adapter, verb, step, "fault", code, reports, firmware)
        return {
            "success": False, "step_sent": step, "steps_total": total, "error_code": code,
            "message": (
                (f"The printer refused step {step}: {verdict.message}" if not verdict.ok
                 else f"The printer raised {code} during step {step}.")
                + " The walk is over; do not send another step. Use the printer's own controls from here. "
                + ("Kiln was told, and this walk is withdrawn for everyone until a person there has looked."
                   if reported else "Kiln could not be told just now; say so with walk_went_wrong when you can.")
            ),
            "reported": reported,
        }
    _exec.note_motion_outcome(adapter, verb, "step_sent", (step, total))
    if step < total:
        _progress[key] = (step + 1, time.monotonic())
    nxt = walk.get("next") if isinstance(walk.get("next"), dict) else None
    return {
        "success": True, "step_sent": step, "steps_total": total,
        "label": str(served.get("label") or f"step {step}"),
        "you_should_see": str(served.get("you_will_see") or ""),
        "stops_when": str(served.get("stops_when") or ""),
        "confirm": str(served.get("confirm") or "Did the printer do exactly that, and nothing else?"),
        "if_not": str(served.get("if_not") or "Press the printer's own stop and say it went wrong."),
        "stop": str(walk.get("stop") or "The printer's own stop, pause and power switch work at every moment."),
        "next_step": ({"number": int(nxt.get("number") or step + 1), "label": str(nxt.get("label") or "")}
                      if nxt and step < total else None),
        "message": (
            f"Step {step} of {total} sent ({served.get('label') or 'step'}): {served.get('you_will_see') or ''}. "
            f"Raised no fault in {watch:g}s. "
            + (f"Before step {step + 1}: {served.get('confirm') or 'did it do exactly that?'}" if step < total
               else "That was the last step; the walk is complete.")
        ),
    }


def walk_went_wrong(adapter: Any, verb: str, step: int) -> dict[str, Any]:
    """The person says step *step* did not do what it described.

    Ends the walk here and tells Kiln's servers.  Sends nothing to the
    printer.  Returns ``{"reported": bool, "message"}`` and never raises.
    """
    from kiln.printers import motion_plan as _exec

    _progress.pop((_machine(adapter), verb), None)
    if verb not in WALK_VERBS:
        return {"reported": False, "message": f"A walk is for one of: {', '.join(WALK_VERBS)}."}
    try:
        reports: str | None = _machine_reports(adapter)
    except Exception:  # noqa: BLE001 -- a report is still owed
        reports = None
    try:
        firmware: str | None = _firmware(adapter)
    except Exception:  # noqa: BLE001
        firmware = None
    number = step if isinstance(step, int) and not isinstance(step, bool) and step > 0 else 0
    _exec.note_motion_outcome(adapter, verb, "fault", "person")
    reported = _report(adapter, verb, number, "person", "person", reports, firmware)
    return {
        "reported": reported,
        "message": (
            "The walk is over; do not send another step. Use the printer's own controls from here. "
            + ("Kiln was told, and this walk is withdrawn for everyone until a person there has looked."
               if reported else "Kiln could not be told just now; try this report again shortly.")
        ),
    }
