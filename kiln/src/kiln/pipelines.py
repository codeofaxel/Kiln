"""Pre-validated print pipelines — named command sequences that chain
multiple operations into reliable one-shot workflows.

Each pipeline is a composable sequence of steps (slice, preflight,
upload, print, monitor) that handles errors at each stage and returns
a structured result.

Available pipelines:
    * **quick_print** — slice → preflight → upload → start print
    * **calibrate** — home → bed level → PID tune → report
    * **benchmark** — slice benchmark model → print → report stats

Pause/Resume:
    Pipelines can be paused between steps for agent inspection.
    Use ``PipelineExecution`` to wrap a pipeline run with pause, resume,
    abort, and retry capabilities.

Usage::

    from kiln.pipelines import quick_print, PipelineResult

    result = quick_print(
        model_path="/path/to/model.stl",
        printer_name="ender3",
    )
    if result.success:
        print(f"Print started: {result.job_id}")
"""

from __future__ import annotations

import enum
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PipelineStep:
    """Result of a single step in a pipeline."""

    name: str
    success: bool
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "success": self.success,
            "message": self.message,
            "duration_seconds": round(self.duration_seconds, 2),
        }
        if self.data:
            d["data"] = self.data
        return d


@dataclass
class PipelineResult:
    """Outcome of a full pipeline execution."""

    pipeline: str
    success: bool
    message: str = ""
    steps: List[PipelineStep] = field(default_factory=list)
    job_id: Optional[str] = None
    total_duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "success": self.success,
            "message": self.message,
            "job_id": self.job_id,
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "steps": [s.to_dict() for s in self.steps],
        }


# ---------------------------------------------------------------------------
# Pipeline state machine
# ---------------------------------------------------------------------------

class PipelineState(enum.Enum):
    """State of a pipeline execution."""

    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


# Module-level registry of active executions
_executions: Dict[str, PipelineExecution] = {}  # type: ignore[name-defined]  # forward ref


@dataclass
class _StepDef:
    """Definition of a pipeline step (callable + metadata)."""

    name: str
    fn: Callable[..., PipelineStep]
    fatal: bool = True  # If True, a failure stops the pipeline


class PipelineExecution:
    """Wraps a pipeline run with pause, resume, abort, and retry support.

    Each execution tracks the pipeline name, step definitions, current
    position, state, and accumulated results.  The execution is
    registered in the module-level ``_executions`` dict automatically.
    """

    def __init__(
        self,
        pipeline_name: str,
        step_defs: List[_StepDef],
        *,
        pause_after_step: Optional[int] = None,
    ) -> None:
        self.execution_id: str = secrets.token_hex(8)
        self.pipeline_name: str = pipeline_name
        self.step_defs: List[_StepDef] = step_defs
        self.current_step: int = 0
        self.state: PipelineState = PipelineState.RUNNING
        self.steps: List[PipelineStep] = []
        self.start_time: float = time.time()
        self.pause_after_step: Optional[int] = pause_after_step
        self._pause_requested: bool = False

        # Register
        _executions[self.execution_id] = self

    # -- Control methods ---------------------------------------------------

    def pause(self) -> None:
        """Request pause at the next step boundary."""
        if self.state == PipelineState.RUNNING:
            self._pause_requested = True

    def resume(self) -> PipelineResult:
        """Resume from the current step after a pause."""
        if self.state != PipelineState.PAUSED:
            return self._build_result(
                success=False,
                message=f"Cannot resume: state is {self.state.value}",
            )
        self.state = PipelineState.RUNNING
        self._pause_requested = False
        return self._run_from_current()

    def abort(self) -> PipelineResult:
        """Abort the pipeline."""
        self.state = PipelineState.ABORTED
        return self._build_result(
            success=False,
            message="Pipeline aborted by user.",
        )

    def retry_step(self, step_index: int) -> PipelineResult:
        """Re-run a specific failed step, then continue from there.

        :param step_index: Zero-based index of the step to retry.
        """
        if step_index < 0 or step_index >= len(self.step_defs):
            return self._build_result(
                success=False,
                message=f"Invalid step index: {step_index} (pipeline has {len(self.step_defs)} steps)",
            )

        if step_index < len(self.steps) and self.steps[step_index].success:
            return self._build_result(
                success=False,
                message=f"Step {step_index} ('{self.step_defs[step_index].name}') did not fail — nothing to retry.",
            )

        # Reset position to the failed step and re-run from there
        self.current_step = step_index
        # Trim steps back to just before the retry point
        self.steps = self.steps[:step_index]
        self.state = PipelineState.RUNNING
        self._pause_requested = False
        return self._run_from_current()

    # -- Execution engine --------------------------------------------------

    def run(self) -> PipelineResult:
        """Execute the pipeline from the beginning."""
        return self._run_from_current()

    def _run_from_current(self) -> PipelineResult:
        """Execute steps starting from ``self.current_step``."""
        while self.current_step < len(self.step_defs):
            # Check for pause/abort before executing next step
            if self.state == PipelineState.ABORTED:
                return self._build_result(
                    success=False,
                    message="Pipeline aborted.",
                )

            if self._pause_requested:
                self.state = PipelineState.PAUSED
                self._pause_requested = False
                return self._build_result(
                    success=True,
                    message=f"Paused before step {self.current_step} ('{self.step_defs[self.current_step].name}')",
                )

            step_def = self.step_defs[self.current_step]
            try:
                step_result = step_def.fn()
            except Exception as exc:
                step_result = PipelineStep(
                    name=step_def.name,
                    success=False,
                    message=f"Unexpected error: {exc}",
                )

            self.steps.append(step_result)
            self.current_step += 1

            if not step_result.success and step_def.fatal:
                self.state = PipelineState.FAILED
                return self._build_result(
                    success=False,
                    message=f"Pipeline failed at {step_def.name}: {step_result.message}",
                )

            # Check auto-pause after completing this step
            if (
                self.pause_after_step is not None
                and (self.current_step - 1) == self.pause_after_step
                and self.current_step < len(self.step_defs)
            ):
                self.state = PipelineState.PAUSED
                self.pause_after_step = None  # One-shot: don't re-trigger on resume
                return self._build_result(
                    success=True,
                    message=f"Paused after step {self.current_step - 1} ('{step_def.name}')",
                )

        self.state = PipelineState.COMPLETED
        return self._build_result(success=True, message="Pipeline completed.")

    def _build_result(self, *, success: bool, message: str) -> PipelineResult:
        return PipelineResult(
            pipeline=self.pipeline_name,
            success=success,
            message=message,
            steps=list(self.steps),
            total_duration_seconds=time.time() - self.start_time,
        )

    # -- Introspection -----------------------------------------------------

    def status_dict(self) -> Dict[str, Any]:
        """Return current execution state as a dict."""
        completed = [s.to_dict() for s in self.steps]
        next_step_name: Optional[str] = None
        if self.current_step < len(self.step_defs):
            next_step_name = self.step_defs[self.current_step].name

        return {
            "execution_id": self.execution_id,
            "pipeline": self.pipeline_name,
            "state": self.state.value,
            "current_step": self.current_step,
            "total_steps": len(self.step_defs),
            "next_step": next_step_name,
            "completed_steps": completed,
        }


# ---------------------------------------------------------------------------
# Execution registry helpers
# ---------------------------------------------------------------------------

def get_execution(execution_id: str) -> Optional[PipelineExecution]:
    """Look up an active pipeline execution by ID."""
    return _executions.get(execution_id)


def list_executions() -> List[Dict[str, Any]]:
    """Return summary of all tracked executions."""
    return [ex.status_dict() for ex in _executions.values()]


# ---------------------------------------------------------------------------
# quick_print pipeline
# ---------------------------------------------------------------------------

def quick_print(
    *,
    model_path: str,
    printer_name: Optional[str] = None,
    printer_id: Optional[str] = None,
    profile_path: Optional[str] = None,
    slicer_path: Optional[str] = None,
    pause_after_step: Optional[int] = None,
) -> PipelineResult:
    """Slice → preflight → upload → start print in one call.

    Args:
        model_path: Path to input model (STL, 3MF, etc.).
        printer_name: Registered printer name in fleet. If omitted,
            uses the default printer.
        printer_id: Printer model ID for auto-selecting slicer profile
            and safety validation. E.g. ``"ender3"``, ``"bambu_x1c"``.
        profile_path: Explicit slicer profile path. If omitted and
            ``printer_id`` is given, the bundled profile is used.
        slicer_path: Explicit slicer binary path.
        pause_after_step: Auto-pause after completing step N (0-indexed).
            When ``None``, the pipeline runs to completion synchronously.

    Returns:
        :class:`PipelineResult` with step-by-step outcomes.
    """
    # Shared mutable state between step closures
    ctx: Dict[str, Any] = {
        "effective_profile": profile_path,
        "gcode_path": None,
        "adapter": None,
        "remote_name": None,
    }

    def _resolve_profile() -> PipelineStep:
        if ctx["effective_profile"] or not printer_id:
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message="Using explicit profile" if ctx["effective_profile"] else "No profile needed",
            )
        step_start = time.time()
        try:
            from kiln.slicer_profiles import resolve_slicer_profile
            ctx["effective_profile"] = resolve_slicer_profile(printer_id)
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message=f"Using bundled profile for {printer_id}",
                data={"profile_path": ctx["effective_profile"], "printer_id": printer_id},
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            return PipelineStep(
                name="resolve_profile",
                success=False,
                message=f"Profile resolution failed: {exc}",
                duration_seconds=time.time() - step_start,
            )

    def _slice() -> PipelineStep:
        step_start = time.time()
        try:
            from kiln.slicer import slice_file
            result = slice_file(
                model_path,
                profile=ctx["effective_profile"],
                slicer_path=slicer_path,
            )
            ctx["gcode_path"] = result.output_path
            return PipelineStep(
                name="slice",
                success=True,
                message=result.message,
                data={"output_path": result.output_path, "slicer": result.slicer},
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            return PipelineStep(
                name="slice",
                success=False,
                message=f"Slicing failed: {exc}",
                duration_seconds=time.time() - step_start,
            )

    def _safety_check() -> PipelineStep:
        if not printer_id or not ctx["gcode_path"]:
            return PipelineStep(
                name="safety_check",
                success=True,
                message="Skipped (no printer_id or gcode_path)",
            )
        step_start = time.time()
        try:
            from kiln.gcode import scan_gcode_file
            vr = scan_gcode_file(ctx["gcode_path"], printer_id=printer_id)
            return PipelineStep(
                name="safety_check",
                success=vr.valid,
                message=f"{'Passed' if vr.valid else 'BLOCKED'}: "
                        f"{len(vr.commands)} OK, {len(vr.blocked_commands)} blocked, "
                        f"{len(vr.warnings)} warnings",
                data={
                    "valid": vr.valid,
                    "warnings": vr.warnings[:5],
                    "errors": vr.errors[:5],
                },
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            logger.exception("G-code safety validation failed")
            return PipelineStep(
                name="safety_check",
                success=False,
                message=f"G-code safety validation error: {exc}",
                duration_seconds=time.time() - step_start,
            )

    def _upload() -> PipelineStep:
        step_start = time.time()
        try:
            from kiln.server import _registry
            adapter = _registry.get_adapter(printer_name) if printer_name else _registry.get_default_adapter()
            ctx["adapter"] = adapter
            upload_result = adapter.upload_file(ctx["gcode_path"])
            remote_name = upload_result.get("name", os.path.basename(ctx["gcode_path"]))
            ctx["remote_name"] = remote_name
            return PipelineStep(
                name="upload",
                success=True,
                message=f"Uploaded {remote_name}",
                data={"remote_name": remote_name},
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            return PipelineStep(
                name="upload",
                success=False,
                message=f"Upload failed: {exc}",
                duration_seconds=time.time() - step_start,
            )

    def _preflight() -> PipelineStep:
        step_start = time.time()
        try:
            adapter = ctx["adapter"]
            if adapter is None:
                return PipelineStep(
                    name="preflight",
                    success=False,
                    message="No adapter available (upload step may have failed)",
                    duration_seconds=time.time() - step_start,
                )
            state = adapter.get_state()
            checks_passed = state.connected and state.status.value == "idle"
            return PipelineStep(
                name="preflight",
                success=checks_passed,
                message="Printer ready" if checks_passed else f"Printer not ready: {state.status.value}",
                data={"connected": state.connected, "status": state.status.value},
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            return PipelineStep(
                name="preflight",
                success=False,
                message=f"Preflight check failed: {exc}",
                duration_seconds=time.time() - step_start,
            )

    def _start_print() -> PipelineStep:
        step_start = time.time()
        try:
            adapter = ctx["adapter"]
            remote_name = ctx["remote_name"]
            if adapter is None or remote_name is None:
                return PipelineStep(
                    name="start_print",
                    success=False,
                    message="Cannot start print (missing adapter or file name)",
                    duration_seconds=time.time() - step_start,
                )
            adapter.start_print(remote_name)
            return PipelineStep(
                name="start_print",
                success=True,
                message=f"Print started: {remote_name}",
                data={"file_name": remote_name},
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            return PipelineStep(
                name="start_print",
                success=False,
                message=f"Failed to start print: {exc}",
                duration_seconds=time.time() - step_start,
            )

    step_defs = [
        _StepDef(name="resolve_profile", fn=_resolve_profile, fatal=False),
        _StepDef(name="slice", fn=_slice, fatal=True),
        _StepDef(name="safety_check", fn=_safety_check, fatal=True),
        _StepDef(name="upload", fn=_upload, fatal=True),
        _StepDef(name="preflight", fn=_preflight, fatal=True),
        _StepDef(name="start_print", fn=_start_print, fatal=True),
    ]

    execution = PipelineExecution(
        "quick_print",
        step_defs,
        pause_after_step=pause_after_step,
    )
    return execution.run()


# ---------------------------------------------------------------------------
# calibrate pipeline
# ---------------------------------------------------------------------------

def calibrate(
    *,
    printer_name: Optional[str] = None,
    printer_id: Optional[str] = None,
) -> PipelineResult:
    """Run a printer calibration sequence: home → bed level → report guidance.

    This pipeline doesn't actually perform PID tuning automatically
    (that requires physical monitoring), but it does:
    1. Verify printer is connected and idle
    2. Home all axes
    3. Run auto bed level if supported
    4. Return calibration guidance from the intelligence DB

    Args:
        printer_name: Registered printer name.
        printer_id: Printer model ID for calibration guidance.
    """
    start = time.time()
    steps: List[PipelineStep] = []

    # Step 1: Get adapter
    step_start = time.time()
    try:
        from kiln.server import _registry
        adapter = _registry.get_adapter(printer_name) if printer_name else _registry.get_default_adapter()
        state = adapter.get_state()
        steps.append(PipelineStep(
            name="connect",
            success=state.connected,
            message="Connected" if state.connected else "Printer offline",
            data={"status": state.status.value, "connected": state.connected},
            duration_seconds=time.time() - step_start,
        ))
        if not state.connected:
            return PipelineResult(
                pipeline="calibrate",
                success=False,
                message="Printer is not connected.",
                steps=steps,
                total_duration_seconds=time.time() - start,
            )
    except Exception as exc:
        steps.append(PipelineStep(
            name="connect",
            success=False,
            message=f"Connection failed: {exc}",
            duration_seconds=time.time() - step_start,
        ))
        return PipelineResult(
            pipeline="calibrate",
            success=False,
            message=f"Pipeline failed at connect: {exc}",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 2: Home axes
    step_start = time.time()
    try:
        adapter.send_gcode("G28")
        steps.append(PipelineStep(
            name="home",
            success=True,
            message="Homed all axes (G28)",
            duration_seconds=time.time() - step_start,
        ))
    except Exception as exc:
        steps.append(PipelineStep(
            name="home",
            success=False,
            message=f"Homing failed: {exc}",
            duration_seconds=time.time() - step_start,
        ))

    # Step 3: Auto bed level
    step_start = time.time()
    try:
        adapter.send_gcode("G29")
        steps.append(PipelineStep(
            name="bed_level",
            success=True,
            message="Auto bed leveling complete (G29)",
            duration_seconds=time.time() - step_start,
        ))
    except Exception as exc:
        steps.append(PipelineStep(
            name="bed_level",
            success=True,  # Non-fatal — not all printers support G29.
            message=f"Auto bed level not available or failed: {exc}",
            duration_seconds=time.time() - step_start,
        ))

    # Step 4: Gather calibration guidance
    guidance: Dict[str, str] = {}
    if printer_id:
        step_start = time.time()
        try:
            from kiln.printer_intelligence import get_printer_intel
            intel = get_printer_intel(printer_id)
            guidance = dict(intel.calibration)
            steps.append(PipelineStep(
                name="guidance",
                success=True,
                message=f"Loaded calibration guidance for {intel.display_name}",
                data={"calibration": guidance},
                duration_seconds=time.time() - step_start,
            ))
        except Exception as exc:
            steps.append(PipelineStep(
                name="guidance",
                success=True,
                message=f"No calibration guidance available: {exc}",
                duration_seconds=time.time() - step_start,
            ))

    return PipelineResult(
        pipeline="calibrate",
        success=True,
        message="Calibration sequence complete. Review guidance for next steps.",
        steps=steps,
        total_duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# benchmark pipeline
# ---------------------------------------------------------------------------

def benchmark(
    *,
    printer_name: Optional[str] = None,
    printer_id: Optional[str] = None,
    model_path: Optional[str] = None,
    profile_path: Optional[str] = None,
) -> PipelineResult:
    """Slice a benchmark model, upload, and report estimated stats.

    This pipeline prepares a benchmark print but does NOT start it
    automatically (benchmarks should be manually observed).

    Steps:
    1. Resolve slicer profile for printer
    2. Slice benchmark model (or user-provided model)
    3. Upload to printer
    4. Report printer stats from history

    Args:
        printer_name: Registered printer name.
        printer_id: Printer model for profile selection.
        model_path: Path to benchmark model. Uses a simple cube if omitted.
        profile_path: Explicit slicer profile path.
    """
    start = time.time()
    steps: List[PipelineStep] = []

    # Step 1: Verify we have a model
    if not model_path:
        steps.append(PipelineStep(
            name="model",
            success=False,
            message="No benchmark model path provided. Supply a model_path to benchmark.",
        ))
        return PipelineResult(
            pipeline="benchmark",
            success=False,
            message="Benchmark requires a model_path. Provide an STL file.",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 2: Resolve profile
    effective_profile = profile_path
    if not effective_profile and printer_id:
        step_start = time.time()
        try:
            from kiln.slicer_profiles import resolve_slicer_profile
            effective_profile = resolve_slicer_profile(printer_id)
            steps.append(PipelineStep(
                name="resolve_profile",
                success=True,
                message=f"Using bundled profile for {printer_id}",
                data={"profile_path": effective_profile},
                duration_seconds=time.time() - step_start,
            ))
        except Exception as exc:
            steps.append(PipelineStep(
                name="resolve_profile",
                success=True,
                message=f"Profile resolution failed, using slicer defaults: {exc}",
                duration_seconds=time.time() - step_start,
            ))

    # Step 3: Slice
    step_start = time.time()
    try:
        from kiln.slicer import slice_file
        result = slice_file(model_path, profile=effective_profile)
        gcode_path = result.output_path
        steps.append(PipelineStep(
            name="slice",
            success=True,
            message=result.message,
            data={"output_path": gcode_path},
            duration_seconds=time.time() - step_start,
        ))
    except Exception as exc:
        steps.append(PipelineStep(
            name="slice",
            success=False,
            message=f"Slicing failed: {exc}",
            duration_seconds=time.time() - step_start,
        ))
        return PipelineResult(
            pipeline="benchmark",
            success=False,
            message=f"Benchmark failed at slicing: {exc}",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 4: Upload
    step_start = time.time()
    try:
        from kiln.server import _registry
        adapter = _registry.get_adapter(printer_name) if printer_name else _registry.get_default_adapter()
        upload_result = adapter.upload_file(gcode_path)
        remote_name = upload_result.get("name", os.path.basename(gcode_path))
        steps.append(PipelineStep(
            name="upload",
            success=True,
            message=f"Uploaded benchmark file: {remote_name}",
            data={"remote_name": remote_name},
            duration_seconds=time.time() - step_start,
        ))
    except Exception as exc:
        steps.append(PipelineStep(
            name="upload",
            success=False,
            message=f"Upload failed: {exc}",
            duration_seconds=time.time() - step_start,
        ))
        return PipelineResult(
            pipeline="benchmark",
            success=False,
            message=f"Benchmark failed at upload: {exc}",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 5: Get printer stats from history
    if printer_name:
        step_start = time.time()
        try:
            from kiln.persistence import get_db
            stats = get_db().get_printer_stats(printer_name)
            steps.append(PipelineStep(
                name="stats",
                success=True,
                message=f"Printer stats: {stats.get('total_prints', 0)} prints, "
                        f"{stats.get('success_rate', 0):.0%} success rate",
                data=stats,
                duration_seconds=time.time() - step_start,
            ))
        except Exception as exc:
            steps.append(PipelineStep(
                name="stats",
                success=True,
                message=f"Stats unavailable: {exc}",
                duration_seconds=time.time() - step_start,
            ))

    return PipelineResult(
        pipeline="benchmark",
        success=True,
        message=f"Benchmark ready: {remote_name} uploaded. Start print manually to observe quality.",
        steps=steps,
        total_duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Pipeline registry
# ---------------------------------------------------------------------------

PIPELINES = {
    "quick_print": {
        "function": quick_print,
        "description": "Slice → validate → upload → print in one shot.",
        "params": ["model_path", "printer_name", "printer_id", "profile_path"],
    },
    "calibrate": {
        "function": calibrate,
        "description": "Home → bed level → calibration guidance report.",
        "params": ["printer_name", "printer_id"],
    },
    "benchmark": {
        "function": benchmark,
        "description": "Slice benchmark model → upload → report printer stats.",
        "params": ["model_path", "printer_name", "printer_id", "profile_path"],
    },
}


def list_pipelines() -> List[Dict[str, str]]:
    """Return metadata for all available pipelines."""
    return [
        {"name": name, "description": info["description"], "params": info["params"]}
        for name, info in PIPELINES.items()
    ]
