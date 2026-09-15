"""The scheduler reads printers only when it has work to hand out.

Every ``kiln serve`` starts the job scheduler, and its dispatch phase read
every registered printer's state every 5 seconds even with an empty queue.
A Bambu counts each read as use, so its idle release never fired: measured
2026-09-15, four idle servers held all four of an A1's LAN connection slots
for 10-14 hours and a fifth Kiln was turned away.  An empty queue has
nothing a free printer could take, so it reads none -- while a print the
scheduler already started is still followed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from kiln.events import EventBus
from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
    PrintResult,
)
from kiln.queue import PrintQueue
from kiln.registry import PrinterRegistry
from kiln.scheduler import JobScheduler


@pytest.fixture(autouse=True)
def _no_emergency_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(JobScheduler, "_emergency_block_reason", lambda self, name: None)


def _adapter(state: PrinterStatus = PrinterStatus.IDLE) -> MagicMock:
    adapter = MagicMock()
    type(adapter).name = PropertyMock(return_value="printer-1")
    type(adapter).capabilities = PropertyMock(return_value=PrinterCapabilities())
    adapter.get_state.return_value = PrinterState(connected=True, state=state)
    adapter.get_job.return_value = JobProgress()
    adapter.start_print.return_value = PrintResult(success=True, message="OK")
    return adapter


def _setup() -> tuple[PrintQueue, MagicMock, JobScheduler]:
    queue, registry = PrintQueue(), PrinterRegistry()
    adapter = _adapter()
    registry.register("printer-1", adapter)
    return queue, adapter, JobScheduler(queue, registry, EventBus(), poll_interval=0.1, max_retries=0)


def test_an_empty_queue_reads_no_printer() -> None:
    queue, adapter, scheduler = _setup()
    adapter.get_state.reset_mock()
    scheduler.tick()
    adapter.get_state.assert_not_called()


def test_a_queued_job_still_finds_an_idle_printer() -> None:
    queue, adapter, scheduler = _setup()
    job_id = queue.submit(file_name="part.gcode", submitted_by="test")
    result = scheduler.tick()
    assert [d["job_id"] for d in result["dispatched"]] == [job_id]
    adapter.start_print.assert_called_once_with("part.gcode")


def test_a_print_it_started_is_still_followed_with_an_empty_queue() -> None:
    queue, adapter, scheduler = _setup()
    queue.submit(file_name="part.gcode", submitted_by="test")
    scheduler.tick()
    assert queue.pending_count() == 0
    adapter.get_state.reset_mock()
    adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PRINTING)
    scheduler.tick()
    assert adapter.get_state.called, "the job it dispatched is still followed"
