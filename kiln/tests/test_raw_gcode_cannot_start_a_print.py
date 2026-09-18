"""Raw G-code is a door too, and it is closed.

Every start door in Kiln shows the person the print first.  ``send_gcode``
was not on the list because it is not ``start_print`` — but ``M23 part`` +
``M24`` on a serial, OctoPrint or Moonraker printer, and ``M32 "part"`` on
a Duet, start a file on the printer just the same, and the validator let
them through.  Klipper's own ``SDCARD_PRINT_FILE`` was already refused as
an unrecognised word.  Now the validator refuses the start words in every
dialect, and the door walker reads raw sends as doors.

A/B: the validator and tool tests here fail on the tree before the words
were blocked, and pass after.
"""

from __future__ import annotations

import ast
from unittest import mock

import pytest
from click.testing import CliRunner

import kiln.server as server
from kiln import gcode, print_doors
from kiln.printers.base import PrinterState, PrinterStatus

START_LINES = ["M23 part.gcode", "M24", 'M32 "0:/gcodes/part.gcode"', "SDCARD_PRINT_FILE FILENAME=part.gcode"]


@pytest.mark.parametrize("dialect", list(gcode.GCodeDialect))
@pytest.mark.parametrize("line", START_LINES)
def test_the_validator_refuses_every_print_start_word_in_every_dialect(dialect, line):
    result = gcode.validate_gcode([line], dialect=dialect)
    assert result.valid is False
    assert result.commands == []
    assert line in result.blocked_commands


def test_the_refusal_sends_the_reader_to_the_door_that_asks():
    result = gcode.validate_gcode(["M32 part.gcode"])
    assert "start_print" in " ".join(result.errors)


def test_a_resume_word_is_refused_too_because_resume_has_its_own_door():
    result = gcode.validate_gcode(["M24"])
    assert result.valid is False
    assert "resume_print" in " ".join(result.errors)


def test_the_walker_and_the_validator_agree_on_the_words():
    assert set(gcode._PRINT_START_COMMANDS) == set(print_doors.PRINT_START_WORDS)


# ---------------------------------------------------------------------------
# The two raw doors a user can reach
# ---------------------------------------------------------------------------


def _tool():
    fn = server.send_gcode
    return getattr(fn, "fn", getattr(fn, "callback", fn))


def _adapter():
    adapter = mock.MagicMock()
    adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE)
    adapter.capabilities.can_send_gcode = True
    adapter.send_gcode.return_value = True
    return adapter


@pytest.fixture(autouse=True)
def _generic_validator(monkeypatch):
    monkeypatch.setattr(server, "_resolve_printer_model_live", lambda: None)
    monkeypatch.setattr(server, "_PRINTER_MODEL", None)
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_check_auth", lambda scope: None)
    monkeypatch.setattr(server, "_audit", lambda *a, **k: None)


def test_the_send_gcode_tool_never_hands_a_start_to_the_printer(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    result = _tool()("M23 part.gcode\nM24")
    assert result["success"] is False
    adapter.send_gcode.assert_not_called()


def test_the_cli_gcode_command_never_hands_a_start_to_the_printer(monkeypatch):
    from kiln.cli.main import cli

    adapter = _adapter()
    monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: adapter)
    result = CliRunner().invoke(cli, ["gcode", 'M32 "part.gcode"', "--json"])
    assert result.exit_code != 0
    assert "blocked" in result.output.lower()
    adapter.send_gcode.assert_not_called()


# ---------------------------------------------------------------------------
# The walker sees raw sends
# ---------------------------------------------------------------------------


def test_the_walker_reads_both_raw_doors_as_validated():
    raw = {d.label: d.gated_by for d in print_doors.enumerate_print_doors() if d.kind == "raw"}
    assert raw.get("server.py::send_gcode") in print_doors.RAW_GATE_HELPERS
    assert raw.get("cli/main.py::gcode") in print_doors.RAW_GATE_HELPERS


def _call(src: str) -> ast.Call:
    return ast.parse(src).body[0].value


def test_a_fixed_send_is_vouched_for_only_when_its_words_are_visibly_not_a_start():
    ok = print_doors._fixed_commands_are_not_a_start
    assert ok(_call('adapter.send_gcode(["G28"])')) is True
    assert ok(_call('adapter.send_gcode([f"M141 S{t}"])')) is True
    assert ok(_call('adapter.send_gcode(["G28", "M23 part.gcode"])')) is False
    assert ok(_call('adapter.send_gcode(["M32 x"])')) is False
    assert ok(_call("adapter.send_gcode(cmds)")) is False
    assert ok(_call('adapter.send_gcode([f"{word} S0"])')) is False
    assert ok(_call("adapter.send_gcode([])")) is False
