"""Tests for pipeline pause/resume/abort/retry via PipelineExecution.

Covers:
    - PipelineState enum values
    - PipelineExecution creation and registration
    - Full synchronous run (no pause)
    - Auto-pause after a specific step
    - Manual pause via pause() method
    - Resume from paused state
    - Abort from running and paused states
    - Retry of a failed step
    - Retry of a non-existent step index
    - Retry of a succeeded step (error)
    - Resume when not paused (error)
    - status_dict() introspection
    - get_execution() lookup
    - list_executions() summary
    - Fatal vs non-fatal step handling
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln.pipelines import (
    PipelineExecution,
    PipelineState,
    PipelineStep,
    PipelineResult,
    _StepDef,
    _executions,
    get_execution,
    list_executions,
)


@pytest.fixture(autouse=True)
def _clear_executions():
    """Clear the global execution registry before/after each test."""
    _executions.clear()
    yield
    _executions.clear()


def _ok_step(name: str = "ok") -> _StepDef:
    """Helper: create a step that always succeeds."""
    def fn() -> PipelineStep:
        return PipelineStep(name=name, success=True, message=f"{name} done")
    return _StepDef(name=name, fn=fn, fatal=True)


def _fail_step(name: str = "fail") -> _StepDef:
    """Helper: create a step that always fails."""
    def fn() -> PipelineStep:
        return PipelineStep(name=name, success=False, message=f"{name} failed")
    return _StepDef(name=name, fn=fn, fatal=True)


def _nonfatal_fail_step(name: str = "warn") -> _StepDef:
    """Helper: step that fails but is non-fatal."""
    def fn() -> PipelineStep:
        return PipelineStep(name=name, success=False, message=f"{name} failed (non-fatal)")
    return _StepDef(name=name, fn=fn, fatal=False)


def _exception_step(name: str = "boom") -> _StepDef:
    """Helper: step that raises an exception."""
    def fn() -> PipelineStep:
        raise RuntimeError("kaboom")
    return _StepDef(name=name, fn=fn, fatal=True)


# ===================================================================
# PipelineState enum
# ===================================================================

class TestPipelineState:
    """PipelineState enum values."""

    def test_all_values(self) -> None:
        assert PipelineState.RUNNING.value == "running"
        assert PipelineState.PAUSED.value == "paused"
        assert PipelineState.COMPLETED.value == "completed"
        assert PipelineState.FAILED.value == "failed"
        assert PipelineState.ABORTED.value == "aborted"

    def test_member_count(self) -> None:
        assert len(PipelineState) == 5


# ===================================================================
# PipelineExecution — basic lifecycle
# ===================================================================

class TestPipelineExecutionBasic:
    """Basic creation, run, and registration."""

    def test_creation_generates_unique_id(self) -> None:
        ex1 = PipelineExecution("test", [_ok_step()])
        ex2 = PipelineExecution("test", [_ok_step()])
        assert ex1.execution_id != ex2.execution_id

    def test_registered_on_creation(self) -> None:
        ex = PipelineExecution("test", [_ok_step()])
        assert _executions[ex.execution_id] is ex

    def test_initial_state_is_running(self) -> None:
        ex = PipelineExecution("test", [_ok_step()])
        assert ex.state == PipelineState.RUNNING

    def test_run_all_steps_completes(self) -> None:
        steps = [_ok_step("a"), _ok_step("b"), _ok_step("c")]
        ex = PipelineExecution("test", steps)
        result = ex.run()
        assert result.success is True
        assert result.pipeline == "test"
        assert len(result.steps) == 3
        assert ex.state == PipelineState.COMPLETED

    def test_empty_pipeline_completes(self) -> None:
        ex = PipelineExecution("empty", [])
        result = ex.run()
        assert result.success is True
        assert ex.state == PipelineState.COMPLETED


# ===================================================================
# Fatal step failures
# ===================================================================

class TestPipelineExecutionFatalFailure:
    """Pipeline stops on fatal step failure."""

    def test_fatal_step_stops_pipeline(self) -> None:
        steps = [_ok_step("a"), _fail_step("b"), _ok_step("c")]
        ex = PipelineExecution("test", steps)
        result = ex.run()
        assert result.success is False
        assert ex.state == PipelineState.FAILED
        assert len(result.steps) == 2  # a succeeded, b failed, c never ran
        assert "b" in result.message

    def test_exception_in_step_treated_as_failure(self) -> None:
        steps = [_ok_step("a"), _exception_step("boom")]
        ex = PipelineExecution("test", steps)
        result = ex.run()
        assert result.success is False
        assert ex.state == PipelineState.FAILED
        assert "kaboom" in result.steps[1].message

    def test_nonfatal_failure_continues(self) -> None:
        steps = [_nonfatal_fail_step("warn"), _ok_step("b")]
        ex = PipelineExecution("test", steps)
        result = ex.run()
        assert result.success is True
        assert len(result.steps) == 2
        assert result.steps[0].success is False
        assert result.steps[1].success is True


# ===================================================================
# Pause / Resume
# ===================================================================

class TestPipelineExecutionPauseResume:
    """Pause and resume execution."""

    def test_auto_pause_after_step(self) -> None:
        steps = [_ok_step("a"), _ok_step("b"), _ok_step("c")]
        ex = PipelineExecution("test", steps, pause_after_step=0)
        result = ex.run()
        # Should pause after step 0 ("a")
        assert ex.state == PipelineState.PAUSED
        assert result.success is True
        assert len(result.steps) == 1
        assert result.steps[0].name == "a"
        assert "Paused" in result.message

    def test_resume_after_auto_pause(self) -> None:
        steps = [_ok_step("a"), _ok_step("b"), _ok_step("c")]
        ex = PipelineExecution("test", steps, pause_after_step=0)
        ex.run()
        assert ex.state == PipelineState.PAUSED

        result = ex.resume()
        assert result.success is True
        assert ex.state == PipelineState.COMPLETED
        assert len(result.steps) == 3  # all three steps

    def test_manual_pause_request(self) -> None:
        # We can't truly test mid-execution pause in sync code,
        # but we can test that calling pause() before run causes
        # it to pause before the first step.
        steps = [_ok_step("a"), _ok_step("b")]
        ex = PipelineExecution("test", steps)
        ex.pause()
        result = ex.run()
        assert ex.state == PipelineState.PAUSED
        assert len(result.steps) == 0  # paused before executing any step

    def test_resume_when_not_paused_returns_error(self) -> None:
        steps = [_ok_step("a")]
        ex = PipelineExecution("test", steps)
        ex.run()  # completes
        result = ex.resume()
        assert result.success is False
        assert "Cannot resume" in result.message

    def test_auto_pause_after_last_step_completes_normally(self) -> None:
        steps = [_ok_step("a"), _ok_step("b")]
        ex = PipelineExecution("test", steps, pause_after_step=1)
        result = ex.run()
        # pause_after_step=1 is the last step, no more steps to pause before
        assert ex.state == PipelineState.COMPLETED
        assert result.success is True


# ===================================================================
# Abort
# ===================================================================

class TestPipelineExecutionAbort:
    """Abort pipeline execution."""

    def test_abort_paused_pipeline(self) -> None:
        steps = [_ok_step("a"), _ok_step("b")]
        ex = PipelineExecution("test", steps, pause_after_step=0)
        ex.run()
        assert ex.state == PipelineState.PAUSED

        result = ex.abort()
        assert result.success is False
        assert ex.state == PipelineState.ABORTED
        assert "aborted" in result.message.lower()

    def test_abort_preserves_completed_steps(self) -> None:
        steps = [_ok_step("a"), _ok_step("b"), _ok_step("c")]
        ex = PipelineExecution("test", steps, pause_after_step=1)
        ex.run()
        result = ex.abort()
        assert len(result.steps) == 2  # a and b completed before abort


# ===================================================================
# Retry
# ===================================================================

class TestPipelineExecutionRetry:
    """Retry a specific failed step."""

    def test_retry_failed_step_succeeds(self) -> None:
        call_count = {"n": 0}

        def flaky_fn() -> PipelineStep:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return PipelineStep(name="flaky", success=False, message="first try failed")
            return PipelineStep(name="flaky", success=True, message="retry succeeded")

        steps = [_ok_step("a"), _StepDef(name="flaky", fn=flaky_fn, fatal=True), _ok_step("c")]
        ex = PipelineExecution("test", steps)
        result = ex.run()
        assert result.success is False
        assert ex.state == PipelineState.FAILED

        result = ex.retry_step(1)
        assert result.success is True
        assert ex.state == PipelineState.COMPLETED
        assert len(result.steps) == 3

    def test_retry_invalid_index(self) -> None:
        steps = [_ok_step("a")]
        ex = PipelineExecution("test", steps)
        ex.run()
        # Force state to failed so retry is allowed
        ex.state = PipelineState.FAILED
        result = ex.retry_step(99)
        assert result.success is False
        assert "Invalid step index" in result.message

    def test_retry_negative_index(self) -> None:
        steps = [_ok_step("a")]
        ex = PipelineExecution("test", steps)
        ex.state = PipelineState.FAILED
        result = ex.retry_step(-1)
        assert result.success is False
        assert "Invalid step index" in result.message

    def test_retry_succeeded_step_returns_error(self) -> None:
        steps = [_ok_step("a"), _fail_step("b")]
        ex = PipelineExecution("test", steps)
        ex.run()
        assert ex.state == PipelineState.FAILED
        result = ex.retry_step(0)  # step 0 succeeded
        assert result.success is False
        assert "did not fail" in result.message


# ===================================================================
# Status introspection
# ===================================================================

class TestPipelineExecutionStatus:
    """status_dict() and get_execution() / list_executions()."""

    def test_status_dict_fields(self) -> None:
        steps = [_ok_step("a"), _ok_step("b")]
        ex = PipelineExecution("test", steps, pause_after_step=0)
        ex.run()

        sd = ex.status_dict()
        assert sd["execution_id"] == ex.execution_id
        assert sd["pipeline"] == "test"
        assert sd["state"] == "paused"
        assert sd["current_step"] == 1
        assert sd["total_steps"] == 2
        assert sd["next_step"] == "b"
        assert len(sd["completed_steps"]) == 1

    def test_status_dict_completed_pipeline(self) -> None:
        ex = PipelineExecution("test", [_ok_step("a")])
        ex.run()
        sd = ex.status_dict()
        assert sd["state"] == "completed"
        assert sd["next_step"] is None

    def test_get_execution_found(self) -> None:
        ex = PipelineExecution("test", [_ok_step()])
        assert get_execution(ex.execution_id) is ex

    def test_get_execution_not_found(self) -> None:
        assert get_execution("nonexistent") is None

    def test_list_executions(self) -> None:
        PipelineExecution("a", [_ok_step()])
        PipelineExecution("b", [_ok_step()])
        result = list_executions()
        assert len(result) == 2
        names = {r["pipeline"] for r in result}
        assert names == {"a", "b"}


# ===================================================================
# quick_print with pause_after_step
# ===================================================================

class TestQuickPrintPauseAfterStep:
    """quick_print() with pause_after_step parameter."""

    @patch("kiln.pipelines.time.time", return_value=1000.0)
    def test_pause_after_step_zero(self, _mock_time) -> None:
        """quick_print pauses after resolve_profile when pause_after_step=0."""
        from kiln.pipelines import quick_print

        # The first step (resolve_profile) needs no external deps when
        # no printer_id is given and profile_path is explicit.
        result = quick_print(
            model_path="/fake/model.stl",
            profile_path="/fake/profile.ini",
            pause_after_step=0,
        )
        # Should pause after step 0 (resolve_profile)
        assert len(result.steps) == 1
        assert result.steps[0].name == "resolve_profile"
        assert "Paused" in result.message

    @patch("kiln.pipelines.time.time", return_value=1000.0)
    def test_pause_registers_execution(self, _mock_time) -> None:
        """Paused quick_print is findable in the execution registry."""
        from kiln.pipelines import quick_print

        quick_print(
            model_path="/fake/model.stl",
            profile_path="/fake/profile.ini",
            pause_after_step=0,
        )
        execs = list_executions()
        assert len(execs) == 1
        assert execs[0]["pipeline"] == "quick_print"
        assert execs[0]["state"] == "paused"
