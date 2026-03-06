"""Tests for pre-validated print pipelines — quick_print, calibrate, benchmark.

Covers:
    - PipelineStep dataclass and to_dict()
    - PipelineResult dataclass and to_dict() with steps
    - list_pipelines() returns 3 pipelines with correct names
    - quick_print with missing model — fails at slice step
    - benchmark with no model_path — returns early with error
    - Pipeline result serialization — all fields present
    - PipelineStep with empty data — data field omitted from dict

All tests mock external dependencies (slicer, registry, adapters) so no
real printers or slicer binaries are needed.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from kiln.pipelines import (
    PipelineStep,
    PipelineResult,
    list_pipelines,
    quick_print,
    reslice_and_print,
    benchmark,
    PIPELINES,
)


# ===================================================================
# PipelineStep dataclass
# ===================================================================

class TestPipelineStep:
    """Tests for PipelineStep dataclass and to_dict()."""

    def test_basic_fields(self) -> None:
        step = PipelineStep(name="slice", success=True, message="OK")
        assert step.name == "slice"
        assert step.success is True
        assert step.message == "OK"

    def test_default_data_is_empty_dict(self) -> None:
        step = PipelineStep(name="test", success=True)
        assert step.data == {}

    def test_default_duration_is_zero(self) -> None:
        step = PipelineStep(name="test", success=True)
        assert step.duration_seconds == 0.0

    def test_to_dict_with_data(self) -> None:
        step = PipelineStep(
            name="slice",
            success=True,
            message="Sliced OK",
            data={"output_path": "/tmp/out.gcode"},
            duration_seconds=1.234,
        )
        d = step.to_dict()
        assert d["name"] == "slice"
        assert d["success"] is True
        assert d["message"] == "Sliced OK"
        assert d["duration_seconds"] == 1.23  # rounded to 2 decimal places
        assert d["data"] == {"output_path": "/tmp/out.gcode"}

    def test_to_dict_empty_data_omitted(self) -> None:
        """When data is empty dict, it should NOT appear in to_dict() output."""
        step = PipelineStep(name="test", success=True, message="OK")
        d = step.to_dict()
        assert "data" not in d

    def test_to_dict_is_json_serializable(self) -> None:
        step = PipelineStep(
            name="slice",
            success=True,
            message="OK",
            data={"key": "value"},
            duration_seconds=0.5,
        )
        serialized = json.dumps(step.to_dict())
        assert isinstance(serialized, str)

    def test_to_dict_duration_rounding(self) -> None:
        step = PipelineStep(name="test", success=True, duration_seconds=1.999)
        d = step.to_dict()
        assert d["duration_seconds"] == 2.0

    def test_mutable_step(self) -> None:
        """PipelineStep is a regular (mutable) dataclass."""
        step = PipelineStep(name="test", success=False)
        step.success = True
        assert step.success is True


# ===================================================================
# PipelineResult dataclass
# ===================================================================

class TestPipelineResult:
    """Tests for PipelineResult dataclass and to_dict()."""

    def test_basic_fields(self) -> None:
        result = PipelineResult(pipeline="quick_print", success=True, message="Done")
        assert result.pipeline == "quick_print"
        assert result.success is True
        assert result.message == "Done"

    def test_default_steps_is_empty(self) -> None:
        result = PipelineResult(pipeline="test", success=True)
        assert result.steps == []

    def test_default_job_id_is_none(self) -> None:
        result = PipelineResult(pipeline="test", success=True)
        assert result.job_id is None

    def test_to_dict_includes_all_fields(self) -> None:
        step = PipelineStep(name="slice", success=True, message="Sliced")
        result = PipelineResult(
            pipeline="quick_print",
            success=True,
            message="Print started",
            steps=[step],
            job_id="job-123",
            total_duration_seconds=5.678,
        )
        d = result.to_dict()
        assert d["pipeline"] == "quick_print"
        assert d["success"] is True
        assert d["message"] == "Print started"
        assert d["job_id"] == "job-123"
        assert d["total_duration_seconds"] == 5.68
        assert len(d["steps"]) == 1
        assert d["steps"][0]["name"] == "slice"

    def test_to_dict_with_no_steps(self) -> None:
        result = PipelineResult(pipeline="test", success=False, message="Failed")
        d = result.to_dict()
        assert d["steps"] == []
        assert d["job_id"] is None

    def test_to_dict_is_json_serializable(self) -> None:
        result = PipelineResult(
            pipeline="benchmark",
            success=True,
            message="OK",
            steps=[PipelineStep(name="s1", success=True)],
        )
        serialized = json.dumps(result.to_dict())
        assert isinstance(serialized, str)

    def test_multiple_steps_serialized(self) -> None:
        steps = [
            PipelineStep(name="step1", success=True, message="OK"),
            PipelineStep(name="step2", success=False, message="Failed"),
        ]
        result = PipelineResult(
            pipeline="test",
            success=False,
            message="Pipeline failed",
            steps=steps,
        )
        d = result.to_dict()
        assert len(d["steps"]) == 2
        assert d["steps"][0]["success"] is True
        assert d["steps"][1]["success"] is False


# ===================================================================
# list_pipelines
# ===================================================================

class TestListPipelines:
    """Tests for list_pipelines() registry."""

    def test_returns_four_pipelines(self) -> None:
        pipelines = list_pipelines()
        assert len(pipelines) == 4

    def test_pipeline_names(self) -> None:
        pipelines = list_pipelines()
        names = [p["name"] for p in pipelines]
        assert "quick_print" in names
        assert "reslice_and_print" in names
        assert "calibrate" in names
        assert "benchmark" in names

    def test_each_pipeline_has_description(self) -> None:
        pipelines = list_pipelines()
        for p in pipelines:
            assert "description" in p
            assert isinstance(p["description"], str)
            assert len(p["description"]) > 0

    def test_each_pipeline_has_params(self) -> None:
        pipelines = list_pipelines()
        for p in pipelines:
            assert "params" in p
            assert isinstance(p["params"], list)

    def test_quick_print_params_include_model_path(self) -> None:
        pipelines = list_pipelines()
        qp = next(p for p in pipelines if p["name"] == "quick_print")
        assert "model_path" in qp["params"]

    def test_pipelines_dict_has_function_key(self) -> None:
        """The PIPELINES registry dict should have a callable 'function' for each entry."""
        for name, info in PIPELINES.items():
            assert "function" in info
            assert callable(info["function"])


# ===================================================================
# quick_print pipeline
# ===================================================================

class TestQuickPrintPipeline:
    """Tests for quick_print() pipeline with mocked dependencies.

    slice_file is imported locally inside the function body via
    ``from kiln.slicer import slice_file``, so we mock at
    ``kiln.slicer.slice_file``. Same for resolve_slicer_profile
    (``kiln.slicer_profiles.resolve_slicer_profile``).
    """

    @patch("kiln.slicer.slice_file", side_effect=FileNotFoundError("model.stl not found"))
    def test_fails_at_slice_with_missing_model(self, mock_slice: MagicMock) -> None:
        """quick_print should fail at the slice step if the model file is missing."""
        result = quick_print(model_path="/nonexistent/model.stl")
        assert result.success is False
        assert result.pipeline == "quick_print"
        assert "slicing" in result.message.lower() or "slice" in result.message.lower()
        # Should have at least the slice step recorded
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1
        assert slice_steps[0].success is False

    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer not found"))
    def test_fails_at_slice_with_slicer_error(self, mock_slice: MagicMock) -> None:
        result = quick_print(model_path="/tmp/model.stl")
        assert result.success is False
        assert any(s.name == "slice" and not s.success for s in result.steps)

    def test_result_has_pipeline_name(self) -> None:
        """Even on failure, pipeline name should be 'quick_print'."""
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = quick_print(model_path="/tmp/model.stl")
        assert result.pipeline == "quick_print"

    def test_result_has_total_duration(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = quick_print(model_path="/tmp/model.stl")
        assert result.total_duration_seconds >= 0

    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini")
    def test_profile_resolution_step_recorded(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
    ) -> None:
        """When printer_id is given, profile resolution step should be recorded."""
        result = quick_print(model_path="/tmp/model.stl", printer_id="ender3")
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is True

    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    @patch("kiln.slicer_profiles.resolve_slicer_profile", side_effect=KeyError("no profile"))
    def test_profile_resolution_failure_non_fatal(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
    ) -> None:
        """Profile resolution failure should be recorded but not abort the pipeline."""
        result = quick_print(model_path="/tmp/model.stl", printer_id="unknown_printer")
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is False
        # Pipeline continues to slice step
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1


# ===================================================================
# benchmark pipeline
# ===================================================================

class TestBenchmarkPipeline:
    """Tests for benchmark() pipeline with mocked dependencies."""

    def test_no_model_path_returns_error(self) -> None:
        """benchmark() without model_path should fail immediately."""
        result = benchmark()
        assert result.success is False
        assert result.pipeline == "benchmark"
        assert "model_path" in result.message.lower() or "model" in result.message.lower()

    def test_no_model_path_has_model_step(self) -> None:
        result = benchmark()
        model_steps = [s for s in result.steps if s.name == "model"]
        assert len(model_steps) == 1
        assert model_steps[0].success is False

    def test_no_model_path_total_duration(self) -> None:
        result = benchmark()
        assert result.total_duration_seconds >= 0

    @patch("kiln.slicer.slice_file", side_effect=Exception("slicer error"))
    def test_fails_at_slice(self, mock_slice: MagicMock) -> None:
        result = benchmark(model_path="/tmp/bench.stl")
        assert result.success is False
        assert result.pipeline == "benchmark"
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1
        assert slice_steps[0].success is False

    @patch("kiln.slicer.slice_file", side_effect=Exception("fail"))
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini")
    def test_profile_resolution_with_printer_id(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
    ) -> None:
        result = benchmark(model_path="/tmp/bench.stl", printer_id="ender3")
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is True


# ===================================================================
# Pipeline result serialization
# ===================================================================

class TestPipelineResultSerialization:
    """Tests for end-to-end pipeline result serialization."""

    def test_failed_quick_print_serializable(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = quick_print(model_path="/tmp/model.stl")
        d = result.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["pipeline"] == "quick_print"
        assert parsed["success"] is False

    def test_failed_benchmark_serializable(self) -> None:
        result = benchmark()
        d = result.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["pipeline"] == "benchmark"
        assert parsed["success"] is False

    def test_all_result_fields_present_in_dict(self) -> None:
        result = benchmark()
        d = result.to_dict()
        expected_keys = [
            "pipeline", "success", "message", "job_id",
            "total_duration_seconds", "steps",
        ]
        for key in expected_keys:
            assert key in d, f"Missing key '{key}' in PipelineResult.to_dict()"

    def test_step_with_empty_data_omits_data(self) -> None:
        """PipelineStep.to_dict() should omit 'data' when data is empty."""
        result = benchmark()
        for step_dict in result.to_dict()["steps"]:
            if "data" in step_dict:
                # If data is present, it must be non-empty
                assert len(step_dict["data"]) > 0


# ===================================================================
# reslice_and_print pipeline
# ===================================================================

class TestResliceAndPrintPipeline:
    """Tests for reslice_and_print() pipeline with mocked dependencies.

    Covers:
        - Full pipeline execution with mocked slicer + adapter
        - Overrides passed through to resolve_slicer_profile
        - Slicer not found error handling
        - Printer offline at preflight
        - Upload failure stops pipeline
        - Pipeline name and serialization
    """

    @patch("kiln.slicer.slice_file", side_effect=FileNotFoundError("model.stl not found"))
    def test_fails_at_slice_with_missing_model(self, mock_slice: MagicMock) -> None:
        result = reslice_and_print(model_path="/nonexistent/model.stl")
        assert result.success is False
        assert result.pipeline == "reslice_and_print"
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1
        assert slice_steps[0].success is False

    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer not found"))
    def test_fails_at_slice_with_slicer_error(self, mock_slice: MagicMock) -> None:
        result = reslice_and_print(model_path="/tmp/model.stl")
        assert result.success is False
        assert any(s.name == "slice" and not s.success for s in result.steps)

    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini")
    def test_overrides_passed_to_resolve_slicer_profile(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
    ) -> None:
        overrides = {"brim_width": "8", "fill_density": "25%"}
        reslice_and_print(
            model_path="/tmp/model.stl",
            printer_id="ender3",
            overrides=overrides,
        )
        mock_resolve.assert_called_once_with("ender3", overrides=overrides)

    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini")
    def test_profile_resolution_step_recorded_with_overrides(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
    ) -> None:
        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_id="ender3",
            overrides={"brim_width": "8"},
        )
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is True
        assert "override" in profile_steps[0].message.lower()

    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    @patch("kiln.slicer_profiles.resolve_slicer_profile", side_effect=KeyError("no profile"))
    def test_profile_resolution_failure_non_fatal(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
    ) -> None:
        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_id="unknown_printer",
            overrides={"brim_width": "8"},
        )
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is False
        # Pipeline continues to slice step
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1

    def test_explicit_profile_skips_resolution(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = reslice_and_print(
                model_path="/tmp/model.stl",
                profile_path="/tmp/custom.ini",
            )
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is True
        assert "explicit" in profile_steps[0].message.lower()

    @patch("kiln.server._registry")
    @patch("kiln.gcode.scan_gcode_file")
    @patch("kiln.slicer.slice_file")
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini")
    def test_full_pipeline_success(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
        mock_gcode: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        # Setup slice mock
        slice_result = MagicMock()
        slice_result.output_path = "/tmp/out.gcode"
        slice_result.message = "Sliced OK"
        slice_result.slicer = "prusaslicer"
        mock_slice.return_value = slice_result

        # Setup gcode mock
        gcode_result = MagicMock()
        gcode_result.valid = True
        gcode_result.commands = ["G28"]
        gcode_result.blocked_commands = []
        gcode_result.warnings = []
        gcode_result.errors = []
        mock_gcode.return_value = gcode_result

        # Setup adapter mock
        mock_adapter = MagicMock()
        mock_adapter.upload_file.return_value = {"name": "out.gcode"}
        state = MagicMock()
        state.connected = True
        state.status.value = "idle"
        mock_adapter.get_state.return_value = state
        mock_registry.get_adapter.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="myprinter",
            printer_id="ender3",
            overrides={"brim_width": "8"},
        )

        assert result.success is True
        assert result.pipeline == "reslice_and_print"
        assert len(result.steps) == 6
        assert all(s.success for s in result.steps)
        mock_adapter.start_print.assert_called_once_with("out.gcode")

    @patch("kiln.server._registry")
    @patch("kiln.slicer.slice_file")
    def test_upload_failure_stops_pipeline(
        self,
        mock_slice: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        # Setup slice mock
        slice_result = MagicMock()
        slice_result.output_path = "/tmp/out.gcode"
        slice_result.message = "Sliced OK"
        slice_result.slicer = "prusaslicer"
        mock_slice.return_value = slice_result

        # Setup adapter that fails on upload
        mock_adapter = MagicMock()
        mock_adapter.upload_file.side_effect = RuntimeError("Upload failed: connection timeout")
        mock_registry.get_adapter.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="myprinter",
        )

        assert result.success is False
        upload_steps = [s for s in result.steps if s.name == "upload"]
        assert len(upload_steps) == 1
        assert upload_steps[0].success is False
        # Pipeline should stop — no preflight or start_print steps
        assert not any(s.name == "start_print" for s in result.steps)

    @patch("kiln.server._registry")
    @patch("kiln.slicer.slice_file")
    def test_printer_offline_at_preflight(
        self,
        mock_slice: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        # Setup slice mock
        slice_result = MagicMock()
        slice_result.output_path = "/tmp/out.gcode"
        slice_result.message = "Sliced OK"
        slice_result.slicer = "prusaslicer"
        mock_slice.return_value = slice_result

        # Setup adapter that uploads OK but reports offline at preflight
        mock_adapter = MagicMock()
        mock_adapter.upload_file.return_value = {"name": "out.gcode"}
        state = MagicMock()
        state.connected = False
        state.status.value = "offline"
        mock_adapter.get_state.return_value = state
        mock_registry.get_adapter.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="myprinter",
        )

        assert result.success is False
        preflight_steps = [s for s in result.steps if s.name == "preflight"]
        assert len(preflight_steps) == 1
        assert preflight_steps[0].success is False
        assert "offline" in preflight_steps[0].message.lower() or "not ready" in preflight_steps[0].message.lower()

    def test_result_has_pipeline_name(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = reslice_and_print(model_path="/tmp/model.stl")
        assert result.pipeline == "reslice_and_print"

    def test_result_has_total_duration(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = reslice_and_print(model_path="/tmp/model.stl")
        assert result.total_duration_seconds >= 0

    def test_result_is_json_serializable(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = reslice_and_print(model_path="/tmp/model.stl")
        d = result.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["pipeline"] == "reslice_and_print"

    def test_empty_overrides_treated_as_no_overrides(self) -> None:
        with (
            patch("kiln.slicer.slice_file", side_effect=Exception("fail")),
            patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/p.ini") as mock_resolve,
        ):
            reslice_and_print(
                model_path="/tmp/model.stl",
                printer_id="ender3",
                overrides={},
            )
            mock_resolve.assert_called_once_with("ender3", overrides={})

    def test_registered_in_pipelines_dict(self) -> None:
        assert "reslice_and_print" in PIPELINES
        assert callable(PIPELINES["reslice_and_print"]["function"])
        assert "overrides" in PIPELINES["reslice_and_print"]["params"]


# ===================================================================
# run_reslice_and_print MCP tool
# ===================================================================

class TestRunResliceAndPrintTool:
    """Tests for the run_reslice_and_print MCP tool wrapper in server.py.

    Covers:
        - Invalid JSON overrides returns VALIDATION_ERROR
        - Non-object overrides returns VALIDATION_ERROR
        - Valid overrides parsed and forwarded to pipeline
        - Pipeline failure propagated as structured result
    """

    @patch("kiln.server._check_auth", return_value=None)
    def test_invalid_overrides_json(self, mock_auth: MagicMock) -> None:
        from kiln.server import run_reslice_and_print

        result = run_reslice_and_print(
            model_path="/tmp/model.stl",
            overrides="not valid json{{{",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "json" in result["error"]["message"].lower()

    @patch("kiln.server._check_auth", return_value=None)
    def test_non_object_overrides_json(self, mock_auth: MagicMock) -> None:
        from kiln.server import run_reslice_and_print

        result = run_reslice_and_print(
            model_path="/tmp/model.stl",
            overrides='["not", "an", "object"]',
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "object" in result["error"]["message"].lower()

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._pipeline_reslice_and_print")
    def test_valid_overrides_forwarded_to_pipeline(
        self,
        mock_pipeline: MagicMock,
        mock_auth: MagicMock,
    ) -> None:
        from kiln.server import run_reslice_and_print

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.to_dict.return_value = {
            "pipeline": "reslice_and_print",
            "success": True,
            "message": "Done",
            "steps": [],
            "job_id": None,
            "total_duration_seconds": 0.1,
        }
        mock_pipeline.return_value = mock_result

        result = run_reslice_and_print(
            model_path="/tmp/model.stl",
            printer_id="ender3",
            overrides='{"brim_width": "8", "fill_density": "25%"}',
        )
        assert result["success"] is True
        mock_pipeline.assert_called_once_with(
            model_path="/tmp/model.stl",
            printer_name=None,
            printer_id="ender3",
            overrides={"brim_width": "8", "fill_density": "25%"},
            profile_path=None,
            slicer_path=None,
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._pipeline_reslice_and_print", side_effect=Exception("boom"))
    def test_unexpected_error_returns_internal_error(
        self,
        mock_pipeline: MagicMock,
        mock_auth: MagicMock,
    ) -> None:
        from kiln.server import run_reslice_and_print

        result = run_reslice_and_print(model_path="/tmp/model.stl")
        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._pipeline_reslice_and_print")
    def test_none_overrides_calls_pipeline_with_none(
        self,
        mock_pipeline: MagicMock,
        mock_auth: MagicMock,
    ) -> None:
        from kiln.server import run_reslice_and_print

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.to_dict.return_value = {
            "pipeline": "reslice_and_print",
            "success": False,
            "message": "Failed",
            "steps": [],
            "job_id": None,
            "total_duration_seconds": 0.0,
        }
        mock_pipeline.return_value = mock_result

        run_reslice_and_print(model_path="/tmp/model.stl")
        mock_pipeline.assert_called_once_with(
            model_path="/tmp/model.stl",
            printer_name=None,
            printer_id=None,
            overrides=None,
            profile_path=None,
            slicer_path=None,
        )
