"""The push-cache staleness clock, for Moonraker and OctoPrint.

``describe_stale_state`` turns ``PrinterState.state_age_seconds`` into the
only honest signal Kiln has that its numbers may no longer be true.  Two
adapters answer ``get_state`` from a push cache when
``KILN_PUSH_MONITORING`` is set and from a fresh HTTP request otherwise —
and on the cached path they used to report no age at all, so the warning
could never fire.  Absence on the HTTP path is CORRECT (the reading is
current by construction) and must stay absent; absence on the cached path
was a silent gap.

The clock is deliberately a SECOND clock, separate from "when was the
cache last written".  Klipper pushes deltas, so a cache-wide timestamp
would answer "when did any field arrive" — a temperature tick every
second reporting a fresh age beside a print state that stopped updating
minutes ago.  That is precisely the reassuring lie the age exists to
prevent, and it is what these tests pin.
"""

from __future__ import annotations

import json
import time

from kiln.printers.moonraker import MoonrakerWebSocketMonitor
from kiln.printers.octoprint import OctoPrintSockJSMonitor


def _moonraker_monitor() -> MoonrakerWebSocketMonitor:
    return MoonrakerWebSocketMonitor("printer.local")


def _octoprint_monitor() -> OctoPrintSockJSMonitor:
    return OctoPrintSockJSMonitor("printer.local", "key")


class TestMoonrakerStateClock:
    def test_no_push_yet_reports_unknown_not_fresh(self):
        """None means nobody has told us — never a zero, which would read
        as a guarantee of freshness we cannot make."""
        assert _moonraker_monitor().get_print_state_age() is None

    def test_a_frame_carrying_print_stats_starts_the_clock(self):
        mon = _moonraker_monitor()
        with mon._cache_lock:
            mon._stamp_print_state_locked({"print_stats": {"state": "printing"}})
        age = mon.get_print_state_age()
        assert age is not None and age < 1.0

    def test_a_temperature_only_frame_does_not_touch_the_clock(self):
        """The property the whole two-clock design exists for.

        Klipper sends deltas: during a print, temperature frames arrive
        every second while ``print_stats.state`` changed once, at the
        start.  If a temps frame reset the clock, a print that silently
        stopped advancing would report a one-second-old state forever.
        """
        mon = _moonraker_monitor()
        with mon._cache_lock:
            mon._stamp_print_state_locked({"print_stats": {"state": "printing"}})
            mon._print_state_time = time.time() - 300.0  # state is 5 min old

        with mon._cache_lock:
            mon._stamp_print_state_locked({"extruder": {"temperature": 210.0}})

        age = mon.get_print_state_age()
        assert age is not None and age >= 299.0, (
            "a temps-only delta reset the state clock — the exact "
            "fresh-looking-age-beside-a-stale-state failure"
        )

    def test_a_non_dict_print_stats_is_not_a_state_frame(self):
        mon = _moonraker_monitor()
        with mon._cache_lock:
            mon._stamp_print_state_locked({"print_stats": None})
        assert mon.get_print_state_age() is None


class TestOctoPrintStateClock:
    def test_no_push_yet_reports_unknown_not_fresh(self):
        assert _octoprint_monitor().get_print_state_age() is None

    def test_a_current_frame_carrying_state_starts_the_clock(self):
        mon = _octoprint_monitor()
        mon._on_message(None, json.dumps({"current": {"state": {"text": "Printing"}}}))
        age = mon.get_print_state_age()
        assert age is not None and age < 1.0

    def test_a_frame_without_state_does_not_touch_the_clock(self):
        """OctoPrint's ``current`` is documented as a full snapshot, so in
        practice every frame should carry ``state``.  That is not
        verifiable from the code, and the separate clock costs nothing and
        is correct either way — so it is pinned rather than assumed."""
        mon = _octoprint_monitor()
        mon._on_message(None, json.dumps({"current": {"state": {"text": "Printing"}}}))
        with mon._cache_lock:
            mon._print_state_time = time.time() - 300.0

        mon._on_message(None, json.dumps({"current": {"temps": [{"tool0": {"actual": 210}}]}}))

        age = mon.get_print_state_age()
        assert age is not None and age >= 299.0
