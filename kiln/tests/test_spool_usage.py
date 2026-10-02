"""A recorded spool counts down as it prints, and stops being offered when it is empty.

Through the real doors: the start template every print passes through, each
backend's own cancel, the spool tools a person's assistant calls, and the
real spool inventory on this test's database.

Coverage:
  - a start takes each filament's own grams from the spool that prints it;
  - nothing is taken when the file's grams cannot be read, cannot be told
    apart per filament, or no recorded spool fits;
  - a linked spool wins over a colour pairing, unless the tray plainly holds
    something else; two spools that fit equally resolve to the opened one;
  - a printer with no multi-material unit charges only a linked spool;
  - a cancel Kiln sends gives back the unprinted share, and nothing when the
    progress is unknown or the printer names another file; a resume file
    takes back what the cancel returned; every backend's cancel is wired,
    and a polled backend reads its own progress;
  - a tray's own reading sets the figure, and an ambiguous pairing writes
    nothing;
  - a start and a cancel survive a broken inventory;
  - the person's word sets or empties a spool through the existing tool;
  - a spool printed down to nothing is no longer offered as one to load.
"""

from __future__ import annotations

import sqlite3
import zipfile
from typing import Any

import pytest
import responses

from kiln import server, spool_usage
from kiln.colour_availability import MISSING, OWNED, colour_availability
from kiln.materials import MaterialTracker
from kiln.persistence import KilnDB, get_db
from kiln.printers.base import JobProgress, PrintResult
from tests.test_colour_availability import _ams as _plain_ams
from tests.test_colour_availability import _Printer, printer, shelf  # noqa: F401

from .test_filament_handling import _all_adapter_classes, bambu  # noqa: F401

# ruff: noqa: F811  -- `bambu`, `printer` and `shelf` are fixtures, re-used by name

_RED = "#F72323"
_WHITE = "#FFFFFF"
_BLUE = "#1E3FD0"


def _tray(slot: int, colour: str, *, material: str = "PLA", remain: int | None = None) -> dict[str, Any]:
    """One loaded tray.  ``remain`` is a reading only when the printer measures the spool."""
    return {
        "slot": str(slot),
        "tray_type": material,
        "tray_color": f"{colour.lstrip('#')}FF",
        "remain": 0 if remain is None else remain,
        "remaining_known": remain is not None,
    }


def _unit(*trays: dict[str, Any]) -> dict[str, Any]:
    return {"units": [{"unit_id": "0", "trays": list(trays)}]}


_NO_UNIT = {"units": [], "ams_exist_bits": "0", "tray_exist_bits": "0"}


def _sliced(tmp_path, *, name: str = "part.gcode", grams: str | None = "12.5, 3.5",
            colours: str | None = "#F72323;#FFFFFF", types: str = "PLA;PLA") -> str:
    """A sliced file as a slicer leaves one: its totals and its filaments in the footer."""
    body = "G28\n" * 20
    if grams is not None:
        body += f"; filament used [g] = {grams}\n"
    if colours is not None:
        body += f"; filament_colour = {colours}\n; filament_type = {types}\n"
    path = tmp_path / name
    if name.endswith(".3mf"):
        # A sliced 3MF carries its plate's G-code inside the package.
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", body)
    else:
        path.write_text(body)
    return str(path)


@pytest.fixture
def machine(bambu, monkeypatch):
    """The real Bambu adapter, named "workshop", whose start reaches the template's bookkeeping."""
    import kiln.printers.base as base

    bambu._printer_model = "bambu_a1"
    bambu._kiln_registered_name = "workshop"
    monkeypatch.setattr("kiln.printers.print_gate.run_adapter_gate", lambda *a, **k: None)
    monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", ())
    monkeypatch.setattr(bambu, "_start_print_impl", lambda file_name, **kw: PrintResult(success=True, message="ok"))
    monkeypatch.setattr(server, "_material_tracker", None)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    def load(reading: dict[str, Any]) -> None:
        monkeypatch.setattr(bambu, "get_ams_status", lambda: reading)

    bambu.load = load
    load(_unit(_tray(0, _RED), _tray(1, _WHITE)))
    yield bambu
    spool_usage.settle()


def _add(material: str, color: str, **kwargs: Any) -> str:
    result = server.add_spool(material=material, color=color, **kwargs)
    assert result["success"] is True, result
    return result["spool"]["id"]


def _spool(spool_id: str) -> dict[str, Any]:
    return next(s for s in server.list_spools()["spools"] if s["id"] == spool_id)


def _left(spool_id: str) -> float:
    return _spool(spool_id)["remaining_grams"]


def _start(machine, file_name: str, **kwargs: Any) -> None:
    assert machine.start_print(file_name, **kwargs).success
    spool_usage.settle()


def _printing(machine, monkeypatch, *, completion: float | None, file_name: str | None = "part.gcode") -> None:
    monkeypatch.setattr(machine, "get_job", lambda: JobProgress(file_name=file_name, completion=completion))


class TestAStartCountsTheSpoolsDown:
    def test_a_two_colour_file_takes_each_colours_own_grams(self, machine, tmp_path):
        red, white = _add("PLA", "red"), _add("PLA", "white")
        _start(machine, _sliced(tmp_path))
        assert _left(red) == 987.5
        assert _left(white) == 996.5
        assert _spool(red)["remaining_determined_by"] == "inferred"

    def test_a_file_whose_grams_cannot_be_read_takes_nothing(self, machine, tmp_path):
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path, grams=None))
        _start(machine, "never-sliced-here.gcode")
        assert _left(red) == 1000.0
        assert _spool(red)["remaining_determined_by"] == "user_reported"

    def test_grams_that_cannot_be_told_apart_per_filament_take_nothing(self, machine, tmp_path):
        # Three filaments declared, two figures: which two is not in the totals.
        red, white = _add("PLA", "red"), _add("PLA", "white")
        machine.load(_unit(_tray(0, _RED), _tray(1, _WHITE), _tray(2, _BLUE)))
        _start(machine, _sliced(tmp_path, colours="#F72323;#FFFFFF;#1E3FD0", types="PLA;PLA;PLA"))
        assert (_left(red), _left(white)) == (1000.0, 1000.0)

    def test_a_colour_no_recorded_spool_fits_takes_nothing(self, machine, tmp_path):
        blue = _add("PLA", "blue")
        _start(machine, _sliced(tmp_path))
        assert _left(blue) == 1000.0

    def test_a_spool_of_a_clearly_different_material_is_not_charged(self, machine, tmp_path):
        petg = _add("PETG", "red")
        _start(machine, _sliced(tmp_path))
        assert _left(petg) == 1000.0

    def test_the_starts_own_slot_mapping_names_the_tray(self, machine, tmp_path):
        # The file's red is closest to tray 0; the start says tray 1 prints it.
        bright, dark = _add("PLA", "red"), _add("PLA", "firebrick")
        machine.load(_unit(_tray(0, "#FF0000"), _tray(1, "#B22222")))
        _start(machine, _sliced(tmp_path, grams="20.0", colours="#FF0000", types="PLA"), use_ams=True, ams_mapping=[1])
        assert (_left(bright), _left(dark)) == (1000.0, 980.0)

    def test_two_spools_that_fit_equally_resolve_to_the_opened_one(self, machine, tmp_path):
        full = _add("PLA", "red")
        opened = _add("PLA", "red", remaining_grams=400.0)
        machine.load(_unit(_tray(0, _RED)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert (_left(opened), _left(full)) == (387.5, 1000.0)

    def test_a_linked_spool_wins_over_the_pairing(self, machine, tmp_path):
        opened = _add("PLA", "red", remaining_grams=400.0)
        linked = _add("PLA", "red")
        assert server.set_material("workshop", "PLA", color="red", spool_id=linked, tool_index=0)["success"]
        machine.load(_unit(_tray(0, _RED)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert (_left(linked), _left(opened)) == (987.5, 400.0)

    def test_a_link_the_tray_contradicts_is_not_charged(self, machine, tmp_path):
        # Linked to tray 0 when it held blue; tray 0 now reads red.
        blue, red = _add("PLA", "blue"), _add("PLA", "red")
        assert server.set_material("workshop", "PLA", color="blue", spool_id=blue, tool_index=0)["success"]
        machine.load(_unit(_tray(0, _RED)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert (_left(blue), _left(red)) == (1000.0, 987.5)

    def test_a_spool_loaded_on_another_printer_is_not_charged(self, machine, tmp_path):
        elsewhere = _add("PLA", "red")
        assert server.set_material("garage", "PLA", color="red", spool_id=elsewhere)["success"]
        machine.load(_unit(_tray(0, _RED)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert _left(elsewhere) == 1000.0

    def test_a_sliced_3mf_is_read_through_its_package(self, machine, tmp_path):
        red, white = _add("PLA", "red"), _add("PLA", "white")
        _start(machine, _sliced(tmp_path, name="part.gcode.3mf"))
        assert (_left(red), _left(white)) == (987.5, 996.5)

    def test_a_resume_file_is_not_a_second_print(self, machine, tmp_path):
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path, name="transformed_resume_ab12.3mf", grams="12.5", colours="#F72323", types="PLA"))
        assert _left(red) == 1000.0

    def test_the_hosted_server_counts_nothing(self, machine, tmp_path, monkeypatch):
        red = _add("PLA", "red")
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        _start(machine, _sliced(tmp_path))
        assert _left(red) == 1000.0


class TestAPrinterWithOneFeed:
    def test_a_linked_spool_takes_the_plates_grams(self, machine, tmp_path):
        red = _add("PLA", "red")
        assert server.set_material("workshop", "PLA", color="red", spool_id=red)["success"]
        machine.load(_NO_UNIT)
        _start(machine, _sliced(tmp_path, grams="16.0", colours="#F72323", types="PLA"))
        assert _left(red) == 984.0

    def test_two_tools_whose_figures_cannot_be_told_apart_take_nothing(self, machine, tmp_path):
        # Three filaments declared, two figures, two tools: laying the
        # figures on tools 0 and 1 would be a guess about which were used.
        first, second = _add("PLA", "red"), _add("PLA", "white")
        assert server.set_material("workshop", "PLA", spool_id=first, tool_index=0)["success"]
        assert server.set_material("workshop", "PLA", spool_id=second, tool_index=1)["success"]
        machine.load(_NO_UNIT)
        _start(machine, _sliced(tmp_path, colours="#F72323;#FFFFFF;#1E3FD0", types="PLA;PLA;PLA"))
        assert (_left(first), _left(second)) == (1000.0, 1000.0)

    def test_each_tool_takes_its_own_figure(self, machine, tmp_path):
        first, second = _add("PLA", "red"), _add("PLA", "white")
        assert server.set_material("workshop", "PLA", spool_id=first, tool_index=0)["success"]
        assert server.set_material("workshop", "PLA", spool_id=second, tool_index=1)["success"]
        machine.load(_NO_UNIT)
        _start(machine, _sliced(tmp_path))
        assert (_left(first), _left(second)) == (987.5, 996.5)

    def test_with_no_link_nothing_is_taken(self, machine, tmp_path):
        # A red spool on record and a red file, but nothing says what is in the feed.
        red = _add("PLA", "red")
        machine.load(_NO_UNIT)
        _start(machine, _sliced(tmp_path, grams="16.0", colours="#F72323", types="PLA"))
        assert _left(red) == 1000.0

    def test_a_link_counts_down_though_the_loaded_record_carries_no_figure(self, tmp_path):
        tracker = MaterialTracker(db=KilnDB(db_path=str(tmp_path / "t.db")))
        spool = tracker.add_spool("pla", color="red")
        tracker.set_material("voron", "pla", spool_id=spool.id)  # remaining_grams=None
        assert tracker.deduct_usage("voron", 30.0) == 970.0
        assert tracker.get_spool(spool.id).remaining_grams == 970.0


class TestACancelGivesBackWhatWasNotPrinted:
    def test_a_cancel_at_forty_percent_gives_back_sixty(self, machine, tmp_path, monkeypatch):
        red, white = _add("PLA", "red"), _add("PLA", "white")
        _start(machine, _sliced(tmp_path))
        _printing(machine, monkeypatch, completion=40.0)
        assert machine.cancel_print().success
        assert _left(red) == 995.0  # 12.5 g charged, 5 g printed
        assert _left(white) == 998.6  # 3.5 g charged, 1.4 g printed

    def test_a_second_cancel_gives_nothing_more(self, machine, tmp_path, monkeypatch):
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path))
        _printing(machine, monkeypatch, completion=40.0)
        machine.cancel_print()
        machine.cancel_print()
        assert _left(red) == 995.0

    def test_unknown_progress_leaves_the_charge(self, machine, tmp_path, monkeypatch):
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path))
        _printing(machine, monkeypatch, completion=None)
        assert machine.cancel_print().success
        assert _left(red) == 987.5

    def test_a_progress_read_that_fails_leaves_the_charge_and_the_stop(self, machine, tmp_path, monkeypatch):
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path))

        def _boom():
            raise RuntimeError("printer not answering")

        monkeypatch.setattr(machine, "get_job", _boom)
        assert machine.cancel_print().success
        assert machine._mqtt_client.publish.called
        assert _left(red) == 987.5

    def test_a_different_file_on_the_printer_is_given_nothing(self, machine, tmp_path, monkeypatch):
        # The charged print ended on its own; this cancel is of one started at the machine.
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path))
        _printing(machine, monkeypatch, completion=10.0, file_name="something-else.gcode")
        assert machine.cancel_print().success
        assert _left(red) == 987.5

    def test_a_job_that_already_ended_is_given_nothing(self, machine, tmp_path, monkeypatch):
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path))
        monkeypatch.setattr(
            machine, "get_job", lambda: JobProgress(file_name="part.gcode", completion=10.0, active=False)
        )
        assert machine.cancel_print().success
        assert _left(red) == 987.5

    def test_a_spool_that_held_less_than_the_print_gets_back_only_what_is_left_of_it(
        self, machine, tmp_path, monkeypatch
    ):
        red = _add("PLA", "red", remaining_grams=5.0)
        machine.load(_unit(_tray(0, _RED)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert _left(red) == 0.0
        _printing(machine, monkeypatch, completion=20.0)  # 2.5 g printed of the 5 g it held
        machine.cancel_print()
        assert _left(red) == 2.5

    def test_a_cancel_asks_the_printer_nothing_when_nothing_was_charged(self, machine, monkeypatch):
        asked: list[int] = []
        monkeypatch.setattr(machine, "get_job", lambda: asked.append(1) or JobProgress(completion=40.0))
        assert machine.cancel_print().success
        assert asked == []

    def test_a_resume_file_takes_back_what_the_cancel_returned(self, machine, tmp_path, monkeypatch):
        red = _add("PLA", "red")
        machine.load(_unit(_tray(0, _RED)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        _printing(machine, monkeypatch, completion=40.0)
        machine.cancel_print()
        assert _left(red) == 995.0
        _start(machine, "transformed_resume_ab12.3mf")  # the rest of that print
        assert _left(red) == 987.5
        machine.cancel_print()  # how far the whole print got is no longer readable
        assert _left(red) == 987.5

    def test_a_linked_spool_is_given_back_through_its_link(self, machine, tmp_path, monkeypatch):
        red = _add("PLA", "red")
        assert server.set_material("workshop", "PLA", color="red", spool_id=red)["success"]
        machine.load(_NO_UNIT)
        _start(machine, _sliced(tmp_path, grams="16.0", colours="#F72323", types="PLA"))
        _printing(machine, monkeypatch, completion=75.0)
        machine.cancel_print()
        assert _left(red) == 988.0


class TestEveryBackendsCancel:
    def test_every_backends_own_cancel_gives_back(self):
        for name, cls in _all_adapter_classes().items():
            layers, fn = [], cls.cancel_print
            while fn is not None:
                layers.append(fn)
                fn = getattr(fn, "__wrapped__", None)
            assert any(getattr(layer, "_kiln_spool_wrapped", False) for layer in layers), name

    @responses.activate
    def test_a_polled_backend_reads_its_own_progress_and_gives_back(
        self, adapter, job_response_printing, monkeypatch
    ):
        # An OctoPrint, through its real job read (45.6789 % of benchy.gcode)
        # and its real cancel, for a charge of 100 g remembered at the start.
        monkeypatch.setattr(server, "_material_tracker", None)
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        red = _add("PLA", "red")
        MaterialTracker(db=get_db()).count_spool_usage(red, 100.0)
        get_db().save_spool_charge(
            "octoprint",
            "benchy.gcode",
            [{"spool_id": red, "grams": 100.0, "taken": 100.0, "label": None, "tool_index": None}],
        )
        responses.add(responses.GET, "http://octopi.local/api/job", json=job_response_printing, status=200)
        responses.add(responses.POST, "http://octopi.local/api/job", status=204)
        assert adapter.cancel_print().success
        assert _left(red) == pytest.approx(954.321, abs=0.001)
        assert get_db().get_spool_charge("octoprint")["state"] == "given_back"


class TestThePrintersOwnReading:
    def test_a_measured_trays_reading_overrides_the_count(self, machine, tmp_path):
        red = _add("PLA", "red", remaining_grams=300.0)
        machine.load(_unit(_tray(0, _RED, remain=80), _tray(1, _WHITE)))
        # The print uses white only, so the red figure is the reading alone.
        _start(machine, _sliced(tmp_path, grams="3.5", colours="#FFFFFF", types="PLA"))
        assert _left(red) == 800.0
        assert _spool(red)["remaining_determined_by"] == "observed"

    def test_the_reading_is_taken_before_this_prints_grams(self, machine, tmp_path):
        red = _add("PLA", "red", remaining_grams=300.0)
        machine.load(_unit(_tray(0, _RED, remain=80)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert _left(red) == 787.5

    def test_a_tray_the_printer_cannot_measure_writes_nothing(self, machine, tmp_path):
        red = _add("PLA", "red", remaining_grams=300.0)
        machine.load(_unit(_tray(0, _RED), _tray(1, _WHITE)))
        _start(machine, _sliced(tmp_path, grams="3.5", colours="#FFFFFF", types="PLA"))
        assert _left(red) == 300.0

    def test_an_ambiguous_pairing_writes_nothing(self, machine, tmp_path):
        # Two red spools on record and one red tray: the reading belongs to
        # one of them and nothing says which.
        one = _add("PLA", "red", remaining_grams=300.0)
        other = _add("PLA", "red", remaining_grams=600.0)
        machine.load(_unit(_tray(0, _RED, remain=80), _tray(1, _WHITE)))
        _start(machine, _sliced(tmp_path, grams="3.5", colours="#FFFFFF", types="PLA"))
        assert (_left(one), _left(other)) == (300.0, 600.0)

    def test_one_spool_that_fits_two_trays_is_not_written(self, machine, tmp_path):
        red = _add("PLA", "red", remaining_grams=300.0)
        machine.load(_unit(_tray(0, _RED, remain=80), _tray(1, "#F02020", remain=20), _tray(2, _WHITE)))
        _start(machine, _sliced(tmp_path, grams="3.5", colours="#FFFFFF", types="PLA"))
        assert _left(red) == 300.0

    def test_a_spool_the_count_emptied_is_restored_by_its_trays_reading(self, machine, tmp_path):
        red = _add("PLA", "red", remaining_grams=0.0)
        machine.load(_unit(_tray(0, _RED, remain=25), _tray(1, _WHITE)))
        _start(machine, _sliced(tmp_path, grams="3.5", colours="#FFFFFF", types="PLA"))
        assert _left(red) == 250.0


class TestNothingHereBreaksAPrint:
    def test_a_start_survives_a_broken_inventory(self, machine, tmp_path, monkeypatch):
        def _boom():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(spool_usage, "_db", _boom)
        assert machine.start_print(_sliced(tmp_path)).success

    def test_a_printer_that_will_not_say_what_is_loaded_is_charged_nothing(self, machine, tmp_path):
        # A unit that could not be read is not a printer with one feed: the
        # spool linked to tray 0 may not be the one printing.
        red = _add("PLA", "red")
        assert server.set_material("workshop", "PLA", color="red", spool_id=red)["success"]

        def _boom():
            raise RuntimeError("not connected")

        machine.get_ams_status = _boom
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert _left(red) == 1000.0

    def test_a_cancel_survives_a_broken_inventory(self, machine, monkeypatch):
        def _boom():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(spool_usage, "_db", _boom)
        assert machine.cancel_print().success
        assert machine._mqtt_client.publish.called

    def test_the_start_does_not_wait_for_the_printer(self, machine, tmp_path):
        # The tray read happens on the counting thread: a start returns
        # while that read is still held up.
        import threading

        red = _add("PLA", "red")
        release = threading.Event()
        reading = _unit(_tray(0, _RED), _tray(1, _WHITE))

        def _slow():
            release.wait(5)
            return reading

        machine.get_ams_status = _slow
        assert machine.start_print(_sliced(tmp_path)).success
        assert _left(red) == 1000.0  # not taken yet: the printer has not answered
        release.set()
        spool_usage.settle()
        assert _left(red) == 987.5


class TestThePersonsWord:
    def test_i_am_out_empties_the_record(self, machine):
        red = _add("PLA", "red")
        result = server.add_spool(spool_id=red, remaining_grams=0)
        assert result["success"] is True and result["updated"] is True
        assert _left(red) == 0.0

    def test_i_still_have_plenty_sets_the_figure_without_a_new_spool(self, machine, tmp_path):
        red = _add("PLA", "red")
        _start(machine, _sliced(tmp_path))
        assert _spool(red)["remaining_determined_by"] == "inferred"
        assert server.add_spool(spool_id=red, remaining_grams=850)["success"] is True
        assert _left(red) == 850.0
        assert _spool(red)["remaining_determined_by"] == "user_reported"
        assert len(server.list_spools()["spools"]) == 1

    def test_a_part_used_spool_can_be_added_as_one(self, machine):
        assert _left(_add("PETG", "black", remaining_grams=320.0)) == 320.0

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"spool_id": "no-such-spool", "remaining_grams": 10}, "NOT_FOUND"),
            ({"spool_id": "X", "remaining_grams": None}, "VALIDATION_ERROR"),
            ({"spool_id": "X", "remaining_grams": -5}, "VALIDATION_ERROR"),
            ({"spool_id": "X", "remaining_grams": 5000}, "VALIDATION_ERROR"),
            ({"material": None}, "VALIDATION_ERROR"),
            ({"material": "PLA", "remaining_grams": 2000}, "VALIDATION_ERROR"),
        ],
    )
    def test_a_figure_that_cannot_be_right_is_refused(self, machine, kwargs, code):
        red = _add("PLA", "red")
        if kwargs.get("spool_id") == "X":
            kwargs = {**kwargs, "spool_id": red}
        result = server.add_spool(**kwargs)
        assert result["success"] is False
        assert result["error"]["code"] == code
        assert _left(red) == 1000.0

    def test_a_database_from_before_the_count_reads_as_the_persons_figure(self, tmp_path):
        path = str(tmp_path / "old.db")
        old = sqlite3.connect(path)
        old.execute(
            "CREATE TABLE spools (id TEXT PRIMARY KEY, material_type TEXT NOT NULL, color TEXT, brand TEXT, "
            "weight_grams REAL NOT NULL DEFAULT 1000.0, remaining_grams REAL NOT NULL DEFAULT 1000.0, "
            "cost_usd REAL, purchase_date REAL, notes TEXT)"
        )
        old.execute("INSERT INTO spools (id, material_type, color) VALUES ('s1', 'PLA', 'red')")
        old.commit()
        old.close()
        tracker = MaterialTracker(db=KilnDB(db_path=path))
        assert tracker.get_spool("s1").remaining_determined_by == "user_reported"
        assert tracker.count_spool_usage("s1", 100.0) == 900.0
        assert tracker.get_spool("s1").remaining_determined_by == "inferred"


class TestAnEmptySpoolIsNotOffered:
    def test_a_spool_printed_down_to_nothing_is_no_longer_one_to_load(
        self, machine, tmp_path, printer, shelf
    ):
        red = _add("PLA", "red", brand="Polymaker", weight_grams=12.5)
        printer(_Printer(_plain_ams(_WHITE)))  # white is loaded; red is on the shelf
        before = colour_availability([_RED])
        assert before["colours"][0]["state"] == OWNED
        assert "Load it before printing" in before["say"]

        # The red goes in and one print uses the whole spool.
        machine.load(_unit(_tray(0, _RED)))
        _start(machine, _sliced(tmp_path, grams="12.5", colours="#F72323", types="PLA"))
        assert _left(red) == 0.0

        # White is loaded again.  The red on record is used up.
        after = colour_availability([_RED])
        assert after["colours"][0]["state"] == MISSING
        assert "You have" not in after["say"]

    def test_a_spool_the_person_says_is_used_up_is_no_longer_one_to_load(self, machine, printer, shelf):
        red = _add("PLA", "red", brand="Polymaker")
        printer(_Printer(_plain_ams(_WHITE)))
        assert colour_availability([_RED])["colours"][0]["state"] == OWNED
        assert server.add_spool(spool_id=red, remaining_grams=0)["success"] is True
        assert colour_availability([_RED])["colours"][0]["state"] == MISSING

    def test_the_charge_is_kept_in_the_one_database(self, machine, tmp_path):
        _add("PLA", "red")
        _start(machine, _sliced(tmp_path))
        row = get_db().get_spool_charge("workshop")
        assert row["state"] == "open"
        assert [(c["grams"], c["taken"]) for c in row["charges"]] == [(12.5, 12.5)]
