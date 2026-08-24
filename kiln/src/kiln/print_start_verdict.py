"""One verdict for "did the print start?", shared by every door that asks.

Why this module exists
----------------------
``PrinterAdapter.start_print`` answers with a boolean, and a boolean has no
room for the state a printer is genuinely in a moment after the command goes
out: the job is accepted and the machine is still homing, loading filament or
calibrating.  So every caller that folded that boolean into a response
invented its own convention, and the conventions disagreed — ``slice_and_print``
published a hardcoded ``success: True`` beside a nested ``success: False``
about the same print, and the pipeline steps discarded the boolean and
reported success unconditionally.  A caller could not name a field to branch
on and be right.

It also exists because a NEGATIVE answer needs corroboration.  An adapter that
answers from a push cache (Bambu, over MQTT) reports the last thing the printer
said, and the last thing the printer said may predate the command being asked
about: a ``gcode_state`` left at ``failed`` by the previous, cancelled job
reads exactly like this job failing.  Measured on an A1 on 2026-08-11 — a print
that ran to completion was reported ``success=False, "printer reported a
failure"``, twice, because the start check read the cache microseconds after
publishing the command, before the printer could have said anything about it.

So this module enforces one sentence:

    A reading that predates the command is not a verdict on the command.

Three states, and every answer is exactly one of them
-----------------------------------------------------
``started``
    The printer, asked after the command went out, is printing.
``accepted``
    The command was sent and not refused, and the machine has not confirmed it
    is running.  This is the honest answer during the transient (homing, AMS
    load, calibration) and the honest answer when nothing has been heard back
    yet.  It is a real state, not a hedge: the caller's next move is to watch.
``failed``
    The printer, asked after the command went out, is idle or errored — it did
    not take the job.

The softening is ONE-DIRECTIONAL by design: an uncorroborated failure becomes
``accepted``, and an ``accepted`` is never promoted to ``started`` on the
strength of a reading the adapter itself did not confirm.  A false "it failed"
costs a duplicate print or an abandoned running machine; a false "it started"
costs an unwatched failure.  ``accepted`` costs neither, because it tells the
caller to look.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ACCEPTED",
    "FAILED",
    "STARTED",
    "PrintStartVerdict",
    "resolve_print_start",
]

#: The printer, asked after the command, is printing.
STARTED = "started"
#: Sent and not refused; the machine has not confirmed it is running.
ACCEPTED = "accepted"
#: The printer, asked after the command, did not take the job.
FAILED = "failed"

#: Printer states that confirm a job is running right now.
_RUNNING = frozenset({"printing"})

#: Printer states that, seen AFTER the command, refute a start.  Deliberately
#: short: ``unknown`` and ``offline`` are absence of evidence, not evidence of
#: refusal, so they do not license a ``failed``.
_REFUTES_START = frozenset({"idle", "error"})

#: ``state_age_seconds`` is rounded to a tenth and is measured a hair after the
#: elapsed clock this compares it against.  This covers that arithmetic — not a
#: real second of staleness.
_AGE_TOLERANCE_S = 0.2


@dataclass(frozen=True)
class PrintStartVerdict:
    """A single answer to "did the print start?", with its reasoning attached."""

    state: str
    message: str
    job_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """``False`` only when the printer refused the job."""
        return self.state != FAILED

    @property
    def confirmed(self) -> bool:
        """``True`` only when the printer was seen printing after the command."""
        return self.state == STARTED

    def to_dict(self) -> dict[str, Any]:
        """The print half of a tool envelope.

        ``success`` and ``print_start`` here are the same two values the
        enclosing envelope publishes, so the two halves cannot disagree.
        """
        return {
            "success": self.ok,
            "print_start": self.state,
            "confirmed_running": self.confirmed,
            "message": self.message,
            "job_id": self.job_id,
            "evidence": dict(self.evidence),
        }


def _status_name(state: Any) -> str:
    """The printer status as a lowercase string.

    Reads ``PrinterStatus`` by value rather than importing it, so this module
    stays free of the adapter package and tolerates an adapter that answers
    with a plain string.
    """
    raw = getattr(state, "value", state)
    return str(raw or "").strip().lower()


def _reading_after_command(adapter: Any, sent_at: float) -> tuple[str | None, dict[str, Any]]:
    """The printer's state — but only if the printer said so after the command.

    Returns ``(status_name, evidence)``, where *status_name* is ``None`` when
    no reading postdates the command.  ``state_age_seconds`` is ``None`` for
    adapters that query the printer on every call, which are current by
    construction; it is a real age only for the push-cache adapters, and there
    a reading older than the command is about the previous job.
    """
    try:
        from kiln.printers.engagement import internal_read

        # Kiln corroborating the command it just sent, on the machine it just
        # sent it to.  Its own follow-up read, never a user command.
        with internal_read():
            state = adapter.get_state()
    except Exception as exc:  # noqa: BLE001 - any transport failure is "no reading"
        return None, {"corroboration": "unavailable", "detail": str(exc)}

    elapsed = max(0.0, time.monotonic() - sent_at)
    name = _status_name(getattr(state, "state", None))
    age = getattr(state, "state_age_seconds", None)

    if age is None:
        return name, {"corroboration": "live", "printer_state": name}
    if float(age) <= elapsed + _AGE_TOLERANCE_S:
        return name, {
            "corroboration": "after_command",
            "printer_state": name,
            "reading_age_seconds": float(age),
        }
    return None, {
        "corroboration": "predates_command",
        "printer_state": name,
        "reading_age_seconds": float(age),
        "seconds_since_command": round(elapsed, 1),
    }


def resolve_print_start(
    adapter: Any,
    result: Any,
    *,
    sent_at: float,
    file_name: str = "",
) -> PrintStartVerdict:
    """Turn an adapter's ``PrintResult`` into the one verdict every door publishes.

    Args:
        adapter: The printer adapter the command was sent through.  Only
            ``get_state()`` is called, and only to corroborate.
        result: The ``PrintResult`` returned by ``adapter.start_print``.
        sent_at: ``time.monotonic()`` captured immediately BEFORE the
            ``start_print`` call.  This is what makes a reading's age mean
            something: it is the line between "about the previous job" and
            "about this one".
        file_name: The file being printed, for the message.
    """
    adapter_ok = bool(getattr(result, "success", False))
    adapter_message = str(getattr(result, "message", "") or "")
    job_id = getattr(result, "job_id", None)
    name = file_name or "the file"

    status, evidence = _reading_after_command(adapter, sent_at)
    evidence["adapter_reported_success"] = adapter_ok
    evidence["adapter_message"] = adapter_message

    if adapter_ok:
        # The adapter's own message carries detail this layer does not have —
        # the transient state it saw, and any pre-print warnings (AMS colour
        # mismatch) it appended.  Keep it verbatim; only the state is ours.
        state = STARTED if status in _RUNNING else ACCEPTED
        return PrintStartVerdict(
            state=state,
            message=adapter_message or f"Print command accepted for {name}.",
            job_id=job_id,
            evidence=evidence,
        )

    if status in _REFUTES_START:
        return PrintStartVerdict(
            state=FAILED,
            message=adapter_message or f"The printer did not start {name}.",
            job_id=job_id,
            evidence=evidence,
        )

    # The adapter says it failed and the printer has not been heard from since
    # the command — or has, and is busy.  Either way the failure is not
    # supported, so it is not published.
    if status is None:
        detail = (
            "Nothing has been heard from the printer since the command went out, "
            "so whether it started is not yet known."
        )
    else:
        detail = f"The printer reports {status}."
    return PrintStartVerdict(
        state=ACCEPTED,
        message=(
            f"Print command accepted for {name}. {detail} The start check's "
            f"failure verdict came from a reading that does not postdate this "
            f"command, so it is not evidence about this job — call "
            f"printer_status() to confirm."
        ),
        job_id=job_id,
        evidence=evidence,
    )
