"""Tests for high-value plugin tools — monitoring, recovery, intelligence.

Covers:
    - Plugin metadata (name, description) for each plugin
    - register() registers all expected tools
    - Tool wiring: valid inputs, invalid inputs, edge cases
    - External dependencies fully mocked (adapters, DB, engines)

Note: material_catalog_tools and material_inventory_tools are tested in
their own dedicated test files (test_material_catalog_tools.py and
test_material_inventory_tools.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch PrinterNotFoundError into kiln.printers so that monitoring_tools
# plugin imports succeed (the plugin does ``from kiln.printers import
# PrinterNotFoundError`` but the class is only exported from kiln.registry).
# ---------------------------------------------------------------------------
import kiln.printers as _printers_pkg

if not hasattr(_printers_pkg, "PrinterNotFoundError"):
    from kiln.registry import PrinterNotFoundError as _PNFE

    _printers_pkg.PrinterNotFoundError = _PNFE  # type: ignore[attr-defined]

# ===================================================================
# Shared fixtures
# ===================================================================


@pytest.fixture()
def mock_mcp():
    """Create a mock MCP server that captures registered tools."""
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    return MockMCP(), tools


# ===================================================================
# Monitoring Tools Plugin
# ===================================================================


@pytest.fixture()
def monitoring_tools(mock_mcp):
    """Register monitoring plugin and return captured tools dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.monitoring_tools import plugin
    plugin.register(mcp)
    return tools


class TestMonitoringPluginMeta:
    """Tests for monitoring plugin identity and registration."""

    def test_plugin_name(self) -> None:
        from kiln.plugins.monitoring_tools import plugin
        assert plugin.name == "monitoring_tools"

    def test_plugin_description(self) -> None:
        from kiln.plugins.monitoring_tools import plugin
        assert "monitoring" in plugin.description.lower()

    def test_registers_all_tools(self, monitoring_tools) -> None:
        expected = {
            "monitor_print_vision",
            "watch_print",
            "watch_print_status",
            "stop_watch_print",
            "start_monitored_print",
            "first_layer_status",
        }
        assert expected == set(monitoring_tools.keys())


class TestMonitorPrintVision:
    """Tests for monitor_print_vision tool."""

    def _make_mock_adapter(self, *, state="printing", completion=50.0, can_snapshot=False):
        """Build a mock adapter with configurable state and job."""
        from kiln.printers import PrinterStatus

        adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus(state)
        mock_state.to_dict.return_value = {"state": state}
        adapter.get_state.return_value = mock_state

        mock_job = MagicMock()
        mock_job.completion = completion
        mock_job.to_dict.return_value = {
            "completion": completion,
            "print_time_seconds": 3600,
            "print_time_left_seconds": 1800,
        }
        adapter.get_job.return_value = mock_job

        mock_caps = MagicMock()
        mock_caps.can_snapshot = can_snapshot
        adapter.capabilities = mock_caps

        return adapter

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_returns_success_with_printing_state(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        adapter = self._make_mock_adapter()
        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["monitor_print_vision"]()
        assert result["success"] is True
        assert result["monitoring_context"]["is_printing"] is True
        assert result["monitoring_context"]["print_phase"] == "mid_print"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_first_layers_phase_detection(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        adapter = self._make_mock_adapter(completion=5.0)
        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["monitor_print_vision"]()
        assert result["monitoring_context"]["print_phase"] == "first_layers"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_final_layers_phase_detection(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        adapter = self._make_mock_adapter(completion=95.0)
        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["monitor_print_vision"]()
        assert result["monitoring_context"]["print_phase"] == "final_layers"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_snapshot_not_available_without_capability(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        adapter = self._make_mock_adapter(can_snapshot=False)
        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["monitor_print_vision"](include_snapshot=True)
        assert result["snapshot"]["available"] is False
        assert result["snapshot"]["reason"] == "no_capability"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_snapshot_not_requested(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        adapter = self._make_mock_adapter()
        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["monitor_print_vision"](include_snapshot=False)
        assert result["snapshot"]["reason"] == "not_requested"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_printer_not_found_returns_error(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        from kiln.printers import PrinterNotFoundError
        with patch("kiln.server._registry") as mock_reg:
            mock_reg.get.side_effect = PrinterNotFoundError("ghost")
            result = monitoring_tools["monitor_print_vision"](printer_name="ghost")
        assert result.get("success") is False or "error" in result

    @patch("kiln.server._check_auth", return_value={"error": "auth failed", "success": False})
    def test_auth_failure_returns_error(self, _mock_auth, monitoring_tools) -> None:
        result = monitoring_tools["monitor_print_vision"]()
        assert result["success"] is False


class TestWatchPrint:
    """Tests for watch_print tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    def test_idle_printer_returns_no_active_print(
        self, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        from kiln.printers import PrinterStatus

        adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.IDLE
        adapter.get_state.return_value = mock_state

        mock_job = MagicMock()
        mock_job.completion = None
        adapter.get_job.return_value = mock_job

        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["watch_print"]()
        assert result["success"] is True
        assert result["outcome"] == "no_active_print"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    def test_printing_returns_watch_id(
        self, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        from kiln.printers import PrinterStatus

        adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.PRINTING
        adapter.get_state.return_value = mock_state

        mock_job = MagicMock()
        mock_job.completion = 10.0
        adapter.get_job.return_value = mock_job

        mock_caps = MagicMock()
        mock_caps.can_snapshot = False
        adapter.capabilities = mock_caps

        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["watch_print"]()

        assert result["success"] is True
        assert "watch_id" in result
        assert result["status"] == "started"

        # Clean up the watcher thread
        wid = result["watch_id"]
        monitoring_tools["stop_watch_print"](wid)


class TestWatchPrintStatus:
    """Tests for watch_print_status tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_unknown_watch_id_returns_error(self, _mock_auth, monitoring_tools) -> None:
        result = monitoring_tools["watch_print_status"]("nonexistent_id")
        assert "error" in result


class TestStopWatchPrint:
    """Tests for stop_watch_print tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_unknown_watch_id_returns_error(self, _mock_auth, monitoring_tools) -> None:
        result = monitoring_tools["stop_watch_print"]("nonexistent_id")
        assert "error" in result


class TestFirstLayerStatus:
    """Tests for first_layer_status tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_unknown_monitor_id_returns_error(self, _mock_auth, monitoring_tools) -> None:
        result = monitoring_tools["first_layer_status"]("nonexistent_id")
        assert "error" in result


class TestDetectPhase:
    """Tests for _detect_phase helper."""

    def test_none_completion(self) -> None:
        from kiln.plugins.monitoring_tools import _detect_phase
        assert _detect_phase(None) == "unknown"

    def test_negative_completion(self) -> None:
        from kiln.plugins.monitoring_tools import _detect_phase
        assert _detect_phase(-1.0) == "unknown"

    def test_first_layers(self) -> None:
        from kiln.plugins.monitoring_tools import _detect_phase
        assert _detect_phase(5.0) == "first_layers"

    def test_mid_print(self) -> None:
        from kiln.plugins.monitoring_tools import _detect_phase
        assert _detect_phase(50.0) == "mid_print"

    def test_final_layers(self) -> None:
        from kiln.plugins.monitoring_tools import _detect_phase
        assert _detect_phase(95.0) == "final_layers"

    def test_boundary_10_is_mid(self) -> None:
        from kiln.plugins.monitoring_tools import _detect_phase
        assert _detect_phase(10.0) == "mid_print"

    def test_boundary_90_is_mid(self) -> None:
        from kiln.plugins.monitoring_tools import _detect_phase
        assert _detect_phase(90.0) == "mid_print"


# ===================================================================
# Recovery Tools Plugin
# ===================================================================


@pytest.fixture()
def recovery_tools(mock_mcp):
    """Register recovery plugin and return captured tools dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.recovery_tools import plugin
    plugin.register(mcp)
    return tools


class TestRecoveryPluginMeta:
    """Tests for recovery plugin identity and registration."""

    def test_plugin_name(self) -> None:
        from kiln.plugins.recovery_tools import plugin
        assert plugin.name == "recovery_tools"

    def test_plugin_description(self) -> None:
        from kiln.plugins.recovery_tools import plugin
        assert "recovery" in plugin.description.lower()

    def test_registers_all_tools(self, recovery_tools) -> None:
        expected = {
            "analyze_print_failure_smart",
            "get_recovery_plan",
            "failure_history",
            "plan_multi_copy_split",
            "plan_assembly_split",
            "split_plan_status",
            "cancel_split_plan",
            "analyze_generation_feedback",
            "improve_generation_prompt",
            "generation_feedback_loop_status",
            "detect_print_failure",
            "plan_failure_recovery",
            "start_print_recovery",
            "confirm_print_recovery",
            "cancel_print_recovery",
            "get_recovery_session_status",
            "get_recovery_gcode_steps",
            "record_recovery_check",
            "complete_print_recovery",
            "get_recovery_statistics",
        }
        assert expected == set(recovery_tools.keys())


class TestAnalyzePrintFailureSmart:
    """Tests for analyze_print_failure_smart tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_returns_analysis(self, _mock_auth, recovery_tools) -> None:
        from kiln.failure_recovery import (
            FailureAnalysis,
            FailureClassification,
            FailureType,
            RecoveryAction,
            RecoveryPlan,
        )

        mock_classification = FailureClassification(
            failure_type=FailureType.SPAGHETTI,
            confidence=0.9,
            evidence=["Filament not adhering"],
            progress_at_failure=0.3,
            time_printing_seconds=1800,
            material_wasted_grams=15.0,
        )
        mock_plan = RecoveryPlan(
            action=RecoveryAction.RESTART,
            steps=["Clear bed", "Restart"],
            automated=False,
            estimated_time_minutes=10,
            risk_level="low",
            settings_adjustments={},
            prevent_recurrence=["Check bed adhesion"],
        )
        mock_analysis = FailureAnalysis(
            classification=mock_classification,
            recovery_plan=mock_plan,
            similar_failures=[],
            printer_health={},
        )

        with patch("kiln.failure_recovery.analyze_failure", return_value=mock_analysis), \
             patch("kiln.failure_recovery.record_failure"):
            result = recovery_tools["analyze_print_failure_smart"](
                progress=0.3,
                error_message="spaghetti detected",
            )

        assert result["success"] is True
        assert "analysis" in result
        assert "spaghetti" in result["message"].lower()

    @patch("kiln.server._check_auth", return_value=None)
    def test_handles_unexpected_error(self, _mock_auth, recovery_tools) -> None:
        with patch("kiln.failure_recovery.analyze_failure", side_effect=RuntimeError("boom")):
            result = recovery_tools["analyze_print_failure_smart"](progress=0.5)
        assert result.get("success") is False or "error" in result


class TestGetRecoveryPlan:
    """Tests for get_recovery_plan tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_valid_failure_type(self, _mock_auth, recovery_tools) -> None:
        result = recovery_tools["get_recovery_plan"](failure_type="spaghetti")
        assert result["success"] is True
        assert "recovery_plan" in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_invalid_failure_type(self, _mock_auth, recovery_tools) -> None:
        result = recovery_tools["get_recovery_plan"](failure_type="nonexistent_xyz")
        assert result["success"] is False
        assert "error" in result


class TestFailureHistory:
    """Tests for failure_history tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_empty_history(self, _mock_auth, recovery_tools) -> None:
        with patch("kiln.failure_recovery.get_failure_history", return_value=[]):
            result = recovery_tools["failure_history"]()
        assert result["success"] is True
        assert result["count"] == 0
        assert result["records"] == []

    @patch("kiln.server._check_auth", return_value=None)
    def test_handles_error(self, _mock_auth, recovery_tools) -> None:
        with patch("kiln.failure_recovery.get_failure_history", side_effect=RuntimeError("db error")):
            result = recovery_tools["failure_history"]()
        assert "error" in result


class TestPlanMultiCopySplit:
    """Tests for plan_multi_copy_split tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_zero_copies_returns_error(self, _mock_auth, recovery_tools) -> None:
        result = recovery_tools["plan_multi_copy_split"](
            file_path="/tmp/test.gcode", copies=0,
        )
        assert "error" in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_valid_split_plan(self, _mock_auth, recovery_tools) -> None:
        mock_plan = MagicMock()
        mock_plan.to_dict.return_value = {"printers": ["p1"], "copies_per_printer": [3]}
        mock_plan.total_printers = 1
        mock_plan.time_savings_percentage = 0.0

        with patch("kiln.job_splitter.plan_multi_copy_split", return_value=mock_plan):
            result = recovery_tools["plan_multi_copy_split"](
                file_path="/tmp/test.gcode", copies=3,
            )
        assert result["success"] is True
        assert "plan" in result


class TestPlanAssemblySplit:
    """Tests for plan_assembly_split tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_empty_file_paths_returns_error(self, _mock_auth, recovery_tools) -> None:
        result = recovery_tools["plan_assembly_split"](file_paths=[])
        assert "error" in result


class TestDetectPrintFailure:
    """Tests for detect_print_failure tool (AI recovery)."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_no_failure_detected(self, _mock_auth, recovery_tools) -> None:
        mock_engine = MagicMock()
        mock_engine.detect_failure.return_value = None

        with patch("kiln.print_recovery.get_recovery_engine", return_value=mock_engine):
            result = recovery_tools["detect_print_failure"](
                printer_name="test",
                telemetry={"hotend_temp": 200, "bed_temp": 60},
            )
        assert result["success"] is True
        assert result["failure_detected"] is False

    @patch("kiln.server._check_auth", return_value=None)
    def test_failure_detected(self, _mock_auth, recovery_tools) -> None:
        mock_report = MagicMock()
        mock_report.to_dict.return_value = {
            "failure_id": "f-123",
            "failure_type": "thermal_runaway",
            "severity": "critical",
        }

        mock_engine = MagicMock()
        mock_engine.detect_failure.return_value = mock_report

        with patch("kiln.print_recovery.get_recovery_engine", return_value=mock_engine):
            result = recovery_tools["detect_print_failure"](
                printer_name="test",
                telemetry={"hotend_temp": 300, "bed_temp": 60},
            )
        assert result["success"] is True
        assert result["failure_detected"] is True
        assert result["failure"]["failure_id"] == "f-123"

    @patch("kiln.server._check_auth", return_value=None)
    def test_validation_error(self, _mock_auth, recovery_tools) -> None:
        mock_engine = MagicMock()
        mock_engine.detect_failure.side_effect = ValueError("bad telemetry")

        with patch("kiln.print_recovery.get_recovery_engine", return_value=mock_engine):
            result = recovery_tools["detect_print_failure"](
                printer_name="test",
                telemetry={},
            )
        assert "error" in result


class TestGetRecoverySessionStatus:
    """Tests for get_recovery_session_status tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_session_not_found(self, _mock_auth, recovery_tools) -> None:
        mock_engine = MagicMock()
        mock_engine.get_session.return_value = None

        with patch("kiln.print_recovery.get_recovery_engine", return_value=mock_engine):
            result = recovery_tools["get_recovery_session_status"](session_id="nonexistent")
        assert "error" in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_session_found(self, _mock_auth, recovery_tools) -> None:
        mock_session = MagicMock()
        mock_session.to_dict.return_value = {
            "session_id": "s-1",
            "status": "executing",
        }

        mock_engine = MagicMock()
        mock_engine.get_session.return_value = mock_session

        with patch("kiln.print_recovery.get_recovery_engine", return_value=mock_engine):
            result = recovery_tools["get_recovery_session_status"](session_id="s-1")
        assert result["success"] is True
        assert result["session"]["session_id"] == "s-1"


class TestGetRecoveryStatistics:
    """Tests for get_recovery_statistics tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_returns_statistics(self, _mock_auth, recovery_tools) -> None:
        mock_stats = {"total_recoveries": 5, "success_rate": 0.8}
        mock_engine = MagicMock()
        mock_engine.get_recovery_statistics.return_value = mock_stats

        with patch("kiln.print_recovery.get_recovery_engine", return_value=mock_engine):
            result = recovery_tools["get_recovery_statistics"]()
        assert result["success"] is True
        assert result["statistics"]["total_recoveries"] == 5


class TestCompletePrintRecoveryProOutcome:
    """Regression tests for the kiln-pro outcome-recording side path.

    The path from ``complete_print_recovery`` into
    ``kiln_pro.recovery.outcome_learning.record_outcome`` is wrapped in a
    broad ``except Exception`` so any AttributeError or ImportError is
    silently swallowed.  That swallowing previously masked a bug where
    the plugin referenced ``session.failure_report`` (no such attribute)
    instead of ``session.failure``, resulting in *every* recovery
    completion silently failing to record an outcome.

    These tests use a REAL :class:`RecoverySession` (not a MagicMock) so
    attribute typos surface as AttributeError.  A MagicMock would
    auto-generate the bogus attribute and the bug would survive.
    """

    @staticmethod
    def _build_session_with_failure():
        """Build a real RecoverySession ready for completion."""
        from kiln.print_recovery import (
            FailureReport,
            FailureType,
            RecoveryConfidence,
            RecoveryPlan,
            RecoverySession,
            RecoveryStatus,
            RecoveryStrategy,
        )

        failure = FailureReport(
            failure_id="f-test",
            failure_type=FailureType.LAYER_SHIFT,
            detected_at="2026-04-26T00:00:00+00:00",
            printer_name="bambu-a1",
            material_type="pla",
            severity="high",
        )
        plan = RecoveryPlan(
            plan_id="p-test",
            failure_id="f-test",
            strategy=RecoveryStrategy.RESUME_FROM_LAYER,
            confidence=RecoveryConfidence.MEDIUM,
            requires_confirmation=False,
        )
        session = RecoverySession(
            session_id="s-test",
            plan=plan,
            failure=failure,
            status=RecoveryStatus.MONITORING,
            started_at="2026-04-26T00:00:00+00:00",
            monitoring_required=3,
            monitoring_passed=3,
            monitoring_checks=3,
        )
        return session

    @patch("kiln.server._check_auth", return_value=None)
    def test_pro_outcome_recorded_with_correct_args(
        self,
        _mock_auth,
        recovery_tools,
    ) -> None:
        """Pro outcome learning must receive failure_type + strategy + printer.

        Regression for the ``session.failure_report`` typo.  With the
        bug, the AttributeError is swallowed and ``record_outcome`` is
        never called, so the assertion below would fire on the call
        count.
        """
        session = self._build_session_with_failure()

        # The MCP tool calls engine.complete_recovery and uses the
        # returned session for the kiln-pro path.  Stub the engine to
        # return our real session.
        mock_engine = MagicMock()
        mock_engine.complete_recovery.return_value = session

        # Build a fake pro_features that looks installed.
        fake_pro_features = MagicMock()
        fake_pro_features.recovery = MagicMock()  # truthy

        recorded: dict = {}

        def _capture_record_outcome(**kwargs):
            recorded.update(kwargs)
            return {"recorded_at": 0.0, "strategy": kwargs["strategy"]}

        with (
            patch(
                "kiln.print_recovery.get_recovery_engine",
                return_value=mock_engine,
            ),
            patch.dict(
                "sys.modules",
                {
                    "kiln_pro": MagicMock(),
                    "kiln_pro.bridge": MagicMock(pro_features=fake_pro_features),
                    "kiln_pro.recovery": MagicMock(),
                    "kiln_pro.recovery.outcome_learning": MagicMock(
                        record_outcome=_capture_record_outcome,
                    ),
                },
            ),
        ):
            result = recovery_tools["complete_print_recovery"](
                session_id="s-test",
                success=True,
                notes="finished",
            )

        assert result["success"] is True
        # The bug under regression: with session.failure_report (typo),
        # AttributeError is swallowed and record_outcome is NEVER called,
        # so `recorded` stays empty and `pro_outcome_recorded` is absent.
        assert recorded, (
            "kiln-pro record_outcome was never called — the recovery "
            "outcome silently failed to record (regression for "
            "session.failure_report typo at recovery_tools.py:920)"
        )
        assert recorded["failure_type"] == "layer_shift"
        assert recorded["strategy"] == "resume_from_layer"
        assert recorded["printer_name"] == "bambu-a1"
        assert recorded["material_type"] == "pla"
        assert recorded["session_id"] == "s-test"
        assert recorded["success"] is True
        assert result.get("pro_outcome_recorded") is True

    @patch("kiln.server._check_auth", return_value=None)
    def test_pro_outcome_skipped_when_kiln_pro_not_installed(
        self,
        _mock_auth,
        recovery_tools,
    ) -> None:
        """Free tier path: ImportError is swallowed, no outcome recorded."""
        session = self._build_session_with_failure()

        mock_engine = MagicMock()
        mock_engine.complete_recovery.return_value = session

        # Make every kiln_pro.* import raise ImportError.
        import builtins
        real_import = builtins.__import__

        def _blocking_import(name, *args, **kwargs):
            if name.startswith("kiln_pro"):
                raise ImportError(f"blocked for test: {name}")
            return real_import(name, *args, **kwargs)

        with (
            patch(
                "kiln.print_recovery.get_recovery_engine",
                return_value=mock_engine,
            ),
            patch("builtins.__import__", side_effect=_blocking_import),
        ):
            result = recovery_tools["complete_print_recovery"](
                session_id="s-test",
                success=True,
            )

        assert result["success"] is True
        assert "pro_outcome_recorded" not in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_reroute_recommendation_attached_on_failure(
        self,
        _mock_auth,
        recovery_tools,
    ) -> None:
        """Failed recovery + supplied fleet -> reroute_recommendation in response.

        Covers the wiring from complete_print_recovery into the kiln-pro
        rerouter.  Verifies the rerouter is consulted ONLY on failure
        AND only when alternatives are supplied.
        """
        from kiln.print_recovery import RecoveryStatus

        session = self._build_session_with_failure()
        session.status = RecoveryStatus.FAILED

        mock_engine = MagicMock()
        mock_engine.complete_recovery.return_value = session

        # Stub a rerouter whose evaluate_reroute returns an approved
        # decision pointing at "voron-1".
        mock_decision = MagicMock()
        mock_decision.to_dict.return_value = {
            "should_reroute": True,
            "target_printer_id": "voron-1",
            "blocked_by_rule": None,
            "reason": "reroute approved -> 'voron-1'",
        }
        mock_rerouter = MagicMock()
        mock_rerouter.evaluate_reroute.return_value = mock_decision

        fake_pro_features = MagicMock()
        fake_pro_features.recovery = MagicMock()  # truthy

        with (
            patch(
                "kiln.print_recovery.get_recovery_engine",
                return_value=mock_engine,
            ),
            patch.dict(
                "sys.modules",
                {
                    "kiln_pro": MagicMock(),
                    "kiln_pro.bridge": MagicMock(pro_features=fake_pro_features),
                    "kiln_pro.recovery": MagicMock(),
                    "kiln_pro.recovery.outcome_learning": MagicMock(
                        record_outcome=lambda **kw: {"recorded_at": 0.0},
                    ),
                    "kiln_pro.recovery.failure_rerouter": MagicMock(
                        get_rerouter=lambda: mock_rerouter,
                    ),
                },
            ),
        ):
            result = recovery_tools["complete_print_recovery"](
                session_id="s-test",
                success=False,
                alternative_printers=[
                    {"printer_id": "voron-1", "is_idle": True},
                ],
                completion_pct_at_failure=0.45,
            )

        assert result["success"] is True
        assert "reroute_recommendation" in result
        assert result["reroute_recommendation"]["should_reroute"] is True
        assert result["reroute_recommendation"]["target_printer_id"] == "voron-1"
        # Confirm the rerouter was actually called with the failure's
        # printer + failure_type, not stale strings from the caller.
        mock_rerouter.evaluate_reroute.assert_called_once()
        call_kwargs = mock_rerouter.evaluate_reroute.call_args.kwargs
        assert call_kwargs["original_printer_id"] == "bambu-a1"
        assert call_kwargs["failure_type"] == "layer_shift"
        assert call_kwargs["completion_pct"] == 0.45
        assert call_kwargs["material"] == "pla"

    @patch("kiln.server._check_auth", return_value=None)
    def test_reroute_recommendation_skipped_on_success(
        self,
        _mock_auth,
        recovery_tools,
    ) -> None:
        """Successful recovery does NOT consult the rerouter."""
        session = self._build_session_with_failure()
        # already MONITORING with passed checks — complete_recovery
        # would mark it COMPLETED.

        mock_engine = MagicMock()
        mock_engine.complete_recovery.return_value = session

        mock_rerouter = MagicMock()
        fake_pro_features = MagicMock()
        fake_pro_features.recovery = MagicMock()

        with (
            patch(
                "kiln.print_recovery.get_recovery_engine",
                return_value=mock_engine,
            ),
            patch.dict(
                "sys.modules",
                {
                    "kiln_pro": MagicMock(),
                    "kiln_pro.bridge": MagicMock(pro_features=fake_pro_features),
                    "kiln_pro.recovery": MagicMock(),
                    "kiln_pro.recovery.outcome_learning": MagicMock(
                        record_outcome=lambda **kw: {"recorded_at": 0.0},
                    ),
                    "kiln_pro.recovery.failure_rerouter": MagicMock(
                        get_rerouter=lambda: mock_rerouter,
                    ),
                },
            ),
        ):
            result = recovery_tools["complete_print_recovery"](
                session_id="s-test",
                success=True,
                alternative_printers=[
                    {"printer_id": "voron-1", "is_idle": True},
                ],
            )

        assert result["success"] is True
        assert "reroute_recommendation" not in result
        mock_rerouter.evaluate_reroute.assert_not_called()


class TestImproveGenerationPromptSanityGate:
    """Tests for the KILN-010 claim 51 sanity gate behavior.

    Before this gate landed, ``improve_generation_prompt`` happily
    returned contradictory / over-budget / intent-drifted prompts with
    only a logger warning.  The default contract is now: refuse with
    ``code="SANITY_GATE_FAILED"`` unless the caller explicitly opts
    out via ``enforce_sanity=False``.
    """

    @staticmethod
    def _build_failed_sanity_result():
        from kiln.generation_feedback import (
            SanityFailure,
            SanityFailureKind,
            SanityResult,
        )

        return SanityResult(
            passed=False,
            failures=[
                SanityFailure(
                    kind=SanityFailureKind.CONTRADICTION,
                    message="rigid + flexible material conflict",
                    detail={"shape": "material_family"},
                ),
            ],
            token_overlap_pct=0.85,
            length=400,
            budget=600,
        )

    @staticmethod
    def _build_passed_sanity_result():
        from kiln.generation_feedback import SanityResult

        return SanityResult(
            passed=True,
            failures=[],
            token_overlap_pct=0.92,
            length=300,
            budget=600,
        )

    def _build_improved_prompt(self, sanity):
        from kiln.generation_feedback import ImprovedPrompt

        return ImprovedPrompt(
            original_prompt="phone stand",
            improved_prompt="phone stand. Requirements: minimum wall 2mm.",
            feedback_applied=[],
            constraints_added=["minimum wall thickness 2mm"],
            iteration=1,
            expected_improvements=[],
            sanity=sanity,
        )

    @patch("kiln.server._check_auth", return_value=None)
    def test_default_refuses_when_sanity_fails(
        self,
        _mock_auth,
        recovery_tools,
    ) -> None:
        """Default enforce_sanity=True -> refusal on failed gate."""
        improved = self._build_improved_prompt(self._build_failed_sanity_result())

        with (
            patch(
                "kiln.generation_feedback.analyze_for_feedback",
                return_value=[],
            ),
            patch(
                "kiln.generation_feedback.generate_improved_prompt",
                return_value=improved,
            ),
        ):
            result = recovery_tools["improve_generation_prompt"](
                original_prompt="phone stand",
            )

        assert result["success"] is False
        assert result["error"]["code"] == "SANITY_GATE_FAILED"
        assert "sanity" in result
        # The would-be prompt is still returned for inspection / repair.
        assert "improved_prompt" in result
        assert result["sanity"]["passed"] is False

    @patch("kiln.server._check_auth", return_value=None)
    def test_passes_through_when_sanity_passes(
        self,
        _mock_auth,
        recovery_tools,
    ) -> None:
        """Healthy prompt always returns success, regardless of enforce_sanity."""
        improved = self._build_improved_prompt(self._build_passed_sanity_result())

        with (
            patch(
                "kiln.generation_feedback.analyze_for_feedback",
                return_value=[],
            ),
            patch(
                "kiln.generation_feedback.generate_improved_prompt",
                return_value=improved,
            ),
        ):
            result = recovery_tools["improve_generation_prompt"](
                original_prompt="phone stand",
            )

        assert result["success"] is True
        assert "improved_prompt" in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_enforce_sanity_false_returns_failed_prompt(
        self,
        _mock_auth,
        recovery_tools,
    ) -> None:
        """Opt-out path: failed sanity returns the prompt for repair."""
        improved = self._build_improved_prompt(self._build_failed_sanity_result())

        with (
            patch(
                "kiln.generation_feedback.analyze_for_feedback",
                return_value=[],
            ),
            patch(
                "kiln.generation_feedback.generate_improved_prompt",
                return_value=improved,
            ),
        ):
            result = recovery_tools["improve_generation_prompt"](
                original_prompt="phone stand",
                enforce_sanity=False,
            )

        assert result["success"] is True
        assert result["improved_prompt"]["sanity"]["passed"] is False


class TestGenerationFeedbackLoopStatus:
    """Tests for generation_feedback_loop_status tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_loop_not_found(self, _mock_auth, recovery_tools) -> None:
        with patch("kiln.generation_feedback.get_feedback_loop", return_value=None):
            result = recovery_tools["generation_feedback_loop_status"](model_id="m-999")
        assert "error" in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_loop_found(self, _mock_auth, recovery_tools) -> None:
        mock_loop = MagicMock()
        mock_loop.to_dict.return_value = {"model_id": "m-1", "iteration": 2}
        mock_loop.current_iteration = 2
        mock_loop.resolved = False

        with patch("kiln.generation_feedback.get_feedback_loop", return_value=mock_loop):
            result = recovery_tools["generation_feedback_loop_status"](model_id="m-1")
        assert result["success"] is True
        assert result["feedback_loop"]["model_id"] == "m-1"


# ===================================================================
# Intelligence Tools Plugin
# ===================================================================


@pytest.fixture()
def intelligence_tools(mock_mcp):
    """Register intelligence plugin and return captured tools dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.intelligence_tools import plugin
    plugin.register(mcp)
    return tools


class TestIntelligencePluginMeta:
    """Tests for intelligence plugin identity and registration."""

    def test_plugin_name(self) -> None:
        from kiln.plugins.intelligence_tools import plugin
        assert plugin.name == "intelligence_tools"

    def test_plugin_description(self) -> None:
        from kiln.plugins.intelligence_tools import plugin
        assert "intelligence" in plugin.description.lower() or "dna" in plugin.description.lower()

    def test_registers_all_tools(self, intelligence_tools) -> None:
        expected = {
            "fingerprint_model",
            "record_print_dna",
            "predict_print_settings",
            "find_similar_prints",
            "get_model_print_history",
            "contribute_community_print",
            "get_community_insight",
            "community_stats",
            "recommend_material",
            "list_available_materials",
        }
        assert expected == set(intelligence_tools.keys())


class TestFingerprintModel:
    """Tests for fingerprint_model tool."""

    def test_file_not_found(self, intelligence_tools) -> None:
        with patch("kiln.print_dna.fingerprint_model", side_effect=FileNotFoundError("missing")):
            result = intelligence_tools["fingerprint_model"]("/nonexistent/model.stl")
        assert "error" in result

    def test_invalid_file(self, intelligence_tools) -> None:
        with patch("kiln.print_dna.fingerprint_model", side_effect=ValueError("not an STL")):
            result = intelligence_tools["fingerprint_model"]("/tmp/bad.txt")
        assert "error" in result

    def test_valid_fingerprint(self, intelligence_tools) -> None:
        mock_fp = MagicMock()
        mock_fp.to_dict.return_value = {
            "file_hash": "abc123",
            "triangle_count": 1000,
            "geometric_signature": "sig123",
        }

        with patch("kiln.print_dna.fingerprint_model", return_value=mock_fp):
            result = intelligence_tools["fingerprint_model"]("/tmp/model.stl")
        assert result["success"] is True
        assert result["fingerprint"]["file_hash"] == "abc123"


class TestRecordPrintDNA:
    """Tests for record_print_dna tool."""

    def test_valid_record(self, intelligence_tools) -> None:
        with patch("kiln.print_dna.record_print_dna"):
            result = intelligence_tools["record_print_dna"](
                file_hash="abc123",
                geometric_signature="sig123",
                triangle_count=1000,
                surface_area_mm2=500.0,
                volume_mm3=100.0,
                overhang_ratio=0.1,
                complexity_score=0.5,
                printer_model="Bambu A1",
                material="PLA",
                settings={"layer_height": 0.2},
                outcome="success",
            )
        assert result["success"] is True
        assert result["file_hash"] == "abc123"
        assert result["outcome"] == "success"

    def test_invalid_outcome(self, intelligence_tools) -> None:
        with patch("kiln.print_dna.record_print_dna", side_effect=ValueError("invalid outcome")):
            result = intelligence_tools["record_print_dna"](
                file_hash="abc",
                geometric_signature="sig",
                triangle_count=0,
                surface_area_mm2=0.0,
                volume_mm3=0.0,
                overhang_ratio=0.0,
                complexity_score=0.0,
                printer_model="test",
                material="PLA",
                settings={},
                outcome="invalid",
            )
        assert "error" in result


class TestPredictPrintSettings:
    """Tests for predict_print_settings tool."""

    def test_valid_prediction(self, intelligence_tools) -> None:
        mock_pred = MagicMock()
        mock_pred.to_dict.return_value = {
            "settings": {"layer_height": 0.2},
            "source": "exact_match",
            "confidence": 0.95,
        }

        with patch("kiln.print_dna.predict_settings", return_value=mock_pred):
            result = intelligence_tools["predict_print_settings"](
                file_hash="abc123",
                geometric_signature="sig123",
                surface_area_mm2=500.0,
                volume_mm3=100.0,
                complexity_score=0.5,
                printer_model="Bambu A1",
                material="PLA",
            )
        assert result["success"] is True
        assert result["prediction"]["source"] == "exact_match"

    def test_handles_error(self, intelligence_tools) -> None:
        with patch("kiln.print_dna.predict_settings", side_effect=RuntimeError("no data")):
            result = intelligence_tools["predict_print_settings"](
                file_hash="abc",
                geometric_signature="sig",
                surface_area_mm2=0.0,
                volume_mm3=0.0,
                complexity_score=0.0,
                printer_model="test",
                material="PLA",
            )
        assert "error" in result


class TestFindSimilarPrints:
    """Tests for find_similar_prints tool."""

    def test_no_similar_models(self, intelligence_tools) -> None:
        with patch("kiln.print_dna.find_similar_models", return_value=[]):
            result = intelligence_tools["find_similar_prints"](
                file_hash="abc", geometric_signature="sig",
            )
        assert result["success"] is True
        assert result["count"] == 0
        assert result["similar_models"] == []

    def test_with_similar_models(self, intelligence_tools) -> None:
        mock_record = MagicMock()
        mock_record.to_dict.return_value = {"file_hash": "xyz", "similarity": 0.92}

        with patch("kiln.print_dna.find_similar_models", return_value=[mock_record]):
            result = intelligence_tools["find_similar_prints"](
                file_hash="abc", geometric_signature="sig",
            )
        assert result["success"] is True
        assert result["count"] == 1


class TestGetModelPrintHistory:
    """Tests for get_model_print_history tool."""

    def test_no_history(self, intelligence_tools) -> None:
        mock_rate = {
            "total_prints": 0,
            "success_rate": 0.0,
            "outcomes": {},
            "grade_distribution": {},
        }
        with patch("kiln.print_dna.get_model_history", return_value=[]), \
             patch("kiln.print_dna.get_success_rate", return_value=mock_rate):
            result = intelligence_tools["get_model_print_history"](file_hash="abc")
        assert result["success"] is True
        assert result["total_prints"] == 0
        assert result["history"] == []


class TestContributeCommunityPrint:
    """Tests for contribute_community_print tool."""

    def test_valid_contribution(self, intelligence_tools) -> None:
        with patch("kiln.community_registry.contribute_print"):
            result = intelligence_tools["contribute_community_print"](
                geometric_signature="sig123",
                printer_model="Bambu A1",
                material="PLA",
                settings={"layer_height": 0.2},
                outcome="success",
            )
        assert result["success"] is True
        assert result["geometric_signature"] == "sig123"
        assert result["outcome"] == "success"

    def test_invalid_outcome(self, intelligence_tools) -> None:
        with patch(
            "kiln.community_registry.contribute_print",
            side_effect=ValueError("invalid outcome"),
        ):
            result = intelligence_tools["contribute_community_print"](
                geometric_signature="sig",
                printer_model="test",
                material="PLA",
                settings={},
                outcome="invalid",
            )
        assert "error" in result


class TestGetCommunityInsight:
    """Tests for get_community_insight tool."""

    def test_no_data(self, intelligence_tools) -> None:
        with patch("kiln.community_registry.get_community_insight", return_value=None):
            result = intelligence_tools["get_community_insight"](geometric_signature="sig123")
        assert result["success"] is True
        assert result["has_data"] is False

    def test_with_data(self, intelligence_tools) -> None:
        mock_insight = MagicMock()
        mock_insight.to_dict.return_value = {
            "success_rate": 0.85,
            "total_prints": 10,
        }

        with patch("kiln.community_registry.get_community_insight", return_value=mock_insight):
            result = intelligence_tools["get_community_insight"](geometric_signature="sig123")
        assert result["success"] is True
        assert result["has_data"] is True
        assert result["insight"]["success_rate"] == 0.85


class TestCommunityStats:
    """Tests for community_stats tool."""

    def test_returns_stats(self, intelligence_tools) -> None:
        mock_stats = MagicMock()
        mock_stats.to_dict.return_value = {
            "total_records": 100,
            "unique_models": 50,
        }

        with patch("kiln.community_registry.get_community_stats", return_value=mock_stats):
            result = intelligence_tools["community_stats"]()
        assert result["success"] is True
        assert result["stats"]["total_records"] == 100


class TestRecommendMaterial:
    """Tests for recommend_material tool."""

    def test_valid_recommendation(self, intelligence_tools) -> None:
        mock_rec = MagicMock()
        mock_rec.to_dict.return_value = {
            "material": "PLA",
            "score": 95.0,
            "reasoning": "Easy to print",
        }

        with patch("kiln.material_routing.recommend_material", return_value=mock_rec):
            result = intelligence_tools["recommend_material"](intent="strong and easy")
        assert result["success"] is True
        assert result["recommendation"]["score"] == 95.0

    def test_handles_error(self, intelligence_tools) -> None:
        with patch("kiln.material_routing.recommend_material", side_effect=RuntimeError("no match")):
            result = intelligence_tools["recommend_material"](intent="exotic material")
        assert "error" in result

    def test_printer_id_forwarded_and_named_in_response(
        self, intelligence_tools
    ) -> None:
        """Explicit printer_id reaches the engine AND the response names
        the machine the answer was computed for — a mixed fleet needs to
        know which printer the recommendation targets."""
        mock_rec = MagicMock()
        mock_rec.to_dict.return_value = {"material": "PETG-CF"}

        with patch(
            "kiln.material_routing.recommend_material", return_value=mock_rec
        ) as mock_engine:
            result = intelligence_tools["recommend_material"](
                intent="strong", printer_id="a1-combo"
            )
        assert result["success"] is True
        assert result["answered_for_printer"] == "a1-combo"
        assert mock_engine.call_args.kwargs["printer_id"] == "a1-combo"

    def test_no_printer_id_means_no_machine_claim(
        self, intelligence_tools
    ) -> None:
        """Printer-agnostic answers must not claim a machine."""
        mock_rec = MagicMock()
        mock_rec.to_dict.return_value = {"material": "PLA"}

        with patch("kiln.material_routing.recommend_material", return_value=mock_rec):
            result = intelligence_tools["recommend_material"](intent="easy")
        assert result["success"] is True
        assert "answered_for_printer" not in result

    def test_default_path_passes_no_inventory_and_claims_no_scope(
        self, intelligence_tools
    ) -> None:
        """Without on_hand_only the engine sees on_hand=None and the
        response makes no on-hand claim — catalog behavior unchanged."""
        mock_rec = MagicMock()
        mock_rec.to_dict.return_value = {"material": "PLA"}

        with patch(
            "kiln.material_routing.recommend_material", return_value=mock_rec
        ) as mock_engine:
            result = intelligence_tools["recommend_material"](intent="easy")
        assert result["success"] is True
        assert "on_hand_scope" not in result
        assert mock_engine.call_args.kwargs["on_hand"] is None


class TestRecommendMaterialOnHand:
    """On-hand scope through the real door: seeded DB, real engine.

    The mixed-fleet contract: two printers with different loads must
    produce an answer attributed to the RIGHT machine, and scoping to
    one printer must hide the other machine's loads.
    """

    @pytest.fixture()
    def seeded_db(self, tmp_path):
        from kiln.persistence import KilnDB

        db = KilnDB(db_path=str(tmp_path / "onhand.db"))
        yield db
        db.close()

    def test_mixed_fleet_names_the_right_machine(
        self, intelligence_tools, seeded_db
    ) -> None:
        seeded_db.save_material(
            "a1-left", 0, "PETG", color="black", remaining_grams=800.0
        )
        seeded_db.save_material(
            "a1-right", 0, "PLA", color="white", remaining_grams=900.0
        )

        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True
            )

        assert result["success"] is True
        assert result["on_hand_scope"] == "fleet"
        rec = result["recommendation"]
        assert rec["material"]["name"] == "petg"
        availability = rec["availability"]
        assert availability["status"] == "loaded"
        printers = [r["printer_name"] for r in availability["loaded_on"]]
        assert printers == ["a1-left"]

    def test_printer_id_scopes_to_that_machine(
        self, intelligence_tools, seeded_db
    ) -> None:
        """The other machine's PETG must not leak into a scoped answer."""
        seeded_db.save_material(
            "a1-left", 0, "PETG", color="black", remaining_grams=800.0
        )
        seeded_db.save_material(
            "a1-right", 0, "PLA", color="white", remaining_grams=900.0
        )

        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True, printer_id="a1-right"
            )

        assert result["success"] is True
        assert result["on_hand_scope"] == "printer:a1-right"
        assert result["answered_for_printer"] == "a1-right"
        rec = result["recommendation"]
        assert rec["material"]["name"] == "pla"
        printers = [
            r["printer_name"] for r in rec["availability"]["loaded_on"]
        ]
        assert printers == ["a1-right"]

    def test_shelf_spool_counts_for_scoped_machine(
        self, intelligence_tools, seeded_db
    ) -> None:
        """A shelved spool is reachable via a swap, so it counts even when
        scoped to a machine that has nothing loaded."""
        seeded_db.save_spool({
            "id": "sp-petg",
            "material_type": "PETG",
            "color": "black",
            "brand": "Polymaker",
            "weight_grams": 1000.0,
            "remaining_grams": 600.0,
        })

        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True, printer_id="a1-left"
            )

        rec = result["recommendation"]
        assert rec["material"]["name"] == "petg"
        assert rec["availability"]["status"] == "on_shelf"
        assert rec["availability"]["swap_needed"] is True

    def test_nothing_viable_labeled_needs_purchase(
        self, intelligence_tools, seeded_db
    ) -> None:
        """No silent widening: an unmatched inventory yields a catalog
        answer explicitly labeled as a purchase."""
        seeded_db.save_material(
            "ender3", 0, "PVA", color="natural", remaining_grams=400.0
        )

        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True
            )

        assert result["success"] is True
        rec = result["recommendation"]
        assert rec["availability"]["status"] == "needs_purchase"
        assert rec["availability"]["on_hand_recorded"] == ["PVA"]
        assert "NOT ON HAND" in rec["reasoning"]
        assert rec["material"]["name"]  # catalog pick still present

    @staticmethod
    def _fleet_of(monkeypatch, machines: int, cap: int | None = 1):
        """Fake a multi-machine install with a controllable tier cap."""
        import sys
        import types

        import kiln.registry as registry_mod

        class _Reg:
            count = machines

        monkeypatch.setattr(registry_mod, "get_registry", lambda: _Reg())
        lic = sys.modules.get("kiln.licensing")
        if lic is None:
            lic = types.ModuleType("kiln.licensing")
            monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
        monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(
            lic, "max_printers_for_tier", lambda _t: cap, raising=False
        )

    def test_fleet_wide_sweep_needs_business(
        self, intelligence_tools, seeded_db, monkeypatch
    ) -> None:
        """Sweeping every machine at once is the same cross-machine answer
        the fleet inventory tools sell — so it takes the same gate."""
        seeded_db.save_material("a1-left", 0, "PETG", remaining_grams=800.0)
        self._fleet_of(monkeypatch, machines=3, cap=1)

        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True
            )
        assert result["success"] is False
        assert result["code"] == "TIER_FLEET_SCOPE"
        assert "printer_id" in result["upgrade_hint"]

    def test_one_machine_scope_stays_free_on_a_fleet(
        self, intelligence_tools, seeded_db, monkeypatch
    ) -> None:
        """Asking about ONE machine is single-machine awareness — free at
        every tier, even when the install has many printers."""
        seeded_db.save_material("a1-left", 0, "PETG", remaining_grams=800.0)
        self._fleet_of(monkeypatch, machines=3, cap=1)

        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True, printer_id="a1-left"
            )
        assert result["success"] is True
        assert result["recommendation"]["material"]["name"] == "petg"

    def test_single_printer_install_sweeps_free(
        self, intelligence_tools, seeded_db, monkeypatch
    ) -> None:
        """A one-printer install is never a fleet: the unscoped sweep is
        just 'my printer and my shelf'."""
        seeded_db.save_material("only-one", 0, "PETG", remaining_grams=800.0)
        self._fleet_of(monkeypatch, machines=1, cap=1)

        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True
            )
        assert result["success"] is True
        assert result["on_hand_scope"] == "fleet"

    def test_catalog_mode_is_never_fleet_gated(
        self, intelligence_tools, monkeypatch
    ) -> None:
        """Plain catalog advice touches no machine state — a fleet gate
        must never reach it."""
        self._fleet_of(monkeypatch, machines=9, cap=1)
        result = intelligence_tools["recommend_material"](intent="strong")
        assert result["success"] is True

    def test_empty_inventory_is_honest(
        self, intelligence_tools, seeded_db
    ) -> None:
        with patch("kiln.persistence.get_db", return_value=seeded_db):
            result = intelligence_tools["recommend_material"](
                intent="strong", on_hand_only=True
            )

        rec = result["recommendation"]
        assert rec["availability"]["status"] == "no_inventory_recorded"
        assert "add_spool" in rec["reasoning"]


class TestListAvailableMaterials:
    """Tests for list_available_materials tool."""

    def test_returns_materials(self, intelligence_tools) -> None:
        mock_mat = MagicMock()
        mock_mat.to_dict.return_value = {"name": "PLA", "strength": 6}

        with patch("kiln.material_routing.list_materials", return_value=[mock_mat, mock_mat]):
            result = intelligence_tools["list_available_materials"]()
        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["materials"]) == 2
