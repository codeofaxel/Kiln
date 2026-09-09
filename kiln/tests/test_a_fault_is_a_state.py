"""A fault the printer is reporting is the machine's state, not a field.

Measured on a Bambu A1 (2026-09-07), verified live:

  * ``printer_status`` returned ``state: "idle"`` while the SAME payload
    carried ``print_error: 302022663`` and ``print_error_code: "1200-8007"``
    -- "failed to extrude the filament".  The printer's own screen was
    holding a modal error dialog at that moment.  Anything reading the
    headline saw a healthy, ready machine; the contradiction underneath was
    visible only to a reader who already knew to look for it.
  * The fault came from a filament load the user started at the printer's
    own touchscreen, which failed at step 5.  Kiln held the MQTT connection
    throughout and had already parsed the code.  It said nothing, because
    the watchdog attaches only to prints Kiln itself starts -- so the
    ``kiln_watch`` block reported watchdog unattached, no health session and
    no watch, and the fault sat in the data for several minutes until the
    user mentioned it.

The shape of the fix is the one the two neighbouring defects already
settled.  A fact that decides what the reader should do next takes the
HEADLINE, and the fact it displaces is kept rather than destroyed: the
stale-job fix moved the run state to ``last_known_state`` and led with the
age, and the temperature floor emptied a field it could not vouch for and
said why.  Here the headline becomes ``error``, the run state moves to
``last_known_state`` so every occupancy gate still reads it, and a sentence
names the fault in words.

Deliberately NOT a new ``PrinterStatus`` member: the vocabulary already has
the word for "this machine is reporting something wrong", and every gate
already treats it correctly.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import (
    BUSY_STATES,
    READY_STATES,
    JobProgress,
    PrinterState,
    PrinterStatus,
    reconcile_job_with_state,
)

HOST = "192.0.2.10"
ACCESS_CODE = "12345678"
SERIAL = "TEST1234567890"

#: The measured reading: the decimal the firmware publishes, and the form
#: the printer's own screen renders it in.
MEASURED_FAULT_DECIMAL = 302022663
MEASURED_FAULT_RENDERED = "1200-8007"


@pytest.fixture
def adapter(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> BambuAdapter:
    """A Bambu adapter with a mocked-connected MQTT client."""
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"))
    a = BambuAdapter(host=HOST, access_code=ACCESS_CODE, serial=SERIAL, timeout=2)
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    publish_result.rc = mqtt.MQTT_ERR_SUCCESS
    a._mqtt_client.publish.return_value = publish_result
    a._confirm_window_s = 0.0
    return a


def _push(adapter: BambuAdapter, **fields: Any) -> None:
    """Feed *fields* in as a real ``push_status`` message."""
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _join_fault_notices(timeout: float = 5.0) -> None:
    """Wait for the off-thread fault notices to finish publishing.

    The notice is deliberately not published on the MQTT callback thread
    (see ``BambuAdapter._notice_fault``), so a test that asserts on what was
    published has to wait for the thread that publishes it.
    """
    import threading

    for t in threading.enumerate():
        if t.name == "kiln-fault-notice":
            t.join(timeout)


# ---------------------------------------------------------------------------
# 1. The measured reading -- the headline stops saying "idle"
# ---------------------------------------------------------------------------


class TestTheMeasuredFault:
    """The exact payload the A1 served on 2026-09-07."""

    def test_the_measured_fault_is_not_reported_as_idle(
        self, adapter: BambuAdapter
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        state = adapter.get_state()

        assert state.state is not PrinterStatus.IDLE
        assert state.state is PrinterStatus.ERROR

    def test_a_reader_of_the_headline_alone_learns_of_the_fault(
        self, adapter: BambuAdapter
    ) -> None:
        """The whole defect in one assertion: no digging required."""
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        headline = adapter.get_state().to_dict()["state"]

        assert headline == "error"

    def test_the_fault_is_named_in_words_beside_the_code(
        self, adapter: BambuAdapter
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        note = adapter.get_state().fault_note

        assert note
        # The code a person can look up...
        assert MEASURED_FAULT_RENDERED in note
        # ...this firmware's own reading of it...
        assert "nozzle" in note.lower()
        # ...and the one thing that clears it.
        assert "clear" in note.lower()

    def test_the_good_half_is_not_regressed(self, adapter: BambuAdapter) -> None:
        """The raw value and the rendered code both survive the promotion."""
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        data = adapter.get_state().to_dict()

        assert data["print_error"] == MEASURED_FAULT_DECIMAL
        assert data["print_error_code"] == MEASURED_FAULT_RENDERED

    def test_a_printer_with_no_fault_is_untouched(
        self, adapter: BambuAdapter
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=0)

        state = adapter.get_state()

        assert state.state is PrinterStatus.IDLE
        assert state.fault_note is None
        assert "fault_note" not in state.to_dict()


# ---------------------------------------------------------------------------
# 2. The displaced run state is kept, not destroyed
# ---------------------------------------------------------------------------


class TestTheRunStateSurvives:
    """The gates that ask "what is it doing" must still get an answer."""

    def test_the_run_state_moves_under_the_headline(
        self, adapter: BambuAdapter
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        state = adapter.get_state()

        assert state.last_known_state is PrinterStatus.IDLE
        assert state.effective_state is PrinterStatus.IDLE

    def test_a_fault_mid_print_does_not_free_the_bed(
        self, adapter: BambuAdapter
    ) -> None:
        """The one that would have been a crash.

        A machine that raises a fault while printing is still printing.  A
        router reading the bare ERROR headline -- which is not in
        ``BUSY_STATES`` -- would have called the bed free and sent it a
        second job.
        """
        _push(adapter, gcode_state="running", print_error=MEASURED_FAULT_DECIMAL)

        state = adapter.get_state()

        assert state.state is PrinterStatus.ERROR
        assert state.effective_state is PrinterStatus.PRINTING
        assert state.is_occupied is True

    def test_a_faulted_idle_machine_is_not_occupied(
        self, adapter: BambuAdapter
    ) -> None:
        """The other direction: a fault is not a reason to claim work."""
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        assert adapter.get_state().is_occupied is False

    def test_a_faulted_machine_is_never_ready(self, adapter: BambuAdapter) -> None:
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        assert adapter.get_state().state not in READY_STATES

    def test_the_job_block_still_reads_as_live_during_a_faulted_print(self) -> None:
        """Reconciliation asks the run state, so it must see through this."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            print_error=MEASURED_FAULT_DECIMAL,
        )
        job = JobProgress(file_name="plate_1.3mf", completion=42.0)

        out = reconcile_job_with_state(state, job)

        assert state.state is PrinterStatus.ERROR
        assert state.effective_state in BUSY_STATES
        assert out.ended_as is None


# ---------------------------------------------------------------------------
# 3. The promotion is the engine's, and it knows what it may not outrank
# ---------------------------------------------------------------------------


class TestThePromotionRules:
    """One rule, in PrinterState, for every adapter that reports a code."""

    def test_the_promotion_belongs_to_printer_state_not_to_one_adapter(self) -> None:
        """Any adapter reporting a fault gets it, with no code of its own."""
        state = PrinterState(
            connected=True, state=PrinterStatus.IDLE, print_error=MEASURED_FAULT_DECIMAL
        )

        assert state.state is PrinterStatus.ERROR
        assert state.last_known_state is PrinterStatus.IDLE
        # No adapter reading supplied: the generic sentence, never a guess at
        # what this firmware's code means.
        assert state.fault_note
        assert MEASURED_FAULT_RENDERED in state.fault_note

    def test_an_adapters_own_reading_is_kept(self) -> None:
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            print_error=MEASURED_FAULT_DECIMAL,
            fault_note="the adapter's own words",
        )

        assert state.fault_note == "the adapter's own words"

    def test_staleness_outranks_a_fault(self) -> None:
        """A reading Kiln cannot vouch for cannot vouch for its fault either.

        And the concrete cost of getting the order wrong: promoting first
        would let the staleness promotion overwrite ``last_known_state`` with
        ERROR, dropping the PRINTING underneath and freeing an occupied bed.
        """
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            print_error=MEASURED_FAULT_DECIMAL,
            state_age_seconds=1396.0,
            state_stale_after_seconds=60.0,
        )

        assert state.state is PrinterStatus.STALE
        assert state.last_known_state is PrinterStatus.PRINTING
        assert state.is_occupied is True
        # The code is not lost, it is just not the headline.
        assert state.print_error == MEASURED_FAULT_DECIMAL

    def test_an_unreachable_printer_is_not_relabelled(self) -> None:
        """Its cause and remedy already say what is wrong."""
        state = PrinterState(
            connected=False,
            state=PrinterStatus.OFFLINE,
            print_error=MEASURED_FAULT_DECIMAL,
        )

        assert state.state is PrinterStatus.OFFLINE

    def test_a_firmware_error_state_gets_the_sentence_too(self) -> None:
        """However the ERROR arrived, a reader gets the same words."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.ERROR,
            print_error=MEASURED_FAULT_DECIMAL,
        )

        assert state.state is PrinterStatus.ERROR
        # Nothing was displaced, so nothing is claimed to have been.
        assert state.last_known_state is None
        assert state.fault_note

    def test_the_post_cancel_idle_downgrade_still_works(
        self, adapter: BambuAdapter
    ) -> None:
        """``failed`` with no code is a cancel, and must stay printable."""
        _push(adapter, gcode_state="failed", print_error=0)

        state = adapter.get_state()

        assert state.state is PrinterStatus.IDLE
        assert state.fault_note is None


# ---------------------------------------------------------------------------
# 4. Every door, not only the one the user knocked on
# ---------------------------------------------------------------------------


class TestEveryDoor:
    """A fix at one door is how the bug survives at the others."""

    def _faulted(self) -> PrinterState:
        """The measured payload: an idle run state with a live fault code.

        Built through the promotion rather than by handing in a ready-made
        note, so each door below is exercised on what an adapter actually
        produces.
        """
        return PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            print_error=MEASURED_FAULT_DECIMAL,
        )

    @pytest.mark.parametrize("detail", ["full", "lite"])
    def test_printer_status_leads_with_the_fault(self, detail: str) -> None:
        from unittest.mock import MagicMock, patch

        from kiln import server

        adapter = MagicMock()
        adapter.get_state.return_value = self._faulted()
        adapter.get_job.return_value = JobProgress()
        adapter.capabilities.to_dict.return_value = {}

        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status(detail=detail)

        assert out["printer"]["state"] == "error"
        # The sentence survives the lite trim -- lite is what a live poller
        # reads, so it is where a bare "error" does its damage.
        assert out["printer"]["fault_note"]
        assert out["fault_warning"]

    def test_the_cli_prints_the_fault_under_the_state(self) -> None:
        from kiln.cli.output import format_status

        text = format_status(self._faulted().to_dict(), {}, json_mode=False)

        assert "error" in text
        assert "1200-8007" in text

    def test_preflight_refuses_a_faulted_printer(self) -> None:
        """A latched extrusion fault is not a machine to start a print on."""
        from unittest.mock import MagicMock, patch

        from kiln import server

        adapter = MagicMock()
        adapter.get_state.return_value = self._faulted()
        adapter.get_job.return_value = JobProgress()

        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.preflight_check()

        named = {c["name"]: c for c in out.get("checks", [])}
        assert named["no_errors"]["passed"] is False

# ---------------------------------------------------------------------------
# 5. Kiln notices a fault on a machine it did not start the job on
# ---------------------------------------------------------------------------


class TestKilnNoticesWhatItDidNotStart:
    """"could u not see this if i didnt mention it to u?"

    The answer was: yes, but only if asked.  What Kiln does about that is
    deliberately bounded.  It REPORTS -- on every read, and once on the
    fault's leading edge -- and it does not act: the watchdog stays attached
    only to prints Kiln started, because a job a person began at the
    touchscreen is theirs to stop.
    """

    def test_a_fault_on_a_hand_started_job_is_noticed_once(
        self, adapter: BambuAdapter
    ) -> None:
        noticed: list[tuple[int, str]] = []
        adapter._notice_fault = lambda code, name: noticed.append((code, name))

        # The load the user started at the touchscreen, failing at its purge
        # step, then continuing to report the same latched code.
        _push(adapter, gcode_state="idle", print_error=0)
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        assert len(noticed) == 1
        assert noticed[0][0] == MEASURED_FAULT_DECIMAL

    def test_a_new_fault_after_the_first_is_noticed_too(
        self, adapter: BambuAdapter
    ) -> None:
        noticed: list[tuple[int, str]] = []
        adapter._notice_fault = lambda code, name: noticed.append((code, name))

        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)
        _push(adapter, gcode_state="idle", print_error=50348032)

        assert [c for c, _ in noticed] == [MEASURED_FAULT_DECIMAL, 50348032]

    def test_a_healthy_printer_is_never_noticed(
        self, adapter: BambuAdapter
    ) -> None:
        noticed: list[Any] = []
        adapter._notice_fault = lambda code, name: noticed.append(code)

        _push(adapter, gcode_state="running", print_error=0)
        _push(adapter, gcode_state="running")

        assert noticed == []

    def test_the_notice_never_blocks_the_telemetry_thread(
        self, adapter: BambuAdapter
    ) -> None:
        """This runs inside paho's ``on_message``, which is serial.

        Anything slow here delays every later push for this printer, and a
        status cache that stops advancing is the exact failure the rest of
        this adapter exists to report. The first publish also imports
        ``kiln.server``, which loads the whole plugin surface.
        """
        import time

        bus = mock.MagicMock()
        bus.publish.side_effect = lambda _e: time.sleep(0.5)

        with mock.patch("kiln.server._get_event_bus", return_value=bus):
            started = time.monotonic()
            _push(
                adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL
            )
            blocked = time.monotonic() - started

        assert blocked < 0.2, f"on_message blocked for {blocked:.2f}s"

    def test_the_notice_reports_and_never_commands(
        self, adapter: BambuAdapter
    ) -> None:
        """No stop, no pause, no gcode -- a hand-started job stays the
        operator's."""
        published: list[Any] = []
        bus = mock.MagicMock()
        bus.publish.side_effect = published.append

        with mock.patch("kiln.server._get_event_bus", return_value=bus):
            _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)
            _join_fault_notices()

        assert len(published) == 1
        event = published[0]
        assert event.data["print_error"] == MEASURED_FAULT_DECIMAL
        assert event.data["print_error_code"] == MEASURED_FAULT_RENDERED
        # How Kiln came to know, so a reader can tell this from a watchdog
        # catch on Kiln's own print.
        assert event.data["noticed_by"] == "connection"
        # Nothing was sent to the printer.
        adapter._mqtt_client.publish.assert_not_called()

    def test_a_broken_event_bus_never_breaks_the_status_cache(
        self, adapter: BambuAdapter
    ) -> None:
        with mock.patch("kiln.server._get_event_bus", side_effect=RuntimeError("no bus")):
            _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        assert adapter.get_state().state is PrinterStatus.ERROR

    def test_the_watch_block_stops_claiming_nothing_is_watching(self) -> None:
        """The measured ``kiln_watch``: watchdog false, health false, watch
        false -- while Kiln was connected and parsing the fault."""
        from kiln.watch_state import kiln_watch_state

        watch = kiln_watch_state("default", adapter=object(), state_word="error")

        assert watch["connection"]["live"] is True
        rules = watch["watchers"]["connection"]
        assert "printer_fault" in rules["checks"]
        # It reports; it does not act.  That distinction is the whole
        # argument for doing this on a machine Kiln did not start.
        assert "never stops" in rules["acts"]

    def test_no_adapter_is_not_a_claim_of_watching(self) -> None:
        from kiln.watch_state import kiln_watch_state

        watch = kiln_watch_state("default", adapter=None, state_word="idle")

        assert watch["connection"]["live"] is False


# ---------------------------------------------------------------------------
# 6. The promotion must not open a gate it used to close
# ---------------------------------------------------------------------------


class TestThePromotionOpensNoGate:
    """The cost of moving a fact into the headline, paid rather than ignored.

    Five gates asked "what is this machine doing" by reading the bare state
    word, and a promoted headline is a different word.  Left alone, the fix
    for a hidden fault would have quietly disabled a mid-print filament
    refusal, a mid-print Z-home block and a mid-print error acknowledgement
    -- trading a reporting bug for three safety ones.  Each now reads
    ``effective_state``, and each is pinned here.

    These are guards, not defect pins: they pass on the pre-fix code too,
    because on the pre-fix code the headline never moved.  Their job is to
    fail the moment one of those reads goes back to ``state``.
    """

    def _printing_with_a_fault(self) -> PrinterState:
        return PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            print_error=MEASURED_FAULT_DECIMAL,
        )

    def test_a_filament_op_is_still_refused_during_a_faulted_print(self) -> None:
        """The extruder is parked over the part; a fault does not move it."""
        from kiln.printers.base import PrinterError

        adapter = mock.MagicMock()
        adapter.get_state.return_value = self._printing_with_a_fault()

        with pytest.raises(PrinterError, match="while a print is running"):
            BambuAdapter._prepare_filament_op(
                adapter,
                "load",
                slot=None,
                material=None,
                temperature=None,
                length_mm=None,
            )

    def test_an_error_acknowledgement_is_still_refused_mid_print(self) -> None:
        from unittest.mock import patch

        from kiln import server

        adapter = mock.MagicMock()
        adapter.get_state.return_value = self._printing_with_a_fault()
        adapter.capabilities.can_clear_error = True

        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.clear_printer_error()

        assert out["success"] is False
        assert out["error"]["code"] == "PRINTER_BUSY"
        # And it says what the machine is DOING, not the word the fault put
        # in front of it -- "while a print is error" answers nothing.
        assert "printing" in out["error"]["message"]
        adapter.clear_error.assert_not_called()

    def test_a_faulted_idle_printer_can_still_be_cleared(self) -> None:
        """The refusal above must not swallow the recovery path itself."""
        from unittest.mock import patch

        from kiln import server
        from kiln.printers.base import PrintResult

        faulted = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            print_error=MEASURED_FAULT_DECIMAL,
        )
        cleared = PrinterState(connected=True, state=PrinterStatus.IDLE)
        adapter = mock.MagicMock()
        adapter.capabilities.can_clear_error = True
        adapter.get_state.side_effect = [faulted, cleared]
        adapter.clear_error.return_value = PrintResult(success=True, message="sent")

        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.clear_printer_error()

        assert out["success"] is True
        assert out["cleared"] is True


# ---------------------------------------------------------------------------
# 7. An error Kiln caused is not a fault Kiln discovered
# ---------------------------------------------------------------------------


class TestOurOwnStopIsNotADiscovery:
    """A cancel walks this firmware through a real error code.

    Measured on an A1 (2026-08-14): ``print_error: 50348044`` for about four
    seconds after a deliberate stop.  Announcing that as a fault found on an
    unwatched machine would make the new notice the boy who cried wolf, and
    the notice only earns its keep by being rare.  The STATE still reports
    it -- what is suppressed is the claim that Kiln discovered something.
    """

    OUR_CANCEL_CODE = 50348044

    def test_our_own_cancel_raises_no_fault_notice(
        self, adapter: BambuAdapter
    ) -> None:
        noticed: list[int] = []
        adapter._notice_fault = lambda code, name: noticed.append(code)

        adapter.cancel_print()
        _push(adapter, gcode_state="failed", print_error=self.OUR_CANCEL_CODE)

        assert noticed == []

    def test_the_state_still_reports_it(self, adapter: BambuAdapter) -> None:
        """Suppressing the notice must not suppress the reading."""
        adapter.cancel_print()
        _push(adapter, gcode_state="failed", print_error=self.OUR_CANCEL_CODE)

        state = adapter.get_state()

        assert state.state is PrinterStatus.ERROR
        assert state.print_error == self.OUR_CANCEL_CODE
        assert state.fault_note

    def test_a_fault_after_the_window_is_still_noticed(
        self, adapter: BambuAdapter
    ) -> None:
        """The window is a settling delay, not an off switch."""
        noticed: list[int] = []
        adapter._notice_fault = lambda code, name: noticed.append(code)

        adapter.cancel_print()
        adapter._stop_sent_at -= adapter._STOP_SETTLE_SECONDS + 1.0
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        assert noticed == [MEASURED_FAULT_DECIMAL]

    def test_a_printer_kiln_never_commanded_is_never_suppressed(
        self, adapter: BambuAdapter
    ) -> None:
        """The measured case: nothing was sent, so nothing is settling."""
        noticed: list[int] = []
        adapter._notice_fault = lambda code, name: noticed.append(code)

        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        assert noticed == [MEASURED_FAULT_DECIMAL]


class TestOnlyARealCodePromotes:
    """One function decides what counts as a firmware error, not two.

    ``format_error_code`` calls anything at or below zero "no error", because
    zero is how this firmware says nothing is wrong and formatting it would
    invent a fault.  The promotion has to agree, or a payload contradicts
    itself: an ``error`` headline over an empty code field, explained by a
    sentence that names no code.
    """

    @pytest.mark.parametrize("bogus", [0, -1, -302022663])
    def test_a_value_that_renders_no_code_promotes_nothing(
        self, bogus: int
    ) -> None:
        state = PrinterState(
            connected=True, state=PrinterStatus.IDLE, print_error=bogus
        )

        assert state.state is PrinterStatus.IDLE
        assert state.fault_note is None
        assert state.print_error_code is None

    def test_a_real_code_still_promotes(self) -> None:
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            print_error=MEASURED_FAULT_DECIMAL,
        )

        assert state.state is PrinterStatus.ERROR
        assert state.print_error_code == MEASURED_FAULT_RENDERED


class TestTheFaultCopyIsTwoThings:
    """What happened and what clears it are read at different moments.

    Joined, they were one 380-character string that ended by telling a person
    reading a web page to call an MCP tool.  Split, a surface with one line of
    room can show the half that is about their printer.
    """

    def test_the_note_says_what_happened_and_stops(
        self, adapter: BambuAdapter
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        note = adapter.get_state().fault_note

        assert MEASURED_FAULT_RENDERED in note
        assert "nozzle" in note.lower()
        # No instruction, and above all no tool name.
        assert "clear_printer_error" not in note

    def test_the_remedy_says_what_clears_it(self, adapter: BambuAdapter) -> None:
        _push(adapter, gcode_state="idle", print_error=MEASURED_FAULT_DECIMAL)

        remedy = adapter.get_state().fault_remedy

        assert remedy
        assert "screen" in remedy.lower()

    def test_both_ride_the_lite_path(self) -> None:
        from unittest.mock import MagicMock, patch

        from kiln import server

        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            print_error=MEASURED_FAULT_DECIMAL,
        )
        adapter = MagicMock()
        adapter.get_state.return_value = state
        adapter.get_job.return_value = JobProgress()

        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status(detail="lite")

        assert out["printer"]["fault_note"]
        assert out["printer"]["fault_remedy"]

    def test_a_healthy_printer_carries_neither(self) -> None:
        state = PrinterState(connected=True, state=PrinterStatus.IDLE)

        data = state.to_dict()

        assert "fault_note" not in data
        assert "fault_remedy" not in data


class TestTheReadingNamesTheCodeOnce:
    """Every caller already names the code before handing over the reading.

    Two of them did, in two different spellings: "The printer raised
    0502-4007 during the purge: The printer reported 0502_4007, a code Kiln
    has no reading for."  The reading says what the code MEANS and stops.
    """

    def test_an_unknown_code_is_not_restated_by_its_own_reading(self) -> None:
        from kiln.printers.bambu import describe_bambu_filament_fault

        reading, _url = describe_bambu_filament_fault(
            "0502-4007", kind="print_error"
        )

        assert "0502" not in reading
        assert "no reading" in reading

    def test_a_family_fallback_is_not_restated_either(self) -> None:
        from kiln.printers.bambu import describe_bambu_filament_fault

        reading, _url = describe_bambu_filament_fault(
            "0300-400C", kind="print_error"
        )

        assert "0300" not in reading
        assert "extruder" in reading

    def test_the_composed_note_names_it_exactly_once(self) -> None:
        state = PrinterState(
            connected=True, state=PrinterStatus.IDLE, print_error=84033543
        )

        assert state.fault_note.count("0502") == 1
