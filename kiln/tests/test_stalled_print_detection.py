"""A print that is dead while every signal says it is fine.

MEASURED ON A BAMBU A1, 2026-08-11.  A print was paused for 13 minutes via
the relay and resumed.  ``resume_print`` returned
``{"success": true, "message": "Print resumed."}``; ``gcode_state`` flipped
to ``RUNNING`` and telemetry stayed TWO SECONDS fresh for the next twenty
minutes; ``print_error`` was ``0``.  Meanwhile ``layer_num`` stayed ``2``,
``mc_percent`` stayed ``5``, ``mc_remaining_time`` was frozen, and the
printer's own screen said "Paused at 5%".  A second ``resume_print`` was then
REFUSED — "No paused print to resume — the printer isn't paused" — because
Kiln believed ``RUNNING``.  The lie disabled the recovery path.  Only a
cancel ended it.

Two calibration facts from the same night, which the threshold sits between:

* a LEGITIMATE first layer on a 70 mm solid disc held ``layer=1, pct=2`` for
  about five minutes while genuinely printing;
* the real stall was frozen for twenty-plus minutes.

A false stall warning on a healthy print is how a user learns to ignore the
one that matters, so the tests below pin BOTH directions: the incident must
be caught, and the slow first layer must stay silent.
"""

from __future__ import annotations

import pytest

from kiln.printers import progress_motion as pm
from kiln.printers.base import (
    JobProgress,
    JobResult,
    PrinterState,
    PrinterStatus,
    PrintResult,
    describe_stale_state,
)

MINUTE = 60.0


class _Printer:
    """A stand-in adapter.  The primitive keys on the OBJECT, not a name, so
    the recorder and the reader cannot drift apart; the tests hold one of
    these for the same reason production holds one adapter."""

    def __init__(self, name: str = "bambu"):
        self.name = name


BAMBU = _Printer()
RELAYED = _Printer("relayed")
WEIRD = _Printer("weird")
HOSTILE = _Printer("hostile")


@pytest.fixture(autouse=True)
def _clean_observations():
    """No test inherits another's samples."""
    pm.reset_progress_observations()
    yield
    pm.reset_progress_observations()


def _state(
    status: PrinterStatus = PrinterStatus.PRINTING,
    *,
    age: float | None = 2.0,
    job_result: JobResult | None = None,
) -> PrinterState:
    return PrinterState(
        connected=True,
        state=status,
        state_age_seconds=age,
        last_job_result=job_result,
    )


def _job(
    layer: int | None,
    percent: float | None,
    *,
    remaining_s: int | None = None,
    name: str | None = "bracket.3mf",
) -> JobProgress:
    return JobProgress(
        file_name=name,
        completion=percent,
        print_time_left_seconds=remaining_s,
        current_layer=layer,
    )


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------


def test_the_incident_is_caught():
    """Fresh telemetry + RUNNING + frozen counters for 21 minutes."""
    # The printer said RUNNING the whole time, two seconds fresh, and never
    # moved off layer 2 / 5 %.
    first = pm.observe_progress(BAMBU, _state(), _job(2, 5.0), now=0.0)
    assert first.motion is pm.Motion.UNKNOWN, "one reading cannot show motion"

    verdict = pm.observe_progress(BAMBU, _state(), _job(2, 5.0), now=21 * MINUTE)

    assert verdict.motion is pm.Motion.STALLED
    assert verdict.frozen_for_seconds == pytest.approx(21 * MINUTE)
    note = verdict.note()
    assert note is not None
    # The sentence has to carry the two facts the user could not get anywhere
    # else: how long, and where it is stuck.
    assert "21 minutes" in note
    assert "layer 2" in note and "5%" in note
    # And it must not dead-end — the recovery path is named in the sentence.
    assert "force=True" in note


def test_the_freshness_check_still_says_nothing_about_the_incident():
    """The two signals are complements, and this is the proof.

    ``describe_stale_state`` asks whether the last MESSAGE is old.  During
    the incident the messages were two seconds old and wrong, so it was
    silent — correctly, by its own contract.  If this ever starts returning
    a warning for a 2-second-old reading, the stall detector has been made
    redundant by accident and somebody should know.
    """
    assert describe_stale_state(2.0, "printing") is None

    pm.observe_progress(BAMBU, _state(age=2.0), _job(2, 5.0), now=0.0)
    stalled = pm.observe_progress(
        BAMBU, _state(age=2.0), _job(2, 5.0), now=21 * MINUTE
    )
    assert stalled.stalled


def test_a_legitimate_slow_first_layer_stays_silent():
    """The measured healthy case: 70 mm solid disc, layer 1 / 2 % for ~5 min.

    This is the assertion that keeps the detector trustworthy.  If it fails,
    every user learns to scroll past the warning.
    """
    pm.observe_progress(BAMBU, _state(), _job(1, 2.0), now=0.0)

    for elapsed in (60.0, 3 * MINUTE, 5 * MINUTE, 6 * MINUTE):
        verdict = pm.observe_progress(BAMBU, _state(), _job(1, 2.0), now=elapsed)
        assert verdict.motion is pm.Motion.UNKNOWN, f"cried wolf at {elapsed}s"
        assert verdict.note() is None


def test_the_threshold_sits_between_the_two_measurements():
    """Stated as an invariant, not a magic number in one assertion."""
    threshold = pm.stall_threshold_seconds()
    assert 5 * MINUTE < threshold < 20 * MINUTE, (
        "the default must stay above the measured legitimate 5-minute freeze "
        "and below the measured 20-minute real stall"
    )


# ---------------------------------------------------------------------------
# Motion, and the things that are not motion
# ---------------------------------------------------------------------------


def test_real_progress_reads_as_motion():
    """The measured 45-second window during genuine printing: 6→7, 17→20 %."""
    pm.observe_progress(BAMBU, _state(), _job(6, 17.0), now=0.0)
    verdict = pm.observe_progress(BAMBU, _state(), _job(7, 20.0), now=45.0)
    assert verdict.motion is pm.Motion.MOVING


def test_message_counters_are_not_progress():
    """``sequence_id`` and ``msg`` keep incrementing while paused.

    They are why every freshness check passed during the incident.  The
    detector must be unable to see them at all — so a sample carrying wildly
    different "activity" still reads as frozen, because the two fields that
    mean progress did not move.
    """
    pm.observe_progress(BAMBU, _state(age=2.0), _job(2, 5.0), now=0.0)
    # Telemetry is arriving constantly (age stays 2s, the cache is alive) and
    # the temperatures wander — none of it is progress.
    live = PrinterState(
        connected=True,
        state=PrinterStatus.PRINTING,
        state_age_seconds=2.0,
        tool_temp_actual=219.7,
        bed_temp_actual=59.4,
        cooling_fan_speed=87,
        wifi_signal="-51dBm",
    )
    verdict = pm.observe_progress(BAMBU, live, _job(2, 5.0), now=21 * MINUTE)
    assert verdict.stalled


def test_a_paused_printer_is_supposed_to_be_frozen():
    """Frozen counters are correct here, so there is nothing to report."""
    pm.observe_progress(BAMBU, _state(PrinterStatus.PAUSED), _job(2, 5.0), now=0.0)
    verdict = pm.observe_progress(
        BAMBU, _state(PrinterStatus.PAUSED), _job(2, 5.0), now=40 * MINUTE
    )
    assert verdict.motion is pm.Motion.UNKNOWN
    assert verdict.note() is None


def test_a_busy_printer_is_not_stalled():
    """Bambu's prepare/slicing/init can take 5-8 minutes with no layer."""
    pm.observe_progress(BAMBU, _state(PrinterStatus.BUSY), _job(0, 0.0), now=0.0)
    verdict = pm.observe_progress(
        BAMBU, _state(PrinterStatus.BUSY), _job(0, 0.0), now=30 * MINUTE
    )
    assert verdict.motion is pm.Motion.UNKNOWN


def test_resuming_restarts_the_clock():
    """A pause→resume is exactly when the window should start fresh.

    Without this the frozen minutes spent legitimately paused would be
    counted against the resumed print and it would be called stalled the
    instant it came back.
    """
    pm.observe_progress(BAMBU, _state(PrinterStatus.PAUSED), _job(2, 5.0), now=0.0)
    pm.observe_progress(
        BAMBU, _state(PrinterStatus.PAUSED), _job(2, 5.0), now=13 * MINUTE
    )
    # Resumed — same frozen counters, but the state word changed.
    resumed_at = 13 * MINUTE + 1
    back = pm.observe_progress(
        BAMBU, _state(PrinterStatus.PRINTING), _job(2, 5.0), now=resumed_at
    )
    assert back.motion is pm.Motion.UNKNOWN
    assert back.note() is None

    # THE DISCRIMINATING ASSERTION.  Fourteen minutes after the resume, the
    # print has been frozen for twenty-seven minutes in total — but thirteen
    # of those were an honest pause and must not be charged to the resumed
    # print.  Without the reset this reads as a stall and the detector cries
    # wolf at every user who pauses for a spool change.
    during = pm.observe_progress(
        BAMBU,
        _state(PrinterStatus.PRINTING),
        _job(2, 5.0),
        now=resumed_at + 14 * MINUTE,
    )
    assert during.motion is pm.Motion.UNKNOWN
    assert during.frozen_for_seconds == pytest.approx(14 * MINUTE)

    # ...and twenty-one minutes after the resume, it fires.  Which is the
    # incident: the pause was honest, the resume was not.
    later = pm.observe_progress(
        BAMBU,
        _state(PrinterStatus.PRINTING),
        _job(2, 5.0),
        now=resumed_at + 21 * MINUTE,
    )
    assert later.stalled


def test_a_different_job_is_not_a_stalled_one():
    pm.observe_progress(BAMBU, _state(), _job(2, 5.0, name="a.3mf"), now=0.0)
    verdict = pm.observe_progress(
        BAMBU, _state(), _job(2, 5.0, name="b.3mf"), now=30 * MINUTE
    )
    assert verdict.motion is pm.Motion.UNKNOWN


def test_a_layer_counter_going_backwards_is_change_not_stall():
    """Something happened.  Reading it as a stall would invent an incident."""
    pm.observe_progress(BAMBU, _state(), _job(200, 90.0), now=0.0)
    verdict = pm.observe_progress(BAMBU, _state(), _job(1, 0.0), now=30 * MINUTE)
    assert verdict.motion is pm.Motion.MOVING


def test_the_eta_guard_holds_the_detector_quiet():
    """A big first layer on a long print: layer and percent frozen, ETA moving.

    The guard runs in ONE direction — it can only subtract alarms.  It exists
    because a full 256 mm bed is ~17x the area of the 70 mm disc that was
    measured, so a genuinely long first layer can outlast any threshold in
    the 10-15 minute range while the printer counts down normally.
    """
    pm.observe_progress(BAMBU, _state(), _job(1, 2.0, remaining_s=1800), now=0.0)
    verdict = pm.observe_progress(
        BAMBU, _state(), _job(1, 2.0, remaining_s=1500), now=21 * MINUTE
    )
    assert verdict.motion is pm.Motion.UNKNOWN
    assert verdict.reason == "eta still moving"


def test_a_frozen_eta_does_not_hold_it_quiet():
    """During the incident the ETA was frozen too, so the guard stands down."""
    pm.observe_progress(BAMBU, _state(), _job(2, 5.0, remaining_s=1800), now=0.0)
    verdict = pm.observe_progress(
        BAMBU, _state(), _job(2, 5.0, remaining_s=1800), now=21 * MINUTE
    )
    assert verdict.stalled


def test_a_missing_eta_does_not_hold_it_quiet():
    """Absence of the secondary signal must not be read as reassurance."""
    pm.observe_progress(BAMBU, _state(), _job(2, 5.0, remaining_s=None), now=0.0)
    verdict = pm.observe_progress(
        BAMBU, _state(), _job(2, 5.0, remaining_s=None), now=21 * MINUTE
    )
    assert verdict.stalled


def test_the_clock_measures_from_the_last_real_movement():
    """Not from the last poll.

    A caller that polls every thirty seconds for twenty minutes must get the
    same answer as one that looks twice, twenty minutes apart.  If the anchor
    advanced on every sample the window would never accumulate and the
    detector would be silent for the frequent poller — the one most likely
    to be watching.
    """
    pm.observe_progress(BAMBU, _state(), _job(2, 5.0), now=0.0)
    verdict = None
    for tick in range(1, 43):  # every 30s for 21 minutes
        verdict = pm.observe_progress(BAMBU, _state(), _job(2, 5.0), now=tick * 30.0)
    assert verdict is not None and verdict.stalled
    assert verdict.frozen_for_seconds == pytest.approx(21 * MINUTE)


# ---------------------------------------------------------------------------
# Absence is unknown, and the detector never raises
# ---------------------------------------------------------------------------


def test_the_recorder_and_the_reader_agree_on_who_the_printer_is():
    """Caught during development, before it shipped, and worth pinning.

    The status tools know a printer by its REGISTRY name (``"a1"``); an
    adapter knows only its FAMILY (``adapter.name`` is ``"bambu"`` for every
    Bambu).  Key the writer one way and the reader the other and the resume
    gate silently never sees the samples the status tool wrote — which
    reinstates the whole lockout while every test above still passes, because
    each half works perfectly on its own key.
    """
    adapter = _Printer("bambu")
    key_from_writer = pm.observation_key(adapter)
    key_from_reader = pm.observation_key(adapter)
    assert key_from_writer == key_from_reader

    pm.observe_progress(adapter, _state(), _job(2, 5.0), now=0.0)
    pm.observe_progress(adapter, _state(), _job(2, 5.0), now=21 * MINUTE)
    # The reader is a different door with no sample of its own.
    assert pm.latest_verdict(adapter).stalled


def test_the_doors_really_do_hand_back_one_adapter_instance():
    """The assumption the whole detector rests on, made a witness.

    Keying on the instance is only stable because ``_get_adapter`` caches in
    a module global and the registry stores the object.  If either ever
    started building a fresh adapter per call, every observation would land
    under a new key, no history would ever accumulate, and the detector would
    go permanently silent — with no error, no failing assertion anywhere else,
    and a status surface that looks exactly as healthy as it does today.
    That is the silent-measurement failure mode, so it gets a test.
    """
    from unittest.mock import patch

    import kiln.server as srv

    sentinel = _Printer("cached")
    with patch.object(srv, "_adapter", sentinel):
        assert srv._get_adapter() is sentinel
        assert srv._get_adapter() is srv._get_adapter()

    from kiln.registry import PrinterRegistry

    registry = PrinterRegistry()
    registry.register("a1", sentinel)  # type: ignore[arg-type]
    assert registry.get("a1") is registry.get("a1") is sentinel


def test_two_printers_of_the_same_brand_do_not_share_a_history():
    """``adapter.name`` is the brand, so keying on it would merge them —
    and each machine's readings would look like the other one moving."""
    one, two = _Printer("bambu"), _Printer("bambu")
    assert pm.observation_key(one) != pm.observation_key(two)

    pm.observe_progress(one, _state(), _job(2, 5.0), now=0.0)
    pm.observe_progress(one, _state(), _job(2, 5.0), now=21 * MINUTE)
    # The second machine is genuinely printing and must not inherit a stall.
    pm.observe_progress(two, _state(), _job(6, 17.0), now=0.0)
    assert pm.observe_progress(two, _state(), _job(7, 20.0), now=45.0).motion is (
        pm.Motion.MOVING
    )
    assert pm.latest_verdict(one).stalled


def test_one_observation_is_unknown_not_fine():
    verdict = pm.observe_progress(BAMBU, _state(), _job(2, 5.0), now=0.0)
    assert verdict.motion is pm.Motion.UNKNOWN
    assert verdict.reason == "first observation"


def test_a_never_observed_printer_is_unknown():
    assert pm.latest_verdict(_Printer("nobody-looked")).motion is pm.Motion.UNKNOWN


def test_an_adapter_with_no_progress_fields_is_unknown_not_stalled():
    """Two missing fields are not two frozen ones."""
    generic = _Printer("generic")
    pm.observe_progress(generic, _state(), _job(None, None), now=0.0)
    verdict = pm.observe_progress(generic, _state(), _job(None, None), now=40 * MINUTE)
    assert verdict.motion is pm.Motion.UNKNOWN
    assert verdict.reason == "no progress fields"


@pytest.mark.parametrize(
    "state, job",
    [
        (None, None),
        (object(), object()),
        ({"state": "printing"}, {"current_layer": "not-a-number"}),
        (_state(), {"completion": object()}),
    ],
)
def test_malformed_readings_degrade_to_unknown(state, job):
    """Junk in, ``UNKNOWN`` out — never a verdict, never an exception."""
    verdict = pm.observe_progress(WEIRD, state, job, now=0.0)
    assert isinstance(verdict, pm.MotionVerdict)
    assert verdict.motion is pm.Motion.UNKNOWN
    assert pm.progress_stall_note(WEIRD, state, job, now=1.0) is None


class _Hostile:
    """Every read explodes — the shape the defensive getters cannot absorb.

    ``getattr(obj, name, default)`` only swallows ``AttributeError``; a
    property that raises anything else propagates straight out.  So this is
    what actually exercises the outer guard, and the reason the earlier
    malformed-input cases could not: they were absorbed before reaching it.
    """

    @property
    def state(self):
        raise RuntimeError("telemetry decode blew up")

    @property
    def current_layer(self):
        raise RuntimeError("telemetry decode blew up")


def test_the_detector_never_raises():
    """A detector that throws is worse than no detector.

    It runs inside status reads and inside a resume gate; neither may be
    turned into an error by a thing whose whole job is to be advisory.
    """
    hostile = _Hostile()
    with pytest.raises(RuntimeError):
        _ = hostile.state  # the fixture really is explosive

    verdict = pm.observe_progress(HOSTILE, hostile, hostile, now=0.0)
    assert verdict.motion is pm.Motion.UNKNOWN
    assert verdict.reason == "observation failed"
    assert pm.progress_stall_note(HOSTILE, hostile, hostile, now=1.0) is None
    assert pm.latest_verdict(HOSTILE).motion is pm.Motion.UNKNOWN


def test_it_reads_a_serialised_state_the_same_way():
    """A relayed ``to_dict()`` payload must not need its own field rules."""
    pm.observe_progress(
        RELAYED,
        _state().to_dict(),
        _job(2, 5.0).to_dict(),
        now=0.0,
    )
    verdict = pm.observe_progress(
        RELAYED,
        _state().to_dict(),
        _job(2, 5.0).to_dict(),
        now=21 * MINUTE,
    )
    assert verdict.stalled


def test_the_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("KILN_STALL_WARN_MINUTES", "3")
    assert pm.stall_threshold_seconds() == pytest.approx(180.0)
    pm.observe_progress(BAMBU, _state(), _job(2, 5.0), now=0.0)
    assert pm.observe_progress(BAMBU, _state(), _job(2, 5.0), now=200.0).stalled


@pytest.mark.parametrize("bad", ["", "0", "-5", "soon", "NaN"])
def test_a_broken_threshold_setting_falls_back(monkeypatch, bad):
    """It must not silently disable the detector, nor make it a hair trigger."""
    monkeypatch.setenv("KILN_STALL_WARN_MINUTES", bad)
    assert pm.stall_threshold_seconds() == pytest.approx(15 * MINUTE)


# ---------------------------------------------------------------------------
# Honest resume — the lie must not disable the recovery path
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal stand-in wired to the real template methods."""

    from kiln.printers.base import PrinterAdapter as _Base

    resume_print = _Base.resume_print
    _not_paused_refusal = _Base._not_paused_refusal
    _job_or_none = _Base._job_or_none
    _verify_resume_took = _Base._verify_resume_took
    _no_paused_print_result = _Base._no_paused_print_result
    _unverified_running_result = _Base._unverified_running_result
    _RESUME_VERIFY_TIMEOUT = 0.0
    _RESUME_VERIFY_INTERVAL = 0.0

    def __init__(self, states, job=None):
        self.name = "bambu"
        self._states = list(states)
        self._job = job or _job(2, 5.0)
        self.impl_called = False

    def get_state(self):
        return self._states.pop(0) if len(self._states) > 1 else self._states[0]

    def get_job(self):
        return self._job

    def _resume_print_impl(self):
        self.impl_called = True
        return PrintResult(success=True, message="Print resumed.")


def test_a_stalled_running_printer_may_still_be_resumed():
    """THE LOCKOUT.  This is the assertion the unfixed gate fails.

    Kiln believed ``RUNNING``, so it refused the user's second resume and
    left them with nothing but a cancel.  With motion evidence contradicting
    the state word, the refusal is no longer justified.
    """
    adapter = _FakeAdapter([_state(PrinterStatus.PRINTING)])
    # The user watched it sit there for twenty-one minutes, through the same
    # adapter they are now trying to resume.
    pm.observe_progress(adapter, _state(), _job(2, 5.0), now=0.0)
    pm.observe_progress(adapter, _state(), _job(2, 5.0), now=21 * MINUTE)

    result = adapter.resume_print()

    assert adapter.impl_called, "the stalled printer's resume was refused again"
    assert result.success


def test_a_genuinely_moving_printer_is_still_refused():
    """The gate keeps its teeth where there is positive evidence."""
    adapter = _FakeAdapter([_state(PrinterStatus.PRINTING)], job=_job(7, 20.0))
    pm.observe_progress(adapter, _state(), _job(6, 17.0), now=0.0)

    result = adapter.resume_print()

    assert not adapter.impl_called
    assert not result.success
    assert "isn't paused" in result.message


def test_an_unverifiable_running_printer_is_refused_but_given_a_way_out():
    """Nobody was watching, so Kiln cannot tell.  It must not dead-end."""
    adapter = _FakeAdapter([_state(PrinterStatus.PRINTING)])
    result = adapter.resume_print()

    assert not adapter.impl_called
    assert not result.success
    assert "force=True" in result.message


def test_force_skips_the_gate():
    adapter = _FakeAdapter([_state(PrinterStatus.PRINTING)])
    result = adapter.resume_print(force=True)
    assert adapter.impl_called
    assert result.success


def test_an_idle_printer_is_still_refused_even_with_a_stall_recorded():
    """There is no print to continue, and no progress signal changes that."""
    adapter = _FakeAdapter([_state(PrinterStatus.IDLE)])
    pm.observe_progress(adapter, _state(), _job(2, 5.0), now=0.0)
    pm.observe_progress(adapter, _state(), _job(2, 5.0), now=21 * MINUTE)

    result = adapter.resume_print()
    assert not adapter.impl_called
    assert "isn't paused" in result.message


def test_a_resume_that_did_not_take_reports_failure():
    """The read-back.  ``_resume_print_impl`` returns success for a PUBLISHED
    command; that is not the same sentence as "the print resumed"."""
    adapter = _FakeAdapter(
        [_state(PrinterStatus.PAUSED), _state(PrinterStatus.PAUSED)]
    )
    result = adapter.resume_print()

    assert adapter.impl_called
    assert not result.success
    assert "still reports paused" in result.message


def test_a_resume_that_took_does_not_claim_the_print_is_progressing():
    """Honest bound, made explicit: the state word is what lied that night.

    So the success message reports the state word AS a state word, and points
    at the thing that can actually tell — observed motion.
    """
    adapter = _FakeAdapter(
        [_state(PrinterStatus.PAUSED), _state(PrinterStatus.PRINTING)]
    )
    result = adapter.resume_print()

    assert result.success
    assert "not yet observed motion" in result.message


def test_verification_never_turns_a_resume_into_an_error():
    """A verification step may never become a way for a control path to fail."""

    class _Exploding(_FakeAdapter):
        def get_state(self):
            if self.impl_called:
                raise RuntimeError("read blew up during verification")
            return _state(PrinterStatus.PAUSED)

    adapter = _Exploding([_state(PrinterStatus.PAUSED)])
    result = adapter.resume_print()
    assert adapter.impl_called
    assert result.success
    assert result.message == "Print resumed."


def test_an_unreadable_state_fails_open():
    """A transient read must never stand between a user and their print."""

    class _Blind(_FakeAdapter):
        def get_state(self):
            raise RuntimeError("offline")

    adapter = _Blind([])
    result = adapter.resume_print()
    assert adapter.impl_called
    assert result.success


def test_cancel_is_untouched():
    """Cancel is a safety/control path: no gate, no read-back, no new way to
    fail.  It was verified on hardware to genuinely stop the machine, and a
    verification step that can fail is the one thing a cancel must not grow.
    """
    import inspect

    from kiln.printers.bambu import BambuAdapter

    source = inspect.getsource(BambuAdapter.cancel_print)
    assert "_send_print_command" in source
    assert "get_state" not in source
    assert "sleep" not in source
    assert "_verify" not in source


def test_the_serial_flag_gate_also_has_a_way_out():
    """Serial tracks pause with a local flag, so a pause it did not perform
    is invisible to it — which is a lockout of exactly the same shape."""
    import inspect

    from kiln.printers.serial_adapter import SerialPrinterAdapter

    signature = inspect.signature(SerialPrinterAdapter.resume_print)
    assert "force" in signature.parameters
    source = inspect.getsource(SerialPrinterAdapter.resume_print)
    assert "self._paused or force" in source


# ---------------------------------------------------------------------------
# Elapsed: measured, or absent — never extrapolated
# ---------------------------------------------------------------------------


class _CachedBambu:
    """A Bambu adapter answering from a fixed MQTT status dict."""

    def __init__(self, status):
        from kiln.printers.bambu import BambuAdapter

        self._adapter = object.__new__(BambuAdapter)
        self._adapter._get_cached_status = lambda: status  # type: ignore[attr-defined]

    def get_job(self):
        from kiln.printers.bambu import BambuAdapter

        return BambuAdapter.get_job(self._adapter)


def test_the_fabricated_elapsed_is_gone():
    """MEASURED: 99 % with 1 minute remaining rendered "1h 39m" on the web
    for a print that had run about 31 minutes.

    The old arithmetic — ``remaining / (1 - pct/100) - remaining`` — makes
    that inevitable: whenever remaining is one minute, the elapsed in minutes
    equals the completion percentage, so the 1h39m WAS the 99 %.
    """
    job = _CachedBambu(
        {
            "gcode_file": "bracket.3mf",
            "mc_percent": 99,
            "mc_remaining_time": 1,
            "layer_num": 120,
        }
    ).get_job()

    # The old code produced exactly this.  Pin the number so nobody
    # reintroduces the formula and calls it a refactor.
    assert 5939 == int((60 / (1 - 99 / 100)) - 60)
    assert job.print_time_seconds != 5939
    # Kiln did not start this print, so it does not know when it began.
    assert job.print_time_seconds is None


def test_elapsed_is_measured_when_kiln_started_the_print():
    pm.note_job_start(BAMBU, "/sdcard/model/bracket.3mf", at=0.0)
    assert pm.job_elapsed_seconds(BAMBU, "bracket.3mf", now=31 * MINUTE) == 31 * 60


def test_elapsed_does_not_depend_on_the_percentage():
    """The whole point: the same clock, whatever the printer claims."""
    pm.note_job_start(BAMBU, "bracket.3mf", at=0.0)
    at_five_percent = pm.job_elapsed_seconds(BAMBU, "bracket.3mf", now=600.0)
    at_ninety_nine = pm.job_elapsed_seconds(BAMBU, "bracket.3mf", now=600.0)
    assert at_five_percent == at_ninety_nine == 600


def test_elapsed_is_unknown_for_a_print_kiln_did_not_start():
    """Attached to a print already running, or a fresh process."""
    assert pm.job_elapsed_seconds(BAMBU, "bracket.3mf") is None


def test_elapsed_is_unknown_for_a_different_job():
    pm.note_job_start(BAMBU, "bracket.3mf", at=0.0)
    assert pm.job_elapsed_seconds(BAMBU, "totally-different.3mf", now=600.0) is None


def test_the_job_label_match_survives_the_two_spellings():
    """A print is STARTED as a path and REPORTED back as a bare name."""
    pm.note_job_start(BAMBU, "/Users/adam/parts/bracket.gcode.3mf", at=0.0)
    assert pm.job_elapsed_seconds(BAMBU, "bracket", now=60.0) == 60


def test_an_ended_job_stops_the_clock_for_the_next_one():
    """A finished print must not lend its start stamp to the next print.

    Keyed on the TRANSITION into an ended state, using the ``last_job_result``
    the adapters already report, rather than a second way of noticing.
    """
    pm.note_job_start(BAMBU, "bracket.3mf", at=0.0)
    pm.observe_progress(BAMBU, _state(), _job(30, 90.0), now=0.0)
    pm.observe_progress(
        BAMBU,
        _state(PrinterStatus.IDLE, job_result=JobResult.COMPLETED),
        _job(35, 100.0),
        now=600.0,
    )
    assert pm.job_elapsed_seconds(BAMBU, "bracket.3mf", now=900.0) is None


def test_a_steady_finish_in_the_cache_does_not_wipe_a_fresh_stamp():
    """The push cache keeps reporting the LAST ending until the next print
    replaces it.  Reading that steady value as news would delete the stamp
    Kiln just wrote for the print about to begin."""
    pm.observe_progress(
        BAMBU,
        _state(PrinterStatus.IDLE, job_result=JobResult.COMPLETED),
        _job(35, 100.0),
        now=0.0,
    )
    pm.note_job_start(BAMBU, "next.3mf", at=1.0)
    pm.observe_progress(
        BAMBU,
        _state(PrinterStatus.IDLE, job_result=JobResult.COMPLETED),
        _job(35, 100.0),
        now=2.0,
    )
    assert pm.job_elapsed_seconds(BAMBU, "next.3mf", now=61.0) == 60


def test_start_print_stamps_the_clock():
    """The stamp goes on the one event Kiln is guaranteed to witness."""
    import inspect

    from kiln.printers.base import PrinterAdapter

    source = inspect.getsource(PrinterAdapter.start_print)
    assert "note_job_start" in source
    # Inside the non-resume block: a resume 3MF continues the print that is
    # already running, so restamping it would reset a clock mid-print.
    assert source.index("is_resume_mode_3mf") < source.index("note_job_start")
