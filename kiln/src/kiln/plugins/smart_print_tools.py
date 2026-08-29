"""Smart print tools plugin.

Provides a single ``retry_print_with_fix`` MCP tool that chains failure
diagnosis, override resolution, and slice-upload-print into one call.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from kiln.print_start_verdict import resolve_print_start
from kiln.tool_results import unwrap_tool_result

_logger = logging.getLogger(__name__)


class _SmartPrintToolsPlugin:
    """Smart print orchestration tools.

    Tools:
        - retry_print_with_fix
    """

    @property
    def name(self) -> str:
        return "smart_print_tools"

    @property
    def description(self) -> str:
        return "Diagnose a print failure and re-slice/print with fixes applied"

    def register(self, mcp: Any) -> None:
        """Register smart print tools with the MCP server."""

        @mcp.tool()
        def retry_print_with_fix(
            model_path: str,
            printer_name: str | None = None,
            material: str | None = None,
            printer_id: str | None = None,
            custom_overrides: str | None = None,
            skip_diagnosis: bool = False,
            skip_validation: bool = False,
            preview_token: str | None = None,
        ) -> dict:
            """Diagnose the last print failure and re-slice + print with fixes.

            When a print fails, call this tool instead of manually chaining
            ``diagnose_print_failure_live`` → ``slice_and_print``.  It:

            1. Reads live printer state and analyses the model geometry to
               diagnose the failure (unless ``skip_diagnosis`` is True).
            2. Auto-detects the loaded material from the AMS when
               ``material`` is omitted and the printer supports it.
            3. Merges diagnosis-recommended slicer overrides with any
               ``custom_overrides`` you supply (your overrides win on
               conflict).
            4. Re-validates the mesh's printability before re-slicing.  A
               retry path that re-sends a broken mesh is the highest-
               value place to validate — the previous attempt already
               failed, and slicer overrides can't fix mesh-level issues.
               Bypass with ``skip_validation=True`` if the caller already
               validated.
            5. Re-slices the (possibly auto-repaired) mesh with the merged
               overrides, uploads the result, and starts the print.

            Args:
                model_path: Path to the STL/OBJ/3MF that failed.
                printer_name: Target printer name.  Omit for the default
                    printer.
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``).
                    Auto-detected from AMS when omitted.
                printer_id: Printer model ID for intelligence lookup
                    (e.g. ``"bambu_a1"``).
                custom_overrides: JSON object of additional slicer overrides
                    to merge on top of the diagnosis recommendations
                    (e.g. ``'{"brim_width": "8"}'``).  Your values win on
                    conflict.
                skip_diagnosis: If True, skip the failure diagnosis step and
                    re-slice using only ``custom_overrides``.
                skip_validation: If True, bypass the pre-print mesh
                    validation gate.  Defaults to False — designs are
                    pre-tested for printability before the retry reaches
                    the printer.
            """
            import kiln.server as _srv
            from kiln.printability import (
                analyze_printability,
                diagnose_from_signals,
            )
            from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file
            from kiln.slicer_profiles import resolve_slicer_profile

            # ------------------------------------------------------------------
            # 0. Parse custom_overrides early so we can fail fast on bad JSON.
            # ------------------------------------------------------------------
            extra_overrides: dict[str, str] = {}
            if custom_overrides:
                try:
                    parsed = json.loads(custom_overrides)
                    if not isinstance(parsed, dict):
                        return _srv._error_dict(
                            "custom_overrides must be a JSON object (key-value pairs).",
                            code="VALIDATION_ERROR",
                        )
                    extra_overrides = {str(k): str(v) for k, v in parsed.items()}
                except json.JSONDecodeError as exc:
                    return _srv._error_dict(
                        f"custom_overrides is not valid JSON: {exc}",
                        code="VALIDATION_ERROR",
                    )

            # ------------------------------------------------------------------
            # 1. Resolve adapter + effective printer_id.
            # ------------------------------------------------------------------
            try:
                # _srv._registry is the raw module global and is None until
                # something calls _get_registry(); reaching through it reported
                # a configured printer as "could not connect".  The shared door
                # initialises the registry and falls back to config.yaml.
                adapter = _srv._resolve_adapter(printer_name)
            except Exception as exc:
                return _srv._error_dict(
                    f"Could not connect to printer: {exc}",
                    code="PRINTER_UNAVAILABLE",
                )

            effective_pid: str | None = _srv._resolve_printer_profile_id(
                printer_id, printer_name
            )

            # ------------------------------------------------------------------
            # 2. Auto-detect material from AMS when not supplied.
            # ------------------------------------------------------------------
            material_detected: str | None = None
            effective_material = material
            if effective_material is None and hasattr(adapter, "get_ams_status"):
                try:
                    ams = adapter.get_ams_status()
                    tray_now_str = ams.get("tray_now", "255")
                    # "255" means no tray / external spool.
                    with_suppress = True
                    try:
                        tray_now = int(tray_now_str)
                    except (TypeError, ValueError):
                        with_suppress = False
                        tray_now = 255

                    if with_suppress and tray_now != 255:
                        for unit in ams.get("units", []):
                            for tray in unit.get("trays", []):
                                if tray.get("slot") == tray_now:
                                    ttype = tray.get("tray_type", "")
                                    if ttype:
                                        material_detected = ttype.upper()
                                        effective_material = material_detected
                                    break
                            if material_detected:
                                break
                except Exception as exc:
                    _logger.debug("AMS material detection failed: %s", exc)

            # ------------------------------------------------------------------
            # 3. Diagnosis pipeline (skipped when skip_diagnosis=True).
            # ------------------------------------------------------------------
            diagnosis_dict: dict[str, Any] | None = None
            diagnosis_overrides: dict[str, str] = {}

            if not skip_diagnosis:
                try:
                    signals: dict[str, Any] = {}

                    # Printer state signals.
                    try:
                        state = adapter.get_state()
                        signals["tool_temp_actual"] = state.tool_temp_actual
                        signals["tool_temp_target"] = state.tool_temp_target
                        signals["bed_temp_actual"] = state.bed_temp_actual
                        signals["bed_temp_target"] = state.bed_temp_target
                        if state.print_error:
                            signals["print_error"] = state.print_error
                    except Exception as exc:
                        _logger.debug("Could not read printer state: %s", exc)

                    # Model geometry signals.
                    if model_path and model_path.lower().endswith(
                        (".stl", ".obj", ".3mf")
                    ):
                        try:
                            report = analyze_printability(
                                model_path,
                                material=material or "pla",
                                printer_id=printer_id or None,
                            )
                            if report.bed_adhesion:
                                signals["adhesion_risk"] = (
                                    report.bed_adhesion.adhesion_risk
                                )
                                signals["contact_percentage"] = (
                                    report.bed_adhesion.contact_percentage
                                )
                            if report.overhangs:
                                signals["overhang_pct"] = (
                                    report.overhangs.overhang_percentage
                                )
                            if report.bridging:
                                signals["max_bridge_mm"] = (
                                    report.bridging.max_bridge_length
                                )
                        except Exception as exc:
                            _logger.debug("Model analysis failed: %s", exc)

                    # Printer intelligence signals.
                    if effective_pid:
                        try:
                            from kiln.printer_intelligence import (
                                diagnose_issue,
                                get_printer_intel,
                            )

                            intel = get_printer_intel(effective_pid)
                            if intel:
                                signals["printer_has_enclosure"] = intel.get(
                                    "has_enclosure", False
                                )
                                symptom_queries = _build_symptom_queries(signals)
                                modes: list[dict[str, str]] = []
                                for symptom in symptom_queries:
                                    modes.extend(
                                        diagnose_issue(effective_pid, symptom)
                                    )
                                if modes:
                                    signals["failure_modes_from_intel"] = modes
                        except Exception as exc:
                            _logger.debug(
                                "Printer intelligence lookup failed: %s", exc
                            )

                    if effective_material:
                        signals["material"] = effective_material.upper()

                    diagnosis = diagnose_from_signals(
                        signals,
                        printer_id=effective_pid,
                        material=effective_material,
                    )
                    diagnosis_dict = diagnosis.to_dict()
                    # Pull out the recommended slicer overrides.
                    raw_overrides = diagnosis_dict.get("slicer_overrides") or {}
                    if isinstance(raw_overrides, dict):
                        diagnosis_overrides = {
                            str(k): str(v) for k, v in raw_overrides.items()
                        }
                except Exception as exc:
                    _logger.warning(
                        "Diagnosis pipeline failed, continuing without: %s", exc
                    )

            # ------------------------------------------------------------------
            # 4. Merge overrides: diagnosis first, custom_overrides win.
            # ------------------------------------------------------------------
            merged_overrides: dict[str, str] = {**diagnosis_overrides, **extra_overrides}

            # ------------------------------------------------------------------
            # 5. Resolve slicer profile with merged overrides.
            # ------------------------------------------------------------------
            effective_profile: str | None = None
            if effective_pid:
                try:
                    effective_profile = resolve_slicer_profile(
                        effective_pid,
                        overrides=merged_overrides if merged_overrides else None,
                    )
                except Exception as exc:
                    _logger.debug(
                        "Profile resolution failed for %s: %s", effective_pid, exc
                    )

            # If no bundled profile but no overrides either, fall back to
            # letting the slicer use its built-in defaults.

            # ------------------------------------------------------------------
            # 5b. Pre-print validation gate.
            #
            # A retry is the most consequential place to validate: the
            # previous attempt failed, and slicer-override fixes can only
            # paper over mesh-level issues (non-manifold, paper-thin
            # walls, intersecting volumes).  Re-sending the same broken
            # mesh through a re-slice will fail the same way.  Auto-
            # repair the mesh before slicing; block the retry on a
            # ready_to_print=False verdict.  Bypass with skip_validation=True.
            # ------------------------------------------------------------------
            validation_summary: dict | None = None
            if not skip_validation:
                try:
                    from kiln.plugins._validation_pipeline_internals import (
                        _SUPPORTED_FORMATS,
                    )
                    from kiln.plugins.validation_pipeline_tools import (
                        run_full_validation_pipeline,
                    )

                    _ext = os.path.splitext(model_path)[1].lower()
                    if _ext in _SUPPORTED_FORMATS:
                        val_report = run_full_validation_pipeline(
                            model_path,
                            printer_id=effective_pid or "",
                            material=effective_material or "",
                        )
                        if not val_report.get("ready_to_print", True):
                            score = val_report.get("printability_score", 0)
                            summary = val_report.get(
                                "summary", "Validation failed",
                            )
                            err_resp = _srv._error_dict(
                                f"Retry blocked — mesh failed pre-print "
                                f"validation (score {score}/100): {summary} "
                                f"Slicer-override fixes won't repair the "
                                f"underlying mesh.  Pass skip_validation=True "
                                f"to bypass.",
                                code="VALIDATION_FAILED",
                            )
                            err_resp["validation"] = val_report
                            return err_resp

                        validated_path = (
                            val_report.get("validated_path") or model_path
                        )
                        if validated_path and validated_path != model_path:
                            _logger.info(
                                "retry_print_with_fix: using validated path "
                                "%s (repaired=%s)",
                                validated_path,
                                val_report.get("repaired", False),
                            )
                            model_path = validated_path

                        validation_summary = {
                            "printability_score": val_report.get(
                                "printability_score",
                            ),
                            "ready_to_print": val_report.get("ready_to_print"),
                            "repaired": val_report.get("repaired"),
                            "summary": val_report.get("summary"),
                        }
                except ImportError:
                    _logger.debug(
                        "Validation pipeline unavailable, proceeding without",
                        exc_info=True,
                    )
                except Exception:
                    # Validator infrastructure failure must not block a
                    # legitimate retry — log and proceed.
                    _logger.warning(
                        "Validation pipeline raised — proceeding without gate",
                        exc_info=True,
                    )

            # ------------------------------------------------------------------
            # 6. Slice, upload, print — mirroring slice_and_print's flow.
            # ------------------------------------------------------------------
            try:
                slice_result = slice_file(model_path, profile=effective_profile)
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Slicer not found: {exc}. "
                    "Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(
                    f"Slicing failed: {exc}", code="SLICER_ERROR"
                )
            except FileNotFoundError as exc:
                return _srv._error_dict(
                    f"Model file not found: {exc}", code="FILE_NOT_FOUND"
                )

            # Bambu 3MF wrapping.
            upload_path = slice_result.output_path
            if (
                hasattr(adapter, "wrap_gcode_as_3mf")
                and slice_result.output_path.endswith(".gcode")
            ):
                try:
                    from kiln.printers.bambu_3mf import (
                        thumbnail_inputs_for_model,
                    )

                    # Hand the wrap the model it was sliced from, or the
                    # printer shows a blank tile for a retry the user is
                    # already watching more closely than a first attempt.
                    _stl_paths, _source_3mf = thumbnail_inputs_for_model(
                        model_path
                    )
                    upload_path = adapter.wrap_gcode_as_3mf(
                        slice_result.output_path,
                        stl_paths=_stl_paths,
                        source_3mf_path=_source_3mf,
                    )
                    _logger.info("Wrapped gcode as Bambu 3MF: %s", upload_path)
                except Exception:
                    _logger.warning(
                        "Bambu 3MF wrapping failed, uploading raw gcode",
                        exc_info=True,
                    )

            try:
                upload_result = adapter.upload_file(upload_path)
            except Exception as exc:
                return _srv._error_dict(
                    f"Upload failed: {exc}", code="UPLOAD_ERROR"
                )

            file_name = upload_result.file_name or os.path.basename(upload_path)

            # Pre-flight safety gate.
            safety_name = _srv._resolve_effective_printer_name(printer_name)
            if block := _srv._emergency_latch_error(
                "retry_print_with_fix", safety_name
            ):
                return block
            pf = unwrap_tool_result(_srv.preflight_check(printer_name=printer_name))
            if not pf.get("ready", False):
                return _srv._error_dict(
                    pf.get("summary", "Pre-flight checks failed"),
                    code="PREFLIGHT_FAILED",
                )

            try:
                # Captured before the command: it is what lets the verdict
                # below tell a reading about THIS job from the printer's last
                # word about the previous one.
                # A reprint of the same object with a hotter nozzle is the
                # print already approved.  One whose mesh was repaired, or
                # whose overrides move supports/orientation/scale, is a
                # different object leaning on the old approval.
                _mesh_repaired = bool(
                    (validation_summary or {}).get("repaired")
                )
                if _srv._retry_changes_the_object(
                    merged_overrides, _mesh_repaired
                ) and (
                    block := _srv._preview_gate_error(
                        "retry_print_with_fix", model_path, preview_token,
                        printer_name=printer_name,
                    )
                ):
                    return block
                sent_at = time.monotonic()
                print_result = adapter.start_print(file_name)
            except Exception as exc:
                return _srv._error_dict(
                    f"Failed to start print: {exc}", code="PRINT_ERROR"
                )

            _srv._note_print_started(adapter)

            # ------------------------------------------------------------------
            # 7. Build response message.
            # ------------------------------------------------------------------
            model_name = os.path.basename(model_path)
            msg_parts: list[str] = []
            if diagnosis_dict:
                category = diagnosis_dict.get("failure_category", "unknown")
                msg_parts.append(f"Diagnosed {category} failure.")
            if merged_overrides:
                override_summary = ", ".join(
                    f"{k}={v}" for k, v in list(merged_overrides.items())[:3]
                )
                if len(merged_overrides) > 3:
                    override_summary += f" (+{len(merged_overrides) - 3} more)"
                msg_parts.append(f"Applied overrides: {override_summary}.")
            verdict = resolve_print_start(
                adapter, print_result, sent_at=sent_at, file_name=model_name,
            )
            if verdict.confirmed:
                msg_parts.append(f"Re-sliced and started printing {model_name}.")
            elif verdict.ok:
                msg_parts.append(
                    f"Re-sliced {model_name} and sent the print command. The "
                    f"printer has not confirmed it is running yet — call "
                    f"printer_status() to watch it start."
                )
            else:
                msg_parts.append(
                    f"Re-sliced {model_name}, but the printer did not start "
                    f"it. {verdict.message}"
                )
            message = "  ".join(msg_parts)

            result: dict[str, Any] = {
                "success": verdict.ok,
                "print_start": verdict.state,
                "diagnosis": diagnosis_dict,
                "material_detected": material_detected,
                "overrides_applied": merged_overrides,
                "slice": slice_result.to_dict(),
                "upload": upload_result.to_dict(),
                "print": verdict.to_dict(),
                "message": message,
            }
            if validation_summary is not None:
                result["validation"] = validation_summary
            if effective_pid:
                result["printer_id"] = effective_pid
            if effective_profile:
                result["profile_path"] = effective_profile
            return result

        _logger.debug("Registered smart print tools")


def _build_symptom_queries(signals: dict[str, Any]) -> list[str]:
    """Build symptom query strings for printer intelligence lookup."""
    queries: list[str] = []

    risk = signals.get("adhesion_risk")
    if risk == "high":
        queries.append("bed adhesion failure")
        queries.append("print detached from bed")
    elif risk == "medium":
        queries.append("poor bed adhesion")

    tool_actual = signals.get("tool_temp_actual")
    tool_target = signals.get("tool_temp_target")
    if tool_actual is not None and tool_target is not None and abs(tool_actual - tool_target) > 10:
        queries.append("temperature fluctuation")

    if signals.get("print_error"):
        queries.append(str(signals["print_error"]))

    if signals.get("overhang_pct", 0) > 30:
        queries.append("overhang failure")
    if signals.get("max_bridge_mm", 0) > 15:
        queries.append("bridge failure")

    mat = signals.get("material", "")
    if mat.upper() in {"ABS", "ASA", "PA", "PC"} and not signals.get(
        "printer_has_enclosure"
    ):
        queries.append("warping")
        queries.append("layer splitting")

    if not queries:
        queries.append("print failure")

    return queries


plugin = _SmartPrintToolsPlugin()
