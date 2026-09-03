"""The tool surface tells the truth about how old a reading is.

The web Monitor already did.  Kiln 1.4.0 put the principle in writing --
"when a reading has gone stale, the Monitor says so, instead of presenting
old numbers as live" -- and the adapter layer underneath it did not honour
that principle for anything else reading the same printer.  ``printer_status``
led with a confident state word and buried the age in a trailing string, so
the two surfaces disagreed about one machine.

Everything here is pinned against a measured session on a Bambu A1
(2026-09-03), verified live:

  * ``state: "idle"``, ``connected: true`` and a full job block while the
    printer had published nothing for 28 minutes.  Two samples five minutes
    apart: ``state_age_seconds`` 1396, then 1701, no new data in between.
    The printer answered ping and held port 8883 open throughout.
  * That job block named a print cancelled hours earlier -- layer 1 of 225,
    3h 57m remaining, ``last_job_result: "cancelled"``.  The same held job
    greyed out Load and Unload on the printer's own screen, so a filament
    jam could not be cleared by hand.  A power cycle fixed it: the age fell
    to 69 s, the job block emptied, Load became pressable.
  * A ``set_temperature`` was accepted and the nozzle was visibly heating
    while the freshest reading Kiln held was 435 s old and still showed
    target 0 with the cooling fans on.
  * ``print_error`` came back as the decimal ``302022663``.  That is
    ``0x12008007`` -- the HMS code the printer's own screen renders as
    ``1200-8007``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest

from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import (
    BUSY_STATES,
    CAUSE_CONNECTION_LIMIT,
    CAUSE_POWERED_OFF,
    CAUSE_SILENT,
    CAUSE_WRONG_ACCESS_CODE,
    INDETERMINATE_STATES,
    READY_STATES,
    STALE_STATE_MAX_AGE,
    STALE_STATE_WARN_AGE,
    UNREACHABLE_STATES,
    JobProgress,
    JobResult,
    PrinterState,
    PrinterStatus,
    TelemetryCadence,
    diagnose_read_failure,
    format_error_code,
    reconcile_job_with_state,
    status_is_occupied,
    stuck_job_note,
)

HOST = "192.0.2.10"
ACCESS_CODE = "12345678"
SERIAL = "TEST1234567890"

# The two readings the live session actually took, five minutes apart.
MEASURED_STALE_AGE = 1396.0
MEASURED_STALE_AGE_LATER = 1701.0
# The reading that could not tell whether a commanded heater was on.
MEASURED_HEATER_BLIND_AGE = 435.0
# What a healthy A1 looked like immediately after the power cycle.
MEASURED_HEALTHY_AGE = 69.0
# The HMS code from the same session.
MEASURED_HMS_DECIMAL = 302022663
MEASURED_HMS_RENDERED = "1200-8007"


@pytest.fixture
def adapter(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> BambuAdapter:
    """A Bambu adapter with a mocked-connected MQTT client."""
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"))
    a = BambuAdapter(
        host=HOST, access_code=ACCESS_CODE, serial=SERIAL, timeout=2
    )
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    a._mqtt_client.publish.return_value = publish_result
    return a


def _push(adapter: BambuAdapter, **fields: Any) -> None:
    """Feed *fields* in as a real ``push_status`` message.

    Through ``_on_message`` rather than by assigning ``_last_status``, so the
    merge, the timestamp guard, the vintage stamp and the cadence
    measurement are all exercised the way the printer exercises them.
    """
    msg = mock.MagicMock()
    msg.payload = json.dumps(
        {"print": {"command": "push_status", **fields}}
    ).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _age(adapter: BambuAdapter, seconds: float) -> None:
    """Pretend the last state-bearing push arrived *seconds* ago."""
    adapter._gcode_state_time -= seconds
    adapter._last_state_time -= seconds


# ---------------------------------------------------------------------------
# 1. The age is the headline, not a footnote
# ---------------------------------------------------------------------------


class TestStalenessLeads:
    """A reading past its budget stops being reported in the present tense."""

    def test_the_measured_28_minute_silence_reads_as_stale_not_idle(
        self, adapter: BambuAdapter
    ) -> None:
        """The incident itself: ``idle`` for a printer silent for 28 minutes."""
        _push(adapter, gcode_state="IDLE", nozzle_temper=24)
        _age(adapter, MEASURED_STALE_AGE)

        state = adapter.get_state()

        assert state.state is PrinterStatus.STALE
        # The run state is moved, not destroyed.
        assert state.last_known_state is PrinterStatus.IDLE
        assert state.effective_state is PrinterStatus.IDLE
        assert state.state_age_seconds is not None
        assert state.state_age_seconds > 1390.0
        # And it says so in the serialised form every surface reads.
        data = state.to_dict()
        assert data["state"] == "stale"
        assert data["last_known_state"] == "idle"
        assert data["remedy"]

    def test_a_stale_print_still_reads_as_printing_underneath(
        self, adapter: BambuAdapter
    ) -> None:
        """Demoting the run state would let a second print start.

        The headline changes; the fact every concurrency gate depends on
        does not.
        """
        _push(adapter, gcode_state="RUNNING", nozzle_temper=210)
        _age(adapter, MEASURED_STALE_AGE_LATER)

        state = adapter.get_state()

        assert state.state is PrinterStatus.STALE
        assert state.effective_state is PrinterStatus.PRINTING
        assert state.is_occupied is True

    def test_a_fresh_reading_is_untouched(self, adapter: BambuAdapter) -> None:
        """Nothing here puts a warning on a printer that is answering."""
        _push(adapter, gcode_state="RUNNING", nozzle_temper=210)

        state = adapter.get_state()

        assert state.state is PrinterStatus.PRINTING
        assert state.last_known_state is None
        assert state.staleness_note() is None

    def test_the_note_names_what_the_printer_was_doing(
        self, adapter: BambuAdapter
    ) -> None:
        """"PRINTING describes then" is the fact; "STALE describes then" is not."""
        _push(adapter, gcode_state="RUNNING")
        _age(adapter, MEASURED_STALE_AGE)

        note = adapter.get_state().staleness_note()

        assert note is not None
        assert "PRINTING" in note
        assert "STALE" not in note


# ---------------------------------------------------------------------------
# 2. The budget is measured from the printer, not picked in advance
# ---------------------------------------------------------------------------


class TestMeasuredCadence:
    """How long is too long is a property of the machine, not a constant."""

    def test_no_samples_falls_back_to_the_shipped_rule(self) -> None:
        cadence = TelemetryCadence()
        assert cadence.observed_interval_seconds is None
        assert cadence.stale_after_seconds() == STALE_STATE_WARN_AGE

    def test_a_fast_cadence_cannot_make_the_check_noisy(self) -> None:
        """A printer reporting every second does not get a 3-second budget."""
        cadence = TelemetryCadence()
        for tick in range(20):
            cadence.record(float(tick))
        assert cadence.observed_interval_seconds == pytest.approx(1.0)
        assert cadence.stale_after_seconds() == STALE_STATE_WARN_AGE

    def test_a_slow_cadence_widens_the_budget_to_match(self) -> None:
        """A printer that reports every 40 s is not stale at 90 s."""
        cadence = TelemetryCadence()
        for tick in range(10):
            cadence.record(tick * 40.0)
        assert cadence.observed_interval_seconds == pytest.approx(40.0)
        assert cadence.stale_after_seconds() == pytest.approx(120.0)

    def test_the_budget_is_capped_however_slow_the_printer_is(self) -> None:
        """The reading that could not see a commanded heater is still stale.

        Measured: 435 s old, target 0, fans cooling, nozzle visibly heating.
        No cadence excuses that, so the budget stops at five minutes.
        """
        cadence = TelemetryCadence()
        for tick in range(10):
            cadence.record(tick * 200.0)
        assert cadence.stale_after_seconds() == STALE_STATE_MAX_AGE
        assert cadence.stale_after_seconds() < MEASURED_HEATER_BLIND_AGE

    def test_an_outage_does_not_widen_the_budget_that_would_catch_it(
        self,
    ) -> None:
        """Feeding the failure back in makes the next failure invisible."""
        cadence = TelemetryCadence()
        for tick in range(6):
            cadence.record(tick * 10.0)
        before = cadence.stale_after_seconds()
        cadence.record(50.0 + MEASURED_STALE_AGE)  # the 28-minute silence
        assert cadence.stale_after_seconds() == before

    def test_the_adapter_measures_its_own_printer(
        self, adapter: BambuAdapter
    ) -> None:
        """The cadence comes off the real pushes, not off a setting."""
        for _ in range(6):
            _push(adapter, gcode_state="RUNNING")
            adapter._gcode_state_time -= 0.0  # pushes land at the same instant
        # Gaps of ~0 are discarded, so the budget stays at the floor.
        state = adapter.get_state()
        assert state.state_stale_after_seconds == pytest.approx(
            STALE_STATE_WARN_AGE
        )

    def test_the_budget_in_force_is_reported_with_the_reading(
        self, adapter: BambuAdapter
    ) -> None:
        """A reader sees the rule, not only its verdict."""
        _push(adapter, gcode_state="IDLE")
        data = adapter.get_state().to_dict()
        assert data["state_stale_after_seconds"] == pytest.approx(
            STALE_STATE_WARN_AGE
        )

    def test_a_healthy_printer_just_after_a_power_cycle_is_not_stale(
        self, adapter: BambuAdapter
    ) -> None:
        """69 s old with a two-minute reporting interval is a healthy printer.

        The fixed one-minute rule called this stale.  Measured from the
        session: the age immediately after the power cycle that fixed the
        held job, on a printer that was working perfectly.
        """
        _push(adapter, gcode_state="IDLE")
        with adapter._state_lock:
            for tick in range(8):
                adapter._cadence.record(tick * 120.0)
        _age(adapter, MEASURED_HEALTHY_AGE)

        state = adapter.get_state()

        assert state.state is PrinterStatus.IDLE
        assert state.is_stale() is False


# ---------------------------------------------------------------------------
# 3. A job block that cannot contradict itself
# ---------------------------------------------------------------------------


class TestJobBlockHonesty:
    """A cancelled or finished job is never served as if it were current."""

    def test_the_measured_cancelled_job_is_not_presented_as_live(
        self, adapter: BambuAdapter
    ) -> None:
        """Layer 1 of 225, 3h 57m remaining -- for a print cancelled hours ago."""
        _push(
            adapter,
            gcode_state="failed",
            print_error=0,  # a cancel, on this firmware
            gcode_file="PRINT_ME_jar_v8.3mf",
            layer_num=1,
            total_layer_num=225,
            mc_percent=0,
            mc_remaining_time=237,  # 14220 seconds
        )

        state, job = adapter.get_status()
        jd = job.to_dict()

        assert jd["active"] is False
        assert jd["ended_as"] == "cancelled"
        # The forecast for a future that is not coming is gone.
        assert "print_time_left_seconds" not in jd
        # What actually happened is kept: it is true.
        assert jd["file_name"] == "PRINT_ME_jar_v8.3mf"
        assert jd["current_layer"] == 1
        assert jd["total_layers"] == 225
        assert state.last_job_result is JobResult.CANCELLED

    def test_an_idle_printer_holding_a_filename_says_the_job_is_over(
        self, adapter: BambuAdapter
    ) -> None:
        """A Bambu keeps the last file name in its cache long after the print."""
        _push(adapter, gcode_state="IDLE", gcode_file="yesterday.3mf")

        _state, job = adapter.get_status()

        assert job.is_active is False
        assert job.to_dict()["active"] is False

    def test_a_running_job_is_untouched(self, adapter: BambuAdapter) -> None:
        _push(
            adapter,
            gcode_state="RUNNING",
            gcode_file="now.3mf",
            mc_percent=42,
            mc_remaining_time=30,
        )

        _state, job = adapter.get_status()
        jd = job.to_dict()

        assert job.is_active is True
        assert "active" not in jd
        assert "ended_as" not in jd
        assert jd["print_time_left_seconds"] == 1800

    def test_reconcile_refuses_an_ending_on_a_running_machine(self) -> None:
        """A machine that is running a job has not ended one."""
        state = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        job = JobProgress(file_name="x.3mf", ended_as=JobResult.COMPLETED)

        out = reconcile_job_with_state(state, job)

        assert out.ended_as is None
        assert out.is_active is True

    def test_reconcile_stamps_the_ending_a_stale_state_still_carries(
        self,
    ) -> None:
        """The exact shape the session measured, at the shared helper."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.STALE,
            last_known_state=PrinterStatus.IDLE,
            last_job_result=JobResult.CANCELLED,
            state_age_seconds=MEASURED_STALE_AGE,
        )
        job = JobProgress(
            file_name="jar.3mf", current_layer=1, total_layers=225,
            print_time_left_seconds=14220,
        )

        out = reconcile_job_with_state(state, job)

        assert out.ended_as is JobResult.CANCELLED
        assert out.is_active is False
        assert "print_time_left_seconds" not in out.to_dict()


# ---------------------------------------------------------------------------
# 4. The held job, and the one thing that clears it
# ---------------------------------------------------------------------------


class TestStuckJob:
    """Frozen telemetry plus a finished job has a name and a fix."""

    def test_the_condition_is_detected_and_the_remedy_named(self) -> None:
        state = PrinterState(
            connected=True,
            state=PrinterStatus.STALE,
            last_known_state=PrinterStatus.IDLE,
            last_job_result=JobResult.CANCELLED,
            state_age_seconds=MEASURED_STALE_AGE,
        )
        job = JobProgress(file_name="jar.3mf", ended_as=JobResult.CANCELLED)

        note = stuck_job_note(state, job)

        assert note is not None
        assert "power-cycle" in note.lower()
        assert "load" in note.lower() and "unload" in note.lower()
        assert "1396s" in note

    def test_a_fresh_reading_of_a_finished_job_is_not_stuck(self) -> None:
        """A printer that just finished and is still talking is fine."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            last_job_result=JobResult.COMPLETED,
            state_age_seconds=3.0,
        )
        job = JobProgress(file_name="done.3mf", ended_as=JobResult.COMPLETED)

        assert stuck_job_note(state, job) is None

    def test_a_stale_reading_of_a_running_job_is_not_stuck(self) -> None:
        """Silence during a print is a different problem with a different fix."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.STALE,
            last_known_state=PrinterStatus.PRINTING,
            state_age_seconds=MEASURED_STALE_AGE,
        )
        job = JobProgress(file_name="running.3mf")

        assert stuck_job_note(state, job) is None


# ---------------------------------------------------------------------------
# 5. HMS codes in the form the printer shows
# ---------------------------------------------------------------------------


class TestErrorCodeFormatting:
    """The number Kiln hands back is one a user can look up."""

    def test_the_measured_code_renders_the_way_the_screen_does(self) -> None:
        assert format_error_code(MEASURED_HMS_DECIMAL) == MEASURED_HMS_RENDERED

    def test_zero_and_nonsense_produce_no_code(self) -> None:
        assert format_error_code(0) is None
        assert format_error_code(None) is None
        assert format_error_code("not a number") is None

    def test_the_raw_value_survives_beside_the_formatted_one(
        self, adapter: BambuAdapter
    ) -> None:
        _push(
            adapter, gcode_state="failed", print_error=MEASURED_HMS_DECIMAL
        )

        data = adapter.get_state().to_dict()

        assert data["print_error"] == MEASURED_HMS_DECIMAL
        assert data["print_error_code"] == MEASURED_HMS_RENDERED


# ---------------------------------------------------------------------------
# 6. Four causes, four fixes -- no longer one word
# ---------------------------------------------------------------------------


class TestUnreachableCauses:
    """"Offline" was one word for four problems, so three fixes were wrong."""

    def test_a_powered_off_printer(self) -> None:
        d = diagnose_read_failure(
            "Couldn't reach the printer at 192.0.2.10 — no response within 30s.",
            reachable=False,
        )
        assert d.state is PrinterStatus.OFFLINE
        assert d.cause == CAUSE_POWERED_OFF
        assert "powered on" in d.remedy

    def test_a_wrong_access_code_is_never_called_offline(self) -> None:
        d = diagnose_read_failure("Not authorized: bad access code", reachable=True)
        assert d.state is PrinterStatus.UNAUTHORIZED
        assert d.cause == CAUSE_WRONG_ACCESS_CODE
        assert "access code" in d.remedy.lower()

    def test_leaked_servers_holding_the_slots_name_the_trim_tool(self) -> None:
        """The cause a user cannot guess, and would "fix" by power-cycling."""
        d = diagnose_read_failure(
            "Couldn't reach the printer at 192.0.2.10 — no response within 30s.",
            kiln_slot_holders=5,
            reachable=True,
        )
        assert d.state is PrinterStatus.CONNECTION_LIMIT
        assert d.cause == CAUSE_CONNECTION_LIMIT
        assert "trim_serve_processes" in d.remedy
        assert "Power-cycling the printer will not help" in d.remedy

    def test_reachable_but_silent_is_its_own_answer(self) -> None:
        """Answers on the wire, says nothing -- the measured A1 condition."""
        d = diagnose_read_failure("mqtt disconnected", reachable=True)
        assert d.state is PrinterStatus.STALE
        assert d.cause == CAUSE_SILENT
        assert "power-cycle" in d.remedy.lower()

    def test_credentials_beat_every_other_signal(self) -> None:
        """A refused access code needs no reachability probe to explain it."""
        d = diagnose_read_failure(
            "unauthorized", kiln_slot_holders=9, reachable=False
        )
        assert d.state is PrinterStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# 7. No silent fallthrough
# ---------------------------------------------------------------------------


class TestStateVocabulary:
    """Every state is classified, so a new one cannot slip through a gate."""

    def test_every_status_lands_in_exactly_one_bucket(self) -> None:
        buckets = (
            BUSY_STATES,
            READY_STATES,
            UNREACHABLE_STATES,
            INDETERMINATE_STATES,
        )
        covered: set[PrinterStatus] = set()
        for bucket in buckets:
            assert not (covered & bucket), "a state is in two buckets"
            covered |= bucket
        assert covered == set(PrinterStatus), (
            "every PrinterStatus must be classified — an unclassified member "
            "is exactly the silent fallthrough these buckets exist to stop"
        )

    def test_stale_is_never_ready(self) -> None:
        """An expired reading cannot show a machine is free to take work."""
        assert PrinterStatus.STALE not in READY_STATES
        assert status_is_occupied(PrinterStatus.STALE) is True

    def test_the_new_diagnosed_states_are_unreachable(self) -> None:
        assert PrinterStatus.UNAUTHORIZED in UNREACHABLE_STATES
        assert PrinterStatus.CONNECTION_LIMIT in UNREACHABLE_STATES

    def test_occupancy_reads_through_a_stale_reading(self) -> None:
        printing = PrinterState(
            connected=True,
            state=PrinterStatus.STALE,
            last_known_state=PrinterStatus.PRINTING,
        )
        idle = PrinterState(
            connected=True,
            state=PrinterStatus.STALE,
            last_known_state=PrinterStatus.IDLE,
        )
        unknown = PrinterState(connected=True, state=PrinterStatus.STALE)

        assert printing.is_occupied is True
        assert idle.is_occupied is False
        # Nothing known behind the staleness: assume occupied.  A refused
        # print is a retry; a second print onto an occupied bed is a crash.
        assert unknown.is_occupied is True


# ---------------------------------------------------------------------------
# 8. The tool surface itself — the door the session went through
# ---------------------------------------------------------------------------


class TestPrinterStatusTool:
    """``printer_status`` now says what the web Monitor already said.

    Kiln 1.4.0: "when a reading has gone stale, the Monitor says so, instead
    of presenting old numbers as live."  These pin the tool surface to the
    same promise, off the same shared rule.
    """

    @staticmethod
    def _tool(state: PrinterState, job: JobProgress) -> dict[str, Any]:
        from unittest.mock import MagicMock, patch

        from kiln import server
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = state
        adapter.get_job.return_value = job
        with patch("kiln.server._get_adapter", return_value=adapter):
            return server.printer_status()

    def test_the_measured_reading_leads_with_its_age(self) -> None:
        """state "idle", a full job block, 28 minutes of silence."""
        out = self._tool(
            PrinterState(
                connected=True,
                state=PrinterStatus.STALE,
                last_known_state=PrinterStatus.IDLE,
                last_job_result=JobResult.CANCELLED,
                state_age_seconds=MEASURED_STALE_AGE,
                state_stale_after_seconds=STALE_STATE_WARN_AGE,
                remedy="Check the machine itself before acting on it.",
            ),
            JobProgress(
                file_name="PRINT_ME_jar_v8.3mf",
                completion=0.0,
                current_layer=1,
                total_layers=225,
                print_time_left_seconds=14220,
            ),
        )

        assert out["success"] is True
        # The headline, where a caller reads the state.
        assert out["printer"]["state"] == "stale"
        assert out["printer"]["last_known_state"] == "idle"
        assert out["printer"]["remedy"]
        # The sentence still names what it was doing, and how old it is.
        assert "1396s old" in out["telemetry_warning"]
        assert "IDLE" in out["telemetry_warning"]
        # And the job block beside it no longer contradicts itself.
        assert out["job"]["active"] is False
        assert out["job"]["ended_as"] == "cancelled"
        assert "print_time_left_seconds" not in out["job"]
        # The condition, and the only thing that clears it.
        assert "power-cycle" in out["stuck_job_warning"].lower()

    def test_a_healthy_printer_gets_no_warnings(self) -> None:
        out = self._tool(
            PrinterState(
                connected=True,
                state=PrinterStatus.PRINTING,
                state_age_seconds=2.0,
                state_stale_after_seconds=STALE_STATE_WARN_AGE,
            ),
            JobProgress(file_name="now.3mf", completion=40.0,
                        print_time_left_seconds=1800),
        )

        assert out["printer"]["state"] == "printing"
        assert "telemetry_warning" not in out
        assert "stuck_job_warning" not in out
        assert out["job"]["print_time_left_seconds"] == 1800

    def test_an_unreachable_printer_names_its_cause_and_its_fix(self) -> None:
        from unittest.mock import MagicMock, patch

        from kiln import server
        from kiln.printers.base import PrinterError
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.side_effect = PrinterError(
            "connection refused by 192.0.2.9"
        )
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status()

        assert out["success"] is True
        assert out["printer"]["connected"] is False
        # Still "offline" — but now because it was diagnosed as offline,
        # with the fix that actually matches, not as a catch-all.
        assert out["printer"]["state"] == "offline"
        assert out["printer"]["cause"] == CAUSE_POWERED_OFF
        assert "powered on" in out["remedy"]


# ---------------------------------------------------------------------------
# 9. The reconciliation never breaks a read
# ---------------------------------------------------------------------------


def test_a_duck_typed_job_passes_straight_through() -> None:
    """A status read must not fail because the reconciliation could not run.

    Adapters outside this package (and test doubles) answer ``get_job`` with
    objects that cannot be rebuilt with ``dataclasses.replace``.  Those are
    handed back untouched rather than raising into a status read.
    """
    state = PrinterState(
        connected=True,
        state=PrinterStatus.STALE,
        last_known_state=PrinterStatus.IDLE,
        last_job_result=JobResult.CANCELLED,
    )
    duck = mock.MagicMock()

    assert reconcile_job_with_state(state, duck) is duck
    assert stuck_job_note(state, duck) is None


# ---------------------------------------------------------------------------
# 10. The engine, not the instance
# ---------------------------------------------------------------------------


def test_the_promotion_belongs_to_printer_state_not_to_one_adapter() -> None:
    """Any adapter that supplies an age AND a measured budget gets this.

    The rule lives on :class:`PrinterState`, so the two push-cache adapters
    -- Bambu over MQTT, Elegoo over websocket -- cannot grow two versions of
    it.  Two versions of one rule is how the tool surface and the web
    Monitor came to disagree about a single printer.
    """
    fresh = PrinterState(
        connected=True,
        state=PrinterStatus.PRINTING,
        state_age_seconds=20.0,
        state_stale_after_seconds=60.0,
    )
    expired = PrinterState(
        connected=True,
        state=PrinterStatus.PRINTING,
        state_age_seconds=1396.0,
        state_stale_after_seconds=60.0,
    )

    assert fresh.state is PrinterStatus.PRINTING
    assert expired.state is PrinterStatus.STALE
    assert expired.last_known_state is PrinterStatus.PRINTING
    assert expired.remedy


def test_an_adapter_that_measures_no_age_is_left_alone() -> None:
    """A polling adapter is current by construction; warning it is noise."""
    polled = PrinterState(connected=True, state=PrinterStatus.PRINTING)
    assert polled.state is PrinterStatus.PRINTING
    assert polled.is_stale() is False
    assert polled.staleness_note() is None
    assert "state_stale_after_seconds" not in polled.to_dict()


def test_an_diagnosed_state_is_never_re_promoted() -> None:
    """OFFLINE with a diagnosis stays OFFLINE — it is not a stale reading."""
    off = PrinterState(
        connected=False,
        state=PrinterStatus.OFFLINE,
        state_age_seconds=9_999.0,
        state_stale_after_seconds=60.0,
        cause=CAUSE_POWERED_OFF,
        remedy="check the power switch",
    )
    assert off.state is PrinterStatus.OFFLINE
    assert off.cause == CAUSE_POWERED_OFF


def test_the_elegoo_adapter_gets_the_same_headline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same failure shape, same fix, no second copy of the rule."""
    from kiln.printers.elegoo import ElegooAdapter

    a = ElegooAdapter(host="192.0.2.11", timeout=2)
    a._handle_message({"Status": {"CurrentStatus": 13}})  # printing
    a._print_state_time -= MEASURED_STALE_AGE
    a._last_state_time -= MEASURED_STALE_AGE

    with mock.patch.object(a, "_ensure_ws"), mock.patch.object(
        a, "_send_command", return_value=None
    ):
        state = a.get_state()

    assert state.state is PrinterStatus.STALE
    assert state.effective_state is PrinterStatus.PRINTING
    assert state.is_occupied is True


# ---------------------------------------------------------------------------
# 11. One membership list, not one per surface
# ---------------------------------------------------------------------------


class TestStatusWordClassifiers:
    """Fleet listings carry the state as a word, and used to keep their own
    sets of which words mean busy.  Those sets are how a new member reaches
    half the product; these are the one conversion they all go through."""

    def test_a_word_classifies_the_same_as_its_member(self) -> None:
        from kiln.printers.base import as_status, status_is_unreachable

        for member in PrinterStatus:
            assert as_status(member.value) is member
            assert status_is_occupied(member.value) is status_is_occupied(member)
            assert status_is_unreachable(member.value) is status_is_unreachable(
                member
            )

    def test_the_words_a_fleet_listing_must_not_call_free(self) -> None:
        assert status_is_occupied("stale") is True
        assert status_is_occupied("printing") is True
        assert status_is_occupied("idle") is False

    def test_every_unreachable_cause_reads_as_unreachable(self) -> None:
        from kiln.printers.base import status_is_unreachable

        assert status_is_unreachable("offline") is True
        assert status_is_unreachable("unauthorized") is True
        assert status_is_unreachable("connection_limit") is True
        # A stale printer is CONNECTED — calling it unreachable would send
        # the user to the wrong fix.
        assert status_is_unreachable("stale") is False

    def test_an_unknown_word_is_not_silently_classified(self) -> None:
        from kiln.printers.base import as_status, status_is_unreachable

        assert as_status("banana") is None
        assert status_is_occupied("banana") is False
        assert status_is_unreachable("banana") is False


def test_the_cooldown_ceiling_follows_the_measured_budget(
    adapter: BambuAdapter,
) -> None:
    """Two clocks, one budget.

    "Is this cache still worth serving" and "is this reading still
    evidence" are the same question; answering them with two different
    numbers is the drift the single budget exists to prevent.
    """
    _push(adapter, gcode_state="IDLE")
    with adapter._state_lock:
        for tick in range(8):
            adapter._cadence.record(tick * 120.0)
    adapter._backoff.record_failure()
    assert adapter._backoff.in_cooldown()
    # 200 s: past the old fixed 60 s ceiling, inside the measured 300 s one.
    _age(adapter, 200.0)

    state = adapter.get_state()

    assert state.connected is True          # still served, not a bare OFFLINE
    assert state.state is PrinterStatus.IDLE


def test_every_name_the_printers_package_advertises_resolves() -> None:
    """``__all__`` is a promise other modules import against.

    A helper added to ``base`` and left out of the package's re-exports is
    invisible to ruff and to every test that imports from ``base``
    directly -- and it breaks ``import kiln.server`` outright, because that
    is where the tool layer imports from.  This is the cheap guard for it.
    """
    import kiln.printers as pkg

    missing = [name for name in pkg.__all__ if not hasattr(pkg, name)]
    assert not missing, f"advertised but not importable: {missing}"

    # The vocabulary this change added, reachable from the package the tool
    # layer actually imports from — not only from `base`.
    for name in (
        "as_status",
        "status_is_occupied",
        "status_is_unreachable",
        "reconcile_job_with_state",
        "read_status",
        "stuck_job_note",
        "diagnose_read_failure",
        "format_error_code",
        "TelemetryCadence",
    ):
        assert hasattr(pkg, name), f"kiln.printers is missing {name}"


class TestStaleIsEarnedNotAssumed:
    """Before calling a reading stale, ask the printer once more.

    The cheapest fix for the common case — a dropped push nobody
    retransmitted — is one publish.  Doing it first turns STALE from "nothing
    arrived on its own" into "we asked again and it still said nothing",
    which is a different and much stronger claim.
    """

    @staticmethod
    def _pushalls(adapter: BambuAdapter) -> int:
        return sum(
            1
            for call in adapter._mqtt_client.publish.call_args_list
            if "pushall" in str(call)
        )

    def test_an_expired_cache_is_re_asked(self, adapter: BambuAdapter) -> None:
        _push(adapter, gcode_state="IDLE")
        _age(adapter, MEASURED_STALE_AGE)
        before = self._pushalls(adapter)

        adapter.get_state()

        assert self._pushalls(adapter) == before + 1

    def test_a_fresh_cache_is_left_alone(self, adapter: BambuAdapter) -> None:
        """No extra traffic on a printer that is answering normally."""
        _push(adapter, gcode_state="RUNNING")
        before = self._pushalls(adapter)

        adapter.get_state()

        assert self._pushalls(adapter) == before

    def test_a_wedged_printer_is_asked_once_not_every_poll(
        self, adapter: BambuAdapter
    ) -> None:
        """The measured case: it will not answer, so stop republishing at it."""
        _push(adapter, gcode_state="IDLE")
        _age(adapter, MEASURED_STALE_AGE)
        before = self._pushalls(adapter)

        for _ in range(5):
            state = adapter.get_state()

        assert self._pushalls(adapter) == before + 1
        # ...and it is still reported honestly as stale.
        assert state.state is PrinterStatus.STALE


# ---------------------------------------------------------------------------
# 12. The string-keyed sets that live outside the printers package
# ---------------------------------------------------------------------------


class TestBusyStateSetsAgree:
    """Four modules keep their own set of "these words mean busy".

    They exist for good reasons — each is consulted somewhere the whole
    PrinterState is not in hand — but a state word that reaches only some
    of them is worse than one that reaches none, because the disagreement
    is invisible.  These pin every one of them to the shared classifier.

    Two of the four are safety gates, and both failed OPEN before this:
    a stale printer read as "nothing is printing", so a trim could have
    killed the server watching a live print, and the router could have sent
    a second job to a bed that already had one.
    """

    def test_the_trim_guard_will_not_kill_a_watch_on_a_stale_printer(
        self,
    ) -> None:
        from kiln.serve_siblings import _ACTIVE_PRINT_STATES

        assert "stale" in _ACTIVE_PRINT_STATES
        for word in _ACTIVE_PRINT_STATES:
            assert status_is_occupied(word), word

    def test_the_router_will_not_send_a_job_to_a_stale_printer(self) -> None:
        from kiln.routing_candidates import _BUSY_STATES

        assert "stale" in _BUSY_STATES
        for word in _BUSY_STATES:
            assert status_is_occupied(word), word

    def test_the_camera_gate_fails_closed_on_a_stale_reading(self) -> None:
        from kiln.monitor_payload import is_active_print_state

        assert is_active_print_state("stale") is True
        assert is_active_print_state("idle") is False

    def test_every_state_the_cli_can_print_has_a_colour(self) -> None:
        from kiln.cli.output import _STATE_COLORS

        missing = [
            m.value for m in PrinterStatus if m.value not in _STATE_COLORS
        ]
        assert not missing, f"uncoloured states: {missing}"
