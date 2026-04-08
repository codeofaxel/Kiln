"""G-code validation tools plugin.

Extracts G-code validation MCP tools from server.py into a focused plugin
module.  Tools validate G-code syntax, safety (generic and printer-specific),
and post-print quality.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any

_logger = logging.getLogger(__name__)


class _GcodeValidationToolsPlugin:
    """G-code validation and print quality assessment tools.

    Tools:
        - validate_gcode
        - validate_gcode_safe
        - validate_print_quality
    """

    @property
    def name(self) -> str:
        return "gcode_validation_tools"

    @property
    def description(self) -> str:
        return "G-code validation and print quality assessment tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register G-code validation tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # validate_gcode
        # ------------------------------------------------------------------

        @mcp.tool()
        def validate_gcode(commands: str) -> dict:
            """Validate G-code syntax and basic safety (generic, no printer-specific limits).

            For printer-specific safety validation (PTFE temp caps, speed limits),
            use ``validate_gcode_safe`` with a ``printer_id`` instead.

            Args:
                commands: One or more G-code commands separated by newlines.

            Returns a JSON object with:
            - ``valid``: whether all commands passed safety checks
            - ``commands``: the parsed command list
            - ``errors``: blocking issues (temperature limits, firmware commands)
            - ``warnings``: non-blocking advisories (Z below bed, high feedrate)
            - ``blocked_commands``: specific commands that were blocked

            Use this to preview what ``send_gcode`` would accept or reject.
            """
            from kiln.gcode import validate_gcode as _validate_gcode_impl

            raw_lines = re.split(r"[\n\r]+", commands.strip())
            cmd_list = [line.strip() for line in raw_lines if line.strip()]

            if not cmd_list:
                return _srv._error_dict("No commands provided.", code="INVALID_ARGS")

            result = _validate_gcode_impl(cmd_list)
            return {
                "success": True,
                "valid": result.valid,
                "commands": result.commands,
                "errors": result.errors,
                "warnings": result.warnings,
                "blocked_commands": result.blocked_commands,
            }

        # ------------------------------------------------------------------
        # validate_gcode_safe
        # ------------------------------------------------------------------

        @mcp.tool()
        def validate_gcode_safe(
            commands: str,
            printer_id: str = "",
        ) -> dict:
            """Validate G-code with printer-specific safety limits (PTFE temp caps, speed limits).

            Preferred over ``validate_gcode`` when you know the target printer — uses
            that printer's safety profile for accurate limits.  Without a printer_id,
            falls back to conservative generic defaults.

            Args:
                commands: G-code commands separated by newlines.
                printer_id: Optional printer model ID for profile-aware validation.
            """
            from kiln.gcode import validate_gcode as _validate_gcode_impl
            from kiln.gcode import validate_gcode_for_printer
            from kiln.safety_profiles import get_profile

            if err := _srv._check_auth("gcode"):
                return err
            try:
                if printer_id:
                    result = validate_gcode_for_printer(commands, printer_id)
                    profile = get_profile(printer_id)
                    profile_info = {
                        "id": profile.id,
                        "display_name": profile.display_name,
                    }
                else:
                    result = _validate_gcode_impl(commands)
                    profile_info = {"id": "default", "display_name": "Generic defaults"}

                return {
                    "success": True,
                    "valid": result.valid,
                    "profile": profile_info,
                    "commands_accepted": len(result.commands),
                    "commands_blocked": len(result.blocked_commands),
                    "warnings": result.warnings,
                    "errors": result.errors,
                    "blocked_commands": result.blocked_commands,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in validate_gcode_safe")
                return _srv._error_dict(
                    f"Unexpected error in validate_gcode_safe: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # validate_print_quality
        # ------------------------------------------------------------------

        @mcp.tool()
        def validate_print_quality(
            job_id: str | None = None,
            printer_name: str | None = None,
            save_snapshot: str | None = None,
        ) -> dict:
            """Validate print quality after a completed print job.

            Captures a webcam snapshot (if available), examines the job record and
            events, and produces a quality assessment with recommendations.

            Args:
                job_id: The completed job's ID.  If omitted, uses the most recent
                    completed job.
                printer_name: Target printer name (omit for default printer).
                save_snapshot: Optional file path to save the post-print snapshot.

            Returns a quality report with snapshot data, job metrics, and any
            detected issues.
            """
            from kiln.printers.base import PrinterError
            from kiln.queue import JobNotFoundError, JobStatus
            from kiln.registry import PrinterNotFoundError

            try:
                import base64

                # Resolve the job
                target_job = None
                if job_id:
                    try:
                        target_job = _srv._get_queue().get_job(job_id)
                    except JobNotFoundError:
                        return _srv._error_dict(f"Job {job_id!r} not found.", code="JOB_NOT_FOUND")
                else:
                    # Find most recent completed job
                    recent = _srv._get_queue().list_jobs(limit=20)
                    for j in recent:
                        if j.status == JobStatus.COMPLETED:
                            target_job = j
                            break
                    if target_job is None:
                        return _srv._error_dict(
                            "No completed jobs found. Provide a job_id explicitly.",
                            code="NO_COMPLETED_JOB",
                        )

                job_data = target_job.to_dict()

                # Gather adapter for snapshot
                if printer_name:
                    adapter = _srv._get_registry().get(printer_name)
                else:
                    try:
                        adapter = _srv._get_adapter()
                    except RuntimeError:
                        adapter = None

                # Capture snapshot
                snapshot_info: dict[str, Any] = {"available": False}
                if adapter is not None:
                    try:
                        image_data = adapter.get_snapshot()
                        if image_data is not None:
                            snapshot_info = {
                                "available": True,
                                "size_bytes": len(image_data),
                            }
                            if save_snapshot:
                                # Sanitise path — restrict to home dir or temp dir
                                _safe = os.path.abspath(save_snapshot)
                                _home = os.path.expanduser("~")
                                _tmpdir = os.path.realpath(tempfile.gettempdir())
                                if not (_safe.startswith(_home) or _safe.startswith(_tmpdir)):
                                    return _srv._error_dict(
                                        "save_snapshot path must be under home directory or temp directory.",
                                        code="VALIDATION_ERROR",
                                    )
                                os.makedirs(os.path.dirname(_safe) or ".", exist_ok=True)
                                with open(_safe, "wb") as f:
                                    f.write(image_data)
                                snapshot_info["saved_to"] = _safe
                            else:
                                snapshot_info["image_base64"] = base64.b64encode(image_data).decode("ascii")
                    except Exception as snap_exc:
                        snapshot_info = {"available": False, "error": str(snap_exc)}

                # Gather related events
                all_events = _srv._get_event_bus().recent_events(limit=200)
                job_events = [e.to_dict() for e in all_events if e.data.get("job_id") == target_job.id]

                # Analyse quality indicators
                issues: list[str] = []
                metrics: dict[str, Any] = {}
                recommendations: list[str] = []

                # Duration analysis
                if target_job.elapsed_seconds is not None:
                    metrics["print_duration_seconds"] = target_job.elapsed_seconds
                    metrics["print_duration_hours"] = round(target_job.elapsed_seconds / 3600, 2)

                # Check for retries (may indicate intermittent problems)
                retry_events = [e for e in job_events if e.get("data", {}).get("retry")]
                if retry_events:
                    issues.append(f"Job required {len(retry_events)} retry attempt(s) before completing")
                    recommendations.append(
                        "Retries during a print may indicate connectivity or mechanical issues. "
                        "Inspect the print closely for layer shifts or gaps."
                    )

                # Check progress consistency
                progress_events = [
                    e for e in job_events if e.get("type") in ("print.progress", "job.progress")
                ]
                if progress_events:
                    completions = [e.get("data", {}).get("completion", 0) for e in progress_events]
                    # Detect non-monotonic progress (resets may indicate issues)
                    for i in range(1, len(completions)):
                        if completions[i] < completions[i - 1] - 5:
                            issues.append(
                                f"Progress dropped from {completions[i - 1]:.0f}% to "
                                f"{completions[i]:.0f}% — possible restart or error recovery"
                            )
                            break

                # Snapshot-based hints (we can't do actual vision analysis here,
                # but we can note the snapshot is available for the agent to inspect)
                if snapshot_info.get("available"):
                    recommendations.append(
                        "A post-print snapshot was captured. Visually inspect it for: "
                        "stringing, layer shifts, warping, incomplete layers, or "
                        "spaghetti-like extrusion failures."
                    )
                else:
                    recommendations.append(
                        "No webcam available for visual inspection. "
                        "Consider adding a camera for automated quality checks."
                    )

                # Overall quality grade
                if not issues:
                    grade = "PASS"
                    summary = "Print completed successfully with no detected issues."
                elif len(issues) <= 2:
                    grade = "WARNING"
                    summary = "Print completed but with potential quality concerns."
                else:
                    grade = "REVIEW"
                    summary = "Print completed with multiple issues detected. Manual inspection recommended."

                return {
                    "success": True,
                    "job": job_data,
                    "quality": {
                        "grade": grade,
                        "summary": summary,
                        "issues": issues,
                        "recommendations": recommendations,
                        "metrics": metrics,
                    },
                    "snapshot": snapshot_info,
                    "related_events": job_events[-10:],
                }

            except PrinterNotFoundError:
                return _srv._error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
            except (PrinterError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to validate print quality: {exc}. Check that the printer is online."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in validate_print_quality")
                return _srv._error_dict(
                    f"Unexpected error in validate_print_quality: {exc}",
                    code="INTERNAL_ERROR",
                )


plugin = _GcodeValidationToolsPlugin()
