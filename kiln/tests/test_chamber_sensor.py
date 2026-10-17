"""A chamber temperature is reported only from a machine that can measure one.

The incident (2026-09-14, live, through ``printer_status`` against a Bambu
A1 -- an open-frame bed-slinger with no enclosure): ``chamber_temp_actual: 5``
beside a 22.5 C bed and a 24 C hotend, idle, freshly powered on.  The A1's
firmware publishes ``chamber_temper`` in every report whether or not the
machine has a chamber thermistor, and the adapter passed the field through
untouched, so every door -- full and lite status, the monitor text, the
ambient check -- quoted a 5 C chamber that does not exist.

The rule lives in ONE place, :class:`PrinterState`, so every door that reads
a state gets it: a state whose adapter says the machine has no chamber
sensor carries no chamber numbers, and says why.  The per-model fact
(``has_chamber_sensor``) is part of each printer's catalogue record, served
per printer; that every Bambu row states it the way Bambu's own per-printer
configuration does (a chamber temperature is shown only where
``support_chamber_temp_display``, or failing that ``support_chamber``, is
true -- every X1, X2D, P2S and H2 machine and none of the A1, A1 mini, A2L,
P1P or P1S; the P1S has an enclosure and no sensor, which is what makes
"enclosed" the wrong anchor) is pinned beside the records, in kiln-pro's
``tests/test_printer_catalogue_numbers.py``.

Here the machines are the session fixture's synthetic printers
(``tests/fixtures/printer_catalogue_test.json``): ``kilntest_open`` states no
sensor, ``kilntest_enclosed`` states one, ``kilntest_big`` leaves the fact
unstated.  A Bambu serial names a REAL model, so the serial-only path is
exercised against the served door: sealed (the signed-out install public CI
is) it answers unknown, and with the record served it answers the record.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln import _pro_catalogue_bridge as bridge
from kiln import printer_intelligence as pi
from kiln import safety_profiles as sp
from kiln.printer_intelligence import _get_raw, chamber_sensor_for_model
from kiln.printers import bed_fit
from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import PrinterState, PrinterStatus

# The reading the incident produced, verbatim.
INCIDENT_CHAMBER_PLACEHOLDER = 5

#: The synthetic rows and what each states about a chamber sensor.
SENSORLESS = "kilntest_open"
SENSORED = "kilntest_enclosed"
UNSTATED = "kilntest_big"
FIXTURE_CHAMBER_SENSOR: dict[str, bool | None] = {
    SENSORLESS: False,
    SENSORED: True,
    UNSTATED: None,
    "kilntest_hot": True,
    "kilntest_ptfe": False,
}

#: The model Bambu's documented serial scheme names for an A1 serial, and a
#: SYNTHETIC record for it: every number here is made up; only the id and
#: the one fact under test (no chamber sensor) are the A1's.
SERIAL_NAMED_MODEL = "bambu_a1"
SERIAL_NAMED_RECORD = {
    "printer_id": SERIAL_NAMED_MODEL,
    "matched_by": "exact",
    "display_name": "Serial-named test record",
    "safety": {
        "max_hotend_temp": 290.0, "max_bed_temp": 95.0, "max_chamber_temp": None,
        "max_feedrate": 9000.0, "min_safe_z": 0.0, "max_volumetric_flow": 14.0,
        "build_volume": [210, 210, 210], "notes": "Synthetic served record.",
    },
    "intel": {
        "firmware": "bambu", "extruder_type": "direct_drive", "hotend_type": "all_metal",
        "has_enclosure": False, "has_chamber_sensor": False, "has_abl": True,
        "has_input_shaping": True, "build_volume_mm": [210, 210, 210],
        "max_hotend_temp": 290, "max_bed_temp": 95,
        "materials": {"PLA": {"hotend": 207, "bed": 58, "fan": 100}},
        "nozzle_clumping_probe": None,
    },
}


def _forget_served_records() -> None:
    """Drop any record a test had served, so the next test starts sealed."""
    sp._cache.clear()
    sp._roster.clear()
    sp._variant_data.clear()
    sp._loaded = False
    pi._reset_caches()
    bed_fit._printer_intelligence_cache = None
    bridge.forget()


@pytest.fixture
def served_a1_record(monkeypatch, tmp_path):
    """The servers answer the serial-named model with ``SERIAL_NAMED_RECORD``,
    and the record is kept under a throwaway home, never this computer's."""
    monkeypatch.setattr(bridge, "available", lambda: False)
    monkeypatch.setattr(bridge, "KEEP_DIR", tmp_path / "printer_catalogue")
    monkeypatch.setattr(
        bridge, "_served",
        lambda key: (json.loads(json.dumps(SERIAL_NAMED_RECORD)), None) if key == SERIAL_NAMED_MODEL else (None, None),
    )
    _forget_served_records()
    yield
    _forget_served_records()


def _bambu(serial: str, printer_model: str | None = None) -> BambuAdapter:
    adapter = BambuAdapter(
        host="192.168.1.50",
        access_code="12345678",
        serial=serial,
        timeout=2,
        printer_model=printer_model,
    )
    adapter._mqtt_connected.set()
    adapter._connected = True
    adapter._mqtt_client = MagicMock()
    return adapter


def _idle_report(chamber: float | int = INCIDENT_CHAMBER_PLACEHOLDER) -> dict:
    return {
        "gcode_state": "IDLE",
        "nozzle_temper": 24.06,
        "nozzle_target_temper": 0,
        "bed_temper": 22.5,
        "bed_target_temper": 0,
        "chamber_temper": chamber,
    }


# ---------------------------------------------------------------------------
# The catalogue states the fact, for every Bambu machine, from the vendor.
# ---------------------------------------------------------------------------


class TestCatalogue:
    @pytest.mark.parametrize("model,expected", sorted(FIXTURE_CHAMBER_SENSOR.items()))
    def test_every_held_row_states_its_chamber_sensor(self, model: str, expected: bool | None) -> None:
        raw = _get_raw(model)
        assert raw is not None, f"{model} is not in the catalogue"
        assert raw.get("has_chamber_sensor") is expected

    def test_accessor_answers_the_catalogue(self) -> None:
        assert chamber_sensor_for_model(SENSORLESS) is False
        assert chamber_sensor_for_model(SENSORED) is True

    def test_accessor_is_unknown_where_nothing_is_stated(self) -> None:
        # A row that has never been judged for this fact, and no model at
        # all.  Neither is "no sensor": unknown must not blank a real reading
        # and must not invent one.
        assert chamber_sensor_for_model(UNSTATED) is None
        assert chamber_sensor_for_model(None) is None
        assert chamber_sensor_for_model("") is None
        assert chamber_sensor_for_model("not_a_printer") is None

    def test_a_roster_printer_whose_record_is_not_held_is_unknown(self) -> None:
        # A catalogue name with no record here (the session fixture seals the
        # served door as signed out): the fact is on the record, so the
        # answer is unknown -- not "no sensor".
        assert chamber_sensor_for_model("kilntest_served") is None

    def test_the_served_record_answers(self, served_a1_record) -> None:
        assert chamber_sensor_for_model(SERIAL_NAMED_MODEL) is False

    def test_accessor_is_exact_not_fuzzy(self) -> None:
        # ``_get_raw`` prefix-matches, which is fine for quirks and
        # calibration prose and wrong for a fact that decides whether a
        # number is a measurement: a prefix of the enclosed row's id must not
        # borrow its answer.
        assert chamber_sensor_for_model("kilntest_enclose") is None
        assert chamber_sensor_for_model("kilntest") is None


# ---------------------------------------------------------------------------
# The rule, once, on the state every door reads.
# ---------------------------------------------------------------------------


class TestPrinterStateRule:
    def test_no_sensor_blanks_both_chamber_fields_and_says_why(self) -> None:
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            bed_temp_actual=22.5,
            chamber_temp_actual=INCIDENT_CHAMBER_PLACEHOLDER,
            chamber_temp_target=0,
            chamber_sensor=False,
        )
        assert state.chamber_temp_actual is None
        assert state.chamber_temp_target is None
        assert state.chamber_sensor is False
        assert state.chamber_note
        assert "chamber" in state.chamber_note.lower()
        # The other temperatures are untouched: the machine measures those.
        assert state.bed_temp_actual == 22.5
        assert state.temperature_note is None

    def test_sensor_present_keeps_the_reading(self) -> None:
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            chamber_temp_actual=41.0,
            chamber_temp_target=45.0,
            chamber_sensor=True,
        )
        assert state.chamber_temp_actual == 41.0
        assert state.chamber_temp_target == 45.0
        assert state.chamber_note is None

    def test_unstated_keeps_the_reading(self) -> None:
        # Every adapter that only fills the field when a named sensor exists
        # (Klipper's ``temperature_sensor chamber``, a Duet chamber heater)
        # says nothing and is untouched.
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            chamber_temp_actual=41.0,
        )
        assert state.chamber_temp_actual == 41.0
        assert state.chamber_note is None

    def test_no_sensor_and_no_reading_still_says_why(self) -> None:
        # The sentence is about the MACHINE, not about this reading, so it
        # rides every state from a sensorless model -- otherwise a blank
        # chamber reads as "the printer went quiet".
        state = PrinterState(connected=True, state=PrinterStatus.IDLE, chamber_sensor=False)
        assert state.chamber_note

    def test_serialised_form_carries_the_sentence_and_not_the_number(self) -> None:
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            chamber_temp_actual=INCIDENT_CHAMBER_PLACEHOLDER,
            chamber_sensor=False,
        )
        data = state.to_dict()
        assert data["chamber_temp_actual"] is None
        assert data["chamber_sensor"] is False
        assert data["chamber_note"] == state.chamber_note

    def test_serialised_form_omits_the_fields_when_unstated(self) -> None:
        # Same compaction as every other extended field, so the existing
        # exact-key-set pins on the base payload hold.
        data = PrinterState(connected=True, state=PrinterStatus.IDLE).to_dict()
        assert "chamber_sensor" not in data
        assert "chamber_note" not in data

    def test_stale_floor_still_wins(self) -> None:
        # A machine WITH a chamber sensor whose reading Kiln cannot vouch
        # for: the trust floor blanks the chamber along with the rest.
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            chamber_temp_actual=41.0,
            chamber_sensor=True,
            state_age_seconds=1000.0,
            state_stale_after_seconds=300.0,
        )
        assert state.state is PrinterStatus.STALE
        assert state.chamber_temp_actual is None
        assert state.temperature_note


# ---------------------------------------------------------------------------
# The Bambu adapter: the field is published for every model, so the model
# decides whether it is a measurement.
# ---------------------------------------------------------------------------


class TestBambuAdapter:
    def test_incident_sensorless_machine_reports_no_chamber(self) -> None:
        adapter = _bambu("039001234567890", printer_model=SENSORLESS)
        adapter._last_status = _idle_report()
        state = adapter.get_state()
        assert state.chamber_temp_actual is None
        assert state.chamber_sensor is False
        assert state.chamber_note
        # The measured temperatures beside it are untouched.
        assert state.bed_temp_actual == 22.5
        assert state.tool_temp_actual == 24.06

    def test_by_serial_alone_with_the_record_served(self, served_a1_record) -> None:
        # No ``printer_model`` in config: the serial prefix names the model,
        # and the model's served record says it has no sensor.
        adapter = _bambu("039001234567890")
        adapter._last_status = _idle_report()
        state = adapter.get_state()
        assert state.chamber_temp_actual is None
        assert state.chamber_sensor is False

    def test_by_serial_alone_with_no_record_is_unknown(self) -> None:
        # The same serial on an install that could not get the record (the
        # session fixture seals the served door as signed out): the number
        # is still not quoted, and no claim is made either way.
        adapter = _bambu("039001234567890")
        adapter._last_status = _idle_report()
        state = adapter.get_state()
        assert state.chamber_temp_actual is None
        assert state.chamber_sensor is None

    def test_enclosed_but_sensorless_is_sensorless(self) -> None:
        # The P1S shape: an enclosure is not a sensor, the flag decides.
        # (The P1S row itself -- enclosed, sensorless -- is pinned in
        # kiln-pro.)
        adapter = _bambu("01P001234567890", printer_model="kilntest_ptfe")
        adapter._last_status = _idle_report(chamber=27)
        state = adapter.get_state()
        assert state.chamber_temp_actual is None
        assert state.chamber_sensor is False

    def test_a_sensored_machine_keeps_its_measurement(self) -> None:
        adapter = _bambu("00M001234567890", printer_model=SENSORED)
        adapter._last_status = _idle_report(chamber=38.0)
        state = adapter.get_state()
        assert state.chamber_temp_actual == 38.0
        assert state.chamber_sensor is True
        assert state.chamber_note is None

    def test_configured_model_outranks_serial(self, served_a1_record) -> None:
        # The config-declared model owns every behaviour decision, this one
        # included -- the same boundary ``get_printer_info`` documents.  The
        # serial names a sensored family; the declared model has no sensor.
        adapter = _bambu("00M001234567890", printer_model=SENSORLESS)
        adapter._last_status = _idle_report(chamber=38.0)
        assert adapter.get_state().chamber_temp_actual is None

    def test_state_builder_takes_no_lock(self, served_a1_record) -> None:
        # ``get_state``'s backoff-cooldown branch builds the state while
        # HOLDING ``_state_lock`` (non-reentrant).  The first cut of this
        # feature resolved the model through ``get_printer_info``, whose
        # firmware-identity read takes that same lock, and every status read
        # during a cooldown hung forever -- caught by a hang-dump, not by a
        # failing assertion.  So: hold the lock, build a state on another
        # thread, and require it to finish.
        import threading

        adapter = _bambu("039001234567890")  # serial-only: the branch that looked up identity
        finished = threading.Event()
        result: list[PrinterState] = []

        def build() -> None:
            result.append(adapter._build_state_from_cache(_idle_report(), age=1.0))
            finished.set()

        with adapter._state_lock:
            threading.Thread(target=build, daemon=True).start()
            assert finished.wait(5.0), "state builder blocked on _state_lock"
        assert result[0].chamber_sensor is False
        assert result[0].chamber_temp_actual is None

    def test_bare_instance_builds_a_state_with_unknown_chamber(self) -> None:
        # The state builder is exercised on an adapter that never ran
        # ``__init__`` (see test_finished_state_distinction).  It must still
        # build, and a machine nobody named is an unknown model: no chamber
        # number, and no claim that there is no sensor.
        bare = object.__new__(BambuAdapter)
        state = BambuAdapter._build_state_from_cache(bare, _idle_report(chamber=38.0))
        assert state.chamber_temp_actual is None
        assert state.chamber_sensor is None

    def test_unknown_model_reports_unknown_not_a_number(self) -> None:
        # Neither config nor serial names the machine: Kiln cannot say a
        # sensor produced the number, so it does not quote it -- and does
        # not claim there is no sensor either.
        adapter = _bambu("ZZZ001234567890")
        adapter._last_status = _idle_report(chamber=38.0)
        state = adapter.get_state()
        assert state.chamber_temp_actual is None
        assert state.chamber_sensor is None
        assert state.chamber_note is None


# ---------------------------------------------------------------------------
# The doors.
# ---------------------------------------------------------------------------


class TestDoors:
    def _a1_adapter(self) -> MagicMock:
        real = _bambu("039001234567890", printer_model=SENSORLESS)
        real._last_status = _idle_report()
        state = real.get_state()
        adapter = MagicMock()
        adapter.get_state.return_value = state
        adapter.get_status.side_effect = AttributeError  # fall back to get_state/get_job
        from kiln.printers.base import JobProgress

        adapter.get_job.return_value = JobProgress()
        type(adapter).capabilities = PropertyMock(return_value=real.capabilities)
        return adapter

    @pytest.mark.parametrize("detail", ["full", "lite"])
    def test_printer_status_both_levels(self, detail: str) -> None:
        from kiln.server import printer_status

        with patch("kiln.server._get_adapter", return_value=self._a1_adapter()):
            result = printer_status(detail=detail)
        assert result["success"] is True
        printer = result["printer"]
        assert printer["chamber_temp_actual"] is None
        assert printer["chamber_sensor"] is False
        assert printer["chamber_note"]

    def test_monitor_text_does_not_quote_a_chamber(self) -> None:
        from kiln.server import monitor_print

        with patch("kiln.server._get_adapter", return_value=self._a1_adapter()):
            text = monitor_print(include_snapshot=False)
        assert "Chamber: 5" not in text
        # The line is still there, and it says why -- a blank where the
        # chamber used to be reads as "the printer went quiet".
        assert "Chamber: This printer has no chamber temperature sensor" in text

    def test_ambient_check_has_no_chamber_to_judge(self) -> None:
        from kiln.server import check_ambient_conditions

        with patch("kiln.server._get_adapter", return_value=self._a1_adapter()):
            result = check_ambient_conditions(material="PLA")
        assert result["success"] is True
        assert result["ambient_safety"]["chamber_temp_c"] is None
