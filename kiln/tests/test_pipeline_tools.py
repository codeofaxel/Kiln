"""Tests for pipeline MCP tools in server.py.

Covers:
- run_quick_print() — happy path, auth failure, internal error
- run_calibrate() — happy path, auth failure, internal error
- run_benchmark() — happy path, auth failure, internal error
- pipeline_status() — found, not found, auth failure
- pipeline_pause() — running, not running, not found, auth failure
- pipeline_resume() — paused, not paused, not found, auth failure
- pipeline_abort() — running, completed/aborted, not found, auth failure
- pipeline_retry_step() — failed, wrong state, not found, auth failure
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln.pipelines import PipelineExecution, PipelineResult, PipelineState, PipelineStep, _StepDef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_result(pipeline: str = "quick_print") -> PipelineResult:
    """Build a successful PipelineResult for mocking."""
    return PipelineResult(
        pipeline=pipeline,
        success=True,
        message="Pipeline completed.",
        steps=[
            PipelineStep(name="slice", success=True, message="Sliced OK"),
            PipelineStep(name="upload", success=True, message="Uploaded OK"),
        ],
        total_duration_seconds=1.5,
    )


def _fail_result(pipeline: str = "quick_print") -> PipelineResult:
    """Build a failed PipelineResult for mocking."""
    return PipelineResult(
        pipeline=pipeline,
        success=False,
        message="Pipeline failed at slice: Slicer not found",
        steps=[
            PipelineStep(name="slice", success=False, message="Slicer not found"),
        ],
        total_duration_seconds=0.3,
    )


def _auth_error() -> dict:
    """Simulate the dict _check_auth returns when auth fails."""
    return {
        "success": False,
        "error": {
            "code": "AUTH_ERROR",
            "message": "Authentication failed.",
            "retryable": False,
        },
    }


def _make_execution(state: PipelineState) -> PipelineExecution:
    """Create a PipelineExecution in the given state with dummy steps."""
    step_defs = [
        _StepDef(name="step_a", fn=lambda: PipelineStep(name="step_a", success=True, message="ok")),
        _StepDef(name="step_b", fn=lambda: PipelineStep(name="step_b", success=True, message="ok")),
        _StepDef(name="step_c", fn=lambda: PipelineStep(name="step_c", success=True, message="ok")),
    ]
    ex = PipelineExecution.__new__(PipelineExecution)
    ex.execution_id = "test-exec-123"
    ex.pipeline_name = "test_pipeline"
    ex.step_defs = step_defs
    ex.current_step = 0
    ex.state = state
    ex.steps = []
    ex.start_time = 0.0
    ex.pause_after_step = None
    ex._pause_requested = False
    return ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Disable auth for all tests by default."""
    with patch("kiln.server._check_auth", return_value=None):
        yield


# ---------------------------------------------------------------------------
# TestRunQuickPrint
# ---------------------------------------------------------------------------


class TestRunQuickPrint:
    """Tests for the run_quick_print() MCP tool."""

    @patch("kiln.server._pipeline_quick_print")
    def test_happy_path(self, mock_pipeline):
        from kiln.server import run_quick_print

        mock_pipeline.return_value = _ok_result("quick_print")
        result = run_quick_print(model_path="/tmp/test.stl")

        assert result["success"] is True
        assert result["pipeline"] == "quick_print"
        assert result["message"] == "Pipeline completed."
        assert len(result["steps"]) == 2
        mock_pipeline.assert_called_once_with(
            model_path="/tmp/test.stl",
            printer_name=None,
            printer_id=None,
            profile_path=None,
            material=None,
            use_ams=None,
            ams_mapping=None,
            skip_validation=False,
        )

    @patch("kiln.server._pipeline_quick_print")
    def test_passes_all_args(self, mock_pipeline):
        from kiln.server import run_quick_print

        mock_pipeline.return_value = _ok_result()
        run_quick_print(
            model_path="/tmp/model.stl",
            printer_name="my_printer",
            printer_id="ender3",
            profile_path="/tmp/profile.ini",
        )

        mock_pipeline.assert_called_once_with(
            model_path="/tmp/model.stl",
            printer_name="my_printer",
            printer_id="ender3",
            profile_path="/tmp/profile.ini",
            material=None,
            use_ams=None,
            ams_mapping=None,
            skip_validation=False,
        )

    @patch("kiln.server._pipeline_quick_print")
    def test_skip_validation_passed_through(self, mock_pipeline):
        from kiln.server import run_quick_print

        mock_pipeline.return_value = _ok_result("quick_print")
        run_quick_print(model_path="/tmp/test.stl", skip_validation=True)

        mock_pipeline.assert_called_once_with(
            model_path="/tmp/test.stl",
            printer_name=None,
            printer_id=None,
            profile_path=None,
            material=None,
            use_ams=None,
            ams_mapping=None,
            skip_validation=True,
        )

    @patch("kiln.server._pipeline_quick_print")
    def test_pipeline_failure_propagated(self, mock_pipeline):
        from kiln.server import run_quick_print

        mock_pipeline.return_value = _fail_result()
        result = run_quick_print(model_path="/tmp/test.stl")

        assert result["success"] is False
        assert "failed" in result["message"].lower()

    @patch("kiln.server._pipeline_quick_print", side_effect=RuntimeError("boom"))
    def test_unexpected_exception_returns_error_dict(self, mock_pipeline):
        from kiln.server import run_quick_print

        result = run_quick_print(model_path="/tmp/test.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert "boom" in result["error"]["message"]

    def test_auth_failure(self):
        from kiln.server import run_quick_print

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = run_quick_print(model_path="/tmp/test.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"

    @patch("kiln.server._pipeline_quick_print")
    def test_ams_mapping_json_parsed(self, mock_pipeline):
        from kiln.server import run_quick_print

        mock_pipeline.return_value = _ok_result()
        run_quick_print(model_path="/tmp/test.stl", ams_mapping="[0, 2]")

        assert mock_pipeline.call_args.kwargs["ams_mapping"] == [0, 2]

    @patch("kiln.server._pipeline_quick_print")
    def test_ams_mapping_bad_json_rejected(self, mock_pipeline):
        from kiln.server import run_quick_print

        result = run_quick_print(model_path="/tmp/test.stl", ams_mapping="not json")

        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        mock_pipeline.assert_not_called()

    @patch("kiln.server._pipeline_quick_print")
    def test_ams_mapping_non_list_rejected(self, mock_pipeline):
        from kiln.server import run_quick_print

        result = run_quick_print(model_path="/tmp/test.stl", ams_mapping='{"a": 1}')

        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        mock_pipeline.assert_not_called()

    @patch("kiln.server._pipeline_quick_print")
    def test_use_ams_tristate_normalized(self, mock_pipeline):
        from kiln.server import run_quick_print

        for raw, expected in (("true", True), ("false", False), ("auto", None)):
            mock_pipeline.reset_mock()
            mock_pipeline.return_value = _ok_result()
            run_quick_print(model_path="/tmp/test.stl", use_ams=raw)
            assert mock_pipeline.call_args.kwargs["use_ams"] is expected

    @patch("kiln.server._pipeline_quick_print")
    def test_ams_selection_hoisted_to_top_level(self, mock_pipeline):
        from kiln.server import run_quick_print

        sel = {"slot": 1, "type": "PLA", "color": "161616FF"}
        mock_pipeline.return_value = PipelineResult(
            pipeline="quick_print",
            success=True,
            message="ok",
            steps=[
                PipelineStep(name="slice", success=True, message="ok"),
                PipelineStep(
                    name="start_print", success=True, message="ok",
                    data={"file_name": "x.gcode.3mf", "ams_selection": sel},
                ),
            ],
            total_duration_seconds=1.0,
        )
        result = run_quick_print(model_path="/tmp/test.stl")

        assert result["ams_selection"] == sel


# ---------------------------------------------------------------------------
# TestRunCalibrate
# ---------------------------------------------------------------------------


class TestRunCalibrate:
    """Tests for the run_calibrate() MCP tool."""

    @patch("kiln.server._pipeline_calibrate")
    def test_happy_path(self, mock_pipeline):
        from kiln.server import run_calibrate

        mock_pipeline.return_value = _ok_result("calibrate")
        result = run_calibrate()

        assert result["success"] is True
        assert result["pipeline"] == "calibrate"
        mock_pipeline.assert_called_once_with(
            printer_name=None,
            printer_id=None,
        )

    @patch("kiln.server._pipeline_calibrate")
    def test_passes_printer_args(self, mock_pipeline):
        from kiln.server import run_calibrate

        mock_pipeline.return_value = _ok_result("calibrate")
        run_calibrate(printer_name="ender", printer_id="ender3")

        mock_pipeline.assert_called_once_with(
            printer_name="ender",
            printer_id="ender3",
        )

    @patch("kiln.server._pipeline_calibrate", side_effect=RuntimeError("hardware fault"))
    def test_unexpected_exception_returns_error_dict(self, mock_pipeline):
        from kiln.server import run_calibrate

        result = run_calibrate()

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert "hardware fault" in result["error"]["message"]

    def test_auth_failure(self):
        from kiln.server import run_calibrate

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = run_calibrate()

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# TestRunBenchmark
# ---------------------------------------------------------------------------


class TestRunBenchmark:
    """Tests for the run_benchmark() MCP tool."""

    @patch("kiln.server._pipeline_benchmark")
    def test_happy_path(self, mock_pipeline):
        from kiln.server import run_benchmark

        mock_pipeline.return_value = _ok_result("benchmark")
        result = run_benchmark(model_path="/tmp/bench.stl")

        assert result["success"] is True
        assert result["pipeline"] == "benchmark"
        mock_pipeline.assert_called_once_with(
            model_path="/tmp/bench.stl",
            printer_name=None,
            printer_id=None,
            profile_path=None,
            skip_validation=False,
        )

    @patch("kiln.server._pipeline_benchmark")
    def test_passes_all_args(self, mock_pipeline):
        from kiln.server import run_benchmark

        mock_pipeline.return_value = _ok_result("benchmark")
        run_benchmark(
            model_path="/tmp/bench.stl",
            printer_name="prusa",
            printer_id="prusa_mk4",
            profile_path="/tmp/profile.ini",
        )

        mock_pipeline.assert_called_once_with(
            model_path="/tmp/bench.stl",
            printer_name="prusa",
            printer_id="prusa_mk4",
            profile_path="/tmp/profile.ini",
            skip_validation=False,
        )

    @patch("kiln.server._pipeline_benchmark")
    def test_skip_validation_passed_through(self, mock_pipeline):
        from kiln.server import run_benchmark

        mock_pipeline.return_value = _ok_result("benchmark")
        run_benchmark(model_path="/tmp/bench.stl", skip_validation=True)

        mock_pipeline.assert_called_once_with(
            model_path="/tmp/bench.stl",
            printer_name=None,
            printer_id=None,
            profile_path=None,
            skip_validation=True,
        )

    @patch("kiln.server._pipeline_benchmark", side_effect=ValueError("no model"))
    def test_unexpected_exception_returns_error_dict(self, mock_pipeline):
        from kiln.server import run_benchmark

        result = run_benchmark(model_path="/tmp/bench.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert "no model" in result["error"]["message"]

    def test_auth_failure(self):
        from kiln.server import run_benchmark

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = run_benchmark(model_path="/tmp/bench.stl")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# TestPipelineStatus
# ---------------------------------------------------------------------------


class TestPipelineStatus:
    """Tests for the pipeline_status() MCP tool."""

    def test_found_execution(self):
        from kiln.server import pipeline_status

        ex = _make_execution(PipelineState.RUNNING)
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_status(execution_id="test-exec-123")

        assert result["success"] is True
        assert result["execution_id"] == "test-exec-123"
        assert result["state"] == "running"
        assert result["pipeline"] == "test_pipeline"
        assert result["total_steps"] == 3

    def test_not_found(self):
        from kiln.server import pipeline_status

        with patch("kiln.server._get_execution", return_value=None):
            result = pipeline_status(execution_id="nonexistent")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        assert "nonexistent" in result["error"]["message"]

    def test_completed_execution(self):
        from kiln.server import pipeline_status

        ex = _make_execution(PipelineState.COMPLETED)
        ex.current_step = 3  # Past last step
        ex.steps = [
            PipelineStep(name="step_a", success=True, message="ok"),
            PipelineStep(name="step_b", success=True, message="ok"),
            PipelineStep(name="step_c", success=True, message="ok"),
        ]
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_status(execution_id="test-exec-123")

        assert result["success"] is True
        assert result["state"] == "completed"
        assert result["next_step"] is None
        assert len(result["completed_steps"]) == 3

    def test_paused_execution_shows_next_step(self):
        from kiln.server import pipeline_status

        ex = _make_execution(PipelineState.PAUSED)
        ex.current_step = 1
        ex.steps = [PipelineStep(name="step_a", success=True, message="ok")]
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_status(execution_id="test-exec-123")

        assert result["state"] == "paused"
        assert result["next_step"] == "step_b"
        assert result["current_step"] == 1

    def test_auth_failure(self):
        from kiln.server import pipeline_status

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = pipeline_status(execution_id="any")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# TestPipelinePause
# ---------------------------------------------------------------------------


class TestPipelinePause:
    """Tests for the pipeline_pause() MCP tool."""

    def test_pause_running_pipeline(self):
        from kiln.server import pipeline_pause

        ex = _make_execution(PipelineState.RUNNING)
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_pause(execution_id="test-exec-123")

        assert result["success"] is True
        assert "pause" in result["message"].lower()

    def test_pause_not_running_fails(self):
        from kiln.server import pipeline_pause

        for state in (PipelineState.PAUSED, PipelineState.COMPLETED, PipelineState.FAILED, PipelineState.ABORTED):
            ex = _make_execution(state)
            with patch("kiln.server._get_execution", return_value=ex):
                result = pipeline_pause(execution_id="test-exec-123")

            assert result["success"] is False
            assert result["error"]["code"] == "INVALID_STATE"
            assert state.value in result["error"]["message"]

    def test_pause_not_found(self):
        from kiln.server import pipeline_pause

        with patch("kiln.server._get_execution", return_value=None):
            result = pipeline_pause(execution_id="ghost")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_auth_failure(self):
        from kiln.server import pipeline_pause

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = pipeline_pause(execution_id="any")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# TestPipelineResume
# ---------------------------------------------------------------------------


class TestPipelineResume:
    """Tests for the pipeline_resume() MCP tool."""

    def test_resume_paused_pipeline(self):
        from kiln.server import pipeline_resume

        ex = _make_execution(PipelineState.PAUSED)
        mock_result = _ok_result("test_pipeline")
        ex.resume = MagicMock(return_value=mock_result)

        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_resume(execution_id="test-exec-123")

        assert result["success"] is True
        assert result["pipeline"] == "test_pipeline"
        ex.resume.assert_called_once()

    def test_resume_not_paused_fails(self):
        from kiln.server import pipeline_resume

        for state in (PipelineState.RUNNING, PipelineState.COMPLETED, PipelineState.FAILED, PipelineState.ABORTED):
            ex = _make_execution(state)
            with patch("kiln.server._get_execution", return_value=ex):
                result = pipeline_resume(execution_id="test-exec-123")

            assert result["success"] is False
            assert result["error"]["code"] == "INVALID_STATE"
            assert state.value in result["error"]["message"]

    def test_resume_not_found(self):
        from kiln.server import pipeline_resume

        with patch("kiln.server._get_execution", return_value=None):
            result = pipeline_resume(execution_id="ghost")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_auth_failure(self):
        from kiln.server import pipeline_resume

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = pipeline_resume(execution_id="any")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# TestPipelineAbort
# ---------------------------------------------------------------------------


class TestPipelineAbort:
    """Tests for the pipeline_abort() MCP tool."""

    def test_abort_running_pipeline(self):
        from kiln.server import pipeline_abort

        ex = _make_execution(PipelineState.RUNNING)
        mock_result = _fail_result("test_pipeline")
        mock_result.message = "Pipeline aborted by user."
        ex.abort = MagicMock(return_value=mock_result)

        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_abort(execution_id="test-exec-123")

        # pipeline_abort always returns success=False (pipeline didn't complete)
        assert result["success"] is False
        assert "abort" in result["message"].lower()
        ex.abort.assert_called_once()

    def test_abort_paused_pipeline(self):
        from kiln.server import pipeline_abort

        ex = _make_execution(PipelineState.PAUSED)
        mock_result = _fail_result("test_pipeline")
        mock_result.message = "Pipeline aborted by user."
        ex.abort = MagicMock(return_value=mock_result)

        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_abort(execution_id="test-exec-123")

        assert result["success"] is False
        ex.abort.assert_called_once()

    def test_abort_failed_pipeline(self):
        from kiln.server import pipeline_abort

        ex = _make_execution(PipelineState.FAILED)
        mock_result = _fail_result("test_pipeline")
        mock_result.message = "Pipeline aborted by user."
        ex.abort = MagicMock(return_value=mock_result)

        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_abort(execution_id="test-exec-123")

        assert result["success"] is False
        ex.abort.assert_called_once()

    def test_abort_completed_pipeline_fails(self):
        from kiln.server import pipeline_abort

        ex = _make_execution(PipelineState.COMPLETED)
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_abort(execution_id="test-exec-123")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_STATE"
        assert "completed" in result["error"]["message"]

    def test_abort_already_aborted_fails(self):
        from kiln.server import pipeline_abort

        ex = _make_execution(PipelineState.ABORTED)
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_abort(execution_id="test-exec-123")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_STATE"
        assert "aborted" in result["error"]["message"]

    def test_abort_not_found(self):
        from kiln.server import pipeline_abort

        with patch("kiln.server._get_execution", return_value=None):
            result = pipeline_abort(execution_id="ghost")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_auth_failure(self):
        from kiln.server import pipeline_abort

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = pipeline_abort(execution_id="any")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# TestPipelineRetryStep
# ---------------------------------------------------------------------------


class TestPipelineRetryStep:
    """Tests for the pipeline_retry_step() MCP tool."""

    def test_retry_failed_pipeline(self):
        from kiln.server import pipeline_retry_step

        ex = _make_execution(PipelineState.FAILED)
        mock_result = _ok_result("test_pipeline")
        ex.retry_step = MagicMock(return_value=mock_result)

        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_retry_step(execution_id="test-exec-123", step_index=0)

        assert result["success"] is True
        ex.retry_step.assert_called_once_with(0)

    def test_retry_paused_pipeline(self):
        from kiln.server import pipeline_retry_step

        ex = _make_execution(PipelineState.PAUSED)
        mock_result = _ok_result("test_pipeline")
        ex.retry_step = MagicMock(return_value=mock_result)

        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_retry_step(execution_id="test-exec-123", step_index=1)

        assert result["success"] is True
        ex.retry_step.assert_called_once_with(1)

    def test_retry_running_pipeline_fails(self):
        from kiln.server import pipeline_retry_step

        ex = _make_execution(PipelineState.RUNNING)
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_retry_step(execution_id="test-exec-123", step_index=0)

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_STATE"
        assert "running" in result["error"]["message"]

    def test_retry_completed_pipeline_fails(self):
        from kiln.server import pipeline_retry_step

        ex = _make_execution(PipelineState.COMPLETED)
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_retry_step(execution_id="test-exec-123", step_index=0)

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_STATE"

    def test_retry_aborted_pipeline_fails(self):
        from kiln.server import pipeline_retry_step

        ex = _make_execution(PipelineState.ABORTED)
        with patch("kiln.server._get_execution", return_value=ex):
            result = pipeline_retry_step(execution_id="test-exec-123", step_index=0)

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_STATE"

    def test_retry_not_found(self):
        from kiln.server import pipeline_retry_step

        with patch("kiln.server._get_execution", return_value=None):
            result = pipeline_retry_step(execution_id="ghost", step_index=0)

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_auth_failure(self):
        from kiln.server import pipeline_retry_step

        with patch("kiln.server._check_auth", return_value=_auth_error()):
            result = pipeline_retry_step(execution_id="any", step_index=0)

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"
