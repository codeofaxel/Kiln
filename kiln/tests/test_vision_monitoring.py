"""Tests for vision monitoring foundation.

Covers event types, capability flags, phase detection logic, and
the building blocks for monitor_print_vision and watch_print tools.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from kiln.events import Event, EventType
from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)


class TestVisionEventTypes:
    """Verify new vision-related event types exist."""

    def test_vision_check_exists(self) -> None:
        assert EventType.VISION_CHECK.value == "vision.check"

    def test_vision_alert_exists(self) -> None:
        assert EventType.VISION_ALERT.value == "vision.alert"

    def test_vision_check_creates_event(self) -> None:
        event = Event(
            type=EventType.VISION_CHECK,
            data={"printer_name": "voron", "completion": 45.0},
            source="vision",
        )
        d = event.to_dict()
        assert d["type"] == "vision.check"
        assert d["data"]["printer_name"] == "voron"

    def test_vision_alert_creates_event(self) -> None:
        event = Event(
            type=EventType.VISION_ALERT,
            data={"printer_name": "voron", "action": "pause", "reason": "spaghetti detected"},
            source="vision",
        )
        d = event.to_dict()
        assert d["type"] == "vision.alert"


class TestSnapshotCapability:
    """Verify can_snapshot capability flag on adapters."""

    def test_default_is_false(self) -> None:
        caps = PrinterCapabilities()
        assert caps.can_snapshot is False

    def test_octoprint_has_snapshot(self) -> None:
        from kiln.printers.octoprint import OctoPrintAdapter
        adapter = OctoPrintAdapter(host="http://test", api_key="key")
        assert adapter.capabilities.can_snapshot is True

    def test_moonraker_has_snapshot(self) -> None:
        from kiln.printers.moonraker import MoonrakerAdapter
        adapter = MoonrakerAdapter(host="http://test")
        assert adapter.capabilities.can_snapshot is True

    def test_bambu_no_snapshot(self) -> None:
        try:
            from kiln.printers.bambu import BambuAdapter
        except ImportError:
            pytest.skip("paho-mqtt not installed")
        adapter = BambuAdapter(host="192.168.1.1", access_code="12345678", serial="SN123")
        assert adapter.capabilities.can_snapshot is False

    def test_capability_in_to_dict(self) -> None:
        caps = PrinterCapabilities(can_snapshot=True)
        d = caps.to_dict()
        assert d["can_snapshot"] is True


# -- Phase detection helpers (used by monitor_print_vision) ----------------

def _detect_phase(completion: float | None) -> str:
    """Replicate the phase detection logic that will be in server.py."""
    if completion is None or completion < 0:
        return "unknown"
    if completion < 10:
        return "first_layers"
    if completion > 90:
        return "final_layers"
    return "mid_print"


def _failure_hints(phase: str) -> list[str]:
    """Replicate the failure hints logic that will be in server.py."""
    hints = {
        "first_layers": [
            "Check bed adhesion — first layer should be firmly stuck",
            "Look for warping at corners or edges lifting from bed",
            "Verify extrusion is consistent (no gaps or blobs)",
        ],
        "mid_print": [
            "Check for spaghetti — filament not adhering to previous layers",
            "Look for layer shifting (misaligned layers)",
            "Check for stringing between features",
        ],
        "final_layers": [
            "Check for cooling artifacts on overhangs",
            "Look for stringing or blobs on fine details",
            "Verify top surface is smooth and complete",
        ],
        "unknown": [
            "Verify print is progressing normally",
            "Check for any visible defects",
        ],
    }
    return hints.get(phase, hints["unknown"])


class TestPhaseDetection:
    """Test print phase classification from completion percentage."""

    def test_none_completion(self) -> None:
        assert _detect_phase(None) == "unknown"

    def test_negative_completion(self) -> None:
        assert _detect_phase(-1.0) == "unknown"

    def test_zero_percent(self) -> None:
        assert _detect_phase(0.0) == "first_layers"

    def test_five_percent(self) -> None:
        assert _detect_phase(5.0) == "first_layers"

    def test_nine_percent(self) -> None:
        assert _detect_phase(9.9) == "first_layers"

    def test_ten_percent(self) -> None:
        assert _detect_phase(10.0) == "mid_print"

    def test_fifty_percent(self) -> None:
        assert _detect_phase(50.0) == "mid_print"

    def test_ninety_percent(self) -> None:
        assert _detect_phase(90.0) == "mid_print"

    def test_ninety_one_percent(self) -> None:
        assert _detect_phase(91.0) == "final_layers"

    def test_hundred_percent(self) -> None:
        assert _detect_phase(100.0) == "final_layers"


class TestFailureHints:
    def test_first_layers_mentions_adhesion(self) -> None:
        hints = _failure_hints("first_layers")
        assert any("adhesion" in h.lower() for h in hints)

    def test_mid_print_mentions_spaghetti(self) -> None:
        hints = _failure_hints("mid_print")
        assert any("spaghetti" in h.lower() for h in hints)

    def test_final_layers_mentions_cooling(self) -> None:
        hints = _failure_hints("final_layers")
        assert any("cooling" in h.lower() for h in hints)

    def test_unknown_has_hints(self) -> None:
        hints = _failure_hints("unknown")
        assert len(hints) >= 1

    def test_each_phase_has_multiple_hints(self) -> None:
        for phase in ("first_layers", "mid_print", "final_layers"):
            assert len(_failure_hints(phase)) >= 2


class TestVisionMonitoringData:
    """Test that vision monitoring data structures can be assembled correctly."""

    def test_snapshot_context_assembly(self) -> None:
        """Verify the monitoring context dict can be built from printer data."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            tool_temp_actual=210.0,
            tool_temp_target=210.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )
        job = JobProgress(
            file_name="benchy.gcode",
            completion=45.0,
            print_time_seconds=1800,
            print_time_left_seconds=2200,
        )
        phase = _detect_phase(job.completion)
        hints = _failure_hints(phase)

        context = {
            "printer_state": state.to_dict(),
            "job_progress": job.to_dict(),
            "monitoring_context": {
                "completion_percent": job.completion,
                "print_phase": phase,
                "failure_hints": hints,
            },
        }
        assert context["monitoring_context"]["print_phase"] == "mid_print"
        assert len(context["monitoring_context"]["failure_hints"]) >= 2
        assert context["printer_state"]["state"] == "printing"

    def test_idle_printer_context(self) -> None:
        state = PrinterState(connected=True, state=PrinterStatus.IDLE)
        job = JobProgress()
        phase = _detect_phase(job.completion)
        assert phase == "unknown"

    def test_error_printer_context(self) -> None:
        state = PrinterState(connected=True, state=PrinterStatus.ERROR)
        assert state.to_dict()["state"] == "error"
