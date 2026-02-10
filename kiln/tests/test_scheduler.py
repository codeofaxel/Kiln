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
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
    PrintResult,
)
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterNotFoundError, PrinterRegistry
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
    return JobScheduler(queue, registry, event_bus, poll_interval=0.1)


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
        assert events[0].data["printer_name"] == "printer-1"
        assert "error" in events[0].data


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

        job_id1 = queue.submit(file_name="first.gcode")
        scheduler.tick()

        # Adapter still reports idle (race condition), but active_jobs tracks it
        # The printer won't appear in idle_printers because it is PRINTING now
        # after dispatch. But let's explicitly test the busy filter by keeping
        # the adapter reporting idle but having an active job.
        # To test the filter directly, we inject into active_jobs.
        with scheduler._lock:
            scheduler._active_jobs["fake-job"] = "printer-1"

        job_id2 = queue.submit(file_name="second.gcode")
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
        job_id = queue.submit(
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

        job_id = queue.submit(file_name="any.gcode", printer_name=None)
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
        job_id = queue.submit(file_name="test.gcode")

        result = scheduler.tick()

        assert len(result["failed"]) == 1
        assert "start_print returned failure" in result["failed"][0]["error"]


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
        job_id = queue.submit(file_name="benchy.gcode")
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

        job1 = queue.submit(file_name="file1.gcode")
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

        job1 = queue.submit(file_name="file1.gcode")
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
