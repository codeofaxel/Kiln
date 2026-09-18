"""The raw G-code door does not start prints.

``send_gcode`` blocked M112, EEPROM writes, network config and firmware
updates, and nothing else — so ``M23 part.gcode`` + ``M24`` on a serial,
OctoPrint or Moonraker printer, or ``M32 "part.gcode"`` on Duet, started a
print through the raw door with no preview and no sign-off.  The adapter
template never ran, because it is not ``adapter.start_print``.  Found
2026-09-19 while confirming "you see the print before it starts, from
every door" before that line went into the changelog.

Klipper's ``SDCARD_PRINT_FILE`` is an extended command the parser has
always refused as unrecognised; pinned here so that refusal is a promise
rather than an accident.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln import print_doors
from kiln.gcode import GCodeDialect, validate_gcode, validate_gcode_for_printer
from kiln.printers.octoprint import OctoPrintAdapter


@pytest.mark.parametrize(
    "line",
    [
        "M23 part.gcode",
        "M24",
        'M32 "0:/gcodes/part.gcode"',
        "SDCARD_PRINT_FILE FILENAME=part.gcode",
        "m23 part.gcode",
        "N10 M24",
    ],
)
@pytest.mark.parametrize("dialect", list(GCodeDialect))
def test_a_print_start_is_refused_in_every_dialect(line, dialect):
    r = validate_gcode(line, dialect=dialect)
    assert r.valid is False, (line, dialect)
    assert r.blocked_commands, (line, dialect)


def test_the_refusal_names_the_door_to_use_instead():
    r = validate_gcode("M23 part.gcode")
    assert "start_print" in r.errors[0]
    r = validate_gcode("M24")
    assert "start_print" in r.errors[0] or "resume_print" in r.errors[0]


def test_printer_profiles_refuse_it_too():
    r = validate_gcode_for_printer(["M23 part.gcode", "M24"], "ender3")
    assert r.valid is False
    assert len(r.blocked_commands) == 2


def test_one_start_line_invalidates_the_batch():
    r = validate_gcode("G28\nM23 part.gcode\nM24")
    assert r.valid is False
    assert r.blocked_commands == ["M23 part.gcode", "M24"]


@patch("kiln.server._get_adapter")
def test_send_gcode_never_hands_a_start_to_the_printer(mock_get_adapter, monkeypatch):
    from kiln import server

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    adapter = MagicMock(spec=OctoPrintAdapter)
    mock_get_adapter.return_value = adapter
    result = server.send_gcode("M23 part.gcode\nM24")
    assert result["success"] is False, result
    adapter.send_gcode.assert_not_called()
    assert "start_print" in str(result)


def test_the_doctor_line_vouches_for_the_raw_door():
    """The walker cannot see this door — it is a table, not a call — so
    the doctor reads the table and says so, or says it is open."""
    assert print_doors.raw_starts_refused() == []
    ok, line = print_doors.summarize()
    assert ok is True
    assert "raw G-code starts refused" in line


def test_the_doctor_line_names_a_reopened_raw_start(monkeypatch):
    from kiln import gcode

    table = {k: v for k, v in gcode._BLOCKED_COMMANDS.items() if k != "M24"}
    monkeypatch.setattr(gcode, "_BLOCKED_COMMANDS", table)
    assert print_doors.raw_starts_refused() == ["M24"]
    ok, line = print_doors.summarize()
    assert ok is False
    assert "RAW G-CODE STARTS OPEN: M24" in line
