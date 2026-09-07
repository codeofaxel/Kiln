"""A temperature Kiln cannot vouch for is never stated as the temperature.

The incident (2026-09-06, a Bambu A1, a person with no gloves): a hotend
clog was being cleared and the user asked whether it was safe to start
pulling parts.  ``set_temperature``'s advisory string read "Large hotend
temperature change: 38°C -> 0°C".  The 38 was a target read from a push
cache of unknown vintage; the printer's own screen read 110°C.  On the
strength of that 38 the agent said the hotend had cooled.  The user caught
it by looking at the display.

Every rule below follows from one fact about the failure: the caveat
machinery already existed -- ``state_age_seconds``, the STALE promotion,
the "describes then, not now" sentence -- and the number still got quoted,
because the number was still THERE, as a bare float in a field named
``tool_temp_target``.  A number with a caveat beside it is a number a
person will act on.  So the floor is not a better caveat; it is that the
field is empty.

The rule lives in ONE place, :meth:`PrinterState.__post_init__`, so every
door that reads a temperature -- the status tool, the monitor, the CLI,
the health monitor, the heater watchdog, the hosted panels, and any code
not written yet -- is covered by construction and cannot opt out.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.base import (
    STALE_STATE_WARN_AGE,
    JobProgress,
    PrinterState,
    PrinterStatus,
)

# The six fields that carry a temperature a person might act on.
TEMPERATURE_FIELDS = (
    "tool_temp_actual",
    "tool_temp_target",
    "bed_temp_actual",
    "bed_temp_target",
    "chamber_temp_actual",
    "chamber_temp_target",
)

# The incident's numbers.  The target Kiln quoted, and the age the cache
# had reached in the same session.
INCIDENT_CACHED_TARGET = 38.0
INCIDENT_AGE = 1743.0
INCIDENT_BUDGET = 300.0


def _stale_state(**overrides: Any) -> PrinterState:
    """The incident reading: every temperature populated, the cache expired."""
    fields: dict[str, Any] = dict(
        connected=True,
        state=PrinterStatus.IDLE,
        tool_temp_actual=INCIDENT_CACHED_TARGET,
        tool_temp_target=INCIDENT_CACHED_TARGET,
        bed_temp_actual=25.0,
        bed_temp_target=45.0,
        chamber_temp_actual=29.0,
        chamber_temp_target=35.0,
        state_age_seconds=INCIDENT_AGE,
        state_stale_after_seconds=INCIDENT_BUDGET,
    )
    fields.update(overrides)
    return PrinterState(**fields)


def _fresh_state(**overrides: Any) -> PrinterState:
    fields: dict[str, Any] = dict(
        connected=True,
        state=PrinterStatus.IDLE,
        tool_temp_actual=INCIDENT_CACHED_TARGET,
        tool_temp_target=INCIDENT_CACHED_TARGET,
        bed_temp_actual=25.0,
        bed_temp_target=45.0,
        state_age_seconds=2.0,
        state_stale_after_seconds=INCIDENT_BUDGET,
    )
    fields.update(overrides)
    return PrinterState(**fields)


# ---------------------------------------------------------------------------
# 1. The rule, at the type
# ---------------------------------------------------------------------------


class TestAStaleReadingCarriesNoTemperatures:
    def test_every_temperature_field_is_empty(self) -> None:
        state = _stale_state()

        assert state.state is PrinterStatus.STALE
        for field in TEMPERATURE_FIELDS:
            assert getattr(state, field) is None, field

    def test_it_says_it_does_not_know_and_names_the_display(self) -> None:
        state = _stale_state()

        note = state.temperature_note
        assert note is not None
        assert "does not know" in note
        assert "display" in note
        # The age is the evidence; the reader sees the rule being applied.
        assert f"{INCIDENT_AGE:.0f}s" in note

    def test_the_serialised_form_carries_the_sentence_and_no_number(self) -> None:
        data = _stale_state().to_dict()

        assert data["temperature_note"]
        for field in TEMPERATURE_FIELDS:
            assert data[field] is None, field
        # Not even in the text of the payload: the incident number is gone.
        assert str(int(INCIDENT_CACHED_TARGET)) not in json.dumps(data)

    def test_a_stale_temperature_cannot_be_rendered_as_a_bare_current_value(
        self,
    ) -> None:
        """The property the whole design rests on.

        Every door in the codebase renders a temperature with an f-string
        format spec.  On a stale reading that expression cannot succeed:
        there is no number to format, and Python refuses to format
        ``None`` with ``.0f``.  A door that forgets the guard crashes in
        its own test; it does not print 38.
        """
        state = _stale_state()

        for field in TEMPERATURE_FIELDS:
            value = getattr(state, field)
            with pytest.raises(TypeError):
                f"{value:.0f}°C"  # noqa: B018 -- the raise IS the assertion

    def test_a_fresh_reading_is_untouched(self) -> None:
        state = _fresh_state()

        assert state.state is PrinterStatus.IDLE
        assert state.tool_temp_actual == INCIDENT_CACHED_TARGET
        assert state.tool_temp_target == INCIDENT_CACHED_TARGET
        assert state.bed_temp_actual == 25.0
        assert state.temperature_note is None
        assert "temperature_note" not in state.to_dict()

    def test_a_reading_at_the_budget_is_still_fresh(self) -> None:
        """The budget is inclusive, exactly as the STALE promotion's is."""
        state = _fresh_state(state_age_seconds=INCIDENT_BUDGET)

        assert state.tool_temp_actual == INCIDENT_CACHED_TARGET
        assert state.temperature_note is None


class TestTheRuleIsTotal:
    """No route into a PrinterState leaves a temperature Kiln cannot vouch for."""

    def test_a_bare_age_with_no_measured_budget_does_NOT_blank(self) -> None:
        """The floor fires on a verdict, never on a bare age.

        Regression, found on real hardware 2026-09-06.  An idle Klipper
        stamps its run-state clock only when a push carries ``print_stats``,
        and Klipper subscriptions send deltas -- so that clock climbs past
        the 60s fallback for ever on a perfectly healthy machine while its
        temperatures keep arriving every few seconds.  A Bambu A1 showed the
        same split directly: one reading carried a run state 200s old beside
        a bed temperature 2s old.

        Blanking here would hide live readings on a healthy printer, and a
        floor that cries wolf teaches people to ignore the one blanking that
        matters.  The prose warning still fires; the numbers stay.
        """
        past = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=200.0,
            tool_temp_target=200.0,
            bed_temp_actual=60.0,
            state_age_seconds=STALE_STATE_WARN_AGE + 1.0,
        )
        assert past.state is PrinterStatus.IDLE  # the run state is not rewritten
        assert past.staleness_note() is not None  # the sentence still fires...
        assert past.tool_temp_actual == 200.0  # ...but the readings survive
        assert past.bed_temp_actual == 60.0
        assert past.temperature_note is None

    def test_the_stale_sentence_never_claims_the_temperature_is_unknown(
        self,
    ) -> None:
        """Because on that path it usually is not.

        ``describe_stale_state`` fires on the bare age above, where the
        readings are still live.  A temperature claim in that sentence would
        be false exactly where it is loudest.
        """
        note = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=200.0,
            state_age_seconds=STALE_STATE_WARN_AGE + 1.0,
        ).staleness_note()

        assert note is not None
        assert "temperature" not in note.lower()

    def test_a_measured_budget_is_what_earns_the_blanking(self) -> None:
        """The same age, plus a budget the adapter measured, does blank.

        That is the promotion path, and on a push adapter it means the
        printer was ASKED at budget expiry and did not answer.
        """
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=200.0,
            bed_temp_actual=60.0,
            state_age_seconds=STALE_STATE_WARN_AGE + 1.0,
            state_stale_after_seconds=STALE_STATE_WARN_AGE,
        )

        assert state.state is PrinterStatus.STALE
        assert state.tool_temp_actual is None
        assert state.temperature_note is not None

    def test_a_fresh_reading_within_the_floor_is_untouched(self) -> None:
        within = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=200.0,
            state_age_seconds=STALE_STATE_WARN_AGE - 1.0,
        )
        assert within.tool_temp_actual == 200.0
        assert within.temperature_note is None

    def test_a_state_built_as_stale_by_hand(self) -> None:
        """An adapter that names STALE itself, with no age to show for it."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.STALE,
            tool_temp_actual=INCIDENT_CACHED_TARGET,
            bed_temp_target=60.0,
        )

        assert state.tool_temp_actual is None
        assert state.bed_temp_target is None
        assert state.temperature_note is not None
        assert "does not know" in state.temperature_note

    def test_a_disconnected_reading(self) -> None:
        """No connection, no temperatures -- whatever a builder passed in."""
        state = PrinterState(
            connected=False,
            state=PrinterStatus.OFFLINE,
            tool_temp_actual=210.0,
            bed_temp_actual=60.0,
        )

        assert state.tool_temp_actual is None
        assert state.bed_temp_actual is None

    def test_no_age_means_current_by_construction(self) -> None:
        """A polling adapter asks the printer on every call; it sets no age."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=24.5,
            bed_temp_actual=23.1,
        )

        assert state.tool_temp_actual == 24.5
        assert state.temperature_note is None


# ---------------------------------------------------------------------------
# 2. The adapter that produced the incident
# ---------------------------------------------------------------------------

HOST = "192.0.2.10"
ACCESS_CODE = "12345678"
SERIAL = "TEST1234567890"


@pytest.fixture
def bambu(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"))
    a = BambuAdapter(host=HOST, access_code=ACCESS_CODE, serial=SERIAL, timeout=2)
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    a._mqtt_client.publish.return_value = publish_result
    return a


def _push(adapter: Any, **fields: Any) -> None:
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _age(adapter: Any, seconds: float) -> None:
    adapter._gcode_state_time -= seconds
    adapter._last_state_time -= seconds


class TestTheBambuCache:
    def test_the_incident_reading_carries_no_target_to_quote(self, bambu: Any) -> None:
        """The cache still holds 38; the reading the tool sees does not."""
        _push(
            bambu,
            gcode_state="IDLE",
            nozzle_temper=INCIDENT_CACHED_TARGET,
            nozzle_target_temper=INCIDENT_CACHED_TARGET,
            bed_temper=25,
            bed_target_temper=0,
        )
        _age(bambu, INCIDENT_AGE)

        state = bambu.get_state()

        assert state.state is PrinterStatus.STALE
        assert bambu._last_status["nozzle_target_temper"] == INCIDENT_CACHED_TARGET
        assert state.tool_temp_target is None
        assert state.tool_temp_actual is None
        assert state.bed_temp_actual is None
        assert state.temperature_note is not None

    def test_a_live_session_still_reports_its_temperatures(self, bambu: Any) -> None:
        _push(bambu, gcode_state="IDLE", nozzle_temper=24, nozzle_target_temper=0)
        _age(bambu, 5.0)

        state = bambu.get_state()

        assert state.state is PrinterStatus.IDLE
        assert state.tool_temp_actual == 24
        assert state.temperature_note is None


# ---------------------------------------------------------------------------
# 3. The doors
# ---------------------------------------------------------------------------


def _octoprint_adapter(state: PrinterState, job: JobProgress | None = None) -> Any:
    from kiln.printers.octoprint import OctoPrintAdapter

    adapter = MagicMock(spec=OctoPrintAdapter)
    adapter.get_state.return_value = state
    adapter.get_job.return_value = job or JobProgress()
    adapter.get_snapshot.return_value = None
    adapter.set_tool_temp.return_value = True
    adapter.set_bed_temp.return_value = True
    return adapter


class TestSetTemperature:
    """The door that produced the incident string."""

    def test_the_incident_string_cannot_be_produced(self) -> None:
        from kiln.server import set_temperature

        adapter = _octoprint_adapter(_stale_state())
        with (
            patch("kiln.server._get_adapter", return_value=adapter),
            patch("kiln.server._check_rate_limit", return_value=None),
        ):
            result = set_temperature(tool_temp=0.0)

        assert result["success"] is True, result
        warnings = result.get("warnings", [])
        text = " ".join(str(w) for w in warnings)
        assert "38°C" not in text
        assert "temperature change" not in text
        assert "->" not in text  # no before/after pair at all
        # And the response says why there is no comparison to make.
        assert any("does not know" in w for w in warnings)

    def test_a_fresh_advisory_names_the_number_as_a_target(self) -> None:
        """Even fresh, the old wording presented a setpoint as a temperature."""
        from kiln.server import set_temperature

        adapter = _octoprint_adapter(_fresh_state(tool_temp_target=220.0))
        with (
            patch("kiln.server._get_adapter", return_value=adapter),
            patch("kiln.server._check_rate_limit", return_value=None),
        ):
            result = set_temperature(tool_temp=0.0)

        warnings = result.get("warnings", [])
        assert warnings, result
        assert "target" in warnings[0]
        assert "220°C -> 0°C" in warnings[0]
        assert "temperature change" not in warnings[0]


class TestPrinterStatus:
    def test_the_payload_carries_the_sentence_and_no_number(self) -> None:
        from kiln import server

        adapter = _octoprint_adapter(_stale_state())
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status()

        assert out["success"] is True
        assert out["printer"]["state"] == "stale"
        for field in TEMPERATURE_FIELDS:
            assert out["printer"].get(field) is None, field
        assert "does not know" in out["printer"]["temperature_note"]
        # Promoted beside the telemetry warning, where an agent reads warnings.
        assert "does not know" in out["temperature_warning"]
        # The printer block is the serialised PrinterState; the number is
        # not in it, in any field, under any name.
        assert str(int(INCIDENT_CACHED_TARGET)) not in json.dumps(out["printer"])

    def test_the_lite_reading_keeps_the_sentence(self) -> None:
        """Lite is the polled level -- the one a frozen cache is read through."""
        from kiln import server

        adapter = _octoprint_adapter(_stale_state())
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status(detail="lite")

        assert out["printer"].get("tool_temp_actual") is None
        assert "does not know" in out["printer"]["temperature_note"]

    def test_a_fresh_reading_has_no_temperature_warning(self) -> None:
        from kiln import server

        adapter = _octoprint_adapter(_fresh_state())
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status()

        assert out["printer"]["tool_temp_actual"] == INCIDENT_CACHED_TARGET
        assert "temperature_warning" not in out
        assert "temperature_note" not in out["printer"]


class TestMonitorPrint:
    def test_the_report_says_unknown_not_a_number(self) -> None:
        from kiln.server import monitor_print

        adapter = _octoprint_adapter(
            _stale_state(state=PrinterStatus.PRINTING),
            JobProgress(file_name="part.3mf", completion=12.0),
        )
        with patch("kiln.server._get_adapter", return_value=adapter):
            report = monitor_print(include_snapshot=False)

        assert "38°C" not in report
        assert "does not know" in report
        # The nozzle and bed lines say so in their own words, not "N/A",
        # which reads as "this printer does not report it".
        nozzle_line = next(line for line in report.splitlines() if line.startswith("- Nozzle:"))
        bed_line = next(line for line in report.splitlines() if line.startswith("- Bed:"))
        assert "unknown" in nozzle_line
        assert "unknown" in bed_line

    def test_a_fresh_report_still_prints_the_numbers(self) -> None:
        from kiln.server import monitor_print

        adapter = _octoprint_adapter(
            _fresh_state(state=PrinterStatus.PRINTING, tool_temp_actual=215.0, tool_temp_target=220.0),
            JobProgress(file_name="part.3mf", completion=12.0),
        )
        with patch("kiln.server._get_adapter", return_value=adapter):
            report = monitor_print(include_snapshot=False)

        assert "215°C → 220°C target" in report
        assert "does not know" not in report


class TestTheCli:
    def test_status_says_unknown_and_shows_the_sentence(self) -> None:
        from kiln.cli.output import format_status

        state = _stale_state().to_dict()
        out = format_status(state, JobProgress().to_dict(), json_mode=False)

        assert "38" not in out
        assert "does not know" in out

    def test_two_unknowns_are_not_an_off_heater(self) -> None:
        """``N/A → off`` claimed the heater was off.  Nobody knows that."""
        from kiln.cli.output import format_temp

        assert format_temp(None, None) == "unknown"
        # A reading with an actual and no target still means what it meant.
        assert format_temp(60.0, None) == "60.0°C → off"


class TestTheHeaterWatchdog:
    """A blank target is not an OFF heater."""

    def test_an_unknown_target_does_not_clear_the_idle_heat_tracker(self) -> None:
        from kiln.heater_watchdog import HeaterWatchdog

        adapter = _octoprint_adapter(_stale_state(tool_temp_target=220.0))
        wd = HeaterWatchdog(
            get_adapter=lambda: adapter,
            timeout_minutes=0.0001,
            poll_interval=0.1,
        )
        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        # Nothing commanded on a printer Kiln cannot hear...
        adapter.set_tool_temp.assert_not_called()
        adapter.set_bed_temp.assert_not_called()
        # ...and the watch is NOT stood down: the heater may still be on.
        assert wd._last_heater_activity is not None

    def test_a_fresh_off_reading_still_clears_it(self) -> None:
        from kiln.heater_watchdog import HeaterWatchdog

        adapter = _octoprint_adapter(_fresh_state(tool_temp_target=0.0, bed_temp_target=0.0))
        wd = HeaterWatchdog(
            get_adapter=lambda: adapter,
            timeout_minutes=0.0001,
            poll_interval=0.1,
        )
        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        assert wd._last_heater_activity is None
