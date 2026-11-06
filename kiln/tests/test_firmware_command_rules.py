"""A printer's own firmware rules reach every door that checks G-code.

Some firmware has a command that hangs it, or accepts a pause word and does
nothing.  Those are facts about one model, kept in
``data/firmware_command_rules.json`` and applied by the one line validator
that a typed command and a print file both pass through.

Found on 2026-11-05: the Anycubic Kobra 2 Neo's firmware stops responding on
``M2003 P`` and has no handler for the ``M601`` its maker's slicer writes for
a pause, and both passed Kiln's check.  Nothing had a place to state a
per-printer command fact, so no test could ask for one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import kiln.firmware_command_rules as fcr
from kiln.gcode import scan_gcode_file, validate_gcode_for_printer

NEO = "anycubic_kobra_2_neo"
_DATA = Path(fcr.__file__).resolve().parent / "data"


def _scan(path: Path, printer_id: str | None):
    """A print file judged the way an upload or a start judges it."""
    return scan_gcode_file(str(path), printer_id=printer_id, generic_when_unread=True, macros=True, keep_commands=False)


class TestTheTable:
    def test_every_row_names_a_catalogue_printer(self):
        roster = {k for k in json.loads((_DATA / "printer_intelligence.json").read_text()) if not k.startswith("_")}
        rows = json.loads((_DATA / "firmware_command_rules.json").read_text())["printers"]
        assert rows and set(rows) <= roster, sorted(set(rows) - roster)

    def test_every_rule_names_a_command_and_says_why(self):
        rows = json.loads((_DATA / "firmware_command_rules.json").read_text())["printers"]
        for key, row in rows.items():
            assert set(row) <= {"refused", "ignored"}, key
            for rule in [*row.get("refused", []), *row.get("ignored", [])]:
                assert re.fullmatch(r"[GMT]\d+(\.\d+)?", rule["command"]), (key, rule)
                assert rule["why"] and not rule["why"].endswith("."), (key, rule)
                assert re.fullmatch(r"[A-Z]", rule.get("with", "A")), (key, rule)

    def test_the_reader_holds_what_the_file_holds(self):
        rows = json.loads((_DATA / "firmware_command_rules.json").read_text())["printers"]
        for key, row in rows.items():
            rules = fcr.rules_for(key)
            assert rules is not None
            assert len(rules.refused) == len(row.get("refused", []))
            assert len(rules.ignored) == len(row.get("ignored", []))


class TestATypedCommand:
    @pytest.mark.parametrize(
        "command", ["M2003 P", "m2003 p", "M2003 P1", "N12 M2003 P*33", "M2003P", "M2003P1", "m2003p", "M2003 C1 P", "M2003\tP"]
    )
    def test_a_command_that_hangs_the_firmware_is_refused(self, command):
        result = validate_gcode_for_printer(command, NEO)
        assert not result.valid and result.blocked_commands
        assert "Kiln will not send it" in result.errors[0]

    @pytest.mark.parametrize("command", ["M2003 C", "M2003C", "M2003", "M20030 P"])
    def test_the_same_word_with_another_letter_is_not_that_command(self, command):
        assert validate_gcode_for_printer(command, NEO).valid

    def test_a_pause_word_the_firmware_ignores_is_said_and_not_blocked(self):
        result = validate_gcode_for_printer("M601", NEO)
        assert result.valid
        assert any("does not act on M601" in w and "M25" in w for w in result.warnings)

    @pytest.mark.parametrize("spelling", ["kobra 2 neo", "Anycubic Kobra 2 Neo", "kobra2 neo"])
    def test_the_printer_is_known_by_the_names_people_type(self, spelling):
        assert not validate_gcode_for_printer("M2003 P", spelling).valid

    @pytest.mark.parametrize("other", ["anycubic_kobra_x", "prusa_mk4", "ender3", "bambu_a1"])
    def test_a_rule_is_never_carried_to_another_printer(self, other):
        assert fcr.rules_for(other) is None
        result = validate_gcode_for_printer("M601", other)
        assert not any("does not act on M601" in w for w in result.warnings)


class TestAPrintFile:
    def test_a_file_with_the_hanging_command_is_refused(self, tmp_path):
        file = tmp_path / "p.gcode"
        file.write_text("G28\nG1 X10 Y10 F3000\nM2003 P\nG1 X20\n")
        result = _scan(file, NEO)
        assert not result.valid and "Kiln will not send it" in result.errors[0]
        assert _scan(file, "ender3").valid

    def test_a_file_that_pauses_with_the_ignored_word_is_told_once(self, tmp_path):
        file = tmp_path / "p.gcode"
        file.write_text("G28\nM601\nG1 X20\nM601\nG1 X30\n")
        result = _scan(file, NEO)
        assert result.valid
        assert len([w for w in result.warnings if "does not act on M601" in w]) == 1
        assert not any("M601" in w for w in _scan(file, "prusa_mk4").warnings)
        assert not any("M601" in w for w in _scan(file, None).warnings)


class TestTheRulesHoldUnderAPrintersOwnLimitsToo:
    """The rules apply whether Kiln holds the printer's own limits or is
    working from the generic ones: both paths of the validator read them."""

    def test_a_printer_whose_record_is_in_hand(self, monkeypatch, tmp_path):
        held = fcr.CommandRules(
            "kilntest_enclosed",
            refused=(fcr._Rule("M9999", "Q", "M9999 Q is a test of the refusal"),),
            ignored=(fcr._Rule("M9998", None, "M9998 is a test of the warning"),),
        )
        monkeypatch.setattr(fcr, "_table", lambda: {"kilntest_enclosed": held})
        assert not validate_gcode_for_printer("M9999 Q", "kilntest_enclosed").valid
        assert validate_gcode_for_printer("M9999 R", "kilntest_enclosed").valid
        warned = validate_gcode_for_printer("M9998", "kilntest_enclosed")
        assert warned.valid and any("test of the warning" in w for w in warned.warnings)
        file = tmp_path / "p.gcode"
        file.write_text("G28\nM9999 Q\n")
        assert not _scan(file, "kilntest_enclosed").valid

    def test_an_unreadable_table_is_no_rule_and_never_a_refusal(self, monkeypatch):
        def broken():
            raise OSError("no file")

        monkeypatch.setattr(fcr, "_table", broken)
        assert validate_gcode_for_printer("M2003 P", NEO).valid
