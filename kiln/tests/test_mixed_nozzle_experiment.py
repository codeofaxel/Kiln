"""A print with nozzles of different sizes is an experiment the person agrees to.

On an invented two-nozzle machine with invented numbers:

- a slice that would use nozzles of different sizes is refused until the
  caller passes the person's yes, and the refusal names the way to try it;
- with the yes, the settings for those sizes are asked for once (one
  printer, its sizes, the map when one was given) and laid over the presets
  as they come; the result and its message say the print is experimental;
- when the settings cannot be had the slice is refused and nothing runs:
  offline, signed out, no answer and refused each have their sentence;
- nozzles of one size never ask and never say experimental;
- the file that prints says it is an experiment: the approval shows it, a
  yes that lets prints start without asking does not start it, its record
  carries the word, and its ending asks the person how it came out;
- the account's copy of the record carries the word and the sizes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import _pro_catalogue_bridge as catalogue
from kiln import ending_question, experimental_print, outcome_courier, print_signoff, server, slicer_profiles
from kiln.persistence import KilnDB
from kiln.print_consent import SOURCE_ELICITED, SOURCE_WINDOW, consent_for
from kiln.served_answer import Miss
from kiln.slicer import NozzleMapError, slice_file
from tests.test_a_window_from_the_dialog import (  # noqa: F401 - _isolated is the fixture
    CHOICE_NEXT_TWO_HOURS,
    CHOICE_THIS_PRINT,
    _Host,
    _isolated,
    _NeverAsks,
    _obtain,
    _token_for,
)
from tests.test_slicer_multi_nozzle import (
    _WIDE,
    _fitted,
    _never_runs,
    _plate,
    _profile,
    _slice_model_tool,
    _slicer_that_writes,
    _studio,
)

_MIXED = _fitted((1, 0.4, "hardened_steel"), (2, 0.6, "hardened_steel"))
_ONE_SIZE = _fitted((1, 0.4, "hardened_steel"), (2, 0.4, "hardened_steel"))
#: What the servers answer for the two sizes: placeholders, laid over as they come.
_SERVED = {
    "kind": "fixed_pair",
    "sliced_as": 2,
    "preset_extras": {
        "machine": {"example_sizes_key": ["a", "b"]},
        "process": {"example_process_key": "served"},
        "filament": {},
    },
}


def _slice(tmp_path, run, positions, **kwargs):
    record = slicer_profiles.fitted_record(_WIDE, positions)
    with (
        patch("kiln.slicer.find_slicer", return_value=_studio()),
        patch("kiln.maker_slicer._find", return_value=None),
        patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
        patch("kiln.slicer._run_slicer_with_startup_retry", run),
    ):
        return slice_file(
            _plate(tmp_path), output_dir=str(tmp_path / "out"), profile=_profile(tmp_path, record), **kwargs,
        )


@pytest.fixture
def served(monkeypatch):
    """What Kiln's servers are asked for the sizes, and what they answer."""
    asked: list[tuple] = []
    answer: dict = {"record": _SERVED, "miss": None}

    def nozzles_for_sizes(printer_id, sizes, filament_map=None):
        asked.append((printer_id, list(sizes), filament_map))
        return (None, answer["miss"]) if answer["miss"] else (dict(answer["record"]), None)

    monkeypatch.setattr(catalogue, "nozzles_for_sizes", nozzles_for_sizes)
    return asked, answer


class TestTheSlice:
    def test_without_the_persons_yes_it_is_refused_and_nothing_is_asked(self, tmp_path, served):
        asked, _ = served
        with pytest.raises(NozzleMapError) as refused:
            _slice(tmp_path, _never_runs, _MIXED)
        assert refused.value.code == "MULTI_NOZZLE_MIXED_SIZES"
        said = str(refused.value)
        assert "Nozzle 1 is 0.4 mm and nozzle 2 is 0.6 mm" in said
        assert "not proven on a printer" in said and "Nothing was sliced" in said
        assert refused.value.parts.headline == "Two nozzle sizes at once is an experiment"
        assert slicer_profiles.MIXED_SIZES_CONFIRM in said and "mixed_nozzle_experiment=true" in said
        assert asked == []

    def test_with_the_yes_the_served_settings_are_laid_over_and_the_result_says_experimental(self, tmp_path, served):
        asked, _ = served
        seen: dict = {}
        result = _slice(
            tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen), _MIXED,
            mixed_nozzle_experiment=True, filament_map=[1, 2],
        )
        assert asked == [(None, [0.4, 0.6], [1, 2])]
        assert seen["machine"]["example_sizes_key"] == ["a", "b"]
        assert seen["process"]["example_process_key"] == "served"
        # The record's own placeholder extras are replaced by the served ones.
        assert "example_machine_key" not in seen["machine"]
        block = result.to_dict()["experimental"]
        assert block["experiment"] == "mixed_nozzle_sizes"
        assert block["nozzles"] == [{"id": 1, "diameter_mm": 0.4}, {"id": 2, "diameter_mm": 0.6}]
        assert block["note"].startswith("Experimental: this print uses nozzles of different sizes at once")
        assert result.message.endswith(block["note"])

    def test_with_no_map_the_ask_carries_none(self, tmp_path, served):
        asked, _ = served
        _slice(tmp_path, _slicer_that_writes("; filament_map = 1,2\n", {}), _MIXED, mixed_nozzle_experiment=True)
        assert asked == [(None, [0.4, 0.6], None)]

    @pytest.mark.parametrize(
        "miss, words",
        [
            (Miss("offline"), "this computer is offline"),
            (Miss("signed_out", "ACCOUNT_REQUIRED"), "Kiln is signed out"),
            (Miss("unanswered", "SERVER_UNREACHABLE"), "Kiln's servers didn't answer"),
            (Miss("refused", "MIXED_SIZES_NOT_SERVED", "No settings for that pair yet."), "Kiln's servers said no"),
        ],
    )
    def test_each_miss_refuses_with_its_own_sentence_and_nothing_is_sliced(self, tmp_path, served, miss, words):
        _, answer = served
        answer["miss"] = miss
        with pytest.raises(NozzleMapError) as refused:
            _slice(tmp_path, _never_runs, _MIXED, mixed_nozzle_experiment=True)
        assert refused.value.code == "MULTI_NOZZLE_MIXED_SIZES_UNAVAILABLE"
        assert refused.value.why == miss.cause
        said = str(refused.value)
        assert words in said and "so it sliced nothing" in said
        assert "one filament for one nozzle" in said
        if miss.cause == "refused":
            assert "No settings for that pair yet." in said

    def test_nozzles_of_one_size_never_ask_and_say_nothing(self, tmp_path, served):
        asked, _ = served
        result = _slice(
            tmp_path, _slicer_that_writes("; filament_map = 1,2\n", {}), _ONE_SIZE, mixed_nozzle_experiment=True,
        )
        assert asked == []
        assert result.experimental is None and "experimental" not in result.to_dict()
        assert "Experimental" not in result.message


class TestTheSliceModelDoor:
    def _call(self, tmp_path, run, **kwargs):
        record = slicer_profiles.fitted_record(_WIDE, _MIXED)
        with (
            patch("kiln.server._check_auth", return_value=None),
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return _slice_model_tool()(
                input_path=_plate(tmp_path), output_dir=str(tmp_path / "out"), printer_id=None,
                profile=_profile(tmp_path, record), **kwargs,
            )

    def test_the_door_refuses_then_slices_on_the_yes(self, tmp_path, served):
        reply = self._call(tmp_path, _never_runs)
        assert reply["success"] is False and reply["error"]["code"] == "MULTI_NOZZLE_MIXED_SIZES"
        reply = self._call(
            tmp_path, _slicer_that_writes("; filament_map = 1,2\n", {}), mixed_nozzle_experiment=True,
        )
        assert reply["success"], reply
        assert reply["experimental"]["experiment"] == "mixed_nozzle_sizes"

    def test_a_miss_at_the_door_says_why(self, tmp_path, served):
        _, answer = served
        answer["miss"] = Miss("signed_out", "ACCOUNT_REQUIRED")
        reply = self._call(tmp_path, _never_runs, mixed_nozzle_experiment=True)
        assert reply["success"] is False
        assert reply["error"]["code"] == "MULTI_NOZZLE_MIXED_SIZES_UNAVAILABLE"
        assert reply["why"] == "signed_out"


class TestTheServedAsk:
    @pytest.fixture(autouse=True)
    def _plain_install(self, monkeypatch):
        monkeypatch.setattr(catalogue, "available", lambda: False)

    def test_it_asks_for_one_printer_and_its_sizes_and_returns_the_nozzle_record(self, monkeypatch):
        sent: list = []

        def wire(tool, **kwargs):
            sent.append((tool, {k: v for k, v in kwargs.items() if not k.startswith("_")}))
            return {"success": True, "known": True, "slicer": {"nozzles": _SERVED}}

        monkeypatch.setattr(server, "_pro_api_call", wire)
        record, miss = catalogue.nozzles_for_sizes("invented_two", [0.4, 0.6], [2, 1])
        assert miss is None and record["preset_extras"] == _SERVED["preset_extras"]
        assert sent == [
            ("get_printer_record", {"printer_model": "invented_two", "nozzle_sizes": [0.4, 0.6], "filament_map": [2, 1]})
        ]

    @pytest.mark.parametrize(
        "answer, cause",
        [
            ({"status": "error", "success": False, "code": "ACCOUNT_REQUIRED", "error": "Sign in."}, "signed_out"),
            ({"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "error": "x", "why": "offline"}, "offline"),
            ({"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "error": "x"}, "unanswered"),
            ({"status": "error", "success": False, "code": "MIXED_SIZES_NOT_SERVED", "error": "No."}, "refused"),
            ({"success": True, "known": True, "slicer": {}}, "unanswered"),
        ],
    )
    def test_an_answer_that_is_not_a_record_is_a_miss(self, monkeypatch, answer, cause):
        monkeypatch.setattr(server, "_pro_api_call", lambda tool, **kwargs: answer)
        record, miss = catalogue.nozzles_for_sizes("invented_two", [0.4, 0.6])
        assert record is None and miss.cause == cause

    def test_a_network_failure_is_a_miss_never_an_exception(self, monkeypatch):
        def wire(tool, **kwargs):
            raise ConnectionResetError("gone")

        monkeypatch.setattr(server, "_pro_api_call", wire)
        record, miss = catalogue.nozzles_for_sizes("invented_two", [0.4, 0.6])
        assert record is None and miss.cause in ("offline", "unanswered")


# ---------------------------------------------------------------------------
# The file that prints
# ---------------------------------------------------------------------------


def _sliced(path: Path, sizes: str) -> str:
    path.write_text(f"G28\nG1 X1 Y1 E1\n; CONFIG_BLOCK_START\n; nozzle_diameter = {sizes}\n; CONFIG_BLOCK_END\n")
    return str(path)


class TestTheFileSaysSo:
    def test_two_sizes_are_an_experiment_and_one_size_is_not(self, tmp_path):
        mixed = _sliced(tmp_path / "mixed.gcode", "0.4,0.6")
        same = _sliced(tmp_path / "same.gcode", "0.4,0.4")
        assert experimental_print.mixed_sizes_of_file(mixed) == (0.4, 0.6)
        line = experimental_print.approval_line(mixed)
        assert line.startswith("This print uses nozzles of different sizes") and "0.4 mm and 0.6 mm" in line
        assert experimental_print.mixed_sizes_of_file(same) is None and experimental_print.approval_line(same) is None
        assert experimental_print.approval_line(str(tmp_path / "missing.gcode")) is None

    def test_the_record_word_round_trips_and_junk_is_nothing(self):
        word = experimental_print.recorded_word(["; nozzle_diameter = 0.4,0.6"])
        assert word == "mixed_nozzle_sizes:0.4,0.6"
        assert experimental_print.read_recorded(word) == ("mixed_nozzle_sizes", (0.4, 0.6))
        assert experimental_print.recorded_word(["; nozzle_diameter = 0.4,0.4"]) is None
        for junk in (None, "", "mixed_nozzle_sizes", "other_thing:0.4,0.6", "mixed_nozzle_sizes:big,small"):
            assert experimental_print.read_recorded(junk) is None


class TestThePersonIsAskedAboutThisPrint:
    def test_the_approval_says_experimental(self, tmp_path):
        path = _sliced(tmp_path / "mixed.gcode", "0.4,0.6")
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": path, "printer_name": "garage"}, host)
        [(message, _)] = host.asked
        assert "  Experimental: This print uses nozzles of different sizes at once (0.4 mm and 0.6 mm)" in message
        assert "not proven on a printer" in message

    def test_any_other_print_shows_no_such_line(self, tmp_path):
        path = _sliced(tmp_path / "same.gcode", "0.4,0.4")
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": path, "printer_name": "garage"}, host)
        [(message, _)] = host.asked
        assert "xperimental" not in message

    def test_a_standing_window_does_not_silence_the_question(self, tmp_path):
        plain = _sliced(tmp_path / "same.gcode", "0.4,0.4")
        mixed = _sliced(tmp_path / "mixed.gcode", "0.4,0.6")
        _obtain("start_print", {"file_name": plain, "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        never = _NeverAsks()
        _obtain("start_print", {"file_name": plain, "printer_name": "garage"}, never)
        assert never.asked == []
        host = _Host(CHOICE_THIS_PRINT)
        _obtain("start_print", {"file_name": mixed, "printer_name": "garage"}, host)
        assert len(host.asked) == 1

    def test_the_gate_holds_it_on_a_window_and_starts_it_on_its_own_yes(self, tmp_path):
        plain = _sliced(tmp_path / "same.gcode", "0.4,0.4")
        mixed = _sliced(tmp_path / "mixed.gcode", "0.4,0.6")
        _obtain("start_print", {"file_name": plain, "printer_name": "garage"}, _Host(CHOICE_NEXT_TWO_HOURS))
        granted = consent_for(file_name=mixed, printer_name="garage", aimed_at="garage")
        assert granted is not None and granted.source == SOURCE_WINDOW
        print_signoff.clear()
        held = server._preview_gate_error("start_print", mixed, _token_for(mixed), printer_name="garage")
        assert held["error"]["code"] == "EXPERIMENT_NEEDS_A_YES"
        assert held["error"]["message"].startswith("Experimental: This print uses nozzles of different sizes")
        assert "a yes to that print itself" in held["error"]["message"]
        # The same window still starts any other print.
        assert server._preview_gate_error("start_print", plain, _token_for(plain), printer_name="garage") is None

        def gate_inside():
            granted = consent_for(file_name=mixed, printer_name="garage", aimed_at="garage")
            assert granted.source == SOURCE_ELICITED
            return server._preview_gate_error("start_print", mixed, _token_for(mixed), printer_name="garage")

        _, verdict = _obtain(
            "start_print", {"file_name": mixed, "printer_name": "garage"}, _Host(CHOICE_THIS_PRINT), observe=gate_inside,
        )
        assert verdict is None


# ---------------------------------------------------------------------------
# The record, the question at the ending, and the account's copy
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    return KilnDB(str(tmp_path / "kiln.db"))


def _start(db, job_id: str, word: str | None) -> None:
    db.open_pending_outcome(job_id=job_id, printer_name="garage", file_name="part.gcode")
    if word:
        assert db.set_outcome_evidence(job_id, "experiment", word)


def _end(db, job_id: str, outcome: str, by: str = "observed") -> dict:
    db.save_print_outcome({"job_id": job_id, "printer_name": "garage", "outcome": outcome, "determined_by": by})
    return db.get_print_outcome(job_id)


class TestTheEndingAsksHowItCameOut:
    def test_an_experimental_print_that_finished_is_asked_about_once(self, db):
        _start(db, "start:garage:1", "mixed_nozzle_sizes:0.4,0.6")
        row = _end(db, "start:garage:1", "success")
        assert row["experiment"] == "mixed_nozzle_sizes:0.4,0.6"
        ask = ending_question.for_row(row)
        assert ask["about"] == "experiment" and "How did it come out?" in ask["question"]
        assert set(ask["record"]) == {"it came out well", "it came out, with flaws", "it came out bad", "they cannot say"}
        assert 'job_id="start:garage:1"' in ask["record"]["it came out well"]
        assert [r["job_id"] for r in db.list_endings_to_ask_about(printer_name="garage")] == ["start:garage:1"]
        assert ending_question.for_printer("garage", db=db, experiment_only=True)["about"] == "experiment"
        # The person answers: the question is not asked again.
        answered = _end(db, "start:garage:1", "success", by="user_reported")
        assert ending_question.for_row(answered) is None
        assert db.list_endings_to_ask_about(printer_name="garage") == []

    def test_nothing_to_add_is_remembered(self, db):
        _start(db, "start:garage:2", "mixed_nozzle_sizes:0.4,0.6")
        _end(db, "start:garage:2", "success")
        marked = db.mark_ending_asked("start:garage:2")
        assert marked is not None and marked["stop_reason_asked"]
        assert ending_question.for_row(marked) is None

    def test_any_other_finished_print_owes_no_question(self, db):
        _start(db, "start:garage:3", None)
        row = _end(db, "start:garage:3", "success")
        assert ending_question.for_row(row) is None
        assert ending_question.for_printer("garage", db=db, experiment_only=True) is None
        assert db.mark_ending_asked("start:garage:3") is None

    def test_the_start_writes_the_word_from_the_sent_files_own_lines(self, db, monkeypatch):
        from kiln import print_evidence

        monkeypatch.setattr(print_evidence, "_db", lambda: db)
        monkeypatch.setattr(print_evidence, "_hosted", lambda: False)
        _start(db, "start:garage:4", None)
        print_evidence.note_start_experiment("start:garage:4", ["; nozzle_diameter = 0.4,0.6"])
        assert db.get_print_outcome("start:garage:4")["experiment"] == "mixed_nozzle_sizes:0.4,0.6"
        _start(db, "start:garage:5", None)
        print_evidence.note_start_experiment("start:garage:5", ["; nozzle_diameter = 0.4,0.4"])
        assert db.get_print_outcome("start:garage:5")["experiment"] is None

    def test_every_start_reads_the_sent_file_for_it(self, db, tmp_path, monkeypatch):
        from kiln import learned_print_time, persistence, print_evidence

        sent = _sliced(tmp_path / "mixed.gcode", "0.4,0.6")
        monkeypatch.setattr(persistence, "get_db", lambda: db)
        monkeypatch.setattr(print_evidence, "_db", lambda: db)
        monkeypatch.setattr(print_evidence, "_hosted", lambda: False)
        monkeypatch.setattr(learned_print_time, "_hosted", lambda: False)
        monkeypatch.setattr("kiln._pro_cutter_bridge.local_sliced_path", lambda name: sent)
        _start(db, "start:garage:6", None)
        learned_print_time.note_start_estimate("start:garage:6", "mixed.gcode")
        assert db.get_print_outcome("start:garage:6")["experiment"] == "mixed_nozzle_sizes:0.4,0.6"


class TestTheAccountsCopy:
    def test_it_carries_the_word_and_the_sizes_and_nothing_for_any_other_print(self):
        base = {"version_id": "v1", "outcome": "success", "determined_by": "user_reported"}
        sent = outcome_courier.account_row({**base, "experiment": "mixed_nozzle_sizes:0.4,0.6"}, "a" * 32)
        assert sent["experiment"] == "mixed_nozzle_sizes" and sent["nozzle_sizes"] == [0.4, 0.6]
        for other in (None, "", "someone's words", "other:0.4,0.6"):
            plain = outcome_courier.account_row({**base, "experiment": other}, "a" * 32)
            assert "experiment" not in plain and "nozzle_sizes" not in plain
