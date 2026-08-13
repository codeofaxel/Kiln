"""A latched firmware error must not be a dead end.

Measured on an A1 (2026-08-13).  A print cancelled during bed levelling left
the firmware reporting ``gcode_state=failed`` with ``print_error=50348032``,
which maps to :attr:`PrinterStatus.ERROR`, and ``preflight_check`` then
refused every subsequent print.  Dismissing the message on the printer's own
screen cleared the notification but not the reported state: the machine read
"ready" while Kiln — correctly — would not start a job.  Nothing in Kiln could
reconcile the two.  Only a power cycle did.

Kiln is allowed to refuse to act on what a printer reports.  It is not allowed
to leave the user with no way to settle the disagreement, and that is the only
thing this capability adds.  It clears the LATCH, never the cause: a printer
that halted for a real fault halts again on the next attempt, which is the
outcome that keeps this honest rather than a way to talk a machine out of its
own safety response.

``duet.emergency_stop`` had already promised this: it deliberately sends only
``M112`` and not the ``M999`` that Duet Web Control pairs with it, "because
Kiln exposes clearing an emergency stop as its own separate, deliberate
action."  That action did not exist for printer-side errors until now.
"""

from __future__ import annotations

import importlib

import pytest

from kiln.printers.base import (
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
    PrintResult,
)

#: Every shipped backend, and whether it is expected to know its firmware's
#: acknowledgement.  The two that do not are declared, not forgotten.
_BACKENDS = [
    ("kiln.printers.bambu", "BambuAdapter", True),
    ("kiln.printers.moonraker", "MoonrakerAdapter", True),
    ("kiln.printers.octoprint", "OctoPrintAdapter", True),
    ("kiln.printers.duet", "DuetAdapter", True),
    ("kiln.printers.serial_adapter", "SerialPrinterAdapter", True),
    ("kiln.printers.creality", "CrealityAdapter", True),
    ("kiln.printers.prusalink", "PrusaLinkAdapter", False),
    ("kiln.printers.elegoo", "ElegooAdapter", False),
]


def _cls(module: str, name: str):
    return getattr(importlib.import_module(module), name)


def _tool(server):
    """The tool's callable, however the MCP decorator happens to wrap it."""
    fn = server.clear_printer_error
    return getattr(fn, "fn", getattr(fn, "callback", fn))


@pytest.mark.parametrize(("module", "name", "expected"), _BACKENDS)
def test_every_backend_declares_whether_it_can_clear(module, name, expected):
    """The roster is explicit, so a NEW backend cannot join by silence.

    A backend that inherits the base refusal without anyone deciding that is
    correct looks identical, from the outside, to one that was considered and
    genuinely cannot — which is the difference this list exists to keep.
    """
    implements = "clear_error" in _cls(module, name).__dict__
    assert implements is expected, (
        f"{name} {'no longer implements' if expected else 'now implements'} "
        "clear_error — update this roster deliberately, and say why in "
        "scripts/adapter_conformance.yaml"
    )


def test_an_adapter_never_claims_more_than_it_implements():
    """The exact drift that bit Creality while this was being written.

    It fulfils the protocol by holding a Moonraker backend and delegates
    ``capabilities`` to it — so the moment Moonraker learned to clear errors,
    Creality advertised the capability and still answered with the base
    class's refusal.  A button that reports itself available and does nothing
    is worse than no button, so the claim and the implementation are pinned
    together here rather than trusted to stay in step.
    """

    class _ClaimsButCannot(PrinterAdapter):
        @property
        def name(self) -> str:
            return "liar"

        @property
        def capabilities(self) -> PrinterCapabilities:
            return PrinterCapabilities(can_clear_error=True)

        def get_state(self) -> PrinterState:
            return PrinterState(connected=True, state=PrinterStatus.ERROR)

        def get_job(self): ...
        def _start_print_impl(self, file_name, **kw): ...
        def list_files(self): return []
        def upload_file(self, file_path): ...
        def delete_file(self, file_name): return True
        def cancel_print(self): ...
        def pause_print(self): ...
        def _resume_print_impl(self): ...
        def emergency_stop(self): ...
        def send_gcode(self, command): return "ok"
        def set_tool_temp(self, celsius, tool=0): return True
        def set_bed_temp(self, celsius): return True

    adapter = _ClaimsButCannot()
    # It claims the capability but never overrode the method, so it falls back
    # to the base refusal — the shape the real audit above is guarding against.
    assert adapter.capabilities.can_clear_error is True
    assert "clear_error" not in type(adapter).__dict__
    assert adapter.clear_error().success is False


def test_the_default_refuses_rather_than_pretending():
    """An unteachable backend must say so, not report a hollow success."""

    class _Unteachable(PrinterAdapter):
        @property
        def name(self) -> str:
            return "mystery-printer"

        @property
        def capabilities(self) -> PrinterCapabilities:
            return PrinterCapabilities()

        def get_state(self): ...
        def get_job(self): ...
        def _start_print_impl(self, file_name, **kw): ...
        def list_files(self): return []
        def upload_file(self, file_path): ...
        def delete_file(self, file_name): return True
        def cancel_print(self): ...
        def pause_print(self): ...
        def _resume_print_impl(self): ...
        def emergency_stop(self): ...
        def send_gcode(self, command): return "ok"
        def set_tool_temp(self, celsius, tool=0): return True
        def set_bed_temp(self, celsius): return True

    result = _Unteachable().clear_error()

    assert result.success is False
    assert isinstance(result, PrintResult)
    # It names the machine and the way out, because the user still has to get
    # the printer going and Kiln has just admitted it cannot help.
    assert "mystery-printer" in result.message
    assert "power-cycle" in result.message.lower()
    assert _Unteachable().capabilities.can_clear_error is False


def test_bambu_sends_the_acknowledgement_the_firmware_listens_for(monkeypatch):
    """Pinned by the payload, because this one talks to real hardware.

    ``clean_print_error`` is a print-category command like ``stop`` and
    ``pause``.  Bambu ignores commands it does not recognise, so a model that
    lacks it is left exactly as it was rather than harmed.
    """
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    sent: list[dict] = []
    monkeypatch.setattr(
        BambuAdapter, "_publish_command", lambda self, payload, **kw: sent.append(payload)
    )
    adapter = BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000000",
    )
    adapter._last_status["subtask_id"] = "12345"

    result = adapter.clear_error()

    assert result.success is True
    assert len(sent) == 1
    assert sent[0]["print"]["command"] == "clean_print_error"
    # Named, so the printer knows which job is being acknowledged.
    assert sent[0]["print"]["subtask_id"] == "12345"


def test_bambu_falls_back_to_a_local_jobs_id(monkeypatch):
    """Once the cache no longer names a job, "0" is what Bambu itself uses."""
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    sent: list[dict] = []
    monkeypatch.setattr(
        BambuAdapter, "_publish_command", lambda self, payload, **kw: sent.append(payload)
    )
    adapter = BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000000",
    )

    adapter.clear_error()

    assert sent[0]["print"]["subtask_id"] == "0"


def test_the_tool_refuses_mid_print(monkeypatch):
    """A live print is not an error to be cleared.

    Some acknowledgements reset the board, which would end a running print
    far less gracefully than ``cancel_print`` does.
    """
    from unittest.mock import MagicMock

    from kiln import server

    adapter = MagicMock()
    adapter.get_state.return_value = PrinterState(
        connected=True, state=PrinterStatus.PRINTING,
    )
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)

    result = _tool(server)()

    assert result["success"] is False
    assert result["error"]["code"] == "PRINTER_BUSY"
    adapter.clear_error.assert_not_called()


def test_the_tool_reports_the_state_it_read_back(monkeypatch):
    """Not the state it hoped for.

    Firmware takes a moment, and a tool that echoed its own request would let
    a caller believe a printer recovered when it had not.
    """
    from unittest.mock import MagicMock

    from kiln import server

    adapter = MagicMock()
    adapter.name = "bambu"
    adapter.capabilities = PrinterCapabilities(can_clear_error=True)
    adapter.get_state.side_effect = [
        PrinterState(connected=True, state=PrinterStatus.ERROR),   # before
        PrinterState(connected=True, state=PrinterStatus.ERROR),   # still stuck
    ]
    adapter.clear_error.return_value = PrintResult(success=True, message="sent")
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)

    result = _tool(server)()

    # The send succeeded; the printer did not recover. Both are reported.
    assert result["success"] is True
    assert result["cleared"] is False
    assert result["printer_state"] == "error"


def test_the_tool_declines_for_a_backend_that_cannot(monkeypatch):
    """And says where to go instead, rather than failing silently."""
    from unittest.mock import MagicMock

    from kiln import server

    adapter = MagicMock()
    adapter.name = "prusalink"
    adapter.capabilities = PrinterCapabilities(can_clear_error=False)
    adapter.get_state.return_value = PrinterState(
        connected=True, state=PrinterStatus.ERROR,
    )
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)

    result = _tool(server)()

    assert result["success"] is False
    assert result["error"]["code"] == "NOT_SUPPORTED"
    assert "power-" in result["error"]["message"]
    adapter.clear_error.assert_not_called()
