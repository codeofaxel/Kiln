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
# A file run by its path is a start too, unless it is the printer's own macro
# ---------------------------------------------------------------------------

#: A print file run as a macro, in the spellings RepRapFirmware reads, and
#: the ways a print file could be made into a macro first.
RUNS_A_PRINT_FILE = [
    'M98 P"0:/gcodes/part.gcode"',
    'M98 P"/gcodes/part.gcode"',
    "M98 P/gcodes/part.gcode",
    'M98 P"../gcodes/part.gcode"',
    'M98 P"/macros/../gcodes/part.gcode"',
    'm98 p"0:/GCODES/part.gcode"',
    'N10 M98 P"0:/gcodes/part.gcode"*12',
    'M98 P"1:/macros/part.g"',
    'M98 P"0:gcodes/part.gcode"',
    'M98 P{"0:/gcodes/" ^ "part.gcode"}',
    'M98 P"/macros/purge.g" P"/gcodes/part.gcode"',
    'M98 P"0:/gcodes/part.gcode',
    'M471 S"0:/gcodes/part.gcode" T"0:/macros/part.g"',
    'M471 S"/gcodes/part.gcode" T"/sys/part.g" D1',
    'M471 S"/gcodes" T"/sys" D1',
    'M471 S"/gcodes/part.gcode" T"part.g"',
    'M505 P"0:/gcodes"',
    # Read the way the firmware reads parameters: a letter starts one with no
    # space before it, in either case, and a line it cannot read one way only
    # is refused.
    'M98P"/gcodes/part.gcode"',
    'M98 R1P"/gcodes/part.gcode"',
    'M98 P"/macros/purge.g"P"/gcodes/part.gcode"',
    'M98 P"/macros/purge.g" p"/gcodes/part.gcode"',
    'M98 P"/macros/purge.g" S{1}',
    "M98 Pconfig.g",
    'M98 P""',
    "M98",
    "M98 R1",
    'M471 S"/gcodes/part.gcode"',
    "M471 S\"/gcodes/part.gcode\" T/gcodes/b.gcode",
    'M471 S"/gcodes/part.gcode"T"/macros/part.g"',
    # A word with a subcode is the word.
    'M98.1 P"/gcodes/part.gcode"',
    "M24.1",
    'M32.5 "part.gcode"',
]

#: The files the firmware keeps to start an interrupted print again, and the
#: words that run them.
RESTARTS_AN_INTERRUPTED_PRINT = [
    'M98 P"resurrect.g"',
    'M98 P"0:/sys/resurrect.g"',
    'M98 P"/SYS/Resurrect.g"',
    'M98 P"/macros/../sys/./resurrect.g"',
    'M98 P"resurrect-prologue.g"',
    "M916",
    "M1000",
    "M1000 S",
]

#: The printer's own macros, which a person wrote and keeps, still run, and
#: a print file renamed among the print files is not a macro.
THE_PRINTERS_OWN_MACROS = [
    'M98 P"homeall.g"',
    'M98 P"0:/sys/homeall.g"',
    'M98 P"/macros/Load Filament (PLA).g"',
    'M98 P"0:/macros/purge.g"',
    'M98 P"/MACROS/Purge.g"',
    'M98P"/macros/purge.g"',
    'M98 R1 P"/macros/purge.g"',
    'M98 P"/macros/purge.g" R1',
    'm98 p"0:/macros/purge.g"',
    'M98 P"/macros/say ""hi"".g"',
    'M471 S"0:/gcodes/a.gcode" T"0:/gcodes/b.gcode"',
    'M471 S"/gcodes/a.gcode"T"/gcodes/b.gcode"',
]


@pytest.mark.parametrize("dialect", list(gcode.GCodeDialect))
@pytest.mark.parametrize("line", RESTARTS_AN_INTERRUPTED_PRINT)
def test_an_interrupted_print_is_not_restarted_by_a_raw_command(dialect, line):
    result = gcode.validate_gcode([line], dialect=dialect)
    assert result.valid is False and result.commands == [], (line, result.errors)
    assert "resume_print" in result.errors[0], result.errors


@pytest.mark.parametrize(
    "lines, dialect",
    [
        (["G28", "SDCARD_PRINT_FILE FILENAME=other.gcode"], gcode.GCodeDialect.KLIPPER),
        (["G28", 'M98 P"0:/gcodes/other.gcode"'], gcode.GCodeDialect.GENERIC),
        (["G28", "M23 other.gcode", "M24"], gcode.GCodeDialect.MARLIN),
    ],
    ids=["klipper", "reprapfirmware", "marlin"],
)
def test_a_print_file_that_starts_another_file_is_refused(tmp_path, lines, dialect):
    path = tmp_path / "part.gcode"
    path.write_text("\n".join(lines) + "\n")
    assert gcode.scan_gcode_file(str(path), dialect=dialect, macros=True).valid is False


def test_a_print_file_calling_its_own_macros_still_passes(tmp_path):
    path = tmp_path / "part.gcode"
    path.write_text('PRINT_START EXTRUDER=200 BED=60\nM98 P"/macros/purge.g"\nG1 X10 Y10 F3000\n')
    result = gcode.scan_gcode_file(str(path), dialect=gcode.GCodeDialect.KLIPPER, macros=True)
    assert result.valid is True, result.errors


@pytest.mark.parametrize("dialect", list(gcode.GCodeDialect))
@pytest.mark.parametrize("line", RUNS_A_PRINT_FILE)
def test_a_print_file_run_by_its_path_is_refused_in_every_dialect(dialect, line):
    result = gcode.validate_gcode([line], dialect=dialect)
    assert result.valid is False, (line, result.errors)
    assert result.commands == [] and len(result.blocked_commands) == 1


@pytest.mark.parametrize("line", RUNS_A_PRINT_FILE)
def test_a_print_file_run_by_its_path_is_refused_against_a_printers_own_limits(line):
    assert gcode.validate_commands([line], "ender3").valid is False, line


def test_a_print_file_run_as_a_second_command_on_the_line_is_refused():
    result = gcode.validate_gcode(['G4 P1 M98 P"0:/gcodes/part.gcode"'])
    assert result.valid is False
    assert "M98" in result.errors[0]


#: A ';' inside a closed quote is text to a firmware that reads quotes, so it
#: hides nothing written after the quote.
BEHIND_A_QUOTED_SEMICOLON = [
    'M117 "a;" M32 "0:/gcodes/part.gcode"',
    'M117 "a;" M98 P"0:/gcodes/part.gcode"',
    'M117 "a;" M24',
    'M118 S"a;" M32 "0:/gcodes/part.gcode"',
    'M291 P"a;" S1 M32 "0:/gcodes/part.gcode"',
]


@pytest.mark.parametrize("line", BEHIND_A_QUOTED_SEMICOLON)
def test_a_start_behind_a_quoted_semicolon_is_refused(line):
    for result in (gcode.validate_gcode([line]), gcode.validate_commands([line], "ender3")):
        assert result.valid is False and result.commands == [], (line, result.errors)


@pytest.mark.parametrize("line", ['M117 "a;b', 'M118 S"done; M32 0:/gcodes/part.gcode'])
def test_a_quote_left_open_at_a_semicolon_is_refused(line):
    for result in (gcode.validate_gcode([line]), gcode.validate_commands([line], "ender3")):
        assert result.valid is False and result.blocked_commands == [line], (line, result.errors)
        assert "quote is still open" in result.errors[0]


@pytest.mark.parametrize(
    "line, read_as",
    [('M117 "a;b"', 'M117 "a;b"'), ('M117 "say ""hi;"""', 'M117 "say ""hi;"""'), ('G1 X10 ; to the "edge', "G1 X10")],
)
def test_a_semicolon_inside_a_closed_quote_is_text(line, read_as):
    result = gcode.validate_gcode([line])
    assert result.valid is True and result.commands == [read_as], result.errors


@pytest.mark.parametrize("line", BEHIND_A_QUOTED_SEMICOLON[:2])
def test_a_print_file_hiding_a_start_behind_a_quoted_semicolon_is_refused(tmp_path, line):
    path = tmp_path / "part.gcode"
    path.write_text(f"G28\nG1 X10 Y10 Z0.3 F3000\n{line}\n")
    assert gcode.scan_gcode_file(str(path)).valid is False


def test_the_interceptor_blocks_a_quote_left_open_at_a_semicolon():
    from kiln.gcode_interceptor import GcodeInterceptor, InterceptionAction

    interceptor = GcodeInterceptor()
    session = interceptor.create_session("workshop")
    assert interceptor.intercept(session.session_id, 'M117 "a;b').action == InterceptionAction.BLOCK
    assert interceptor.intercept(session.session_id, 'M117 "a;b"').action == InterceptionAction.ALLOW


#: Writing a file into the folders whose files run as macros, however the
#: place is spelled.
WRITES_INTO_A_MACRO_FOLDER = [
    'M28 "0:/macros/job.g"',
    "M28 0:/macros/job.g",
    "M28 /MACROS/Job.g",
    'M28 "0:/gcodes/../macros/job.g"',
    'M28 "/sys/daemon.g"',
    'M559 P"0:/sys/daemon.g"',
    'M559 P"daemon.g"',
    "M559",
    'M560 P"0:/macros/job.g"',
    'M560 P"/sys/config.g"',
    'G4 S0 M28 "0:/macros/job.g"',
    # The card reads a name with a trailing dot or space, or a short alias,
    # as the folder it stands for.
    'M28 "0:/macros./job.g"',
    'M28 "0:/sys /daemon.g"',
    'M28 "0:/macros. ./job.g"',
    'M28 "0:/gcodes/.. /macros/job.g"',
    'M28 "0:/FILAME~1/PLA/load.g"',
    # The filament words run the files in the filaments folder.
    'M28 "0:/filaments/PLA/load.g"',
    'M559 P"/filaments/PLA/unload.g"',
    'M560 P"/Filaments/PLA/config.g"',
]
#: Writing a print file among the print files, or a page among the pages.
WRITES_ELSEWHERE = [
    'M28 "0:/gcodes/part.gcode"', "M28 part.gcode", 'M28 "0:/gcodes/part.v2.gcode"',
    'M560 P"0:/www/index.html"', 'M560 P"reprap.htm"',
]
#: Moving a file into a folder whose files run as macros, however spelled.
MOVES_INTO_A_MACRO_FOLDER = [
    'M471 S"/gcodes/part.gcode" T"/filaments/PLA/load.g"',
    'M471 S"/gcodes/part.gcode" T"/macros./part.g"',
    'M471 S"/gcodes/part.gcode" T"/sys /part.g"',
]


@pytest.mark.parametrize("line", MOVES_INTO_A_MACRO_FOLDER)
def test_moving_a_file_into_a_macro_folder_is_refused(line):
    for result in (gcode.validate_gcode([line]), gcode.validate_commands([line], "ender3")):
        assert result.valid is False and result.commands == [], (line, result.errors)


def test_the_filament_folders_own_files_still_run():
    line = 'M98 P"/filaments/PLA/load.g"'
    assert gcode.validate_gcode([line]).valid is True


#: Filament words naming a filament that reaches outside the filaments
#: folder, or that the card reads as another name.
FILAMENT_WORDS_REACHING_OUT = [
    'M701 S"../gcodes/part"',
    'M701 S"..\\gcodes\\part"',
    'M701 S".."',
    'M701 S"PLA."',
    'M701 S"PLA "',
    'M701 S" PLA"',
    'M701 S"FILAME~1"',
    'M701 S"{global.part}"',
    'M701 S"0:PLA"',
    "M701 SPLA",
    'M701 S"PLA" S"../gcodes/part"',
    'M702 S"../gcodes/part"',
    'M703 S"../gcodes/part"',
    'G4 S0 M701 S"../gcodes/part"',
]
#: Making a folder where the macro words read their files.
MAKES_A_MACRO_FOLDER = ['M470 P"/filaments/part"', 'M470 P"0:/macros/new"', 'M470 P"/sys./x"', 'M470 P"part"', "M470"]


@pytest.mark.parametrize("line", FILAMENT_WORDS_REACHING_OUT + MAKES_A_MACRO_FOLDER)
def test_a_filament_or_folder_word_that_reaches_a_file_unseen_is_refused(line):
    for result in (gcode.validate_gcode([line]), gcode.validate_commands([line], "ender3")):
        assert result.valid is False and result.commands == [], (line, result.errors)


@pytest.mark.parametrize(
    "line", ['M701 S"PLA"', 'M701 S"PETG-CF 0.6"', "M702", "M703", "M701 T0 L50", 'M470 P"/gcodes/parts"'],
)
def test_a_plain_filament_name_and_a_folder_among_the_prints_still_work(line):
    assert gcode.validate_gcode([line]).valid is True, gcode.validate_gcode([line]).errors


@pytest.mark.parametrize("line", WRITES_INTO_A_MACRO_FOLDER)
def test_writing_a_file_into_a_macro_folder_is_refused(line):
    for result in (gcode.validate_gcode([line]), gcode.validate_commands([line], "ender3")):
        assert result.valid is False and result.commands == [], (line, result.errors)
        assert "/macros" in result.errors[0], result.errors


@pytest.mark.parametrize("line", WRITES_ELSEWHERE)
def test_writing_a_file_elsewhere_is_still_a_write(line):
    assert gcode.validate_gcode([line]).valid is True, line


@pytest.mark.parametrize("line", THE_PRINTERS_OWN_MACROS)
def test_the_printers_own_macros_still_run(line):
    for result in (gcode.validate_gcode([line]), gcode.validate_commands([line], "ender3")):
        assert result.valid is True and result.commands == [line], (line, result.errors)


def test_the_refusal_names_the_folders_that_still_run():
    result = gcode.validate_gcode(['M98 P"0:/gcodes/part.gcode"'])
    assert "/sys" in result.errors[0] and "/macros" in result.errors[0] and "start_print" in result.errors[0]


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


def test_the_send_gcode_tool_never_runs_a_print_file_as_a_macro(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    result = _tool()('M98 P"0:/gcodes/part.gcode"')
    assert result["success"] is False
    adapter.send_gcode.assert_not_called()


def test_the_send_gcode_tool_never_writes_a_file_into_a_macro_folder(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    monkeypatch.setattr(server, "_check_confirmation", lambda *_a, **_k: None)
    result = _tool()('M28 "0:/macros/job.g"\nG1 X10\nM29')
    assert result["success"] is False, result
    adapter.send_gcode.assert_not_called()


@pytest.mark.parametrize("line", [BEHIND_A_QUOTED_SEMICOLON[0], BEHIND_A_QUOTED_SEMICOLON[1], 'M117 "a;b'])
def test_the_send_gcode_tool_never_sends_a_start_behind_a_quoted_semicolon(monkeypatch, line):
    adapter = _adapter()
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    monkeypatch.setattr(server, "_check_confirmation", lambda *_a, **_k: None)
    result = _tool()(line)
    assert result["success"] is False, result
    adapter.send_gcode.assert_not_called()


def test_the_send_gcode_tool_still_runs_the_printers_own_macro(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    monkeypatch.setattr(server, "_check_confirmation", lambda *_a, **_k: None)
    result = _tool()('M98 P"/macros/purge.g"')
    assert result["success"] is True, result
    adapter.send_gcode.assert_called_once_with(['M98 P"/macros/purge.g"'])


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
    assert ok(_call("""adapter.send_gcode(['M98 P"0:/gcodes/part.gcode"'])""")) is False
    assert ok(_call("""adapter.send_gcode([f'M98 P"{path}"'])""")) is False
    assert ok(_call("""adapter.send_gcode(['M98 P"homeall.g"'])""")) is True
