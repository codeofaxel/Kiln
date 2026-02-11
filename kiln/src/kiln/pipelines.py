"""Pre-validated print pipelines — named command sequences that chain
multiple operations into reliable one-shot workflows.

Each pipeline is a composable sequence of steps (slice, preflight,
upload, print, monitor) that handles errors at each stage and returns
a structured result.

Available pipelines:
    * **quick_print** — slice → preflight → upload → start print
    * **calibrate** — home → bed level → PID tune → report
    * **benchmark** — slice benchmark model → print → report stats

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

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
# quick_print pipeline
# ---------------------------------------------------------------------------

def quick_print(
    *,
    model_path: str,
    printer_name: Optional[str] = None,
    printer_id: Optional[str] = None,
    profile_path: Optional[str] = None,
    slicer_path: Optional[str] = None,
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

    Returns:
        :class:`PipelineResult` with step-by-step outcomes.
    """
    start = time.time()
    steps: List[PipelineStep] = []

    # Step 1: Resolve slicer profile
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
                data={"profile_path": effective_profile, "printer_id": printer_id},
                duration_seconds=time.time() - step_start,
            ))
        except Exception as exc:
            steps.append(PipelineStep(
                name="resolve_profile",
                success=False,
                message=f"Profile resolution failed: {exc}",
                duration_seconds=time.time() - step_start,
            ))
            # Continue with no profile — slicer will use its defaults.

    # Step 2: Slice
    step_start = time.time()
    try:
        from kiln.slicer import slice_file
        result = slice_file(
            model_path,
            profile=effective_profile,
            slicer_path=slicer_path,
        )
        gcode_path = result.output_path
        steps.append(PipelineStep(
            name="slice",
            success=True,
            message=result.message,
            data={"output_path": gcode_path, "slicer": result.slicer},
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
            pipeline="quick_print",
            success=False,
            message=f"Pipeline failed at slicing: {exc}",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 3: G-code safety validation (if printer_id known)
    if printer_id and gcode_path:
        step_start = time.time()
        try:
            from kiln.gcode import scan_gcode_file
            vr = scan_gcode_file(gcode_path, printer_id=printer_id)
            steps.append(PipelineStep(
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
            ))
            if not vr.valid:
                return PipelineResult(
                    pipeline="quick_print",
                    success=False,
                    message=f"G-code safety validation failed: {'; '.join(vr.errors[:3])}",
                    steps=steps,
                    total_duration_seconds=time.time() - start,
                )
        except Exception as exc:
            logger.exception("G-code safety validation failed")
            steps.append(PipelineStep(
                name="safety_check",
                success=False,
                message=f"G-code safety validation error: {exc}",
                duration_seconds=time.time() - step_start,
            ))
            return PipelineResult(
                pipeline="quick_print",
                success=False,
                message="Pipeline aborted: G-code safety validation failed.",
                steps=steps,
                total_duration_seconds=time.time() - start,
            )

    # Step 4: Upload
    step_start = time.time()
    try:
        from kiln.registry import PrinterRegistry
        from kiln.server import _registry
        adapter = _registry.get_adapter(printer_name) if printer_name else _registry.get_default_adapter()
        upload_result = adapter.upload_file(gcode_path)
        remote_name = upload_result.get("name", os.path.basename(gcode_path))
        steps.append(PipelineStep(
            name="upload",
            success=True,
            message=f"Uploaded {remote_name}",
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
            pipeline="quick_print",
            success=False,
            message=f"Pipeline failed at upload: {exc}",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 5: Preflight check (always runs — never skip safety checks)
    step_start = time.time()
    try:
        state = adapter.get_state()
        checks_passed = state.connected and state.status.value == "idle"
        steps.append(PipelineStep(
            name="preflight",
            success=checks_passed,
            message="Printer ready" if checks_passed else f"Printer not ready: {state.status.value}",
            data={"connected": state.connected, "status": state.status.value},
            duration_seconds=time.time() - step_start,
        ))
        if not checks_passed:
            return PipelineResult(
                pipeline="quick_print",
                success=False,
                message=f"Preflight failed: printer status is {state.status.value}",
                steps=steps,
                total_duration_seconds=time.time() - start,
            )
    except Exception as exc:
        steps.append(PipelineStep(
            name="preflight",
            success=False,
            message=f"Preflight check failed: {exc}",
            duration_seconds=time.time() - step_start,
        ))
        return PipelineResult(
            pipeline="quick_print",
            success=False,
            message=f"Pipeline failed at preflight: {exc}",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    # Step 6: Start print
    step_start = time.time()
    try:
        adapter.start_print(remote_name)
        steps.append(PipelineStep(
            name="start_print",
            success=True,
            message=f"Print started: {remote_name}",
            data={"file_name": remote_name},
            duration_seconds=time.time() - step_start,
        ))
    except Exception as exc:
        steps.append(PipelineStep(
            name="start_print",
            success=False,
            message=f"Failed to start print: {exc}",
            duration_seconds=time.time() - step_start,
        ))
        return PipelineResult(
            pipeline="quick_print",
            success=False,
            message=f"Pipeline failed at start_print: {exc}",
            steps=steps,
            total_duration_seconds=time.time() - start,
        )

    return PipelineResult(
        pipeline="quick_print",
        success=True,
        message=f"Print started successfully: {remote_name}",
        steps=steps,
        total_duration_seconds=time.time() - start,
    )


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
