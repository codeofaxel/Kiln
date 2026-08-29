"""send_gcode must refuse to home Z while a job is printing or paused.

A bare ``G28`` (or any ``G28`` naming Z) descends the nozzle toward the
bed at the machine's homing XY.  Mid-print, the bed carries a partial
print — the descent is a collision whenever the homing point coincides
with the part.  The gate queries the LIVE printer state and blocks Z
homing only in ``printing``/``paused``; idle machines, unreachable
machines (fail open), and the explicit env bypass all pass through.
"""

from __future__ import annotations

from unittest import mock

import pytest

import kiln.server as server
from kiln.printers.base import PrinterState, PrinterStatus


def _tool():
    fn = server.send_gcode
    return getattr(fn, "fn", getattr(fn, "callback", fn))


def _adapter(state: PrinterStatus | Exception):
    adapter = mock.MagicMock()
    if isinstance(state, Exception):
        adapter.get_state.side_effect = state
    else:
        adapter.get_state.return_value = PrinterState(
            connected=True, state=state
        )
    adapter.capabilities.can_send_gcode = True
    adapter.send_gcode.return_value = True
    return adapter


@pytest.fixture()
def _no_model(monkeypatch):
    # Force the generic validator so these tests exercise the homing gate,
    # not a per-model bounds profile — and a fresh rate limiter so the
    # per-tool send_gcode budget doesn't couple the cases to each other.
    monkeypatch.setattr(server, "_resolve_printer_model_live", lambda: None)
    monkeypatch.setattr(server, "_PRINTER_MODEL", None)
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())


@pytest.mark.usefixtures("_no_model")
class TestMidPrintZHomingGate:
    @pytest.mark.parametrize("status", [PrinterStatus.PRINTING, PrinterStatus.PAUSED])
    @pytest.mark.parametrize("command", ["G28", "G28 Z", "G28 X Z"])
    def test_z_homing_refused_mid_print(self, monkeypatch, status, command):
        adapter = _adapter(status)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        result = _tool()(command)
        assert result["success"] is False
        assert result["error"]["code"] == "GCODE_MIDPRINT_Z_HOME"
        adapter.send_gcode.assert_not_called()

    def test_safe_shape_allowed_mid_print(self, monkeypatch):
        adapter = _adapter(PrinterStatus.PAUSED)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        result = _tool()("G91\nG1 Z5 F600\nG90\nG28 X Y")
        assert result["success"] is True, result
        adapter.send_gcode.assert_called_once()

    def test_bare_g28_allowed_when_idle(self, monkeypatch):
        adapter = _adapter(PrinterStatus.IDLE)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        result = _tool()("G28")
        assert result["success"] is True, result
        adapter.send_gcode.assert_called_once()

    def test_unreachable_state_fails_open(self, monkeypatch):
        adapter = _adapter(RuntimeError("printer cannot be asked"))
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        result = _tool()("G28")
        assert result["success"] is True, result

    def test_env_bypass(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_MIDPRINT_HOMING_CHECK", "1")
        adapter = _adapter(PrinterStatus.PRINTING)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        result = _tool()("G28")
        assert result["success"] is True, result

    def test_dry_run_surfaces_the_refusal(self, monkeypatch):
        # dry_run runs the full validation pipeline — the would-block is
        # reported instead of a false "validated successfully".
        adapter = _adapter(PrinterStatus.PRINTING)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        result = _tool()("G28", dry_run=True)
        assert result["success"] is False
        assert result["error"]["code"] == "GCODE_MIDPRINT_Z_HOME"
