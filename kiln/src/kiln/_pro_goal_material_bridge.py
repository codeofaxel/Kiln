"""Public Kiln -> Kiln's servers: a saved goal learns the filament its part printed in.

``design_session`` keeps a goal -- what a part is for, where it lives, the
filament chosen for it -- in the person's Kiln account, and on an install
without kiln-pro it lives on Kiln's servers (https://kiln3d.com).  When the
goal's part is printed in a different filament (a reprint in another
material, a print of whatever spool is loaded, a swap after Kiln refused one
the printer cannot melt), the goal has to learn it, or the next make and
every check from the goal start again from the filament that was set aside.

Every print Kiln starts passes through the start template in
:mod:`kiln.printers.base`, which calls :func:`note_print_started`: the one
place, whichever door started the print.  It works out:

* which goal -- the one a door named (:func:`door`), a ``brief_id`` in the
  start's arguments, or the goal named beside the model the file was sliced
  from (its declared-intent note);
* which filament -- the one the start commanded, else the one the sliced
  file was sliced for.  A file sliced for several filaments names none.

and tells the goal through the same served tool, as
``design_session(verb="update", material_change=...)``.  The goal decides
whether anything changed (the same filament records nothing), keeps the old
filament on its history, and says why when the printer could not print it.

A door that named the goal waits for the answer and puts it in its reply
(:meth:`DoorAsk.wait`), or a miss (offline, signed out, no answer, refused)
said once; a start no door is waiting on tells the goal on a thread of its
own, and on a miss the print's own record carries the filament to the goal
with its ending: the row every print opens at its start
(:func:`kiln.auto_record_hook.open_pending_outcome`) names the filament the
same way this module does (:func:`material_printed`), and the courier that
copies the row to the person's account (:mod:`kiln.outcome_courier`) is what
the account reads the swap from.  A print of a model no kept version holds
has no row to ride that way, so a goal it names is told only here.  Nothing
here raises or holds up a print.  Free at every plan: it is the person's own
record of their own part.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The served tool a goal lives behind, and the step that amends it.
WIRE_TOOL = "design_session"
UPDATE_VERB = "update"
#: A person is waiting on the reprint's reply; the record is one small write.
CONSULT_TIMEOUT_S: float = 15.0
#: The key a door's reply carries the answer under.
REPLY_KEY = "goal_material"

#: Who chose the filament, when a door was told.
CHOSEN_BY = ("person", "agent")

_GOAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_GOAL_GENERATOR = "design_brief:"


@dataclass
class DoorAsk:
    """What a door that started a print knows about it, and the goal's answer.

    Opened by :func:`door` around the call that starts the print; the start
    template reads it, and the door reads :meth:`wait` once the call returns.
    """

    via: str
    brief_id: str = ""
    chosen_by: str = ""
    thread: threading.Thread | None = None
    said: dict[str, Any] | None = None

    def wait(self, timeout: float = CONSULT_TIMEOUT_S + 2.0) -> dict[str, Any] | None:
        """The goal's answer for the print this door started, or ``None`` when
        no print of a goal's part, in one known filament, started."""
        thread = self.thread
        if thread is None:
            return None
        thread.join(timeout)
        if thread.is_alive():
            from kiln import served_answer

            return _missed(self.brief_id or "", "", served_answer.Miss("unanswered", detail="still waiting"))
        return self.said


_DOOR: ContextVar[DoorAsk | None] = ContextVar("kiln_goal_material_door", default=None)


@contextlib.contextmanager
def door(*, via: str, brief_id: str = "", chosen_by: str = "") -> Iterator[DoorAsk]:
    """Open a door's ask around the call that starts a print.

    *brief_id* is the goal the door was told the part belongs to, *chosen_by*
    who picked the filament (``person`` / ``agent``; anything else is left
    unsaid).  The yielded :class:`DoorAsk` carries the goal's answer.
    """
    ask = DoorAsk(
        via=via,
        brief_id=str(brief_id or "").strip(),
        chosen_by=chosen_by if chosen_by in CHOSEN_BY else "",
    )
    token = _DOOR.set(ask)
    try:
        yield ask
    finally:
        _DOOR.reset(token)


def note_print_started(adapter: Any, file_name: str, kwargs: dict[str, Any]) -> threading.Thread | None:
    """Tell the goal this print belongs to which filament it is printing in.

    Called by :meth:`kiln.printers.base.PrinterAdapter.start_print` once a
    print has started.  Reads nothing heavier than this start's own
    arguments and the slice ledger here; the files and the servers are read
    on a thread of its own, returned so a test or a door can wait for it.
    ``None`` when there is nothing to tell.  Never raises.
    """
    try:
        ask = _DOOR.get()
        named = (ask.brief_id if ask is not None else "") or _text(kwargs.get("brief_id"))
        files = _printed_files(file_name)
        if not named and not (files and files.get("models")):
            return None
        commanded = _text(kwargs.get("material") or kwargs.get("material_type"))
        printer_model = ""
        with contextlib.suppress(Exception):
            printer_model = str(adapter.declared_printer_model() or "")
        thread = threading.Thread(
            target=_tell_the_goal,
            args=(named, files or {}, commanded, printer_model, ask),
            name="kiln-goal-filament",
            daemon=True,
        )
        if ask is not None:
            ask.thread = thread
        thread.start()
        return thread
    except Exception:  # noqa: BLE001 -- a goal's record never touches a print
        logger.debug("goal filament note not started", exc_info=True)
        return None


def goal_named_beside(model_path: str) -> str:
    """The saved goal a model was made for, read from the declared-intent note
    Kiln keeps beside it; ``""`` when there is none."""
    try:
        from kiln.original_design import intent_sidecar_path

        sidecar = intent_sidecar_path(model_path)
        if sidecar is None or not sidecar.is_file():
            return ""
        generator = json.loads(sidecar.read_text(encoding="utf-8")).get("generator")
    except Exception:  # noqa: BLE001 -- an unreadable note names no goal
        return ""
    if isinstance(generator, str) and generator.startswith(_GOAL_GENERATOR):
        goal = generator[len(_GOAL_GENERATOR):].strip()
        return goal if _GOAL_ID.fullmatch(goal) else ""
    return ""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _printed_files(file_name: str) -> dict[str, Any] | None:
    try:
        from kiln.monitor_twin import printed_files_for

        return printed_files_for(file_name)
    except Exception:  # noqa: BLE001 -- a ledger miss is no file
        return None


def material_printed(file_name: str, commanded: Any = "", files: dict[str, Any] | None = None) -> str:
    """The filament a start of *file_name* is printing in, as far as this
    computer can say: the one the start *commanded*, else the one the sliced
    file names (one filament; a file sliced for several names none); ``""``
    when neither says.  The one rule for the goal and for the print's own
    record, so the two never disagree.  *files* is the slice ledger's answer
    when the caller already has it.  Never raises.
    """
    said = _text(commanded)
    if said:
        return said
    if files is None:
        files = _printed_files(file_name)
    return _material_of((files or {}).get("gcode"))


def _material_of(gcode_path: str | None) -> str:
    """The one filament *gcode_path* was sliced for, or ``""`` (none named, or
    several)."""
    if not gcode_path:
        return ""
    try:
        from kiln.file_metadata import sliced_gcode_lines
        from kiln.gcode import slicer_filament_types

        lines = sliced_gcode_lines(gcode_path)
        if not lines:
            return ""
        types = {t.strip().upper() for t in slicer_filament_types("\n".join(lines)) if t.strip()}
    except Exception:  # noqa: BLE001 -- an unreadable file names no filament
        return ""
    return types.pop() if len(types) == 1 else ""


def _tell_the_goal(
    named: str, files: dict[str, Any], commanded: str, printer_model: str, ask: DoorAsk | None,
) -> None:
    goal = named
    if not goal:
        for model in files.get("models") or []:
            goal = goal_named_beside(model)
            if goal:
                break
    if not goal or not _GOAL_ID.fullmatch(goal):
        return
    material = material_printed("", commanded, files)
    if not material:
        return
    change: dict[str, str] = {
        "material": material,
        "via": (ask.via if ask is not None else "") or "print",
    }
    if ask is not None and ask.chosen_by:
        change["decided_by"] = ask.chosen_by
    if printer_model:
        change["printer_model"] = printer_model
    said = _ask(goal, change, waited=ask is not None)
    if ask is not None:
        ask.said = said
    elif said.get("why"):
        logger.debug(
            "goal %s not told its filament now (%s); the print's own record carries it "
            "to the goal with its ending",
            goal, said.get("why"),
        )


def _ask(goal: str, change: dict[str, str], *, waited: bool) -> dict[str, Any]:
    """The block a door's reply carries: what the goal recorded, or the miss."""
    from kiln import served_answer

    try:
        import kiln.server as _srv

        bearer, expired = _srv._served_bearer()
        if not bearer:
            code = served_answer.SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
            return _missed(goal, change["material"], served_answer.Miss("signed_out", code))
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=CONSULT_TIMEOUT_S,
            # A door that named the goal was asked to print the goal's part;
            # a start nobody waits on is Kiln's own bookkeeping.
            _asked_by_user=waited,
            verb=UPDATE_VERB,
            goal_id=goal,
            material_change=change,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("goal filament request failed", exc_info=True)
        import kiln.server as _srv

        miss = served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
        return _missed(goal, change["material"], miss)
    if isinstance(answer, dict) and answer.get("code") == "NOT_FOUND":
        return {
            "brief_id": goal,
            "recorded": False,
            "material": change["material"],
            "note": (
                f"Saved goal {goal} is not in your Kiln account, so the filament this part "
                "printed in was not recorded on it."
            ),
        }
    said = answer.get("material_change") if isinstance(answer, dict) else None
    if not isinstance(said, dict) or not isinstance(said.get("note"), str):
        miss = served_answer.classify_answer(answer) or served_answer.Miss("unanswered", detail="no goal in the answer")
        return _missed(goal, change["material"], miss)
    block = {k: said[k] for k in ("brief_id", "recorded", "material", "replaced", "why", "note") if k in said}
    block["recorded_on"] = "kiln_servers"
    return block


def _missed(goal: str, material: str, miss: Any) -> dict[str, Any]:
    """The print stands; the goal was not told: said once."""
    from kiln import served_answer

    printed = f" in {material}" if material else ""
    said = served_answer.sentence(
        miss,
        feature="servers",
        on_the_line=f"Saved goal {goal} lives in your Kiln account and keeps the filament its part is made in",
        cannot=f"record that this part printed{printed}",
        wont="left the goal's filament as it was",
        safe_remedy="The print runs as started",
        then="tell the goal with design_session's update step",
    )
    return {
        "brief_id": goal,
        "recorded": False,
        "material": material,
        "note": said,
        **served_answer.fields(miss),
    }


__all__ = [
    "CHOSEN_BY",
    "CONSULT_TIMEOUT_S",
    "REPLY_KEY",
    "UPDATE_VERB",
    "WIRE_TOOL",
    "DoorAsk",
    "door",
    "goal_named_beside",
    "material_printed",
    "note_print_started",
]
