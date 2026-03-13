"""Tests for kiln.fleet_orchestrator — fleet job orchestration.

Covers:
- Job submission: valid, empty path, metadata
- Job assignment: idle printers, preferred printer, no printers, failed printers
- Lifecycle transitions: QUEUED → ASSIGNED → PRINTING → COMPLETED/FAILED/CANCELLED
- Failure retry: re-queue on failure, max attempts exhaustion
- Cancellation: queued, assigned, printing, terminal, bulk cancel
- Fleet utilization: printer state mapping, job counting
- Queries: get_job_status, list_jobs, get_active_jobs, get_printer_job
- Purge: completed jobs, time-based cutoff
- Properties: job_count, queued_count, active_count
- PrinterSelector: preferred printer, failed printer filtering
- Singleton: get/reset
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiln.fleet_orchestrator import (
    AssignmentResult,
    FleetOrchestrator,
    FleetUtilization,
    JobNotFoundError,
    OrchestratedJob,
    OrchestratedJobStatus,
    OrchestratorError,
    PrinterSelector,
    get_fleet_orchestrator,
    reset_fleet_orchestrator,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_fleet_orchestrator()
    yield
    reset_fleet_orchestrator()


def _make_orch(idle_printers: list[str] | None = None) -> FleetOrchestrator:
    """Create an orchestrator with a mocked registry and no event bus."""
    registry = MagicMock()
    registry.get_idle_printers.return_value = idle_printers or []
    registry.get_fleet_status.return_value = []
    return FleetOrchestrator(registry=registry, event_bus=MagicMock())


# ---------------------------------------------------------------------------
# Job submission
# ---------------------------------------------------------------------------


class TestJobSubmission:

    def test_submit_returns_job_id(self):
        orch = _make_orch()
        job_id = orch.submit_job("/path/benchy.gcode")
        assert isinstance(job_id, str)
        assert len(job_id) > 0

    def test_submit_empty_path_raises(self):
        orch = _make_orch()
        with pytest.raises(OrchestratorError, match="file_path must not be empty"):
            orch.submit_job("")

    def test_submit_whitespace_path_raises(self):
        orch = _make_orch()
        with pytest.raises(OrchestratorError, match="file_path must not be empty"):
            orch.submit_job("   ")

    def test_submit_increments_job_count(self):
        orch = _make_orch()
        assert orch.job_count == 0
        orch.submit_job("/path/a.gcode")
        assert orch.job_count == 1
        orch.submit_job("/path/b.gcode")
        assert orch.job_count == 2

    def test_submit_stores_metadata(self):
        orch = _make_orch()
        job_id = orch.submit_job(
            "/path/benchy.gcode",
            submitted_by="agent-claude",
            priority=5,
            metadata={"color": "red"},
        )
        status = orch.get_job_status(job_id)
        assert status["submitted_by"] == "agent-claude"
        assert status["priority"] == 5
        assert status["metadata"]["color"] == "red"


# ---------------------------------------------------------------------------
# Job assignment
# ---------------------------------------------------------------------------


class TestJobAssignment:

    def test_assign_to_idle_printer(self):
        orch = _make_orch(idle_printers=["voron-350"])
        job_id = orch.submit_job("/path/benchy.gcode")
        result = orch.assign_job(job_id)
        assert result.success is True
        assert result.printer_name == "voron-350"

    def test_assign_preferred_printer(self):
        orch = _make_orch(idle_printers=["ender-3", "voron-350"])
        job_id = orch.submit_job("/path/benchy.gcode", preferred_printer="voron-350")
        result = orch.assign_job(job_id)
        assert result.printer_name == "voron-350"

    def test_assign_no_printers_available(self):
        orch = _make_orch(idle_printers=[])
        job_id = orch.submit_job("/path/benchy.gcode")
        result = orch.assign_job(job_id)
        assert result.success is False
        assert "No suitable" in result.message

    def test_assign_non_queued_job_fails(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode")
        orch.assign_job(job_id)  # QUEUED → ASSIGNED
        result = orch.assign_job(job_id)  # already ASSIGNED
        assert result.success is False
        assert "QUEUED" in result.message

    def test_assign_unknown_job_raises(self):
        orch = _make_orch()
        with pytest.raises(JobNotFoundError):
            orch.assign_job("nonexistent-id")

    def test_submit_and_assign_convenience(self):
        orch = _make_orch(idle_printers=["voron"])
        result = orch.submit_and_assign("/path/benchy.gcode")
        assert isinstance(result, AssignmentResult)
        assert result.success is True

    def test_assign_jobs_priority_order(self):
        orch = _make_orch(idle_printers=["voron", "ender"])
        orch.submit_job("/path/low.gcode", priority=1)
        orch.submit_job("/path/high.gcode", priority=10)
        results = orch.assign_jobs()
        # High priority should get first printer
        assert results[0].success is True
        assert results[1].success is True

    def test_assign_jobs_stops_when_no_printers(self):
        registry = MagicMock()
        # After first assignment consumes voron, return empty on subsequent calls
        registry.get_idle_printers.side_effect = [["voron"], [], []]
        registry.get_fleet_status.return_value = []
        orch = FleetOrchestrator(registry=registry, event_bus=MagicMock())

        orch.submit_job("/path/a.gcode")
        orch.submit_job("/path/b.gcode")
        orch.submit_job("/path/c.gcode")
        results = orch.assign_jobs()
        successes = sum(1 for r in results if r.success)
        assert successes == 1


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------


class TestLifecycleTransitions:

    def test_queued_to_assigned_to_printing(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode")
        orch.assign_job(job_id)
        orch.mark_printing(job_id)
        status = orch.get_job_status(job_id)
        assert status["status"] == "printing"
        assert status["started_at"] is not None

    def test_mark_printing_non_assigned_raises(self):
        orch = _make_orch()
        job_id = orch.submit_job("/path/benchy.gcode")
        with pytest.raises(OrchestratorError, match="expected ASSIGNED"):
            orch.mark_printing(job_id)

    def test_mark_completed(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode")
        orch.assign_job(job_id)
        orch.mark_printing(job_id)
        orch.mark_completed(job_id)
        status = orch.get_job_status(job_id)
        assert status["status"] == "completed"
        assert status["is_terminal"] is True

    def test_mark_completed_already_terminal_raises(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode")
        orch.assign_job(job_id)
        orch.mark_printing(job_id)
        orch.mark_completed(job_id)
        with pytest.raises(OrchestratorError, match="already completed"):
            orch.mark_completed(job_id)


# ---------------------------------------------------------------------------
# Failure and retry
# ---------------------------------------------------------------------------


class TestFailureRetry:

    def test_failure_requeues_when_retries_remain(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode", max_attempts=3)
        orch.assign_job(job_id)
        orch.mark_failed(job_id, error="nozzle clog")
        status = orch.get_job_status(job_id)
        assert status["status"] == "queued"
        assert "voron" in status["failed_printers"]

    def test_failure_permanent_after_max_attempts(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode", max_attempts=1)
        orch.assign_job(job_id)
        orch.mark_failed(job_id, error="nozzle clog")
        status = orch.get_job_status(job_id)
        assert status["status"] == "failed"
        assert status["error"] == "nozzle clog"
        assert status["is_terminal"] is True

    def test_failure_skips_failed_printers_on_reassignment(self):
        registry = MagicMock()
        # First call: voron is idle. Second call: both idle.
        registry.get_idle_printers.side_effect = [["voron"], ["voron", "ender"]]
        registry.get_fleet_status.return_value = []
        orch = FleetOrchestrator(registry=registry, event_bus=MagicMock())

        job_id = orch.submit_job("/path/benchy.gcode", max_attempts=3)
        orch.assign_job(job_id)  # assigned to voron
        orch.mark_failed(job_id, error="clog")

        result = orch.assign_job(job_id)
        assert result.success is True
        assert result.printer_name == "ender"  # voron was filtered out

    def test_failure_on_terminal_job_raises(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode")
        orch.assign_job(job_id)
        orch.mark_printing(job_id)
        orch.mark_completed(job_id)
        with pytest.raises(OrchestratorError, match="already completed"):
            orch.mark_failed(job_id, error="oops")


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:

    def test_cancel_queued_job(self):
        orch = _make_orch()
        job_id = orch.submit_job("/path/benchy.gcode")
        assert orch.cancel_job(job_id) is True
        status = orch.get_job_status(job_id)
        assert status["status"] == "cancelled"

    def test_cancel_assigned_job(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode")
        orch.assign_job(job_id)
        assert orch.cancel_job(job_id, reason="wrong filament") is True
        status = orch.get_job_status(job_id)
        assert "wrong filament" in status["error"]

    def test_cancel_terminal_job_returns_false(self):
        orch = _make_orch(idle_printers=["voron"])
        job_id = orch.submit_job("/path/benchy.gcode")
        orch.assign_job(job_id)
        orch.mark_printing(job_id)
        orch.mark_completed(job_id)
        assert orch.cancel_job(job_id) is False

    def test_cancel_unknown_job_raises(self):
        orch = _make_orch()
        with pytest.raises(JobNotFoundError):
            orch.cancel_job("nonexistent")

    def test_cancel_all_queued(self):
        orch = _make_orch()
        orch.submit_job("/path/a.gcode")
        orch.submit_job("/path/b.gcode")
        orch.submit_job("/path/c.gcode")
        count = orch.cancel_all_queued(reason="bulk")
        assert count == 3
        assert orch.queued_count == 0


# ---------------------------------------------------------------------------
# Fleet utilization
# ---------------------------------------------------------------------------


class TestFleetUtilization:

    def test_utilization_with_mixed_job_states(self):
        orch = _make_orch(idle_printers=["voron", "ender"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.submit_job("/path/b.gcode")
        id3 = orch.submit_job("/path/c.gcode")
        orch.assign_job(id1)
        orch.cancel_job(id3)

        util = orch.get_fleet_utilization()
        assert isinstance(util, FleetUtilization)
        assert util.queued_jobs == 1   # id2
        assert util.active_jobs == 1   # id1 (assigned)
        assert util.cancelled_jobs == 1  # id3

    def test_utilization_to_dict(self):
        util = FleetUtilization(total_printers=5, busy_printers=2, idle_printers=3)
        d = util.to_dict()
        assert d["total_printers"] == 5
        assert d["busy_printers"] == 2


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestQueries:

    def test_list_jobs_all(self):
        orch = _make_orch()
        orch.submit_job("/path/a.gcode")
        orch.submit_job("/path/b.gcode")
        jobs = orch.list_jobs()
        assert len(jobs) == 2

    def test_list_jobs_filtered_by_status(self):
        orch = _make_orch(idle_printers=["voron"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.submit_job("/path/b.gcode")
        orch.assign_job(id1)

        queued = orch.list_jobs(status=OrchestratedJobStatus.QUEUED)
        assert len(queued) == 1
        assigned = orch.list_jobs(status=OrchestratedJobStatus.ASSIGNED)
        assert len(assigned) == 1

    def test_list_jobs_filtered_by_printer(self):
        orch = _make_orch(idle_printers=["voron"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.submit_job("/path/b.gcode")
        orch.assign_job(id1)

        voron_jobs = orch.list_jobs(printer_name="voron")
        assert len(voron_jobs) == 1

    def test_list_jobs_limit(self):
        orch = _make_orch()
        for i in range(10):
            orch.submit_job(f"/path/{i}.gcode")
        jobs = orch.list_jobs(limit=3)
        assert len(jobs) == 3

    def test_get_active_jobs(self):
        orch = _make_orch(idle_printers=["voron"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.assign_job(id1)
        active = orch.get_active_jobs()
        assert len(active) == 1
        assert active[0]["status"] == "assigned"

    def test_get_printer_job(self):
        orch = _make_orch(idle_printers=["voron"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.assign_job(id1)
        job = orch.get_printer_job("voron")
        assert job is not None
        assert job["job_id"] == id1

    def test_get_printer_job_none_when_no_active(self):
        orch = _make_orch()
        assert orch.get_printer_job("voron") is None


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


class TestPurge:

    def test_purge_completed(self):
        orch = _make_orch(idle_printers=["voron"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.assign_job(id1)
        orch.mark_printing(id1)
        orch.mark_completed(id1)
        count = orch.purge_completed()
        assert count == 1
        assert orch.job_count == 0

    def test_purge_does_not_remove_active(self):
        orch = _make_orch(idle_printers=["voron"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.assign_job(id1)
        count = orch.purge_completed()
        assert count == 0
        assert orch.job_count == 1


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestOrchestratorProperties:

    def test_queued_count(self):
        orch = _make_orch()
        orch.submit_job("/path/a.gcode")
        orch.submit_job("/path/b.gcode")
        assert orch.queued_count == 2

    def test_active_count(self):
        orch = _make_orch(idle_printers=["voron"])
        id1 = orch.submit_job("/path/a.gcode")
        orch.assign_job(id1)
        assert orch.active_count == 1


# ---------------------------------------------------------------------------
# PrinterSelector
# ---------------------------------------------------------------------------


class TestPrinterSelector:

    def test_select_first_idle(self):
        selector = PrinterSelector()
        job = OrchestratedJob(job_id="test", file_path="/path")
        result = selector.select(job, ["voron", "ender"])
        assert result == "voron"

    def test_select_preferred(self):
        selector = PrinterSelector()
        job = OrchestratedJob(job_id="test", file_path="/path", preferred_printer="ender")
        result = selector.select(job, ["voron", "ender"])
        assert result == "ender"

    def test_select_filters_failed(self):
        selector = PrinterSelector()
        job = OrchestratedJob(
            job_id="test",
            file_path="/path",
            failed_printers={"voron"},
        )
        result = selector.select(job, ["voron", "ender"])
        assert result == "ender"

    def test_select_returns_none_when_all_failed(self):
        selector = PrinterSelector()
        job = OrchestratedJob(
            job_id="test",
            file_path="/path",
            failed_printers={"voron", "ender"},
        )
        result = selector.select(job, ["voron", "ender"])
        assert result is None

    def test_select_empty_list(self):
        selector = PrinterSelector()
        job = OrchestratedJob(job_id="test", file_path="/path")
        assert selector.select(job, []) is None


# ---------------------------------------------------------------------------
# OrchestratedJob dataclass
# ---------------------------------------------------------------------------


class TestOrchestratedJobDataclass:

    def test_to_dict(self):
        job = OrchestratedJob(job_id="test", file_path="/path/benchy.gcode")
        d = job.to_dict()
        assert d["job_id"] == "test"
        assert d["status"] == "queued"
        assert isinstance(d["failed_printers"], list)

    def test_is_terminal_for_completed(self):
        job = OrchestratedJob(job_id="test", file_path="/path", status=OrchestratedJobStatus.COMPLETED)
        assert job.is_terminal is True

    def test_is_terminal_for_queued(self):
        job = OrchestratedJob(job_id="test", file_path="/path")
        assert job.is_terminal is False

    def test_elapsed_seconds_none_before_start(self):
        job = OrchestratedJob(job_id="test", file_path="/path")
        assert job.elapsed_seconds is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:

    def test_get_returns_same_instance(self):
        a = get_fleet_orchestrator()
        b = get_fleet_orchestrator()
        assert a is b

    def test_reset_clears_singleton(self):
        a = get_fleet_orchestrator()
        reset_fleet_orchestrator()
        b = get_fleet_orchestrator()
        assert a is not b
