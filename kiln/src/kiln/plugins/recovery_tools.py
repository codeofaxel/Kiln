"""Failure recovery, job splitting, and generation feedback tools plugin.

Provides MCP tools for intelligent failure analysis, multi-printer job
splitting, and generation feedback loops.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _RecoveryToolsPlugin:
    """Failure recovery, job splitting, and generation feedback tools.

    Tools:
        - analyze_print_failure_smart
        - get_recovery_plan
        - failure_history
        - plan_multi_copy_split
        - plan_assembly_split
        - split_plan_status
        - cancel_split_plan
        - analyze_generation_feedback
        - improve_generation_prompt
        - generation_feedback_loop_status
    """

    @property
    def name(self) -> str:
        return "recovery_tools"

    @property
    def description(self) -> str:
        return "Failure recovery, job splitting, and generation feedback tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register recovery, splitting, and feedback tools with the MCP server."""

        # ---------------------------------------------------------------
        # Failure Recovery Tools
        # ---------------------------------------------------------------

        @mcp.tool()
        def analyze_print_failure_smart(
            progress: float = 0.0,
            error_message: str | None = None,
            printer_name: str | None = None,
            job_id: str | None = None,
        ) -> dict:
            """Classify a print failure and suggest recovery steps.

            Uses heuristics based on error messages, print progress, and
            failure history to classify the failure type and generate an
            actionable recovery plan.

            Args:
                progress: Print progress at failure (0.0 - 1.0).
                error_message: Error message from the printer or system.
                printer_name: Name of the printer that failed.
                job_id: Job ID of the failed print.
            """
            import kiln.server as _srv
            from kiln.failure_recovery import analyze_failure, record_failure

            if err := _srv._check_auth("read"):
                return err
            try:
                analysis = analyze_failure(
                    job_id=job_id,
                    printer_name=printer_name,
                    progress=progress,
                    error_message=error_message,
                )

                # Record the failure for future learning
                try:
                    record_failure(
                        analysis.classification,
                        analysis.recovery_plan,
                        printer_name=printer_name,
                        job_id=job_id,
                    )
                except Exception:
                    _logger.debug("Failed to record failure (non-fatal)")

                return {
                    "success": True,
                    "analysis": analysis.to_dict(),
                    "message": (
                        f"Failure classified as {analysis.classification.failure_type.value} "
                        f"(confidence: {analysis.classification.confidence:.0%}). "
                        f"Recommended action: {analysis.recovery_plan.action.value}."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in analyze_print_failure_smart")
                return _srv._error_dict(
                    f"Unexpected error analyzing failure: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def get_recovery_plan(
            failure_type: str,
            printer_name: str | None = None,
            has_power_loss_recovery: bool = False,
            has_filament_sensor: bool = False,
        ) -> dict:
            """Get a recovery plan for a specific failure type.

            Args:
                failure_type: One of: spaghetti, layer_shift, adhesion_loss,
                    nozzle_clog, stringing, thermal_runaway, power_loss,
                    filament_runout, warping, unknown.
                printer_name: Name of the affected printer.
                has_power_loss_recovery: Whether the printer supports
                    power-loss recovery.
                has_filament_sensor: Whether the printer has a filament
                    runout sensor.
            """
            import kiln.server as _srv
            from kiln.failure_recovery import (
                FailureClassification,
                FailureType,
                plan_recovery,
            )

            if err := _srv._check_auth("read"):
                return err
            try:
                ftype = FailureType(failure_type)
            except ValueError:
                return _srv._error_dict(
                    f"Unknown failure type: {failure_type!r}. "
                    f"Valid types: {[ft.value for ft in FailureType]}",
                    code="VALIDATION_ERROR",
                )

            classification = FailureClassification(
                failure_type=ftype,
                confidence=1.0,
                evidence=["Manual classification"],
                progress_at_failure=0.0,
                time_printing_seconds=0,
                material_wasted_grams=0.0,
            )

            capabilities = {
                "power_loss_recovery": has_power_loss_recovery,
                "filament_sensor": has_filament_sensor,
            }

            plan = plan_recovery(
                classification,
                printer_name=printer_name,
                printer_capabilities=capabilities,
            )

            return {
                "success": True,
                "recovery_plan": plan.to_dict(),
                "message": f"Recovery plan for {failure_type}: {plan.action.value}.",
            }

        @mcp.tool()
        def failure_history(
            printer_name: str | None = None,
            failure_type: str | None = None,
            limit: int = 20,
        ) -> dict:
            """View failure history for a printer or failure type.

            Args:
                printer_name: Filter by printer name.
                failure_type: Filter by failure type.
                limit: Maximum records to return (default 20).
            """
            import kiln.server as _srv
            from kiln.failure_recovery import get_failure_history

            if err := _srv._check_auth("read"):
                return err
            try:
                records = get_failure_history(
                    printer_name=printer_name,
                    failure_type=failure_type,
                    limit=limit,
                )
                return {
                    "success": True,
                    "records": records,
                    "count": len(records),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in failure_history")
                return _srv._error_dict(
                    f"Unexpected error fetching failure history: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ---------------------------------------------------------------
        # Job Splitting Tools
        # ---------------------------------------------------------------

        @mcp.tool()
        def plan_multi_copy_split(
            file_path: str,
            copies: int,
            material: str = "pla",
        ) -> dict:
            """Plan parallel printing of multiple copies across printers.

            Distributes N copies of a file across available printers in the
            fleet for maximum parallelism.

            Args:
                file_path: Path to the G-code or model file.
                copies: Number of copies to print.
                material: Material type (default ``"pla"``).
            """
            import kiln.server as _srv
            from kiln.job_splitter import plan_multi_copy_split as _plan

            if err := _srv._check_auth("queue"):
                return err
            if copies < 1:
                return _srv._error_dict(
                    "copies must be at least 1",
                    code="VALIDATION_ERROR",
                )
            try:
                plan = _plan(file_path, copies, material=material)
                return {
                    "success": True,
                    "plan": plan.to_dict(),
                    "message": (
                        f"Split {copies} copies across {plan.total_printers} printers. "
                        f"Time savings: {plan.time_savings_percentage:.0f}%."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in plan_multi_copy_split")
                return _srv._error_dict(
                    f"Unexpected error planning split: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def plan_assembly_split(
            file_paths: list[str],
            material: str = "pla",
        ) -> dict:
            """Split a multi-file assembly across printers.

            Assigns each file in a multi-part assembly to a different
            printer for parallel printing.

            Args:
                file_paths: List of file paths in the assembly.
                material: Material type (default ``"pla"``).
            """
            import kiln.server as _srv
            from kiln.job_splitter import plan_assembly_split as _plan

            if err := _srv._check_auth("queue"):
                return err
            if not file_paths:
                return _srv._error_dict(
                    "file_paths must not be empty",
                    code="VALIDATION_ERROR",
                )
            try:
                plan = _plan(file_paths, material=material)
                return {
                    "success": True,
                    "plan": plan.to_dict(),
                    "message": (
                        f"Split {len(file_paths)} parts across {plan.total_printers} printers. "
                        f"Time savings: {plan.time_savings_percentage:.0f}%."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in plan_assembly_split")
                return _srv._error_dict(
                    f"Unexpected error planning assembly split: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def split_plan_status(plan_id: str) -> dict:
            """Check the progress of a split plan.

            Args:
                plan_id: The plan ID returned by submitting a split plan.
            """
            import kiln.server as _srv
            from kiln.job_splitter import get_split_progress

            if err := _srv._check_auth("read"):
                return err
            try:
                progress = get_split_progress(plan_id)
                return {
                    "success": True,
                    "progress": progress.to_dict(),
                    "message": (
                        f"Plan {plan_id}: {progress.completed_parts}/{progress.total_parts} "
                        f"completed ({progress.overall_progress:.0%})."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in split_plan_status")
                return _srv._error_dict(
                    f"Unexpected error checking split progress: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def cancel_split_plan(plan_id: str) -> dict:
            """Cancel all pending/in-progress parts of a split plan.

            Args:
                plan_id: The plan ID to cancel.
            """
            import kiln.server as _srv
            from kiln.job_splitter import cancel_split_plan as _cancel

            if err := _srv._check_auth("queue"):
                return err
            try:
                result = _cancel(plan_id)
                return result
            except Exception as exc:
                _logger.exception("Unexpected error in cancel_split_plan")
                return _srv._error_dict(
                    f"Unexpected error cancelling split plan: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ---------------------------------------------------------------
        # Generation Feedback Tools
        # ---------------------------------------------------------------

        @mcp.tool()
        def analyze_generation_feedback(
            file_path: str,
            original_prompt: str,
            failure_mode: str | None = None,
            max_overhang_angle: float | None = None,
            min_wall_thickness: float | None = None,
            has_bridges: bool = False,
            has_floating_parts: bool = False,
            non_manifold: bool = False,
        ) -> dict:
            """Analyze a generated model and get feedback for improvement.

            Returns feedback with specific constraints to add to the
            generation prompt to fix identified issues.

            Args:
                file_path: Path to the generated model file.
                original_prompt: The original generation prompt.
                failure_mode: Optional failure mode if the model was printed
                    and failed (e.g. ``"adhesion"``, ``"spaghetti"``).
                max_overhang_angle: Maximum overhang angle in degrees.
                min_wall_thickness: Minimum wall thickness in mm.
                has_bridges: Whether the model has bridge features.
                has_floating_parts: Whether the model has disconnected parts.
                non_manifold: Whether the mesh is non-manifold.
            """
            import kiln.server as _srv
            from kiln.generation_feedback import analyze_for_feedback

            if err := _srv._check_auth("read"):
                return err
            try:
                printability_report: dict[str, Any] = {}
                if max_overhang_angle is not None:
                    printability_report["max_overhang_angle"] = max_overhang_angle
                if min_wall_thickness is not None:
                    printability_report["min_wall_thickness"] = min_wall_thickness
                if has_bridges:
                    printability_report["has_bridges"] = True
                if has_floating_parts:
                    printability_report["has_floating_parts"] = True
                if non_manifold:
                    printability_report["non_manifold"] = True

                feedback = analyze_for_feedback(
                    file_path,
                    original_prompt=original_prompt,
                    failure_mode=failure_mode,
                    printability_report=printability_report if printability_report else None,
                )
                return {
                    "success": True,
                    "feedback": [f.to_dict() for f in feedback],
                    "feedback_count": len(feedback),
                    "message": (
                        f"Found {len(feedback)} feedback items."
                        if feedback
                        else "No issues detected."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in analyze_generation_feedback")
                return _srv._error_dict(
                    f"Unexpected error analyzing feedback: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def improve_generation_prompt(
            original_prompt: str,
            failure_mode: str | None = None,
            max_overhang_angle: float | None = None,
            min_wall_thickness: float | None = None,
            has_bridges: bool = False,
            iteration: int = 1,
        ) -> dict:
            """Generate an improved prompt from feedback.

            Adds physical constraints to the original prompt to address
            printability issues, without modifying the creative intent.

            Args:
                original_prompt: The original generation prompt.
                failure_mode: Optional failure mode string.
                max_overhang_angle: Maximum overhang angle detected.
                min_wall_thickness: Minimum wall thickness detected.
                has_bridges: Whether bridges were detected.
                iteration: Which retry iteration this is.
            """
            import kiln.server as _srv
            from kiln.generation_feedback import (
                analyze_for_feedback,
                generate_improved_prompt,
            )

            if err := _srv._check_auth("read"):
                return err
            try:
                printability_report: dict[str, Any] = {}
                if max_overhang_angle is not None:
                    printability_report["max_overhang_angle"] = max_overhang_angle
                if min_wall_thickness is not None:
                    printability_report["min_wall_thickness"] = min_wall_thickness
                if has_bridges:
                    printability_report["has_bridges"] = True

                feedback = analyze_for_feedback(
                    "",  # No file needed for prompt improvement
                    original_prompt=original_prompt,
                    failure_mode=failure_mode,
                    printability_report=printability_report if printability_report else None,
                )

                improved = generate_improved_prompt(
                    original_prompt,
                    feedback,
                    iteration=iteration,
                )
                return {
                    "success": True,
                    "improved_prompt": improved.to_dict(),
                    "message": (
                        f"Improved prompt with {len(improved.constraints_added)} constraints "
                        f"(iteration {iteration})."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in improve_generation_prompt")
                return _srv._error_dict(
                    f"Unexpected error improving prompt: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def generation_feedback_loop_status(model_id: str) -> dict:
            """Check the status of a generation feedback loop.

            Args:
                model_id: The model ID of the feedback loop.
            """
            import kiln.server as _srv
            from kiln.generation_feedback import get_feedback_loop

            if err := _srv._check_auth("read"):
                return err
            try:
                loop = get_feedback_loop(model_id)
                if loop is None:
                    return _srv._error_dict(
                        f"No feedback loop found for model {model_id!r}",
                        code="NOT_FOUND",
                    )
                return {
                    "success": True,
                    "feedback_loop": loop.to_dict(),
                    "message": (
                        f"Feedback loop for {model_id}: "
                        f"iteration {loop.current_iteration}, "
                        f"{'resolved' if loop.resolved else 'in progress'}."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in generation_feedback_loop_status")
                return _srv._error_dict(
                    f"Unexpected error checking feedback loop: {exc}",
                    code="INTERNAL_ERROR",
                )

        _logger.debug("Registered recovery, splitting, and feedback tools")


plugin = _RecoveryToolsPlugin()
