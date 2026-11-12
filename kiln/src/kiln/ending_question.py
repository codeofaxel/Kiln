"""The one question a print's ending can owe the person, and how each answer
is recorded.

Two endings are asked about, once each:

* a **stop**: the printer stopped the print and named no cause;
* a **cancel**: someone stopped the print.

Only the person knows whether the part was coming out bad.  A row carries the
question until it is answered or the person has nothing to add
(``KilnDB.mark_ending_asked``).  Every door that tells an agent a print ended
reads :func:`for_row` or :func:`for_printer`; none words the question itself.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiln.failure_vocabulary import ending_wants_a_question

_logger = logging.getLogger(__name__)

#: How long after an ending the question rides the tool that reported it.
AT_THE_ENDING_S = 15 * 60

QUESTIONS: dict[str, str] = {
    "stop": (
        "Your print stopped and the printer gave no reason. Did it come out "
        "bad, or did something stop it?"
    ),
    "cancel": "That print was stopped before it finished. Was it going wrong, or did you just stop it?",
}

#: What to send for each thing the person can say.  ``{job_id}`` is the row's.
ANSWERS: dict[str, dict[str, str]] = {
    "stop": {
        "it came out bad": (
            'record_print_outcome(job_id="{job_id}", outcome="failed", determined_by="user_reported") '
            "(add failure_mode when they name the defect)"
        ),
        "the power went out": (
            'record_print_outcome(job_id="{job_id}", outcome="failed", '
            'failure_mode="power_loss", determined_by="user_reported")'
        ),
        "the filament ran out": (
            'record_print_outcome(job_id="{job_id}", outcome="failed", '
            'failure_mode="filament_runout", determined_by="user_reported")'
        ),
        "the printer faulted": (
            'record_print_outcome(job_id="{job_id}", outcome="failed", '
            'failure_mode="mechanical", determined_by="user_reported")'
        ),
        "the nozzle clogged": (
            'record_print_outcome(job_id="{job_id}", outcome="failed", '
            'failure_mode="clog", determined_by="user_reported")'
        ),
        "they cannot say": 'record_print_outcome(job_id="{job_id}", outcome="{outcome}", ending_asked=True)',
    },
    "cancel": {
        "it was going wrong": (
            'record_print_outcome(job_id="{job_id}", outcome="failed", determined_by="user_reported") '
            "(add failure_mode when they name the defect)"
        ),
        "they just stopped it": 'record_print_outcome(job_id="{job_id}", outcome="cancelled", ending_asked=True)',
    },
}

RULE = (
    "Ask once, in your own words, at a natural moment. Record what they say "
    "and never pick an answer for them. If they do not answer, leave it."
)


def for_row(row: dict[str, Any] | None, *, about: str | None = None) -> dict[str, Any] | None:
    """The question *row* owes, or ``None``.  *about* forces the kind for a
    row whose ending is not written yet (a cancel Kiln just sent)."""
    if not isinstance(row, dict) or row.get("stop_reason_asked"):
        return None
    kind = about or ending_wants_a_question(
        row.get("outcome"), row.get("failure_mode"), row.get("determined_by")
    )
    if kind not in QUESTIONS:
        return None
    job_id = str(row.get("job_id") or "")
    if not job_id:
        return None
    outcome = "cancelled" if kind == "cancel" else str(row.get("outcome") or "failed")
    return {
        "about": kind,
        "job_id": job_id,
        "question": QUESTIONS[kind],
        "record": {said: call.format(job_id=job_id, outcome=outcome) for said, call in ANSWERS[kind].items()},
        "rule": RULE,
    }


def for_printer(
    printer_name: str | None, *, cancelling: bool = False, db: Any = None,
) -> dict[str, Any] | None:
    """The question the print that just ended on *printer_name* owes, or
    ``None``.  *cancelling* is the cancel tool's own call: the newest row is
    asked about as a cancel even while its ending is still being written.
    Never raises: a question is never worth failing the tool that carries it.
    """
    try:
        if db is None:
            from kiln.persistence import get_db

            db = get_db()
        if not printer_name:
            import kiln.server as server
            from kiln.printers.base import outcome_printer_name

            # The name the active printer's rows are filed under.
            printer_name = outcome_printer_name(server._get_adapter())
        if not printer_name:
            return None
        row = db.latest_outcome_row(printer_name)
        if row is None:
            return None
        if cancelling and row.get("outcome") in ("pending", "cancelled"):
            return for_row(row, about="cancel")
        when = row.get("ended_at") or row.get("created_at") or 0
        if time.time() - float(when) > AT_THE_ENDING_S:
            return None
        return for_row(row)
    except Exception:  # noqa: BLE001
        _logger.debug("ending question not read", exc_info=True)
        return None


def attach(result: dict[str, Any], printer_name: str | None = None, *, cancelling: bool = False) -> dict[str, Any]:
    """*result* with ``ask_the_person`` when the ending owes a question."""
    if isinstance(result, dict) and result.get("success") is not False:
        ask = for_printer(printer_name, cancelling=cancelling)
        if ask:
            result["ask_the_person"] = ask
    return result
