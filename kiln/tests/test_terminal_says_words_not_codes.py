"""A person reads a sentence; a code is for agents and scripts.

Every error the terminal shows a person is one plain sentence.  The code
stays where something reads it: ``--json`` and the agent's error envelope.
"""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

import kiln.cli.output as output
import kiln.server as srv
from kiln.cli.main import cli
from kiln.cli.output import format_error
from kiln.cli.spend_caps_commands import _surface_envelope_error
from kiln.printers import PrinterError
from kiln.printers.bed_fit import check_gcode_has_homing
from kiln.printers.print_file_scan import REFUSAL_CODE
from tests.test_3mf_temperature_scan import _plate, _Printer

SENTENCE = "Kiln doesn't know your printer's bed size yet. Tell it which printer you have."


def _flat(text: str) -> str:
    """The words a person reads, with the box border and line wrapping taken out."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", text)).split())


def test_the_terminal_shows_the_sentence_and_never_the_code() -> None:
    shown = format_error(SENTENCE, code="UNKNOWN_BED_SIZE")
    assert SENTENCE in _flat(shown)
    assert "UNKNOWN_BED_SIZE" not in shown


def test_the_plain_fallback_shows_the_sentence_and_never_the_code(monkeypatch) -> None:
    monkeypatch.setattr(output, "RICH_AVAILABLE", False)
    assert format_error(SENTENCE, code="UNKNOWN_BED_SIZE") == f"Error: {SENTENCE}"


def test_scripts_still_get_the_code() -> None:
    reply = json.loads(format_error(SENTENCE, code="UNKNOWN_BED_SIZE", json_mode=True))
    assert reply["error"] == {"code": "UNKNOWN_BED_SIZE", "message": SENTENCE}


@pytest.mark.parametrize("word", ["unauthorized", "SPEND_CAP_STORE_UNAVAILABLE", "below_floor", ""])
def test_a_bare_code_word_is_never_handed_to_a_person_as_the_sentence(word: str) -> None:
    shown = _flat(format_error(word, code="SOME_CODE"))
    assert "SOME_CODE" not in shown
    if word:
        assert word not in shown
    assert "--json" in shown  # the one way to the details, said once


def test_a_real_command_says_words_and_its_json_keeps_the_code() -> None:
    runner = CliRunner()
    said = runner.invoke(cli, ["print", "--list-objects"])
    assert said.exit_code == 1
    assert "--list-objects requires exactly one .gcode.3mf file." in _flat(said.output)
    assert "INVALID_ARGS" not in said.output
    scripted = runner.invoke(cli, ["print", "--list-objects", "--json"])
    assert json.loads(scripted.output)["error"]["code"] == "INVALID_ARGS"


def test_an_upload_refusal_is_a_sentence_and_still_carries_its_code(tmp_path) -> None:
    printer = _Printer()
    with pytest.raises(PrinterError) as refused:
        printer.upload_file(str(_plate(tmp_path, 330)))
    said = str(refused.value)
    assert "was not uploaded" in said
    assert REFUSAL_CODE not in said and "incident" not in said
    assert refused.value.code == REFUSAL_CODE

    # Built inside the except, the way every door builds them: the agent
    # envelope and the terminal's --json both name the refusal, not the door.
    try:
        printer.upload_file(str(_plate(tmp_path, 330)))
    except PrinterError as exc:
        agent = srv._error_dict(f"Upload failed: {exc}", code="UPLOAD_FAILED")
        scripted = json.loads(format_error(f"Upload failed: {exc}", code="UPLOAD_FAILED", json_mode=True))
        person = format_error(f"Upload failed: {exc}", code="UPLOAD_FAILED")
    assert agent["error"]["code"] == REFUSAL_CODE
    assert scripted["error"]["code"] == REFUSAL_CODE
    assert REFUSAL_CODE not in person and "UPLOAD_FAILED" not in person


def test_a_spend_cap_failure_says_a_sentence_not_a_server_word() -> None:
    default = "Kiln couldn't change your spend caps just now."
    unknown = _surface_envelope_error({"error": "spend_cap_store_unavailable"}, default)
    assert unknown.message == default

    floor = _surface_envelope_error(
        {"error": "below_floor", "cap": "per_order", "floor_cents": 50000}, default
    ).message
    assert "floor_cents" not in floor and "cap=" not in floor and "per_order" not in floor
    assert "$500" in floor


def test_a_missing_homing_move_is_explained_without_internal_history(tmp_path) -> None:
    gcode = tmp_path / "no_home.gcode"
    gcode.write_text("G90\nG1 X10 Y10 Z0.2 E1 F1200\nG1 X20 Y10 E2\n")
    verdict = check_gcode_has_homing(str(gcode))
    assert verdict["error_code"] == "NO_HOMING_SEQUENCE"
    assert "incident" not in verdict["error_message"]
