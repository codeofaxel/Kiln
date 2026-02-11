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

    def test_returns_three_pipelines(self) -> None:
        pipelines = list_pipelines()
        assert len(pipelines) == 3

    def test_pipeline_names(self) -> None:
        pipelines = list_pipelines()
        names = [p["name"] for p in pipelines]
        assert "quick_print" in names
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
