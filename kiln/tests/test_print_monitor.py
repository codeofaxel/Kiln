"""Tests for kiln.print_monitor — first-layer monitoring system.

Coverage areas:
- MonitorPolicy defaults, serialisation, from_dict round-trip
- MonitorResult defaults and serialisation
- analyze_snapshot_basic heuristic checks (JPEG validation, brightness, variance)
- FirstLayerMonitor — camera gating, state transitions, snapshot capture,
  event publishing, policy customisation, error handling
- load_monitor_policy — env var overrides, config file loading, defaults
- Autonomy integration — require_first_layer_check constraint
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest
import yaml

from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
)
from kiln.events import EventType
from kiln.print_monitor import (
    FirstLayerMonitor,
    MonitorPolicy,
    MonitorResult,
    analyze_snapshot_basic,
    load_monitor_policy,
    _MIN_SNAPSHOT_BYTES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal JPEG: starts with FF D8 FF, ends with FF D9
_JPEG_HEADER = b"\xff\xd8\xff\xe0"
_JPEG_FOOTER = b"\xff\xd9"


def _make_jpeg(body: bytes) -> bytes:
    """Build a minimal JPEG-like byte sequence for testing."""
    return _JPEG_HEADER + body + _JPEG_FOOTER


def _make_mock_adapter(
    *,
    can_snapshot: bool = True,
    state: PrinterStatus = PrinterStatus.PRINTING,
    completion: float = 5.0,
    snapshot_data: Optional[bytes] = None,
) -> mock.MagicMock:
    """Build a mock PrinterAdapter with sensible defaults."""
    adapter = mock.MagicMock()
    adapter.name = "test-printer"
    adapter.capabilities = PrinterCapabilities(can_snapshot=can_snapshot)
    adapter.get_state.return_value = PrinterState(connected=True, state=state)
    adapter.get_job.return_value = JobProgress(
        completion=completion, file_name="test.gcode"
    )
    if snapshot_data is not None:
        adapter.get_snapshot.return_value = snapshot_data
    elif can_snapshot:
        adapter.get_snapshot.return_value = _make_jpeg(b"\x80" * 200)
    return adapter


# ---------------------------------------------------------------------------
# TestMonitorPolicy
# ---------------------------------------------------------------------------


class TestMonitorPolicy:
    """MonitorPolicy dataclass defaults, serialisation, and from_dict."""

    def test_default_values(self) -> None:
        p = MonitorPolicy()
        assert p.first_layer_delay_seconds == 120
        assert p.first_layer_check_count == 3
        assert p.first_layer_interval_seconds == 60
        assert p.auto_pause_on_failure is True
        assert p.failure_confidence_threshold == 0.8
        assert p.require_camera is False
        assert p.max_snapshot_failures == 3

    def test_to_dict(self) -> None:
        p = MonitorPolicy()
        d = p.to_dict()
        assert d["first_layer_delay_seconds"] == 120
        assert d["first_layer_check_count"] == 3
        assert d["first_layer_interval_seconds"] == 60
        assert d["auto_pause_on_failure"] is True
        assert d["failure_confidence_threshold"] == 0.8
        assert d["require_camera"] is False
        assert d["max_snapshot_failures"] == 3

    def test_from_dict(self) -> None:
        original = MonitorPolicy(
            first_layer_delay_seconds=30,
            first_layer_check_count=5,
            first_layer_interval_seconds=10,
            auto_pause_on_failure=False,
            require_camera=True,
            max_snapshot_failures=5,
        )
        d = original.to_dict()
        restored = MonitorPolicy.from_dict(d)
        assert restored.first_layer_delay_seconds == 30
        assert restored.first_layer_check_count == 5
        assert restored.first_layer_interval_seconds == 10
        assert restored.auto_pause_on_failure is False
        assert restored.require_camera is True
        assert restored.max_snapshot_failures == 5

    def test_from_dict_partial(self) -> None:
        p = MonitorPolicy.from_dict({"first_layer_delay_seconds": 45})
        assert p.first_layer_delay_seconds == 45
        # Other fields use defaults
        assert p.first_layer_check_count == 3
        assert p.first_layer_interval_seconds == 60
        assert p.auto_pause_on_failure is True

    def test_from_dict_empty(self) -> None:
        p = MonitorPolicy.from_dict({})
        assert p.first_layer_delay_seconds == 120
        assert p.first_layer_check_count == 3

    def test_from_dict_ignores_unknown_keys(self) -> None:
        p = MonitorPolicy.from_dict(
            {"first_layer_delay_seconds": 10, "unknown_key": "ignored"}
        )
        assert p.first_layer_delay_seconds == 10
        assert not hasattr(p, "unknown_key")

    def test_custom_values(self) -> None:
        p = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=1,
            first_layer_interval_seconds=5,
            auto_pause_on_failure=False,
            failure_confidence_threshold=0.5,
            require_camera=True,
            max_snapshot_failures=10,
        )
        assert p.first_layer_delay_seconds == 0
        assert p.first_layer_check_count == 1
        assert p.first_layer_interval_seconds == 5
        assert p.auto_pause_on_failure is False
        assert p.failure_confidence_threshold == 0.5
        assert p.require_camera is True
        assert p.max_snapshot_failures == 10

    def test_round_trip(self) -> None:
        original = MonitorPolicy(
            first_layer_delay_seconds=99,
            first_layer_check_count=7,
        )
        restored = MonitorPolicy.from_dict(original.to_dict())
        assert restored == original


# ---------------------------------------------------------------------------
# TestMonitorResult
# ---------------------------------------------------------------------------


class TestMonitorResult:
    """MonitorResult defaults and serialisation."""

    def test_default_values(self) -> None:
        r = MonitorResult(success=True, outcome="passed")
        assert r.success is True
        assert r.outcome == "passed"
        assert r.snapshots == []
        assert r.snapshot_failures == 0
        assert r.duration_seconds == 0.0
        assert r.auto_paused is False
        assert r.failure_type is None
        assert r.message == ""

    def test_to_dict(self) -> None:
        r = MonitorResult(
            success=True,
            outcome="passed",
            snapshots=[{"image_base64": "abc"}],
            snapshot_failures=1,
            duration_seconds=42.5,
            message="All good",
        )
        d = r.to_dict()
        assert d["success"] is True
        assert d["outcome"] == "passed"
        assert len(d["snapshots"]) == 1
        assert d["snapshot_failures"] == 1
        assert d["duration_seconds"] == 42.5
        assert d["auto_paused"] is False
        assert d["failure_type"] is None
        assert d["message"] == "All good"

    def test_passed_result(self) -> None:
        r = MonitorResult(success=True, outcome="passed", message="OK")
        assert r.outcome == "passed"
        assert r.success is True

    def test_failed_result(self) -> None:
        r = MonitorResult(
            success=False,
            outcome="failed",
            failure_type="spaghetti",
            auto_paused=True,
            message="Spaghetti detected",
        )
        assert r.outcome == "failed"
        assert r.failure_type == "spaghetti"
        assert r.auto_paused is True

    def test_no_camera_result(self) -> None:
        r = MonitorResult(
            success=False,
            outcome="no_camera",
            message="No camera available",
        )
        assert r.outcome == "no_camera"
        assert r.success is False

    def test_error_result(self) -> None:
        r = MonitorResult(
            success=False,
            outcome="error",
            message="Printer offline",
        )
        assert r.outcome == "error"
        d = r.to_dict()
        assert d["outcome"] == "error"


# ---------------------------------------------------------------------------
# TestAnalyzeSnapshotBasic
# ---------------------------------------------------------------------------


class TestAnalyzeSnapshotBasic:
    """Heuristic snapshot analysis: JPEG validation, brightness, variance."""

    def test_valid_jpeg(self) -> None:
        data = _make_jpeg(b"\x80" * 200)
        result = analyze_snapshot_basic(data)
        assert result["valid"] is True
        assert result["size_bytes"] == len(data)
        assert isinstance(result["brightness"], float)
        assert isinstance(result["variance"], float)

    def test_too_small(self) -> None:
        data = _make_jpeg(b"\x80" * 10)
        assert len(data) < _MIN_SNAPSHOT_BYTES
        result = analyze_snapshot_basic(data)
        assert result["valid"] is False
        assert any("small" in w or "corrupt" in w for w in result["warnings"])

    def test_empty_bytes(self) -> None:
        result = analyze_snapshot_basic(b"")
        assert result["valid"] is False
        assert result["size_bytes"] == 0

    def test_not_jpeg(self) -> None:
        data = b"\x00\x01\x02\x03" * 50  # 200 bytes, no JPEG header
        result = analyze_snapshot_basic(data)
        assert result["valid"] is False
        assert any("format" in w or "unrecognised" in w for w in result["warnings"])

    def test_dark_image(self) -> None:
        # JPEG header + mostly zero bytes = very low brightness
        data = _make_jpeg(b"\x00" * 500)
        result = analyze_snapshot_basic(data)
        assert result["valid"] is True
        assert result["brightness"] < 0.1
        assert any("dark" in w for w in result["warnings"])

    def test_bright_image(self) -> None:
        # JPEG header + high-value bytes
        data = _make_jpeg(b"\xfe" * 500)
        result = analyze_snapshot_basic(data)
        assert result["valid"] is True
        assert result["brightness"] > 0.5

    def test_low_variance(self) -> None:
        # Uniform byte value = zero variance
        data = _make_jpeg(b"\x80" * 2000)
        result = analyze_snapshot_basic(data)
        assert result["valid"] is True
        # Uniform data may trigger low variance warning
        assert result["variance"] < 0.05

    def test_normal_image(self) -> None:
        # Mix of byte values — should have reasonable brightness and variance
        body = bytes(range(256)) * 4  # 1024 bytes with full range
        data = _make_jpeg(body)
        result = analyze_snapshot_basic(data)
        assert result["valid"] is True
        assert result["brightness"] > 0.1
        assert result["variance"] > 0.01
        # Should have no warnings for a normal image
        assert len(result["warnings"]) == 0

    def test_png_format_accepted(self) -> None:
        # PNG magic header
        png_header = b"\x89PNG\r\n\x1a\n"
        data = png_header + bytes(range(256)) * 4
        result = analyze_snapshot_basic(data)
        assert result["valid"] is True

    def test_none_input_returns_invalid(self) -> None:
        # Passing None-like empty data
        result = analyze_snapshot_basic(b"")
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# TestFirstLayerMonitor
# ---------------------------------------------------------------------------


class TestFirstLayerMonitor:
    """FirstLayerMonitor — camera gating, state tracking, snapshot collection."""

    def test_monitor_no_camera_not_required(self) -> None:
        """Adapter without snapshot capability, require_camera=False -> still runs."""
        adapter = _make_mock_adapter(can_snapshot=False, state=PrinterStatus.PRINTING)
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=2,
            first_layer_interval_seconds=0,
            require_camera=False,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert result.outcome in ("passed", "print_ended")
        assert result.success is True

    def test_monitor_no_camera_required_strict(self) -> None:
        """require_camera=True with no snapshot capability -> no_camera."""
        adapter = _make_mock_adapter(can_snapshot=False)
        policy = MonitorPolicy(require_camera=True)
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        result = monitor.monitor()
        assert result.outcome == "no_camera"
        assert result.success is False

    def test_monitor_print_not_running(self) -> None:
        """Printer is IDLE -> print_ended during delay."""
        adapter = _make_mock_adapter(state=PrinterStatus.IDLE)
        policy = MonitorPolicy(first_layer_delay_seconds=0)
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)

        # _wait_printing returns False when print is not running
        with mock.patch.object(monitor, "_wait_printing", return_value=False):
            result = monitor.monitor()
        assert result.outcome == "print_ended"

    def test_monitor_captures_snapshots(self) -> None:
        """Mock adapter returns snapshots -> result has correct count."""
        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter = _make_mock_adapter(snapshot_data=jpeg_data)
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=3,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert result.outcome == "passed"
        assert result.success is True
        assert len(result.snapshots) == 3

    def test_monitor_print_ends_during_delay(self) -> None:
        """Print finishes during initial wait -> print_ended."""
        adapter = _make_mock_adapter()
        policy = MonitorPolicy(first_layer_delay_seconds=60)
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)

        # Simulate print ending during delay
        with mock.patch.object(monitor, "_wait_printing", return_value=False):
            result = monitor.monitor()
        assert result.outcome == "print_ended"

    def test_monitor_print_errors_during_check(self) -> None:
        """Printer goes ERROR during monitoring -> print_ended or error."""
        adapter = _make_mock_adapter()
        # After delay, get_state returns ERROR
        states = [
            PrinterState(connected=True, state=PrinterStatus.ERROR),
        ]
        adapter.get_state.side_effect = states

        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=3,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        # Should detect ERROR state and stop
        assert result.outcome in ("print_ended", "error")

    def test_monitor_snapshot_failure_retry(self) -> None:
        """First snapshot attempt fails, retry succeeds -> one snapshot captured."""
        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter = _make_mock_adapter()
        # First call raises, second returns data (retry within _capture_snapshot)
        adapter.get_snapshot.side_effect = [
            PrinterError("timeout"),
            jpeg_data,
        ]
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=1,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            with mock.patch("kiln.print_monitor.time.sleep"):
                result = monitor.monitor()
        assert result.success is True
        assert len(result.snapshots) == 1

    def test_monitor_all_snapshots_fail(self) -> None:
        """All snapshot attempts fail -> still completes but with failures."""
        adapter = _make_mock_adapter()
        # All calls fail
        adapter.get_snapshot.side_effect = PrinterError("camera offline")
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=2,
            first_layer_interval_seconds=0,
            max_snapshot_failures=10,  # High threshold so it doesn't trigger alert
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            with mock.patch("kiln.print_monitor.time.sleep"):
                result = monitor.monitor()
        assert result.snapshot_failures > 0

    def test_monitor_max_snapshot_failures_alert(self) -> None:
        """Consecutive failures exceed threshold -> publishes alert."""
        adapter = _make_mock_adapter()
        adapter.get_snapshot.side_effect = PrinterError("camera offline")
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=5,
            first_layer_interval_seconds=0,
            max_snapshot_failures=2,
        )
        event_bus = mock.MagicMock()
        monitor = FirstLayerMonitor(
            adapter, "test", policy=policy, event_bus=event_bus
        )
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            with mock.patch("kiln.print_monitor.time.sleep"):
                result = monitor.monitor()
        # Should have triggered an alert via event_bus
        assert result.snapshot_failures >= 2
        # The monitor should have published a VISION_ALERT
        alert_calls = [
            c for c in event_bus.publish.call_args_list
            if c[0][0] == EventType.VISION_ALERT
        ]
        assert len(alert_calls) > 0

    def test_monitor_publishes_vision_check_events(self) -> None:
        """Event bus receives VISION_CHECK per successful snapshot."""

        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter = _make_mock_adapter(snapshot_data=jpeg_data)
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=2,
            first_layer_interval_seconds=0,
        )
        event_bus = mock.MagicMock()
        monitor = FirstLayerMonitor(
            adapter, "test", policy=policy, event_bus=event_bus
        )
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert result.success is True
        assert len(result.snapshots) == 2
        # Should have published VISION_CHECK for each snapshot
        check_calls = [
            c for c in event_bus.publish.call_args_list
            if c[0][0] == EventType.VISION_CHECK
        ]
        assert len(check_calls) == 2

    def test_monitor_custom_policy(self) -> None:
        """Custom delay/interval/count are all respected."""
        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter = _make_mock_adapter(snapshot_data=jpeg_data)
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=5,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert len(result.snapshots) == 5

    def test_monitor_zero_delay(self) -> None:
        """delay=0 starts checking immediately."""
        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter = _make_mock_adapter(snapshot_data=jpeg_data)
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=1,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert result.success is True
        assert len(result.snapshots) == 1

    def test_monitor_one_check(self) -> None:
        """Single check count works correctly."""
        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter = _make_mock_adapter(snapshot_data=jpeg_data)
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=1,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert len(result.snapshots) == 1
        assert result.outcome == "passed"

    def test_monitor_printer_paused_during_monitoring(self) -> None:
        """Printer pauses -> outcome depends on state classification."""
        adapter = _make_mock_adapter()
        # PAUSED is not in _TERMINAL_STATES, so monitoring should continue.
        # But if IDLE is returned, that IS terminal.
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PAUSED
        )
        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter.get_snapshot.return_value = jpeg_data
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=2,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        # PAUSED is not terminal, so monitoring continues
        assert result.success is True

    def test_monitor_duration_recorded(self) -> None:
        """result.duration_seconds is set to a reasonable value."""
        jpeg_data = _make_jpeg(bytes(range(256)) * 2)
        adapter = _make_mock_adapter(snapshot_data=jpeg_data)
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=1,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert result.duration_seconds >= 0.0

    def test_monitor_default_policy_used_when_none(self) -> None:
        """When no policy is passed, defaults are used."""
        adapter = _make_mock_adapter(can_snapshot=False)
        monitor = FirstLayerMonitor(adapter, "test")
        # With no camera and require_camera=False (default), should succeed
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert result.success is True

    def test_monitor_printer_offline_during_checks(self) -> None:
        """Printer goes OFFLINE during snapshot collection."""
        adapter = _make_mock_adapter()
        adapter.get_state.return_value = PrinterState(
            connected=False, state=PrinterStatus.OFFLINE
        )
        policy = MonitorPolicy(
            first_layer_delay_seconds=0,
            first_layer_check_count=3,
            first_layer_interval_seconds=0,
        )
        monitor = FirstLayerMonitor(adapter, "test", policy=policy)
        with mock.patch.object(monitor, "_wait_printing", return_value=True):
            result = monitor.monitor()
        assert result.outcome == "print_ended"


# ---------------------------------------------------------------------------
# TestLoadMonitorPolicy
# ---------------------------------------------------------------------------


class TestLoadMonitorPolicy:
    """load_monitor_policy — env var overrides, config file, defaults."""

    def test_default_no_config(self) -> None:
        """No config file or env vars -> default policy."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch(
                "kiln.print_monitor.load_monitor_policy",
                wraps=load_monitor_policy,
            ):
                # Patch config loading to avoid file system
                with mock.patch(
                    "kiln.cli.config._read_config_file", return_value={}
                ):
                    policy = load_monitor_policy()
        assert policy.first_layer_delay_seconds == 120
        assert policy.first_layer_check_count == 3
        assert policy.auto_pause_on_failure is True

    def test_env_var_delay(self) -> None:
        """KILN_MONITOR_FIRST_LAYER_DELAY overrides default."""
        env = {"KILN_MONITOR_FIRST_LAYER_DELAY": "30"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch(
                "kiln.cli.config._read_config_file", return_value={}
            ):
                policy = load_monitor_policy()
        assert policy.first_layer_delay_seconds == 30

    def test_env_var_checks(self) -> None:
        """KILN_MONITOR_FIRST_LAYER_CHECKS overrides default."""
        env = {"KILN_MONITOR_FIRST_LAYER_CHECKS": "5"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch(
                "kiln.cli.config._read_config_file", return_value={}
            ):
                policy = load_monitor_policy()
        assert policy.first_layer_check_count == 5

    def test_env_var_auto_pause(self) -> None:
        """KILN_MONITOR_AUTO_PAUSE=false disables auto-pause."""
        env = {"KILN_MONITOR_AUTO_PAUSE": "false"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch(
                "kiln.cli.config._read_config_file", return_value={}
            ):
                policy = load_monitor_policy()
        assert policy.auto_pause_on_failure is False

    def test_env_var_require_camera(self) -> None:
        """KILN_MONITOR_REQUIRE_CAMERA=true enables camera requirement."""
        env = {"KILN_MONITOR_REQUIRE_CAMERA": "true"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch(
                "kiln.cli.config._read_config_file", return_value={}
            ):
                policy = load_monitor_policy()
        assert policy.require_camera is True

    def test_config_file(self, tmp_path: Any) -> None:
        """Config file monitoring section loaded correctly."""
        config_data = {
            "monitoring": {
                "first_layer_delay_seconds": 45,
                "first_layer_check_count": 6,
                "first_layer_interval_seconds": 30,
                "auto_pause_on_failure": False,
                "require_camera": True,
            }
        }
        config_path = tmp_path / "config.yaml"
        with config_path.open("w") as fh:
            yaml.safe_dump(config_data, fh)

        # Clear relevant env vars
        env_clear = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("KILN_MONITOR_")
        }
        with mock.patch.dict(os.environ, env_clear, clear=True):
            with mock.patch(
                "kiln.cli.config.get_config_path", return_value=config_path
            ):
                policy = load_monitor_policy()
        assert policy.first_layer_delay_seconds == 45
        assert policy.first_layer_check_count == 6
        assert policy.first_layer_interval_seconds == 30
        assert policy.auto_pause_on_failure is False
        assert policy.require_camera is True


# ---------------------------------------------------------------------------
# TestAutonomyFirstLayerConstraint
# ---------------------------------------------------------------------------


class TestAutonomyFirstLayerConstraint:
    """Autonomy integration for require_first_layer_check constraint."""

    def test_constraint_default_false(self) -> None:
        from kiln.autonomy import AutonomyConstraints

        c = AutonomyConstraints()
        assert c.require_first_layer_check is False

    def test_constraint_in_to_dict(self) -> None:
        from kiln.autonomy import AutonomyConstraints

        c = AutonomyConstraints(require_first_layer_check=True)
        d = c.to_dict()
        assert d["require_first_layer_check"] is True

    def test_constraint_from_env_var(self) -> None:
        from kiln.autonomy import AutonomyConstraints, load_autonomy_config

        env = {
            "KILN_AUTONOMY_LEVEL": "1",
            "KILN_MONITOR_REQUIRE_FIRST_LAYER": "true",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = load_autonomy_config()
        assert config.constraints.require_first_layer_check is True

    def test_constraint_from_config(self, tmp_path: Any) -> None:
        from kiln.autonomy import load_autonomy_config

        config_data = {
            "autonomy": {
                "level": 1,
                "constraints": {
                    "require_first_layer_check": True,
                },
            }
        }
        config_path = tmp_path / "config.yaml"
        with config_path.open("w") as fh:
            yaml.safe_dump(config_data, fh)

        env_clear = {
            k: v
            for k, v in os.environ.items()
            if k not in ("KILN_AUTONOMY_LEVEL", "KILN_MONITOR_REQUIRE_FIRST_LAYER")
        }
        with mock.patch.dict(os.environ, env_clear, clear=True):
            with mock.patch(
                "kiln.cli.config.get_config_path", return_value=config_path
            ):
                config = load_autonomy_config()
        assert config.constraints.require_first_layer_check is True

    def test_check_autonomy_includes_requirement(self) -> None:
        from kiln.autonomy import (
            AutonomyConfig,
            AutonomyConstraints,
            AutonomyLevel,
            check_autonomy,
        )

        config = AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(
                require_first_layer_check=True,
                allowed_tools=["start_print"],
            ),
        )
        result = check_autonomy(
            "start_print", "confirm", config=config
        )
        assert result["allowed"] is True
        assert result.get("require_first_layer_check") is True

    def test_check_autonomy_no_requirement(self) -> None:
        from kiln.autonomy import (
            AutonomyConfig,
            AutonomyConstraints,
            AutonomyLevel,
            check_autonomy,
        )

        config = AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(
                require_first_layer_check=False,
                allowed_tools=["start_print"],
            ),
        )
        result = check_autonomy(
            "start_print", "confirm", config=config
        )
        assert result["allowed"] is True
        assert "require_first_layer_check" not in result

    def test_check_autonomy_level2_includes_requirement(self) -> None:
        from kiln.autonomy import (
            AutonomyConfig,
            AutonomyConstraints,
            AutonomyLevel,
            check_autonomy,
        )

        config = AutonomyConfig(
            level=AutonomyLevel.FULL_TRUST,
            constraints=AutonomyConstraints(require_first_layer_check=True),
        )
        result = check_autonomy(
            "start_print", "confirm", config=config
        )
        assert result["allowed"] is True
        assert result.get("require_first_layer_check") is True

    def test_check_autonomy_non_print_tool_no_flag(self) -> None:
        """require_first_layer_check only applies to start_print/quick_print."""
        from kiln.autonomy import (
            AutonomyConfig,
            AutonomyConstraints,
            AutonomyLevel,
            check_autonomy,
        )

        config = AutonomyConfig(
            level=AutonomyLevel.FULL_TRUST,
            constraints=AutonomyConstraints(require_first_layer_check=True),
        )
        result = check_autonomy(
            "get_printer_status", "safe", config=config
        )
        assert "require_first_layer_check" not in result
