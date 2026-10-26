"""Printability analysis and auto-orientation tools plugin.

Provides MCP tools for analyzing 3D model printability, finding optimal
print orientations, and estimating support requirements.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.tool_annotations import read_only

_logger = logging.getLogger(__name__)


class _PrintabilityToolsPlugin:
    """Printability analysis and auto-orientation tools.

    Tools:
        - analyze_printability
        - auto_orient_model
        - estimate_supports
    """

    @property
    def name(self) -> str:
        return "printability_tools"

    @property
    def description(self) -> str:
        return "Printability analysis and auto-orientation tools"

    def register(self, mcp: Any) -> None:
        """Register printability tools with the MCP server."""

        @mcp.tool(annotations=read_only("Analyze printability"))
        def analyze_printability(
            file_path: str,
            nozzle_diameter: float | None = None,
            layer_height: float = 0.2,
            max_overhang_angle: float | None = None,
            build_volume_x: float | None = None,
            build_volume_y: float | None = None,
            build_volume_z: float | None = None,
            material: str = "pla",
            printer_id: str = "",
        ) -> dict:
            """Analyze a 3D model for FDM printing readiness.

            Performs deep analysis of an STL or OBJ mesh including overhang
            detection, thin wall analysis, bridging assessment, bed adhesion
            surface estimation, and support volume estimation.  Returns one
            ``verdict`` (``ready``, ``prints_with_supports``, ``wont_print``
            or ``check``) with its sentence as ``message``, and the full
            ``report`` with actionable recommendations.  The report keeps a
            0-100 score and a letter grade for comparing versions of a part;
            tell the person the verdict, not the score.

            Pass ``material`` (and ``printer_id`` when a printer is
            registered) so the warping, thermal-stress, and adhesion checks
            run against the actual filament instead of generic PLA defaults.
            On the free tier those checks use conservative safe-floor
            thresholds; with Kiln Pro they are tuned to the specific
            material and printer (kiln3d.com/pricing).

            When the mesh turns out to have holes in it, the result also
            carries a ``fastener_advice`` block saying they were measured
            but not checked against the hardware meant to go in.  Kiln
            raises that once per session across every tool that can raise
            it, so a later analysis may omit the block even though the
            part has holes.  Read its ``agent_instruction`` before
            repeating any of it to the user.

            Args:
                file_path: Path to an STL or OBJ mesh file.
                nozzle_diameter: Printer nozzle diameter in mm.  Leave unset
                    and name the printer: Kiln then checks for the nozzle on
                    record for it, else the printer's own setting, else the
                    model's stock size, and ``report.nozzle`` says which.
                    With no printer named, the only printer Kiln knows of
                    stands in; with none, the check assumes 0.4 and says so.
                layer_height: Print layer height in mm (default 0.2).
                max_overhang_angle: Maximum overhang angle in degrees before
                    supports are needed.  Leave unset for the material's own
                    limit (45 when none is known) — the value every other
                    tool that reports this score uses, so the numbers agree.
                build_volume_x: Optional build volume X dimension in mm.
                build_volume_y: Optional build volume Y dimension in mm.
                build_volume_z: Optional build volume Z dimension in mm.
                material: Material ID for warping / thermal-stress / adhesion
                    analysis (default ``"pla"``).
                printer_id: Optional registered printer whose real geometry
                    and calibration should inform the analysis.
            """
            import kiln.server as _srv
            from kiln.printability import analyze_printability as _analyze

            try:
                build_volume = None
                if build_volume_x is not None and build_volume_y is not None and build_volume_z is not None:
                    build_volume = (build_volume_x, build_volume_y, build_volume_z)

                # No printer named: the only printer Kiln knows of stands
                # in for the nozzle, and the report says so.
                from kiln.assumed_nozzle import assumed_nozzle

                nozzle = assumed_nozzle(printer_id or None, stated=nozzle_diameter, or_only_printer=True)
                report = _analyze(
                    file_path,
                    nozzle_diameter=nozzle.diameter_mm,
                    layer_height=layer_height,
                    max_overhang_angle=max_overhang_angle,
                    build_volume=build_volume,
                    material=material,
                    printer_id=printer_id or None,
                )
                # Which nozzle the check ran for, said beside the verdict.
                if getattr(report, "nozzle", None) is not None:
                    report.nozzle = nozzle.to_dict()
                nozzle_note = nozzle.sentence()
                # The verdict is the answer; the score stays in the report
                # for comparing versions and is not said.  "Needs supports"
                # is a normal print, not a refusal, and a part that cannot
                # print says why.
                # The verdict is a reading of the report, never a reason to
                # lose it (kiln.printability.verdict_of falls back to the
                # report's own printable flag).
                from kiln.printability import verdict_of as _verdict_of

                verdict_key, verdict_text = _verdict_of(report)
                # Where the file put the part, said once when it is under
                # the plate: the check judged it resting on the plate.
                from kiln.printability import below_plate_note

                plate_note = below_plate_note(
                    getattr(getattr(report, "placement", None), "below_plate_mm", None)
                )
                resp: dict[str, Any] = {
                    "success": True,
                    "verdict": verdict_key,
                    "report": report.to_dict(),
                    "message": (
                        verdict_text
                        + (f"  {plate_note}" if plate_note else "")
                        + (f"  {nozzle_note}" if nozzle_note else "")
                    ),
                }

                # The widest of the fastener-advice seams: this one
                # reaches ANY mesh, however it was made.  The trigger is
                # free — hole detection already ran above and the holes
                # are in the report — and it is DETECTION, never a guess
                # about intent: no holes, no advisory.  Said once per
                # session across every seam that can say it (the shared
                # content key in ``kiln.fastener_advice``).  Wrapped so
                # an advisory can never cost the caller their report,
                # and attached BESIDE ``report`` so nothing in the
                # report's own recommendations is displaced.
                try:
                    if report.holes:
                        from kiln.fastener_advice import (
                            advice_for_detected_holes,
                        )

                        free = advice_for_detected_holes()
                        deep = None
                        try:
                            from kiln_pro.bridge import pro_features
                        except ImportError:
                            pass
                        else:
                            if pro_features.is_available(
                                "fastener_hole_advice"
                            ):
                                deep = (
                                    pro_features.fastener_hole_advice
                                    .advise_detected_holes(
                                        report.holes, free_advice=free,
                                    )
                                )
                        if deep:
                            resp["fastener_advice"] = deep
                        elif free:
                            resp["fastener_advice"] = free
                except Exception:  # noqa: BLE001
                    _logger.debug(
                        "fastener advice skipped on printability report",
                        exc_info=True,
                    )

                return resp
            except MemoryError as exc:
                # The analysis needed more memory than the machine that ran
                # it could give (see analyze_printability, "Where it runs").
                return _srv._error_dict(
                    str(exc)
                    or "This model is too large to check for printability here: the "
                    "check needed more memory than this machine could give it.",
                    code="MESH_TOO_LARGE",
                )
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to analyze printability: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in analyze_printability")
                return _srv._error_dict(
                    f"Unexpected error in analyze_printability: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def auto_orient_model(
            file_path: str,
            candidates: int = 24,
            nozzle_diameter: float = 0.4,
            apply: bool = False,
            output_path: str | None = None,
        ) -> dict:
            """Find the optimal print orientation for a 3D model.

            Evaluates multiple rotations of the model, ranks them, and
            reports each candidate's bed contact, support volume, print
            height and overhang share.  Below Kiln Pro the ranking is by bed
            contact, the lower part first between equals; on Kiln Pro, on
            Kiln's hosted service or on an install whose plan includes it,
            the candidates come in the order of Kiln's own ranking
            (https://kiln3d.com/pricing).  Each candidate's ``score`` is its
            bed-contact percentage at every tier, so under Kiln Pro's order a
            later candidate can score higher than the best.  A caller the
            ranking was withheld from is told so once, in ``upgrade_hint``.
            ``message`` says what the best turn changes (support, overhangs,
            bed contact, height) or that the part already sits best.
            Optionally applies the best orientation and writes a reoriented
            STL file.

            Args:
                file_path: Path to an STL or OBJ mesh file.
                candidates: Number of candidate orientations to evaluate
                    (default 24).
                nozzle_diameter: Printer nozzle diameter in mm (default 0.4).
                apply: If True, apply the best orientation and write the
                    reoriented STL to disk.
                output_path: Output path for the reoriented STL.  Only used
                    when ``apply`` is True.  Defaults to
                    ``<input>_oriented.stl``.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("generate"):
                return err

            # STEP in, mesh out — the one shared door, never a per-tool
            # branch, so the CAD format engineering customers actually send
            # works here instead of failing several layers down.
            from kiln.step_import import resolve_mesh_input

            file_path, _conversion, _refusal = resolve_mesh_input(file_path)
            if _refusal:
                return _refusal

            import kiln.server as _srv
            from kiln.auto_orient import (
                _kiln_pro_ranking_note,
                apply_orientation,
                describe_orientation_change,
                find_optimal_orientation,
                worth_turning,
            )

            try:
                result = find_optimal_orientation(
                    file_path,
                    candidates=candidates,
                    nozzle_diameter=nozzle_diameter,
                )

                # What turning it changes, in its own units; the bed-contact
                # scores stay in ``result`` for comparing versions.
                resp: dict[str, Any] = {
                    "success": True,
                    "result": result.to_dict(),
                    "message": describe_orientation_change(result),
                }
                if not apply and worth_turning(result):
                    resp["message"] += "  Call again with apply=True to write the turned copy."

                note = _kiln_pro_ranking_note()
                if note:
                    resp["upgrade_hint"] = note

                if apply:
                    oriented_path = apply_orientation(
                        file_path,
                        result.best.rotation_x,
                        result.best.rotation_y,
                        result.best.rotation_z,
                        output_path=output_path,
                    )
                    resp["oriented_file"] = oriented_path
                    resp["message"] += f"  Reoriented file: {oriented_path}"

                return resp
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to auto-orient model: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in auto_orient_model")
                return _srv._error_dict(
                    f"Unexpected error in auto_orient_model: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def estimate_supports(
            file_path: str,
            max_overhang_angle: float = 45.0,
        ) -> dict:
            """Estimate support volume for a 3D model.

            Analyzes the mesh for overhangs and estimates the volume of
            support material needed to print the model in its current
            orientation.

            Args:
                file_path: Path to an STL or OBJ mesh file.
                max_overhang_angle: Maximum overhang angle in degrees
                    before supports are needed (default 45).
            """
            # STEP in, mesh out — the one shared door, never a per-tool
            # branch, so the CAD format engineering customers actually send
            # works here instead of failing several layers down.
            from kiln.step_import import resolve_mesh_input

            file_path, _conversion, _refusal = resolve_mesh_input(file_path)
            if _refusal:
                return _refusal

            import kiln.server as _srv
            from kiln.auto_orient import estimate_supports as _estimate

            try:
                estimate = _estimate(
                    file_path,
                    max_overhang_angle=max_overhang_angle,
                )
                return {
                    "success": True,
                    "estimate": estimate.to_dict(),
                    "printability_verdict": estimate.printability_verdict,
                    "printability_verdict_text": estimate.printability_verdict_text,
                    "message": estimate.message,
                }
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to estimate supports: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in estimate_supports")
                return _srv._error_dict(
                    f"Unexpected error in estimate_supports: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def recommend_adhesion_settings(
            model_path: str,
            material: str = "PLA",
            printer_id: str | None = None,
        ) -> dict:
            """Recommend brim/raft settings for a 3D model based on geometry + material.

            Analyzes the model's bed contact area, material warp tendency, and
            printer type to produce a concrete brim width and optional raft
            recommendation.  Returns ``slicer_overrides`` ready to pass to
            ``slice_model`` or ``slice_and_print``.

            Args:
                model_path: Path to an STL or OBJ mesh file.
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``,
                    ``"PETG"``).  Affects warp risk calculation.
                printer_id: Optional printer model ID (e.g. ``"bambu_a1"``).
                    Used to detect bed-slinger printers that need wider brims.
            """
            import kiln.server as _srv
            from kiln.printability import (
                analyze_printability as _analyze,
            )

            try:
                report = _analyze(
                    model_path,
                    material=material,
                    printer_id=printer_id or None,
                )
                # The report's own decision: the same brim every other
                # door, and the report's recommendations, speak of.
                rec = report.adhesion
                if rec is None:
                    return {
                        "success": True,
                        "recommendation": None,
                        "message": "Could not analyze bed adhesion for this model.",
                    }
                return {
                    "success": True,
                    "recommendation": rec.to_dict(),
                    "message": rec.rationale,
                }
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to analyze adhesion: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in recommend_adhesion_settings")
                return _srv._error_dict(
                    f"Unexpected error in recommend_adhesion_settings: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def diagnose_print_failure_live(
            printer_name: str | None = None,
            model_path: str | None = None,
            material: str | None = None,
            printer_id: str | None = None,
        ) -> dict:
            """Diagnose a print failure using live printer state + model geometry.

            Unlike ``analyze_print_failure`` (which requires a job_id and
            analyzes historical data), this tool works in real-time by
            reading the current printer state and optionally analyzing
            the model that was being printed.

            Combines printer temperature deltas, bed adhesion analysis,
            overhang geometry, material properties, and printer intelligence
            to produce a ranked diagnosis with actionable fixes.

            Args:
                printer_name: Printer to diagnose.  Omit for the default printer.
                model_path: Path to the model file that was being printed.
                    Enables geometry-based diagnosis (adhesion, overhangs).
                material: Filament material (e.g. ``"ABS"``, ``"PLA"``).
                printer_id: Printer model ID (e.g. ``"bambu_a1"``).
                    Enables printer-specific intelligence lookup.
            """
            import kiln.server as _srv
            from kiln.printability import collect_failure_signals, diagnose_from_signals

            try:
                # 1. Printer state (when a printer answers)
                state = None
                state_data: dict[str, Any] | None = None
                try:
                    adapter = _srv._resolve_adapter(printer_name)
                    state = adapter.get_state()
                    state_data = state.to_dict()
                except Exception as exc:
                    _logger.debug("Could not get printer state: %s", exc)

                # 2. Model analysis (optional)
                report = None
                model_analysis: dict[str, Any] | None = None
                if model_path:
                    try:
                        from kiln.printability import analyze_printability as _analyze

                        report = _analyze(
                            model_path,
                            material=material or "pla",
                            printer_id=printer_id or None,
                        )
                        model_analysis = report.to_dict()
                    except Exception as exc:
                        _logger.debug("Could not analyze model: %s", exc)

                # 3. Signals, gathered the one way every diagnosis door
                # gathers them — with the intelligence of the machine being
                # diagnosed: an aimed call fell back to the default
                # printer's model, so a second machine's failure was read
                # against another printer's enclosure and failure modes.
                effective_pid = _srv._resolve_printer_profile_id(printer_id, printer_name)
                signals = collect_failure_signals(
                    state=state,
                    report=report,
                    printer_id=effective_pid,
                    material=material,
                )

                # --- Run diagnosis ---
                diagnosis = diagnose_from_signals(
                    signals,
                    printer_id=effective_pid,
                    material=material,
                )

                result: dict[str, Any] = {
                    "success": True,
                    "diagnosis": diagnosis.to_dict(),
                    "message": (
                        f"Diagnosis: {diagnosis.failure_category} failure "
                        f"(confidence {diagnosis.confidence:.0%}).  "
                        f"Top cause: {diagnosis.probable_causes[0] if diagnosis.probable_causes else 'unknown'}."
                    ),
                }
                if state_data:
                    result["printer_state"] = state_data
                if model_analysis:
                    result["model_analysis"] = model_analysis
                return result

            except Exception as exc:
                _logger.exception("Unexpected error in diagnose_print_failure_live")
                return _srv._error_dict(
                    f"Unexpected error in diagnose_print_failure_live: {exc}",
                    code="INTERNAL_ERROR",
                )

        _logger.debug("Registered printability tools")


plugin = _PrintabilityToolsPlugin()
