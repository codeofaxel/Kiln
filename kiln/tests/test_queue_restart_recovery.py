"""A print Kiln was starting or printing when it stopped is settled from the
printer after a restart, never put back in line on a guess.

Until this change ``PrintQueue._reload_from_db`` reset every job it found
STARTING, PRINTING or PAUSED to QUEUED.  A print that finished while Kiln
was stopped was therefore started again the moment its printer went idle
(with the plate wait in front of it, the moment a person cleared the
plate).  Now those jobs come back as they were, and the scheduler's first
pass (``JobScheduler._settle_unfinished``) asks the printer and the plate
record how each one went.

A restart here is: one run of Kiln (queue + registry + scheduler) on a queue
store, a crash or a stop, then a NEW queue on the same store and a NEW
scheduler with nothing in memory.  The printers are the same fake machines
(they did not restart), and the plate record is the real one in a per-test
``KILN_HOME``.  A fake printer's ``start_print`` writes nothing, so the part
a real start puts on the plate record is written by the test.

A/B, recorded where it was run: each test that pins a fix names the
mutation of the source it was run against and the assertion that failed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiln import plate_state
from kiln.events import EventBus, EventType
from kiln.plate_state import PlateJob, mark_clear, mark_occupied
from kiln.printers.base import JobProgress, JobResult, PrinterState, PrinterStatus
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterRegistry
from kiln.scheduler import WAIT_RECOVERING, JobScheduler, _same_file, describe_wait
from tests.test_queue_waits_for_the_plate import (  # noqa: F401 -- _isolated is an autouse fixture
    _Clock,
    _isolated,
    _printer,
    _set_state,
)

FILE = "bracket.gcode"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Crash(BaseException):
    """Kiln stopping in the middle of a call: no ``except Exception`` catches it."""


class _Kiln:
    """One run of Kiln over a queue store that outlives it.

    A queue on *db*, a registry of *printers*, a bus, a scheduler on a clock
    the test moves, and every outcome the scheduler records (its
    ``_auto_record_outcome``, spied: the arguments are the decision, the
    learning database behind it is not under test here).
    """

    def __init__(self, db: str, printers: dict[str, MagicMock]) -> None:
        self.queue = PrintQueue(db_path=db)
        self.registry = PrinterRegistry()
        for name, adapter in printers.items():
            self.registry.register(name, adapter)
        self.bus = EventBus()
        self.clock = _Clock()
        self.sched = JobScheduler(self.queue, self.registry, self.bus, poll_interval=0.1, max_retries=2)
        self.sched._clock = self.clock
        self.outcomes: list[dict[str, Any]] = []
        self.sched._auto_record_outcome = self._record  # type: ignore[method-assign]

    def _record(
        self, job_id: str, printer_name: str, outcome: str, error_msg: str | None = None,
        determined_by: str = "observed", contribute: bool = False,
    ) -> None:
        self.outcomes.append({
            "job_id": job_id, "printer_name": printer_name, "outcome": outcome,
            "error_msg": error_msg, "determined_by": determined_by, "contribute": contribute,
        })

    def status(self, job_id: str) -> JobStatus:
        return self.queue.get_job(job_id).status

    def events(self, kind: EventType) -> list[Any]:
        """Every event of *kind*, oldest first."""
        return list(reversed(self.bus.recent_events(kind, limit=1000)))

    def ticks(self, n: int) -> list[dict[str, Any]]:
        return [self.sched.tick() for _ in range(n)]


@pytest.fixture()
def db(tmp_path) -> str:
    return str(tmp_path / "queue" / "jobs.db")


def _printing(adapter: MagicMock, file_name: str | None = FILE, state: PrinterStatus = PrinterStatus.PRINTING) -> None:
    """The printer is busy with *file_name* (``None``: it reports no file)."""
    _set_state(adapter, state)
    adapter.get_job.return_value = JobProgress(file_name=file_name, completion=40.0)


def _idle(adapter: MagicMock, ended: JobResult | None = None) -> None:
    """The printer is idle; *ended* is the ending it names for its last job, if any."""
    adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE, last_job_result=ended)
    adapter.get_job.return_value = JobProgress()


def _part(adapter: MagicMock, file_name: str = FILE) -> None:
    """What a real start writes: the part on the printer's plate record."""
    mark_occupied(adapter, PlateJob(file=file_name, max_z_mm=12.0))


def _backdate_plate(adapter: MagicMock, seconds: float) -> None:
    """Move *adapter*'s plate record stamp *seconds* into the past, as if the
    part had been put there by an earlier start."""
    path = plate_state._store_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    row = data["machines"][plate_state.machine_id(adapter)]
    row["since"] = (datetime.fromisoformat(row["since"]) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    path.write_text(json.dumps(data), encoding="utf-8")


def _left_printing(
    db: str, printers: dict[str, MagicMock], *, on: str = "A", assigned: bool = True, part: bool = True,
    paused: bool = False,
) -> str:
    """Kiln dispatches FILE to *on*, watches it print, then stops.

    *part*: the start put the part on the plate record, as every real start
    does.  *paused*: the queue had it PAUSED when Kiln stopped.  The
    printer's ``start_print`` mock is reset, so a later call is a second
    start.
    """
    before = _Kiln(db, printers)
    adapter = printers[on]
    job = before.queue.submit(FILE, printer_name=on if assigned else None, submitted_by="test")
    out = before.sched.tick()
    assert [(d["job_id"], d["printer_name"]) for d in out["dispatched"]] == [(job, on)]
    if part:
        _part(adapter)
    _printing(adapter)
    before.sched.tick()
    assert before.status(job) == JobStatus.PRINTING
    if paused:
        before.queue.mark_paused(job)
        _printing(adapter, state=PrinterStatus.PAUSED)
    adapter.start_print.reset_mock()
    return job


def _left_starting(
    db: str, printers: dict[str, MagicMock], *, on: str = "A", assigned: bool = True, took_it: bool = False,
) -> str:
    """Kiln marks the job STARTING and stops inside the start, before the
    printer's answer came back.  *took_it*: the printer had taken it, so its
    start wrote the part to the plate record."""
    before = _Kiln(db, printers)
    adapter = printers[on]

    def _start(file_name: str, **_ids: Any) -> None:
        if took_it:
            _part(adapter, file_name)
        raise _Crash()

    adapter.start_print.side_effect = _start
    job = before.queue.submit(FILE, printer_name=on if assigned else None, submitted_by="test")
    with pytest.raises(_Crash):
        before.sched.tick()
    assert before.status(job) == JobStatus.STARTING
    adapter.start_print.side_effect = None
    adapter.start_print.reset_mock()
    return job


def _forget_dispatched_to(db: str, job_id: str) -> None:
    """Make *job_id* a job from before Kiln recorded where a start was sent."""
    conn = sqlite3.connect(db)
    metadata = json.loads(conn.execute("SELECT metadata FROM jobs WHERE id=?", (job_id,)).fetchone()[0] or "{}")
    metadata.pop("dispatched_to", None)
    conn.execute("UPDATE jobs SET metadata=? WHERE id=?", (json.dumps(metadata), job_id))
    conn.commit()
    conn.close()


def _rows(db: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The incident: a print that finished while Kiln was away
# ---------------------------------------------------------------------------


class TestAPrintThatFinishedWhileKilnWasAway:
    def test_it_is_closed_on_the_first_pass_and_not_started(self, db):
        """PRINTING when Kiln stopped; the printer is idle now and the plate
        record holds the part.  The first pass closes the job; nothing is
        started and nothing waits.

        A/B: with ``_reload_from_db`` restored to the old reset (status
        ``"queued"``, ``started_at=None``, ``_update_job_db`` per row) this
        fails at ``assert after.status(job) == JobStatus.COMPLETED`` --
        ``AssertionError: assert <JobStatus.QUEUED: 'queued'> ==
        <JobStatus.COMPLETED: 'completed'>`` (the job was back in line,
        held only by the plate wait).
        """
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _idle(a)

        after = _Kiln(db, {"A": a})
        result = after.sched.tick()

        a.start_print.assert_not_called()
        assert after.status(job) == JobStatus.COMPLETED
        assert result["completed"] == [job]
        assert job not in after.sched.active_jobs
        assert [e.data["job_id"] for e in after.events(EventType.JOB_COMPLETED)] == [job]
        assert (after.queue.get_job(job).metadata or {}).get("waiting") is None, "a closed job waits for nothing"

    def test_clearing_the_plate_does_not_print_it_a_second_time(self, db):
        """The reprint itself: after the restart a person clears the plate,
        and the next passes start nothing.

        A/B: with the old reload restored this fails at the last
        ``a.start_print.assert_not_called()`` -- ``AssertionError: Expected
        'start_print' to not have been called. Called 1 times. Calls:
        [call('bracket.gcode')].``
        """
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _idle(a)

        after = _Kiln(db, {"A": a})
        after.sched.tick()
        mark_clear(a, "human")
        after.ticks(3)

        a.start_print.assert_not_called()
        assert after.status(job) == JobStatus.COMPLETED

    def test_an_ending_the_printer_does_not_name_is_recorded_unknown(self, db):
        """Idle, naming no ending: Kiln did not see it end, so the outcome is
        ``unknown`` and the person is asked -- never a success on a guess."""
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _idle(a, ended=None)

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        assert after.status(job) == JobStatus.COMPLETED
        assert [(o["job_id"], o["printer_name"], o["outcome"], o["determined_by"]) for o in after.outcomes] == [
            (job, "A", "unknown", "inferred"),
        ]
        assert "restarted" in after.outcomes[0]["error_msg"]

    @pytest.mark.parametrize(
        ("ended", "outcome"),
        [(JobResult.COMPLETED, "success"), (JobResult.CANCELLED, "cancelled"), (JobResult.FAILED, "failed")],
    )
    def test_the_ending_the_printer_names_is_the_one_recorded(self, db, ended, outcome):
        """When the printer names how its last job ended, that is the ending
        recorded for the job, as testimony (``observed``)."""
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _idle(a, ended=ended)

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        a.start_print.assert_not_called()
        assert after.status(job) == JobStatus.COMPLETED
        assert [(o["job_id"], o["printer_name"], o["outcome"], o["determined_by"]) for o in after.outcomes] == [
            (job, "A", outcome, "observed"),
        ]


# ---------------------------------------------------------------------------
# 2. Still printing: watched again, as if Kiln had never stopped
# ---------------------------------------------------------------------------


class TestStillPrinting:
    def test_it_is_watched_again_and_closes_through_the_normal_path(self, db):
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _printing(a)  # still on it

        after = _Kiln(db, {"A": a})
        after.ticks(3)

        a.start_print.assert_not_called()
        assert after.status(job) == JobStatus.PRINTING
        assert after.sched.active_jobs == {job: "A"}
        assert job in after.sched._watch and job in after.sched._seen_printing
        assert after.outcomes == []

        _idle(a, ended=JobResult.COMPLETED)
        result = after.sched.tick()

        assert result["completed"] == [job]
        assert after.status(job) == JobStatus.COMPLETED
        assert after.sched.active_jobs == {}
        assert [(o["outcome"], o["determined_by"], o["contribute"]) for o in after.outcomes] == [
            ("success", "observed", True),
        ]
        a.start_print.assert_not_called()

    @pytest.mark.parametrize("reported", ["bracket.gcode", "/sdcard/cache/bracket.gcode", "bracket.gcode.3mf", None])
    def test_the_printer_may_name_the_file_its_own_way(self, db, reported):
        """A printer drops the folder, adds ``.3mf``, or names no file: it is
        still this job."""
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _printing(a, reported)

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        assert after.sched.active_jobs == {job: "A"}
        assert after.status(job) == JobStatus.PRINTING
        assert after.events(EventType.JOB_COMPLETED) == []


# ---------------------------------------------------------------------------
# 3. Paused
# ---------------------------------------------------------------------------


class TestPaused:
    def test_a_paused_print_on_a_paused_printer_stays_paused_and_is_watched(self, db, caplog):
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a}, paused=True)

        after = _Kiln(db, {"A": a})
        with caplog.at_level(logging.WARNING, logger="kiln.scheduler"):
            after.ticks(3)

        a.start_print.assert_not_called()
        assert after.status(job) == JobStatus.PAUSED
        assert after.sched.active_jobs == {job: "A"}
        assert "Error checking job" not in caplog.text

    def test_a_paused_print_whose_printer_is_idle_now_is_closed_not_looped(self, db, caplog):
        """The queue only ends a job from PRINTING, so a PAUSED job is moved
        there before it is watched for its one reading.

        A/B: with the ``mark_printing`` before the adopt-with-doubt removed
        from ``_settle_one``, this fails at ``assert after.status(job) ==
        JobStatus.COMPLETED`` -- ``assert <JobStatus.PAUSED: 'paused'> ==
        <JobStatus.COMPLETED: 'completed'>``; the log shows ``Error checking
        job ... Invalid state transition`` on every pass and the job never
        leaves ``active_jobs``.
        """
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a}, paused=True)
        _idle(a, ended=JobResult.CANCELLED)

        after = _Kiln(db, {"A": a})
        with caplog.at_level(logging.WARNING, logger="kiln.scheduler"):
            after.ticks(3)

        assert after.status(job) == JobStatus.COMPLETED
        assert after.sched.active_jobs == {}
        assert "Error checking job" not in caplog.text
        assert [o["outcome"] for o in after.outcomes] == ["cancelled"]
        a.start_print.assert_not_called()

    def test_a_paused_print_resumed_at_the_printer_while_kiln_was_away_completes(self, db, caplog):
        """Paused when Kiln stopped, resumed at the printer's own screen
        while Kiln was away: watched again, and it ends when the printer
        finishes it."""
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a}, paused=True)
        _printing(a)  # resumed at the printer

        after = _Kiln(db, {"A": a})
        after.sched.tick()
        assert after.sched.active_jobs == {job: "A"}

        _idle(a, ended=JobResult.COMPLETED)
        with caplog.at_level(logging.WARNING, logger="kiln.scheduler"):
            after.ticks(3)

        assert "Error checking job" not in caplog.text
        assert after.status(job) == JobStatus.COMPLETED
        assert after.sched.active_jobs == {}


# ---------------------------------------------------------------------------
# 4. Starting: did a printer take it?
# ---------------------------------------------------------------------------


class TestStarting:
    def test_a_start_no_printer_took_goes_back_in_line_and_starts_once(self, db):
        """Kiln stopped inside the start; the printer is idle and its plate
        record holds nothing.  The job is back in line and the same pass
        starts it, once.

        A/B: with ``_started_on`` returning ``True`` unconditionally this
        fails at ``a.start_print.assert_called_once_with("bracket.gcode")``
        -- ``Expected 'start_print' to be called once. Called 0 times.``
        (the job was closed as a print nobody saw end, and never printed).
        """
        a = _printer("A", "SER-A")
        job = _left_starting(db, {"A": a})

        after = _Kiln(db, {"A": a})
        result = after.sched.tick()

        a.start_print.assert_called_once_with("bracket.gcode")
        assert [d["job_id"] for d in result["dispatched"]] == [job]
        assert after.status(job) == JobStatus.PRINTING
        assert after.events(EventType.JOB_COMPLETED) == []
        assert after.outcomes == []

    def test_a_start_the_plate_record_shows_was_taken_is_closed_not_requeued(self, db):
        """Kiln stopped inside the start, but the printer had taken it: the
        plate record holds the part from after the start was sent.  The
        printer is idle now, so it ran; it is closed, never sent again.

        A/B: with ``_started_on`` returning ``False`` unconditionally this
        fails at ``assert after.status(job) == JobStatus.COMPLETED`` --
        ``assert <JobStatus.QUEUED: 'queued'> == <JobStatus.COMPLETED:
        'completed'>`` (back in line behind its own part on the plate).
        """
        a = _printer("A", "SER-A")
        job = _left_starting(db, {"A": a}, took_it=True)
        _idle(a)

        after = _Kiln(db, {"A": a})
        result = after.sched.tick()

        assert after.status(job) == JobStatus.COMPLETED
        assert result["completed"] == [job]
        a.start_print.assert_not_called()
        mark_clear(a, "human")
        after.ticks(2)
        a.start_print.assert_not_called()

    @pytest.mark.parametrize(("age_s", "taken"), [(60, True), (600, False)])
    def test_only_a_part_from_this_start_is_evidence(self, db, age_s, taken):
        """The plate record counts only when it was written at or after the
        job's own start, less two minutes of slack: a part from an earlier
        print of the same file says nothing about this start."""
        a = _printer("A", "SER-A")
        job = _left_starting(db, {"A": a}, took_it=True)
        _backdate_plate(a, age_s)
        _idle(a)

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        a.start_print.assert_not_called()  # the part on the plate holds a requeued job too
        if taken:
            assert after.status(job) == JobStatus.COMPLETED
        else:
            assert after.status(job) == JobStatus.QUEUED
            assert (after.queue.get_job(job).metadata or {}).get("waiting", {}).get("kind") == "plate"

    def test_a_start_the_printer_is_printing_is_watched_and_promoted(self, db):
        a = _printer("A", "SER-A")
        job = _left_starting(db, {"A": a}, took_it=True)
        _printing(a)

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        assert after.status(job) == JobStatus.PRINTING
        assert after.sched.active_jobs == {job: "A"}
        a.start_print.assert_not_called()


# ---------------------------------------------------------------------------
# 5. The printer is busy with a different file
# ---------------------------------------------------------------------------


class TestThePrinterMovedOn:
    @pytest.mark.parametrize("paused", [False, True])
    def test_a_print_that_ran_is_closed_unknown_and_not_seen(self, db, paused):
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a}, paused=paused)
        _printing(a, "lid.gcode")

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        assert after.status(job) == JobStatus.COMPLETED
        assert after.sched.active_jobs == {}
        completed = after.events(EventType.JOB_COMPLETED)
        assert [(e.data["job_id"], e.data["printer_name"], e.data["ending"]) for e in completed] == [
            (job, "A", "not_seen"),
        ]
        assert [(o["outcome"], o["determined_by"], o["printer_name"]) for o in after.outcomes] == [
            ("unknown", "inferred", "A"),
        ]
        assert "lid" in after.outcomes[0]["error_msg"]
        a.start_print.assert_not_called()

    def test_a_start_no_printer_took_goes_back_in_line(self, db):
        a = _printer("A", "SER-A")
        job = _left_starting(db, {"A": a})
        _printing(a, "lid.gcode")

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        assert after.status(job) == JobStatus.QUEUED
        assert after.queue.get_job(job).started_at is None
        assert after.events(EventType.JOB_COMPLETED) == [] and after.outcomes == []
        a.start_print.assert_not_called()  # A is busy


# ---------------------------------------------------------------------------
# 6. The printer is not answering at the restart
# ---------------------------------------------------------------------------


def _offline(adapter: MagicMock, how: str) -> None:
    if how == "disconnected":
        adapter.get_state.return_value = PrinterState(connected=False, state=PrinterStatus.OFFLINE)
    elif how == "raises":
        adapter.get_state.side_effect = ConnectionError("no route to host")
    elif how == "stale":
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.STALE, last_known_state=PrinterStatus.IDLE,
        )
    else:  # pragma: no cover
        raise AssertionError(how)


class TestThePrinterIsNotAnswering:
    @pytest.mark.parametrize("how", ["disconnected", "raises", "stale"])
    def test_it_waits_says_so_and_is_settled_when_the_printer_answers(self, db, how, monkeypatch):
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _offline(a, how)

        after = _Kiln(db, {"A": a})
        after.ticks(3)

        a.start_print.assert_not_called()
        assert after.status(job) == JobStatus.PRINTING, "a job whose printer is silent keeps its state"
        assert job not in after.sched.active_jobs
        note = after.queue.get_job(job).metadata["waiting"]
        assert note["kind"] == WAIT_RECOVERING and note["printers"] == ["A"]
        assert note["sentence"] == (
            "Kiln was restarted while bracket was printing on A, and A is not answering yet. Kiln picks it back "
            "up as soon as it does; nothing is started again in the meantime."
        )
        assert describe_wait(after.queue.get_job(job), after.queue) == {**note, "ahead": 0}
        assert PrintQueue(db_path=db).get_job(job).metadata["waiting"] == note, "the note is on the store"

        import kiln.server as srv
        from kiln.plugins import queue_tools

        monkeypatch.setattr(srv, "_get_queue", lambda: after.queue)
        monkeypatch.setattr(srv, "_get_registry", lambda: after.registry)
        monkeypatch.setattr(srv, "_scheduler", after.sched)
        monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
        out = queue_tools.job_status(job)
        assert out["success"] is True and out["waiting"]["sentence"] == note["sentence"]

        a.get_state.side_effect = None
        _idle(a, ended=JobResult.COMPLETED)
        after.sched.tick()

        assert after.status(job) == JobStatus.COMPLETED
        assert "waiting" not in (after.queue.get_job(job).metadata or {})
        stored = sqlite3.connect(db).execute("SELECT metadata FROM jobs WHERE id=?", (job,)).fetchone()[0]
        assert "waiting" not in json.loads(stored), "and off the store"
        assert [o["outcome"] for o in after.outcomes] == ["success"]
        a.start_print.assert_not_called()

    def test_a_silent_printer_holding_a_start_says_starting(self, db):
        a = _printer("A", "SER-A")
        job = _left_starting(db, {"A": a})
        _offline(a, "disconnected")

        after = _Kiln(db, {"A": a})
        after.sched.tick()

        assert after.status(job) == JobStatus.STARTING
        assert after.queue.get_job(job).metadata["waiting"]["sentence"].startswith(
            "Kiln was restarted while bracket was starting on A",
        )

        _idle(a)
        after.sched.tick()
        a.start_print.assert_called_once_with("bracket.gcode")  # nothing took it: back in line, started
        assert after.status(job) == JobStatus.PRINTING
        assert "waiting" not in after.queue.get_job(job).metadata


# ---------------------------------------------------------------------------
# 7. A job that named no printer
# ---------------------------------------------------------------------------


class TestAJobThatNamedNoPrinter:
    def test_the_printer_it_was_sent_to_is_kept_and_used(self, db):
        """``mark_starting`` keeps where the start went, on the store; after
        a restart that is the printer asked, even with no plate record."""
        a, b = _printer("A", "SER-A"), _printer("B", "SER-B")
        job = _left_printing(db, {"A": a}, assigned=False, part=False)
        assert PrintQueue(db_path=db).get_job(job).metadata["dispatched_to"] == "A"
        _printing(a)
        _printing(b, "lid.gcode")

        after = _Kiln(db, {"B": b, "A": a})
        after.sched.tick()

        assert after.sched.active_jobs == {job: "A"}
        assert after.status(job) == JobStatus.PRINTING
        a.start_print.assert_not_called()
        b.start_print.assert_not_called()

    def test_an_older_job_is_found_on_the_plate_that_holds_its_part(self, db):
        a, b = _printer("A", "SER-A"), _printer("B", "SER-B")
        job = _left_printing(db, {"A": a}, assigned=False)
        _forget_dispatched_to(db, job)
        _idle(a, ended=JobResult.COMPLETED)
        mark_occupied(b, PlateJob(file="lid.gcode"))

        after = _Kiln(db, {"B": b, "A": a})
        assert "dispatched_to" not in after.queue.get_job(job).metadata
        after.sched.tick()

        assert after.status(job) == JobStatus.COMPLETED
        assert [(o["printer_name"], o["outcome"]) for o in after.outcomes] == [("A", "success")]
        assert [e.data["printer_name"] for e in after.events(EventType.JOB_COMPLETED)] == ["A"]

    def test_an_older_start_with_no_evidence_anywhere_goes_back_in_line(self, db):
        a = _printer("A", "SER-A")
        before = _Kiln(db, {"A": a})
        job = before.queue.submit(FILE, submitted_by="test")
        before.queue.mark_starting(job)  # a start from before Kiln recorded where it went
        assert "dispatched_to" not in before.queue.get_job(job).metadata

        after = _Kiln(db, {"A": a})
        result = after.sched.tick()

        a.start_print.assert_called_once_with("bracket.gcode")
        assert [d["job_id"] for d in result["dispatched"]] == [job]
        assert result["failed"] == []

    def test_an_older_print_with_no_evidence_anywhere_fails_and_says_to_queue_it_again(self, db):
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a}, assigned=False, part=False)
        _forget_dispatched_to(db, job)
        _idle(a)

        after = _Kiln(db, {"A": a})
        result = after.ticks(2)

        assert after.status(job) == JobStatus.FAILED
        assert after.queue.get_job(job).error == (
            "Kiln was restarted while bracket was printing, and it cannot tell on which printer or whether it "
            "finished, so it will not print it again. If it did not print, queue it again."
        )
        assert result[0]["failed"] == [{"job_id": job, "error": "unsettled after restart"}]
        assert result[1]["failed"] == []
        failed = after.events(EventType.JOB_FAILED)
        assert [(e.data["job_id"], e.data["reason"]) for e in failed] == [(job, "unsettled_after_restart")]
        a.start_print.assert_not_called()


# ---------------------------------------------------------------------------
# 8. The printer is no longer set up
# ---------------------------------------------------------------------------


class TestThePrinterWasRemoved:
    @pytest.mark.parametrize("assigned", [True, False])
    def test_the_job_fails_with_its_sentence(self, db, assigned):
        a, b = _printer("A", "SER-A"), _printer("B", "SER-B")
        job = _left_printing(db, {"A": a}, assigned=assigned)

        after = _Kiln(db, {"B": b})
        result = after.sched.tick()

        assert after.status(job) == JobStatus.FAILED
        assert after.queue.get_job(job).error == (
            "Kiln was restarted while bracket was on A, and A is no longer set up in Kiln, so how it ended "
            "cannot be read. It will not print again; queue it again if it did not print."
        )
        assert result["failed"] == [{"job_id": job, "error": "printer no longer registered"}]
        b.start_print.assert_not_called()


# ---------------------------------------------------------------------------
# 9. Steady state: a watched job is never settled
# ---------------------------------------------------------------------------


class TestSteadyState:
    def test_a_watched_job_costs_one_read_a_pass_and_is_never_settled(self, db, monkeypatch):
        """One job dispatched and watched by this run: each pass reads its
        printer once (phase 1), as before phase 0 existed.  Run with
        ``_settle_unfinished`` made a no-op (phase 0 as it was before this
        change), the count is the same 5 and the test passes unchanged."""
        a = _printer("A", "SER-A")
        run = _Kiln(db, {"A": a})
        job = run.queue.submit(FILE, printer_name="A", submitted_by="test")
        run.sched.tick()
        _printing(a)
        settled: list[str] = []
        real = run.sched._settle_one
        monkeypatch.setattr(run.sched, "_settle_one", lambda j, f: (settled.append(j.id), real(j, f)))
        a.get_state.reset_mock()

        run.ticks(5)

        assert settled == []
        assert a.get_state.call_count == 5
        assert run.sched.active_jobs == {job: "A"}

    def test_an_adopted_job_is_settled_once(self, db, monkeypatch):
        a = _printer("A", "SER-A")
        job = _left_printing(db, {"A": a})
        _printing(a)

        after = _Kiln(db, {"A": a})
        settled: list[str] = []
        real = after.sched._settle_one
        monkeypatch.setattr(after.sched, "_settle_one", lambda j, f: (settled.append(j.id), real(j, f)))
        after.sched.tick()
        a.get_state.reset_mock()
        after.ticks(4)

        assert settled == [job]
        assert a.get_state.call_count == 4


# ---------------------------------------------------------------------------
# 10. The reload
# ---------------------------------------------------------------------------


class TestTheReload:
    def test_unfinished_jobs_come_back_as_they_were_and_nothing_is_rewritten(self, db, monkeypatch):
        """A/B: with the old reload restored this fails at ``assert after ==
        before`` -- the STARTING, PRINTING and PAUSED jobs read back as
        ``(JobStatus.QUEUED, None)``."""
        q = PrintQueue(db_path=db)
        queued = q.submit("a.gcode")
        starting = q.submit("b.gcode")
        printing = q.submit("c.gcode")
        paused = q.submit("d.gcode")
        q.mark_starting(starting, printer_name="A")
        for jid in (printing, paused):
            q.mark_starting(jid, printer_name="A")
            q.mark_printing(jid)
        q.mark_paused(paused)
        before = {jid: (q.get_job(jid).status, q.get_job(jid).started_at) for jid in (queued, starting, printing, paused)}
        rows = _rows(db)

        writes: list[str] = []
        monkeypatch.setattr(PrintQueue, "_update_job_db", lambda self, job: writes.append(job.id))
        monkeypatch.setattr(PrintQueue, "_update_metadata_db", lambda self, job: writes.append(job.id))
        again = PrintQueue(db_path=db)

        after = {jid: (again.get_job(jid).status, again.get_job(jid).started_at) for jid in before}
        assert after == before
        assert [s for s, _ in after.values()] == [
            JobStatus.QUEUED, JobStatus.STARTING, JobStatus.PRINTING, JobStatus.PAUSED,
        ]
        assert after[queued][1] is None and all(after[j][1] is not None for j in (starting, printing, paused))
        assert writes == [], "the reload rewrites no row"
        assert _rows(db) == rows

    def test_mark_starting_keeps_the_printer_on_the_store_only_when_named(self, db):
        q = PrintQueue(db_path=db)
        named = q.submit("a.gcode", metadata={"material_type": "PLA"})
        unnamed = q.submit("b.gcode")
        q.mark_starting(named, printer_name="A")
        q.mark_starting(unnamed)

        again = PrintQueue(db_path=db)
        assert again.get_job(named).metadata == {"material_type": "PLA", "dispatched_to": "A"}
        assert again.get_job(unnamed).metadata == {}

    def test_the_scheduler_records_where_it_sent_the_start(self, db):
        a = _printer("A", "SER-A")
        run = _Kiln(db, {"A": a})
        job = run.queue.submit(FILE, submitted_by="test")
        run.sched.tick()
        assert run.queue.get_job(job).metadata["dispatched_to"] == "A"


# ---------------------------------------------------------------------------
# 11. One file, however it is named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("bracket.gcode", "bracket.gcode", True),
        ("/sdcard/cache/bracket.gcode", "bracket.gcode", True),  # base name
        ("bracket.gcode.3mf", "bracket.gcode", True),  # the name a person reads
        ("cache/bracket.gcode.3mf", "/tmp/kiln/bracket.gcode", True),  # folder and extension both differ
        ("bracket.gcode", "lid.gcode", False),
        ("bracket_v2.gcode", "bracket.gcode", False),
        (None, "bracket.gcode", False),
        ("bracket.gcode", "", False),
    ],
)
def test_same_file(a, b, same):
    assert _same_file(a, b) is same
    assert _same_file(b, a) is same


# --- defects a lap found (2026-10-25; each fails at 5cbf08d2 with the recovery applied) ---


class TestDefectsTheLapFound:
    """Found by the restart tests themselves and kept here for the engine,
    not the restart: the phase-1 ending path assumed a watched job is
    PRINTING in the queue."""

    def test_a_job_paused_in_the_queue_whose_printer_goes_idle_ends_without_looping(self, db, caplog):
        """No restart: a watched job the queue holds PAUSED (paused through
        Kiln, then cancelled at the printer's screen) and a printer that goes
        idle.  The queue refused paused -> completed, logged it every pass,
        and the job never left the active set."""
        a = _printer("A", "SER-A")
        kiln = _Kiln(db, {"A": a})
        job = kiln.queue.submit(file_name=FILE, printer_name="A", submitted_by="t")
        kiln.ticks(1)                                   # dispatched on the idle printer
        _printing(a)
        kiln.ticks(1)                                   # watched printing
        kiln.queue.mark_paused(job)
        _idle(a, JobResult.CANCELLED)
        with caplog.at_level(logging.WARNING, logger="kiln.scheduler"):
            kiln.ticks(3)
        assert kiln.status(job) == JobStatus.COMPLETED
        assert job not in kiln.sched.active_jobs
        assert not [r for r in caplog.records if "Invalid state transition" in r.getMessage()]

    def test_a_job_for_a_printer_not_set_up_says_so(self, db):
        """It said the printer was "busy or not answering", which sent a
        person to look at a machine Kiln has never been told about."""
        kiln = _Kiln(db, {"A": _printer("A", "SER-A")})
        job = kiln.queue.submit(file_name=FILE, printer_name="garage-mk4", submitted_by="t")
        kiln.ticks(1)
        wait = kiln.sched.waiting_for(job)
        assert wait["kind"] == "no_printer"
        assert "garage-mk4, which is not set up in Kiln" in wait["sentence"]

    def test_the_restart_check_reads_only_live_jobs(self, db):
        """Every pass listed and sorted the whole job history to find the few
        a restart left behind; it now reads only jobs a printer may be on."""
        kiln = _Kiln(db, {"A": _printer("A", "SER-A")})
        for i in range(5):
            jid = kiln.queue.submit(file_name=f"done_{i}.gcode", printer_name="A", submitted_by="t")
            kiln.queue.mark_starting(jid)
            kiln.queue.mark_printing(jid)
            kiln.queue.mark_completed(jid)
        spy = MagicMock(wraps=kiln.queue.list_jobs)
        kiln.queue.list_jobs = spy
        kiln.sched._settle_unfinished([])
        spy.assert_not_called()
