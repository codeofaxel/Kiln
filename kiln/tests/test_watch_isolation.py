"""A watch one test leaves running must not refuse the next test's watch.

Kiln counts watched MACHINES process-wide -- live entries in
``kiln.server._watchers`` plus the print health monitor's sessions -- and
refuses a new watch past the tier's limit, which is one machine on the free
tier every test runs as.  So a test that starts a watch and never stops it
spends the only slot for the rest of the worker process, and every later
test that watches anything gets a refusal instead of a watch.

Measured 2026-09-15: the two watch tests in ``test_plugin_tools.py`` leaked
one watcher, and in that same process six tests in
``test_vision_monitoring.py`` failed -- the file passes on its own.  In CI
the victims moved between runs as the parallel workers' mix changed, which
is what made a leak look like flakiness.

The tests run in file order: the first leaves something behind, the second
proves it did not survive into a fresh test.
"""

from __future__ import annotations

from unittest import mock

from kiln import server
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)


class _LiveWatch:
    """A watcher as the capacity count sees one: an adapter and no exited thread."""

    def __init__(self, adapter: object) -> None:
        self.adapter = adapter
        self._thread = None
        self.stopped = False

    def stop(self) -> dict:
        self.stopped = True
        return {"success": True}


def _adapter() -> mock.MagicMock:
    adapter = mock.MagicMock(spec=PrinterAdapter)
    adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PRINTING)
    adapter.get_job.return_value = JobProgress(completion=10.0)
    adapter.capabilities = PrinterCapabilities(can_snapshot=False)
    return adapter


def test_a_test_leaves_a_watch_running() -> None:
    server._watchers["leaked-by-a-test"] = _LiveWatch(_adapter())
    assert "leaked-by-a-test" in server._watchers


def test_the_next_test_starts_with_no_watch_running() -> None:
    assert server._watchers == {}, "a leaked watch spends the next test's only watch slot"
    assert server._watch_capacity_error(_adapter(), "default") is None


def test_a_test_leaves_a_health_monitor_session() -> None:
    from kiln.print_health_monitor import get_print_health_monitor

    get_print_health_monitor()._sessions["leaked-session"] = mock.MagicMock(printer_name="garage")
    assert get_print_health_monitor()._sessions


def test_the_next_test_starts_with_no_health_monitor_session() -> None:
    from kiln.print_health_monitor import get_print_health_monitor

    assert not get_print_health_monitor()._sessions
