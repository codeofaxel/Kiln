"""Tests for kiln.queue -- print job queue management.

Covers:
- PrintJob dataclass: to_dict(), elapsed_seconds, wait_seconds
- PrintQueue.submit: returns unique IDs, stores job with correct fields
- PrintQueue.cancel: cancel queued job, cancel printing job, error on
  terminal state, not found
- Status transitions: mark_starting, mark_printing, mark_completed,
  mark_failed
- Queries: get_job, list_jobs with filters, next_job with priority
  ordering, next_job with printer filtering
- Counts: pending_count, active_count, total_count, summary
- Thread safety: concurrent submit calls
- JobNotFoundError
"""

from __future__ import annotations

import threading
import time

import pytest

from kiln.queue import JobNotFoundError, JobStatus, PrintJob, PrintQueue


# ---------------------------------------------------------------------------
# PrintJob dataclass
# ---------------------------------------------------------------------------

class TestPrintJob:
    """Tests for the PrintJob dataclass."""

    def test_to_dict_converts_status_enum(self):
        job = PrintJob(
            id="abc123",
            file_name="benchy.gcode",
            printer_name="voron",
            status=JobStatus.QUEUED,
            submitted_by="agent",
        )
        d = job.to_dict()
        assert d["status"] == "queued"
        assert d["id"] == "abc123"
        assert d["file_name"] == "benchy.gcode"
        assert d["printer_name"] == "voron"
        assert d["submitted_by"] == "agent"

    def test_to_dict_includes_all_fields(self):
        job = PrintJob(
            id="x",
            file_name="f.gcode",
            printer_name=None,
            status=JobStatus.PRINTING,
            submitted_by="user",
            priority=5,
            metadata={"key": "value"},
        )
        d = job.to_dict()
        expected_keys = {
            "id", "file_name", "printer_name", "status", "submitted_by",
            "priority", "created_at", "started_at", "completed_at",
            "error", "metadata",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_metadata_preserved(self):
        job = PrintJob(
            id="m1",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.QUEUED,
            submitted_by="agent",
            metadata={"material": "PLA", "weight_g": 42},
        )
        d = job.to_dict()
        assert d["metadata"] == {"material": "PLA", "weight_g": 42}

    def test_elapsed_seconds_none_when_not_started(self):
        job = PrintJob(
            id="e1",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.QUEUED,
            submitted_by="agent",
        )
        assert job.elapsed_seconds is None

    def test_elapsed_seconds_running_job(self):
        now = time.time()
        job = PrintJob(
            id="e2",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.PRINTING,
            submitted_by="agent",
            started_at=now - 100.0,
        )
        elapsed = job.elapsed_seconds
        assert elapsed is not None
        assert elapsed >= 99.0

    def test_elapsed_seconds_completed_job(self):
        job = PrintJob(
            id="e3",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.COMPLETED,
            submitted_by="agent",
            started_at=1000.0,
            completed_at=1250.0,
        )
        assert job.elapsed_seconds == 250.0

    def test_wait_seconds_queued_job(self):
        now = time.time()
        job = PrintJob(
            id="w1",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.QUEUED,
            submitted_by="agent",
            created_at=now - 60.0,
        )
        wait = job.wait_seconds
        assert wait >= 59.0

    def test_wait_seconds_started_job(self):
        job = PrintJob(
            id="w2",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.PRINTING,
            submitted_by="agent",
            created_at=1000.0,
            started_at=1030.0,
        )
        assert job.wait_seconds == 30.0

    def test_default_priority_is_zero(self):
        job = PrintJob(
            id="p0",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.QUEUED,
            submitted_by="agent",
        )
        assert job.priority == 0

    def test_default_metadata_is_empty_dict(self):
        job = PrintJob(
            id="md",
            file_name="test.gcode",
            printer_name=None,
            status=JobStatus.QUEUED,
            submitted_by="agent",
        )
        assert job.metadata == {}


# ---------------------------------------------------------------------------
# JobNotFoundError
# ---------------------------------------------------------------------------

class TestJobNotFoundError:
    """Tests for the JobNotFoundError exception."""

    def test_is_key_error(self):
        exc = JobNotFoundError("abc")
        assert isinstance(exc, KeyError)

    def test_stores_job_id(self):
        exc = JobNotFoundError("job-42")
        assert exc.job_id == "job-42"

    def test_message_contains_job_id(self):
        exc = JobNotFoundError("xyz")
        assert "xyz" in str(exc)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(JobNotFoundError):
            raise JobNotFoundError("missing")


# ---------------------------------------------------------------------------
# JobStatus enum
# ---------------------------------------------------------------------------

class TestJobStatus:
    """Tests for the JobStatus enum."""

    def test_all_members_present(self):
        expected = {"QUEUED", "STARTING", "PRINTING", "COMPLETED", "FAILED", "CANCELLED"}
        actual = {member.name for member in JobStatus}
        assert actual == expected

    def test_values(self):
        assert JobStatus.QUEUED.value == "queued"
        assert JobStatus.STARTING.value == "starting"
        assert JobStatus.PRINTING.value == "printing"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.CANCELLED.value == "cancelled"


# ---------------------------------------------------------------------------
# PrintQueue.submit
# ---------------------------------------------------------------------------

class TestPrintQueueSubmit:
    """Tests for PrintQueue.submit."""

    def test_returns_string_id(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        assert isinstance(job_id, str)
        assert len(job_id) == 12

    def test_returns_unique_ids(self):
        queue = PrintQueue()
        ids = {queue.submit(file_name="test.gcode") for _ in range(50)}
        assert len(ids) == 50

    def test_job_stored_with_correct_fields(self):
        queue = PrintQueue()
        job_id = queue.submit(
            file_name="benchy.gcode",
            printer_name="voron",
            submitted_by="claude",
            priority=3,
            metadata={"material": "PLA"},
        )
        job = queue.get_job(job_id)
        assert job.file_name == "benchy.gcode"
        assert job.printer_name == "voron"
        assert job.submitted_by == "claude"
        assert job.priority == 3
        assert job.status == JobStatus.QUEUED
        assert job.metadata == {"material": "PLA"}

    def test_default_values(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        job = queue.get_job(job_id)
        assert job.printer_name is None
        assert job.submitted_by == "unknown"
        assert job.priority == 0
        assert job.metadata == {}

    def test_submit_increments_total_count(self):
        queue = PrintQueue()
        assert queue.total_count == 0
        queue.submit(file_name="a.gcode")
        assert queue.total_count == 1
        queue.submit(file_name="b.gcode")
        assert queue.total_count == 2


# ---------------------------------------------------------------------------
# PrintQueue.cancel
# ---------------------------------------------------------------------------

class TestPrintQueueCancel:
    """Tests for PrintQueue.cancel."""

    def test_cancel_queued_job(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        job = queue.cancel(job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.completed_at is not None

    def test_cancel_printing_job(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_printing(job_id)
        job = queue.cancel(job_id)
        assert job.status == JobStatus.CANCELLED

    def test_cancel_starting_job(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_starting(job_id)
        job = queue.cancel(job_id)
        assert job.status == JobStatus.CANCELLED

    def test_cancel_completed_raises_value_error(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_completed(job_id)
        with pytest.raises(ValueError, match="already completed"):
            queue.cancel(job_id)

    def test_cancel_failed_raises_value_error(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_failed(job_id, error="nozzle clog")
        with pytest.raises(ValueError, match="already failed"):
            queue.cancel(job_id)

    def test_cancel_already_cancelled_raises_value_error(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.cancel(job_id)
        with pytest.raises(ValueError, match="already cancelled"):
            queue.cancel(job_id)

    def test_cancel_not_found_raises(self):
        queue = PrintQueue()
        with pytest.raises(JobNotFoundError):
            queue.cancel("nonexistent")


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    """Tests for mark_starting, mark_printing, mark_completed, mark_failed."""

    def test_mark_starting(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        job = queue.mark_starting(job_id)
        assert job.status == JobStatus.STARTING
        assert job.started_at is not None

    def test_mark_printing(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        job = queue.mark_printing(job_id)
        assert job.status == JobStatus.PRINTING
        assert job.started_at is not None

    def test_mark_printing_preserves_started_at(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_starting(job_id)
        started_at = queue.get_job(job_id).started_at
        job = queue.mark_printing(job_id)
        assert job.started_at == started_at

    def test_mark_printing_sets_started_at_if_missing(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        job = queue.mark_printing(job_id)
        assert job.started_at is not None

    def test_mark_completed(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_printing(job_id)
        job = queue.mark_completed(job_id)
        assert job.status == JobStatus.COMPLETED
        assert job.completed_at is not None

    def test_mark_failed(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_printing(job_id)
        job = queue.mark_failed(job_id, error="thermal runaway")
        assert job.status == JobStatus.FAILED
        assert job.completed_at is not None
        assert job.error == "thermal runaway"

    def test_mark_starting_not_found(self):
        queue = PrintQueue()
        with pytest.raises(JobNotFoundError):
            queue.mark_starting("nope")

    def test_mark_printing_not_found(self):
        queue = PrintQueue()
        with pytest.raises(JobNotFoundError):
            queue.mark_printing("nope")

    def test_mark_completed_not_found(self):
        queue = PrintQueue()
        with pytest.raises(JobNotFoundError):
            queue.mark_completed("nope")

    def test_mark_failed_not_found(self):
        queue = PrintQueue()
        with pytest.raises(JobNotFoundError):
            queue.mark_failed("nope", error="oops")

    def test_full_lifecycle(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="benchy.gcode", printer_name="voron")
        assert queue.get_job(job_id).status == JobStatus.QUEUED

        queue.mark_starting(job_id)
        assert queue.get_job(job_id).status == JobStatus.STARTING

        queue.mark_printing(job_id)
        assert queue.get_job(job_id).status == JobStatus.PRINTING

        queue.mark_completed(job_id)
        assert queue.get_job(job_id).status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

class TestPrintQueueQueries:
    """Tests for get_job, list_jobs, next_job."""

    def test_get_job_returns_correct_job(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        id2 = queue.submit(file_name="b.gcode")
        assert queue.get_job(id1).file_name == "a.gcode"
        assert queue.get_job(id2).file_name == "b.gcode"

    def test_get_job_not_found(self):
        queue = PrintQueue()
        with pytest.raises(JobNotFoundError):
            queue.get_job("missing-id")

    def test_list_jobs_no_filter(self):
        queue = PrintQueue()
        queue.submit(file_name="a.gcode")
        queue.submit(file_name="b.gcode")
        jobs = queue.list_jobs()
        assert len(jobs) == 2

    def test_list_jobs_filter_by_status(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        queue.submit(file_name="b.gcode")
        queue.mark_printing(id1)

        queued_jobs = queue.list_jobs(status=JobStatus.QUEUED)
        assert len(queued_jobs) == 1
        assert queued_jobs[0].file_name == "b.gcode"

        printing_jobs = queue.list_jobs(status=JobStatus.PRINTING)
        assert len(printing_jobs) == 1
        assert printing_jobs[0].file_name == "a.gcode"

    def test_list_jobs_filter_by_printer_name(self):
        queue = PrintQueue()
        queue.submit(file_name="a.gcode", printer_name="voron")
        queue.submit(file_name="b.gcode", printer_name="ender")
        queue.submit(file_name="c.gcode", printer_name="voron")

        voron_jobs = queue.list_jobs(printer_name="voron")
        assert len(voron_jobs) == 2
        assert all(j.printer_name == "voron" for j in voron_jobs)

    def test_list_jobs_combined_filters(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode", printer_name="voron")
        queue.submit(file_name="b.gcode", printer_name="voron")
        queue.mark_printing(id1)

        jobs = queue.list_jobs(status=JobStatus.QUEUED, printer_name="voron")
        assert len(jobs) == 1
        assert jobs[0].file_name == "b.gcode"

    def test_list_jobs_limit(self):
        queue = PrintQueue()
        for i in range(10):
            queue.submit(file_name=f"file{i}.gcode")
        jobs = queue.list_jobs(limit=3)
        assert len(jobs) == 3

    def test_list_jobs_ordered_by_priority_then_fifo(self):
        queue = PrintQueue()
        queue.submit(file_name="low.gcode", priority=0)
        time.sleep(0.01)
        queue.submit(file_name="high.gcode", priority=10)
        time.sleep(0.01)
        queue.submit(file_name="low2.gcode", priority=0)

        jobs = queue.list_jobs()
        assert jobs[0].file_name == "high.gcode"
        assert jobs[1].file_name == "low.gcode"
        assert jobs[2].file_name == "low2.gcode"

    def test_next_job_returns_highest_priority(self):
        queue = PrintQueue()
        queue.submit(file_name="normal.gcode", priority=0)
        time.sleep(0.01)
        queue.submit(file_name="urgent.gcode", priority=5)
        next_job = queue.next_job()
        assert next_job is not None
        assert next_job.file_name == "urgent.gcode"

    def test_next_job_fifo_within_same_priority(self):
        queue = PrintQueue()
        queue.submit(file_name="first.gcode", priority=0)
        time.sleep(0.01)
        queue.submit(file_name="second.gcode", priority=0)
        next_job = queue.next_job()
        assert next_job is not None
        assert next_job.file_name == "first.gcode"

    def test_next_job_empty_queue(self):
        queue = PrintQueue()
        assert queue.next_job() is None

    def test_next_job_no_queued_jobs(self):
        queue = PrintQueue()
        job_id = queue.submit(file_name="test.gcode")
        queue.mark_printing(job_id)
        assert queue.next_job() is None

    def test_next_job_printer_filter_specific(self):
        queue = PrintQueue()
        queue.submit(file_name="voron_job.gcode", printer_name="voron")
        time.sleep(0.01)
        queue.submit(file_name="ender_job.gcode", printer_name="ender")

        next_job = queue.next_job(printer_name="ender")
        assert next_job is not None
        assert next_job.file_name == "ender_job.gcode"

    def test_next_job_printer_filter_includes_any_printer(self):
        queue = PrintQueue()
        queue.submit(file_name="specific.gcode", printer_name="voron")
        time.sleep(0.01)
        queue.submit(file_name="any_printer.gcode", printer_name=None, priority=10)

        next_job = queue.next_job(printer_name="ender")
        assert next_job is not None
        assert next_job.file_name == "any_printer.gcode"

    def test_next_job_printer_filter_excludes_other_printers(self):
        queue = PrintQueue()
        queue.submit(file_name="voron_only.gcode", printer_name="voron")

        next_job = queue.next_job(printer_name="ender")
        assert next_job is None


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

class TestPrintQueueCounts:
    """Tests for pending_count, active_count, total_count, summary."""

    def test_pending_count(self):
        queue = PrintQueue()
        queue.submit(file_name="a.gcode")
        queue.submit(file_name="b.gcode")
        assert queue.pending_count() == 2

    def test_pending_count_excludes_non_queued(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        queue.submit(file_name="b.gcode")
        queue.mark_printing(id1)
        assert queue.pending_count() == 1

    def test_active_count_starting(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        queue.mark_starting(id1)
        assert queue.active_count() == 1

    def test_active_count_printing(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        queue.mark_printing(id1)
        assert queue.active_count() == 1

    def test_active_count_mixed(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        id2 = queue.submit(file_name="b.gcode")
        queue.submit(file_name="c.gcode")
        queue.mark_starting(id1)
        queue.mark_printing(id2)
        assert queue.active_count() == 2

    def test_active_count_excludes_completed(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        queue.mark_completed(id1)
        assert queue.active_count() == 0

    def test_total_count(self):
        queue = PrintQueue()
        assert queue.total_count == 0
        id1 = queue.submit(file_name="a.gcode")
        queue.submit(file_name="b.gcode")
        queue.mark_completed(id1)
        assert queue.total_count == 2

    def test_summary(self):
        queue = PrintQueue()
        id1 = queue.submit(file_name="a.gcode")
        queue.submit(file_name="b.gcode")
        id3 = queue.submit(file_name="c.gcode")
        queue.mark_printing(id1)
        queue.mark_completed(id3)

        s = queue.summary()
        assert s["printing"] == 1
        assert s["queued"] == 1
        assert s["completed"] == 1

    def test_summary_empty_queue(self):
        queue = PrintQueue()
        assert queue.summary() == {}


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestPrintQueueThreadSafety:
    """Tests for thread-safe concurrent operations."""

    def test_concurrent_submit(self):
        queue = PrintQueue()
        ids: list[str] = []
        lock = threading.Lock()

        def submit_jobs(count: int) -> None:
            for i in range(count):
                job_id = queue.submit(file_name=f"file_{threading.current_thread().name}_{i}.gcode")
                with lock:
                    ids.append(job_id)

        threads = [threading.Thread(target=submit_jobs, args=(20,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ids) == 100
        assert len(set(ids)) == 100
        assert queue.total_count == 100

    def test_concurrent_submit_and_cancel(self):
        queue = PrintQueue()
        submitted: list[str] = []
        lock = threading.Lock()

        for _ in range(10):
            job_id = queue.submit(file_name="test.gcode")
            submitted.append(job_id)

        def cancel_jobs(job_ids: list[str]) -> None:
            for jid in job_ids:
                try:
                    queue.cancel(jid)
                except (JobNotFoundError, ValueError):
                    pass

        half = len(submitted) // 2
        t1 = threading.Thread(target=cancel_jobs, args=(submitted[:half],))
        t2 = threading.Thread(target=cancel_jobs, args=(submitted[half:],))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        for jid in submitted:
            job = queue.get_job(jid)
            assert job.status == JobStatus.CANCELLED
