"""Tests for kiln.server MCP resource endpoints.

Covers:
- kiln://status — system-wide snapshot
- kiln://printers — fleet listing
- kiln://printers/{name} — single printer detail
- kiln://queue — queue summary
- kiln://queue/{job_id} — single job detail
- kiln://events — recent event listing
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)
from kiln.queue import PrintQueue, JobStatus
from kiln.events import EventBus, EventType
from kiln.registry import PrinterRegistry
from kiln.server import (
    resource_status,
    resource_printers,
    resource_printer_detail,
    resource_queue,
    resource_job_detail,
    resource_events,
    _queue,
    _registry,
    _event_bus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_adapter(name="test-printer", state=PrinterStatus.IDLE):
    """Create a mock adapter with configurable state."""
    adapter = MagicMock()
    type(adapter).name = PropertyMock(return_value=name)
    type(adapter).capabilities = PropertyMock(return_value=PrinterCapabilities())
    adapter.get_state.return_value = PrinterState(
        connected=True,
        state=state,
        tool_temp_actual=24.0,
        tool_temp_target=0.0,
        bed_temp_actual=22.0,
        bed_temp_target=0.0,
    )
    adapter.get_job.return_value = JobProgress()
    return adapter


@pytest.fixture(autouse=True)
def _clean_singletons():
    """Reset the module-level singletons before each test."""
    # Save state
    old_printers = dict(_registry._printers)
    old_jobs = dict(_queue._jobs)
    old_history = list(_event_bus._history)

    # Start each test with a clean slate
    _registry._printers.clear()
    _queue._jobs.clear()
    _event_bus._history.clear()

    yield

    # Restore state
    _registry._printers.clear()
    _registry._printers.update(old_printers)
    _queue._jobs.clear()
    _queue._jobs.update(old_jobs)
    _event_bus._history.clear()
    _event_bus._history.extend(old_history)


# ---------------------------------------------------------------------------
# kiln://status
# ---------------------------------------------------------------------------


class TestResourceStatus:
    """Tests for the kiln://status resource."""

    def test_empty_system(self):
        result = json.loads(resource_status())
        assert result["printer_count"] == 0
        assert result["queue"]["total"] == 0
        assert result["recent_events"] == []

    def test_with_registered_printer(self):
        adapter = _make_mock_adapter("octoprint")
        _registry.register("my-printer", adapter)

        result = json.loads(resource_status())
        assert result["printer_count"] == 1
        assert result["printers"][0]["name"] == "my-printer"

    def test_with_queued_jobs(self):
        _queue.submit("test.gcode", printer_name="p1")
        _queue.submit("test2.gcode")

        result = json.loads(resource_status())
        assert result["queue"]["pending"] == 2
        assert result["queue"]["total"] == 2

    def test_with_events(self):
        _event_bus.publish(EventType.JOB_SUBMITTED, {"job_id": "abc"}, source="test")

        result = json.loads(resource_status())
        assert result["recent_events"][0]["type"] == "job.submitted"

    @patch("kiln.server._get_adapter")
    def test_auto_register_from_env(self, mock_get_adapter):
        adapter = _make_mock_adapter("octoprint")
        mock_get_adapter.return_value = adapter

        with patch("kiln.server._PRINTER_HOST", "http://test.local"):
            result = json.loads(resource_status())

        assert result["printer_count"] == 1
        assert result["printers"][0]["state"] == "idle"


# ---------------------------------------------------------------------------
# kiln://printers
# ---------------------------------------------------------------------------


class TestResourcePrinters:
    """Tests for the kiln://printers resource."""

    def test_empty(self):
        result = json.loads(resource_printers())
        assert result["count"] == 0
        assert result["printers"] == []

    def test_with_printers(self):
        _registry.register("p1", _make_mock_adapter("octoprint"))
        _registry.register("p2", _make_mock_adapter("moonraker"))

        result = json.loads(resource_printers())
        assert result["count"] == 2
        names = {p["name"] for p in result["printers"]}
        assert names == {"p1", "p2"}

    def test_idle_printers_listed(self):
        _registry.register("idle-one", _make_mock_adapter("octoprint", PrinterStatus.IDLE))

        result = json.loads(resource_printers())
        assert "idle-one" in result["idle_printers"]


# ---------------------------------------------------------------------------
# kiln://printers/{printer_name}
# ---------------------------------------------------------------------------


class TestResourcePrinterDetail:
    """Tests for the kiln://printers/{name} resource."""

    def test_found(self):
        _registry.register("voron", _make_mock_adapter("octoprint"))

        result = json.loads(resource_printer_detail("voron"))
        assert result["name"] == "voron"
        assert result["backend"] == "octoprint"
        assert result["state"]["connected"] is True
        assert "capabilities" in result

    def test_not_found(self):
        result = json.loads(resource_printer_detail("nonexistent"))
        assert "error" in result
        assert "not found" in result["error"]

    def test_adapter_error(self):
        adapter = _make_mock_adapter("octoprint")
        adapter.get_state.side_effect = RuntimeError("connection refused")
        _registry.register("broken", adapter)

        result = json.loads(resource_printer_detail("broken"))
        assert "error" in result
        assert "connection refused" in result["error"]


# ---------------------------------------------------------------------------
# kiln://queue
# ---------------------------------------------------------------------------


class TestResourceQueue:
    """Tests for the kiln://queue resource."""

    def test_empty(self):
        result = json.loads(resource_queue())
        assert result["total"] == 0
        assert result["pending"] == 0
        assert result["next_job"] is None
        assert result["recent_jobs"] == []

    def test_with_jobs(self):
        _queue.submit("a.gcode", submitted_by="agent")
        _queue.submit("b.gcode", submitted_by="agent", priority=5)

        result = json.loads(resource_queue())
        assert result["total"] == 2
        assert result["pending"] == 2
        # High priority job should be next
        assert result["next_job"]["file_name"] == "b.gcode"

    def test_counts_by_status(self):
        job_id = _queue.submit("c.gcode")
        _queue.mark_starting(job_id)

        result = json.loads(resource_queue())
        assert result["active"] == 1
        assert result["pending"] == 0


# ---------------------------------------------------------------------------
# kiln://queue/{job_id}
# ---------------------------------------------------------------------------


class TestResourceJobDetail:
    """Tests for the kiln://queue/{job_id} resource."""

    def test_found(self):
        job_id = _queue.submit("test.gcode", printer_name="p1")

        result = json.loads(resource_job_detail(job_id))
        assert result["job"]["id"] == job_id
        assert result["job"]["file_name"] == "test.gcode"
        assert result["job"]["status"] == "queued"

    def test_not_found(self):
        result = json.loads(resource_job_detail("fake-id-999"))
        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# kiln://events
# ---------------------------------------------------------------------------


class TestResourceEvents:
    """Tests for the kiln://events resource."""

    def test_empty(self):
        result = json.loads(resource_events())
        assert result["count"] == 0
        assert result["events"] == []

    def test_with_events(self):
        _event_bus.publish(EventType.JOB_SUBMITTED, {"job_id": "1"}, source="test")
        _event_bus.publish(EventType.PRINT_STARTED, {"job_id": "1"}, source="test")

        result = json.loads(resource_events())
        assert result["count"] == 2
        # Newest first
        assert result["events"][0]["type"] == "print.started"
        assert result["events"][1]["type"] == "job.submitted"

    def test_max_50_events(self):
        for i in range(60):
            _event_bus.publish(EventType.PRINT_PROGRESS, {"i": i}, source="test")

        result = json.loads(resource_events())
        assert result["count"] == 50
