"""A print the owner said to print anyway is said at every approval and
starts only on a yes to that print itself.

The owner's word to print an abrasive filament through a nozzle that is not
wear-resistant is kept on the slice's record.  From there:

- every approval shows a ``print_anyway`` row, in one wording, read from that
  record by the file itself (``kiln.print_consent.what_the_print_is``);
- a yes that lets prints start without asking (a standing window, always
  allow, an account's window) does not start it, and the dialog asks about it
  even with a window open;
- an approval asked for before the print is sliced says it from the
  request's own words, and never names a filament the request did not.

The machine and its record are invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln import experimental_print, print_lineage, print_signoff, server
from kiln.print_consent import (
    PRINT_ROW_LABELS,
    SOURCE_ALWAYS,
    SOURCE_ELICITED,
    SOURCE_HOSTED_APPROVAL,
    SOURCE_HOSTED_DELEGATION,
    SOURCE_TERMINAL,
    SOURCE_WINDOW,
    consent_for,
    row_label,
    what_the_print_is,
)
from kiln.slicer import owner_asks
from tests.test_a_window_from_the_dialog import (  # noqa: F401 - _isolated is the fixture
    CHOICE_NEXT_TWO_HOURS,
    CHOICE_THIS_PRINT,
    _Host,
    _isolated,
    _NeverAsks,
    _obtain,
    _token_for,
)
from tests.test_print_lineage import GCODE, _cube_stl

#: The row a slice of PLA-CF through the one brass nozzle on record shows.
ROW = "PLA-CF through the brass nozzle, which it wears out quickly."


@pytest.fixture(autouse=True)
def _lineage_home(monkeypatch, tmp_path):
    """This computer's slice records, in a home of the test's own."""
    monkeypatch.setenv("HOME", str(tmp_path / "lineage_home"))
    yield
    print_lineage.flush()


def _slice(tmp_path: Path, name: str) -> str:
    model = _cube_stl(tmp_path / f"{name}.stl")
    out = tmp_path / f"{name}.gcode"
    out.write_text(GCODE, encoding="utf-8")
    print_lineage.record_slice(model, str(out), profile="invented.ini")
    print_lineage.flush()
    return str(out)


def said_anyway(tmp_path: Path, name: str = "cf", nozzles=None) -> str:
    """A sliced file whose record keeps the owner's word to print it anyway."""
    path = _slice(tmp_path, name)
    print_lineage.note_soft_nozzle(path, {
        "confirmed_by": "owner",
        "filaments": [{"filament": 1, "material": "PLA-CF"}],
        "nozzles": nozzles or [{"position": None, "material": "brass"}],
    })
    return path


# ---------------------------------------------------------------------------
# The row, from the one source
# ---------------------------------------------------------------------------


class TestTheRow:
    def test_a_slice_the_owner_said_to_print_anyway_has_the_row_first(self, tmp_path):
        path = said_anyway(tmp_path)
        rows = what_the_print_is(Path(path).name, path)
        assert rows == {"print_anyway": ROW}
        assert list(PRINT_ROW_LABELS)[0] == "print_anyway" and row_label("print_anyway") == "Print anyway"

    def test_any_other_slice_has_none(self, tmp_path):
        path = _slice(tmp_path, "plain")
        assert "print_anyway" not in what_the_print_is(Path(path).name, path)
        assert experimental_print.print_anyway_line(None) is None
        assert experimental_print.print_anyway_line(str(tmp_path / "missing.gcode")) is None

    def test_a_file_of_the_same_name_is_not_the_one_the_owner_said(self, tmp_path):
        (tmp_path / "a").mkdir()
        said_anyway(tmp_path / "a", "cf")
        other = tmp_path / "b"
        other.mkdir()
        twin = other / "cf.gcode"
        twin.write_text("G28\nG1 X1 Y1 E1\n", encoding="utf-8")
        assert experimental_print.print_anyway_line(str(twin)) is None

    def test_nozzles_by_position_and_several_filaments_are_named(self):
        said = experimental_print.print_anyway_said({
            "filaments": [{"filament": 1, "material": "PLA-CF"}, {"filament": 2, "material": "PETG-GF"}],
            "nozzles": [{"position": 2, "material": "brass"}],
        })
        assert said == "PETG-GF and PLA-CF through nozzle 2 (brass), which they wear out quickly."
        # A record that names no filament still holds the owner's word, said plainly.
        assert experimental_print.print_anyway_said({"filaments": [], "nozzles": [{"position": 1}]}) == (
            "Abrasive filament through a soft nozzle, which it wears out quickly."
        )
        assert experimental_print.print_anyway_said(None) is None

    def test_each_row_stands_for_one_owners_word(self):
        assert set(experimental_print.ROW_WORDS) <= set(PRINT_ROW_LABELS)
        for word in experimental_print.ROW_WORDS.values():
            assert word in owner_asks(soft_nozzle_confirmed=True, mixed_nozzle_experiment=True)


class TestBeforeTheSlice:
    """An approval asked for before the print is sliced: the request's words."""

    def test_the_word_alone_names_no_filament(self):
        rows = experimental_print.asked_rows(soft_nozzle_confirmed=True)
        assert rows == {"print_anyway": "Abrasive filament through a soft nozzle, which it wears out quickly."}

    def test_an_abrasive_filament_the_request_names_is_named(self):
        rows = experimental_print.asked_rows(soft_nozzle_confirmed=True, material="PLA-CF")
        assert rows["print_anyway"] == "PLA-CF through a soft nozzle, which it wears out quickly."

    @pytest.mark.parametrize("material", ["PLA", "", None, 7, "PLA-CF; rm -rf /", "x" * 41 + "-CF"])
    def test_anything_else_it_names_is_not(self, material):
        rows = experimental_print.asked_rows(soft_nozzle_confirmed=True, material=material)
        assert rows["print_anyway"].startswith("Abrasive filament through")

    def test_the_yes_to_two_sizes_is_the_experimental_row(self):
        rows = experimental_print.asked_rows(mixed_nozzle_experiment=True)
        assert set(rows) == {"experimental"} and "not proven on a printer" in rows["experimental"]

    @pytest.mark.parametrize("said", [{"soft_nozzle_confirmed": "true"}, {"mixed_nozzle_experiment": 1}, {}])
    def test_only_true_itself_is_a_word(self, said):
        assert experimental_print.asked_rows(**said) == {}


# ---------------------------------------------------------------------------
# The approval dialog, and the yes it needs
# ---------------------------------------------------------------------------


class TestTheDialog:
    def test_the_approval_says_print_anyway(self, tmp_path):
        path = said_anyway(tmp_path)
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": path, "printer_name": "garage"}, host)
        [(message, _)] = host.asked
        assert f"  Print anyway: {ROW}" in message

    def test_a_standing_window_does_not_silence_the_question(self, tmp_path):
        plain = _slice(tmp_path, "plain")
        anyway = said_anyway(tmp_path)
        _obtain("start_print", {"file_name": plain, "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        never = _NeverAsks()
        _obtain("start_print", {"file_name": plain, "printer_name": "garage"}, never)
        assert never.asked == []
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": anyway, "printer_name": "garage"}, host)
        assert len(host.asked) == 1


class TestOnlyItsOwnYesStartsIt:
    def test_the_gate_holds_it_on_a_window_and_starts_it_on_its_own_yes(self, tmp_path):
        plain = _slice(tmp_path, "plain")
        anyway = said_anyway(tmp_path)
        _obtain("start_print", {"file_name": plain, "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        granted = consent_for(file_name=anyway, printer_name="garage", aimed_at="garage")
        assert granted is not None and granted.source == SOURCE_WINDOW
        print_signoff.clear()
        held = server._preview_gate_error("start_print", anyway, _token_for(anyway), printer_name="garage")
        assert held["error"]["code"] == experimental_print.PRINT_ANYWAY_NEEDS_A_YES_CODE
        assert held["error"]["message"].startswith(f"The owner said to print this anyway: {ROW}")
        assert "a yes to that print itself" in held["error"]["message"]
        assert server._preview_gate_error("start_print", plain, _token_for(plain), printer_name="garage") is None

        def gate_inside():
            granted = consent_for(file_name=anyway, printer_name="garage", aimed_at="garage")
            assert granted.source == SOURCE_ELICITED
            return server._preview_gate_error("start_print", anyway, _token_for(anyway), printer_name="garage")

        _, verdict = _obtain(
            "start_print", {"file_name": anyway, "printer_name": "garage"}, _Host(CHOICE_THIS_PRINT),
            observe=gate_inside,
        )
        assert verdict is None

    @pytest.mark.parametrize("source", [SOURCE_ALWAYS, SOURCE_WINDOW, SOURCE_HOSTED_DELEGATION])
    def test_no_yes_to_prints_in_general_starts_it(self, tmp_path, source):
        hold = experimental_print.needs_its_own_yes(said_anyway(tmp_path), source)
        assert hold is not None and hold.code == experimental_print.PRINT_ANYWAY_NEEDS_A_YES_CODE

    @pytest.mark.parametrize("source", [SOURCE_ELICITED, SOURCE_TERMINAL, SOURCE_HOSTED_APPROVAL])
    def test_a_yes_to_this_print_starts_it(self, tmp_path, source):
        assert experimental_print.needs_its_own_yes(said_anyway(tmp_path), source) is None

    def test_any_other_print_is_not_held(self, tmp_path):
        assert experimental_print.needs_its_own_yes(_slice(tmp_path, "plain"), SOURCE_ALWAYS) is None


# ---------------------------------------------------------------------------
# A row is Kiln's own words, said from facts that check
# ---------------------------------------------------------------------------


class TestTheRowsAreKilnsOwnWords:
    @pytest.mark.parametrize("facts", [
        {"filaments": ["nothing unusual"], "nozzles": []},
        {"filaments": ["PLA"], "nozzles": []},
        {"filaments": ["PLA-CF"], "nozzles": [{"position": None, "material": "hardened_steel"}]},
        {"filaments": ["PLA-CF"], "nozzles": [{"position": 0, "material": "brass"}]},
        {"filaments": ["PLA-CF"], "nozzles": [{"position": True, "material": "brass"}]},
        {"filaments": ["PLA-CF"], "nozzles": [{"position": 1, "material": "Brass, fine to print"}]},
        {"filaments": ["PLA-CF"], "nozzles": [], "said": "Nothing unusual."},
        {"filaments": "PLA-CF", "nozzles": []},
        "PLA-CF through the brass nozzle",
    ])
    def test_print_anyway_facts_that_do_not_check_say_nothing(self, facts):
        assert experimental_print.checked_print_anyway(facts) is None
        assert experimental_print.print_anyway_row(facts) is None
        # The word still has its row: the plain one, never the facts' words.
        assert experimental_print.word_rows({"soft_nozzle_confirmed": facts}) == {
            "print_anyway": "Abrasive filament through a soft nozzle, which it wears out quickly.",
        }

    @pytest.mark.parametrize("facts", [
        {"sizes": [0.4]}, {"sizes": [0.4, 0.4]}, {"sizes": [0.4, 9.0]}, {"sizes": [0.4, 0.6001]},
        {"sizes": ["0.4", "0.6"]}, {"sizes": [0.4, True]}, {"sizes": [0.4, 0.6], "note": "safe"},
    ])
    def test_experiment_facts_that_do_not_check_say_nothing(self, facts):
        assert experimental_print.experimental_row(facts) is None

    @pytest.mark.parametrize("facts", [
        {"printed": 1, "slicer": "Any slicer at all"},
        {"printed": 4, "asked": 4},
        {"printed": 1, "slicer": "Bambu Studio", "need": "update", "floor": "2.0; nothing to see"},
        {"printed": 1, "because": "it is fine"},
    ])
    def test_colours_facts_that_do_not_check_say_nothing(self, facts):
        from kiln.maker_slicer_notes import colours_row

        assert colours_row(facts) is None

    def test_a_word_that_is_not_an_owners_word_says_no_row_at_all(self):
        assert experimental_print.word_rows({
            "soft_nozzle_confirmed": {"filaments": ["PLA-CF"], "nozzles": []}, "unchecked_bed_ok": {},
        }) == {"print_anyway": "PLA-CF through a soft nozzle, which it wears out quickly."}

    def test_each_row_has_one_wording_from_its_facts(self, tmp_path):
        from kiln.print_consent import print_rows_of, what_the_print_states

        path = said_anyway(tmp_path)
        states = what_the_print_states(Path(path).name, path)
        assert states == {"owner_words": {"soft_nozzle_confirmed": {
            "filaments": ["PLA-CF"], "nozzles": [{"position": None, "material": "brass"}],
        }}}
        assert print_rows_of(states) == what_the_print_is(Path(path).name, path) == {"print_anyway": ROW}
        assert experimental_print.experimental_row({"sizes": [0.4, 0.6]}) == (
            "This print uses nozzles of different sizes at once (0.4 mm and 0.6 mm), which Kiln has not proven "
            "on a printer. Watch the first layers; Kiln will ask how it came out."
        )


# ---------------------------------------------------------------------------
# Facts that do not check never lift the hold
# ---------------------------------------------------------------------------

#: The plain rows, said when the facts on record do not check.
PLAIN_ANYWAY = "Abrasive filament through a soft nozzle, which it wears out quickly."
PLAIN_EXPERIMENT = (
    "This print uses nozzles of different sizes at once, which Kiln has not proven on a printer. "
    "Watch the first layers; Kiln will ask how it came out."
)


class TestFactsThatDoNotCheckStillHold:
    @pytest.mark.parametrize("nozzle", [
        {"position": None, "material": None},
        {"position": None, "material": "hardened_steel"},
        {"position": None, "material": "Brass (0.4 mm)"},
        {"position": 99, "material": "brass"},
    ])
    def test_a_print_anyway_record_whose_facts_do_not_check_is_held_and_said_plainly(self, tmp_path, nozzle):
        path = said_anyway(tmp_path, nozzles=[nozzle])
        assert what_the_print_is(Path(path).name, path) == {"print_anyway": PLAIN_ANYWAY}
        for source in (SOURCE_WINDOW, SOURCE_ALWAYS, SOURCE_HOSTED_DELEGATION):
            hold = experimental_print.needs_its_own_yes(path, source)
            assert hold is not None and hold.code == experimental_print.PRINT_ANYWAY_NEEDS_A_YES_CODE
            assert PLAIN_ANYWAY in hold.message

    @pytest.mark.parametrize("sizes", ["0.4,3.5", "0.02,0.4", "0.4,0.6001", "0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0"])
    def test_a_file_of_mixed_sizes_whose_sizes_do_not_check_is_still_an_experiment(self, tmp_path, sizes):
        path = tmp_path / "odd.gcode"
        path.write_text(f"G28\nG1 X1 Y1 E1\n; CONFIG_BLOCK_START\n; nozzle_diameter = {sizes}\n; CONFIG_BLOCK_END\n")
        assert experimental_print.mixed_sizes_of_file(str(path))
        assert what_the_print_is(path.name, str(path)) == {"experimental": PLAIN_EXPERIMENT}
        hold = experimental_print.needs_its_own_yes(str(path), SOURCE_WINDOW)
        assert hold is not None and hold.code == experimental_print.NEEDS_A_YES_CODE

    def test_the_dialog_asks_and_the_window_holds_a_record_whose_facts_do_not_check(self, tmp_path):
        plain = _slice(tmp_path, "plain")
        odd = said_anyway(tmp_path, name="odd", nozzles=[{"position": None, "material": None}])
        _obtain("start_print", {"file_name": plain, "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": odd, "printer_name": "garage"}, host)
        [(message, _)] = host.asked
        assert f"  Print anyway: {PLAIN_ANYWAY}" in message
        print_signoff.clear()
        held = server._preview_gate_error("start_print", odd, _token_for(odd), printer_name="garage")
        assert held["error"]["code"] == experimental_print.PRINT_ANYWAY_NEEDS_A_YES_CODE

    def test_an_owners_word_always_has_its_row(self):
        assert experimental_print.word_rows({
            "soft_nozzle_confirmed": {"filaments": ["Nothing unusual"], "nozzles": []},
            "mixed_nozzle_experiment": "junk",
        }) == {"print_anyway": PLAIN_ANYWAY, "experimental": PLAIN_EXPERIMENT}
        assert experimental_print.word_rows({"unchecked_bed_ok": {}}) == {}
