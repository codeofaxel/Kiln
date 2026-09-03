"""The lite printer-status roster carries what a live poller needs — pinned.

``printer_status(detail="lite")`` is the shape polled every ~2.5 seconds by
anything watching a print, so its key roster IS a contract: a key trimmed
from it does not degrade gracefully, it blanks a readout somewhere that
cannot see why.  These tests pin the two halves added for the web Monitor:

* ``last_job_result`` survives the lite trim — the completion card needs the
  machine's own word for how the last job ended, on its own axis, because
  ``idle`` means ready and cannot also mean finished; and
* a configured printer that is not ANSWERING is a successful, structured
  snapshot (``connected: false``, ``state: "offline"``) rather than a tool
  error — "your printer is off" and "the question failed" are different
  answers with different fixes, while a refusal that reads as credentials
  and the no-printer-configured case remain typed errors.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln import server
from kiln.printers.base import (
    JobProgress,
    JobResult,
    PrinterError,
    PrinterState,
    PrinterStatus,
)
from kiln.printers.octoprint import OctoPrintAdapter


def _adapter_with(state: PrinterState) -> MagicMock:
    adapter = MagicMock(spec=OctoPrintAdapter)
    adapter.get_state.return_value = state
    adapter.get_job.return_value = JobProgress()
    return adapter


def test_last_job_result_survives_the_lite_trim():
    state = PrinterState(
        state=PrinterStatus.IDLE,
        connected=True,
        last_job_result=JobResult.COMPLETED,
    )
    with patch("kiln.server._get_adapter", return_value=_adapter_with(state)):
        out = server.printer_status(detail="lite")

    assert out["success"] is True
    assert out["printer"]["last_job_result"] == "completed"


def test_lite_still_omits_an_unreported_result():
    """Absent means "not reported" — the trim must not invent the key."""
    state = PrinterState(state=PrinterStatus.IDLE, connected=True)
    with patch("kiln.server._get_adapter", return_value=_adapter_with(state)):
        out = server.printer_status(detail="lite")

    assert out["success"] is True
    assert "last_job_result" not in out["printer"]


@pytest.mark.parametrize("detail", ["full", "lite"])
def test_an_unreachable_printer_is_a_structured_offline_answer(detail):
    """The ordinary case: configured, powered off or unplugged.

    A successful snapshot with ``connected: false`` and ``state: "offline"``
    — renderable as "your printer is offline", with the adapter's own words
    carried as ``offline_reason`` instead of thrown away.
    """
    adapter = MagicMock(spec=OctoPrintAdapter)
    adapter.get_state.side_effect = PrinterError("connection refused by 192.0.2.9")
    with patch("kiln.server._get_adapter", return_value=adapter):
        out = server.printer_status(detail=detail)

    assert out["success"] is True
    assert out["printer"]["connected"] is False
    assert out["printer"]["state"] == "offline"
    assert "connection refused" in out["offline_reason"]


def test_an_auth_refusal_stays_a_typed_error():
    """A wrong access code is not a powered-off printer.

    Laundering it into offline sends someone to check a power switch for a
    credentials problem — the spec's own forbidden case.
    """
    adapter = MagicMock(spec=OctoPrintAdapter)
    adapter.get_state.side_effect = PrinterError("Not authorized: check the access code")
    with patch("kiln.server._get_adapter", return_value=adapter):
        out = server.printer_status()

    assert out["success"] is False


def test_no_printer_configured_stays_a_typed_error():
    """RuntimeError = nothing is configured — a setup problem, not a state."""
    with patch(
        "kiln.server._get_adapter",
        side_effect=RuntimeError("No printer configured. Set KILN_PRINTER_HOST …"),
    ):
        out = server.printer_status()

    assert out["success"] is False
    assert "No printer configured" in out["error"]["message"]


def test_every_lite_key_still_exists_on_printer_state():
    """The roster names keys a PrinterState can actually emit.

    A key that drifts from the dataclass silently blanks a readout at poll
    cadence — exactly the failure a contract test exists to catch at commit
    time instead.  Checked against what ``to_dict`` can produce rather than
    against the fields alone, because some readouts are DERIVED: the HMS
    code is formatted from the raw one rather than stored twice."""
    import dataclasses

    emitted = set(
        PrinterState(
            connected=True,
            state=PrinterStatus.STALE,
            last_known_state=PrinterStatus.PRINTING,
            print_error=302022663,
            state_age_seconds=1396.0,
            state_stale_after_seconds=60.0,
            last_job_result=JobResult.CANCELLED,
            cause="reachable_but_silent",
            remedy="power-cycle the printer",
        ).to_dict()
    )
    known = {f.name for f in dataclasses.fields(PrinterState)} | emitted
    for key in server._LITE_PRINTER_KEYS:
        assert key in known, f"lite key {key!r} is not a PrinterState readout"
