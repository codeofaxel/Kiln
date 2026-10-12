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
        """Without ANY resolvable printer model, fall back to the generic
        floor: the PTFE-safe 240 C hotend and the 80 C bed.

        Both ``_PRINTER_MODEL`` AND ``_resolve_printer_model_live`` must
        be neutralised — the live resolver will otherwise auto-detect a
        Bambu over the network / serial-prefix and return that printer's
        bed limit (e.g. A1's 100°C), masking the true fallback path.
        """
        monkeypatch.setattr("kiln.server._PRINTER_MODEL", "")
        monkeypatch.setattr(
            "kiln.server._resolve_printer_model_live", lambda *_a, **_kw: "",
        )
        from kiln.server import _get_temp_limits
        max_tool, max_bed = _get_temp_limits()
        assert max_tool == 240.0
        assert max_bed == 80.0

    def test_a_ptfe_lined_printers_limits(self, monkeypatch) -> None:
        """A PTFE-lined hotend is clamped to 240 (the PTFE-safe ceiling)
        below its profile's own 260; the bed honours the profile.  The
        session fixture's synthetic ``kilntest_ptfe`` (260 C / 100 C,
        ``hotend_type: ptfe_lined``) stands in for the lined desktop
        machines whose real rows are served."""
        monkeypatch.setattr("kiln.server._PRINTER_MODEL", "kilntest_ptfe")
        monkeypatch.setattr(
            "kiln.server._resolve_printer_model_live",
            lambda *_a, **_kw: "kilntest_ptfe",
        )
        from kiln.server import _get_temp_limits
        max_tool, max_bed = _get_temp_limits()
        # PTFE-clamp ceiling kicks in for the lined hotend.
        assert max_tool == 240.0
        assert max_bed == 100.0

    def test_unknown_model_falls_back(self, monkeypatch) -> None:
        """Unknown model falls back to the generic limits — same as the
        no-model path.

        Those limits were 300C/130C until 2026-07-20, described as
        "conservative" while actually being the LOOSEST ceiling in the fleet.
        An unidentified printer is held to the floor (240C/80C): since
        2026-10-12 a PTFE-safe hotend ceiling, because whether a hotend is
        lined is part of the record Kiln serves per printer."""
        monkeypatch.setattr(
            "kiln.server._PRINTER_MODEL", "nonexistent_printer_xyz",
        )
        monkeypatch.setattr(
            "kiln.server._resolve_printer_model_live",
            lambda *_a, **_kw: "nonexistent_printer_xyz",
        )
        from kiln.server import _get_temp_limits
        max_tool, max_bed = _get_temp_limits()
        assert max_tool == 240.0
        assert max_bed == 80.0


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
            # Filament handling is part of the adapter contract; these stubs
            # never move filament, so the hooks refuse.
            def _load_filament_impl(self, plan):
                raise NotImplementedError
            def _unload_filament_impl(self, plan):
                raise NotImplementedError
            def _purge_filament_impl(self, plan):
                raise NotImplementedError

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
            def _start_print_impl(self, name):
                pass
            def cancel_print(self):
                pass
            def pause_print(self):
                pass
            def _resume_print_impl(self):
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

        # Without a profile the adapter is held to the generic floor, not to
        # the 300 the caller passes.  This line used to assert the opposite
        # ("without profile: 300 should be OK"); it was inverted on purpose,
        # because an adapter literal is the loosest hotend its protocol can
        # drive and says nothing about the one on the bench.
        with pytest.raises(PrinterError, match="has not been told this printer's model"):
            adapter._validate_temp(300, 300.0, "Hotend")
        adapter._validate_temp(240, 300.0, "Hotend")  # at the floor: fine

        # With a lined printer's profile (the synthetic kilntest_ptfe, 260 C
        # ceiling): 300 should fail against the profile's own number.
        adapter.set_safety_profile("kilntest_ptfe")
        with pytest.raises(PrinterError, match="260"):
            adapter._validate_temp(300, 300.0, "Hotend")

        # 260 is the profile's ceiling and still passes.
        adapter._validate_temp(260, 300.0, "Hotend")  # should not raise


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
