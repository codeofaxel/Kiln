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
from unittest.mock import MagicMock, patch

from kiln.pipelines import (
    PIPELINES,
    PipelineResult,
    PipelineStep,
    benchmark,
    list_pipelines,
    quick_print,
    reslice_and_print,
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
        for _name, info in PIPELINES.items():
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

    These tests pass ``skip_validation=True`` to isolate the slice /
    safety / upload steps from the pre-print validation gate.  The
    validation step has its own coverage in
    :class:`TestQuickPrintValidationStep`.
    """

    @patch("kiln.slicer.slice_file", side_effect=FileNotFoundError("model.stl not found"))
    def test_fails_at_slice_with_missing_model(self, mock_slice: MagicMock) -> None:
        """quick_print should fail at the slice step if the model file is missing."""
        result = quick_print(model_path="/nonexistent/model.stl", skip_validation=True)
        assert result.success is False
        assert result.pipeline == "quick_print"
        assert "slicing" in result.message.lower() or "slice" in result.message.lower()
        # Should have at least the slice step recorded
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1
        assert slice_steps[0].success is False

    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer not found"))
    def test_fails_at_slice_with_slicer_error(self, mock_slice: MagicMock) -> None:
        result = quick_print(model_path="/tmp/model.stl", skip_validation=True)
        assert result.success is False
        assert any(s.name == "slice" and not s.success for s in result.steps)

    def test_result_has_pipeline_name(self) -> None:
        """Even on failure, pipeline name should be 'quick_print'."""
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = quick_print(model_path="/tmp/model.stl", skip_validation=True)
        assert result.pipeline == "quick_print"

    def test_result_has_total_duration(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = quick_print(model_path="/tmp/model.stl", skip_validation=True)
        assert result.total_duration_seconds >= 0

    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini")
    def test_profile_resolution_step_recorded(
        self,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
    ) -> None:
        """When printer_id is given, profile resolution step should be recorded."""
        result = quick_print(
            model_path="/tmp/model.stl",
            printer_id="ender3",
            skip_validation=True,
        )
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
        result = quick_print(
            model_path="/tmp/model.stl",
            printer_id="unknown_printer",
            skip_validation=True,
        )
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is False
        # Pipeline continues to slice step
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1


# ===================================================================
# quick_print pre-print validation gate (validate_mesh step)
# ===================================================================


class TestQuickPrintValidationStep:
    """Tests for the pre-print validation gate that runs as the first
    step of ``quick_print`` (and ``reslice_and_print``).

    Wires through ``run_full_validation_pipeline`` to gate the print on
    mesh-level printability — manifold, walls, overhangs, bridges,
    bed-fit, material.  Auto-repaired meshes update the working path so
    downstream slicing operates on the repaired geometry.
    """

    @patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline")
    @patch("kiln.slicer.slice_file")
    def test_validation_failure_blocks_pipeline(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """A non-printable mesh aborts the pipeline at validate_mesh — slice never runs."""
        mock_validate.return_value = {
            "ready_to_print": False,
            "printability_score": 25,
            "validated_path": "/tmp/model.stl",
            "summary": "Not ready (25/100). 1 issue: non-manifold",
            "next_action": {"tool": "repair_mesh_advanced", "reason": "non-manifold"},
            "repaired": False,
        }

        result = quick_print(model_path="/tmp/model.stl")

        assert result.success is False
        assert result.pipeline == "quick_print"
        # validate_mesh step ran, was the failure
        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is False
        assert "25/100" in validate_steps[0].message
        # slice never ran
        assert mock_slice.call_count == 0
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 0

    @patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline")
    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer mock"))
    def test_validation_pass_lets_pipeline_continue(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """A passing mesh records validate_mesh success and proceeds to slice."""
        mock_validate.return_value = {
            "ready_to_print": True,
            "printability_score": 92,
            "validated_path": "/tmp/model.stl",
            "summary": "Print-ready (92/100).",
            "next_action": None,
            "repaired": False,
        }

        result = quick_print(model_path="/tmp/model.stl")

        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is True
        assert validate_steps[0].data["printability_score"] == 92
        # Pipeline reached slice step (which is mocked to fail — that's fine)
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1

    @patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline")
    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer mock"))
    def test_auto_repair_updates_slice_path(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """When validation auto-repairs the mesh, slice runs on the repaired path."""
        mock_validate.return_value = {
            "ready_to_print": True,
            "printability_score": 75,
            "validated_path": "/tmp/repaired_model.stl",
            "summary": "Print-ready (75/100). Auto-repaired non-manifold.",
            "next_action": None,
            "repaired": True,
        }

        quick_print(model_path="/tmp/original_model.stl")

        # slice was called with the repaired path, not the original
        assert mock_slice.call_count == 1
        call_args = mock_slice.call_args
        assert call_args.args[0] == "/tmp/repaired_model.stl"

    @patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline")
    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer mock"))
    def test_skip_validation_bypasses_gate(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """skip_validation=True records a skipped validate_mesh and never calls the validator."""
        result = quick_print(
            model_path="/tmp/model.stl",
            skip_validation=True,
        )

        # Validator never invoked
        assert mock_validate.call_count == 0
        # validate_mesh step still recorded as a clean skip
        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is True
        assert "skip" in validate_steps[0].message.lower()
        # Pipeline reached slice (which is mocked to fail — fine)
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1

    @patch(
        "kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline",
        side_effect=ValueError("validator infrastructure failure"),
    )
    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer mock"))
    def test_validation_pipeline_crash_does_not_block_print(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """If the validator itself raises, the pipeline still proceeds —
        an infrastructure-side bug must not block users from printing."""
        result = quick_print(model_path="/tmp/model.stl")

        # validate_mesh step recorded as a soft skip, not a fatal
        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is True
        # Pipeline reached slice
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1

    def test_unsupported_format_skips_validation_cleanly(self) -> None:
        """An input format the validator doesn't understand (e.g. .gcode)
        records a skip — it doesn't fail the pipeline."""
        with patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer mock")):
            result = quick_print(model_path="/tmp/model.gcode")

        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is True
        assert "skip" in validate_steps[0].message.lower()


# ===================================================================
# benchmark pipeline
# ===================================================================

class TestBenchmarkPipeline:
    """Tests for benchmark() pipeline with mocked dependencies.

    These tests pass ``skip_validation=True`` to isolate slice / upload
    semantics from the pre-print validation gate.  Validation gating
    coverage lives in :class:`TestBenchmarkValidationStep`.
    """

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
        result = benchmark(model_path="/tmp/bench.stl", skip_validation=True)
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
        result = benchmark(
            model_path="/tmp/bench.stl",
            printer_id="ender3",
            skip_validation=True,
        )
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is True


# ===================================================================
# benchmark pre-print validation gate
# ===================================================================


class TestBenchmarkValidationStep:
    """Tests for the pre-print validation step in benchmark()."""

    @patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline")
    @patch("kiln.slicer.slice_file")
    def test_validation_failure_blocks_benchmark(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """A non-printable benchmark mesh aborts before slicing."""
        mock_validate.return_value = {
            "ready_to_print": False,
            "printability_score": 30,
            "validated_path": "/tmp/bench.stl",
            "summary": "Not ready (30/100). 1 issue: paper-thin walls",
            "next_action": None,
            "repaired": False,
        }
        result = benchmark(model_path="/tmp/bench.stl")

        assert result.success is False
        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is False
        assert "30/100" in validate_steps[0].message
        # Slicer never invoked
        assert mock_slice.call_count == 0

    @patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline")
    @patch("kiln.slicer.slice_file", side_effect=Exception("slicer mock"))
    def test_validation_pass_proceeds_to_slice(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """A passing benchmark mesh records success and continues to slice."""
        mock_validate.return_value = {
            "ready_to_print": True,
            "printability_score": 90,
            "validated_path": "/tmp/bench.stl",
            "summary": "Print-ready (90/100).",
            "next_action": None,
            "repaired": False,
        }
        result = benchmark(model_path="/tmp/bench.stl")

        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is True
        assert validate_steps[0].data["printability_score"] == 90
        # Reached slice
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1

    @patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline")
    @patch("kiln.slicer.slice_file", side_effect=Exception("slicer mock"))
    def test_skip_validation_bypasses_gate(
        self,
        mock_slice: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """skip_validation=True records a clean skip and never calls the validator."""
        result = benchmark(model_path="/tmp/bench.stl", skip_validation=True)

        assert mock_validate.call_count == 0
        validate_steps = [s for s in result.steps if s.name == "validate_mesh"]
        assert len(validate_steps) == 1
        assert validate_steps[0].success is True
        assert "skip" in validate_steps[0].message.lower()


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

    These tests pass ``skip_validation=True`` to isolate slicer / adapter /
    upload semantics from the pre-print validation gate.  Validation has
    its own coverage in :class:`TestQuickPrintValidationStep`.
    """

    @patch("kiln.slicer.slice_file", side_effect=FileNotFoundError("model.stl not found"))
    def test_fails_at_slice_with_missing_model(self, mock_slice: MagicMock) -> None:
        result = reslice_and_print(model_path="/nonexistent/model.stl", skip_validation=True)
        assert result.success is False
        assert result.pipeline == "reslice_and_print"
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1
        assert slice_steps[0].success is False

    @patch("kiln.slicer.slice_file", side_effect=RuntimeError("slicer not found"))
    def test_fails_at_slice_with_slicer_error(self, mock_slice: MagicMock) -> None:
        result = reslice_and_print(model_path="/tmp/model.stl", skip_validation=True)
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
            skip_validation=True,
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
            skip_validation=True,
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
            skip_validation=True,
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
                skip_validation=True,
            )
        profile_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert len(profile_steps) == 1
        assert profile_steps[0].success is True
        assert "explicit" in profile_steps[0].message.lower()

    @patch("kiln.server._resolve_adapter")
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

        # Setup adapter mock — use spec to exclude Bambu-only methods so
        # the pipeline treats this as a non-Bambu printer (no 3MF wrapping).
        mock_adapter = MagicMock(spec=["upload_file", "get_state", "start_print"])
        mock_adapter.upload_file.return_value = {"name": "out.gcode"}
        state = MagicMock()
        state.connected = True
        state.state.value = "idle"
        mock_adapter.get_state.return_value = state
        mock_registry.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="myprinter",
            printer_id="ender3",
            overrides={"brim_width": "8"},
            skip_validation=True,
        )

        assert result.success is True
        assert result.pipeline == "reslice_and_print"
        # 8 steps: validate_mesh (skipped) + resolve_profile + stability_check
        # + slice + safety_check + upload + preflight + start_print.
        assert len(result.steps) == 8
        assert all(s.success for s in result.steps)
        mock_adapter.start_print.assert_called_once_with("out.gcode")

    @patch("kiln.gcode.scan_gcode_file")
    @patch("kiln.slicer.slice_file")
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/p.ini")
    @patch("kiln.server._resolve_adapter")
    def test_upload_resolves_adapter_through_shared_door(
        self,
        mock_resolve: MagicMock,
        mock_profile: MagicMock,
        mock_slice: MagicMock,
        mock_gcode: MagicMock,
    ) -> None:
        """The upload step goes through kiln.server._resolve_adapter.

        It used to reach for the raw registry global and call two
        methods PrinterRegistry has never had; the AttributeError was
        swallowed into "Upload failed", so every pipeline upload with a
        registered printer failed — and the tests never noticed because
        they mocked the registry into HAVING the fantasy methods.  This
        test pins the real door and the name passed through it.
        """
        slice_result = MagicMock()
        slice_result.output_path = "/tmp/out.gcode"
        slice_result.message = "Sliced OK"
        slice_result.slicer = "prusaslicer"
        mock_slice.return_value = slice_result

        gcode_result = MagicMock()
        gcode_result.valid = True
        gcode_result.commands = ["G28"]
        gcode_result.blocked_commands = []
        gcode_result.warnings = []
        gcode_result.errors = []
        mock_gcode.return_value = gcode_result

        mock_adapter = MagicMock(spec=["upload_file", "get_state", "start_print"])
        mock_adapter.upload_file.return_value = {"name": "out.gcode"}
        state = MagicMock()
        state.connected = True
        state.state.value = "idle"
        mock_adapter.get_state.return_value = state
        mock_resolve.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="shop-x1",
            printer_id="ender3",
            overrides={"brim_width": "8"},
            skip_validation=True,
        )

        assert result.success is True, [s.message for s in result.steps if not s.success]
        mock_resolve.assert_called_with("shop-x1")

    @patch("kiln.gcode.scan_gcode_file")
    @patch("kiln.slicer.slice_file")
    @patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/p.ini")
    @patch("kiln.server._resolve_adapter")
    def test_bambu_adapter_wraps_gcode_as_3mf(
        self,
        mock_registry: MagicMock,
        mock_resolve: MagicMock,
        mock_slice: MagicMock,
        mock_gcode: MagicMock,
    ) -> None:
        """Bambu adapters auto-wrap gcode in 3MF for full AMS/timelapse support."""
        slice_result = MagicMock()
        slice_result.output_path = "/tmp/out.gcode"
        slice_result.message = "Sliced OK"
        slice_result.slicer = "prusaslicer"
        mock_slice.return_value = slice_result

        gcode_result = MagicMock()
        gcode_result.valid = True
        gcode_result.commands = ["G28"]
        gcode_result.blocked_commands = []
        gcode_result.warnings = []
        gcode_result.errors = []
        mock_gcode.return_value = gcode_result

        # Bambu adapter mock — has wrap_gcode_as_3mf
        mock_adapter = MagicMock()
        mock_adapter.wrap_gcode_as_3mf.return_value = "/tmp/out.3mf"
        mock_adapter.upload_file.return_value = {"name": "out.3mf"}
        state = MagicMock()
        state.connected = True
        state.state.value = "idle"
        mock_adapter.get_state.return_value = state
        mock_registry.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="bambu",
            printer_id="bambu_a1",
            overrides={"temperature": "220", "bed_temperature": "65"},
            skip_validation=True,
        )

        assert result.success is True
        # Verify gcode was wrapped as 3MF
        mock_adapter.wrap_gcode_as_3mf.assert_called_once_with(
            "/tmp/out.gcode", hotend_temp=220, bed_temp=65
        )
        # Verify 3MF was uploaded (not raw gcode)
        mock_adapter.upload_file.assert_called_once_with("/tmp/out.3mf")
        # Verify start_print got local_file_path for MD5/AMS
        mock_adapter.start_print.assert_called_once_with(
            "out.3mf", local_file_path="/tmp/out.3mf"
        )
        # Upload step should note the wrapping
        upload_step = [s for s in result.steps if s.name == "upload"][0]
        assert upload_step.data["wrapped_3mf"] is True

    @patch("kiln.server._resolve_adapter")
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
        mock_registry.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="myprinter",
            skip_validation=True,
        )

        assert result.success is False
        upload_steps = [s for s in result.steps if s.name == "upload"]
        assert len(upload_steps) == 1
        assert upload_steps[0].success is False
        # Pipeline should stop — no preflight or start_print steps
        assert not any(s.name == "start_print" for s in result.steps)

    @patch("kiln.server._resolve_adapter")
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
        state.state.value = "offline"
        mock_adapter.get_state.return_value = state
        mock_registry.return_value = mock_adapter

        result = reslice_and_print(
            model_path="/tmp/model.stl",
            printer_name="myprinter",
            skip_validation=True,
        )

        assert result.success is False
        preflight_steps = [s for s in result.steps if s.name == "preflight"]
        assert len(preflight_steps) == 1
        assert preflight_steps[0].success is False
        assert "offline" in preflight_steps[0].message.lower() or "not ready" in preflight_steps[0].message.lower()

    def test_result_has_pipeline_name(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = reslice_and_print(model_path="/tmp/model.stl", skip_validation=True)
        assert result.pipeline == "reslice_and_print"

    def test_result_has_total_duration(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = reslice_and_print(model_path="/tmp/model.stl", skip_validation=True)
        assert result.total_duration_seconds >= 0

    def test_result_is_json_serializable(self) -> None:
        with patch("kiln.slicer.slice_file", side_effect=Exception("fail")):
            result = reslice_and_print(model_path="/tmp/model.stl", skip_validation=True)
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
                skip_validation=True,
            )
            mock_resolve.assert_called_once_with("ender3", overrides={})

    def test_registered_in_pipelines_dict(self) -> None:
        assert "reslice_and_print" in PIPELINES
        assert callable(PIPELINES["reslice_and_print"]["function"])
        assert "overrides" in PIPELINES["reslice_and_print"]["params"]


# ===================================================================
# Stability check in pipelines
# ===================================================================

class TestStabilityCheckPipeline:
    """Tests for stability_check step integration in quick_print and reslice_and_print.

    Covers:
        - Pipeline includes a stability_check step
        - Unstable model sets stability_warning in context
        - Stability check failure does not block the pipeline
        - Non-STL files skip the stability check
        - Stable model produces no warning
    """

    @patch("kiln.auto_orient.check_stability", create=True)
    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    def test_quick_print_includes_stability_step(
        self, mock_slice: MagicMock, mock_stability: MagicMock
    ) -> None:
        mock_result = MagicMock()
        mock_result.stable = True
        mock_result.risk_level = "low"
        mock_result.height_to_base_ratio = 0.5
        mock_result.recommendation = "Orientation looks stable."
        mock_stability.return_value = mock_result

        result = quick_print(model_path="/tmp/model.stl", skip_validation=True)
        stability_steps = [s for s in result.steps if s.name == "stability_check"]
        assert len(stability_steps) == 1
        assert stability_steps[0].success is True

    @patch("kiln.auto_orient.check_stability", create=True)
    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    def test_reslice_includes_stability_step(
        self, mock_slice: MagicMock, mock_stability: MagicMock
    ) -> None:
        mock_result = MagicMock()
        mock_result.stable = True
        mock_result.risk_level = "low"
        mock_result.height_to_base_ratio = 0.5
        mock_result.recommendation = "Looks stable."
        mock_stability.return_value = mock_result

        result = reslice_and_print(model_path="/tmp/model.stl", skip_validation=True)
        stability_steps = [s for s in result.steps if s.name == "stability_check"]
        assert len(stability_steps) == 1
        assert stability_steps[0].success is True

    @patch("kiln.auto_orient.check_stability", create=True)
    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    def test_unstable_model_sets_warning_in_step(
        self, mock_slice: MagicMock, mock_stability: MagicMock
    ) -> None:
        mock_result = MagicMock()
        mock_result.stable = False
        mock_result.risk_level = "high"
        mock_result.height_to_base_ratio = 5.0
        mock_result.recommendation = "High wobble risk. Reorienting recommended."
        mock_stability.return_value = mock_result

        result = quick_print(model_path="/tmp/tall_tower.stl", skip_validation=True)
        stability_steps = [s for s in result.steps if s.name == "stability_check"]
        assert len(stability_steps) == 1
        step = stability_steps[0]
        # Step still succeeds (informational only)
        assert step.success is True
        # Warning should be in the message
        assert "risk" in step.message.lower() or "wobble" in step.message.lower()
        # Step data should include risk info
        assert step.data["stable"] is False
        assert step.data["risk_level"] == "high"

    @patch("kiln.auto_orient.check_stability", create=True, side_effect=RuntimeError("mesh parse failed"))
    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    def test_stability_failure_does_not_block_pipeline(
        self, mock_slice: MagicMock, mock_stability: MagicMock
    ) -> None:
        result = quick_print(model_path="/tmp/model.stl", skip_validation=True)
        stability_steps = [s for s in result.steps if s.name == "stability_check"]
        assert len(stability_steps) == 1
        # Stability check should succeed even on internal error (it's non-fatal)
        assert stability_steps[0].success is True
        assert "skipped" in stability_steps[0].message.lower()
        # Pipeline continues — slice step should also be present
        slice_steps = [s for s in result.steps if s.name == "slice"]
        assert len(slice_steps) == 1

    @patch("kiln.auto_orient.check_stability", create=True)
    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    def test_non_stl_file_skips_stability_check(
        self, mock_slice: MagicMock, mock_stability: MagicMock
    ) -> None:
        result = quick_print(model_path="/tmp/model.gcode", skip_validation=True)
        stability_steps = [s for s in result.steps if s.name == "stability_check"]
        assert len(stability_steps) == 1
        assert stability_steps[0].success is True
        assert "skip" in stability_steps[0].message.lower()
        # check_stability should NOT have been called for non-mesh files
        mock_stability.assert_not_called()

    @patch("kiln.auto_orient.check_stability", create=True)
    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    def test_3mf_file_skips_stability_check(
        self, mock_slice: MagicMock, mock_stability: MagicMock
    ) -> None:
        result = quick_print(model_path="/tmp/model.3mf", skip_validation=True)
        stability_steps = [s for s in result.steps if s.name == "stability_check"]
        assert len(stability_steps) == 1
        assert "skip" in stability_steps[0].message.lower()
        mock_stability.assert_not_called()

    @patch("kiln.auto_orient.check_stability", create=True)
    @patch("kiln.slicer.slice_file", side_effect=Exception("slice error"))
    def test_stable_model_no_warning(
        self, mock_slice: MagicMock, mock_stability: MagicMock
    ) -> None:
        mock_result = MagicMock()
        mock_result.stable = True
        mock_result.risk_level = "low"
        mock_result.height_to_base_ratio = 0.3
        mock_result.recommendation = "Model is stable."
        mock_stability.return_value = mock_result

        result = quick_print(model_path="/tmp/flat_part.stl", skip_validation=True)
        stability_steps = [s for s in result.steps if s.name == "stability_check"]
        assert len(stability_steps) == 1
        step = stability_steps[0]
        assert step.success is True
        assert "stable" in step.message.lower()
        assert step.data["stable"] is True
        assert step.data["risk_level"] == "low"


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
        mock_pipeline.assert_called_once()
        call_kwargs = mock_pipeline.call_args[1]
        assert call_kwargs["model_path"] == "/tmp/model.stl"
        assert call_kwargs["printer_id"] == "ender3"
        # User overrides must be present (speed overrides may also be
        # auto-injected by printer speed intelligence — that's expected).
        assert call_kwargs["overrides"]["brim_width"] == "8"
        assert call_kwargs["overrides"]["fill_density"] == "25%"

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
        mock_pipeline.assert_called_once()
        call_kwargs = mock_pipeline.call_args[1]
        assert call_kwargs["model_path"] == "/tmp/model.stl"
        assert call_kwargs["printer_id"] is None
        # overrides may be None or a dict with auto-injected speed
        # overrides from printer type detection — both are valid.
