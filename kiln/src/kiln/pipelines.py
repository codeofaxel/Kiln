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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kiln.print_start_verdict import resolve_print_start
from kiln.printers.base import PrinterStatus

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
    data: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
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
    steps: list[PipelineStep] = field(default_factory=list)
    job_id: str | None = None
    total_duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "success": self.success,
            "message": self.message,
            "job_id": self.job_id,
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "steps": [s.to_dict() for s in self.steps],
        }


def _start_print_step_message(verdict: Any, remote_name: str) -> str:
    """The start_print step's one-line message, matching its verdict.

    Both pipelines that start a print say the same three things, so they say
    them from one place — the step used to report "Print started" whatever the
    printer answered, including when it answered nothing.
    """
    if verdict.confirmed:
        return f"Print started: {remote_name}"
    if verdict.ok:
        return f"Print command accepted, not yet confirmed running: {remote_name}"
    return f"Printer did not start {remote_name}. {verdict.message}"


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
_executions: dict[str, PipelineExecution] = {}  # type: ignore[name-defined]  # forward ref


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
        step_defs: list[_StepDef],
        *,
        pause_after_step: int | None = None,
    ) -> None:
        self.execution_id: str = secrets.token_hex(8)
        self.pipeline_name: str = pipeline_name
        self.step_defs: list[_StepDef] = step_defs
        self.current_step: int = 0
        self.state: PipelineState = PipelineState.RUNNING
        self.steps: list[PipelineStep] = []
        self.start_time: float = time.time()
        self.pause_after_step: int | None = pause_after_step
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

    def status_dict(self) -> dict[str, Any]:
        """Return current execution state as a dict."""
        completed = [s.to_dict() for s in self.steps]
        next_step_name: str | None = None
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


def get_execution(execution_id: str) -> PipelineExecution | None:
    """Look up an active pipeline execution by ID."""
    return _executions.get(execution_id)


def list_executions() -> list[dict[str, Any]]:
    """Return summary of all tracked executions."""
    return [ex.status_dict() for ex in _executions.values()]


# ---------------------------------------------------------------------------
# Shared step helpers
# ---------------------------------------------------------------------------


def _resolve_pipeline_adapter(printer_name: str | None) -> Any:
    """Resolve the adapter a pipeline step should talk to.

    Every upload step here used to reach for the raw ``kiln.server``
    registry global directly and call a pair of methods on it that
    ``PrinterRegistry`` has never had (its API is ``get(name)``) — and
    the global itself stays ``None`` until the server's registry
    initialiser runs.  Both branches raised, the exception was swallowed
    into a failed "Upload failed" step, and every pipeline upload with a
    registered printer failed.  The same mistake was already found and
    fixed at its other occurrence (``retry_print_with_fix`` — see the
    note in smart_print_tools); this is the shared door for the
    pipelines: ``_resolve_adapter`` initialises the registry, handles
    the default-printer case for a falsy name, and falls back to
    config.yaml.
    """
    from kiln.server import _resolve_adapter

    return _resolve_adapter(printer_name)


def _run_stability_check(model_path: str, ctx: dict[str, Any]) -> PipelineStep:
    """Check model stability and warn if orientation is risky.

    This is informational only — it sets ``ctx["stability_warning"]`` but
    never fails the pipeline.
    """
    step_start = time.time()

    # Only check mesh files — skip pre-sliced gcode/3mf
    if not model_path.lower().endswith((".stl", ".obj")):
        ctx["stability_warning"] = None
        return PipelineStep(
            name="stability_check",
            success=True,
            message="Skipped (not a mesh file)",
            duration_seconds=time.time() - step_start,
        )

    try:
        from kiln.auto_orient import check_stability

        result = check_stability(model_path)
        ctx["stability_warning"] = None
        if not result.stable:
            warning = (
                f"\u26a0 Stability risk ({result.risk_level}): "
                f"{result.recommendation}"
            )
            ctx["stability_warning"] = warning
            logger.warning(
                "Stability check: %s (ratio=%.1f)",
                result.risk_level,
                result.height_to_base_ratio,
            )
        return PipelineStep(
            name="stability_check",
            success=True,
            message=ctx["stability_warning"] or "Model orientation looks stable",
            data={
                "stable": result.stable,
                "risk_level": result.risk_level,
                "height_to_base_ratio": result.height_to_base_ratio,
            },
            duration_seconds=time.time() - step_start,
        )
    except Exception as exc:
        logger.debug("Stability check skipped: %s", exc, exc_info=True)
        ctx["stability_warning"] = None
        return PipelineStep(
            name="stability_check",
            success=True,
            message="Stability check skipped (analysis unavailable)",
            duration_seconds=time.time() - step_start,
        )


def _target_printer_id(printer_id: str | None, printer_name: str | None) -> str | None:
    """The printer-model id EVERY step of an aimed pipeline should use.

    A pipeline is one job aimed at one machine, so the profile it slices
    with, the bed it is measured against and the firmware limits its
    G-code is scanned for have to name the same printer.  Callers who
    aim by ``printer_name`` never passed ``printer_id``, which left the
    bed-fit and safety steps with nothing to check against — they
    silently skipped while the slice went ahead.

    Resolved through the server's one resolver so a pipeline and a
    single-shot tool cannot disagree about what a printer is.  Falls
    back to the caller's own ``printer_id`` if the server module is
    unavailable (import cycles during partial initialisation).
    """
    try:
        import kiln.server as _server

        return _server._resolve_printer_profile_id(printer_id, printer_name)
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        logger.debug("Target printer resolution failed: %s", exc)
        return printer_id


# ---------------------------------------------------------------------------
# quick_print pipeline
# ---------------------------------------------------------------------------


def quick_print(
    *,
    model_path: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    profile_path: str | None = None,
    slicer_path: str | None = None,
    pause_after_step: int | None = None,
    material: str | None = None,
    use_ams: bool | None = None,
    ams_mapping: list[int] | None = None,
    skip_validation: bool = False,
) -> PipelineResult:
    """Validate → slice → preflight → upload → start print in one call.

    The pipeline pre-tests the mesh for printability before slicing
    (manifold, walls, overhangs, bridges, bed-fit, material).  Auto-repairs
    non-manifold meshes; the slicer runs against the repaired path.  Designs
    that fail the gate are blocked at the validation step with a clear
    next_action — they never reach the printer.

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
        skip_validation: Bypass the pre-print validation step.  Defaults
            to False — designs are pre-tested for printability before
            they reach the printer.  Use True only for already-validated
            inputs or pre-sliced 3MFs the validator can't introspect.

    Returns:
        :class:`PipelineResult` with step-by-step outcomes.
    """
    # Shared mutable state between step closures.
    # ctx["model_path"] is the EFFECTIVE path used by downstream steps —
    # the validation step may update it to point at an auto-repaired or
    # auto-scaled mesh.
    ctx: dict[str, Any] = {
        "effective_profile": profile_path,
        "gcode_path": None,
        "adapter": None,
        "remote_name": None,
        "model_path": model_path,
        "validation_report": None,
    }
    effective_pid = _target_printer_id(printer_id, printer_name)

    def _validate_mesh() -> PipelineStep:
        step_start = time.time()
        if skip_validation:
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message="Validation skipped (skip_validation=True)",
                duration_seconds=time.time() - step_start,
            )

        try:
            from kiln.plugins._validation_pipeline_internals import (
                _SUPPORTED_FORMATS,
            )
            from kiln.plugins.validation_pipeline_tools import (
                run_full_validation_pipeline,
            )
        except ImportError as exc:
            logger.debug("Validation pipeline import failed: %s", exc, exc_info=True)
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message="Validation skipped (pipeline unavailable)",
                duration_seconds=time.time() - step_start,
            )

        ext = os.path.splitext(model_path)[1].lower()
        if ext not in _SUPPORTED_FORMATS:
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message=f"Validation skipped (unsupported format {ext})",
                duration_seconds=time.time() - step_start,
            )

        try:
            report = run_full_validation_pipeline(
                model_path,
                printer_id=effective_pid or "",
                material="",
            )
        except Exception as exc:
            logger.warning(
                "Validation pipeline raised — proceeding without gate: %s",
                exc, exc_info=True,
            )
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message=f"Validation skipped ({exc.__class__.__name__})",
                duration_seconds=time.time() - step_start,
            )

        ctx["validation_report"] = report
        ready = report.get("ready_to_print", True)
        score = report.get("printability_score", 0)
        summary = report.get("summary", "")

        if not ready:
            return PipelineStep(
                name="validate_mesh",
                success=False,
                message=(
                    f"Mesh failed pre-print validation (score {score}/100): "
                    f"{summary} Pass skip_validation=True to bypass."
                ),
                data={
                    "printability_score": score,
                    "ready_to_print": False,
                    "next_action": report.get("next_action"),
                    "summary": summary,
                },
                duration_seconds=time.time() - step_start,
            )

        # Slice the (possibly repaired/scaled) validated mesh.
        validated_path = report.get("validated_path") or model_path
        if validated_path and validated_path != model_path:
            ctx["model_path"] = validated_path
            logger.info(
                "quick_print: using validated path %s (repaired=%s)",
                validated_path,
                report.get("repaired", False),
            )

        return PipelineStep(
            name="validate_mesh",
            success=True,
            message=f"Print-ready (score {score}/100)",
            data={
                "printability_score": score,
                "ready_to_print": True,
                "repaired": report.get("repaired", False),
                "summary": summary,
            },
            duration_seconds=time.time() - step_start,
        )

    def _resolve_profile() -> PipelineStep:
        if ctx["effective_profile"]:
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message="Using explicit profile",
            )
        if not effective_pid:
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message="No profile needed",
            )
        step_start = time.time()
        try:
            from kiln.slicer_profiles import resolve_slicer_profile

            ctx["effective_profile"] = resolve_slicer_profile(effective_pid)
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message=f"Using bundled profile for {effective_pid}",
                data={"profile_path": ctx["effective_profile"], "printer_id": effective_pid},
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
                ctx["model_path"],
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
        if not effective_pid or not ctx["gcode_path"]:
            return PipelineStep(
                name="safety_check",
                success=True,
                message="Skipped (no printer_id or gcode_path)",
            )
        step_start = time.time()
        try:
            from kiln.gcode import scan_gcode_file

            vr = scan_gcode_file(ctx["gcode_path"], printer_id=effective_pid)
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
            adapter = _resolve_pipeline_adapter(printer_name)
            ctx["adapter"] = adapter
            upload_result = adapter.upload_file(ctx["gcode_path"])
            remote_name = getattr(upload_result, "file_name", None) or os.path.basename(ctx["gcode_path"])
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
            checks_passed = state.connected and state.state.value == "idle"
            return PipelineStep(
                name="preflight",
                success=checks_passed,
                message="Printer ready" if checks_passed else f"Printer not ready: {state.state.value}",
                data={"connected": state.connected, "status": state.state.value},
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
            # Resolve AMS routing through the shared resolver so the A1
            # tray_now="255" quirk can't silently route to the external
            # spool when trays are loaded.  Mirrors slice_and_print + the
            # start_print MCP tool.  Lazy import avoids a server<->pipelines
            # import cycle (R1).  Non-Bambu adapters return use_ams=False
            # and we leave the kwarg ABSENT so the Bambu adapter's own
            # single-filament auto-route safety net still governs (R3).
            from kiln.server import _resolve_use_ams

            start_kwargs: dict[str, Any] = {}
            ams_decision = _resolve_use_ams(
                "auto" if use_ams is None else use_ams,
                ams_mapping,
                adapter,
                material=material,
                # The G-code just sliced says which colours it wants, so
                # each extruder is routed to the tray of that colour.
                file_path=ctx.get("gcode_path"),
            )
            ams_warnings = list(ams_decision.get("warnings") or [])
            if ams_decision.get("blocked"):
                return PipelineStep(
                    name="start_print",
                    success=False,
                    message="Not started: " + " ".join(ams_warnings),
                    duration_seconds=time.time() - step_start,
                    data={"ams_plan": ams_decision.get("plan"), "ams_warnings": ams_warnings},
                )
            ams_selection = None
            if ams_decision.get("use_ams"):
                start_kwargs["use_ams"] = True
                resolved_mapping = (
                    ams_mapping
                    if ams_mapping is not None
                    else ams_decision.get("ams_mapping")
                )
                if resolved_mapping is not None:
                    start_kwargs["ams_mapping"] = resolved_mapping
                ams_selection = ams_decision.get("selection")
            elif use_ams is False:
                # Caller EXPLICITLY opted out of AMS — forward use_ams=False
                # so the adapter's single-filament auto-route can't silently
                # re-enable AMS.  (Auto-resolved "no AMS" leaves the kwarg
                # absent on purpose — that's the R3 safety net.)
                start_kwargs["use_ams"] = False

            sent_at = time.monotonic()
            print_result = adapter.start_print(remote_name, **start_kwargs)
            verdict = resolve_print_start(
                adapter, print_result, sent_at=sent_at, file_name=remote_name,
            )

            step_data: dict[str, Any] = {
                "file_name": remote_name,
                "print_start": verdict.state,
                "print": verdict.to_dict(),
            }
            if ams_selection is not None:
                step_data["ams_selection"] = ams_selection
            if ams_warnings:
                step_data["ams_warnings"] = ams_warnings
            ams_plan = ams_decision.get("plan") if "ams_decision" in locals() else None
            if ams_plan:
                step_data["ams_plan"] = ams_plan
            msg = _start_print_step_message(verdict, remote_name)
            if ams_plan and len(ams_plan.get("matches") or []) > 1:
                msg += f" (AMS: {ams_plan.get('summary')})"
            elif ams_selection is not None:
                msg += (
                    f" (AMS slot {ams_selection['slot']} — "
                    f"{ams_selection['type']})"
                )
            return PipelineStep(
                name="start_print",
                success=verdict.ok,
                message=msg,
                data=step_data,
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            return PipelineStep(
                name="start_print",
                success=False,
                message=f"Failed to start print: {exc}",
                duration_seconds=time.time() - step_start,
            )

    def _check_stability() -> PipelineStep:
        return _run_stability_check(ctx["model_path"], ctx)

    step_defs = [
        _StepDef(name="validate_mesh", fn=_validate_mesh, fatal=True),
        _StepDef(name="resolve_profile", fn=_resolve_profile, fatal=False),
        _StepDef(name="stability_check", fn=_check_stability, fatal=False),
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
# reslice_and_print pipeline
# ---------------------------------------------------------------------------


def reslice_and_print(
    *,
    model_path: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    overrides: dict[str, str] | None = None,
    profile_path: str | None = None,
    slicer_path: str | None = None,
    extra_args: list[str] | None = None,
    pause_after_step: int | None = None,
    material: str | None = None,
    use_ams: bool | None = None,
    ams_mapping: list[int] | None = None,
    skip_validation: bool = False,
) -> PipelineResult:
    """Reslice a model with parameter overrides, then upload and print.

    Steps:
        1. validate_mesh — Pre-print printability gate (manifold, walls,
            overhangs, bridges, bed-fit, material).  Auto-repairs on
            non-manifold; blocks failed designs.  Skip with skip_validation=True.
        2. resolve_profile — Merge base profile with overrides
        3. slice — Call slicer with merged profile
        4. safety_check — Validate gcode against printer limits
        5. upload — Upload gcode to printer
        6. preflight — Verify printer is ready
        7. start_print — Begin printing

    Args:
        model_path: Path to STL/3MF file.
        printer_name: Registered printer name in fleet. If omitted,
            uses the default printer.
        printer_id: Printer model ID for base profile selection.
        overrides: Dict of PrusaSlicer INI key-value overrides.
        profile_path: Explicit profile path (overrides printer_id resolution).
        slicer_path: Explicit path to slicer binary.
        extra_args: Additional CLI arguments to pass to the slicer.
        pause_after_step: Auto-pause after completing step N (0-indexed).
        skip_validation: Bypass the pre-print validation step.  Defaults
            to False — designs are pre-tested for printability before
            they reach the printer.

    Returns:
        :class:`PipelineResult` with step-by-step outcomes.
    """
    effective_overrides = overrides or {}

    # Shared mutable state between step closures.
    # ctx["model_path"] is the EFFECTIVE path used by downstream steps —
    # the validation step may update it to point at an auto-repaired or
    # auto-scaled mesh.
    ctx: dict[str, Any] = {
        "effective_profile": profile_path,
        "gcode_path": None,
        "adapter": None,
        "remote_name": None,
        "model_path": model_path,
        "validation_report": None,
    }
    effective_pid = _target_printer_id(printer_id, printer_name)

    def _validate_mesh() -> PipelineStep:
        step_start = time.time()
        if skip_validation:
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message="Validation skipped (skip_validation=True)",
                duration_seconds=time.time() - step_start,
            )

        try:
            from kiln.plugins._validation_pipeline_internals import (
                _SUPPORTED_FORMATS,
            )
            from kiln.plugins.validation_pipeline_tools import (
                run_full_validation_pipeline,
            )
        except ImportError as exc:
            logger.debug("Validation pipeline import failed: %s", exc, exc_info=True)
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message="Validation skipped (pipeline unavailable)",
                duration_seconds=time.time() - step_start,
            )

        ext = os.path.splitext(model_path)[1].lower()
        if ext not in _SUPPORTED_FORMATS:
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message=f"Validation skipped (unsupported format {ext})",
                duration_seconds=time.time() - step_start,
            )

        try:
            report = run_full_validation_pipeline(
                model_path,
                printer_id=effective_pid or "",
                material="",
            )
        except Exception as exc:
            logger.warning(
                "Validation pipeline raised — proceeding without gate: %s",
                exc, exc_info=True,
            )
            return PipelineStep(
                name="validate_mesh",
                success=True,
                message=f"Validation skipped ({exc.__class__.__name__})",
                duration_seconds=time.time() - step_start,
            )

        ctx["validation_report"] = report
        ready = report.get("ready_to_print", True)
        score = report.get("printability_score", 0)
        summary = report.get("summary", "")

        if not ready:
            return PipelineStep(
                name="validate_mesh",
                success=False,
                message=(
                    f"Mesh failed pre-print validation (score {score}/100): "
                    f"{summary} Pass skip_validation=True to bypass."
                ),
                data={
                    "printability_score": score,
                    "ready_to_print": False,
                    "next_action": report.get("next_action"),
                    "summary": summary,
                },
                duration_seconds=time.time() - step_start,
            )

        validated_path = report.get("validated_path") or model_path
        if validated_path and validated_path != model_path:
            ctx["model_path"] = validated_path
            logger.info(
                "reslice_and_print: using validated path %s (repaired=%s)",
                validated_path,
                report.get("repaired", False),
            )

        return PipelineStep(
            name="validate_mesh",
            success=True,
            message=f"Print-ready (score {score}/100)",
            data={
                "printability_score": score,
                "ready_to_print": True,
                "repaired": report.get("repaired", False),
                "summary": summary,
            },
            duration_seconds=time.time() - step_start,
        )

    def _resolve_profile() -> PipelineStep:
        if ctx["effective_profile"]:
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message="Using explicit profile",
            )
        if not effective_pid:
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message="No profile needed (no printer_id)",
            )
        step_start = time.time()
        try:
            from kiln.slicer_profiles import resolve_slicer_profile

            ctx["effective_profile"] = resolve_slicer_profile(
                effective_pid, overrides=effective_overrides
            )
            override_msg = f" with {len(effective_overrides)} override(s)" if effective_overrides else ""
            return PipelineStep(
                name="resolve_profile",
                success=True,
                message=f"Using bundled profile for {effective_pid}{override_msg}",
                data={
                    "profile_path": ctx["effective_profile"],
                    "printer_id": effective_pid,
                    "overrides": effective_overrides,
                },
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
                ctx["model_path"],
                profile=ctx["effective_profile"],
                slicer_path=slicer_path,
                extra_args=extra_args,
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
        if not effective_pid or not ctx["gcode_path"]:
            return PipelineStep(
                name="safety_check",
                success=True,
                message="Skipped (no printer_id or gcode_path)",
            )
        step_start = time.time()
        try:
            from kiln.gcode import scan_gcode_file

            vr = scan_gcode_file(ctx["gcode_path"], printer_id=effective_pid)
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
            adapter = _resolve_pipeline_adapter(printer_name)
            ctx["adapter"] = adapter

            # Bambu printers need gcode wrapped in a 3MF with proprietary
            # BambuStudio start/end sequences for the extruder to function.
            # Other adapters (OctoPrint, Moonraker, Serial) upload raw gcode.
            upload_path = ctx["gcode_path"]
            wrapped_3mf = False
            if (
                hasattr(adapter, "wrap_gcode_as_3mf")
                and upload_path.endswith(".gcode")
            ):
                try:
                    wrap_kwargs: dict[str, Any] = {}
                    if effective_overrides.get("temperature"):
                        wrap_kwargs["hotend_temp"] = int(effective_overrides["temperature"])
                    if effective_overrides.get("bed_temperature"):
                        wrap_kwargs["bed_temp"] = int(effective_overrides["bed_temperature"])
                    upload_path = adapter.wrap_gcode_as_3mf(
                        upload_path, **wrap_kwargs
                    )
                    wrapped_3mf = True
                    logger.info("Wrapped gcode as Bambu 3MF: %s", upload_path)
                except Exception:
                    logger.warning(
                        "Bambu 3MF wrapping failed, uploading raw gcode",
                        exc_info=True,
                    )

            upload_result = adapter.upload_file(upload_path)
            remote_name = getattr(upload_result, "file_name", None) or os.path.basename(upload_path)
            ctx["remote_name"] = remote_name
            ctx["local_3mf_path"] = upload_path if wrapped_3mf else None
            return PipelineStep(
                name="upload",
                success=True,
                message=f"Uploaded {remote_name}"
                + (" (Bambu 3MF wrapped)" if wrapped_3mf else ""),
                data={"remote_name": remote_name, "wrapped_3mf": wrapped_3mf},
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
            checks_passed = state.connected and state.state.value == "idle"
            return PipelineStep(
                name="preflight",
                success=checks_passed,
                message="Printer ready" if checks_passed else f"Printer not ready: {state.state.value}",
                data={"connected": state.connected, "status": state.state.value},
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
            # For Bambu 3MF uploads, pass local_file_path so the adapter
            # can compute MD5 and enable AMS auto-detection.
            start_kwargs: dict[str, Any] = {}
            local_3mf = ctx.get("local_3mf_path")
            if local_3mf:
                start_kwargs["local_file_path"] = local_3mf

            ams_selection = None
            ams_warnings: list[str] = []
            # A 3MF plate carries its own filament map.  Multi-material (or
            # unreadable) plates MUST defer to the adapter's auto-detect so we
            # never override a multi-color mapping; a confidently
            # single-material plate is safe to route through the resolver (one
            # filament, one tray) and so earns the same material-match +
            # selection record as quick_print.
            defer_multi_material_3mf = False
            if local_3mf:
                defer_multi_material_3mf = True  # fail-safe default
                _count_fn = getattr(adapter, "filament_count_3mf", None)
                if _count_fn is not None:
                    try:
                        _n = _count_fn(local_3mf)
                        if _n is not None and _n <= 1:
                            defer_multi_material_3mf = False
                    except Exception:
                        pass  # unreadable → stays deferred

            if use_ams is not None or ams_mapping is not None:
                # Explicit caller routing — pass straight through.
                if use_ams is not None:
                    start_kwargs["use_ams"] = use_ams
                if ams_mapping is not None:
                    start_kwargs["ams_mapping"] = ams_mapping
            else:
                # Every 3MF and raw G-code goes through the shared resolver.
                # A multi-material 3MF used to be deferred to the adapter,
                # which fed extruder N from slot N whatever was loaded there;
                # the resolver now reads the colours the file wants and
                # matches them to the trays.  When it cannot read them, the
                # old deferral stands.  Lazy import avoids the
                # server<->pipelines cycle (R1).
                from kiln.server import _resolve_use_ams

                ams_decision = _resolve_use_ams(
                    "auto", None, adapter, material=material,
                    file_path=local_3mf or ctx.get("gcode_path"),
                )
                ams_warnings = list(ams_decision.get("warnings") or [])
                if ams_decision.get("blocked"):
                    return PipelineStep(
                        name="start_print",
                        success=False,
                        message="Not started: " + " ".join(ams_warnings),
                        duration_seconds=time.time() - step_start,
                        data={"ams_plan": ams_decision.get("plan"), "ams_warnings": ams_warnings},
                    )
                if defer_multi_material_3mf and not ams_decision.get("plan"):
                    ams_decision = {"use_ams": False, "warnings": ams_warnings}
                if ams_decision.get("use_ams"):
                    start_kwargs["use_ams"] = True
                    if ams_decision.get("ams_mapping") is not None:
                        start_kwargs["ams_mapping"] = ams_decision["ams_mapping"]
                    ams_selection = ams_decision.get("selection")

            sent_at = time.monotonic()
            print_result = adapter.start_print(remote_name, **start_kwargs)
            verdict = resolve_print_start(
                adapter, print_result, sent_at=sent_at, file_name=remote_name,
            )
            step_data: dict[str, Any] = {
                "file_name": remote_name,
                "print_start": verdict.state,
                "print": verdict.to_dict(),
            }
            if ams_selection is not None:
                step_data["ams_selection"] = ams_selection
            if ams_warnings:
                step_data["ams_warnings"] = ams_warnings
            ams_plan = ams_decision.get("plan") if "ams_decision" in locals() else None
            if ams_plan:
                step_data["ams_plan"] = ams_plan
            msg = _start_print_step_message(verdict, remote_name)
            if ams_plan and len(ams_plan.get("matches") or []) > 1:
                msg += f" (AMS: {ams_plan.get('summary')})"
            elif ams_selection is not None:
                msg += (
                    f" (AMS slot {ams_selection['slot']} — "
                    f"{ams_selection['type']})"
                )
            return PipelineStep(
                name="start_print",
                success=verdict.ok,
                message=msg,
                data=step_data,
                duration_seconds=time.time() - step_start,
            )
        except Exception as exc:
            return PipelineStep(
                name="start_print",
                success=False,
                message=f"Failed to start print: {exc}",
                duration_seconds=time.time() - step_start,
            )

    def _check_stability() -> PipelineStep:
        return _run_stability_check(ctx["model_path"], ctx)

    step_defs = [
        _StepDef(name="validate_mesh", fn=_validate_mesh, fatal=True),
        _StepDef(name="resolve_profile", fn=_resolve_profile, fatal=False),
        _StepDef(name="stability_check", fn=_check_stability, fatal=False),
        _StepDef(name="slice", fn=_slice, fatal=True),
        _StepDef(name="safety_check", fn=_safety_check, fatal=True),
        _StepDef(name="upload", fn=_upload, fatal=True),
        _StepDef(name="preflight", fn=_preflight, fatal=True),
        _StepDef(name="start_print", fn=_start_print, fatal=True),
    ]

    execution = PipelineExecution(
        "reslice_and_print",
        step_defs,
        pause_after_step=pause_after_step,
    )
    return execution.run()


def calibrate(
    *,
    printer_name: str | None = None,
    printer_id: str | None = None,
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
    steps: list[PipelineStep] = []

    # Step 1: Get adapter
    step_start = time.time()
    try:
        adapter = _resolve_pipeline_adapter(printer_name)
        state = adapter.get_state()
        steps.append(
            PipelineStep(
                name="connect",
                success=state.connected,
                message="Connected" if state.connected else "Printer offline",
                data={"status": state.state.value, "connected": state.connected},
                duration_seconds=time.time() - step_start,
            )
        )
        if not state.connected:
            return PipelineResult(
                pipeline="calibrate",
                success=False,
                message="Printer is not connected.",
                steps=steps,
                total_duration_seconds=time.time() - start,
            )
        # Calibration homes ALL axes and probes the bed — both need a
        # clear plate.  While a job is printing or paused the plate
        # carries a part, and the G28's Z descent (and the probe pass
        # after it) would drive the nozzle into it.
        if state.state in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
            steps.append(
                PipelineStep(
                    name="connect",
                    success=False,
                    message=(
                        f"Printer is {state.state.value} — calibration "
                        "homes Z and probes the bed, which needs a clear "
                        "plate. Finish or cancel the job first."
                    ),
                    duration_seconds=time.time() - step_start,
                )
            )
            return PipelineResult(
                pipeline="calibrate",
                success=False,
                message=(
                    f"Refused: printer is {state.state.value} and the bed "
                    "carries a print. Calibrate only on a clear plate."
                ),
                steps=steps,
                total_duration_seconds=time.time() - start,
            )
    except Exception as exc:
        steps.append(
            PipelineStep(
                name="connect",
                success=False,
                message=f"Connection failed: {exc}",
                duration_seconds=time.time() - step_start,
            )
        )
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
        steps.append(
            PipelineStep(
                name="home",
                success=True,
                message="Homed all axes (G28)",
                duration_seconds=time.time() - step_start,
            )
        )
    except Exception as exc:
        steps.append(
            PipelineStep(
                name="home",
                success=False,
                message=f"Homing failed: {exc}",
                duration_seconds=time.time() - step_start,
            )
        )

    # Step 3: Auto bed level
    step_start = time.time()
    try:
        adapter.send_gcode("G29")
        steps.append(
            PipelineStep(
                name="bed_level",
                success=True,
                message="Auto bed leveling complete (G29)",
                duration_seconds=time.time() - step_start,
            )
        )
    except Exception as exc:
        steps.append(
            PipelineStep(
                name="bed_level",
                success=True,  # Non-fatal — not all printers support G29.
                message=f"Auto bed level not available or failed: {exc}",
                duration_seconds=time.time() - step_start,
            )
        )

    # Step 4: Gather calibration guidance
    guidance: dict[str, str] = {}
    if printer_id:
        step_start = time.time()
        try:
            from kiln.printer_intelligence import get_printer_intel

            intel = get_printer_intel(printer_id)
            guidance = dict(intel.calibration)
            steps.append(
                PipelineStep(
                    name="guidance",
                    success=True,
                    message=f"Loaded calibration guidance for {intel.display_name}",
                    data={"calibration": guidance},
                    duration_seconds=time.time() - step_start,
                )
            )
        except Exception as exc:
            steps.append(
                PipelineStep(
                    name="guidance",
                    success=True,
                    message=f"No calibration guidance available: {exc}",
                    duration_seconds=time.time() - step_start,
                )
            )

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
    printer_name: str | None = None,
    printer_id: str | None = None,
    model_path: str | None = None,
    profile_path: str | None = None,
    skip_validation: bool = False,
) -> PipelineResult:
    """Slice a benchmark model, upload, and report estimated stats.

    This pipeline prepares a benchmark print but does NOT start it
    automatically (benchmarks should be manually observed).

    Steps:
    1. Verify model is supplied
    2. Validate the mesh (manifold, walls, overhangs, bridges, bed-fit).
       Skipped when skip_validation=True (e.g. for fixed reference
       benchmark models that are pre-validated).
    3. Resolve slicer profile for printer
    4. Slice benchmark model (or user-provided model)
    5. Upload to printer
    6. Report printer stats from history

    Args:
        printer_name: Registered printer name.
        printer_id: Printer model for profile selection.
        model_path: Path to benchmark model. Uses a simple cube if omitted.
        profile_path: Explicit slicer profile path.
        skip_validation: Bypass the pre-print mesh validation step.
            Defaults to False — user-supplied benchmark meshes are
            pre-tested for printability before slicing.  Set to True
            for known-good fixed reference models.
    """
    start = time.time()
    steps: list[PipelineStep] = []
    effective_pid = _target_printer_id(printer_id, printer_name)

    # Step 1: Verify we have a model
    if not model_path:
        steps.append(
            PipelineStep(
                name="model",
                success=False,
                message="No benchmark model path provided. Supply a model_path to benchmark.",
            )
        )
        return PipelineResult(
            pipeline="benchmark",
            success=False,
            message="Benchmark requires a model_path. Provide an STL file.",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 1b: Pre-print validation gate.
    # Same gate as quick_print / reslice_and_print so user-supplied
    # benchmark meshes get the same engineering review.  Skipped for
    # known-good reference models via skip_validation=True.
    if not skip_validation:
        step_start = time.time()
        try:
            from kiln.plugins._validation_pipeline_internals import (
                _SUPPORTED_FORMATS,
            )
            from kiln.plugins.validation_pipeline_tools import (
                run_full_validation_pipeline,
            )
        except ImportError as exc:
            logger.debug(
                "Validation pipeline import failed: %s", exc, exc_info=True,
            )
            steps.append(
                PipelineStep(
                    name="validate_mesh",
                    success=True,
                    message="Validation skipped (pipeline unavailable)",
                    duration_seconds=time.time() - step_start,
                )
            )
        else:
            ext = os.path.splitext(model_path)[1].lower()
            if ext not in _SUPPORTED_FORMATS:
                steps.append(
                    PipelineStep(
                        name="validate_mesh",
                        success=True,
                        message=f"Validation skipped (unsupported format {ext})",
                        duration_seconds=time.time() - step_start,
                    )
                )
            else:
                try:
                    report = run_full_validation_pipeline(
                        model_path,
                        printer_id=effective_pid or "",
                        material="",
                    )
                except Exception as exc:
                    logger.warning(
                        "Validation pipeline raised — proceeding without gate: %s",
                        exc, exc_info=True,
                    )
                    steps.append(
                        PipelineStep(
                            name="validate_mesh",
                            success=True,
                            message=f"Validation skipped ({exc.__class__.__name__})",
                            duration_seconds=time.time() - step_start,
                        )
                    )
                else:
                    ready = report.get("ready_to_print", True)
                    score = report.get("printability_score", 0)
                    summary = report.get("summary", "")
                    if not ready:
                        steps.append(
                            PipelineStep(
                                name="validate_mesh",
                                success=False,
                                message=(
                                    f"Mesh failed pre-print validation "
                                    f"(score {score}/100): {summary} "
                                    f"Pass skip_validation=True to bypass."
                                ),
                                data={
                                    "printability_score": score,
                                    "ready_to_print": False,
                                    "next_action": report.get("next_action"),
                                    "summary": summary,
                                },
                                duration_seconds=time.time() - step_start,
                            )
                        )
                        return PipelineResult(
                            pipeline="benchmark",
                            success=False,
                            message=(
                                f"Benchmark blocked at validation: {summary}"
                            ),
                            steps=steps,
                            total_duration_seconds=time.time() - start,
                        )
                    # Slice the (possibly auto-repaired) mesh.
                    validated_path = report.get("validated_path") or model_path
                    if validated_path and validated_path != model_path:
                        logger.info(
                            "benchmark: using validated path %s (repaired=%s)",
                            validated_path,
                            report.get("repaired", False),
                        )
                        model_path = validated_path
                    steps.append(
                        PipelineStep(
                            name="validate_mesh",
                            success=True,
                            message=f"Print-ready (score {score}/100)",
                            data={
                                "printability_score": score,
                                "ready_to_print": True,
                                "repaired": report.get("repaired", False),
                                "summary": summary,
                            },
                            duration_seconds=time.time() - step_start,
                        )
                    )
    else:
        steps.append(
            PipelineStep(
                name="validate_mesh",
                success=True,
                message="Validation skipped (skip_validation=True)",
            )
        )

    # Step 2: Resolve profile
    effective_profile = profile_path
    if not effective_profile and effective_pid:
        step_start = time.time()
        try:
            from kiln.slicer_profiles import resolve_slicer_profile

            effective_profile = resolve_slicer_profile(effective_pid)
            steps.append(
                PipelineStep(
                    name="resolve_profile",
                    success=True,
                    message=f"Using bundled profile for {effective_pid}",
                    data={"profile_path": effective_profile},
                    duration_seconds=time.time() - step_start,
                )
            )
        except Exception as exc:
            steps.append(
                PipelineStep(
                    name="resolve_profile",
                    success=True,
                    message=f"Profile resolution failed, using slicer defaults: {exc}",
                    duration_seconds=time.time() - step_start,
                )
            )

    # Step 3: Slice
    step_start = time.time()
    try:
        from kiln.slicer import slice_file

        result = slice_file(model_path, profile=effective_profile)
        gcode_path = result.output_path
        steps.append(
            PipelineStep(
                name="slice",
                success=True,
                message=result.message,
                data={"output_path": gcode_path},
                duration_seconds=time.time() - step_start,
            )
        )
    except Exception as exc:
        steps.append(
            PipelineStep(
                name="slice",
                success=False,
                message=f"Slicing failed: {exc}",
                duration_seconds=time.time() - step_start,
            )
        )
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
        adapter = _resolve_pipeline_adapter(printer_name)
        upload_result = adapter.upload_file(gcode_path)
        remote_name = getattr(upload_result, "file_name", None) or os.path.basename(gcode_path)
        steps.append(
            PipelineStep(
                name="upload",
                success=True,
                message=f"Uploaded benchmark file: {remote_name}",
                data={"remote_name": remote_name},
                duration_seconds=time.time() - step_start,
            )
        )
    except Exception as exc:
        steps.append(
            PipelineStep(
                name="upload",
                success=False,
                message=f"Upload failed: {exc}",
                duration_seconds=time.time() - step_start,
            )
        )
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
            steps.append(
                PipelineStep(
                    name="stats",
                    success=True,
                    message=f"Printer stats: {stats.get('total_prints', 0)} prints, "
                    f"{stats.get('success_rate', 0):.0%} success rate",
                    data=stats,
                    duration_seconds=time.time() - step_start,
                )
            )
        except Exception as exc:
            steps.append(
                PipelineStep(
                    name="stats",
                    success=True,
                    message=f"Stats unavailable: {exc}",
                    duration_seconds=time.time() - step_start,
                )
            )

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
    "reslice_and_print": {
        "function": reslice_and_print,
        "description": "Reslice with parameter overrides → validate → upload → print.",
        "params": ["model_path", "printer_name", "printer_id", "overrides", "profile_path"],
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


def list_pipelines() -> list[dict[str, str]]:
    """Return metadata for all available pipelines."""
    return [
        {"name": name, "description": info["description"], "params": info["params"]} for name, info in PIPELINES.items()
    ]
