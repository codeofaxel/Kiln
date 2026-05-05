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


def _apply_bed_fit_gate(
    input_path: str,
    effective_printer_id: str | None,
    auto_center: bool,
) -> tuple[str, dict | None, dict]:
    """Pre-slice safety gate: verify the mesh fits within the printer's
    build volume and hasn't been placed off-bed (origin-centered
    geometry crashed a Bambu A1 nozzle into the purge tool on 2026-04-15
    — incident #0).

    When ``auto_center=True`` (default) and the mesh is off-bed but
    physically fits, we translate it to a bed-centered copy in a temp
    directory and return that path.  The original file is not modified.

    When the mesh exceeds the build volume, we return an error dict
    even with ``auto_center=True`` — translation can't fix that.

    Returns ``(effective_input_path, error_dict_or_None, bed_fit_info)``.
    The caller uses ``effective_input_path`` for slicing.  If
    ``error_dict_or_None`` is not None, the caller should return it
    immediately instead of slicing.
    """
    from kiln.printers.bed_fit import (
        apply_translation_to_stl,
        validate_mesh_for_printer,
    )

    if not effective_printer_id:
        # No printer context — skip the gate.  The caller probably knows
        # what they're doing (e.g. generic slice without a target printer).
        return input_path, None, {"gate": "skipped_no_printer"}

    if not input_path.lower().endswith(".stl"):
        # Only validate + translate STLs for now.  3MF/STEP/OBJ are out
        # of scope for the translate path — we'd need format-specific
        # rewriters.  Still run a bbox validation but can't auto-fix.
        fit = validate_mesh_for_printer(input_path, effective_printer_id)
        if not fit["ok"] and fit["error_code"] in ("OFF_BED_GEOMETRY", "EXCEEDS_BED"):
            return input_path, fit, fit
        return input_path, None, fit

    fit = validate_mesh_for_printer(input_path, effective_printer_id)
    if fit["ok"]:
        return input_path, None, fit
    if fit["error_code"] == "EXCEEDS_BED":
        return input_path, fit, fit
    if fit["error_code"] == "OFF_BED_GEOMETRY":
        if auto_center and fit.get("suggested_translate"):
            # Auto-center: translate STL into a temp copy and use it.
            import tempfile
            stem = os.path.splitext(os.path.basename(input_path))[0]
            tmp_dir = tempfile.mkdtemp(prefix="kiln_bedfit_")
            centered_path = os.path.join(tmp_dir, f"{stem}_bedcentered.stl")
            try:
                apply_translation_to_stl(
                    input_path, fit["suggested_translate"], centered_path,
                )
                _logger.info(
                    "Auto-centered off-bed mesh for %s: translate %s -> %s",
                    effective_printer_id, fit["suggested_translate"],
                    centered_path,
                )
                fit["auto_centered"] = True
                fit["centered_input_path"] = centered_path
                return centered_path, None, fit
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Auto-center failed: %s", exc)
                fit["error_message"] = (
                    f"{fit['error_message']} "
                    f"(auto-center also failed: {exc})"
                )
                return input_path, fit, fit
        # auto_center disabled or translation path unavailable — block.
        return input_path, fit, fit
    # Unknown warn-only states (BBOX_UNKNOWN, VOLUME_UNKNOWN) — pass through.
    return input_path, None, fit


def _auto_wrap_bambu_3mf(
    gcode_path: str,
    effective_printer_id: str | None,
    stl_path: str | None,
) -> tuple[str | None, str | None]:
    """If the effective printer is a Bambu Lab, repackage the sliced
    G-code into a 3MF so it can actually start (Bambu firmware ignores
    raw ``.gcode`` via the ``gcode_file`` MQTT command; only
    ``project_file`` works, and that requires ``.3mf``).

    ``stl_path`` is routed by extension: ``.stl`` goes to ``stl_paths``
    so OpenSCAD generates a thumbnail for the LCD preview, while ``.3mf``
    goes to ``source_3mf_path`` to copy its embedded thumbnails.  Other
    extensions (``.step``, ``.obj``) are ignored — the wrap still
    succeeds but the touchscreen shows a blank preview.

    Returns ``(threemf_path, warning)``.  When no wrap happens, both
    are ``None``.  Failure is non-fatal — the original gcode_path is
    still usable for non-Bambu printers or manual wrapping.
    """
    if not effective_printer_id or not effective_printer_id.startswith("bambu"):
        return (None, None)
    try:
        # CRITICAL: use build_bambu_3mf (adds BambuStudio start-gcode —
        # G28 homing, M620 AMS load, purge line, bed leveling) rather
        # than repackage_gcode_as_bambu_3mf (only zips gcode that ALREADY
        # has Bambu init).  PrusaSlicer-native output never has Bambu
        # init, so repackage_* produced 3MFs that caused nozzle crashes
        # because the printer tried to execute G1 moves without ever
        # homing — incident #0 (2026-04-15).  Route through the adapter's
        # wrap_gcode_as_3mf method which wires to build_bambu_3mf with
        # the correct start-gcode for the printer model.
        from pathlib import Path as _Path

        from kiln.printers.bambu_3mf import (
            BambuPrintSettings,
            build_bambu_3mf,
        )

        threemf_path = gcode_path.rsplit(".", 1)[0] + ".gcode.3mf"
        stl_paths: list[str] | None = None
        source_3mf: str | None = None
        if stl_path and os.path.isfile(stl_path):
            ext = os.path.splitext(stl_path)[1].lower()
            if ext == ".stl":
                stl_paths = [stl_path]
            elif ext == ".3mf":
                source_3mf = stl_path

        gcode_body = _Path(gcode_path).read_text(encoding="utf-8")
        settings = BambuPrintSettings(
            model_name=_Path(gcode_path).stem,
            # Temps default to PLA; the PrusaSlicer gcode body already
            # contains M104/M190 with the correct values from the
            # profile, so these are only used for metadata fields.
        )
        build_bambu_3mf(
            gcode_body,
            threemf_path,
            settings=settings,
            source_3mf_path=source_3mf,
            stl_paths=stl_paths,
        )
        _logger.info(
            "Auto-wrapped %s as Bambu 3MF (with Bambu init) at %s",
            os.path.basename(gcode_path), threemf_path,
        )
        return (threemf_path, None)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Bambu auto-wrap failed: %s — leaving as raw gcode", exc)
        return (None, f"Bambu auto-wrap failed: {exc}")


def _maybe_auto_assembly_manual(metadata: dict) -> dict | None:
    """Optional plugin hook: route ``slice_and_print`` metadata through
    kiln-pro's assembly-manual generator if it's installed.

    Returns ``None`` when kiln-pro isn't installed — public Kiln keeps
    no mandatory dependency on it.  When installed it returns a
    JSON-friendly dict the caller can pass through to the user
    verbatim (cached PDF path, pending status, or an upsell hint).
    All errors are caught — never raises out to the print pipeline.

    Multi-language manuals and co-brand wordmarks are kiln-pro
    Business+ features (https://kiln3d.com/pricing); the metadata
    keys for them are accepted at every tier and ignored where the
    tier doesn't allow.
    """
    try:
        from kiln_pro.manuals.auto_trigger import (
            maybe_generate_for_print_job,
        )
    except ImportError:
        return None

    try:
        result = maybe_generate_for_print_job(
            metadata["assembly_json"],
            output_dir=metadata.get("manual_output_dir"),
            design_name=metadata.get("manual_design_name"),
            branding=metadata.get("manual_branding"),
            co_brand_name=metadata.get("manual_co_brand_name"),
            languages=metadata.get("manual_languages"),
            cover_language=metadata.get("manual_cover_language"),
        )
    except Exception as exc:  # noqa: BLE001 — never block slice_and_print
        _logger.info("auto_assembly_manual integration failed: %s", exc)
        return None

    return {
        "skipped": result.skipped,
        "reason": result.reason,
        "parts_count": result.parts_count,
        "fingerprint": result.fingerprint,
        "cached_path": result.cached_path,
        "expected_path": result.expected_path,
        "pending": result.pending,
        "upsell_text": result.upsell_text,
        "first_time_notice": result.first_time_notice,
    }


def _maybe_overlay_calibration(
    parsed_overrides: dict[str, str],
    printer_id: str,
    *,
    material: str | None = None,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Inject a Pro+ user's calibrated slicer values into ``parsed_overrides``.

    Lazy-imports ``kiln_pro.engineering.calibration_coach`` so this is
    a no-op for free-tier installs that don't have kiln-pro present.
    When kiln-pro is installed AND the user has a HIGH/MEDIUM-tier
    calibrated slicer profile for ``printer_id`` (with material
    matched when supplied; fallback to most-recent across all
    materials when ``material`` is ``None``), the helper:

    - Fills in keys the caller did NOT already set in ``parsed_overrides``
      with the user's calibrated values (extrusion_multiplier,
      filament_max_volumetric_speed, pressure_advance, xy_size_compensation,
      filament_retraction_length).  User-supplied overrides ALWAYS win;
      calibration only fills gaps.  Idempotent.
    - Returns the standard ``calibration_used`` block (same shape as
      every other wire-up site) so the slicer tool's response can
      surface what was applied.

    Returns ``(modified_overrides, calibration_used_block_or_None)``.
    The calibration block is ``None`` when kiln-pro isn't installed
    OR ``calibration_for`` raised; never raises out of this helper.
    """
    try:
        from kiln_pro.engineering.calibration_coach import (
            apply_calibration_to_slicer_args,
            calibration_for,
            calibration_used_block,
        )
    except ImportError:
        return parsed_overrides, None

    try:
        merged = apply_calibration_to_slicer_args(
            parsed_overrides, printer_id, material,
        )
        verdict = calibration_for(printer_id, material)
        cal_used = calibration_used_block(verdict, printer_id=printer_id)
    except Exception as exc:  # noqa: BLE001 — never block slicing
        _logger.debug(
            "calibration overlay skipped for printer %r: %s",
            printer_id, exc,
        )
        return parsed_overrides, None

    return merged, cal_used


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
            auto_center: bool = True,
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
                auto_center: When True (default), off-bed STLs are translated
                    to a bed-centered copy before slicing.  This prevents the
                    class of crash where origin-centered meshes (common from
                    compose_part_from_primitives / OpenSCAD output) produce
                    sliced gcode with negative X/Y moves that drive the
                    nozzle into the printer frame.  Set False only if you've
                    verified the input is already correctly positioned.

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

                # Bed-fit safety gate (Layer 1).  Blocks off-bed / oversized
                # geometry before it hits the slicer.  May auto-translate
                # an origin-centered STL into a bed-centered temp copy.
                effective_input, gate_err, gate_info = _apply_bed_fit_gate(
                    input_path, effective_printer_id, auto_center,
                )
                if gate_err is not None:
                    return _srv._error_dict(
                        gate_err.get("error_message", "Bed-fit check failed."),
                        code=gate_err.get("error_code", "BED_FIT_ERROR"),
                    )
                result = slice_file(
                    effective_input,
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

                # Bambu auto-wrap: Bambu firmware ignores gcode_file MQTT
                # commands and only starts via project_file on .3mf.  Wrap
                # here so callers don't have to know the Bambu-specific
                # convention.  Failure is non-fatal — raw gcode still usable.
                # Use the (possibly centered) STL for thumbnail generation
                # so the LCD preview matches the sliced geometry.
                _gcode_path = result.to_dict().get("output_path")
                if _gcode_path:
                    threemf_path, warning = _auto_wrap_bambu_3mf(
                        _gcode_path, effective_printer_id, effective_input,
                    )
                    if threemf_path:
                        response["output_3mf_path"] = threemf_path
                        response["output_path"] = threemf_path
                        response["raw_gcode_path"] = _gcode_path
                        # POST-WRAP VERIFICATION: ensure the final 3MF has
                        # both a valid bbox AND a homing sequence before
                        # handing it to the caller.  Incident #0 showed
                        # that a dormant bug in the wrap function could
                        # produce a 3MF without G28 — the safety check
                        # catches that regression class.
                        try:
                            from kiln.printers.bed_fit import (
                                verify_3mf_is_safe_to_print,
                            )
                            safety = verify_3mf_is_safe_to_print(
                                threemf_path, effective_printer_id,
                            )
                            response["safety_verification"] = safety
                            if not safety["ok"]:
                                return _srv._error_dict(
                                    f"Produced 3MF failed safety verification: "
                                    f"{safety.get('error_message', 'unknown issue')}. "
                                    f"Failed checks: {', '.join(safety['failed'])}. "
                                    f"The slicer or wrapper produced unsafe output. "
                                    f"Do NOT upload this file to the printer.",
                                    code=safety.get("error_code", "UNSAFE_3MF"),
                                )
                        except Exception as _exc:
                            _logger.warning(
                                "Post-wrap safety verification skipped: %s",
                                _exc,
                            )
                    if warning:
                        response.setdefault("warnings", []).append(warning)

                # Surface the bed-fit result so callers can see if we
                # auto-centered + the translation applied.
                if gate_info.get("gate") != "skipped_no_printer":
                    response["bed_fit"] = gate_info

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
            auto_center: bool = True,
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

                # -- Calibration overlay: when kiln-pro is installed and the
                # user has a calibrated slicer profile for (printer, material),
                # inject those values into parsed_overrides BEFORE resolve so
                # the slicer's print-time / cost estimates use values the
                # user has personally verified.  No-op for free users.
                # User-supplied overrides ALWAYS win — the helper only fills
                # gaps.
                cal_used: dict[str, Any] | None = None
                if effective_printer_id:
                    parsed_overrides, cal_used = _maybe_overlay_calibration(
                        parsed_overrides, effective_printer_id,
                    )

                # Pro+ slice-history recording (best-effort, never blocks slicing).
                # When kiln-pro is installed AND the input has a recipe
                # sidecar, this records the per-machine offsets that
                # were applied at this slice into a per-design append-only
                # artifact, enabling design-scoped calibration explanation.
                # Free users (no kiln-pro): try/except fires ImportError, no-op.
                if cal_used is not None:
                    try:
                        from kiln_pro.bridge import pro_features
                        pro_features.record_slice_for_input(
                            input_path=input_path,
                            printer_id=effective_printer_id or "",
                            material=cal_used.get("material") or "",
                        )
                    except Exception:
                        pass  # never block slicing on telemetry

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

                # -- Bed-fit safety gate (Layer 1) --
                effective_input, gate_err, gate_info = _apply_bed_fit_gate(
                    input_abs, effective_printer_id, auto_center,
                )
                if gate_err is not None:
                    return _srv._error_dict(
                        gate_err.get("error_message", "Bed-fit check failed."),
                        code=gate_err.get("error_code", "BED_FIT_ERROR"),
                    )

                # -- Slice --
                result = slice_file(
                    effective_input,
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
                if gate_info.get("gate") != "skipped_no_printer":
                    response["bed_fit"] = gate_info

                # Bambu auto-wrap (same logic as slice_model) so callers
                # don't have to know raw gcode won't start on Bambu.
                _gcode_path = result.to_dict().get("output_path")
                _wrap_path: str | None = None
                if _gcode_path:
                    threemf_path, warning = _auto_wrap_bambu_3mf(
                        _gcode_path, effective_printer_id, effective_input,
                    )
                    _wrap_path = threemf_path
                    if threemf_path:
                        response["output_3mf_path"] = threemf_path
                        response["output_path"] = threemf_path
                        response["raw_gcode_path"] = _gcode_path
                        # Post-wrap safety verification — parity with slice_model
                        try:
                            from kiln.printers.bed_fit import (
                                verify_3mf_is_safe_to_print,
                            )
                            safety = verify_3mf_is_safe_to_print(
                                threemf_path, effective_printer_id,
                            )
                            response["safety_verification"] = safety
                            if not safety["ok"]:
                                return _srv._error_dict(
                                    f"Produced 3MF failed safety verification: "
                                    f"{safety.get('error_message', 'unknown issue')}. "
                                    f"Failed checks: {', '.join(safety['failed'])}. "
                                    f"Do NOT upload this file.",
                                    code=safety.get("error_code", "UNSAFE_3MF"),
                                )
                        except Exception as _exc:
                            _logger.warning(
                                "Post-wrap safety verification skipped: %s", _exc,
                            )
                    if warning:
                        response.setdefault("warnings", []).append(warning)

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
            auto_center: bool = True,
            metadata: dict | None = None,
            skip_validation: bool = False,
        ) -> dict:
            """Slice a 3D model (STL/3MF) + upload + print in one step (basic pipeline).

            For a more comprehensive pipeline with validation and profile auto-detection,
            use ``run_quick_print``. For custom slicer overrides, use ``run_reslice_and_print``.
            Automatically analyzes bed adhesion and adds brim/raft when needed
            based on model geometry, material warp tendency, and printer type.
            This adhesion intelligence only activates when no custom profile is
            supplied.

            Pre-print validation gate: mesh inputs (.stl/.obj/.3mf/.step/.glb)
            run through Kiln's full validation pipeline before slicing —
            format check, watertight check, auto-repair, printability scoring
            (0-100), bed-fit, and material checks.  Designs that fail the gate
            are blocked before reaching the printer; auto-repaired meshes are
            sliced from the repaired path.  Pass ``skip_validation=True`` to
            bypass (e.g. for already-validated meshes or pre-sliced 3MFs).

            Args:
                input_path: Path to the 3D model file (STL, 3MF, STEP, etc.).
                printer_name: Target printer name.  Omit for the default printer.
                profile: Path to a slicer profile/config file.
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"prusa_mini"``).
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``).  Affects
                    automatic brim/raft decisions.
                metadata: Optional dict of pass-through fields.  When
                    kiln-pro (https://kiln3d.com) is installed it
                    consumes keys here to generate a printable
                    assembly manual alongside the print, surfacing it
                    under ``response["assembly_manual"]``.  Without
                    kiln-pro the metadata is silently ignored.
                    Recognised keys (all optional):
                    ``assembly_json``, ``manual_output_dir``,
                    ``manual_design_name``, ``manual_branding``,
                    ``manual_co_brand_name``, ``manual_languages``,
                    ``manual_cover_language``.  Multi-language and
                    co-brand are kiln-pro Business+ features
                    (https://kiln3d.com/pricing).
                skip_validation: Bypass the pre-print validation gate.
                    Defaults to False — designs are pre-tested for
                    printability before they reach the printer.  Set to
                    True only when the caller has already validated the
                    mesh (e.g. ``validate_and_prepare`` was just called)
                    or when the input is a pre-sliced 3MF the validator
                    can't introspect.

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

                # --- Pre-print validation gate ---
                # Mesh inputs are pre-tested for printability (manifold,
                # walls, overhangs, bridges, bed-fit, material) before they
                # reach the printer.  Auto-repair on non-manifold; blocks
                # designs that fail with a clear next_action.  Bypass with
                # skip_validation=True (e.g. pre-sliced 3MFs).
                validation_summary: dict | None = None
                if not skip_validation:
                    try:
                        from kiln.plugins._validation_pipeline_internals import (
                            _SUPPORTED_FORMATS,
                        )
                        from kiln.plugins.validation_pipeline_tools import (
                            run_full_validation_pipeline,
                        )

                        _ext = os.path.splitext(input_path)[1].lower()
                        if _ext in _SUPPORTED_FORMATS:
                            val_report = run_full_validation_pipeline(
                                input_path,
                                printer_id=effective_printer_id or "",
                                material=material or "",
                            )
                            if not val_report.get("ready_to_print", True):
                                score = val_report.get("printability_score", 0)
                                summary = val_report.get("summary", "Validation failed")
                                err_resp = _srv._error_dict(
                                    f"Mesh failed pre-print validation "
                                    f"(score {score}/100): {summary} "
                                    f"Pass skip_validation=True to bypass.",
                                    code="VALIDATION_FAILED",
                                )
                                err_resp["validation"] = val_report
                                return err_resp

                            # Slice the (possibly repaired/scaled) validated mesh.
                            validated_path = val_report.get("validated_path") or input_path
                            if validated_path and validated_path != input_path:
                                _logger.info(
                                    "slice_and_print: using validated path %s (repaired=%s)",
                                    validated_path,
                                    val_report.get("repaired", False),
                                )
                                input_path = validated_path

                            validation_summary = {
                                "printability_score": val_report.get("printability_score"),
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
                        # An infrastructure-side bug in validation must not
                        # block users from printing.  Log and proceed.
                        _logger.warning(
                            "Validation pipeline raised — proceeding without gate",
                            exc_info=True,
                        )

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

                # --- Bed-fit safety gate (Layer 1) ---
                # Blocks off-bed / oversized geometry before slicing.
                # May auto-translate an origin-centered STL to bed-centered.
                effective_input, gate_err, gate_info = _apply_bed_fit_gate(
                    input_path, effective_printer_id, auto_center,
                )
                if gate_err is not None:
                    return _srv._error_dict(
                        gate_err.get("error_message", "Bed-fit check failed."),
                        code=gate_err.get("error_code", "BED_FIT_ERROR"),
                    )

                result = slice_file(
                    effective_input,
                    profile=effective_profile,
                )

                adapter = _srv._resolve_adapter(printer_name)

                # Bambu printers need PrusaSlicer output wrapped in a 3MF with
                # the proprietary BambuStudio start/end gcode.  The adapter
                # exposes wrap_gcode_as_3mf() for this.  Pass the (possibly
                # bed-centered) STL so the LCD thumbnail matches the sliced
                # geometry.
                upload_path = result.output_path
                if hasattr(adapter, "wrap_gcode_as_3mf") and result.output_path.endswith(".gcode"):
                    try:
                        _stl_paths = (
                            [effective_input] if effective_input.lower().endswith(".stl") else None
                        )
                        upload_path = adapter.wrap_gcode_as_3mf(
                            result.output_path, stl_paths=_stl_paths,
                        )
                        _logger.info("Wrapped gcode as Bambu 3MF: %s", upload_path)
                    except Exception:
                        _logger.warning(
                            "Bambu 3MF wrapping failed, uploading raw gcode",
                            exc_info=True,
                        )

                # Post-wrap safety verification — refuse to upload a 3MF
                # that has no homing sequence or off-bed coordinates.
                # Last gate before bytes reach the printer via FTPS.
                try:
                    from kiln.printers.bed_fit import verify_3mf_is_safe_to_print
                    if upload_path.lower().endswith(".3mf"):
                        safety = verify_3mf_is_safe_to_print(
                            upload_path, effective_printer_id,
                        )
                        if not safety["ok"]:
                            return _srv._error_dict(
                                f"Sliced 3MF failed safety verification before upload: "
                                f"{safety.get('error_message', 'unknown issue')}. "
                                f"Failed checks: {', '.join(safety['failed'])}. "
                                f"This would have been the incident #0 class of crash.",
                                code=safety.get("error_code", "UNSAFE_3MF"),
                            )
                except Exception as _exc:
                    _logger.warning("slice_and_print safety check skipped: %s", _exc)

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
                    # Refuse to silent-route when AMS state is ambiguous
                    # (hardware bits say AMS present but no tray state, or
                    # probe errored out).  Returning an error envelope
                    # here blocks the print BEFORE upload instead of
                    # silently routing to the wrong filament feed path.
                    # Memory rule: "always route to AMS when printer has
                    # one — never silent external-spool fallthrough".
                    if ams_decision.get("ambiguous") and not ams_decision.get("use_ams"):
                        return _srv._error_dict(
                            "AMS routing is ambiguous — hardware reports AMS "
                            "present but no tray state is available.  Refusing "
                            "to silently route to external spool (which would "
                            "fail with Bambu error 0300-8015 if nothing is "
                            "loaded there).  Retry in a few seconds for the "
                            "MQTT cache to refresh, or call start_print() "
                            "directly with use_ams='true' and an explicit "
                            "ams_mapping=[<slot>]. "
                            + " ".join(ams_routing_warnings),
                            code="AMS_STATE_AMBIGUOUS",
                        )
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
                if validation_summary is not None:
                    resp["validation"] = validation_summary
                if adhesion_rec:
                    resp["adhesion"] = adhesion_rec
                if ams_routing is not None:
                    resp["ams_routing"] = ams_routing
                if ams_routing_warnings:
                    resp["warnings"] = ams_routing_warnings

                # kiln-pro hook: when installed, generate an assembly
                # manual alongside the print and add it to the
                # response.  No-op when kiln-pro isn't installed.
                if metadata and metadata.get("assembly_json"):
                    auto_manual = _maybe_auto_assembly_manual(metadata)
                    if auto_manual is not None:
                        resp["assembly_manual"] = auto_manual

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
                    ``"bambu_x1c"``, ``"creality_k1_max"``).
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
                                f"The '{profile.display_name}' slicer profile requires Kiln Pro. "
                                f"Free-tier profiles available: default, ender3, prusa_mk3s, klipper_generic. "
                                f"Already subscribed? Run `kiln login` to sync this machine. "
                                f"Otherwise: https://kiln3d.com/pricing"
                            ),
                            "code": "LICENSE_REQUIRED",
                            "required_tier": "pro",
                            "upgrade_url": "https://kiln3d.com/pricing",
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
