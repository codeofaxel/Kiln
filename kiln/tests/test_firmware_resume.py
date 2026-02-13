"""Tests for OctoPrint+Marlin firmware resume integration.

Coverage:
- G-code sequence generation and ordering
- Temperature validation
- Input validation (z_height, clearance, prime length)
- Fan speed conversion (0-100% to 0-255)
- Flow rate restoration
- Error handling (offline printer, send_gcode failure)
- Safety: Z is never homed, only X/Y
"""

from __future__ import annotations

import json

import pytest
import responses

from kiln.printers.base import PrinterError, PrintResult
from kiln.printers.octoprint import OctoPrintAdapter


@pytest.fixture
def adapter():
    """Create an OctoPrintAdapter for testing."""
    return OctoPrintAdapter(
        host="http://localhost:5000",
        api_key="test-api-key",
        retries=1,
    )


def _extract_commands(call_index: int = 0) -> list[str]:
    """Extract G-code commands from the recorded ``responses`` call."""
    body = json.loads(responses.calls[call_index].request.body)
    return body["commands"]


class TestFirmwareResumePrint:
    """Tests for OctoPrintAdapter.firmware_resume_print()."""

    # ------------------------------------------------------------------
    # Happy-path: G-code sequence
    # ------------------------------------------------------------------

    @responses.activate
    def test_sends_correct_gcode_sequence(self, adapter):
        """Verify the exact G-code sequence sent for a resume."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        result = adapter.firmware_resume_print(
            z_height_mm=22.4,
            hotend_temp_c=210.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            layer_number=112,
            fan_speed_pct=80.0,
            flow_rate_pct=100.0,
        )

        assert result.success is True
        commands = _extract_commands()

        # Critical safety checks:
        # 1. Z is NEVER homed (no G28 Z, no bare G28)
        assert not any("G28 Z" in cmd for cmd in commands), \
            "Z must NEVER be homed during firmware resume"
        assert not any(cmd.strip() == "G28" for cmd in commands), \
            "Bare G28 homes all axes including Z — must not appear"

        # 2. Only X/Y are homed
        assert any("G28 X Y" in cmd for cmd in commands)

        # 3. M413 S0 disables Marlin power-loss recovery (first command)
        assert commands[0] == "M413 S0"

        # 4. Bed heats before hotend wait (thermal expansion re-adheres part)
        bed_heat_idx = next(i for i, c in enumerate(commands) if "M140" in c)
        hotend_wait_idx = next(i for i, c in enumerate(commands) if "M109" in c)
        assert bed_heat_idx < hotend_wait_idx

        # 5. Z is set via G92, not movement
        assert any("G92 Z22.4" in cmd for cmd in commands)

    @responses.activate
    def test_full_command_list(self, adapter):
        """Verify every command in the generated sequence."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            fan_speed_pct=100.0,
            flow_rate_pct=100.0,
            prime_length_mm=30.0,
            z_clearance_mm=2.0,
        )

        commands = _extract_commands()
        assert commands == [
            "M413 S0",
            "G28 X Y",
            "M140 S60.0",
            "M104 S200.0",
            "M190 S60.0",
            "M109 S200.0",
            "G92 E0",
            "G92 Z10.0",
            "G91",
            "G1 Z2.0 F300",
            "G90",
            "G1 E30.0 F200",
            "G92 E0",
            "M106 S254",
            "M221 S100",
        ]

    # ------------------------------------------------------------------
    # Fan speed conversion (0-100 % → 0-255 PWM)
    # ------------------------------------------------------------------

    @responses.activate
    def test_fan_speed_conversion(self, adapter):
        """Fan speed converts from 0-100% to 0-255 PWM."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            fan_speed_pct=50.0,
        )

        commands = _extract_commands()
        # 50% of 255 = 127 (int(50.0 * 2.55) = 127)
        assert any("M106 S127" in cmd for cmd in commands)

    @responses.activate
    def test_fan_speed_100_percent(self, adapter):
        """100% fan = M106 S254 (int truncation of 100*2.55=254.999)."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            fan_speed_pct=100.0,
        )

        commands = _extract_commands()
        assert any("M106 S254" in cmd for cmd in commands)

    @responses.activate
    def test_fan_speed_zero(self, adapter):
        """0% fan = M106 S0."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            fan_speed_pct=0.0,
        )

        commands = _extract_commands()
        assert any("M106 S0" in cmd for cmd in commands)

    # ------------------------------------------------------------------
    # Input validation — z_height
    # ------------------------------------------------------------------

    def test_z_height_zero_raises(self, adapter):
        """z_height_mm == 0 raises PrinterError."""
        with pytest.raises(PrinterError, match="z_height_mm must be > 0"):
            adapter.firmware_resume_print(
                z_height_mm=0.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
            )

    def test_z_height_negative_raises(self, adapter):
        """z_height_mm < 0 raises PrinterError."""
        with pytest.raises(PrinterError, match="z_height_mm must be > 0"):
            adapter.firmware_resume_print(
                z_height_mm=-5.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
            )

    # ------------------------------------------------------------------
    # Input validation — z_clearance
    # ------------------------------------------------------------------

    def test_z_clearance_too_large_raises(self, adapter):
        """z_clearance_mm > 10 raises PrinterError."""
        with pytest.raises(PrinterError, match="z_clearance_mm must be > 0 and <= 10"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
                z_clearance_mm=15.0,
            )

    def test_z_clearance_zero_raises(self, adapter):
        """z_clearance_mm == 0 raises PrinterError."""
        with pytest.raises(PrinterError, match="z_clearance_mm must be > 0 and <= 10"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
                z_clearance_mm=0.0,
            )

    def test_z_clearance_negative_raises(self, adapter):
        """z_clearance_mm < 0 raises PrinterError."""
        with pytest.raises(PrinterError, match="z_clearance_mm must be > 0 and <= 10"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
                z_clearance_mm=-1.0,
            )

    # ------------------------------------------------------------------
    # Input validation — prime length
    # ------------------------------------------------------------------

    def test_negative_prime_length_raises(self, adapter):
        """prime_length_mm < 0 raises PrinterError."""
        with pytest.raises(PrinterError, match="prime_length_mm must be >= 0"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
                prime_length_mm=-5.0,
            )

    @responses.activate
    def test_prime_length_zero_allowed(self, adapter):
        """prime_length_mm == 0 is allowed (skip priming)."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        result = adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            prime_length_mm=0.0,
        )
        assert result.success is True

    # ------------------------------------------------------------------
    # Input validation — temperatures
    # ------------------------------------------------------------------

    def test_hotend_temp_zero_raises(self, adapter):
        """Hotend temp must be > 0 for resume (can't print cold)."""
        with pytest.raises(PrinterError, match="Hotend temperature must be > 0"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=0.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
            )

    def test_hotend_temp_negative_raises(self, adapter):
        """Negative hotend temp raises PrinterError."""
        with pytest.raises(PrinterError, match="Hotend temperature must be > 0"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=-10.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
            )

    @responses.activate
    def test_bed_temp_zero_allowed(self, adapter):
        """Bed temp of 0 is allowed (some prints don't use heated bed)."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        result = adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=0.0,
            file_name="test.gcode",
        )
        assert result.success is True

    # ------------------------------------------------------------------
    # Flow rate
    # ------------------------------------------------------------------

    @responses.activate
    def test_custom_flow_rate(self, adapter):
        """Flow rate multiplier is set via M221."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            flow_rate_pct=95.0,
        )

        commands = _extract_commands()
        assert any("M221 S95" in cmd for cmd in commands)

    @responses.activate
    def test_default_flow_rate_100(self, adapter):
        """Default flow rate is 100%."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
        )

        commands = _extract_commands()
        assert any("M221 S100" in cmd for cmd in commands)

    # ------------------------------------------------------------------
    # Return value / result message
    # ------------------------------------------------------------------

    @responses.activate
    def test_return_includes_layer_info(self, adapter):
        """PrintResult message includes layer info when provided."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        result = adapter.firmware_resume_print(
            z_height_mm=22.4,
            hotend_temp_c=210.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            layer_number=112,
        )
        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "112" in result.message
        assert "22.4" in result.message

    @responses.activate
    def test_return_message_without_layer(self, adapter):
        """PrintResult message works without layer_number."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        result = adapter.firmware_resume_print(
            z_height_mm=5.0,
            hotend_temp_c=215.0,
            bed_temp_c=70.0,
            file_name="benchy.gcode",
        )
        assert result.success is True
        assert "benchy.gcode" in result.message
        assert "layer" not in result.message.lower() or "None" not in result.message

    @responses.activate
    def test_return_message_includes_file_name(self, adapter):
        """PrintResult message includes the file name."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        result = adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="my_model.gcode",
        )
        assert "my_model.gcode" in result.message

    # ------------------------------------------------------------------
    # Error handling — G-code send failure
    # ------------------------------------------------------------------

    @responses.activate
    def test_gcode_send_failure_raises(self, adapter):
        """If send_gcode fails (HTTP 409), PrinterError propagates."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            json={"error": "Printer not connected"},
            status=409,
        )

        with pytest.raises(PrinterError, match="409"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
            )

    @responses.activate
    def test_gcode_send_500_raises(self, adapter):
        """HTTP 500 is non-retryable with retries=1, raises PrinterError."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            json={"error": "Internal server error"},
            status=500,
        )

        with pytest.raises(PrinterError, match="500"):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gcode",
            )

    # ------------------------------------------------------------------
    # Custom prime length and Z clearance
    # ------------------------------------------------------------------

    @responses.activate
    def test_custom_prime_and_clearance(self, adapter):
        """Custom prime length and Z clearance are used in G-code."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=15.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            prime_length_mm=50.0,
            z_clearance_mm=5.0,
        )

        commands = _extract_commands()
        # Z clearance of 5mm in relative mode
        assert any("G1 Z5.0 F300" in cmd for cmd in commands)
        # Prime of 50mm
        assert any("G1 E50.0 F200" in cmd for cmd in commands)

    @responses.activate
    def test_z_clearance_boundary_10mm(self, adapter):
        """z_clearance_mm == 10 is the upper boundary and should work."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        result = adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
            z_clearance_mm=10.0,
        )
        assert result.success is True

    # ------------------------------------------------------------------
    # Safety: ordering invariants
    # ------------------------------------------------------------------

    @responses.activate
    def test_relative_mode_brackets_z_raise(self, adapter):
        """G91 (relative) comes before Z raise, G90 (absolute) comes after."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
        )

        commands = _extract_commands()
        g91_idx = next(i for i, c in enumerate(commands) if c == "G91")
        z_raise_idx = next(i for i, c in enumerate(commands) if c.startswith("G1 Z"))
        g90_idx = next(i for i, c in enumerate(commands) if c == "G90")
        assert g91_idx < z_raise_idx < g90_idx

    @responses.activate
    def test_extruder_reset_before_z_set(self, adapter):
        """G92 E0 (extruder reset) occurs before G92 Z (position set)."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
        )

        commands = _extract_commands()
        first_e_reset_idx = next(i for i, c in enumerate(commands) if c == "G92 E0")
        z_set_idx = next(i for i, c in enumerate(commands) if c.startswith("G92 Z"))
        assert first_e_reset_idx < z_set_idx

    @responses.activate
    def test_bed_wait_before_hotend_wait(self, adapter):
        """M190 (bed wait) must come before M109 (hotend wait)."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
        )

        commands = _extract_commands()
        bed_wait_idx = next(i for i, c in enumerate(commands) if "M190" in c)
        hotend_wait_idx = next(i for i, c in enumerate(commands) if "M109" in c)
        assert bed_wait_idx < hotend_wait_idx

    @responses.activate
    def test_all_commands_sent_in_single_call(self, adapter):
        """All G-code commands are sent in a single POST, not multiple."""
        responses.add(
            responses.POST,
            "http://localhost:5000/api/printer/command",
            status=204,
        )

        adapter.firmware_resume_print(
            z_height_mm=10.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gcode",
        )

        # Exactly one HTTP call
        assert len(responses.calls) == 1
