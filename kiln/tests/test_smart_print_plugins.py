"""Tests for smart-print plugin tools.

Covers:
    - get_active_material (material_tools plugin)
    - check_print_health (material_tools plugin)
    - retry_print_with_fix (smart_print_tools plugin)
    - slice_and_estimate (estimate_tools plugin)
    - _format_time helper (estimate_tools module-level)
"""

from __future__ import annotations

from unittest import mock

import pytest  # noqa: F401 — used implicitly by pytest fixture system

from kiln.printers.base import JobProgress, PrinterError, PrinterState, PrinterStatus

# ---------------------------------------------------------------------------
# Helper: minimal MockMCP that captures registered tools
# ---------------------------------------------------------------------------


class _MockMCP:
    """Captures tools registered via @mcp.tool()."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Fixtures: pre-registered tool callables
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def material_tools_fns():
    """Register material_tools plugin and return its tool functions."""
    from kiln.plugins.material_tools import plugin

    mcp = _MockMCP()
    plugin.register(mcp)
    return mcp.tools  # {"get_active_material": fn, "check_print_health": fn}


@pytest.fixture(scope="module")
def smart_print_fns():
    """Register smart_print_tools plugin and return its tool functions."""
    from kiln.plugins.smart_print_tools import plugin

    mcp = _MockMCP()
    plugin.register(mcp)
    return mcp.tools  # {"retry_print_with_fix": fn}


@pytest.fixture(scope="module")
def estimate_tools_fns():
    """Register estimate_tools plugin and return its tool functions."""
    from kiln.plugins.estimate_tools import plugin

    mcp = _MockMCP()
    plugin.register(mcp)
    return mcp.tools  # {"slice_and_estimate": fn}


# ---------------------------------------------------------------------------
# Shared adapter builder helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    connected: bool = True,
    status: PrinterStatus = PrinterStatus.PRINTING,
    tool_actual: float | None = 210.0,
    tool_target: float | None = 210.0,
    bed_actual: float | None = 60.0,
    bed_target: float | None = 60.0,
    print_error: int | None = None,
) -> PrinterState:
    return PrinterState(
        connected=connected,
        state=status,
        tool_temp_actual=tool_actual,
        tool_temp_target=tool_target,
        bed_temp_actual=bed_actual,
        bed_temp_target=bed_target,
        print_error=print_error,
    )


def _make_job(
    *,
    completion: float | None = 42.5,
    current_layer: int | None = 10,
    total_layers: int | None = 100,
    print_time_left_seconds: int | None = 3600,
    file_name: str | None = "cube.gcode",
) -> JobProgress:
    return JobProgress(
        file_name=file_name,
        completion=completion,
        print_time_left_seconds=print_time_left_seconds,
        current_layer=current_layer,
        total_layers=total_layers,
    )


# ===========================================================================
# get_active_material
# ===========================================================================


class TestGetActiveMaterial:
    """Tests for get_active_material tool."""

    def test_non_bambu_printer_returns_unknown(self, material_tools_fns):
        """Non-Bambu adapter (no get_ams_status) returns source='unknown'."""
        fn = material_tools_fns["get_active_material"]
        adapter = mock.MagicMock(spec=[])  # no get_ams_status attribute
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            result = fn()
        assert result["success"] is True
        assert result["source"] == "unknown"
        assert result["material"] == "unknown"

    def test_external_spool_tray_255(self, material_tools_fns):
        """tray_now='255' means external spool → source='external_spool'."""
        fn = material_tools_fns["get_active_material"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = {"tray_now": "255", "units": []}
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            result = fn()
        assert result["success"] is True
        assert result["source"] == "external_spool"
        assert result["material"] == "unknown"

    def test_ams_slot_0_pla_loaded(self, material_tools_fns):
        """AMS slot 0 with PLA → correct material, color, remaining."""
        fn = material_tools_fns["get_active_material"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = {
            "tray_now": "0",
            "units": [
                {
                    "trays": [
                        {
                            "slot": 0,
                            "tray_type": "PLA",
                            "tray_color": "FF0000",
                            "remain": 78,
                            "nozzle_temp_min": 190,
                            "nozzle_temp_max": 240,
                        }
                    ]
                }
            ],
        }
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            result = fn()
        assert result["success"] is True
        assert result["material"] == "PLA"
        assert result["source"] == "ams_slot_0"
        assert result["color"] == "FF0000"
        assert result["remaining_percent"] == 78
        assert result["nozzle_temp_range"] == [190, 240]

    def test_ams_empty_units_list_fallback(self, material_tools_fns):
        """AMS with empty units list → graceful fallback with source='ams_slot_N'."""
        fn = material_tools_fns["get_active_material"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = {"tray_now": "2", "units": []}
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            result = fn()
        assert result["success"] is True
        assert result["material"] == "unknown"
        assert "ams_slot_2" in result["source"]

    def test_printer_connection_error(self, material_tools_fns):
        """Connection exception → error dict with CONNECTION_ERROR code."""
        fn = material_tools_fns["get_active_material"]
        with mock.patch(
            "kiln.server._get_adapter",
            side_effect=RuntimeError("refused"),
        ):
            result = fn()
        assert result["success"] is False
        assert result["error"]["code"] == "CONNECTION_ERROR"

    def test_ams_printer_error_on_get_ams_status(self, material_tools_fns):
        """PrinterError from get_ams_status → PRINTER_ERROR code."""
        fn = material_tools_fns["get_active_material"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.side_effect = PrinterError("AMS timeout")
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            result = fn()
        assert result["success"] is False
        assert result["error"]["code"] == "PRINTER_ERROR"

    def test_printer_not_found_by_name(self, material_tools_fns):
        """Unknown printer_name → NOT_FOUND error."""
        from kiln.registry import PrinterNotFoundError

        fn = material_tools_fns["get_active_material"]
        with mock.patch("kiln.server._registry") as mock_reg:
            mock_reg.get.side_effect = PrinterNotFoundError("no-such")
            result = fn(printer_name="no-such")
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_tray_data_missing_for_active_slot(self, material_tools_fns):
        """Active slot index not found in tray list → unknown with ams_slot source."""
        fn = material_tools_fns["get_active_material"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = {
            "tray_now": "3",
            "units": [{"trays": [{"slot": 0, "tray_type": "PLA"}]}],
        }
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            result = fn()
        assert result["success"] is True
        assert result["material"] == "unknown"
        assert result["source"] == "ams_slot_3"

    def test_result_excludes_color_when_missing(self, material_tools_fns):
        """color key absent from result when tray has no color data."""
        fn = material_tools_fns["get_active_material"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = {
            "tray_now": "1",
            "units": [{"trays": [{"slot": 1, "tray_type": "PETG"}]}],
        }
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            result = fn()
        assert result["success"] is True
        assert "color" not in result
        assert "remaining_percent" not in result


# ===========================================================================
# check_print_health
# ===========================================================================


class TestCheckPrintHealth:
    """Tests for check_print_health tool."""

    def _run(self, fns, adapter, **kwargs):
        fn = fns["check_print_health"]
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            return fn(**kwargs)

    def test_healthy_printer_all_checks_pass(self, material_tools_fns):
        """Nominal temps, printing, no errors → health='healthy'."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state()
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["success"] is True
        assert result["health"] == "healthy"
        assert result["checks"]["temperature"]["status"] == "ok"
        assert result["checks"]["error_state"]["status"] == "ok"
        assert result["checks"]["printer_connected"]["status"] == "ok"

    def test_tool_temperature_drift_over_15(self, material_tools_fns):
        """Tool temp 30°C below target → health='warning'."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state(
            tool_actual=180.0, tool_target=210.0
        )
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["health"] == "warning"
        assert result["checks"]["temperature"]["status"] == "warning"
        assert any("Tool" in a for a in result["anomalies"])

    def test_bed_temperature_drift_over_15(self, material_tools_fns):
        """Bed temp 20°C below target → health='warning'."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state(
            bed_actual=40.0, bed_target=60.0
        )
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["health"] == "warning"
        assert any("Bed" in a for a in result["anomalies"])

    def test_printer_error_state_nonzero(self, material_tools_fns):
        """Non-zero print_error → health='critical'."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state(print_error=0x0300_1234)
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["health"] == "critical"
        assert result["checks"]["error_state"]["status"] == "critical"

    def test_printer_offline(self, material_tools_fns):
        """Disconnected printer → health='critical'."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state(
            connected=False, status=PrinterStatus.OFFLINE
        )
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["health"] == "critical"
        assert result["checks"]["printer_connected"]["status"] == "critical"

    def test_printer_state_raises_printer_error(self, material_tools_fns):
        """PrinterError from get_state → critical connectivity check."""
        adapter = mock.MagicMock()
        adapter.get_state.side_effect = PrinterError("connection refused")
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["success"] is True
        assert result["health"] == "critical"
        assert result["checks"]["printer_connected"]["status"] == "critical"

    def test_idle_printer_returns_healthy(self, material_tools_fns):
        """Idle, connected, no active targets, no errors → healthy."""
        adapter = mock.MagicMock()
        # targets are None when printer is idle (not heating)
        adapter.get_state.return_value = _make_state(
            status=PrinterStatus.IDLE,
            tool_actual=25.0,
            tool_target=None,
            bed_actual=22.0,
            bed_target=None,
        )
        adapter.get_job.return_value = JobProgress()
        result = self._run(material_tools_fns, adapter)
        assert result["success"] is True
        assert result["health"] == "healthy"

    def test_job_progress_included_in_result(self, material_tools_fns):
        """job_progress key is present when get_job succeeds."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state()
        adapter.get_job.return_value = _make_job(completion=75.0)
        result = self._run(material_tools_fns, adapter)
        assert "job_progress" in result
        assert result["job_progress"]["completion"] == 75.0

    def test_printer_state_included_in_result(self, material_tools_fns):
        """printer_state key is present when get_state succeeds."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state()
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert "printer_state" in result

    def test_connection_error_before_adapter_resolved(self, material_tools_fns):
        """Exception resolving adapter → CONNECTION_ERROR."""
        fn = material_tools_fns["check_print_health"]
        with mock.patch(
            "kiln.server._get_adapter", side_effect=OSError("no route")
        ):
            result = fn()
        assert result["success"] is False
        assert result["error"]["code"] == "CONNECTION_ERROR"

    def test_warning_does_not_override_critical(self, material_tools_fns):
        """When both warning and critical checks exist → health='critical'."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state(
            connected=False,
            status=PrinterStatus.OFFLINE,
            tool_actual=180.0,
            tool_target=210.0,
        )
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["health"] == "critical"

    def test_temp_within_15_degrees_is_ok(self, material_tools_fns):
        """Temp drift exactly at boundary (14°C) → no warning."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state(
            tool_actual=196.0, tool_target=210.0  # delta = 14
        )
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["checks"]["temperature"]["status"] == "ok"

    def test_no_temp_data_yields_ok(self, material_tools_fns):
        """No temperature data → temp check ok with informative detail."""
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _make_state(
            tool_actual=None, tool_target=None, bed_actual=None, bed_target=None
        )
        adapter.get_job.return_value = _make_job()
        result = self._run(material_tools_fns, adapter)
        assert result["checks"]["temperature"]["status"] == "ok"
        assert "No temperature" in result["checks"]["temperature"]["detail"]


# ===========================================================================
# retry_print_with_fix
# ===========================================================================


class TestRetryPrintWithFix:
    """Tests for retry_print_with_fix tool."""

    def _make_slice_result(self, output_path: str = "/tmp/out.gcode"):
        sr = mock.MagicMock()
        sr.output_path = output_path
        sr.slicer = "PrusaSlicer"
        sr.to_dict.return_value = {"output_path": output_path, "slicer": "PrusaSlicer"}
        return sr

    def _make_upload_result(self, file_name: str = "out.gcode"):
        ur = mock.MagicMock()
        ur.file_name = file_name
        ur.to_dict.return_value = {"file_name": file_name}
        return ur

    def _make_print_result(self):
        pr = mock.MagicMock()
        pr.to_dict.return_value = {"started": True}
        return pr

    def _make_diagnosis(self, category: str = "adhesion", overrides: dict | None = None):
        d = mock.MagicMock()
        d.to_dict.return_value = {
            "failure_category": category,
            "slicer_overrides": overrides or {"brim_width": "8"},
        }
        return d

    def test_basic_flow_calls_slice_upload_print(self, smart_print_fns):
        """Happy path: diagnosis → overrides → slice → upload → print."""
        fn = smart_print_fns["retry_print_with_fix"]
        # spec lists only methods present so hasattr(adapter, "get_ams_status") is False
        adapter = mock.MagicMock(spec=["get_state", "upload_file", "start_print"])
        adapter.get_state.return_value = _make_state()
        slice_result = self._make_slice_result()
        upload_result = self._make_upload_result()
        print_result = self._make_print_result()
        adapter.upload_file.return_value = upload_result
        adapter.start_print.return_value = print_result

        diag = self._make_diagnosis()
        watchdog = mock.MagicMock()

        with (
            mock.patch("kiln.server._get_adapter", return_value=adapter),
            mock.patch("kiln.server._map_printer_hint_to_profile_id", return_value=None),
            mock.patch("kiln.server._PRINTER_MODEL", None),
            mock.patch("kiln.server._resolve_effective_printer_name", return_value="default"),
            mock.patch("kiln.server._emergency_latch_error", return_value=None),
            mock.patch("kiln.server.preflight_check", return_value={"ready": True}),
            mock.patch("kiln.server._heater_watchdog", watchdog),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.printability.diagnose_from_signals", return_value=diag),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
            mock.patch("kiln.slicer_profiles.resolve_slicer_profile", return_value=None),
        ):
            result = fn(model_path="/tmp/cube.stl")

        assert result["success"] is True
        assert result["diagnosis"] is not None
        adapter.start_print.assert_called_once()
        watchdog.notify_print_started.assert_called_once()

    def test_skip_diagnosis_with_custom_overrides(self, smart_print_fns):
        """skip_diagnosis=True + custom_overrides → overrides applied, no diagnosis."""
        fn = smart_print_fns["retry_print_with_fix"]
        adapter = mock.MagicMock(spec=["get_state", "upload_file", "start_print"])
        adapter.get_state.return_value = _make_state()
        slice_result = self._make_slice_result()
        upload_result = self._make_upload_result()
        print_result = self._make_print_result()
        adapter.upload_file.return_value = upload_result
        adapter.start_print.return_value = print_result
        watchdog = mock.MagicMock()

        with (
            mock.patch("kiln.server._get_adapter", return_value=adapter),
            mock.patch("kiln.server._map_printer_hint_to_profile_id", return_value=None),
            mock.patch("kiln.server._PRINTER_MODEL", None),
            mock.patch("kiln.server._resolve_effective_printer_name", return_value="default"),
            mock.patch("kiln.server._emergency_latch_error", return_value=None),
            mock.patch("kiln.server.preflight_check", return_value={"ready": True}),
            mock.patch("kiln.server._heater_watchdog", watchdog),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.slicer_profiles.resolve_slicer_profile", return_value=None),
        ):
            result = fn(
                model_path="/tmp/cube.stl",
                skip_diagnosis=True,
                custom_overrides='{"brim_width": "5"}',
            )

        assert result["success"] is True
        # No diagnosis run → diagnosis key is None
        assert result["diagnosis"] is None
        assert result["overrides_applied"] == {"brim_width": "5"}

    def test_invalid_custom_overrides_json_returns_validation_error(self, smart_print_fns):
        """Non-JSON custom_overrides string → VALIDATION_ERROR immediately (before adapter)."""
        fn = smart_print_fns["retry_print_with_fix"]
        # VALIDATION_ERROR is raised before the adapter is resolved — no adapter needed
        result = fn(model_path="/tmp/cube.stl", custom_overrides="not json {{")
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_custom_overrides_non_dict_json_returns_validation_error(self, smart_print_fns):
        """JSON array custom_overrides → VALIDATION_ERROR (before adapter)."""
        fn = smart_print_fns["retry_print_with_fix"]
        result = fn(model_path="/tmp/cube.stl", custom_overrides='["a","b"]')
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_ams_material_auto_detection(self, smart_print_fns):
        """When material is omitted and adapter has AMS, material_detected is populated."""
        fn = smart_print_fns["retry_print_with_fix"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = {
            "tray_now": "0",
            "units": [{"trays": [{"slot": 0, "tray_type": "PETG"}]}],
        }
        adapter.get_state.return_value = _make_state()
        slice_result = self._make_slice_result()
        upload_result = self._make_upload_result()
        print_result = self._make_print_result()
        adapter.upload_file.return_value = upload_result
        adapter.start_print.return_value = print_result
        diag = self._make_diagnosis()
        watchdog = mock.MagicMock()

        with (
            mock.patch("kiln.server._get_adapter", return_value=adapter),
            mock.patch("kiln.server._map_printer_hint_to_profile_id", return_value=None),
            mock.patch("kiln.server._PRINTER_MODEL", None),
            mock.patch("kiln.server._resolve_effective_printer_name", return_value="default"),
            mock.patch("kiln.server._emergency_latch_error", return_value=None),
            mock.patch("kiln.server.preflight_check", return_value={"ready": True}),
            mock.patch("kiln.server._heater_watchdog", watchdog),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.printability.diagnose_from_signals", return_value=diag),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
            mock.patch("kiln.slicer_profiles.resolve_slicer_profile", return_value=None),
        ):
            result = fn(model_path="/tmp/cube.stl")

        assert result["success"] is True
        assert result["material_detected"] == "PETG"

    def test_slicer_not_found_returns_slicer_not_found_error(self, smart_print_fns):
        """SlicerNotFoundError → SLICER_NOT_FOUND code."""
        from kiln.slicer import SlicerNotFoundError

        fn = smart_print_fns["retry_print_with_fix"]
        adapter = mock.MagicMock(spec=["get_state", "upload_file", "start_print"])
        adapter.get_state.return_value = _make_state()

        with (
            mock.patch("kiln.server._get_adapter", return_value=adapter),
            mock.patch("kiln.server._map_printer_hint_to_profile_id", return_value=None),
            mock.patch("kiln.server._PRINTER_MODEL", None),
            mock.patch("kiln.slicer.slice_file", side_effect=SlicerNotFoundError("not found")),
            mock.patch("kiln.printability.diagnose_from_signals", side_effect=Exception("skip")),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
            mock.patch("kiln.slicer_profiles.resolve_slicer_profile", return_value=None),
        ):
            result = fn(model_path="/tmp/cube.stl", skip_diagnosis=True)

        assert result["success"] is False
        assert result["error"]["code"] == "SLICER_NOT_FOUND"

    def test_printer_unavailable_returns_error(self, smart_print_fns):
        """Exception resolving adapter → PRINTER_UNAVAILABLE."""
        fn = smart_print_fns["retry_print_with_fix"]
        with mock.patch(
            "kiln.server._get_adapter", side_effect=RuntimeError("offline")
        ):
            result = fn(model_path="/tmp/cube.stl")
        assert result["success"] is False
        assert result["error"]["code"] == "PRINTER_UNAVAILABLE"

    def test_custom_overrides_win_over_diagnosis(self, smart_print_fns):
        """custom_overrides values take precedence over diagnosis overrides on conflict."""
        fn = smart_print_fns["retry_print_with_fix"]
        adapter = mock.MagicMock(spec=["get_state", "upload_file", "start_print"])
        adapter.get_state.return_value = _make_state()
        slice_result = self._make_slice_result()
        upload_result = self._make_upload_result()
        print_result = self._make_print_result()
        adapter.upload_file.return_value = upload_result
        adapter.start_print.return_value = print_result

        # Diagnosis recommends brim_width=4; custom overrides it to 10
        diag = self._make_diagnosis(overrides={"brim_width": "4"})
        watchdog = mock.MagicMock()

        with (
            mock.patch("kiln.server._get_adapter", return_value=adapter),
            mock.patch("kiln.server._map_printer_hint_to_profile_id", return_value=None),
            mock.patch("kiln.server._PRINTER_MODEL", None),
            mock.patch("kiln.server._resolve_effective_printer_name", return_value="default"),
            mock.patch("kiln.server._emergency_latch_error", return_value=None),
            mock.patch("kiln.server.preflight_check", return_value={"ready": True}),
            mock.patch("kiln.server._heater_watchdog", watchdog),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.printability.diagnose_from_signals", return_value=diag),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
            mock.patch("kiln.slicer_profiles.resolve_slicer_profile", return_value=None),
        ):
            result = fn(
                model_path="/tmp/cube.stl",
                custom_overrides='{"brim_width": "10"}',
            )

        assert result["success"] is True
        assert result["overrides_applied"]["brim_width"] == "10"

    def test_ams_external_spool_no_material_detected(self, smart_print_fns):
        """AMS tray_now='255' (external spool) → material_detected stays None."""
        fn = smart_print_fns["retry_print_with_fix"]
        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = {"tray_now": "255", "units": []}
        adapter.get_state.return_value = _make_state()
        slice_result = self._make_slice_result()
        upload_result = self._make_upload_result()
        print_result = self._make_print_result()
        adapter.upload_file.return_value = upload_result
        adapter.start_print.return_value = print_result
        diag = self._make_diagnosis()
        watchdog = mock.MagicMock()

        with (
            mock.patch("kiln.server._get_adapter", return_value=adapter),
            mock.patch("kiln.server._map_printer_hint_to_profile_id", return_value=None),
            mock.patch("kiln.server._PRINTER_MODEL", None),
            mock.patch("kiln.server._resolve_effective_printer_name", return_value="default"),
            mock.patch("kiln.server._emergency_latch_error", return_value=None),
            mock.patch("kiln.server.preflight_check", return_value={"ready": True}),
            mock.patch("kiln.server._heater_watchdog", watchdog),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.printability.diagnose_from_signals", return_value=diag),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
            mock.patch("kiln.slicer_profiles.resolve_slicer_profile", return_value=None),
        ):
            result = fn(model_path="/tmp/cube.stl")

        assert result["success"] is True
        assert result["material_detected"] is None


# ===========================================================================
# _format_time helper
# ===========================================================================


class TestFormatTime:
    """Unit tests for the _format_time module-level helper."""

    def test_none_returns_unknown(self):
        from kiln.plugins.estimate_tools import _format_time

        assert _format_time(None) == "unknown"

    def test_zero_returns_zero_minutes(self):
        from kiln.plugins.estimate_tools import _format_time

        assert _format_time(0) == "0m"

    def test_minutes_only(self):
        from kiln.plugins.estimate_tools import _format_time

        assert _format_time(45 * 60) == "45m"

    def test_hours_and_minutes(self):
        from kiln.plugins.estimate_tools import _format_time

        assert _format_time(90 * 60) == "1h 30m"

    def test_exact_one_hour(self):
        from kiln.plugins.estimate_tools import _format_time

        assert _format_time(3600) == "1h 0m"

    def test_large_duration(self):
        from kiln.plugins.estimate_tools import _format_time

        # 10h 15m = 36900s
        assert _format_time(36_900) == "10h 15m"

    def test_small_under_one_minute(self):
        from kiln.plugins.estimate_tools import _format_time

        # 30 seconds → 0m
        assert _format_time(30) == "0m"


# ===========================================================================
# slice_and_estimate
# ===========================================================================


class TestSliceAndEstimate:
    """Tests for slice_and_estimate tool."""

    def _make_slice_result(
        self,
        output_path: str = "/tmp/out.gcode",
        slicer: str = "OrcaSlicer",
    ):
        sr = mock.MagicMock()
        sr.output_path = output_path
        sr.slicer = slicer
        sr.to_dict.return_value = {"output_path": output_path, "slicer": slicer}
        return sr

    def _make_meta(
        self,
        time_seconds: int | None = 5400,
        filament_mm: float | None = 3000.0,
        slicer: str | None = "OrcaSlicer",
    ):
        m = mock.MagicMock()
        m.estimated_time_seconds = time_seconds
        m.filament_used_mm = filament_mm
        m.slicer = slicer
        return m

    def test_basic_flow_returns_estimate(self, estimate_tools_fns):
        """Happy path: slice + metadata → returns time, filament, material."""
        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()
        meta = self._make_meta()

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", return_value=meta),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
        ):
            result = fn(input_path="/tmp/cube.stl", material="PLA")

        assert result["success"] is True
        assert result["estimate"]["estimated_time_seconds"] == 5400
        assert result["estimate"]["estimated_time_human"] == "1h 30m"
        assert result["estimate"]["material"] == "PLA"
        assert result["estimate"]["filament_used_mm"] == 3000.0
        assert result["estimate"]["filament_used_grams"] == round(3000.0 * 0.003, 1)

    def test_time_formatting_in_estimate(self, estimate_tools_fns):
        """estimated_time_human reflects _format_time output for given seconds."""
        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()
        meta = self._make_meta(time_seconds=2700)  # 45 minutes

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", return_value=meta),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
        ):
            result = fn(input_path="/tmp/cube.stl")

        assert result["estimate"]["estimated_time_human"] == "45m"

    def test_with_printability_analysis_included(self, estimate_tools_fns):
        """STL with printability analysis → printability and adhesion keys populated."""
        from kiln.printability import AdhesionRecommendation, BedAdhesionAnalysis

        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()
        meta = self._make_meta()

        mock_adhesion = BedAdhesionAnalysis(
            contact_area_mm2=150.0,
            contact_percentage=25.0,
            adhesion_risk="low",
        )
        mock_report = mock.MagicMock()
        mock_report.bed_adhesion = mock_adhesion
        mock_report.model_height_mm = 30.0
        mock_report.to_dict.return_value = {"score": 85, "grade": "B"}

        mock_rec = AdhesionRecommendation(
            brim_width_mm=0,
            use_raft=False,
            adhesion_risk="low",
            contact_percentage=25.0,
            rationale="No brim needed.",
            slicer_overrides={},
        )

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", return_value=meta),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("kiln.printability.analyze_printability", return_value=mock_report),
            mock.patch("kiln.printability.recommend_adhesion", return_value=mock_rec),
            mock.patch("kiln.printability.is_bedslinger", return_value=False),
        ):
            result = fn(input_path="/tmp/cube.stl", material="PLA")

        assert result["success"] is True
        assert result["printability"] == {"score": 85, "grade": "B"}
        assert result["adhesion"] is not None
        assert "No brim needed." in result["message"]

    def test_slicer_not_found_returns_error(self, estimate_tools_fns):
        """SlicerNotFoundError → SLICER_NOT_FOUND error code."""
        from kiln.slicer import SlicerNotFoundError

        fn = estimate_tools_fns["slice_and_estimate"]
        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch(
                "kiln.slicer.slice_file",
                side_effect=SlicerNotFoundError("not found"),
            ),
        ):
            result = fn(input_path="/tmp/cube.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "SLICER_NOT_FOUND"

    def test_metadata_extraction_failure_yields_none_times(self, estimate_tools_fns):
        """If extract_metadata raises, estimate times are None / 'unknown'."""
        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", side_effect=Exception("bad gcode")),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
        ):
            result = fn(input_path="/tmp/cube.stl")

        assert result["success"] is True
        assert result["estimate"]["estimated_time_seconds"] is None
        assert result["estimate"]["estimated_time_human"] == "unknown"

    def test_non_stl_file_skips_printability(self, estimate_tools_fns):
        """Non-STL/OBJ/3MF extension → printability and adhesion are None."""
        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()
        meta = self._make_meta()

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", return_value=meta),
            mock.patch("os.path.isfile", return_value=True),
        ):
            result = fn(input_path="/tmp/cube.step")

        assert result["success"] is True
        assert result["printability"] is None
        assert result["adhesion"] is None

    def test_material_uppercase_normalised(self, estimate_tools_fns):
        """Material is uppercased in the estimate response."""
        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()
        meta = self._make_meta()

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", return_value=meta),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
        ):
            result = fn(input_path="/tmp/cube.stl", material="petg")

        assert result["estimate"]["material"] == "PETG"

    def test_response_contains_slice_key(self, estimate_tools_fns):
        """Response always includes the 'slice' key from slice_file result."""
        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()
        meta = self._make_meta()

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", return_value=meta),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
        ):
            result = fn(input_path="/tmp/cube.stl")

        assert "slice" in result
        assert result["slice"]["slicer"] == "OrcaSlicer"

    def test_filament_grams_calculation(self, estimate_tools_fns):
        """filament_used_grams = filament_used_mm * 0.003, rounded to 1dp."""
        fn = estimate_tools_fns["slice_and_estimate"]
        slice_result = self._make_slice_result()
        meta = self._make_meta(filament_mm=5000.0)

        with (
            mock.patch("kiln.server._resolve_slice_profile_context", return_value=(None, None)),
            mock.patch("kiln.slicer.slice_file", return_value=slice_result),
            mock.patch("kiln.gcode_metadata.extract_metadata", return_value=meta),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("kiln.printability.analyze_printability", side_effect=Exception("skip")),
        ):
            result = fn(input_path="/tmp/cube.stl")

        assert result["estimate"]["filament_used_grams"] == round(5000.0 * 0.003, 1)
