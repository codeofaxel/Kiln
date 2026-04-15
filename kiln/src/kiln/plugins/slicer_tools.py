"""Slicer tools plugin.

Extracts slicer-domain MCP tools from server.py into a focused plugin
module.  All tools delegate to helpers and singletons defined in
server.py via lazy ``import kiln.server as _srv``.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


class _SlicerToolsPlugin:
    """Slicer tools: slice, reslice, find slicer, list/get profiles.

    Tools:
        - slice_model
        - slice_and_print
        - reslice_with_overrides
        - find_slicer  (name override)
        - list_slicer_profiles  (name override)
        - get_slicer_profile  (name override)
    """

    @property
    def name(self) -> str:
        return "slicer_tools"

    @property
    def description(self) -> str:
        return "Slicer tools: slice, reslice, find slicer, list/get profiles"

    def register(self, mcp: Any) -> None:  # noqa: C901, PLR0915
        """Register slicer tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # slice_model
        # ------------------------------------------------------------------

        @mcp.tool()
        def slice_model(
            input_path: str,
            output_dir: str | None = None,
            profile: str | None = None,
            printer_id: str | None = None,
            slicer_path: str | None = None,
        ) -> dict:
            """Slice a 3D model (STL/3MF/STEP) to G-code using PrusaSlicer or OrcaSlicer.

            Args:
                input_path: Path to the input file (STL, 3MF, STEP, OBJ, AMF).
                output_dir: Directory for the output G-code.  Defaults to
                    the system temp directory.
                profile: Path to a slicer profile/config file (.ini or .json).
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"prusa_mini"``).
                slicer_path: Explicit path to the slicer binary.  Auto-detected
                    if omitted.

            Returns a JSON object with the output G-code path.  The output file
            can then be uploaded to a printer with ``upload_file`` and printed
            with ``start_print``.
            """
            if err := _srv._check_auth("slicer"):
                return err

            try:
                from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file
                from kiln.slicer_profiles import validate_profile_for_printer

                effective_printer_id, effective_profile = _srv._resolve_slice_profile_context(
                    profile=profile,
                    printer_id=printer_id,
                )
                result = slice_file(
                    input_path,
                    output_dir=output_dir,
                    profile=effective_profile,
                    slicer_path=slicer_path,
                )
                response: dict[str, Any] = {
                    "success": True,
                    **result.to_dict(),
                }
                if effective_printer_id:
                    response["printer_id"] = effective_printer_id
                if effective_profile:
                    response["profile_path"] = effective_profile

                # Cross-check slicer profile against printer safety limits
                if _srv._PRINTER_MODEL and effective_profile:
                    # Extract profile_id from the profile path or use printer model
                    _profile_id = effective_printer_id or os.path.basename(effective_profile).split("_")[0]
                    if _profile_id:
                        validation = validate_profile_for_printer(_profile_id, _srv._PRINTER_MODEL)
                        if validation["warnings"] or validation["errors"]:
                            response["profile_validation"] = validation
                            if validation["errors"]:
                                response["profile_validation_warning"] = (
                                    f"Slicer profile may be incompatible with {_srv._PRINTER_MODEL}: "
                                    + "; ".join(validation["errors"])
                                )
                            elif validation["warnings"]:
                                response["profile_validation_warning"] = "Profile compatibility note: " + "; ".join(
                                    validation["warnings"]
                                )

                # Telemetry: count slice with profile detail
                try:
                    from kiln.daily_stats import record_event
                    _profile_name = effective_printer_id or os.path.basename(effective_profile or "unknown")
                    record_event("slices", detail=_profile_name)
                except Exception:
                    pass

                return response
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(f"Failed to slice model: {exc}", code="SLICER_ERROR")
            except FileNotFoundError as exc:
                return _srv._error_dict(f"Failed to slice model: {exc}", code="FILE_NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in slice_model")
                return _srv._error_dict(f"Unexpected error in slice_model: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # reslice_with_overrides
        # ------------------------------------------------------------------

        _SLICER_INPUT_EXTENSIONS = {".stl", ".3mf", ".step", ".stp", ".obj", ".amf"}

        @mcp.tool()
        def reslice_with_overrides(
            input_path: str,
            printer_id: str | None = None,
            overrides: str | None = None,
            output_dir: str | None = None,
            slicer_path: str | None = None,
        ) -> dict[str, Any]:
            """Reslice a 3D model with custom slicer parameter overrides.

            Accepts a base printer profile and a JSON dict of overrides to customize
            the slice. Common override keys (PrusaSlicer INI format):

              Adhesion: brim_width (mm), skirts (count), skirt_distance (mm)
              Temperature: temperature, first_layer_temperature, bed_temperature
              Speed: perimeter_speed, infill_speed, external_perimeter_speed, first_layer_speed, travel_speed (mm/s)
              Structure: fill_density (e.g. "25%"), fill_pattern (gyroid/grid/honeycomb), layer_height
              Support: support_material (0/1), support_material_buildplate_only (0/1)
              Retraction: retract_length, retract_speed

            Example overrides JSON: {"brim_width": "8", "perimeter_speed": "30", "fill_density": "25%"}

            Use this tool when a print failed due to adhesion, wobble, or quality issues
            and you need to reslice with adjusted settings. Pair with rotate_model to
            also change part orientation before reslicing.

            Requires PrusaSlicer or OrcaSlicer installed locally.
            Use kiln find-slicer or the find_slicer MCP tool to verify.

            Args:
                input_path: Path to the input file (STL, 3MF, STEP, OBJ, AMF).
                printer_id: Printer model ID for bundled profile selection
                    (e.g. ``"prusa_mini"``, ``"bambu_a1"``).
                overrides: JSON string of key-value pairs to override in the slicer
                    profile (e.g. ``'{"brim_width": "8", "fill_density": "25%"}'``).
                output_dir: Directory for the output G-code.  Defaults to the
                    system temp directory.
                slicer_path: Explicit path to the slicer binary.  Auto-detected
                    if omitted.
            """
            if err := _srv._check_auth("slicer"):
                return err

            import json as _json

            from kiln.slicer_profiles import resolve_slicer_profile, validate_profile_for_printer

            # -- Validate input file --
            input_abs = os.path.abspath(input_path)
            if not os.path.isfile(input_abs):
                return _srv._error_dict(
                    f"Input file not found: {os.path.basename(input_abs)}",
                    code="FILE_NOT_FOUND",
                )

            ext = Path(input_abs).suffix.lower()
            if ext not in _SLICER_INPUT_EXTENSIONS:
                return _srv._error_dict(
                    f"Unsupported input format '{ext}'. Supported: {', '.join(sorted(_SLICER_INPUT_EXTENSIONS))}",
                    code="UNSUPPORTED_FORMAT",
                )

            # -- Parse overrides (accept both JSON string and dict) --
            parsed_overrides: dict[str, str] = {}
            if overrides is not None:
                if isinstance(overrides, dict):
                    parsed_overrides = {str(k): str(v) for k, v in overrides.items()}
                else:
                    try:
                        parsed_overrides = _json.loads(overrides)
                    except (_json.JSONDecodeError, TypeError) as exc:
                        return _srv._error_dict(
                            f"Invalid overrides JSON: {exc}",
                            code="VALIDATION_ERROR",
                        )
                    if not isinstance(parsed_overrides, dict):
                        return _srv._error_dict(
                            f"Overrides must be a JSON object (dict), got {type(parsed_overrides).__name__}.",
                            code="VALIDATION_ERROR",
                        )

            try:
                from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

                # -- Resolve profile with overrides --
                effective_printer_id = _srv._map_printer_hint_to_profile_id(
                    printer_id
                ) or _srv._map_printer_hint_to_profile_id(_srv._PRINTER_MODEL)

                effective_profile: str | None = None
                if effective_printer_id:
                    try:
                        effective_profile = resolve_slicer_profile(
                            effective_printer_id,
                            overrides=parsed_overrides or None,
                        )
                    except Exception as exc:
                        _logger.debug(
                            "Profile resolution failed for %s: %s",
                            effective_printer_id,
                            exc,
                        )

                # -- Safety-validate temperature overrides --
                validation_result: dict[str, Any] | None = None
                _temp_keys = {
                    "temperature",
                    "first_layer_temperature",
                    "bed_temperature",
                    "first_layer_bed_temperature",
                }
                has_temp_overrides = bool(parsed_overrides and _temp_keys & parsed_overrides.keys())

                if has_temp_overrides and effective_printer_id and _srv._PRINTER_MODEL:
                    validation_result = validate_profile_for_printer(effective_printer_id, _srv._PRINTER_MODEL)

                # -- Slice --
                result = slice_file(
                    input_abs,
                    output_dir=output_dir,
                    profile=effective_profile,
                    slicer_path=slicer_path,
                )

                response: dict[str, Any] = {
                    "success": True,
                    **result.to_dict(),
                }
                if effective_printer_id:
                    response["printer_id"] = effective_printer_id
                if effective_profile:
                    response["profile_path"] = effective_profile
                if parsed_overrides:
                    response["applied_overrides"] = parsed_overrides

                # Attach validation warnings/errors when present
                if validation_result and (validation_result["warnings"] or validation_result["errors"]):
                    response["profile_validation"] = validation_result
                    if validation_result["errors"]:
                        response["profile_validation_warning"] = (
                            f"Temperature overrides may be unsafe for {_srv._PRINTER_MODEL}: "
                            + "; ".join(validation_result["errors"])
                        )
                    elif validation_result["warnings"]:
                        response["profile_validation_warning"] = "Profile compatibility note: " + "; ".join(
                            validation_result["warnings"]
                        )

                return response
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to reslice model: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(
                    f"Failed to reslice model: {exc}",
                    code="SLICER_ERROR",
                )
            except FileNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to reslice model: {exc}",
                    code="FILE_NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in reslice_with_overrides")
                return _srv._error_dict(
                    f"Unexpected error in reslice_with_overrides: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # find_slicer
        # ------------------------------------------------------------------

        @mcp.tool(name="find_slicer")
        def find_slicer_tool() -> dict:
            """Check if a slicer (PrusaSlicer/OrcaSlicer) is available on the system.

            Returns the slicer path, name, and version if found.
            """
            try:
                from kiln.slicer import SlicerNotFoundError
                from kiln.slicer import find_slicer as _find_slicer

                info = _find_slicer()
                return {
                    "success": True,
                    **info.to_dict(),
                }
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to find slicer: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in find_slicer_tool")
                return _srv._error_dict(f"Unexpected error in find_slicer_tool: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # slice_and_print
        # ------------------------------------------------------------------

        @mcp.tool()
        def slice_and_print(
            input_path: str,
            printer_name: str | None = None,
            profile: str | None = None,
            printer_id: str | None = None,
            material: str | None = None,
        ) -> dict:
            """Slice a 3D model (STL/3MF) + upload + print in one step (basic pipeline).

            For a more comprehensive pipeline with validation and profile auto-detection,
            use ``run_quick_print``. For custom slicer overrides, use ``run_reslice_and_print``.
            Automatically analyzes bed adhesion and adds brim/raft when needed
            based on model geometry, material warp tendency, and printer type.
            This adhesion intelligence only activates when no custom profile is
            supplied.

            Args:
                input_path: Path to the 3D model file (STL, 3MF, STEP, etc.).
                printer_name: Target printer name.  Omit for the default printer.
                profile: Path to a slicer profile/config file.
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"prusa_mini"``).
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``).  Affects
                    automatic brim/raft decisions.

            Combines ``slice_model``, ``upload_file``, and ``start_print`` into
            a single action.
            """
            if err := _srv._check_auth("print"):
                return err
            try:
                from kiln.printers import PrinterError
                from kiln.registry import PrinterNotFoundError
                from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file
                from kiln.slicer_profiles import resolve_slicer_profile

                effective_printer_id, effective_profile = _srv._resolve_slice_profile_context(
                    profile=profile,
                    printer_id=printer_id,
                )

                # --- Auto-material from AMS if not specified ---
                if material is None:
                    try:
                        if printer_name:
                            _adapter = _srv._get_registry().get(printer_name)
                        else:
                            _adapter = _srv._get_adapter()
                        if hasattr(_adapter, "get_ams_status"):
                            ams = _adapter.get_ams_status()
                            tray_now = ams.get("tray_now", "255")
                            if tray_now != "255":
                                slot_idx = int(tray_now)
                                for unit in ams.get("units", []):
                                    for tray in unit.get("trays", []):
                                        if tray.get("slot") == slot_idx and tray.get("tray_type"):
                                            material = tray["tray_type"]
                                            _logger.debug("Auto-detected material from AMS: %s", material)
                                            break
                                    if material:
                                        break
                    except Exception:
                        _logger.debug("AMS material auto-detection failed", exc_info=True)

                # --- Auto-adhesion: analyse model and inject brim/raft if needed ---
                adhesion_rec = None
                adhesion_overrides: dict[str, str] = {}
                if profile is None and input_path.lower().endswith((".stl", ".obj", ".3mf")):
                    try:
                        from kiln.printability import (
                            analyze_printability as _analyze_printability,
                        )
                        from kiln.printability import (
                            is_bedslinger,
                            recommend_adhesion,
                        )

                        report = _analyze_printability(input_path)
                        if report.bed_adhesion:
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
                                material=material or "PLA",
                                has_enclosure=has_enclosure,
                                is_bedslinger_printer=is_bs,
                                model_height_mm=report.model_height_mm,
                            )
                            if rec.brim_width_mm > 0 or rec.use_raft:
                                adhesion_rec = rec.to_dict()
                                adhesion_overrides = dict(rec.slicer_overrides)
                                _logger.info(
                                    "Auto-adhesion: brim=%dmm raft=%s (%s)",
                                    rec.brim_width_mm,
                                    rec.use_raft,
                                    rec.rationale,
                                )
                    except Exception:
                        _logger.debug("Auto-adhesion analysis failed, proceeding without", exc_info=True)

                # Bambu printers: wrap_gcode_as_3mf expects M83 (relative extrusion)
                # and provides its own start/end gcode, so override PrusaSlicer defaults.
                if _srv._PRINTER_TYPE == "bambu":
                    adhesion_overrides["use_relative_e_distances"] = "1"
                    adhesion_overrides["start_gcode"] = ""
                    adhesion_overrides["end_gcode"] = ""

                # Prefer per-model speeds when printer_id is available
                if effective_printer_id:
                    try:
                        from kiln.printer_intelligence import get_slicer_speed_overrides

                        model_speeds = get_slicer_speed_overrides(effective_printer_id)
                        if model_speeds:
                            for k, v in model_speeds.items():
                                if k not in adhesion_overrides:
                                    adhesion_overrides[k] = v
                    except (ImportError, Exception):
                        pass  # fall through to per-type defaults below

                # Inject printer-aware speed overrides
                if _srv._PRINTER_TYPE in _srv._PRINTER_SPEED_OVERRIDES:
                    for k, v in _srv._PRINTER_SPEED_OVERRIDES[_srv._PRINTER_TYPE].items():
                        if k not in adhesion_overrides:  # don't override explicit user settings
                            adhesion_overrides[k] = v

                # Re-resolve profile with adhesion overrides merged in
                if adhesion_overrides and effective_printer_id:
                    try:
                        effective_profile = resolve_slicer_profile(effective_printer_id, overrides=adhesion_overrides)
                    except Exception:
                        _logger.debug("Profile override injection failed", exc_info=True)

                result = slice_file(
                    input_path,
                    profile=effective_profile,
                )

                if printer_name:
                    adapter = _srv._get_registry().get(printer_name)
                else:
                    adapter = _srv._get_adapter()

                # Bambu printers need PrusaSlicer output wrapped in a 3MF with
                # the proprietary BambuStudio start/end gcode.  The adapter
                # exposes wrap_gcode_as_3mf() for this.
                upload_path = result.output_path
                if hasattr(adapter, "wrap_gcode_as_3mf") and result.output_path.endswith(".gcode"):
                    try:
                        upload_path = adapter.wrap_gcode_as_3mf(result.output_path)
                        _logger.info("Wrapped gcode as Bambu 3MF: %s", upload_path)
                    except Exception:
                        _logger.warning(
                            "Bambu 3MF wrapping failed, uploading raw gcode",
                            exc_info=True,
                        )

                upload = adapter.upload_file(upload_path)
                file_name = upload.file_name or os.path.basename(upload_path)

                # Mandatory pre-flight safety gate before starting print.
                safety_printer = _srv._resolve_effective_printer_name(printer_name)
                if block := _srv._emergency_latch_error("slice_and_print", safety_printer):
                    return block
                pf = _srv.preflight_check()
                if not pf.get("ready", False):
                    _srv._audit(
                        "slice_and_print",
                        "preflight_failed",
                        details={
                            "file": file_name,
                            "summary": pf.get("summary", ""),
                        },
                    )
                    return _srv._error_dict(
                        pf.get("summary", "Pre-flight checks failed"),
                        code="PREFLIGHT_FAILED",
                    )

                # --- AMS auto-routing for Bambu printers ---
                # Silent fallthrough to the external-spool feed path caused
                # production failures (error 0300-8015 "filament on external
                # spool has run out") when users had AMS trays loaded but
                # nothing on the external spool.  Delegate to the shared
                # ``_resolve_use_ams`` helper so this matches the behaviour
                # of the ``start_print`` MCP tool exactly.
                print_kwargs: dict[str, Any] = {}
                ams_routing: dict[str, Any] | None = None
                ams_routing_warnings: list[str] = []
                if _srv._PRINTER_TYPE == "bambu":
                    ams_decision = _srv._resolve_use_ams(
                        "auto", None, adapter, material=material,
                    )
                    ams_routing_warnings = list(ams_decision.get("warnings") or [])
                    if ams_decision.get("use_ams"):
                        print_kwargs["use_ams"] = True
                        mapping = ams_decision.get("ams_mapping")
                        if mapping is not None:
                            print_kwargs["ams_mapping"] = mapping
                        ams_routing = {
                            "routed": "ams",
                            "ams_mapping": mapping,
                            "warnings": ams_routing_warnings,
                        }
                    else:
                        ams_routing = {
                            "routed": "external_spool",
                            "warnings": ams_routing_warnings,
                        }

                # Pass local 3MF path so bambu.py can compute MD5 + detect
                # multi-material plates (supersedes single-tray routing above
                # when the 3MF explicitly declares multiple filaments).
                if upload_path.lower().endswith(".3mf") and os.path.isfile(upload_path):
                    print_kwargs["local_file_path"] = upload_path

                print_result = adapter.start_print(file_name, **print_kwargs)
                _srv._get_heater_watchdog().notify_print_started()

                resp: dict[str, Any] = {
                    "success": True,
                    "slice": result.to_dict(),
                    "upload": upload.to_dict(),
                    "print": print_result.to_dict(),
                    "printer_id": effective_printer_id,
                    "profile_path": effective_profile,
                    "message": f"Sliced, uploaded, and started printing {os.path.basename(input_path)}.",
                }
                if adhesion_rec:
                    resp["adhesion"] = adhesion_rec
                if ams_routing is not None:
                    resp["ams_routing"] = ams_routing
                if ams_routing_warnings:
                    resp["warnings"] = ams_routing_warnings
                return resp
            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice and print: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(f"Failed to slice and print: {exc}", code="SLICER_ERROR")
            except PrinterNotFoundError:
                return _srv._error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
            except (PrinterError, RuntimeError, FileNotFoundError) as exc:
                return _srv._error_dict(
                    f"Failed to slice and print: {exc}. Check the input file and printer connection."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in slice_and_print")
                return _srv._error_dict(f"Unexpected error in slice_and_print: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # list_slicer_profiles
        # ------------------------------------------------------------------

        @mcp.tool(name="list_slicer_profiles")
        def list_slicer_profiles_tool() -> dict:
            """List all bundled slicer profiles for supported printers.

            Returns profile IDs, display names, recommended slicer, and the
            minimum license tier required for each.  Free-tier profiles can be
            used by everyone; PRO profiles require a Kiln Pro license.

            Use with ``get_slicer_profile`` to see full settings, or
            ``slice_model`` with printer_id for auto-profile selection.
            """
            if err := _srv._check_auth("slicer"):
                return err
            try:
                from kiln.slicer_profiles import get_slicer_profile, list_slicer_profiles

                ids = list_slicer_profiles()
                profiles = []
                for pid in ids:
                    try:
                        p = get_slicer_profile(pid)
                        profiles.append(
                            {
                                "id": p.id,
                                "display_name": p.display_name,
                                "slicer": p.slicer,
                                "tier": p.tier,
                            }
                        )
                    except KeyError:
                        continue
                return {"success": True, "count": len(profiles), "profiles": profiles}
            except Exception as exc:
                _logger.exception("Unexpected error in list_slicer_profiles_tool")
                return _srv._error_dict(f"Unexpected error in list_slicer_profiles_tool: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # get_slicer_profile
        # ------------------------------------------------------------------

        @mcp.tool(name="get_slicer_profile")
        def get_slicer_profile_tool(printer_id: str) -> dict:
            """Get the full bundled slicer profile for a printer model.

            Returns all INI settings (layer height, speeds, temps, retraction, etc.)
            and the recommended slicer.  Free-tier profiles (default, ender3,
            prusa_mk3s, klipper_generic) are available to all users.  Premium
            profiles require a Kiln Pro license.

            Args:
                printer_id: Printer model identifier (e.g. ``"ender3"``,
                    ``"bambu_x1c"``).
            """
            if err := _srv._check_auth("slicer"):
                return err
            try:
                from kiln.slicer_profiles import get_slicer_profile, slicer_profile_to_dict

                profile = get_slicer_profile(printer_id)

                # Gate premium profiles behind PRO license
                if profile.tier == "pro":
                    ok, message = _srv.check_tier(_srv.LicenseTier.PRO)
                    if not ok:
                        return {
                            "success": False,
                            "error": (
                                f"The '{profile.display_name}' slicer profile requires a Kiln Pro license. "
                                f"Free-tier profiles available: default, ender3, prusa_mk3s, klipper_generic. "
                                f"Upgrade at https://kiln3d.com/pro or run 'kiln upgrade'."
                            ),
                            "code": "LICENSE_REQUIRED",
                            "required_tier": "pro",
                            "upgrade_url": "https://kiln3d.com/pro",
                        }

                return {"success": True, "profile": slicer_profile_to_dict(profile)}
            except KeyError:
                return _srv._error_dict(
                    f"No slicer profile for '{printer_id}' and no default available.",
                    code="NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in get_slicer_profile_tool")
                return _srv._error_dict(f"Unexpected error in get_slicer_profile_tool: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered slicer tools")


plugin = _SlicerToolsPlugin()
