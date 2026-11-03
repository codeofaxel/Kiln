"""Every form a heat command can take, judged by the one G-code scanner.

The scanner behind ``validate_gcode``, ``validate_gcode_for_printer`` and
``scan_gcode_file`` (the gate the print pipelines and ``upload_file`` run)
judges a heater's target.  A lap on 2026-11-02 found it judging only the
FIRST ``S`` word of M104/M109/M140/M190 and nothing else: ``M104 S200 S330``
passed (a firmware takes the last repeated word), ``M109 R330`` and
``M190 R130`` passed (``R`` is a target too), and ``M191`` (chamber, wait),
``G10 P0 S330`` and ``M568 P0 S330`` (RepRapFirmware / Klipper tool
temperatures) were never looked at -- while PrusaSlicer 2.9 writes only
``M191 S200`` for ``chamber_minimal_temperature=200`` with
``chamber_temperature=0``, and a start-block override reaches every one of
these doors as written.  The suite's temperature cases were all one ``S``
word on one of the four commands.

# --- defects a lap found (2026-11-02, lap 6; each fails at c422d968) ---------
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.gcode import scan_gcode_file, validate_gcode, validate_gcode_for_printer

#: A line that must be refused, and the word that exceeds the ceiling.  Each
#: value is over the generic ceiling AND the bambu_a1's (hotend 300, bed
#: 100 against the generic 130, chamber held to the generic 60).
REFUSED: list[tuple[str, str]] = [
    ("M104 S200 S330", "M104 S330"),
    ("M109 S200 S330", "M109 S330"),
    ("M109 R330", "M109 R330"),
    ("M104 S200 B330 F1", "M104 B330"),
    ("M140 S60 S140", "M140 S140"),
    ("M190 R140", "M190 R140"),
    ("M141 S200", "M141 S200"),
    ("M191 S200", "M191 S200"),
    ("M191 R200", "M191 R200"),
    ("G10 P0 S330", "G10 S330"),
    ("G10 P0 S200 R330", "G10 R330"),
    ("M568 P0 S330", "M568 S330"),
    ("M568 P0 S200 R330 A2", "M568 R330"),
    ("m104 s330", "M104 S330"),
]

#: Lines that are not heat targets over a ceiling and must pass: a Marlin
#: retract flag on G10, a tool state on M568, a tool index, a target stated
#: twice at one value.
PASSED: list[str] = ["G10 S1", "G10", "M568 P0 A2", "M104 T0 S215", "M190 S60 R60", "M104 S200 S200", "M141 S55"]

#: A heat command with a target word carrying no number is refused rather
#: than read as nothing.
UNREADABLE: list[str] = ["M109 S", "M104 S200 R", "M191 S"]


@pytest.mark.parametrize("line, word", REFUSED, ids=[r[0] for r in REFUSED])
def test_every_target_word_on_every_heat_command_is_judged_by_both_validators(line: str, word: str) -> None:
    generic = validate_gcode(line)
    assert generic.valid is False and generic.blocked_commands == [line], generic
    assert any(e.startswith(f"{word} exceeds") for e in generic.errors), generic.errors
    printer = validate_gcode_for_printer(line, "bambu_a1")
    assert printer.valid is False, printer
    assert any(e.startswith(f"{word} exceeds") for e in printer.errors), printer.errors


@pytest.mark.parametrize("line, word", REFUSED, ids=[r[0] for r in REFUSED])
def test_the_file_scanner_the_print_doors_run_refuses_each_form(tmp_path: Path, line: str, word: str) -> None:
    path = tmp_path / "part.gcode"
    path.write_text(f"G28\nM140 S60\nM104 S215\n{line}\nG1 X10 Y10 Z0.2 F1200 E1\n")
    for printer_id in (None, "bambu_a1", "ender3"):
        scan = scan_gcode_file(str(path), printer_id=printer_id)
        assert scan.valid is False, (printer_id, scan)
        assert any(e.startswith(f"{word} exceeds") for e in scan.errors), (printer_id, scan.errors)


@pytest.mark.parametrize("line", PASSED)
def test_a_form_that_is_not_a_target_over_a_ceiling_still_passes(line: str) -> None:
    for verdict in (validate_gcode(line), validate_gcode_for_printer(line, "bambu_a1")):
        assert verdict.valid is True and verdict.blocked_commands == [], verdict
        assert not any("cold extrusion" in w for w in verdict.warnings), verdict.warnings


@pytest.mark.parametrize("line", UNREADABLE)
def test_a_target_word_with_no_number_is_refused_not_read_as_nothing(line: str) -> None:
    for verdict in (validate_gcode(line), validate_gcode_for_printer(line, "ender3")):
        assert verdict.valid is False, verdict
        assert any("no readable number" in e for e in verdict.errors), verdict.errors


def test_a_chamber_target_is_held_to_the_printers_ceiling_or_a_generic_one_said_to_be_generic() -> None:
    """A printer that states no chamber limit is held to the generic
    chamber ceiling, and the refusal says so rather than attributing the
    number to the printer."""
    verdict = validate_gcode_for_printer("M191 S61", "bambu_a1")
    assert verdict.valid is False
    assert any("generic chamber ceiling" in e and "states no chamber limit" in e for e in verdict.errors), verdict.errors
    assert validate_gcode_for_printer("M191 S60", "bambu_a1").valid is True
    assert validate_gcode("M191 S60").valid is True and validate_gcode("M191 S61").valid is False


def test_a_cold_target_is_warned_about_only_on_the_commands_a_slicer_extrudes_at() -> None:
    cold = validate_gcode("M104 S100")
    assert cold.valid is True and any("cold extrusion" in w for w in cold.warnings)
    retract = validate_gcode("G10 S1")
    assert retract.valid is True and not any("cold extrusion" in w for w in retract.warnings)
