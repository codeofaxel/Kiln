"""The copies a fill leaves are queued, and print plate after plate.

Until this change ``multi_copy_print(fill=True)`` placed as many copies as
fit, started them, and told the person to come back for the rest.  Now the
rest is queued as one job that remembers the PART, not a sliced file
(``server._queue_fill_rest``).  When its turn comes and the plate is clear,
the scheduler runs the print pipeline for the plate as it is then
(``JobScheduler._start_fill_job``) and queues whatever still does not fit,
until every copy has printed.

WHAT THIS PINS
--------------
* 20 copies at 6 a plate on one printer print 6, then 6, 6 and 2 from the
  queue, each only after the plate before it is cleared, and the queue ends
  empty (``TestPlateAfterPlate``);
* a fill job waits for an occupied plate like any job and says so in words
  that agree with "the other 14 copies" (``TestTheWaitIsSaidForTheCopies``);
* below Kiln Business it asks the start gate's turn check BEFORE slicing,
  and a start the gate turns away waits its turn, no retry spent
  (``TestTheTurn``); any other failure frees the printer, spends a retry,
  then fails in a sentence and queues no rest (``TestAFailedPlate``);
* while a plate is being prepared off the scheduler's thread nothing judges
  the job or hands its printer another (``TestWhileAPlateIsBeingPrepared``);
* the rest is kept in Kiln's own folder with the person's yes
  (``TestQueueFillRest``); the pipeline's start step carries the gate's code
  (``TestThePipelineSaysTheGatesCode``); a restart settles a fill job from
  the plate record and the file the start reported (``TestAfterARestart``);
  the words agree in number and leave a file's own name alone
  (``TestTheWords``).

The scheduler is driven through ``tick()`` with the fake printers and the
real plate record of ``test_queue_waits_for_the_plate.py`` and the
restartable Kiln of ``test_queue_restart_recovery.py``.  The print pipeline
is a fake (``_Pipeline``): it lays out at most six copies, and its "start"
does what a real one does to the world -- the part goes on the plate record
and the printer reports printing -- so no slicer or printer is touched.

A/B, recorded where it was run (2026-10-25, against 3bffe0c4): each test
that pins a fix names the mutation of the source it was run against and the
assertion that failed; every mutation was restored byte-identical.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln import plate_state
from kiln.events import EventType
from kiln.plate_state import START_NOT_YET_CODE, PlateJob, mark_clear, mark_occupied
from kiln.printers.base import (
    CodedPrintResult,
    JobResult,
    PrinterEngagementError,
    PrinterStatus,
    PrintResult,
)
from kiln.queue import JobStatus, PrintQueue
from kiln.scheduler import _cap, _job_label, _many, _verb, describe_wait, plate_wait_sentence, turn_wait_sentence
from tests.test_queue_restart_recovery import _idle, _Kiln, _part, _printing
from tests.test_queue_waits_for_the_plate import (  # noqa: F401 -- _isolated is an autouse fixture
    _isolated,
    _printer,
    _set_state,
)

PART = "bracket.stl"
#: Where ``_queue_fill_rest`` would have kept the part (any path: the fake
#: pipeline never opens it).
KEPT = "/kiln-home/queued_parts/part.0123456789abcdef.stl"
PER_PLATE = 6


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _result(
    success: bool, *, wanted: int, placed: int, file_name: str = "", code: str = "", message: str = "",
) -> SimpleNamespace:
    """A print pipeline result as ``reslice_and_print`` shapes it: a slice
    step saying how many copies went on the plate, and a start step."""
    start = {"file_name": file_name} if file_name else {}
    if code:
        start["code"] = code
    steps = [
        SimpleNamespace(name="slice", success=True, data={"copies": {"wanted": wanted, "placed": placed}}),
        SimpleNamespace(name="start_print", success=success, data=start),
    ]
    return SimpleNamespace(
        success=success, message=message or ("started" if success else "did not start"), steps=steps,
        to_dict=lambda: {"pipeline": "reslice_and_print", "success": success, "steps": []},
    )


class _Pipeline:
    """Stands in for ``kiln.pipelines.reslice_and_print``.

    Each call is recorded with whether the printer's plate held a part at
    that moment.  By default it lays out at most :data:`PER_PLATE` copies
    and starts them: the part goes on the plate record and the printer
    reports printing it, as a real start leaves them.  *answer*, when set,
    is the whole call instead (a failure, a refusal), touching nothing.
    """

    def __init__(self, printers: Any) -> None:
        self.printers = printers            # name -> adapter
        self.calls: list[dict[str, Any]] = []
        self.placed: list[int] = []
        self.answer: Any = None

    def __call__(self, **kw: Any) -> SimpleNamespace:
        adapter = self.printers(kw["printer_name"])
        self.calls.append({**kw, "plate_occupied": plate_state.read(adapter).occupied})
        if self.answer is not None:
            return self.answer(kw)
        placed = min(PER_PLATE, int(kw["copies"]))
        file_name = f"bracket_plate{len(self.calls)}.gcode.3mf"
        mark_occupied(adapter, PlateJob(file=file_name, max_z_mm=20.0))
        _printing(adapter, file_name)
        self.placed.append(placed)
        return _result(True, wanted=kw["copies"], placed=placed, file_name=file_name)


def _fill_job(queue: PrintQueue, copies: int, *, printer: str = "A", priority: int = 0, **extra: Any) -> str:
    """A job as ``_queue_fill_rest`` submits it: the part, not a sliced file."""
    metadata: dict[str, Any] = {"fill": {
        "model_path": KEPT, "copies": copies, "spacing_mm": 10.0,
        "printer_id": None, "overrides": None, "slicer_path": None,
    }}
    metadata.update(extra)
    return queue.submit(
        PART, printer_name=printer, submitted_by="multi_copy_print", priority=priority, metadata=metadata,
    )


def _note(k: _Kiln, job_id: str) -> dict | None:
    return (k.queue.get_job(job_id).metadata or {}).get("waiting")


@pytest.fixture(autouse=True)
def _the_turn_is_free(monkeypatch):
    """The start gate's one-printer check, which the scheduler asks before a
    fill, says yes unless a test says otherwise (the real one reads the
    process's own registry and plan, not this test's)."""
    monkeypatch.setattr("kiln.printers.print_gate._concurrent_fleet_verdict", lambda adapter: None)


@pytest.fixture()
def db(tmp_path) -> str:
    return str(tmp_path / "queue" / "jobs.db")


@pytest.fixture()
def k(db) -> _Kiln:
    """One run of Kiln with printer A; a fill's work runs in line."""
    kiln = _Kiln(db, {"A": _printer("A", "SER-A")})
    kiln.sched._spawn = lambda work: work()
    return kiln


@pytest.fixture()
def fake(k, monkeypatch) -> _Pipeline:
    pipeline = _Pipeline(k.registry.get)
    monkeypatch.setattr("kiln.pipelines.reslice_and_print", pipeline)
    return pipeline


@pytest.fixture()
def server(k, monkeypatch):
    """``kiln.server`` pointed at this Kiln's queue, printers and bus."""
    import kiln.server as srv

    monkeypatch.setattr(srv, "_get_queue", lambda: k.queue)
    monkeypatch.setattr(srv, "_get_registry", lambda: k.registry)
    monkeypatch.setattr(srv, "_get_event_bus", lambda: k.bus)
    monkeypatch.setattr(srv, "_scheduler", k.sched)
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_resolve_control_target", lambda name=None: (k.registry.get(name or "A"), name or "A"))
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    return srv


# ---------------------------------------------------------------------------
# 1. The headline: plate after plate until every copy has printed
# ---------------------------------------------------------------------------


class TestPlateAfterPlate:
    def test_twenty_copies_six_a_plate_print_six_six_six_two_each_after_its_plate_is_cleared(
        self, k, fake, server, monkeypatch, tmp_path,
    ):
        """multi_copy_print starts the first 6 and queues 14.  The queue
        starts nothing while a plate prints or while its part is still on
        the plate; each "the plate is clear" starts the next plate, laid out
        for the copies still owed; the run ends with the queue empty.

        A/B: with ``_finish_fill_job`` not calling ``_queue_rest`` the run
        stops after one queued plate -- ``(job,) = k.queue.queued_for("A")``
        raises ``ValueError: not enough values to unpack (expected 1, got
        0)`` on the second plate.  With the dispatch's fill branch dead
        (``if False:``, the job sent like a sliced file) the queue hands the
        STL's name to the printer and no plate is laid out -- ``assert (20,
        False) == (14, False)`` at the first queued plate.
        """
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        a = k.registry.get("A")
        stl = tmp_path / PART
        stl.write_bytes(b"solid bracket\nendsolid bracket\n")
        monkeypatch.setattr(server, "_pipeline_reslice_and_print", fake)

        summary = server.multi_copy_print(model_path=str(stl), copies=20, printer_name="A", fill=True)

        assert summary["success"] is True, summary
        assert (summary["copies_placed"], summary["copies_remaining"]) == (6, 14)
        assert summary["queued_job_id"]
        started: list[str] = []
        for wanted in (14, 8, 2):
            k.ticks(2)                       # the plate before prints
            assert len(fake.calls) == len(started) + 1, "nothing starts while the printer prints"
            _idle(a, ended=JobResult.COMPLETED)
            k.ticks(3)                       # it finished; its part is on the plate
            assert len(fake.calls) == len(started) + 1, "never onto the part still on the plate"
            (job,) = k.queue.queued_for("A")
            assert job.metadata["fill"]["copies"] == wanted
            assert job.metadata["waiting"]["kind"] == "plate"

            mark_clear(a, "human")
            k.sched.tick()

            call = fake.calls[-1]
            assert (call["copies"], call["plate_occupied"]) == (wanted, False)
            assert k.status(job.id) == JobStatus.PRINTING
            started.append(job.id)
        k.ticks(2)
        _idle(a, ended=JobResult.COMPLETED)
        k.sched.tick()

        assert [c["copies"] for c in fake.calls] == [20, 14, 8, 2]
        assert fake.placed == [6, 6, 6, 2] and sum(fake.placed) == 20
        assert not any(c["plate_occupied"] for c in fake.calls)
        assert started[0] == summary["queued_job_id"]
        assert [k.status(j) for j in started] == [JobStatus.COMPLETED] * 3
        assert k.queue.pending_count() == 0 and k.queue.unfinished() == []
        assert [e.data["copies"] for e in k.events(EventType.JOB_QUEUED)] == [14, 8, 2]
        assert [e.data["copies"] for e in k.events(EventType.JOB_STARTED)] == [6, 6, 2]
        assert [o["outcome"] for o in k.outcomes] == ["success"] * 3
        a.start_print.assert_not_called()

    def test_the_rest_keeps_the_part_the_printer_the_yes_and_its_place(self, k, fake):
        """The plate after this one is queued on the same terms: the same
        kept part, printer, spacing, priority and yes; and the yes reaches
        the pipeline that starts it.

        A/B: with ``_queue_rest`` not called this fails at ``(rest,) =
        k.queue.queued_for("A")`` -- ``ValueError: not enough values to
        unpack (expected 1, got 0)``."""
        signoff = {"printer_name": "A", "token_id": "tok-1"}
        job = _fill_job(k.queue, 14, priority=3, preview_signoff=signoff)

        k.sched.tick()

        assert fake.calls[0]["signoff"] == signoff and fake.calls[0]["model_path"] == KEPT
        (rest,) = k.queue.queued_for("A")
        assert rest.id != job and rest.priority == 3 and rest.printer_name == "A"
        assert rest.file_name == PART and rest.submitted_by == "multi_copy_print"
        assert rest.metadata == {
            "preview_signoff": signoff,
            "fill": {"model_path": KEPT, "copies": 8, "spacing_mm": 10.0, "printer_id": None,
                     "overrides": None, "slicer_path": None},
        }
        assert k.queue.get_job(job).metadata["started_file"] == "bracket_plate1.gcode.3mf"


# ---------------------------------------------------------------------------
# 2. An occupied plate holds it, in words about the copies
# ---------------------------------------------------------------------------


class TestTheWaitIsSaidForTheCopies:
    def test_an_occupied_plate_holds_the_rest_and_says_the_other_fourteen_are_next(self, k, fake, server):
        """A/B: with ``_job_label`` returning the file's name only, the note
        reads "bracket is next on A" -- the ``startswith("The other 14
        copies of bracket are next on A. ")`` assertion fails.  With
        ``next_up_after_clear`` saying "which was" whatever the label, its
        sentence assertion fails."""
        from kiln.plugins.queue_tools import next_up_after_clear

        a = k.registry.get("A")
        mark_occupied(a, PlateJob(file="bracket_x6.gcode.3mf", max_z_mm=20.0))
        job = _fill_job(k.queue, 14)

        k.ticks(3)

        assert fake.calls == [] and k.status(job) == JobStatus.QUEUED
        assert k.sched.active_jobs == {} and k.sched._retry_counts.get(job, 0) == 0
        note = _note(k, job)
        assert note["kind"] == "plate" and note["code"] == START_NOT_YET_CODE and note["printers"] == ["A"]
        assert note["sentence"].startswith("The other 14 copies of bracket are next on A. "), note["sentence"]
        assert "and Kiln prints the next plate of them on its own." in note["sentence"]
        (event,) = k.events(EventType.JOB_WAITING)
        assert event.data["message"] == note["sentence"]
        assert describe_wait(k.queue.get_job(job), k.queue) == {**note, "ahead": 0}

        up = next_up_after_clear("A")
        assert up["job_id"] == job
        assert up["sentence"] == (
            "Next on A: the other 14 copies of bracket, which were waiting for this plate. Kiln's queue starts "
            "them on its next pass; cancel_queued_job stops that."
        )


# ---------------------------------------------------------------------------
# 3. One printer at a time
# ---------------------------------------------------------------------------


class TestTheTurn:
    def test_the_turn_is_asked_before_any_slice_and_the_rest_keeps_its_place(self, k, fake, monkeypatch):
        """B is printing on a one-printer plan.  The gate's own check is
        asked for A before anything is laid out; it says no, so nothing is
        sliced, no retry is spent, and the job waits its turn.  When B is
        done and the gate says yes, the plate is laid out and started.

        A/B: with ``_turn_refusal`` returning ``None`` unconditionally this
        fails at ``assert fake.calls == []`` -- the plate was sliced only to
        be turned away at the start."""
        k.registry.register("B", _printer("B", "SER-B", PrinterStatus.PRINTING))
        asked: list[Any] = []
        verdict = {"code": "TIER_CONCURRENT_PRINT_LIMIT", "reason": "Kiln runs one printer at a time."}
        monkeypatch.setattr(
            "kiln.printers.print_gate._concurrent_fleet_verdict", lambda adapter: asked.append(adapter) or verdict,
        )
        job = _fill_job(k.queue, 14)

        result = k.sched.tick()

        assert asked == [k.registry.get("A")]
        assert fake.calls == []
        assert result["failed"] == [] and k.status(job) == JobStatus.QUEUED
        assert k.sched._retry_counts.get(job, 0) == 0 and job not in k.sched._retry_not_before
        assert k.sched.active_jobs == {} and k.sched._preparing == set()
        note = _note(k, job)
        assert note["kind"] == "turn" and note["code"] == "TIER_CONCURRENT_PRINT_LIMIT"
        assert note["waiting_for"] == ["B"]
        assert note["sentence"] == (
            "Kiln runs one printer at a time on this plan, and B is printing. The other 14 copies of bracket keep "
            "their place in line and start on A once B is done."
        )

        _set_state(k.registry.get("B"), PrinterStatus.IDLE)
        monkeypatch.setattr("kiln.printers.print_gate._concurrent_fleet_verdict", lambda adapter: None)
        k.sched.tick()

        assert [c["copies"] for c in fake.calls] == [14]
        assert k.status(job) == JobStatus.PRINTING

    def test_a_start_the_gate_turns_away_waits_its_turn_and_frees_the_printer(self, k, fake):
        """The turn check said yes, but the start met the engagement rule
        (another machine became Kiln's meanwhile): the pipeline reports the
        code on its start step.  Nothing printed, so the job waits its turn
        -- no retry, no failure, its printer released, no rest queued.

        A/B: with the turn branch of ``_finish_fill_job`` dead (``if
        False:``) the start is read as a failed plate -- ``assert 1 == 0``
        at the ``_retry_counts`` line."""
        k.registry.register("B", _printer("B", "SER-B", PrinterStatus.PRINTING))
        fake.answer = lambda kw: _result(
            False, wanted=kw["copies"], placed=6, code="TIER_SINGLE_PRINTER_LIMIT",
            message="Failed to start print: Kiln is working with B.",
        )
        job = _fill_job(k.queue, 14)

        result = k.sched.tick()

        assert len(fake.calls) == 1
        assert k.sched._retry_counts.get(job, 0) == 0 and job not in k.sched._retry_not_before
        assert result["failed"] == [] and k.events(EventType.JOB_FAILED) == []
        assert k.status(job) == JobStatus.QUEUED
        assert k.sched.active_jobs == {} and k.sched._preparing == set()
        assert [j.id for j in k.queue.list_jobs()] == [job], "a plate that never started queues no rest"
        note = _note(k, job)
        assert note["kind"] == "turn" and note["code"] == "TIER_SINGLE_PRINTER_LIMIT"
        assert note["waiting_for"] == ["B"]

        k.clock.advance(30)
        k.ticks(3)
        assert len(fake.calls) == 1, "nothing is laid out again while B prints"


# ---------------------------------------------------------------------------
# 4. Any other failure
# ---------------------------------------------------------------------------


def _raise(exc: Exception) -> Any:
    def answer(_kw: dict[str, Any]) -> Any:
        raise exc
    return answer


class TestAFailedPlate:
    @pytest.mark.parametrize("how", ["raises", "returns_failure"])
    def test_it_frees_the_printer_spends_the_retries_then_fails_in_a_sentence_and_queues_no_rest(self, k, fake, how):
        """A/B: with ``self._active_jobs.pop(job.id, None)`` removed from the
        failure path of ``_finish_fill_job`` the printer stays taken -- the
        ``k.sched.active_jobs == {}`` assertion fails after the first
        plate (``{'<id>': 'A'} == {}``)."""
        k.sched._retry_backoff_base = 0.0       # retry on the next pass
        if how == "raises":
            fake.answer = _raise(RuntimeError("the slicer crashed"))
            why = "the slicer crashed"
        else:
            fake.answer = lambda kw: _result(False, wanted=kw["copies"], placed=6, message="Upload failed: timeout")
            why = "Upload failed: timeout"
        job = _fill_job(k.queue, 14)

        for attempt in (1, 2):
            k.sched.tick()
            assert len(fake.calls) == attempt
            assert k.sched.active_jobs == {} and job not in k.sched._preparing
            assert k.sched._retry_counts[job] == attempt
            assert k.status(job) == JobStatus.QUEUED
        k.sched.tick()

        assert len(fake.calls) == 3
        sentence = f"The next plate of copies did not start on A: {why}"
        assert k.status(job) == JobStatus.FAILED and k.queue.get_job(job).error == sentence
        assert [e.data for e in k.events(EventType.JOB_FAILED)] == [{"job_id": job, "error": sentence}]
        assert [j.id for j in k.queue.list_jobs()] == [job], "a plate that never started queues no rest"
        assert k.sched.active_jobs == {} and k.events(EventType.JOB_STARTED) == []
        assert k.outcomes == [], "nothing printed, so there is no print outcome to record"


# ---------------------------------------------------------------------------
# 5. While a plate is being laid out and sliced
# ---------------------------------------------------------------------------


class TestWhileAPlateIsBeingPrepared:
    def test_nothing_judges_the_job_or_takes_its_printer_until_the_plate_starts(self, k, fake):
        """The plate is prepared off the scheduler's thread.  Until it starts
        the printer is idle, and a pass reading it would close the job as a
        print that "went idle before it was seen printing".

        A/B: with phase 1's ``if job_id in self._preparing: continue``
        removed this fails at ``assert out["checked"] == 0`` (1 == 0): the
        job's printer is read and the job judged mid-preparation."""
        a = k.registry.get("A")
        later: list[Any] = []
        k.sched._spawn = later.append
        job = _fill_job(k.queue, 14)
        other = k.queue.submit("lid.gcode", printer_name="A", submitted_by="test")

        k.sched.tick()

        assert fake.calls == [] and len(later) == 1
        assert k.sched.active_jobs == {job: "A"} and job in k.sched._preparing
        assert k.status(job) == JobStatus.STARTING
        for _ in range(3):
            k.clock.advance(5)
            out = k.sched.tick()
            assert out["checked"] == 0
            assert out["completed"] == [] and out["dispatched"] == [] and out["failed"] == []
        assert k.status(job) == JobStatus.STARTING and k.outcomes == []
        assert k.status(other) == JobStatus.QUEUED
        a.start_print.assert_not_called()

        later.pop()()

        assert [c["copies"] for c in fake.calls] == [14]
        assert k.status(job) == JobStatus.PRINTING and k.sched._preparing == set()
        assert k.sched.active_jobs == {job: "A"}
        k.sched.tick()
        assert k.status(other) == JobStatus.QUEUED
        a.start_print.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Queueing the rest
# ---------------------------------------------------------------------------


class TestQueueFillRest:
    """The two sentences ``multi_copy_print`` says around it (queued, and
    could not be queued) are pinned in test_plate_fill_doors.py
    (``test_a_fill_that_stops_short_says_how_to_get_the_rest``,
    ``test_a_rest_that_cannot_be_queued_is_said_not_lost``), run beside
    this file."""

    def test_the_part_is_kept_in_kilns_folder_and_one_job_carries_it_and_the_yes(
        self, k, fake, server, db, tmp_path, monkeypatch,
    ):
        """A/B: with the part not kept (``kept = model_path``) this fails at
        ``assert kept != str(src)``."""
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        src = tmp_path / "Downloads" / PART
        src.parent.mkdir()
        src.write_bytes(b"solid bracket\nendsolid bracket\n")
        asked: list[Any] = []
        monkeypatch.setattr(
            server, "_resolve_control_target", lambda name=None: asked.append(name) or (k.registry.get("A"), "A"),
        )
        signoff = {"printer_name": "A", "token_id": "tok-1"}

        out = server._queue_fill_rest(
            str(src), 14, printer_name=None, printer_id="bambu_a1", overrides={"fill_density": "20%"},
            slicer_path="/opt/slicer", spacing_mm=4.0, signoff=signoff,
        )

        assert set(out) == {"job_id", "printer_name"} and out["printer_name"] == "A"
        assert asked == [None]
        job = k.queue.get_job(out["job_id"])
        assert (job.file_name, job.printer_name, job.submitted_by, job.status) == (
            PART, "A", "multi_copy_print", JobStatus.QUEUED,
        )
        fill = dict(job.metadata["fill"])
        kept = fill.pop("model_path")
        assert fill == {
            "copies": 14, "spacing_mm": 4.0, "printer_id": "bambu_a1", "overrides": {"fill_density": "20%"},
            "slicer_path": "/opt/slicer",
        }
        assert job.metadata["preview_signoff"] == signoff
        assert kept != str(src)
        assert kept.startswith(str(home / ".kiln" / "queued_parts") + os.sep)
        src.unlink()
        with open(kept, "rb") as fh:
            assert fh.read() == b"solid bracket\nendsolid bracket\n", "tidying the folder it came from loses nothing"
        assert PrintQueue(db_path=db).get_job(job.id).metadata["fill"]["model_path"] == kept
        assert [e.data for e in k.events(EventType.JOB_QUEUED)] == [
            {"job_id": job.id, "file_name": PART, "printer_name": "A", "copies": 14},
        ]

        k.sched.tick()

        (call,) = fake.calls
        assert (call["model_path"], call["printer_name"], call["copies"], call["spacing_mm"]) == (kept, "A", 14, 4.0)
        assert (call["printer_id"], call["overrides"], call["slicer_path"]) == (
            "bambu_a1", {"fill_density": "20%"}, "/opt/slicer",
        )
        assert call["signoff"] == signoff

    def test_a_printer_it_cannot_resolve_is_said_and_never_raised(self, k, server, tmp_path, monkeypatch):
        """A/B: with the ``return {"why": ...}`` replaced by ``raise`` the
        call raises -- ``ValueError: No printer named 'C' is set up in
        Kiln.``"""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        src = tmp_path / PART
        src.write_bytes(b"solid bracket\n")

        def _no_such_printer(name=None):
            raise ValueError("No printer named 'C' is set up in Kiln.")

        monkeypatch.setattr(server, "_resolve_control_target", _no_such_printer)

        out = server._queue_fill_rest(
            str(src), 14, printer_name="C", printer_id=None, overrides=None, slicer_path=None, spacing_mm=10.0,
            signoff=None,
        )

        assert out == {"why": "No printer named 'C' is set up in Kiln."}
        assert k.queue.list_jobs() == [] and k.events(EventType.JOB_QUEUED) == []


# ---------------------------------------------------------------------------
# 7. The pipeline's start step carries the gate's code
# ---------------------------------------------------------------------------


class TestThePipelineSaysTheGatesCode:
    """``reslice_and_print`` driven to its real start step on the harness of
    test_pipelines.py's ``TestResliceAndPrintPipeline.test_full_pipeline_success``:
    the profile, slicer, G-code scan and adapter lookup are stubbed, the
    model need not exist (``skip_validation=True``), and the adapter is a
    stub with only ``upload_file`` / ``get_state`` / ``start_print`` (so no
    3MF wrap).  Only ``start_print``'s answer differs between the tests.

    A/B: with the ``step_data["code"]`` lines removed the coded refusal's
    start step has no code (``KeyError: 'code'``); with the
    ``except PrinterEngagementError`` removed the engagement refusal falls
    to the step's generic handler and its data is ``{}`` -- ``assert {} ==
    {'code': 'TIER_SINGLE_PRINTER_LIMIT'}``."""

    def _start_step(self, start: Any) -> Any:
        from kiln.pipelines import reslice_and_print

        sliced = MagicMock(output_path="/tmp/out.gcode", message="Sliced OK", slicer="prusaslicer")
        scan = MagicMock(valid=True, commands=["G28"], blocked_commands=[], warnings=[], errors=[])
        adapter = MagicMock(spec=["upload_file", "get_state", "start_print"])
        adapter.upload_file.return_value = {"name": "out.gcode"}
        state = MagicMock()
        state.connected = True
        state.state.value = "idle"
        adapter.get_state.return_value = state
        if isinstance(start, Exception):
            adapter.start_print.side_effect = start
        else:
            adapter.start_print.return_value = start
        with (
            patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini"),
            patch("kiln.slicer.slice_file", return_value=sliced),
            patch("kiln.gcode.scan_gcode_file", return_value=scan),
            patch("kiln.server._resolve_adapter", return_value=adapter),
        ):
            result = reslice_and_print(model_path="/tmp/model.stl", printer_name="A", skip_validation=True)
        adapter.start_print.assert_called_once()
        (step,) = [s for s in result.steps if s.name == "start_print"]
        return result, step

    def test_a_coded_refusal_carries_its_code(self):
        refusal = CodedPrintResult(
            success=False, message="Kiln runs one printer at a time.", refused_before_send=True,
            code="TIER_CONCURRENT_PRINT_LIMIT",
        )
        result, step = self._start_step(refusal)
        assert result.success is False and step.success is False
        assert step.data["code"] == "TIER_CONCURRENT_PRINT_LIMIT"
        assert step.data["file_name"] == "out.gcode"

    def test_the_engagement_rule_carries_its_code(self):
        refusal = PrinterEngagementError({"code": "TIER_SINGLE_PRINTER_LIMIT", "reason": "Kiln is working with B."})
        result, step = self._start_step(refusal)
        assert result.success is False and step.success is False
        assert step.data == {"code": "TIER_SINGLE_PRINTER_LIMIT"}
        assert "Kiln is working with B." in step.message

    def test_a_start_that_went_carries_no_code(self):
        result, step = self._start_step(PrintResult(success=True, message="started"))
        assert result.success is True, [s.message for s in result.steps if not s.success]
        assert "code" not in step.data


# ---------------------------------------------------------------------------
# 8. After a restart
# ---------------------------------------------------------------------------


class TestAfterARestart:
    """A restart as in test_queue_restart_recovery.py: a Kiln on a queue
    store stops, a new one starts on the same store, the printers carry on."""

    def _before(self, db: str, a: Any, monkeypatch, *, copies: int = 14, inline: bool) -> tuple[str, list]:
        before = _Kiln(db, {"A": a})
        later: list[Any] = []
        before.sched._spawn = (lambda work: work()) if inline else later.append
        monkeypatch.setattr("kiln.pipelines.reslice_and_print", _Pipeline({"A": a}.__getitem__))
        job = _fill_job(before.queue, copies)
        before.sched.tick()
        return job, later

    def _after(self, db: str, a: Any, monkeypatch) -> tuple[_Kiln, _Pipeline]:
        after = _Kiln(db, {"A": a})
        after.sched._spawn = lambda work: work()
        fake = _Pipeline({"A": a}.__getitem__)
        monkeypatch.setattr("kiln.pipelines.reslice_and_print", fake)
        return after, fake

    def test_a_fill_left_starting_whose_plate_started_is_watched_not_laid_out_again(self, db, monkeypatch):
        """Kiln stopped while the plate was being prepared, after the
        pipeline had started it: the plate record holds a part from after
        the job's start, and the printer prints a file named for the plate,
        not for the part.  It is the job: watched, never laid out again.

        A/B: with the fill clause removed from ``_settle_one``'s
        ``same_file`` the printer reads as busy with something else -- the
        job is closed, ``assert <JobStatus.COMPLETED: 'completed'> ==
        <JobStatus.PRINTING: 'printing'>``."""
        a = _printer("A", "SER-A")
        job, later = self._before(db, a, monkeypatch, inline=False)
        assert later, "the plate was being prepared when Kiln stopped"
        _part(a, "bracket_plate1.gcode.3mf")
        _printing(a, "bracket_plate1.gcode.3mf")

        after, fake = self._after(db, a, monkeypatch)
        after.sched.tick()

        assert after.status(job) == JobStatus.PRINTING
        assert after.sched.active_jobs == {job: "A"}
        assert fake.calls == [] and after.outcomes == []
        a.start_print.assert_not_called()

        _idle(a, ended=JobResult.COMPLETED)
        after.sched.tick()
        assert after.status(job) == JobStatus.COMPLETED
        assert [o["outcome"] for o in after.outcomes] == ["success"]

    def test_a_fill_left_starting_that_no_printer_took_is_laid_out_again_once(self, db, monkeypatch):
        """The plate was cleared (which is why the job could start), then
        Kiln stopped before anything reached the printer: idle, the record
        clear.  The job goes back in line and is laid out again, once.

        A/B: with ``if is_fill and not state.occupied: return False``
        removed from ``_started_on`` the cleared record reads as this job's
        start -- it is adopted as a print that ran and nothing is laid out:
        ``assert [] == [14]``."""
        a = _printer("A", "SER-A")
        mark_clear(a, "human")
        job, later = self._before(db, a, monkeypatch, inline=False)
        assert later

        after, fake = self._after(db, a, monkeypatch)
        after.sched.tick()

        assert [c["copies"] for c in fake.calls] == [14]
        assert after.status(job) == JobStatus.PRINTING
        assert after.events(EventType.JOB_COMPLETED) == [] and after.outcomes == []

    def test_a_printing_fill_is_known_by_the_file_its_start_reported(self, db, monkeypatch):
        """PRINTING when Kiln stopped, and the plate record no longer says
        so: only the file the start step reported (``started_file``, on the
        queue's store) ties the printer's file to the job.

        A/B: with ``_settle_one`` comparing against ``job.file_name`` only,
        the plate's own file reads as another print -- the job is closed,
        ``assert <JobStatus.COMPLETED: 'completed'> == <JobStatus.PRINTING:
        'printing'>``.  With ``update_metadata`` not writing to the store,
        the new queue has no ``started_file`` -- ``KeyError:
        'started_file'``."""
        a = _printer("A", "SER-A")
        job, _ = self._before(db, a, monkeypatch, copies=PER_PLATE, inline=True)
        assert PrintQueue(db_path=db).get_job(job).metadata["started_file"] == "bracket_plate1.gcode.3mf"
        plate_state._store_path().unlink()       # the record holds nothing for A now

        after, fake = self._after(db, a, monkeypatch)
        after.sched.tick()

        assert after.status(job) == JobStatus.PRINTING
        assert after.sched.active_jobs == {job: "A"}
        assert after.events(EventType.JOB_COMPLETED) == [] and fake.calls == []


# ---------------------------------------------------------------------------
# 9. The words
# ---------------------------------------------------------------------------


def _job(file_name: str, copies: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(file_name=file_name, metadata={"fill": {"copies": copies}} if copies else {})


class TestTheWords:
    """A/B: with ``_cap`` returning its text unchanged the labels open
    lower-case -- ``assert 'the other 1 copy of x' == 'The other 1 copy of
    x'``; with ``_many`` true for every "the other ..." label one copy
    counts as several -- ``assert (True is False)``."""

    def test_one_copy_is_one_thing(self):
        label = _job_label(_job("x.stl", 1))
        assert label == "the other 1 copy of x"
        assert _many(label) is False and _verb(label, "is", "are") == "is"
        assert _cap(label) == "The other 1 copy of x"
        assert plate_wait_sentence(
            "x.stl", ["A"], None, code=START_NOT_YET_CODE, camera=False, label=label,
        ).startswith("The other 1 copy of x is next on A. ")
        assert turn_wait_sentence("x.stl", "A", ["B"], label=label).endswith(
            "The other 1 copy of x keeps its place in line and starts on A once B is done."
        )

    def test_several_copies_agree(self):
        label = _job_label(_job("x.stl", 14))
        assert label == "the other 14 copies of x" and _many(label) is True
        sentence = plate_wait_sentence("x.stl", ["A"], None, code=START_NOT_YET_CODE, camera=False, label=label)
        assert sentence.startswith("The other 14 copies of x are next on A. ")
        assert sentence.endswith("and Kiln prints the next plate of them on its own.")

    def test_a_files_own_name_is_never_capitalised(self):
        q = PrintQueue()
        job = q.submit("iPhone stand.stl")
        assert describe_wait(q.get_job(job), q)["sentence"] == "iPhone stand is next in line."
        assert _job_label(_job("iPhone stand.stl")) == "iPhone stand"
        assert _cap(_job_label(_job("iPhone stand.stl", 14))) == "The other 14 copies of iPhone stand"
        assert plate_wait_sentence(
            "iPhone stand.stl", ["A"], None, code=START_NOT_YET_CODE, camera=False,
        ).startswith("iPhone stand is next on A. ")

    def test_a_file_whose_name_begins_the_other_is_left_as_it_is(self):
        """Found writing these tests: the capital and the plural were judged
        from the label's words, so this file read "The other half are next
        in line."  They now follow the job (a fill's own label), never a
        file's name."""
        q = PrintQueue()
        job = q.submit("the other half.stl")
        assert describe_wait(q.get_job(job), q)["sentence"] == "the other half is next in line."


# --- defects a lap found (2026-10-26; each fails at f5affd68) ---


class TestDefectsTheSecondLapFound:
    def test_the_turn_met_at_the_upload_is_still_the_turn(self, k, fake):
        """The one-printer rule gates the upload, which comes before the
        start, so a queued plate for the second printer met it there and
        spent its retries as "the plate did not start".  A turn code on any
        failed step is the turn: no retry, no failure, the printer freed."""
        k.registry.register("B", _printer("B", "SER-B", PrinterStatus.PRINTING))

        def answer(kw: dict[str, Any]) -> SimpleNamespace:
            steps = [
                SimpleNamespace(name="slice", success=True, data={"copies": {"wanted": kw["copies"], "placed": 6}}),
                SimpleNamespace(name="upload", success=False,
                                data={"code": "TIER_SINGLE_PRINTER_LIMIT"}, message="Upload failed: Kiln is working with B."),
            ]
            return SimpleNamespace(success=False, message="Upload failed", steps=steps, to_dict=lambda: {})

        fake.answer = answer
        job = _fill_job(k.queue, 14)
        k.sched.tick()
        assert k.sched._retry_counts.get(job, 0) == 0
        assert k.status(job) == JobStatus.QUEUED and k.sched.active_jobs == {}
        assert _note(k, job)["kind"] == "turn"

    def test_a_cancel_while_the_plate_is_prepared_watches_what_started_and_queues_no_more(self, k, fake):
        """Laying out a plate takes a slice, long enough for a person to cancel
        the job.  The printer takes the plate anyway: the job stays cancelled
        (its ending recorded as such), the running print is watched, and the
        run queues nothing more.  Before, the success path raised on the
        cancelled job and the print ran unwatched."""
        later: list[Any] = []
        k.sched._spawn = later.append
        job = _fill_job(k.queue, 14)
        k.sched.tick()
        k.queue.cancel(job)
        later.pop()()
        assert k.status(job) == JobStatus.CANCELLED
        assert k.sched.active_jobs == {job: "A"} and job in k.sched._watch, "the print that started is watched"
        assert [j.id for j in k.queue.list_jobs()] == [job], "no rest queued for a cancelled run"
        _idle(k.registry.get("A"))
        k.sched.tick()
        assert k.sched.active_jobs == {}
        assert [o["outcome"] for o in k.outcomes] == ["cancelled"]

    def test_a_cancel_while_the_plate_is_prepared_and_nothing_started_is_simply_done(self, k, fake):
        later: list[Any] = []
        k.sched._spawn = later.append
        fake.answer = _raise(RuntimeError("slicer missing"))
        job = _fill_job(k.queue, 14)
        k.sched.tick()
        k.queue.cancel(job)
        later.pop()()
        assert k.status(job) == JobStatus.CANCELLED
        assert k.sched._retry_counts.get(job, 0) == 0 and k.sched.active_jobs == {}

    def test_the_engagement_rule_met_at_the_upload_carries_its_code_on_that_step(self):
        """The upload is the first gated command, so the rule is met there
        before any start.  Same harness as the start-step tests, with the
        adapter refusing at upload_file."""
        from kiln.pipelines import reslice_and_print

        sliced = MagicMock(output_path="/tmp/out.gcode", message="Sliced OK", slicer="prusaslicer")
        scan = MagicMock(valid=True, commands=["G28"], blocked_commands=[], warnings=[], errors=[])
        adapter = MagicMock(spec=["upload_file", "get_state", "start_print"])
        adapter.upload_file.side_effect = PrinterEngagementError(
            {"code": "TIER_SINGLE_PRINTER_LIMIT", "reason": "Kiln is working with B."}
        )
        with (
            patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini"),
            patch("kiln.slicer.slice_file", return_value=sliced),
            patch("kiln.gcode.scan_gcode_file", return_value=scan),
            patch("kiln.server._resolve_adapter", return_value=adapter),
        ):
            result = reslice_and_print(model_path="/tmp/model.stl", printer_name="A", skip_validation=True)
        (step,) = [s for s in result.steps if s.name == "upload"]
        assert result.success is False and step.success is False
        assert step.data == {"code": "TIER_SINGLE_PRINTER_LIMIT"}
        adapter.start_print.assert_not_called()

    def test_cancel_queued_job_cancels_a_plate_still_being_laid_out(self, k, fake, monkeypatch):
        """``cancel_queued_job`` refused a STARTING job as "already at the
        printer"; a plate being laid out has reached no printer yet."""
        import kiln.server as srv
        from kiln.plugins.queue_tools import cancel_queued_job

        monkeypatch.setattr(srv, "_scheduler", k.sched)
        monkeypatch.setattr(srv, "_get_queue", lambda: k.queue)
        monkeypatch.setattr(srv, "_get_event_bus", lambda: k.bus)
        monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
        later: list[Any] = []
        k.sched._spawn = later.append
        job = _fill_job(k.queue, 14)
        k.sched.tick()
        answer = cancel_queued_job(job)
        assert answer["success"] is True and "being laid out" in answer["message"]
        assert k.status(job) == JobStatus.CANCELLED
        later.pop()()
        assert [j.id for j in k.queue.list_jobs()] == [job]


# --- defects a security review found (2026-10-26; each fails at 12e293bf) ---


class TestACancelStopsThePlateBeforeThePrinter:
    """A cancel landing while a plate was being prepared was recorded, but the
    pipeline went on to upload and start that plate anyway."""

    def test_the_queue_asks_the_pipeline_to_stop_once_the_job_is_cancelled(self, k, fake):
        seen: list[str | None] = []

        def answer(kw: dict[str, Any]) -> SimpleNamespace:
            seen.append(kw["still_wanted"]())
            return _result(False, wanted=kw["copies"], placed=0, message="Not sent")

        later: list[Any] = []
        k.sched._spawn = later.append
        fake.answer = answer
        job = _fill_job(k.queue, 14)
        k.sched.tick()
        k.queue.cancel(job)
        later.pop()()
        assert seen == ["the job was cancelled while its plate was being prepared"]
        assert k.status(job) == JobStatus.CANCELLED and k.sched.active_jobs == {}

    def test_the_pipeline_sends_nothing_once_it_is_no_longer_wanted(self):
        from kiln.pipelines import reslice_and_print

        sliced = MagicMock(output_path="/tmp/out.gcode", message="Sliced OK", slicer="prusaslicer")
        scan = MagicMock(valid=True, commands=["G28"], blocked_commands=[], warnings=[], errors=[])
        adapter = MagicMock(spec=["upload_file", "get_state", "start_print"])
        with (
            patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini"),
            patch("kiln.slicer.slice_file", return_value=sliced),
            patch("kiln.gcode.scan_gcode_file", return_value=scan),
            patch("kiln.server._resolve_adapter", return_value=adapter),
        ):
            result = reslice_and_print(
                model_path="/tmp/model.stl", printer_name="A", skip_validation=True,
                still_wanted=lambda: "the job was cancelled",
            )
        assert result.success is False
        adapter.upload_file.assert_not_called()
        adapter.start_print.assert_not_called()
