"""Every command word on a line is judged, not only the first.

A firmware may run a second command written on the same line.  A blocked
command, a command this printer's rules refuse, or a heater target over its
ceiling is refused wherever on the line it is written.  A later word that is
none of those is left as the parameter it may be, text after a message
command is text, and a file written for a firmware that runs one command a
line is read that firmware's way.
"""

from __future__ import annotations

import pytest

from kiln.gcode import (
    _BLOCKED_COMMANDS,
    GCodeDialect,
    _later_commands,
    validate_gcode,
    validate_gcode_for_printer,
)

#: The ways a second command can follow a harmless first one.
SPELLINGS = [
    "G4 P1 {word}",
    "G4 P1 {word} ",
    "G4P1{word}",
    "g4 p1 {lower}",
    "N10 G4 P1 {word}*33",
    "G4 P1 {word} ; a comment",
    "G90 G4 P1 {word}",
    "G4 P1\t{word}",
]


def _both(line: str, printer: str = "bambu_a1"):
    return validate_gcode(line), validate_gcode_for_printer(line, printer)


@pytest.mark.parametrize("word", sorted(_BLOCKED_COMMANDS))
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_blocked_command_is_refused_wherever_on_the_line_it_is_written(word, spelling):
    line = spelling.format(word=word, lower=word.lower())
    for result in _both(line):
        assert result.valid is False and result.commands == [], line
        assert result.errors and _BLOCKED_COMMANDS[word] in result.errors[0], line
        assert result.blocked_commands, line


@pytest.mark.parametrize("line, heater", [
    ("G28 M104 S400", "hotend"),
    ("G4 P1 M109 S999", "hotend"),
    ("G4 P1 M104 S200 S400", "hotend"),
    ("G4 P1 M140 S200", "bed"),
    ("G4 P1 M190 R200", "bed"),
    ("G4 P1 M141 S90", "chamber"),
    ("G90 G4 P1 M104 S400", "hotend"),
    ("G4 P1 M104 S-5", "negative"),
])
def test_a_heater_target_past_its_ceiling_is_refused_wherever_it_is_written(line, heater):
    for result in _both(line):
        assert result.valid is False and result.commands == [], line
        assert heater in result.errors[0], result.errors


@pytest.mark.parametrize("line", [
    "G28 M104 S200",
    "G4 P1 M140 S60",
    "G91 G1 Z5 F200 G90",
    "G90 G1 X10 Y10 F3000",
    "M104 S200 T1",
    "M204 P500 T1000",
    "M106 P1 S255",
    "G1 X10 Y10 E.04805 F3000",
    "M73 P10 R5",
])
def test_a_line_of_safe_commands_and_parameters_still_passes(line):
    for result in _both(line):
        assert result.valid is True and result.commands == [line], (line, result.errors)


@pytest.mark.parametrize("line", [
    "M117 now M24 starts",
    "M30 old M112 test.gcode",
    'M291 P"M997 is the update command" S2',
    'M98 P"M24.g"',
    'M118 P0 S"send M112 if it jams"',
    'M291 P"say ""M502"" to reset" S2',
])
def test_text_after_a_message_command_and_text_in_quotes_is_never_a_command(line):
    for result in _both(line):
        assert result.valid is True, (line, result.errors)


@pytest.mark.parametrize("line", [
    'M98 P"macro.g" M24',
    'M291 P"done" S2 M112',
    'M118 P0 S"ok" M997',
    'M587 S"net" P"pw" M502',
    'M291 P"never closed M112',
    "M118 send M112 if it jams",
    "M0 press to run M502",
    "M486 A M112",
    "M1002 gcode_claim_action M24",
])
def test_a_command_after_quoted_text_or_behind_an_open_quote_is_still_judged(line):
    """Only a display message and a file name run to the end of the line.
    Text in quotes ends at the closing quote, and a quote left open hides
    nothing."""
    for result in _both(line):
        assert result.valid is False and result.commands == [], line


def test_a_parameter_word_that_looks_like_a_command_is_left_alone():
    """A maker's own line carries a note written with the letter M."""
    line = "M1006 A0 B10 L100 C37 D10 M60 E37 F10 N60"
    for result in _both(line):
        assert result.valid is True and result.commands == [line], result.errors


@pytest.mark.parametrize("dialect", [GCodeDialect.MARLIN, GCodeDialect.KLIPPER, GCodeDialect.BAMBU])
def test_a_file_for_a_firmware_that_runs_one_command_a_line_is_read_its_way(dialect):
    assert validate_gcode("G4 P1 M24", dialect=dialect).valid is True
    assert validate_gcode("M24", dialect=dialect).valid is False


def test_a_firmware_kiln_does_not_know_is_held_to_the_stricter_reading():
    assert validate_gcode("G4 P1 M24", dialect=GCodeDialect.GENERIC).valid is False


def test_the_refusal_says_which_command_and_shows_the_whole_line():
    result = validate_gcode("G4 P1 M24")
    assert "M24" in result.errors[0] and "'G4 P1 M24'" in result.errors[0]
    assert result.blocked_commands == ["G4 P1 M24"]


def test_one_bad_line_does_not_take_the_good_lines_with_it():
    result = validate_gcode(["G28", "G4 P1 M24", "G1 X10 Y10 F3000"])
    assert result.valid is False
    assert result.commands == ["G28", "G1 X10 Y10 F3000"] and result.blocked_commands == ["G4 P1 M24"]


@pytest.mark.parametrize("line, later", [
    ("G4 P1 M24", ["M24"]),
    ("G91 G1 Z5 F200 G90", ["G1 Z5 F200", "G90"]),
    ("G4P1M24G28", ["M24", "G28"]),
    ("G1 X10 Y10", []),
    ("M117 M24", []),
    ('G4 P1 "M24"', []),
    ('M98 P"a.g" M24', ["M24"]),
    ('G4 P1 "M24', ["M24"]),
])
def test_the_later_commands_on_a_line_are_split_at_each_command_word(line, later):
    from kiln.gcode import _parse_command_word

    assert _later_commands(_parse_command_word(line), line) == later


def test_this_printers_own_refusals_hold_for_a_later_command_too():
    """A command one firmware must not be sent is refused as a second
    command on the line, in that printer's own words."""
    from kiln import firmware_command_rules

    tried = 0
    for printer, rules in firmware_command_rules._table().items():
        for rule in rules.refused:
            line = f"{rule.command} {rule.parameter}" if rule.parameter else rule.command
            alone = validate_gcode_for_printer(line, printer)
            later = validate_gcode_for_printer(f"G4 P1 {line}", printer)
            assert alone.valid is False and later.valid is False, (printer, line)
            assert alone.errors[0] in later.errors[0], (printer, line)
            tried += 1
    assert tried, "no printer's rules refuse any command, so nothing was tried"


# ---------------------------------------------------------------------------
# The check on commands sent while a print runs reads a line the same way
# ---------------------------------------------------------------------------


@pytest.fixture
def watched():
    """A running check with a printer's own safety rules loaded."""
    from kiln.gcode_interceptor import GcodeInterceptor

    interceptor = GcodeInterceptor()
    session = interceptor.create_session("ender3")
    return lambda line: interceptor.intercept(session.session_id, line).action.value


@pytest.mark.parametrize("line", [
    "G4 P1 M112", "G4 P1 M997", "G4 P1 M502", "G4P1M500", "g4 p1 m112", "N10 G4 P1 M112*33",
    "G90 G4 P1 M552 S1", "G28 M104 S400", "G4 P1 M140 S200",
    'M98 P"macro.g" M112', 'M291 P"never closed M997',
])
def test_a_running_print_refuses_a_blocked_or_overheating_command_written_later_on_a_line(watched, line):
    assert watched(line) == "block", line


@pytest.mark.parametrize("line", [
    "G28 M104 S200", "G91 G1 Z5 F200 G90", "M104 S200 T1", "M117 now M112 would stop it",
    'M291 P"M997 updates the firmware" S2', "M1006 A0 B10 L100 C37 D10 M60 E37 F10 N60",
    "M1002 gcode_claim_action : 2", "M1002 judge_flag g29_before_print_flag",
])
def test_a_running_print_still_takes_safe_lines(watched, line):
    assert watched(line) != "block", line


def test_a_running_print_holds_this_printers_own_refusals_for_a_later_command():
    from kiln import firmware_command_rules
    from kiln.gcode_interceptor import GcodeInterceptor

    tried = 0
    for printer, rules in firmware_command_rules._table().items():
        interceptor = GcodeInterceptor()
        session = interceptor.create_session(printer)
        for rule in rules.refused:
            line = f"{rule.command} {rule.parameter}" if rule.parameter else rule.command
            assert interceptor.intercept(session.session_id, line).action.value == "block", (printer, line)
            assert interceptor.intercept(session.session_id, f"G4 P1 {line}").action.value == "block", (printer, line)
            tried += 1
    assert tried
