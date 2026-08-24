"""``print_hours`` only counts prints Kiln actually WATCHED end.

The counter had never worked.  Measured against the live heartbeat table on
2026-08-12: 939 prints over 30 days across 63 real installs and 4.0 recorded
hours between them — fourteen seconds a print — with 61 of the 63 reporting
zero.  Both writers that existed (``await_print_completion``,
``record_print_outcome``) showed zero dispatches in production, so nothing
was measuring the one number the whole fleet's usage is judged by.

Capture now happens at the adapter-generic chokepoint every backend passes
through: the ``get_state`` wrap → ``_feed_outcome_lifecycle`` → the same
terminal transition that already records the outcome, reading the elapsed
from the ``get_job()`` call that was being made there anyway.

The constraint is the design, not a limitation of it.  A watched ending is
banked on every backend.  A LATE observation banks only when the adapter
declares its clock ``"frozen"`` — the printer's own job timer stopped with
the print, so late is merely late — and is tagged ``prints_hours_reported``
so the total says how much of itself arrived that way.  A late reading from
a ``"stopwatch"`` clock (Bambu's — nothing stops it) still records NOTHING,
because a confidently wrong number is worse than an honest absence and
``prints - prints_hours_known`` is what makes that absence visible.  These
tests hold that line against the ways it silently breaks.

Bambu reaches that ending by a SECOND door, and it is wired to the same rule.
Its MQTT callback (``bambu._on_message``) fires the terminal hook itself and
calls ``observe_state`` while doing so — the same shared state the wrap reads
— so the push CONSUMES the transition and the wrap's next poll finds prev ==
terminal and no edge at all.  The push site therefore calls the same
``_record_print_duration``, under the same job id it just gave the hook, and
the two doors dedupe against each other through that id.

What differs between the doors is the one measurement neither can borrow: how
long since we last had current knowledge of the printer.  The wrap ASKS, so it
counts from the last ask.  The push site is TOLD, so it counts from the age of
the run state it held before the frame arrived — and two cheaper answers are
both wrong, each by a measured hour.  It cannot use the ask clock, because a
Bambu ``get_state()`` is answered from the push cache, so polling straight
through an MQTT outage keeps that clock warm while nothing is watching.  Nor
can it ask when the printer last SPOKE, because a partial frame carries no run
state, so one landing ahead of the reconnect dump makes an hour-old ending
look a second old.  The second-door section holds every half of that.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from kiln import auto_record_hook as hook
from kiln import daily_stats
from kiln.printers import progress_motion as pm
from kiln.printers.base import (
    JobProgress,
    JobResult,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
    delegate_outcome_lifecycle,
)


@pytest.fixture(autouse=True)
def _reset_lifecycle_state():
    """Both halves of the lifecycle keep module-global state."""
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()
    yield
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()


@pytest.fixture(autouse=True)
def _no_db_writes(monkeypatch):
    """The outcome row is another test's subject; count the calls instead."""
    calls: list[dict] = []

    import kiln.plugins.learning_tools as lt

    monkeypatch.setattr(
        lt, "record_print_outcome",
        lambda **kw: calls.append(kw) or {"success": True},
    )
    return calls


class _Adapter(PrinterAdapter):
    """A scriptable adapter: whatever ``get_state`` returns, the real wrap
    installed by ``__init_subclass__`` feeds into the real lifecycle."""

    def __init__(self, name: str = "moonraker") -> None:
        self._name = name
        self.job_label: str | None = "bracket.gcode"
        self.status = PrinterStatus.PRINTING
        self.job_result: JobResult | None = None
        self.elapsed_seconds: int | None = None
        self.state_age_seconds: float | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(
            connected=True,
            state=self.status,
            last_job_result=self.job_result,
            state_age_seconds=self.state_age_seconds,
        )

    def get_job(self) -> JobProgress:
        return JobProgress(
            file_name=self.job_label,
            print_time_seconds=self.elapsed_seconds,
        )

    def finish(self) -> None:
        self.status = PrinterStatus.IDLE
        self.job_result = JobResult.COMPLETED

    # -- remaining contract, unused here --------------------------------
    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def list_files(self) -> list:
        return []

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, message="ok")

    def delete_file(self, file_name: str) -> bool:
        return True

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def send_gcode(self, command: str) -> str:
        return "ok"

    def set_tool_temp(self, celsius: float, tool: int = 0) -> bool:
        return True

    def set_bed_temp(self, celsius: float) -> bool:
        return True


class _StopwatchAdapter(_Adapter):
    """Reports elapsed the way BAMBU does — a Kiln-side stopwatch.

    Not a mock of the hazard, the mechanism itself: ``bambu.get_job`` fills
    ``print_time_seconds`` from :func:`job_elapsed_seconds`, which subtracts
    the stamp ``start_print`` wrote from ``time.monotonic()`` right now.  It
    keeps counting after the print is over.
    """

    def get_job(self) -> JobProgress:
        return JobProgress(
            file_name=self.job_label,
            print_time_seconds=pm.job_elapsed_seconds(self, self.job_label),
        )


def _rewind_last_look(adapter, seconds: float) -> None:
    """Pretend the previous status read happened ``seconds`` ago."""
    key = pm.observation_key(adapter)
    pm._last_looks[key] = time.monotonic() - seconds


def _rewind_job_start(adapter, seconds: float) -> None:
    """Pretend this job started ``seconds`` ago."""
    key = pm.observation_key(adapter)
    label, _ = pm._job_starts[key]
    pm._job_starts[key] = (label, time.monotonic() - seconds)


def _hours() -> tuple[float, int]:
    stats = daily_stats.get_daily_stats()
    return stats["print_hours"], stats["prints_hours_known"]


def _watch_to_the_end(adapter, *, gap_seconds: float = 5.0) -> None:
    """Poll printing, then poll again ``gap_seconds`` later and see it done."""
    adapter.get_state()          # first look: establishes "was printing"
    _rewind_last_look(adapter, gap_seconds)
    adapter.finish()
    adapter.get_state()          # second look: notices the ending


# ---------------------------------------------------------------------------
# The capture itself
# ---------------------------------------------------------------------------


def test_watched_ending_banks_the_duration():
    adapter = _Adapter()
    adapter.elapsed_seconds = 1800  # half an hour

    _watch_to_the_end(adapter)

    hours, known = _hours()
    assert hours == 0.5
    # Routed through _credit_hours, so the denominator moved with the total —
    # print_hours without it is uninterpretable.
    assert known == 1


def test_duration_is_running_time_not_successful_time():
    """A print cancelled at ten minutes really did run for ten minutes."""
    adapter = _Adapter()
    adapter.elapsed_seconds = 600

    adapter.get_state()
    _rewind_last_look(adapter, 5.0)
    adapter.status = PrinterStatus.IDLE
    adapter.job_result = JobResult.CANCELLED
    adapter.get_state()

    hours, known = _hours()
    assert hours == round(600 / 3600, 2)
    assert known == 1


def test_one_ending_is_banked_once():
    """Polling on after the ending must not keep adding the same print."""
    adapter = _Adapter()
    adapter.elapsed_seconds = 3600

    _watch_to_the_end(adapter)
    for _ in range(3):
        _rewind_last_look(adapter, 5.0)
        adapter.get_state()

    assert _hours() == (1.0, 1)


# ---------------------------------------------------------------------------
# Hazard 1 — the Bambu stopwatch that nothing stops
# ---------------------------------------------------------------------------


def test_late_observed_bambu_ending_banks_nothing():
    """The one that would have gone unnoticed forever.

    A print that ended at 31 minutes, noticed an hour later, reads ~91
    minutes: monotonic, plausible, and wrong in the flattering direction.
    Nothing downstream could ever have caught it.
    """
    adapter = _StopwatchAdapter("bambu")
    pm.note_job_start(adapter, "bracket.3mf")
    adapter.job_label = "bracket.3mf"

    adapter.get_state()                       # seen printing
    _rewind_job_start(adapter, 91 * 60)       # started 91 minutes ago...
    _rewind_last_look(adapter, 60 * 60)       # ...and we last looked an hour ago
    adapter.finish()

    # The inflated number IS what the adapter would report right now —
    # the test is worthless if it isn't.
    assert adapter.get_job().print_time_seconds == pytest.approx(91 * 60, abs=5)

    adapter.get_state()                       # notice the ending, late

    assert _hours() == (0.0, 0)


def test_promptly_observed_bambu_ending_is_banked():
    """The same path, watched — otherwise the guard above is just an off switch."""
    adapter = _StopwatchAdapter("bambu")
    pm.note_job_start(adapter, "bracket.3mf")
    adapter.job_label = "bracket.3mf"

    adapter.get_state()
    _rewind_job_start(adapter, 31 * 60)
    _rewind_last_look(adapter, 10.0)
    adapter.finish()
    adapter.get_state()

    hours, known = _hours()
    assert hours == pytest.approx(31 / 60, abs=0.02)
    assert known == 1


def test_terminal_transition_stops_the_stopwatch():
    """``forget_job_start`` had ZERO callers, so the stamp outlived its print.

    A next print started from the touchscreen never passes ``start_print``
    and never restamps, so it would inherit this one's start and report the
    age of a job that is already over.  The label guard cannot save that
    case: Bambu's file name comes from the push cache, which keeps naming
    the finished job.
    """
    adapter = _StopwatchAdapter("bambu")
    pm.note_job_start(adapter, "bracket.3mf")
    adapter.job_label = "bracket.3mf"

    _watch_to_the_end(adapter)

    assert pm.observation_key(adapter) not in pm._job_starts
    assert pm.job_elapsed_seconds(adapter, "bracket.3mf") is None


def test_a_reading_that_is_itself_stale_banks_nothing():
    """A push-cache answer minutes old dates the transition it reports."""
    adapter = _Adapter()
    adapter.elapsed_seconds = 1800
    adapter.state_age_seconds = 600.0

    _watch_to_the_end(adapter)

    assert _hours() == (0.0, 0)


def test_watching_part_of_a_print_banks_nothing():
    """Watched the middle, stopped looking, came back after it ended."""
    adapter = _Adapter()
    adapter.elapsed_seconds = 1800

    adapter.get_state()
    _rewind_last_look(adapter, pm.WATCHED_ENDING_MAX_GAP_S + 1)
    adapter.finish()
    adapter.get_state()

    assert _hours() == (0.0, 0)


def test_first_ever_look_banks_nothing():
    """A fresh adapter has watched nothing, so it cannot have watched this."""
    adapter = _Adapter()
    adapter.elapsed_seconds = 1800
    adapter.finish()

    adapter.get_state()

    assert _hours() == (0.0, 0)


# ---------------------------------------------------------------------------
# Hazard 2 — a delegating adapter feeding the loop twice
# ---------------------------------------------------------------------------


class _Backend(_Adapter):
    pass


class _Delegating(_Adapter):
    """The Creality shape: fulfils the protocol by holding another adapter."""

    def __init__(self, backend: _Backend, *, delegated: bool = True) -> None:
        super().__init__("creality")
        self._backend = backend
        if delegated:
            delegate_outcome_lifecycle(backend)

    def get_state(self) -> PrinterState:
        return self._backend.get_state()

    def get_job(self) -> JobProgress:
        return self._backend.get_job()


def _watch_delegating_pair(outer, backend) -> None:
    outer.get_state()
    _rewind_last_look(outer, 5.0)
    _rewind_last_look(backend, 5.0)
    backend.finish()
    outer.get_state()


def test_delegating_adapter_records_one_print_once(_no_db_writes):
    backend = _Backend("moonraker")
    backend.elapsed_seconds = 1800
    outer = _Delegating(backend)

    _watch_delegating_pair(outer, backend)

    assert _hours() == (0.5, 1)
    # Under the name the user registered, which is the one every other
    # surface attributes this printer's prints to.
    assert [c["printer_name"] for c in _no_db_writes] == ["creality"]


def test_an_unmarked_delegating_adapter_really_does_double_fire(_no_db_writes):
    """Prove the hazard is real, so the marker above is not decoration.

    One call runs two wrapped ``get_state`` methods.  The hook's idempotency
    key is ``(adapter.name, job_id)``, and "creality" and "moonraker" are not
    the same string, so nothing dedupes them: one print, two outcome rows.
    """
    backend = _Backend("moonraker")
    backend.elapsed_seconds = 1800
    outer = _Delegating(backend, delegated=False)

    _watch_delegating_pair(outer, backend)

    assert [c["printer_name"] for c in _no_db_writes] == ["moonraker", "creality"]
    # The hours survived it either way — record_print_hours_for_job dedupes on
    # the job id alone, which is why this hazard is an OUTCOME bug and the
    # duration ledger is not the thing holding it shut.
    assert _hours() == (0.5, 1)


def test_creality_marks_its_backend_delegated(monkeypatch):
    """The generic fix above only helps if the real adapter uses it."""
    from kiln.printers.creality import CrealityAdapter

    monkeypatch.setattr(
        CrealityAdapter, "_resolve_moonraker_url",
        lambda self: "http://192.0.2.10:7125",
    )
    adapter = CrealityAdapter(host="192.0.2.10")

    assert adapter._backend._kiln_outcome_delegated is True


# ---------------------------------------------------------------------------
# A shipped adapter, not a stand-in
# ---------------------------------------------------------------------------


def test_a_real_adapter_banks_its_own_reported_duration(monkeypatch):
    """Every test above drives a double defined in this file.

    A double inherits ``PrinterAdapter``, so it proves the LOGIC — but not
    that a shipped adapter's own ``get_state``/``get_job`` still reach it,
    which is the half that actually ships.  This drives the real
    ``MoonrakerAdapter`` with only its HTTP seam faked, so the reading is
    the adapter's real ``print_stats.print_duration`` and the transition is
    its real state mapping.
    """
    from kiln.printers.moonraker import MoonrakerAdapter

    printer = {"state": "printing"}

    def fake_get_json(self, path, **kwargs):
        if "printer/info" in path:
            return {"result": {"state": "ready"}}
        return {"result": {"status": {
            "print_stats": {
                "state": printer["state"],
                "filename": "benchy.gcode",
                "print_duration": 2700.0,
            },
            "virtual_sdcard": {"progress": 1.0},
            "extruder": {}, "heater_bed": {},
        }}}

    monkeypatch.setattr(MoonrakerAdapter, "_get_json", fake_get_json)
    adapter = MoonrakerAdapter(host="http://127.0.0.1:7125")

    adapter.get_state()
    _rewind_last_look(adapter, 8.0)
    printer["state"] = "complete"
    ended = adapter.get_state()

    # The ending arrives as last_job_result, not as the machine's status —
    # the distinction the wrap reads before it reads `state`.
    assert ended.last_job_result is JobResult.COMPLETED
    assert _hours() == (0.75, 1)


# ---------------------------------------------------------------------------
# The honest absence
# ---------------------------------------------------------------------------


def test_an_adapter_with_no_clock_stays_unknown():
    """Direct USB reports SD-card byte progress, never a job clock.

    Declared ``not_in_protocol`` in ``scripts/adapter_conformance.yaml``:
    its hours are UNKNOWABLE, not zero.  Nothing may invent a number here,
    and nothing may credit the denominator either — a print counted as
    "duration known" with no duration is the same lie, arriving quietly.
    """
    adapter = _Adapter("serial")
    adapter.elapsed_seconds = None

    _watch_to_the_end(adapter)

    assert _hours() == (0.0, 0)
    # The print itself is still visible; only its duration is missing.
    assert daily_stats.get_daily_stats()["prints_hours_known"] == 0


def test_the_chokepoint_is_the_only_writer_on_the_watched_path():
    """``await_print_completion`` must not bank the same ending a second time.

    It polls ``adapter.get_state()``, which feeds the lifecycle wrap — so the
    duration is already banked by the time its own IDLE branch runs.  Its
    separate ``record_print_hours`` call therefore counted one print twice,
    and twice in the denominator, making coverage read better than reality.
    Pinned by source rather than by driving the tool: the assertion is that
    this call site does not exist, which is exactly what regressed.
    """
    import inspect

    from kiln.server import await_print_completion

    source = inspect.getsource(
        getattr(await_print_completion, "fn", await_print_completion)
    )
    assert "record_print_hours" not in source


def test_an_absurd_duration_is_refused():
    """One bad clock reading would outweigh every honest print in the day."""
    adapter = _Adapter()
    adapter.elapsed_seconds = 200 * 3600

    _watch_to_the_end(adapter)

    assert _hours() == (0.0, 0)


# ---------------------------------------------------------------------------
# The second door — a Bambu ending that arrives over MQTT
# ---------------------------------------------------------------------------
#
# Driven through the real ``BambuAdapter._on_message`` with real push frames,
# because the whole failure being closed here was a door that LOOKED wired.


def _bambu(monkeypatch):
    """A real BambuAdapter with only its socket withheld."""
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    return BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000000",
    )


def _push(adapter, gcode_state: str, **fields) -> None:
    """Deliver one ``push_status`` frame the way the printer's broker would."""
    payload = {
        "print": {
            "command": "push_status",
            "gcode_state": gcode_state,
            "subtask_name": "bracket",
            "gcode_file": "/sdcard/bracket.3mf",
            "print_error": 0,
            **fields,
        }
    }
    adapter._on_message(None, None, SimpleNamespace(payload=json.dumps(payload).encode()))


def _push_partial(adapter, **fields) -> None:
    """A ``push_status`` frame carrying NO ``gcode_state``.

    Bambu sends these constantly — a temperature change, a fan step.  The
    cache is a merge, so such a frame proves the socket is up and says
    nothing whatever about whether the print is still running.
    """
    payload = {"print": {"command": "push_status", **fields}}
    adapter._on_message(None, None, SimpleNamespace(payload=json.dumps(payload).encode()))


def _rewind_the_silence(adapter, seconds: float) -> None:
    """Model an MQTT outage ``seconds`` long.

    Both clocks are wound back together because that is what an outage IS:
    nothing arrived, so neither the cache's age nor the state's age advanced.
    """
    stamp = time.monotonic() - seconds
    adapter._last_state_time = stamp
    adapter._gcode_state_time = stamp


def _printing_bambu(monkeypatch, *, running_for: float):
    """A Bambu mid-print, its stopwatch started ``running_for`` seconds ago."""
    adapter = _bambu(monkeypatch)
    pm.note_job_start(adapter, "bracket.3mf")
    _push(adapter, "RUNNING")
    _rewind_job_start(adapter, running_for)
    return adapter


def test_a_streamed_bambu_ending_is_banked(monkeypatch):
    """The measured gap this whole branch exists to close.

    A connected Bambu never reaches the polled door — its own push wiring
    consumes the transition first — so before this, every Bambu ending banked
    0.0 hours no matter how closely it was watched.
    """
    adapter = _printing_bambu(monkeypatch, running_for=31 * 60)

    _push(adapter, "FINISH")

    hours, known = _hours()
    assert hours == pytest.approx(31 / 60, abs=0.02)
    assert known == 1


def test_a_reconnect_dump_banks_nothing(monkeypatch):
    """The inflation this door would otherwise have reintroduced.

    MQTT drops mid-print; the print ends; an hour later the socket comes back
    and the printer sends a full status dump.  ``prev_gcode_state`` comes from
    the cache and still says RUNNING, so the frame looks exactly like a live
    ending — while the stopwatch, which nothing stopped, has been counting the
    whole outage.
    """
    adapter = _printing_bambu(monkeypatch, running_for=91 * 60)
    _rewind_the_silence(adapter, 60 * 60)

    # The inflated number IS what the stopwatch would report right now — the
    # test is worthless if it isn't.
    assert pm.job_elapsed_seconds(adapter, "bracket.3mf") == pytest.approx(
        91 * 60, abs=5
    )

    _push(adapter, "FINISH")

    assert _hours() == (0.0, 0)


def test_polling_through_the_outage_does_not_launder_the_dump(monkeypatch):
    """Why the push door measures the printer's silence, not our own looking.

    ``note_status_read`` — the clock the polled door guards with — cannot be
    reused here.  A Bambu ``get_state()`` is answered from the push CACHE, so
    a monitor polling every few seconds through an MQTT outage keeps stamping
    that clock while the printer says nothing at all.  Borrow it and the
    reconnect dump above walks straight through the guard looking watched.
    """
    adapter = _printing_bambu(monkeypatch, running_for=91 * 60)
    _rewind_the_silence(adapter, 60 * 60)

    # A monitor kept polling the whole time the socket was down.
    for _ in range(3):
        adapter.get_state()

    # It really did leave the look-clock warm: this is the reading the polled
    # door's guard would have been given, and it passes.
    look_gap = time.monotonic() - pm._last_looks[pm.observation_key(adapter)]
    assert look_gap < pm.WATCHED_ENDING_MAX_GAP_S

    _push(adapter, "FINISH")

    assert _hours() == (0.0, 0)


def test_a_partial_frame_does_not_reopen_the_door(monkeypatch):
    """The reconnect dump is not always the FIRST frame back.

    Bambu streams partial ``push_status`` frames constantly — a temperature,
    a fan step — and the cache is a merge, so one of them proves the socket
    is up and says NOTHING about whether the print is still running.  Let one
    land between the reconnect and the full dump and a guard measuring "when
    did this printer last speak" sees a one-second gap sitting in front of an
    ending that happened during the outage.

    What has to be recent is our knowledge of the RUN STATE, which is the
    same quantity ``state_age_seconds`` reports to the polled door.  A partial
    frame cannot refresh it, because it never carried it.
    """
    adapter = _printing_bambu(monkeypatch, running_for=91 * 60)
    _rewind_the_silence(adapter, 60 * 60)

    # The socket comes back and something trivial arrives ahead of the dump.
    _push_partial(adapter, nozzle_temper=214.0)

    _push(adapter, "FINISH")

    assert _hours() == (0.0, 0)


def test_the_push_door_banks_under_the_id_it_gave_the_hook(_no_db_writes, monkeypatch):
    """One print, one identity — across the hours row and the outcome row.

    ``record_print_hours_for_job`` dedupes on the job id ALONE, and the other
    writer that can bank the same print (``record_print_outcome``, reading the
    job record when an agent later refines an auto-recorded outcome) keys on
    the hook's ``job_id``.  Bank under the file name instead and the two spell
    one print two ways: nothing collapses them, and the hours row stops naming
    the job the outcome row named.
    """
    adapter = _printing_bambu(monkeypatch, running_for=31 * 60)

    _push(adapter, "FINISH")

    recorded = _hours()
    assert recorded[1] == 1
    job_id = _no_db_writes[-1]["job_id"]
    assert job_id == "bracket"  # subtask_name, not /sdcard/bracket.3mf

    # Banking again under that id is refused, which is what proves it IS the
    # key — the outcome row and the hours row cannot drift onto two names.
    daily_stats.record_print_hours_for_job(job_id, 1.0)
    assert _hours() == recorded


def test_a_streamed_ending_stops_the_stopwatch(monkeypatch):
    """The polled door's ``forget_job_start`` never runs on a connected Bambu.

    So this door owes it.  Left running, the stamp outlives its print and the
    next print started from the touchscreen — which never passes
    ``start_print`` and so never restamps — inherits it and reports the age of
    a job that finished hours ago.
    """
    adapter = _printing_bambu(monkeypatch, running_for=31 * 60)

    _push(adapter, "FINISH")

    assert pm.observation_key(adapter) not in pm._job_starts
    assert pm.job_elapsed_seconds(adapter, "bracket.3mf") is None


def test_a_pause_leaves_the_stopwatch_running(monkeypatch):
    """A filament runout is not an ending, and resuming must not restart the clock."""
    adapter = _printing_bambu(monkeypatch, running_for=10 * 60)

    _push(adapter, "PAUSE")

    assert _hours() == (0.0, 0)
    assert pm.job_elapsed_seconds(adapter, "bracket.3mf") == pytest.approx(
        10 * 60, abs=5
    )


def test_one_bambu_ending_is_banked_once(monkeypatch):
    """Both doors witness this printer; only one may bank the print.

    The push door observes the state, so the polled door that follows it sees
    no edge.  Pinned by driving both in the order a real install does.
    """
    adapter = _printing_bambu(monkeypatch, running_for=31 * 60)

    _push(adapter, "FINISH")
    banked = _hours()
    for _ in range(3):
        adapter.get_state()
    _push(adapter, "FINISH")

    assert _hours() == banked
    assert banked[1] == 1


def test_the_push_door_never_raises_into_the_mqtt_callback(monkeypatch):
    """A broken ledger must not break live print monitoring for every Bambu.

    ``_on_message`` is the callback the status cache, the progress display and
    the concurrency gate all read from.  Telemetry raising here would take all
    of it down.
    """
    adapter = _printing_bambu(monkeypatch, running_for=31 * 60)

    def _explode(*args, **kwargs):
        raise RuntimeError("ledger is on fire")

    monkeypatch.setattr(daily_stats, "record_print_hours_for_job", _explode)

    _push(adapter, "FINISH", mc_percent=100)

    # The frame was still merged: the cache the rest of Kiln reads is intact.
    assert adapter._last_status["mc_percent"] == 100
    assert adapter.get_state().state is PrinterStatus.IDLE
    # And the stopwatch still stopped.  It is cleared before the banking for
    # exactly this reason — a failed ledger write must not also hand the next
    # print a clock that has been running since this one started.
    assert pm.job_elapsed_seconds(adapter, "bracket.3mf") is None


# ---------------------------------------------------------------------------
# Frozen clocks — a late reading is merely late
# ---------------------------------------------------------------------------
#
# base.py's asymmetry, now acted on: a printer-reported duration (Moonraker,
# OctoPrint, PrusaLink, Duet, Elegoo) FREEZES at the ending, so a late read
# is still correct and discarding it was the reason production banked 0.0
# hours from every unwatched ending.  An adapter opts in by declaring
# ``_DURATION_SEMANTICS = "frozen"``; the default stays the strict
# "stopwatch", so an adapter that forgets to declare can never inflate.


class _FrozenAdapter(_Adapter):
    """Declares what the real HTTP-polled adapters declare."""

    _DURATION_SEMANTICS = "frozen"


def _reported() -> int:
    return daily_stats.get_daily_stats()["prints_hours_reported"]


def test_a_frozen_adapter_banks_a_late_ending_as_reported():
    """The fix itself: the clock froze with the print, so late is merely late."""
    adapter = _FrozenAdapter()
    adapter.elapsed_seconds = 1800

    adapter.get_state()
    _rewind_last_look(adapter, pm.WATCHED_ENDING_MAX_GAP_S + 1)
    adapter.finish()
    adapter.get_state()

    assert _hours() == (0.5, 1)
    # Tagged as learned-late, so the daily total can say how much of itself
    # arrived that way: watched = known − reported.
    assert _reported() == 1


def test_a_frozen_adapter_banks_a_stale_reading_as_reported():
    """A cached answer dates the OBSERVATION, not a frozen clock's value."""
    adapter = _FrozenAdapter()
    adapter.elapsed_seconds = 1800
    adapter.state_age_seconds = 600.0

    _watch_to_the_end(adapter)

    assert _hours() == (0.5, 1)
    assert _reported() == 1


def test_a_frozen_watched_ending_is_banked_as_watched_not_reported():
    """The tag means "learned late", never "came from a frozen printer"."""
    adapter = _FrozenAdapter()
    adapter.elapsed_seconds = 1800

    _watch_to_the_end(adapter)

    assert _hours() == (0.5, 1)
    assert _reported() == 0


def test_the_reported_path_refuses_an_absurd_duration():
    """The credibility cap guards both doors into the ledger."""
    adapter = _FrozenAdapter()
    adapter.elapsed_seconds = 200 * 3600

    adapter.get_state()
    _rewind_last_look(adapter, pm.WATCHED_ENDING_MAX_GAP_S + 1)
    adapter.finish()
    adapter.get_state()

    assert _hours() == (0.0, 0)
    assert _reported() == 0


def test_an_undeclared_adapter_inherits_the_strict_default():
    """Forgetting to declare must cost hours, never invent them.

    ``_Adapter`` deliberately declares nothing, so this whole file's
    "late banks NOTHING" tests double as proof of the default; this one
    pins the default's VALUE so a future relaxation cannot arrive as a
    one-line change to the base class.
    """
    assert PrinterAdapter._DURATION_SEMANTICS == "stopwatch"
    assert "_DURATION_SEMANTICS" not in vars(_Adapter)

    adapter = _Adapter()
    adapter.elapsed_seconds = 1800
    adapter.get_state()
    _rewind_last_look(adapter, pm.WATCHED_ENDING_MAX_GAP_S + 1)
    adapter.finish()
    adapter.get_state()

    assert _hours() == (0.0, 0)


def _shipped_adapters() -> dict[str, type]:
    """Every concrete adapter, keyed by its module stem (the YAML's key)."""
    from kiln.printers.bambu import BambuAdapter
    from kiln.printers.creality import CrealityAdapter
    from kiln.printers.duet import DuetAdapter
    from kiln.printers.elegoo import ElegooAdapter
    from kiln.printers.moonraker import MoonrakerAdapter
    from kiln.printers.octoprint import OctoPrintAdapter
    from kiln.printers.prusalink import PrusaLinkAdapter
    from kiln.printers.serial_adapter import SerialPrinterAdapter

    return {
        "bambu": BambuAdapter,
        "creality": CrealityAdapter,
        "duet": DuetAdapter,
        "elegoo": ElegooAdapter,
        "moonraker": MoonrakerAdapter,
        "octoprint": OctoPrintAdapter,
        "prusalink": PrusaLinkAdapter,
        "serial_adapter": SerialPrinterAdapter,
    }


def test_every_shipped_adapter_declares_its_semantics_explicitly():
    """Inheriting the default is a missing decision, not a made one.

    The default exists so an UNSHIPPED adapter cannot inflate; a shipped
    one must say which family its clock belongs to, in its own class body,
    where the person adding the ninth backend will be looking.
    """
    for stem, cls in _shipped_adapters().items():
        declared = vars(cls).get("_DURATION_SEMANTICS")
        assert declared in {"stopwatch", "frozen", "none"}, (
            f"{stem}: _DURATION_SEMANTICS must be declared on the class "
            f"itself, not inherited (got {declared!r})"
        )


def test_the_conformance_yaml_mirrors_the_declared_semantics():
    """The YAML is documentation; the ClassVar is what runs.

    scripts/adapter_conformance.yaml is not shipped in the pip package, so
    runtime code cannot read it — but it is where humans audit the adapter
    contract, so the two must not be allowed to disagree.
    """
    import pathlib

    import yaml

    ledger_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts"
        / "adapter_conformance.yaml"
    )
    ledger = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    rows = ledger["adapters"]

    for stem, cls in _shipped_adapters().items():
        assert stem in rows, f"{stem} missing from adapter_conformance.yaml"
        documented = rows[stem].get("duration_semantics")
        assert documented == cls._DURATION_SEMANTICS, (
            f"{stem}: adapter_conformance.yaml says {documented!r} but the "
            f"class declares {cls._DURATION_SEMANTICS!r}"
        )
