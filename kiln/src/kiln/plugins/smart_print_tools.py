"""Smart print tools plugin.

Provides a single ``retry_print_with_fix`` MCP tool that chains failure
diagnosis, override resolution, and slice-upload-print into one call.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from kiln.print_start_verdict import resolve_print_start
from kiln.tool_args import parse_json_object
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
            custom_overrides: str | dict[str, Any] | None = None,
            skip_diagnosis: bool = False,
            skip_validation: bool = False,
            preview_token: str | None = None,
            placement: str | list[float] | None = None,
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
                model_path: Path to the model that failed (STL, OBJ, 3MF,
                    STEP, ...).  One the diagnosis cannot read is said in
                    ``printability_note``.
                printer_name: Target printer name.  Omit for the default
                    printer.
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``); the
                    reslice is set for it (temperatures, melt rate,
                    cooling).  Auto-detected from AMS when omitted.
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
                placement: Where the part goes when the plate still holds
                    the last print: ``[x, y]`` in mm, a named region
                    (``"front-left"``, ``"centre"``, …), or ``"keep"``.
                    Omitted, an occupied plate refuses before slicing and
                    lists the spots that would work.  The clearance verdict
                    is free; placing and starting a second print on an
                    occupied plate is a kiln-pro feature
                    (https://kiln3d.com/pricing).
            """
            import kiln.server as _srv
            if err := _srv._check_auth("print"):
                return err

            from kiln.printability import (
                analyze_printability,
                collect_failure_signals,
                diagnose_from_signals,
            )
            from kiln.slicer import SlicerError, SlicerNotFoundError
            from kiln.slicer_profiles import (
                resolve_slicer_profile,
                start_gcode_override_from_printer,
            )

            # ------------------------------------------------------------------
            # 0. Parse custom_overrides early so we can fail fast on bad JSON.
            # ------------------------------------------------------------------
            parsed, _arg_err = parse_json_object(custom_overrides, "custom_overrides")
            if _arg_err is not None:
                return _arg_err
            extra_overrides: dict[str, str] = {
                str(k): str(v) for k, v in (parsed or {}).items()
            }

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
            # The one reader every slicing door uses (the global tray id,
            # the second AMS unit, the external spool, the A1's tray_now=255
            # with trays loaded are all its business, not this door's), so
            # the retry weighs the print with the same spool slice_and_print
            # would on the same reading.
            material_detected: str | None = None
            effective_material = material
            if effective_material is None:
                from kiln.slicer_filament import loaded_filament_type

                material_detected = loaded_filament_type(adapter)
                if material_detected:
                    effective_material = material_detected

            # ------------------------------------------------------------------
            # 3. Diagnosis pipeline (skipped when skip_diagnosis=True).
            # ------------------------------------------------------------------
            diagnosis_dict: dict[str, Any] | None = None
            diagnosis_overrides: dict[str, str] = {}
            printability_note: str | None = None

            if not skip_diagnosis:
                try:
                    state = None
                    try:
                        state = adapter.get_state()
                    except Exception as exc:
                        _logger.debug("Could not read printer state: %s", exc)

                    # Every format the engine reads, a STEP as Kiln's mesh of
                    # it; one it cannot read is said in the result, and the
                    # diagnosis goes on with what the printer reported.
                    report = None
                    if model_path:
                        try:
                            report = analyze_printability(
                                model_path,
                                material=material or "pla",
                                printer_id=printer_id or None,
                            )
                        except Exception as exc:
                            _logger.debug("Model analysis failed: %s", exc)
                            printability_note = (
                                "The diagnosis was made without the model's geometry: "
                                f"{' '.join(str(exc).split())}"
                            )

                    # The one way every diagnosis door gathers its signals.
                    signals = collect_failure_signals(
                        state=state,
                        report=report,
                        printer_id=effective_pid,
                        material=effective_material,
                    )

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

            # Printer's own start routine (kiln-pro handoff): the adapter is
            # already in hand from step 1, and a retry is precisely where the
            # machine's own PRINT_START — chamber, mesh, purge — matters most.
            start_handoff: str | None = None
            _sg_patch, _sg_reason = start_gcode_override_from_printer(
                adapter, effective_pid, merged_overrides, material=effective_material,
            )
            if _sg_patch:
                merged_overrides.update(_sg_patch)
                start_handoff = _sg_reason.removeprefix("handoff:")
            else:
                _logger.debug("start-gcode handoff declined: %s", _sg_reason)

            # ------------------------------------------------------------------
            # 5. Resolve slicer profile with merged overrides.
            # ------------------------------------------------------------------
            effective_profile: str | None = None
            if effective_pid:
                try:
                    effective_profile = resolve_slicer_profile(
                        effective_pid,
                        overrides=merged_overrides if merged_overrides else None,
                        printer_name=printer_name,
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
                from kiln.plugins.validation_pipeline_tools import gate_for_print

                gate = gate_for_print(
                    model_path,
                    printer_id=effective_pid or "",
                    material=effective_material or "",
                )
                if gate.reason:
                    mesh_note = (
                        " Slicer-override fixes won't repair the underlying mesh."
                        if gate.code == "VALIDATION_FAILED" else ""
                    )
                    err_resp = _srv._error_dict(
                        f"Retry blocked — {gate.reason}{mesh_note} {gate.how_to_bypass}",
                        code=gate.code,
                    )
                    if gate.report is not None:
                        err_resp["validation"] = gate.report
                    return err_resp
                if gate.path != model_path:
                    _logger.info("retry_print_with_fix: using validated path %s", gate.path)
                    model_path = gate.path
                validation_summary = gate.summary

            # ------------------------------------------------------------------
            # 6. Slice, upload, print — mirroring slice_and_print's flow.
            # ------------------------------------------------------------------
            # The plate may still hold the print that failed: the shared
            # step every slice door takes (plate gate, slicer, a skirt or
            # brim past the bed's edge settled, the second verdict on the
            # sliced file).  A retry is where a diagnosis adds a wide brim
            # to a part that already failed.
            from kiln.plugins.slicer_tools import _attach_placement, _placed_slice

            try:
                # The density the slicer weighs the print with: what was
                # declared, else the tray detected above (kiln.slicer_filament).
                slice_result, slice_err, sinfo = _placed_slice(
                    model_path, effective_printer_id=effective_pid, printer_name=printer_name,
                    placement=placement, profile_path=effective_profile, adapter=adapter,
                    material=material, loaded_material=material_detected,
                )
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Slicer not found: {exc}. "
                    "Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(
                    f"Slicing failed: {exc}", code=getattr(exc, "code", "SLICER_ERROR")
                )
            except FileNotFoundError as exc:
                return _srv._error_dict(
                    f"Model file not found: {exc}", code="FILE_NOT_FOUND"
                )
            if slice_err is not None:
                return slice_err
            model_path, place_info = sinfo["effective_input"], sinfo["placement"]
            # A plate that still holds the print that failed is never
            # started onto: the file carries the maker's own start sequence.
            # Refused before the wrap and the upload, with the slice and its
            # verdict attached so the work is not lost.
            # A slice the verdict plans a quiet start for goes on: the wrap
            # writes the plan into the file and the pre-print gate judges it
            # against the printer at the moment of the start.
            from kiln.plate_state import start_refusal

            quiet_plan = sinfo["quiet_start"]
            lift_floor = sinfo["lift_floor_mm"]
            if quiet_plan is None and (block := start_refusal(adapter)):
                block["slice"] = slice_result.to_dict()
                _attach_placement(block, place_info)
                return block

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
                        quiet_start=quiet_plan,
                        lift_floor_mm=lift_floor,
                    )
                    _logger.info("Wrapped gcode as Bambu 3MF: %s", upload_path)
                except Exception as exc:
                    if quiet_plan is not None or lift_floor is not None:
                        # A raw file, or one without the plan, would carry
                        # the vendor's start onto the occupied plate.
                        return _srv._error_dict(
                            f"Kiln could not write the file for a start beside what is on the plate ({exc}), "
                            "so it won't hand a file on. Clear the plate and say so, then retry.",
                            code="QUIET_START_WRAP_FAILED",
                        )
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
                ):
                    if block := _srv._preview_gate_error(
                        "retry_print_with_fix", model_path, preview_token,
                        printer_name=printer_name,
                    ):
                        return block
                else:
                    from kiln import print_signoff

                    # Same object, new settings: the yes the first print got
                    # still stands, and the adapter template is told so.
                    print_signoff.grant(
                        "retry_print_with_fix", file_name, printer_name,
                        source=print_signoff.SOURCE_PRIOR_APPROVAL,
                    )
                if quiet_plan is not None and (
                    block := start_refusal(adapter, file_name=file_name, local_path=upload_path)
                ):
                    block["slice"] = slice_result.to_dict()
                    _attach_placement(block, place_info)
                    return block
                sent_at = time.monotonic()
                start_kwargs: dict[str, Any] = {}
                if upload_path.lower().endswith(".3mf") and os.path.isfile(upload_path):
                    start_kwargs["local_file_path"] = upload_path
                print_result = adapter.start_print(file_name, **start_kwargs)
            except Exception as exc:
                return _srv._error_dict(
                    f"Failed to start print: {exc}", code="PRINT_ERROR"
                )

            _srv._note_print_started(adapter, print_result)

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
                vendor_start_block=quiet_plan is None,
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
            # Top-level beside the message, as start_print carries it.
            if verdict.what_you_will_see:
                result["what_you_will_see"] = list(verdict.what_you_will_see)
            if validation_summary is not None:
                result["validation"] = validation_summary
            if printability_note:
                result["printability_note"] = printability_note
            if effective_pid:
                result["printer_id"] = effective_pid
            if effective_profile:
                result["profile_path"] = effective_profile
            _attach_placement(result, place_info)
            if start_handoff:
                result["start_gcode_source"] = (
                    f"{start_handoff} — the printer's own start routine"
                )
            return result

        _logger.debug("Registered smart print tools")


plugin = _SmartPrintToolsPlugin()
