"""A print watchdog a test leaves running is stopped when that test ends.

``conftest._no_print_watchdog_outlives_its_test`` does the stopping.  Before
it, a watchdog armed by one test kept polling that test's fake printer and
filing its readings into the next tests' outcome ledger; see the fixture.

The check sits in a module-scoped fixture because only its teardown runs
after the test's own fixtures have all been torn down.
"""

from __future__ import annotations

import threading

import pytest

from kiln.print_watchdog import PrintWatchdog
from kiln.printers.base import PrinterState, PrinterStatus


class _IdlePrinter:
    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=PrinterStatus.IDLE)

    def get_job(self) -> None:
        return None


@pytest.fixture(scope="module")
def left_running():
    threads: list[threading.Thread] = []
    yield threads
    for thread in threads:
        assert not thread.is_alive(), "a watchdog outlived the test that started it"


def test_a_test_that_walks_away_from_its_watchdog(left_running):
    watchdog = PrintWatchdog(adapter=_IdlePrinter(), poll_interval_sec=60.0)
    watchdog.start()
    thread = watchdog._thread
    assert thread is not None and thread.is_alive()
    left_running.append(thread)
