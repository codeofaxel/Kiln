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

The constraint is the design, not a limitation of it.  A duration is banked
ONLY when the ending was watched; a partial or late observation records
NOTHING, because a confidently wrong number is worse than an honest absence
and ``prints - prints_hours_known`` is what makes that absence visible.
These tests hold that line against the two ways it silently breaks.

KNOWN GAP — Bambu endings seen over MQTT are still not counted.  The push
callback (``bambu._on_message``) fires the terminal hook itself, and calls
``observe_state`` while doing so.  That is the SAME shared state the wrap
reads, so the push consumes the transition: the next ``get_state()`` sees
prev == terminal, ``is_terminal_transition`` is False, and nothing here
runs.  Measured, not assumed — a push-observed ending banks 0.0 hours.
Closing it means giving the push site the same treatment, and needs two
answers first: a reconnect delivers a full status dump whose ``prev`` predates
the outage, so it needs the same lateness guard or it reintroduces the
inflation above through another door; and the push site keys jobs by
``subtask_name``/``task_id`` while this path keys them by ``file_name``, so
the two would have to agree on a dedupe key before both can bank.
"""

from __future__ import annotations

import time

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
