"""Tests for safety hardening features added to the Kiln MCP server.

Covers:
    - _ToolRateLimiter class
    - _get_temp_limits() with KILN_PRINTER_MODEL
    - Per-printer safety profile propagation via set_safety_profile()
    - Tool safety JSON loading
"""

from __future__ import annotations

import json
import os
import time

import pytest


# ===================================================================
# _ToolRateLimiter
# ===================================================================

class TestToolRateLimiter:
    """Tests for the MCP tool rate limiter."""

    def _make_limiter(self):
        # Import here to avoid triggering server module init.
        from kiln.server import _ToolRateLimiter
        return _ToolRateLimiter()

    def test_first_call_always_allowed(self) -> None:
        limiter = self._make_limiter()
        msg = limiter.check("set_temperature", min_interval_ms=5000, max_per_minute=3)
        assert msg is None

    def test_rapid_calls_blocked(self) -> None:
        limiter = self._make_limiter()
        limiter.check("set_temperature", min_interval_ms=5000, max_per_minute=100)
        msg = limiter.check("set_temperature", min_interval_ms=5000, max_per_minute=100)
        assert msg is not None
        assert "Rate limited" in msg

    def test_max_per_minute_enforced(self) -> None:
        limiter = self._make_limiter()
        for _ in range(3):
            msg = limiter.check("test_tool", min_interval_ms=0, max_per_minute=3)
            assert msg is None
        # 4th call should be blocked
        msg = limiter.check("test_tool", min_interval_ms=0, max_per_minute=3)
        assert msg is not None
        assert "3 times" in msg

    def test_different_tools_independent(self) -> None:
        limiter = self._make_limiter()
        limiter.check("tool_a", min_interval_ms=5000, max_per_minute=1)
        # tool_b should not be affected by tool_a
        msg = limiter.check("tool_b", min_interval_ms=5000, max_per_minute=1)
        assert msg is None

    def test_no_limits_always_passes(self) -> None:
        limiter = self._make_limiter()
        for _ in range(100):
            msg = limiter.check("free_tool", min_interval_ms=0, max_per_minute=0)
            assert msg is None


# ===================================================================
# Per-printer temperature limits
# ===================================================================

class TestGetTempLimits:
    """Tests for _get_temp_limits() with KILN_PRINTER_MODEL."""

    def test_default_limits_without_model(self, monkeypatch) -> None:
        """Without KILN_PRINTER_MODEL, should return generic 300/130."""
        monkeypatch.setattr("kiln.server._PRINTER_MODEL", "")
        from kiln.server import _get_temp_limits
        max_tool, max_bed = _get_temp_limits()
        assert max_tool == 300.0
        assert max_bed == 130.0

    def test_ender3_limits(self, monkeypatch) -> None:
        """Ender 3 has PTFE hotend — max should be 260, not 300."""
        monkeypatch.setattr("kiln.server._PRINTER_MODEL", "ender3")
        from kiln.server import _get_temp_limits
        max_tool, max_bed = _get_temp_limits()
        assert max_tool == 260.0
        assert max_bed == 110.0

    def test_unknown_model_falls_back(self, monkeypatch) -> None:
        """Unknown model should fall back to generic 300/130."""
        monkeypatch.setattr("kiln.server._PRINTER_MODEL", "nonexistent_printer_xyz")
        from kiln.server import _get_temp_limits
        max_tool, max_bed = _get_temp_limits()
        assert max_tool == 300.0
        assert max_bed == 130.0


# ===================================================================
# Adapter safety profile propagation
# ===================================================================

class TestAdapterSafetyProfile:
    """Tests for set_safety_profile() on the base PrinterAdapter."""

    def test_set_safety_profile(self) -> None:
        """Profile ID should be stored on the adapter."""
        from kiln.printers.base import PrinterAdapter

        # Can't instantiate ABC directly, so check the method exists
        assert hasattr(PrinterAdapter, "set_safety_profile")
        assert hasattr(PrinterAdapter, "_safety_profile_id")

    def test_validate_temp_with_profile(self) -> None:
        """_validate_temp should use profile limits when set."""
        from kiln.printers.base import PrinterAdapter, PrinterError

        # Create a minimal concrete subclass for testing
        class _TestAdapter(PrinterAdapter):
            @property
            def name(self):
                return "test"
            @property
            def capabilities(self):
                from kiln.printers.base import PrinterCapabilities
                return PrinterCapabilities()
            def get_state(self):
                pass
            def get_job(self):
                pass
            def list_files(self):
                return []
            def upload_file(self, path):
                pass
            def start_print(self, name):
                pass
            def cancel_print(self):
                pass
            def pause_print(self):
                pass
            def resume_print(self):
                pass
            def emergency_stop(self):
                pass
            def set_tool_temp(self, t):
                return True
            def set_bed_temp(self, t):
                return True
            def send_gcode(self, cmds):
                return True
            def delete_file(self, path):
                return True

        adapter = _TestAdapter()

        # Without profile: 300 should be OK
        adapter._validate_temp(300, 300.0, "Hotend")  # should not raise

        # With ender3 profile: 300 should fail (max 260)
        adapter.set_safety_profile("ender3")
        with pytest.raises(PrinterError, match="260"):
            adapter._validate_temp(300, 300.0, "Hotend")

        # 250 should still be OK for ender3
        adapter._validate_temp(250, 300.0, "Hotend")  # should not raise


# ===================================================================
# Tool safety JSON
# ===================================================================

class TestToolSafetyJson:
    """Tests for the tool_safety.json data file."""

    def test_file_is_valid_json(self) -> None:
        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "kiln", "data", "tool_safety.json"
        )
        with open(path) as fh:
            data = json.load(fh)
        assert "classifications" in data
        assert "_meta" in data

    def test_critical_tools_classified(self) -> None:
        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "kiln", "data", "tool_safety.json"
        )
        with open(path) as fh:
            data = json.load(fh)
        c = data["classifications"]

        # Physical-effect tools must be at least "confirm" level
        assert c["start_print"]["level"] == "confirm"
        assert c["cancel_print"]["level"] == "confirm"
        assert c["set_temperature"]["level"] == "confirm"
        assert c["send_gcode"]["level"] == "confirm"
        assert c["emergency_stop"]["level"] == "emergency"

        # Read-only tools must be "safe"
        assert c["printer_status"]["level"] == "safe"
        assert c["preflight_check"]["level"] == "safe"
        assert c["validate_gcode"]["level"] == "safe"

    def test_all_levels_valid(self) -> None:
        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "kiln", "data", "tool_safety.json"
        )
        with open(path) as fh:
            data = json.load(fh)
        valid_levels = {"safe", "guarded", "confirm", "emergency"}
        for name, info in data["classifications"].items():
            assert info["level"] in valid_levels, f"{name} has invalid level {info['level']}"
