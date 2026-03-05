"""Slice-and-estimate plugin — slice without printing.

Provides a single MCP tool, ``slice_and_estimate``, that slices a 3D model
and returns time/filament estimates, printability analysis, and adhesion
recommendations **without** uploading or printing anything.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRINTABLE_EXTENSIONS = {".stl", ".obj", ".3mf"}


def _format_time(seconds: int | None) -> str:
    """Convert seconds to a human-readable duration string.

    Args:
        seconds: Duration in seconds, or ``None``.

    Returns:
        A string like ``"1h 30m"``, ``"45m"``, or ``"unknown"``.
    """
    if seconds is None:
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class _EstimateToolsPlugin:
    """Slice-and-estimate tools.

    Tools:
        - slice_and_estimate
    """

    @property
    def name(self) -> str:
        return "estimate_tools"

    @property
    def description(self) -> str:
        return "Slice a 3D model and return time/filament estimates without printing"

    def register(self, mcp: Any) -> None:
        """Register estimate tools with the MCP server."""

        @mcp.tool()
        def slice_and_estimate(
            input_path: str,
            printer_id: str | None = None,
            profile: str | None = None,
            material: str = "PLA",
        ) -> dict:
            """Slice a 3D model and return estimates WITHOUT printing.

            Slices the model using PrusaSlicer or OrcaSlicer, parses the
            output G-code for time and filament metadata, runs printability
            analysis (for STL/OBJ/3MF inputs), and returns adhesion
            recommendations — all without uploading or starting a print.

            Use this tool to answer "how long will this take?" or "how much
            filament will I use?" before committing to a print job.

            Args:
                input_path: Path to the input file (STL, OBJ, 3MF, STEP, AMF).
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"bambu_a1"``, ``"prusa_mini"``).
                profile: Path to a slicer profile/config file (.ini or .json).
                    Takes precedence over ``printer_id`` auto-selection.
                material: Filament material for weight and adhesion estimates
                    (e.g. ``"PLA"``, ``"PETG"``, ``"ABS"``).  Default is
                    ``"PLA"``.
            """
            import kiln.server as _srv
            from kiln.gcode_metadata import extract_metadata
            from kiln.printability import (
                analyze_printability,
                is_bedslinger,
                recommend_adhesion,
            )
            from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

            try:
                # 1. Resolve the slicer profile
                effective_printer_id, effective_profile = _srv._resolve_slice_profile_context(
                    profile=profile,
                    printer_id=printer_id,
                )

                # 2. Slice the model
                result = slice_file(input_path, profile=effective_profile)

                # 3. Parse gcode metadata
                meta = None
                if result.output_path and os.path.isfile(result.output_path):
                    try:
                        meta = extract_metadata(result.output_path)
                    except Exception as exc:
                        _logger.debug("Could not extract gcode metadata: %s", exc)

                # 4. Build estimate dict
                mat_upper = material.upper() if material else "PLA"
                filament_mm = meta.filament_used_mm if meta else None
                filament_g: float | None = None
                if filament_mm is not None:
                    filament_g = round(filament_mm * 0.003, 1)

                time_sec = meta.estimated_time_seconds if meta else None
                time_human = _format_time(time_sec)
                slicer_name = (meta.slicer if meta and meta.slicer else None) or result.slicer

                estimate: dict[str, Any] = {
                    "estimated_time_seconds": time_sec,
                    "estimated_time_human": time_human,
                    "filament_used_mm": filament_mm,
                    "filament_used_grams": filament_g,
                    "material": mat_upper,
                    "slicer": slicer_name,
                }

                # 5. Printability analysis (STL/OBJ/3MF only)
                ext = os.path.splitext(input_path)[1].lower()
                printability_dict: dict[str, Any] | None = None
                adhesion_dict: dict[str, Any] | None = None
                adhesion_rationale: str | None = None

                if ext in _PRINTABLE_EXTENSIONS:
                    try:
                        report = analyze_printability(input_path)
                        printability_dict = report.to_dict()

                        # 6. Adhesion recommendation
                        if report.bed_adhesion is not None:
                            has_enclosure = False
                            is_bs = False
                            if effective_printer_id:
                                is_bs = is_bedslinger(effective_printer_id)
                                try:
                                    from kiln.printer_intelligence import get_printer_intel

                                    intel = get_printer_intel(effective_printer_id)
                                    if intel:
                                        has_enclosure = intel.get("has_enclosure", False)
                                except Exception:
                                    pass

                            rec = recommend_adhesion(
                                report.bed_adhesion,
                                material=mat_upper,
                                has_enclosure=has_enclosure,
                                is_bedslinger_printer=is_bs,
                                model_height_mm=report.model_height_mm,
                            )
                            adhesion_dict = rec.to_dict()
                            adhesion_rationale = rec.rationale
                    except Exception as exc:
                        _logger.debug("Printability/adhesion analysis failed: %s", exc)

                # 7. Build human-readable summary message
                parts: list[str] = [f"Estimated {time_human}"]
                if filament_g is not None:
                    parts[0] += f", {filament_g}g {mat_upper}"
                if printability_dict:
                    score = printability_dict.get("score")
                    grade = printability_dict.get("grade")
                    if score is not None and grade:
                        parts.append(f"Printability: {grade} ({score}/100)")
                if adhesion_rationale:
                    parts.append(adhesion_rationale)
                message = ". ".join(parts) + "."

                # 8. Assemble response
                response: dict[str, Any] = {
                    "success": True,
                    "slice": result.to_dict(),
                    "estimate": estimate,
                    "printability": printability_dict,
                    "adhesion": adhesion_dict,
                    "printer_id": effective_printer_id,
                    "profile_path": effective_profile,
                    "message": message,
                }
                return response

            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}",
                    code="SLICER_ERROR",
                )
            except FileNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}",
                    code="FILE_NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in slice_and_estimate")
                return _srv._error_dict(
                    f"Unexpected error in slice_and_estimate: {exc}",
                    code="INTERNAL_ERROR",
                )

        _logger.debug("Registered estimate tools")


plugin = _EstimateToolsPlugin()
