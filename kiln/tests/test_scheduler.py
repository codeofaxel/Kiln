"""Tests for kiln.scheduler -- job scheduler dispatching.

Covers:
- Scheduler start/stop lifecycle
- tick() dispatches a queued job to an idle printer
- tick() detects completed jobs (printer returned to IDLE)
- tick() detects failed jobs (printer in ERROR state)
- tick() handles printer not found (unregistered mid-job)
- tick() skips busy printers (already have active jobs)
- tick() updates STARTING jobs to PRINTING when printer reports printing
- tick() publishes progress events
- Priority ordering -- high-priority job dispatched first
- Printer-name targeting -- job targeting specific printer only dispatched there
- Any-printer jobs dispatched to first available idle printer
- start_print failure handling (adapter returns success=False)
- PrinterError during dispatch
- Thread safety of active_jobs property
- Multiple dispatch in single tick (multiple idle printers, multiple queued jobs)
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln.events import EventBus, EventType
from kiln.printers.base import (
    JobProgress,
    JobResult,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
    PrintResult,
)
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterRegistry
from kiln.scheduler import JobScheduler

# ---------------------------------------------------------------------------
# Helpers -- mock adapter factory
# ---------------------------------------------------------------------------

def make_mock_adapter(
    name: str = "mock-printer",
    state: PrinterStatus = PrinterStatus.IDLE,
    connected: bool = True,
    completion: float | None = None,
    file_name: str | None = None,
    start_print_success: bool = True,
    start_print_message: str = "OK",
) -> MagicMock:
    """Create a MagicMock that behaves like a PrinterAdapter.

    Args:
        name: The adapter name.
        state: Initial printer state.
        connected: Whether the printer reports connected.
        completion: Job completion percentage (None if no job).
        file_name: File name reported by get_job.
        start_print_success: Whether start_print returns success.
        start_print_message: Message from start_print result.
    """
    adapter = MagicMock()
    type(adapter).name = PropertyMock(return_value=name)
    type(adapter).capabilities = PropertyMock(return_value=PrinterCapabilities())

    adapter.get_state.return_value = PrinterState(
        connected=connected,
        state=state,
    )
    adapter.get_job.return_value = JobProgress(
        file_name=file_name,
        completion=completion,
    )
    adapter.start_print.return_value = PrintResult(
        success=start_print_success,
        message=start_print_message,
    )
    return adapter


class _Clock:
    """A monotonic clock the tests can move by hours in a millisecond."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _run_silent(scheduler: JobScheduler, clock: _Clock, *, hours: float, step_minutes: float = 5.0) -> list[dict]:
    """Tick through *hours* of wall time with the adapter's readings unchanged.

    Returns every ``failed`` entry the ticks reported, in order.
    """
    failed: list[dict] = []
    for _ in range(int(hours * 60 / step_minutes)):
        clock.advance(step_minutes * 60)
        failed.extend(scheduler.tick()["failed"])
    return failed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def queue():
    return PrintQueue()


@pytest.fixture()
def registry():
    return PrinterRegistry()


@pytest.fixture()
def event_bus():
    return EventBus()


@pytest.fixture()
def scheduler(queue, registry, event_bus):
    return JobScheduler(queue, registry, event_bus, poll_interval=0.1, max_retries=0)


@pytest.fixture(autouse=True)
def _reset_emergency_state(monkeypatch):
    """Keep scheduler tests isolated from any local persisted E-stop state."""
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    import kiln.emergency as _emergency_mod

    _emergency_mod._coordinator = None
    yield
    _emergency_mod._coordinator = None


# ---------------------------------------------------------------------------
# 1. Scheduler start / stop lifecycle
# ---------------------------------------------------------------------------

class TestSchedulerLifecycle:
    """Tests for start() and stop() methods."""

    def test_is_running_false_initially(self, scheduler):
        assert scheduler.is_running is False

    def test_start_sets_running(self, scheduler):
        scheduler.start()
        assert scheduler.is_running is True
        scheduler.stop()

    def test_stop_clears_running(self, scheduler):
        scheduler.start()
        scheduler.stop()
        assert scheduler.is_running is False

    def test_stop_does_not_wait_out_the_poll_doze(self, queue, registry, event_bus):
        """stop() sits on the server's SIGTERM path, and the loop used to
        doze in a plain ``time.sleep(poll_interval)`` nothing could wake
        — with the production 5s poll that stalled shutdown for seconds
        (measured live 2026-08-09).  The doze must be interruptible."""
        import time as _time

        sched = JobScheduler(queue, registry, event_bus, poll_interval=30.0)
        sched.start()
        started = _time.monotonic()
        sched.stop()
        assert _time.monotonic() - started < 2.0, (
            "stop() waited out the poll sleep instead of waking it"
        )

    def test_restart_after_stop_dozes_again(self, queue, registry, event_bus):
        """The stop event must re-arm on start, or a restarted scheduler
        spins its loop hot instead of dozing between ticks."""
        sched = JobScheduler(queue, registry, event_bus, poll_interval=30.0)
        sched.start()
        sched.stop()
        sched.start()
        assert sched.is_running
        assert not sched._stop_event.is_set()
        sched.stop()

    def test_start_is_idempotent(self, scheduler):
        scheduler.start()
        thread1 = scheduler._thread
        scheduler.start()  # second call should be a no-op
        assert scheduler._thread is thread1
        scheduler.stop()

    def test_stop_without_start_is_safe(self, scheduler):
        # Should not raise
        scheduler.stop()
        assert scheduler.is_running is False

    def test_background_thread_is_daemon(self, scheduler):
        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        scheduler.stop()

    def test_background_thread_name(self, scheduler):
        scheduler.start()
        assert scheduler._thread.name == "kiln-scheduler"
        scheduler.stop()


# ---------------------------------------------------------------------------
# 2. tick() dispatches a queued job to an idle printer
# ---------------------------------------------------------------------------

class TestDispatchJob:
    """Tests for basic job dispatching in tick()."""

    def test_dispatch_single_job(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)

        job_id = queue.submit(file_name="benchy.gcode", submitted_by="test")
        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["job_id"] == job_id
        assert result["dispatched"][0]["printer_name"] == "printer-1"
        assert result["dispatched"][0]["file_name"] == "benchy.gcode"

        adapter.start_print.assert_called_once_with("benchy.gcode")
        assert queue.get_job(job_id).status == JobStatus.PRINTING

    def test_dispatch_publishes_job_started_event(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        scheduler.tick()

        events = event_bus.recent_events(EventType.JOB_STARTED)
        assert len(events) == 1
        assert events[0].data["job_id"] == job_id
        assert events[0].data["printer_name"] == "printer-1"
        assert events[0].source == "scheduler"

    def test_no_dispatch_when_no_queued_jobs(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)

        result = scheduler.tick()
        assert result["dispatched"] == []
        adapter.start_print.assert_not_called()

    def test_no_dispatch_when_no_idle_printers(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1", state=PrinterStatus.PRINTING)
        registry.register("printer-1", adapter)

        queue.submit(file_name="benchy.gcode")
        result = scheduler.tick()
        assert result["dispatched"] == []

    def test_dispatched_job_tracked_in_active_jobs(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        scheduler.tick()

        active = scheduler.active_jobs
        assert job_id in active
        assert active[job_id] == "printer-1"


# ---------------------------------------------------------------------------
# 3. tick() detects completed jobs (printer returned to IDLE)
# ---------------------------------------------------------------------------

class TestCompletedJobs:
    """Tests for detecting completed jobs."""

    def test_job_completed_when_printer_idle(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        # Dispatch the job first
        scheduler.tick()
        assert queue.get_job(job_id).status == JobStatus.PRINTING

        # Printer returns to idle -- job is done
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        result = scheduler.tick()

        assert job_id in result["completed"]
        assert queue.get_job(job_id).status == JobStatus.COMPLETED
        assert job_id not in scheduler.active_jobs

    def test_completed_publishes_event(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        scheduler.tick()

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        scheduler.tick()

        events = event_bus.recent_events(EventType.JOB_COMPLETED)
        assert len(events) == 1
        assert events[0].data["job_id"] == job_id
        assert events[0].data["printer_name"] == "printer-1"


# ---------------------------------------------------------------------------
# 4. tick() detects failed jobs (printer in ERROR state)
# ---------------------------------------------------------------------------

class TestFailedJobs:
    """Tests for detecting failed jobs from printer error states."""

    def test_job_failed_when_printer_error(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        # Dispatch
        scheduler.tick()

        # Printer enters error state
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.ERROR
        )
        result = scheduler.tick()

        assert len(result["failed"]) == 1
        assert result["failed"][0]["job_id"] == job_id
        assert "error state" in result["failed"][0]["error"]
        assert queue.get_job(job_id).status == JobStatus.FAILED
        assert job_id not in scheduler.active_jobs

    def test_failed_publishes_event(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        scheduler.tick()

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.ERROR
        )
        scheduler.tick()

        events = event_bus.recent_events(EventType.JOB_FAILED)
        assert len(events) == 1
        assert events[0].data["job_id"] == job_id
        assert "error" in events[0].data
        assert "printer-1" in events[0].data["error"]


# ---------------------------------------------------------------------------
# 5. tick() handles printer not found (unregistered mid-job)
# ---------------------------------------------------------------------------

class TestPrinterNotFound:
    """Tests for handling printers that disappear mid-job."""

    def test_job_failed_when_printer_unregistered(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        scheduler.tick()
        assert job_id in scheduler.active_jobs

        # Remove the printer mid-job
        registry.unregister("printer-1")
        result = scheduler.tick()

        assert len(result["failed"]) == 1
        assert result["failed"][0]["job_id"] == job_id
        assert "no longer registered" in result["failed"][0]["error"]
        assert queue.get_job(job_id).status == JobStatus.FAILED
        assert job_id not in scheduler.active_jobs


# ---------------------------------------------------------------------------
# 6. tick() skips busy printers (already have active jobs)
# ---------------------------------------------------------------------------

class TestSkipBusyPrinters:
    """Tests for skipping printers that already have active jobs."""

    def test_does_not_dispatch_to_busy_printer(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1", state=PrinterStatus.IDLE)
        registry.register("printer-1", adapter)

        # Submit and dispatch first job
        job_id1 = queue.submit(file_name="first.gcode")
        scheduler.tick()
        assert job_id1 in scheduler.active_jobs

        # Now the printer is still "idle" as far as get_idle_printers() sees
        # but the scheduler should know it is busy via active_jobs.
        # We need the printer to stay in PRINTING state now.
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )

        job_id2 = queue.submit(file_name="second.gcode")
        result = scheduler.tick()

        # Second job should not be dispatched (printer is busy)
        assert result["dispatched"] == []
        assert queue.get_job(job_id2).status == JobStatus.QUEUED

    def test_busy_printer_filtered_from_available(
        self, queue, registry, event_bus, scheduler
    ):
        """Even if the adapter reports IDLE, scheduler tracks active_jobs."""
        adapter = make_mock_adapter(name="printer-1", state=PrinterStatus.IDLE)
        registry.register("printer-1", adapter)

        queue.submit(file_name="first.gcode")
        scheduler.tick()

        # Adapter still reports idle (race condition), but active_jobs tracks it
        # The printer won't appear in idle_printers because it is PRINTING now
        # after dispatch. But let's explicitly test the busy filter by keeping
        # the adapter reporting idle but having an active job.
        # To test the filter directly, we inject into active_jobs.
        with scheduler._lock:
            scheduler._active_jobs["fake-job"] = "printer-1"

        queue.submit(file_name="second.gcode")
        result = scheduler.tick()

        # The printer-1 should be filtered out because it has an active job
        dispatched_printers = [d["printer_name"] for d in result["dispatched"]]
        assert "printer-1" not in dispatched_printers


# ---------------------------------------------------------------------------
# 7. tick() updates STARTING jobs to PRINTING
# ---------------------------------------------------------------------------

class TestStartingToPrinting:
    """Tests for the STARTING -> PRINTING transition."""

    def test_starting_promoted_to_printing(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        # Dispatch -- job goes QUEUED -> STARTING -> PRINTING
        scheduler.tick()

        # The job should already be PRINTING after successful dispatch
        assert queue.get_job(job_id).status == JobStatus.PRINTING

        # Now simulate: set job back to STARTING manually (edge case)
        # and have the printer report PRINTING
        queue._jobs[job_id].status = JobStatus.STARTING
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        adapter.get_job.return_value = JobProgress(completion=10.0, file_name="benchy.gcode")

        scheduler.tick()
        assert queue.get_job(job_id).status == JobStatus.PRINTING


# ---------------------------------------------------------------------------
# 8. tick() publishes progress events
# ---------------------------------------------------------------------------

class TestProgressEvents:
    """Tests for progress event publishing."""

    def test_progress_event_published(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        scheduler.tick()

        # Printer now printing with progress
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        adapter.get_job.return_value = JobProgress(
            file_name="benchy.gcode", completion=45.5
        )
        scheduler.tick()

        events = event_bus.recent_events(EventType.PRINT_PROGRESS)
        assert len(events) == 1
        assert events[0].data["job_id"] == job_id
        assert events[0].data["completion"] == 45.5
        assert events[0].data["printer_name"] == "printer-1"
        assert events[0].data["file_name"] == "benchy.gcode"

    def test_no_progress_event_when_completion_is_none(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        queue.submit(file_name="benchy.gcode")

        scheduler.tick()

        # Printer printing but completion is None
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        adapter.get_job.return_value = JobProgress(
            file_name="benchy.gcode", completion=None
        )
        scheduler.tick()

        events = event_bus.recent_events(EventType.PRINT_PROGRESS)
        assert len(events) == 0


# ---------------------------------------------------------------------------
# 9. Priority ordering -- high-priority job dispatched first
# ---------------------------------------------------------------------------

class TestPriorityOrdering:
    """Tests for priority-based dispatch ordering."""

    def test_high_priority_dispatched_first(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)

        # Submit low priority first, then high priority
        low_id = queue.submit(file_name="low.gcode", priority=0)
        time.sleep(0.01)
        high_id = queue.submit(file_name="high.gcode", priority=10)

        result = scheduler.tick()

        # Only one printer, so only one job dispatched -- should be high priority
        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["job_id"] == high_id
        assert result["dispatched"][0]["file_name"] == "high.gcode"

        # Low priority job remains queued
        assert queue.get_job(low_id).status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# 10. Printer-name targeting
# ---------------------------------------------------------------------------

class TestPrinterTargeting:
    """Tests for printer-specific job targeting."""

    def test_targeted_job_only_dispatched_to_correct_printer(
        self, queue, registry, event_bus, scheduler
    ):
        adapter_a = make_mock_adapter(name="printer-a")
        adapter_b = make_mock_adapter(name="printer-b")
        registry.register("printer-a", adapter_a)
        registry.register("printer-b", adapter_b)

        # Job specifically targeting printer-b
        queue.submit(
            file_name="targeted.gcode", printer_name="printer-b"
        )
        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["printer_name"] == "printer-b"
        adapter_b.start_print.assert_called_once_with("targeted.gcode")
        adapter_a.start_print.assert_not_called()

    def test_targeted_job_not_dispatched_to_wrong_printer(
        self, queue, registry, event_bus, scheduler
    ):
        adapter_a = make_mock_adapter(name="printer-a")
        registry.register("printer-a", adapter_a)

        # Job targeting printer-b, but only printer-a is registered
        job_id = queue.submit(
            file_name="targeted.gcode", printer_name="printer-b"
        )
        result = scheduler.tick()

        assert result["dispatched"] == []
        assert queue.get_job(job_id).status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# 11. Any-printer jobs dispatched to first available idle printer
# ---------------------------------------------------------------------------

class TestAnyPrinterJobs:
    """Tests for jobs with printer_name=None."""

    def test_any_printer_job_dispatched_to_idle(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)

        job_id = queue.submit(file_name="any.gcode", printer_name=None)
        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["printer_name"] == "printer-1"
        assert result["dispatched"][0]["job_id"] == job_id

    def test_any_printer_job_dispatched_to_first_available(
        self, queue, registry, event_bus, scheduler
    ):
        adapter_a = make_mock_adapter(name="printer-a")
        adapter_b = make_mock_adapter(name="printer-b")
        registry.register("printer-a", adapter_a)
        registry.register("printer-b", adapter_b)

        queue.submit(file_name="any.gcode", printer_name=None)
        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        # Should dispatch to one of the available printers
        dispatched_printer = result["dispatched"][0]["printer_name"]
        assert dispatched_printer in ("printer-a", "printer-b")


# ---------------------------------------------------------------------------
# 12. start_print failure handling (adapter returns success=False)
# ---------------------------------------------------------------------------

class TestStartPrintFailure:
    """Tests for handling start_print returning failure."""

    def test_start_print_failure_marks_job_failed(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(
            name="printer-1",
            start_print_success=False,
            start_print_message="File not found on printer",
        )
        registry.register("printer-1", adapter)

        job_id = queue.submit(file_name="missing.gcode")
        result = scheduler.tick()

        assert len(result["failed"]) == 1
        assert result["failed"][0]["job_id"] == job_id
        assert "File not found on printer" in result["failed"][0]["error"]
        assert queue.get_job(job_id).status == JobStatus.FAILED

    def test_start_print_failure_publishes_event(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(
            name="printer-1",
            start_print_success=False,
            start_print_message="nozzle clogged",
        )
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="test.gcode")

        scheduler.tick()

        events = event_bus.recent_events(EventType.JOB_FAILED)
        assert len(events) == 1
        assert events[0].data["job_id"] == job_id

    def test_start_print_failure_does_not_add_to_active_jobs(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(
            name="printer-1",
            start_print_success=False,
            start_print_message="fail",
        )
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="test.gcode")

        scheduler.tick()

        assert job_id not in scheduler.active_jobs

    def test_start_print_failure_with_empty_message(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(
            name="printer-1",
            start_print_success=False,
            start_print_message="",
        )
        registry.register("printer-1", adapter)
        queue.submit(file_name="test.gcode")

        result = scheduler.tick()

        # An adapter that fails without saying why still produces a usable
        # error: the shared print-start resolver names the file.
        assert len(result["failed"]) == 1
        assert "did not start test.gcode" in result["failed"][0]["error"]


# ---------------------------------------------------------------------------
# 13. PrinterError during dispatch
# ---------------------------------------------------------------------------

class TestPrinterErrorDuringDispatch:
    """Tests for PrinterError raised during start_print."""

    def test_printer_error_marks_job_failed(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1")
        adapter.start_print.side_effect = PrinterError("Connection lost")
        registry.register("printer-1", adapter)

        job_id = queue.submit(file_name="benchy.gcode")
        result = scheduler.tick()

        assert len(result["failed"]) == 1
        assert result["failed"][0]["job_id"] == job_id
        assert "Connection lost" in result["failed"][0]["error"]
        assert queue.get_job(job_id).status == JobStatus.FAILED

    def test_printer_error_publishes_event(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1")
        adapter.start_print.side_effect = PrinterError("timeout")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="test.gcode")

        scheduler.tick()

        events = event_bus.recent_events(EventType.JOB_FAILED)
        assert len(events) == 1
        assert events[0].data["job_id"] == job_id

    def test_unexpected_exception_during_dispatch(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1")
        adapter.start_print.side_effect = RuntimeError("something unexpected")
        registry.register("printer-1", adapter)

        job_id = queue.submit(file_name="test.gcode")
        result = scheduler.tick()

        assert len(result["failed"]) == 1
        assert result["failed"][0]["job_id"] == job_id
        assert queue.get_job(job_id).status == JobStatus.FAILED


# ---------------------------------------------------------------------------
# 14. Thread safety of active_jobs property
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Tests for thread-safe access to active_jobs."""

    def test_active_jobs_returns_copy(self, scheduler):
        """Modifying the returned dict should not affect internal state."""
        with scheduler._lock:
            scheduler._active_jobs["job-1"] = "printer-1"

        external = scheduler.active_jobs
        external["job-2"] = "printer-2"

        assert "job-2" not in scheduler.active_jobs
        assert len(scheduler.active_jobs) == 1

    def test_concurrent_active_jobs_access(self, queue, registry, event_bus, scheduler):
        """Multiple threads reading active_jobs concurrently."""
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        queue.submit(file_name="benchy.gcode")
        scheduler.tick()

        results = []
        errors = []

        def read_active_jobs(n: int) -> None:
            try:
                for _ in range(n):
                    active = scheduler.active_jobs
                    results.append(active)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=read_active_jobs, args=(50,))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 250
        for r in results:
            assert isinstance(r, dict)


# ---------------------------------------------------------------------------
# 15. Multiple dispatch in single tick
# ---------------------------------------------------------------------------

class TestMultipleDispatch:
    """Tests for dispatching multiple jobs in a single tick()."""

    def test_multiple_idle_printers_multiple_jobs(
        self, queue, registry, event_bus, scheduler
    ):
        adapter_a = make_mock_adapter(name="printer-a")
        adapter_b = make_mock_adapter(name="printer-b")
        adapter_c = make_mock_adapter(name="printer-c")
        registry.register("printer-a", adapter_a)
        registry.register("printer-b", adapter_b)
        registry.register("printer-c", adapter_c)

        job1 = queue.submit(file_name="file1.gcode")
        time.sleep(0.01)
        job2 = queue.submit(file_name="file2.gcode")
        time.sleep(0.01)
        job3 = queue.submit(file_name="file3.gcode")

        result = scheduler.tick()

        assert len(result["dispatched"]) == 3
        dispatched_jobs = {d["job_id"] for d in result["dispatched"]}
        assert dispatched_jobs == {job1, job2, job3}

        # All three printers should have been used
        dispatched_printers = {d["printer_name"] for d in result["dispatched"]}
        assert dispatched_printers == {"printer-a", "printer-b", "printer-c"}

    def test_more_jobs_than_printers(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)

        queue.submit(file_name="file1.gcode")
        time.sleep(0.01)
        job2 = queue.submit(file_name="file2.gcode")

        result = scheduler.tick()

        # Only one printer, so only one job dispatched
        assert len(result["dispatched"]) == 1
        assert queue.get_job(job2).status == JobStatus.QUEUED

    def test_more_printers_than_jobs(self, queue, registry, event_bus, scheduler):
        adapter_a = make_mock_adapter(name="printer-a")
        adapter_b = make_mock_adapter(name="printer-b")
        registry.register("printer-a", adapter_a)
        registry.register("printer-b", adapter_b)

        job_id = queue.submit(file_name="only.gcode")
        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["job_id"] == job_id


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Additional edge-case tests."""

    def test_tick_returns_correct_checked_count(
        self, queue, registry, event_bus, scheduler
    ):
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)

        queue.submit(file_name="file1.gcode")
        scheduler.tick()

        # Now there is one active job -- tick should check it
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        adapter.get_job.return_value = JobProgress(completion=50.0, file_name="file1.gcode")
        result = scheduler.tick()

        assert result["checked"] == 1

    def test_tick_with_no_printers_no_jobs(self, scheduler):
        result = scheduler.tick()
        assert result == {
            "dispatched": [],
            "completed": [],
            "failed": [],
            "checked": 0,
        }

    def test_active_jobs_empty_initially(self, scheduler):
        assert scheduler.active_jobs == {}

    def test_full_lifecycle_through_scheduler(
        self, queue, registry, event_bus, scheduler
    ):
        """Complete lifecycle: submit -> dispatch -> progress -> complete."""
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)

        # Submit
        job_id = queue.submit(file_name="benchy.gcode", submitted_by="agent")
        assert queue.get_job(job_id).status == JobStatus.QUEUED

        # Dispatch
        result1 = scheduler.tick()
        assert len(result1["dispatched"]) == 1
        assert queue.get_job(job_id).status == JobStatus.PRINTING

        # Progress
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        adapter.get_job.return_value = JobProgress(
            file_name="benchy.gcode", completion=75.0
        )
        result2 = scheduler.tick()
        assert result2["checked"] == 1

        progress_events = event_bus.recent_events(EventType.PRINT_PROGRESS)
        assert len(progress_events) == 1
        assert progress_events[0].data["completion"] == 75.0

        # Complete
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        result3 = scheduler.tick()
        assert job_id in result3["completed"]
        assert queue.get_job(job_id).status == JobStatus.COMPLETED
        assert scheduler.active_jobs == {}

    def test_scheduler_start_stop_start(self, scheduler):
        """Can restart the scheduler after stopping."""
        scheduler.start()
        assert scheduler.is_running is True
        scheduler.stop()
        assert scheduler.is_running is False
        scheduler.start()
        assert scheduler.is_running is True
        scheduler.stop()

    def test_exception_in_get_state_during_check_is_logged(
        self, queue, registry, event_bus, scheduler
    ):
        """Non-PrinterNotFoundError exceptions during check are logged, not fatal."""
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="test.gcode")

        scheduler.tick()

        # Now make get_state raise a generic exception
        adapter.get_state.side_effect = RuntimeError("network timeout")
        result = scheduler.tick()

        # Job should remain active -- it was not completed or failed
        assert job_id in scheduler.active_jobs
        assert result["checked"] == 1
        assert result["completed"] == []
        # The RuntimeError is not a PrinterNotFoundError, so it is just logged
        assert result["failed"] == []


class TestEmergencyLatchGating:
    """Scheduler should not dispatch/start while emergency latch is active."""

    def test_dispatch_blocked_when_latched(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1", state=PrinterStatus.IDLE)
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="blocked.gcode")

        fake_coord = MagicMock()
        fake_coord.get_latch_status.return_value = {
            "printer_id": "printer-1",
            "latched": True,
            "critical_interlocks_pending": ["door_closed"],
        }

        with patch("kiln.emergency.get_emergency_coordinator", return_value=fake_coord):
            result = scheduler.tick()

        assert result["dispatched"] == []
        assert queue.get_job(job_id).status == JobStatus.QUEUED
        adapter.start_print.assert_not_called()

        events = event_bus.recent_events(EventType.SAFETY_ESCALATED)
        assert len(events) == 1
        assert events[0].data["printer_name"] == "printer-1"
        assert events[0].data["job_id"] == job_id

    def test_active_job_marked_failed_when_latched(self, queue, registry, event_bus, scheduler):
        adapter = make_mock_adapter(name="printer-1", state=PrinterStatus.IDLE)
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="running.gcode")

        fake_coord = MagicMock()
        fake_coord.get_latch_status.return_value = {"printer_id": "printer-1", "latched": False}
        with patch("kiln.emergency.get_emergency_coordinator", return_value=fake_coord):
            first = scheduler.tick()
        assert len(first["dispatched"]) == 1
        assert queue.get_job(job_id).status == JobStatus.PRINTING

        fake_coord.get_latch_status.return_value = {
            "printer_id": "printer-1",
            "latched": True,
            "critical_interlocks_pending": [],
        }
        with patch("kiln.emergency.get_emergency_coordinator", return_value=fake_coord):
            second = scheduler.tick()

        assert len(second["failed"]) == 1
        assert second["failed"][0]["job_id"] == job_id
        assert queue.get_job(job_id).status == JobStatus.FAILED
        assert job_id not in scheduler.active_jobs



# ---------------------------------------------------------------------------
# Smart printer routing (persistence-based ranking)
# ---------------------------------------------------------------------------

class TestSmartPrinterRouting:
    """Tests for persistence-based printer ranking in dispatch."""

    def test_rank_printers_no_persistence(self, queue, registry, event_bus):
        """Without persistence, _rank_printers returns list unchanged."""
        scheduler = JobScheduler(queue, registry, event_bus, persistence=None)
        from kiln.queue import JobStatus, PrintJob
        job = PrintJob(
            id="j1", file_name="test.gcode", printer_name=None,
            status=JobStatus.QUEUED, submitted_by="test",
            metadata={"file_hash": "abc123"},
        )
        result = scheduler._rank_printers(["printer-a", "printer-b"], job)
        assert result == ["printer-a", "printer-b"]

    def test_rank_printers_no_metadata(self, queue, registry, event_bus):
        """Without file_hash/material_type in metadata, returns list unchanged."""
        mock_persistence = MagicMock()
        scheduler = JobScheduler(queue, registry, event_bus, persistence=mock_persistence)
        from kiln.queue import JobStatus, PrintJob
        job = PrintJob(
            id="j1", file_name="test.gcode", printer_name=None,
            status=JobStatus.QUEUED, submitted_by="test",
            metadata={},
        )
        result = scheduler._rank_printers(["printer-a", "printer-b"], job)
        assert result == ["printer-a", "printer-b"]
        mock_persistence.suggest_printer_for_outcome.assert_not_called()

    def test_rank_printers_reorders_by_success_rate(self, queue, registry, event_bus):
        """Printers are reordered so the best success rate comes first."""
        mock_persistence = MagicMock()
        mock_persistence.suggest_printer_for_outcome.return_value = [
            {"printer_name": "printer-b", "total_prints": 10, "successes": 9, "success_rate": 0.9},
            {"printer_name": "printer-a", "total_prints": 10, "successes": 5, "success_rate": 0.5},
        ]
        scheduler = JobScheduler(queue, registry, event_bus, persistence=mock_persistence)
        from kiln.queue import JobStatus, PrintJob
        job = PrintJob(
            id="j1", file_name="test.gcode", printer_name=None,
            status=JobStatus.QUEUED, submitted_by="test",
            metadata={"file_hash": "abc123"},
        )
        result = scheduler._rank_printers(["printer-a", "printer-b"], job)
        assert result == ["printer-b", "printer-a"]

    def test_rank_printers_unknown_printers_last(self, queue, registry, event_bus):
        """Printers without history sort after those with history."""
        mock_persistence = MagicMock()
        mock_persistence.suggest_printer_for_outcome.return_value = [
            {"printer_name": "printer-b", "total_prints": 5, "successes": 4, "success_rate": 0.8},
        ]
        scheduler = JobScheduler(queue, registry, event_bus, persistence=mock_persistence)
        from kiln.queue import JobStatus, PrintJob
        job = PrintJob(
            id="j1", file_name="test.gcode", printer_name=None,
            status=JobStatus.QUEUED, submitted_by="test",
            metadata={"material_type": "PLA"},
        )
        # printer-a has no history, printer-b has 80% success
        result = scheduler._rank_printers(["printer-a", "printer-b"], job)
        assert result == ["printer-b", "printer-a"]

    def test_rank_printers_empty_rankings(self, queue, registry, event_bus):
        """When persistence returns no rankings, list unchanged."""
        mock_persistence = MagicMock()
        mock_persistence.suggest_printer_for_outcome.return_value = []
        scheduler = JobScheduler(queue, registry, event_bus, persistence=mock_persistence)
        from kiln.queue import JobStatus, PrintJob
        job = PrintJob(
            id="j1", file_name="test.gcode", printer_name=None,
            status=JobStatus.QUEUED, submitted_by="test",
            metadata={"file_hash": "abc123"},
        )
        result = scheduler._rank_printers(["printer-a", "printer-b"], job)
        assert result == ["printer-a", "printer-b"]

    def test_dispatch_uses_smart_routing(self, queue, registry, event_bus):
        """Full integration: job dispatches to the historically best printer."""
        mock_persistence = MagicMock()
        mock_persistence.suggest_printer_for_outcome.return_value = [
            {"printer_name": "printer-b", "total_prints": 20, "successes": 19, "success_rate": 0.95},
            {"printer_name": "printer-a", "total_prints": 20, "successes": 10, "success_rate": 0.5},
        ]

        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        # Register two idle printers — printer-a first in iteration order
        adapter_a = make_mock_adapter(name="printer-a")
        adapter_b = make_mock_adapter(name="printer-b")
        registry.register("printer-a", adapter_a)
        registry.register("printer-b", adapter_b)

        # Submit an unassigned job with metadata
        queue.submit(
            file_name="benchy.gcode",
            submitted_by="test",
            metadata={"file_hash": "abc123", "material_type": "PLA"},
        )

        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        # printer-b has a 95% success rate vs printer-a's 50%, so it should
        # be ranked first and get the job
        assert result["dispatched"][0]["printer_name"] == "printer-b"
        adapter_b.start_print.assert_called_once_with("benchy.gcode")
        adapter_a.start_print.assert_not_called()

    def test_dispatch_falls_back_without_metadata(self, queue, registry, event_bus):
        """Without metadata, dispatch uses default order (no ranking)."""
        mock_persistence = MagicMock()
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter_a = make_mock_adapter(name="printer-a")
        adapter_b = make_mock_adapter(name="printer-b")
        registry.register("printer-a", adapter_a)
        registry.register("printer-b", adapter_b)

        queue.submit(file_name="benchy.gcode", submitted_by="test")

        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        # Without metadata, ranking is not applied
        mock_persistence.suggest_printer_for_outcome.assert_not_called()

    def test_dispatch_assigned_job_ignores_ranking(self, queue, registry, event_bus):
        """Jobs assigned to a specific printer bypass smart routing."""
        mock_persistence = MagicMock()
        mock_persistence.suggest_printer_for_outcome.return_value = [
            {"printer_name": "printer-b", "total_prints": 20, "successes": 19, "success_rate": 0.95},
        ]

        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter_a = make_mock_adapter(name="printer-a")
        adapter_b = make_mock_adapter(name="printer-b")
        registry.register("printer-a", adapter_a)
        registry.register("printer-b", adapter_b)

        # Job explicitly assigned to printer-a
        queue.submit(
            file_name="benchy.gcode",
            printer_name="printer-a",
            submitted_by="test",
            metadata={"file_hash": "abc123"},
        )

        result = scheduler.tick()

        assert len(result["dispatched"]) == 1
        # Even though printer-b has better stats, the job is assigned to printer-a
        assert result["dispatched"][0]["printer_name"] == "printer-a"


# ---------------------------------------------------------------------------
# Auto-outcome recording
# ---------------------------------------------------------------------------

class TestAutoOutcomeRecording:
    """Tests for automatic outcome recording in the learning database."""

    def test_auto_record_outcome_on_completion(self, queue, registry, event_bus):
        """Complete a job and verify save_print_outcome is called with outcome='success'."""
        mock_persistence = MagicMock()
        mock_persistence.get_print_outcome.return_value = None
        # The adapter layer opened this row at print start; the scheduler
        # only ever RESOLVES such a row — with none open it stays silent.
        mock_persistence.list_unresolved_outcomes.return_value = [
            {"job_id": "start:printer-1:1", "file_name": "benchy.gcode",
             "outcome": "pending"},
        ]
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode", metadata={"file_hash": "abc123"})

        # Dispatch
        scheduler.tick()

        # The scheduler must SEE the job printing before an idle printer
        # can honestly mean "it finished" — idle alone proves nothing.
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING,
        )
        scheduler.tick()

        # Printer returns to idle — job is done
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE,
        )
        scheduler.tick()

        mock_persistence.save_print_outcome.assert_called_once()
        call_args = mock_persistence.save_print_outcome.call_args[0][0]
        assert call_args["job_id"] == job_id
        assert call_args["printer_name"] == "printer-1"
        assert call_args["outcome"] == "success"
        assert call_args["file_name"] == "benchy.gcode"
        assert call_args["file_hash"] == "abc123"
        assert call_args["agent_id"] == "auto"
        assert call_args["determined_by"] == "observed"
        assert "Auto-recorded by scheduler" in call_args["notes"]

    @pytest.mark.parametrize(
        ("ended", "expect_outcome", "expect_contribute"),
        [
            (JobResult.CANCELLED, "cancelled", False),
            (JobResult.COMPLETED, "success", True),
            (None, "success", False),
        ],
    )
    def test_only_the_machines_own_verdict_federates(
        self, queue, registry, event_bus, ended, expect_outcome, expect_contribute
    ):
        """A cancel at the printer's touchscreen must not publish as success.

        The scheduler used to read "watched printing, now IDLE" as the
        machine's testimony that the print finished, and federated it. IDLE is
        not testimony: every adapter folds a clean finish, a cancel and an
        untouched printer into that one value, so a print stopped at the
        machine landed in the community pool as proof the settings worked.

        ``last_job_result`` carries what IDLE threw away. Named ending →
        believed. No ending named (OctoPrint flags, RRF object model) → still
        recorded as success for the user's own history, but NOT federated,
        because contributing is a claim about the model and only the machine
        gets to make it.
        """
        mock_persistence = MagicMock()
        mock_persistence.get_print_outcome.return_value = None
        mock_persistence.list_unresolved_outcomes.return_value = [
            {"job_id": "start:printer-1:1", "file_name": "benchy.gcode",
             "outcome": "pending"},
        ]
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        queue.submit(file_name="benchy.gcode", metadata={"file_hash": "abc123"})

        scheduler.tick()
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING,
        )
        scheduler.tick()

        # Ended — and the printer reads IDLE either way, which is the whole
        # point: the only thing separating these cases is last_job_result.
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE, last_job_result=ended,
        )
        with patch(
            "kiln.community_autofire.contribute_resolved_outcome"
        ) as contribute:
            scheduler.tick()

        recorded = mock_persistence.save_print_outcome.call_args[0][0]
        assert recorded["outcome"] == expect_outcome
        assert contribute.called is expect_contribute, (
            f"last_job_result={ended!r} should "
            f"{'federate' if expect_contribute else 'NOT federate'}"
        )

    def test_auto_record_outcome_on_permanent_failure(self, queue, registry, event_bus):
        """Exhaust retries and verify outcome='failed' is recorded."""
        mock_persistence = MagicMock()
        mock_persistence.get_print_outcome.return_value = None
        mock_persistence.list_unresolved_outcomes.return_value = [
            {"job_id": "start:printer-1:1", "file_name": "benchy.gcode",
             "outcome": "pending"},
        ]
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        # Dispatch
        scheduler.tick()

        # Printer enters error state — job fails permanently (max_retries=0)
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.ERROR,
        )
        scheduler.tick()

        mock_persistence.save_print_outcome.assert_called_once()
        call_args = mock_persistence.save_print_outcome.call_args[0][0]
        assert call_args["job_id"] == job_id
        assert call_args["printer_name"] == "printer-1"
        assert call_args["outcome"] == "failed"
        assert call_args["agent_id"] == "auto"
        assert call_args["determined_by"] == "observed"
        assert "error state" in call_args["notes"]

    def test_auto_record_skips_when_agent_already_recorded(self, queue, registry, event_bus):
        """If get_print_outcome returns existing data, save_print_outcome is NOT called."""
        mock_persistence = MagicMock()
        mock_persistence.get_print_outcome.return_value = {"job_id": "j1", "outcome": "success"}
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        queue.submit(file_name="benchy.gcode")

        # Dispatch
        scheduler.tick()

        # Complete
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE,
        )
        scheduler.tick()

        mock_persistence.save_print_outcome.assert_not_called()

    def test_auto_record_skips_without_persistence(self, queue, registry, event_bus):
        """No persistence configured — verify no errors."""
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=None,
        )

        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        queue.submit(file_name="benchy.gcode")

        # Dispatch
        scheduler.tick()

        # Complete — should not raise
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE,
        )
        result = scheduler.tick()

        assert len(result["completed"]) == 1

    def test_auto_record_failure_does_not_crash_scheduler(self, queue, registry, event_bus):
        """If save_print_outcome raises, the scheduler still functions normally."""
        mock_persistence = MagicMock()
        mock_persistence.get_print_outcome.return_value = None
        mock_persistence.save_print_outcome.side_effect = RuntimeError("DB write failed")
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")

        # Dispatch
        scheduler.tick()

        # Complete — save_print_outcome will raise, but scheduler should not crash
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE,
        )
        result = scheduler.tick()

        # Job still completed successfully despite auto-record failure
        assert job_id in result["completed"]
        assert queue.get_job(job_id).status == JobStatus.COMPLETED

    def test_auto_record_not_called_for_dispatch_failure(self, queue, registry, event_bus):
        """Dispatch failure (Phase 2) should NOT record an outcome."""
        mock_persistence = MagicMock()
        mock_persistence.get_print_outcome.return_value = None
        scheduler = JobScheduler(
            queue, registry, event_bus,
            poll_interval=0.1, max_retries=0,
            persistence=mock_persistence,
        )

        adapter = make_mock_adapter(
            name="printer-1",
            start_print_success=False,
            start_print_message="File not found",
        )
        registry.register("printer-1", adapter)
        queue.submit(file_name="missing.gcode")

        # tick will try to dispatch and fail — this is a Phase 2 failure
        result = scheduler.tick()

        assert len(result["failed"]) == 1
        # No outcome should be recorded for dispatch failures
        mock_persistence.save_print_outcome.assert_not_called()


# ---------------------------------------------------------------------------
# Outcome honesty — the scheduler records only what it actually saw
# ---------------------------------------------------------------------------

class TestOutcomeHonesty:
    """An idle printer proves a WATCHED job ended cleanly — nothing more.

    The scheduler once recorded 'success' for any active job whose printer
    went idle: a job that failed to start, or was cancelled at the
    touchscreen, was laundered into a success row and fed proven-settings.
    These tests pin the honest mapping and the determined_by stamp.
    """

    def _scheduler_with_db(self, queue, registry, event_bus, tmp_path):
        from kiln.persistence import KilnDB

        db = KilnDB(str(tmp_path / "sched.db"))
        return JobScheduler(
            queue, registry, event_bus, poll_interval=0.1,
            max_retries=0, persistence=db,
        ), db

    def _open_pending(self, db, file_name="benchy.gcode"):
        """What the adapter layer does at print start.  The scheduler is a
        RESOLVER: with no pending row open it stays silent, so each flow
        seeds the row a real start_print would have opened."""
        db.open_pending_outcome("start:printer-1:1", "printer-1", file_name)

    def test_watched_job_going_idle_is_observed_success(
        self, queue, registry, event_bus, tmp_path,
    ):
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        scheduler.tick()  # the scheduler SEES the job printing

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        scheduler.tick()

        row = db.get_print_outcome(job_id)
        assert row is not None
        assert row["outcome"] == "success"
        assert row["determined_by"] == "observed"

    def test_never_seen_printing_records_unknown_not_success(
        self, queue, registry, event_bus, tmp_path,
    ):
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch; adapter still reports IDLE
        self._open_pending(db)

        result = scheduler.tick()

        # Queue lifecycle is unchanged -- the job leaves the active set...
        assert job_id in result["completed"]
        # ...but the LEARNING record is an honest unknown, never a guess.
        row = db.get_print_outcome(job_id)
        assert row is not None
        assert row["outcome"] == "unknown"
        assert row["determined_by"] == "inferred"

    def test_error_state_records_failed(
        self, queue, registry, event_bus, tmp_path,
    ):
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.ERROR
        )
        scheduler.tick()

        row = db.get_print_outcome(job_id)
        assert row is not None
        assert row["outcome"] == "failed"
        assert row["determined_by"] == "observed"

    def test_decided_row_is_never_overwritten(
        self, queue, registry, event_bus, tmp_path,
    ):
        """An agent's decided verdict outranks the scheduler's inference."""
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()

        db.save_print_outcome({
            "job_id": job_id, "printer_name": "printer-1",
            "outcome": "failed", "agent_id": "mcp",
            "determined_by": "user_reported",
        })

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        scheduler.tick()

        row = db.get_print_outcome(job_id)
        assert row["outcome"] == "failed"
        assert row["determined_by"] == "user_reported"

    def test_queue_cancelled_job_records_cancelled(
        self, queue, registry, event_bus, tmp_path,
    ):
        """A job the queue itself cancelled ends as 'cancelled' — never
        laundered into success by the idle printer that follows, and the
        terminal queue state is left alone (completing it would raise)."""
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        scheduler.tick()  # watched printing

        queue.cancel(job_id)
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        scheduler.tick()

        row = db.get_print_outcome(job_id)
        assert row is not None
        assert row["outcome"] == "cancelled"
        assert queue.get_job(job_id).status == JobStatus.CANCELLED
        assert job_id not in scheduler.active_jobs


class TestSchedulerFederation:
    """A scheduler-resolved verdict reaches the community pool — the same
    parity the reconcile path gained on 2026-08-05.  The scheduler settles
    exactly the rows the adapter layer could not attribute, and until this
    wire those resolutions stopped at the local DB, so queue-managed prints
    were missing from the shared corpus.

    Only MACHINE testimony federates: watched-printing→idle (success) and a
    printer error observed mid-watch (failed).  The queue's own words —
    stuck-timeout guess, unknown, cancelled, error on a never-watched job —
    contribute nothing; a guessed sample poisons a geometry-keyed corpus.
    """

    def _scheduler_with_db(
        self, queue, registry, event_bus, tmp_path, max_retries=0,
    ):
        from kiln.persistence import KilnDB

        db = KilnDB(str(tmp_path / "sched.db"))
        return JobScheduler(
            queue, registry, event_bus, poll_interval=0.1,
            max_retries=max_retries, persistence=db,
        ), db

    def _open_pending(self, db, file_name="benchy.gcode"):
        """Seed the pending row a real start_print would have opened — the
        scheduler is a RESOLVER and stays silent with nothing owed."""
        db.open_pending_outcome("start:printer-1:1", "printer-1", file_name)

    def _capture_contributions(self, monkeypatch):
        import kiln.community_autofire as caf

        calls: list[dict] = []
        monkeypatch.setattr(
            caf, "contribute_resolved_outcome",
            lambda **kw: calls.append(kw) or {"contributed": True},
        )
        return calls

    def test_observed_success_contributes_to_community(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        calls = self._capture_contributions(monkeypatch)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(
            file_name="benchy.gcode", metadata={"material_type": "PETG"},
        )
        scheduler.tick()  # dispatch
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        scheduler.tick()  # the scheduler SEES the job printing
        # The machine NAMES its ending. Federating is a claim about the model,
        # so it takes the machine's word — a bare IDLE, which is also what a
        # touchscreen cancel produces, is not enough and no longer qualifies.
        adapter.get_state.return_value = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            last_job_result=JobResult.COMPLETED,
        )
        scheduler.tick()

        assert len(calls) == 1
        assert calls[0]["outcome"] == "success"
        assert calls[0]["job_id"] == job_id
        assert calls[0]["printer_name"] == "printer-1"
        assert calls[0]["printer_file_name"] == "benchy.gcode"
        assert calls[0]["material"] == "PETG"

    def test_machine_error_after_watched_printing_contributes_failed(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        calls = self._capture_contributions(monkeypatch)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        scheduler.tick()  # watched printing
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.ERROR
        )
        scheduler.tick()

        row = db.get_print_outcome(job_id)
        assert row is not None and row["outcome"] == "failed"
        assert len(calls) == 1
        assert calls[0]["outcome"] == "failed"

    def test_error_on_never_watched_job_contributes_nothing(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        """An error on a job never seen printing may predate the print —
        the local row still records failed, but the corpus gets no sample."""
        calls = self._capture_contributions(monkeypatch)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch; adapter still reports IDLE
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.ERROR
        )
        scheduler.tick()

        row = db.get_print_outcome(job_id)
        assert row is not None and row["outcome"] == "failed"
        assert calls == []

    def test_unknown_resolution_contributes_nothing(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        calls = self._capture_contributions(monkeypatch)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch; never seen printing
        self._open_pending(db)

        scheduler.tick()  # idle → honest unknown

        row = db.get_print_outcome(job_id)
        assert row is not None and row["outcome"] == "unknown"
        assert calls == []

    def test_cancelled_job_contributes_nothing(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        calls = self._capture_contributions(monkeypatch)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        scheduler.tick()  # watched printing
        queue.cancel(job_id)
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        scheduler.tick()

        row = db.get_print_outcome(job_id)
        assert row is not None and row["outcome"] == "cancelled"
        assert calls == []

    def test_idle_after_a_stall_is_unknown_and_contributes_nothing(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        """A print that stalled and then went idle with nothing named was
        not watched to its end: it is recorded ``unknown`` (the user is
        asked), never ``success``, and never federated."""
        calls = self._capture_contributions(monkeypatch)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        clock = _Clock()
        scheduler._clock = clock
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        adapter.get_job.return_value = JobProgress(
            file_name="benchy.gcode", completion=5.0, current_layer=2,
        )
        scheduler.tick()  # watched printing
        _run_silent(scheduler, clock, hours=1)  # frozen past the stall threshold
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        clock.advance(5)
        scheduler.tick()  # idle, nothing named

        row = db.get_print_outcome(job_id)
        assert row is not None and row["outcome"] == "unknown"
        assert row["determined_by"] == "inferred"
        assert calls == []

    def test_decided_row_contributes_nothing(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        """A row an agent already decided is the deciding path's to
        federate — the scheduler neither rewrites nor re-ships it."""
        calls = self._capture_contributions(monkeypatch)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch

        db.save_print_outcome({
            "job_id": job_id, "printer_name": "printer-1",
            "outcome": "failed", "agent_id": "mcp",
            "determined_by": "user_reported",
        })
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        scheduler.tick()
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        scheduler.tick()

        assert calls == []

    def test_contribution_failure_never_disturbs_local_record(
        self, queue, registry, event_bus, tmp_path, monkeypatch,
    ):
        import kiln.community_autofire as caf

        def _boom(**kw):
            raise RuntimeError("federation endpoint offline")

        monkeypatch.setattr(caf, "contribute_resolved_outcome", _boom)
        scheduler, db = self._scheduler_with_db(queue, registry, event_bus, tmp_path)
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler.tick()  # dispatch
        self._open_pending(db)

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        scheduler.tick()
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE
        )
        result = scheduler.tick()  # must not raise

        assert job_id in result["completed"]
        row = db.get_print_outcome(job_id)
        assert row is not None
        assert row["outcome"] == "success"
        assert row["determined_by"] == "observed"


# ---------------------------------------------------------------------------
# The queue never ends a print the printer has not ended
# ---------------------------------------------------------------------------
# The scheduler used to fail any job over two hours of wall-clock while the
# printer was STILL REPORTING PRINTING, then hand it to the retry path — which
# reset it to QUEUED and dispatched the same file again the moment the machine
# went idle.  A 2h05m print on a Snapmaker U1 was printed three times that way
# (2026-09-13).  Now a watched job is never failed by the queue at all: a stall
# or a loss of contact is announced (event + job_status) and the job stays
# PRINTING with its printer reserved until the machine ends it or a person
# cancels it.
#
# Every test here builds the scheduler with retries ENABLED (max_retries=2, the
# production default).  The shared ``scheduler`` fixture uses 0, under which
# the old code already failed permanently and the re-queue could never be
# seen — a test written against that fixture passed on the broken code.


class TestWatchedPrintIsNeverGivenUp:

    @pytest.fixture(autouse=True)
    def _fresh_motion_store(self):
        from kiln.printers import progress_motion as pm

        pm.reset_progress_observations()
        yield
        pm.reset_progress_observations()

    @staticmethod
    def _watched_print(queue, registry, event_bus, *, hours_old: float = 3.0):
        """Dispatch one job and watch it reach PRINTING.

        ``hours_old`` back-dates ``started_at`` so the print already looks
        older than the retired two-hour cap: on the old code that alone
        re-queued it, which is the regression these tests pin.
        """
        adapter = make_mock_adapter(name="printer-1")
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler = JobScheduler(
            queue, registry, event_bus, poll_interval=0.1,
            max_retries=2, retry_backoff_base=0.0,
        )
        clock = _Clock()
        scheduler._clock = clock
        scheduler.tick()  # dispatch
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING
        )
        adapter.get_job.return_value = JobProgress(
            file_name="benchy.gcode", completion=5.0, current_layer=2,
        )
        scheduler.tick()  # watched printing
        queue.get_job(job_id).started_at = time.time() - hours_old * 3600
        return scheduler, adapter, job_id, clock

    @staticmethod
    def _events(event_bus, kind, job_id):
        return [e for e in event_bus.recent_events(kind) if e.data.get("job_id") == job_id]

    def _still_printing(self, queue, scheduler, event_bus, job_id):
        assert queue.get_job(job_id).status == JobStatus.PRINTING
        assert job_id in scheduler.active_jobs
        assert self._events(event_bus, EventType.JOB_SUBMITTED, job_id) == []
        assert self._events(event_bus, EventType.JOB_FAILED, job_id) == []

    @staticmethod
    def _advance_moving(scheduler, adapter, clock, hours: int):
        for hour in range(1, hours + 1):
            clock.advance(3600)
            adapter.get_job.return_value = JobProgress(
                file_name="benchy.gcode", completion=5.0 + hour * 6, current_layer=2 + hour * 20,
            )
            assert scheduler.tick()["failed"] == []

    def test_long_print_that_keeps_moving_is_left_alone(self, queue, registry, event_bus):
        """Fourteen hours of a print whose layers keep advancing: still
        PRINTING, still watched, never re-queued.  The old code re-queued it
        at hour two."""
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        self._advance_moving(scheduler, adapter, clock, hours=14)
        self._still_printing(queue, scheduler, event_bus, job_id)
        assert scheduler.watch_note(job_id)["state"] == "moving"
        assert scheduler.watch_alerts() == []

    def test_stalled_print_is_announced_once_and_never_ended(self, queue, registry, event_bus):
        """Layer and percent frozen while the printer says PRINTING for a
        day: one JOB_STALLED event, a note on the job, and the job is still
        open -- nothing failed, nothing re-sent."""
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        assert _run_silent(scheduler, clock, hours=24) == []

        self._still_printing(queue, scheduler, event_bus, job_id)
        stalled = self._events(event_bus, EventType.JOB_STALLED, job_id)
        assert len(stalled) == 1
        assert stalled[0].data["printer_name"] == "printer-1"
        assert "has not actually moved" in stalled[0].data["note"]
        note = scheduler.watch_note(job_id)
        assert note["state"] == "stalled"
        assert note["since_seconds"] >= 23 * 3600
        assert "resume_print(force=True)" in note["note"]
        assert [a["job_id"] for a in scheduler.watch_alerts()] == [job_id]
        # A later tick changes nothing: no dispatch, no ending.
        assert scheduler.tick()["dispatched"] == []
        assert queue.get_job(job_id).status == JobStatus.PRINTING

    def test_stall_that_resumes_clears_the_alert(self, queue, registry, event_bus):
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        _run_silent(scheduler, clock, hours=1)
        assert scheduler.watch_note(job_id)["state"] == "stalled"

        self._advance_moving(scheduler, adapter, clock, hours=13)
        self._still_printing(queue, scheduler, event_bus, job_id)
        assert scheduler.watch_note(job_id)["state"] == "moving"
        assert scheduler.watch_alerts() == []
        assert len(self._events(event_bus, EventType.JOB_STALLED, job_id)) == 1

    def test_stall_announced_again_only_after_it_cleared(self, queue, registry, event_bus):
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        _run_silent(scheduler, clock, hours=1)
        self._advance_moving(scheduler, adapter, clock, hours=1)
        _run_silent(scheduler, clock, hours=1)
        assert len(self._events(event_bus, EventType.JOB_STALLED, job_id)) == 2

    def test_unreachable_printer_is_announced_and_the_job_stays_open(
        self, queue, registry, event_bus,
    ):
        """Reads that raise for a day: one JOB_NO_CONTACT, a note that says
        the print may still be running, and the job is still open.  The
        old code logged the error every poll and never said a word."""
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        adapter.get_state.side_effect = PrinterError("connection refused")
        assert _run_silent(scheduler, clock, hours=24, step_minutes=60) == []

        self._still_printing(queue, scheduler, event_bus, job_id)
        silent = self._events(event_bus, EventType.JOB_NO_CONTACT, job_id)
        assert len(silent) == 1
        assert silent[0].data["cause"] == "read_failed"
        note = scheduler.watch_note(job_id)
        assert note["state"] == "no_contact"
        assert "may still be running" in note["note"]
        assert "cancel this job" in note["note"]

    def test_offline_and_stale_readings_are_no_contact(self, queue, registry, event_bus):
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        adapter.get_state.return_value = PrinterState(connected=False, state=PrinterStatus.OFFLINE)
        _run_silent(scheduler, clock, hours=1, step_minutes=15)
        assert scheduler.watch_note(job_id)["state"] == "no_contact"
        assert self._events(event_bus, EventType.JOB_NO_CONTACT, job_id)[0].data["cause"] == "unreachable"

        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.STALE,
            last_known_state=PrinterStatus.PRINTING, state_age_seconds=3600.0,
        )
        _run_silent(scheduler, clock, hours=1, step_minutes=15)
        assert scheduler.watch_note(job_id)["state"] == "no_contact"
        # Still one episode: the cause changed, the silence did not end.
        assert len(self._events(event_bus, EventType.JOB_NO_CONTACT, job_id)) == 1
        self._still_printing(queue, scheduler, event_bus, job_id)

    def test_printer_back_after_a_gap_that_names_completed_is_a_success(
        self, queue, registry, event_bus, tmp_path,
    ):
        """Wi-Fi drops for ten hours on a twelve-hour print.  When the
        printer answers again saying it COMPLETED, that is the machine's
        verdict and it is recorded as such.  The old code would have
        re-queued the job at hour two and re-printed the part."""
        from kiln.persistence import KilnDB

        db = KilnDB(str(tmp_path / "sched.db"))
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        scheduler._persistence = db
        db.save_print_outcome({
            "job_id": job_id, "printer_name": "printer-1", "file_name": "benchy.gcode",
            "outcome": "pending", "agent_id": "auto", "determined_by": "observed",
        })
        adapter.get_state.side_effect = PrinterError("connection refused")
        _run_silent(scheduler, clock, hours=10, step_minutes=60)

        adapter.get_state.side_effect = None
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE, last_job_result=JobResult.COMPLETED,
        )
        clock.advance(60)
        result = scheduler.tick()
        assert result["completed"] == [job_id]
        assert queue.get_job(job_id).status == JobStatus.COMPLETED
        row = db.get_print_outcome(job_id)
        assert row["outcome"] == "success" and row["determined_by"] == "observed"

    def test_printer_back_after_a_gap_naming_nothing_is_unknown(
        self, queue, registry, event_bus, tmp_path,
    ):
        """Same gap, but the printer names no ending (OctoPrint, RRF): the
        ending was not watched, so it is UNKNOWN and the user is asked --
        not banked as a success on the strength of an idle reading."""
        from kiln.persistence import KilnDB

        db = KilnDB(str(tmp_path / "sched.db"))
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        scheduler._persistence = db
        db.save_print_outcome({
            "job_id": job_id, "printer_name": "printer-1", "file_name": "benchy.gcode",
            "outcome": "pending", "agent_id": "auto", "determined_by": "observed",
        })
        adapter.get_state.side_effect = PrinterError("connection refused")
        _run_silent(scheduler, clock, hours=10, step_minutes=60)

        adapter.get_state.side_effect = None
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE)
        clock.advance(60)
        scheduler.tick()
        assert queue.get_job(job_id).status == JobStatus.COMPLETED
        row = db.get_print_outcome(job_id)
        assert row["outcome"] == "unknown" and row["determined_by"] == "inferred"
        assert "no trustworthy reading" in row["notes"]

    def test_paused_printer_is_neither_stalled_nor_silent(self, queue, registry, event_bus):
        """PAUSED is a state that is supposed to be frozen.  A printer that
        keeps answering PAUSED for a day is the user's business."""
        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PAUSED)
        assert _run_silent(scheduler, clock, hours=24, step_minutes=60) == []
        self._still_printing(queue, scheduler, event_bus, job_id)
        assert scheduler.watch_alerts() == []
        assert self._events(event_bus, EventType.JOB_STALLED, job_id) == []
        assert self._events(event_bus, EventType.JOB_NO_CONTACT, job_id) == []

    def test_watch_note_is_none_for_a_job_not_being_watched(self, queue, registry, event_bus):
        scheduler = JobScheduler(queue, registry, event_bus, poll_interval=0.1, max_retries=2)
        assert scheduler.watch_note("nope") is None
        assert scheduler.watch_alerts() == []

    def test_job_status_tool_carries_the_watch(self, queue, registry, event_bus, monkeypatch):
        import kiln.server as _srv
        from kiln.plugins.queue_tools import job_status, queue_summary

        scheduler, adapter, job_id, clock = self._watched_print(queue, registry, event_bus)
        _run_silent(scheduler, clock, hours=1)
        monkeypatch.setattr(_srv, "_scheduler", scheduler)
        monkeypatch.setattr(_srv, "_get_queue", lambda: queue)
        monkeypatch.setattr(_srv, "_get_registry", lambda: registry)

        status = job_status(job_id)
        assert status["job"]["status"] == "printing"
        assert status["watch"]["state"] == "stalled"
        summary = queue_summary()
        assert [a["job_id"] for a in summary["watch_alerts"]] == [job_id]

        monkeypatch.setattr(_srv, "_scheduler", None)
        assert "watch" not in job_status(job_id)

    def test_dispatch_failure_still_retries(self, queue, registry, event_bus):
        """Only watched prints stopped being ended by the queue.  A
        start_print the printer refused never started a print, so retrying
        it is safe."""
        adapter = make_mock_adapter(name="printer-1", start_print_success=False)
        registry.register("printer-1", adapter)
        job_id = queue.submit(file_name="benchy.gcode")
        scheduler = JobScheduler(
            queue, registry, event_bus, poll_interval=0.1, max_retries=2, retry_backoff_base=0.0,
        )
        scheduler.tick()
        assert queue.get_job(job_id).status == JobStatus.QUEUED
        assert self._events(event_bus, EventType.JOB_SUBMITTED, job_id)
