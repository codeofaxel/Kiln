"""Post-generation validation pipeline plugin — comprehensive pass/fail report.

Validates any 3D model (AI-generated, downloaded, or user-created)
before printing.  Chains format check, mesh analysis, watertight check,
auto-repair, printability analysis, structural assessment, and bed-fit
check into a single orchestration tool.

Every AI model passes Kiln's engineering review before it touches your
printer.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.

The helpers, constants, dataclasses, and eleven ``_step_*`` functions
live in :mod:`kiln.plugins._validation_pipeline_internals` so this file
stays focused on the MCP tool surface.  Private symbols are re-exported
below so existing tests that reach for them via
``from kiln.plugins.validation_pipeline_tools import _CheckResult`` keep
working.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

# Re-export the internals so tests + callers can reach private symbols
# from this module path (backward-compatible import surface).
from kiln.plugins._validation_pipeline_internals import (
    _ABS_WARP_THRESHOLD_MM as _ABS_WARP_THRESHOLD_MM,
)
from kiln.plugins._validation_pipeline_internals import (
    _DEFAULT_INFILL_FACTOR as _DEFAULT_INFILL_FACTOR,
)
from kiln.plugins._validation_pipeline_internals import (
    _MATERIAL_COST_PER_GRAM as _MATERIAL_COST_PER_GRAM,
)
from kiln.plugins._validation_pipeline_internals import (
    _MIN_PASS_SCORE as _MIN_PASS_SCORE,
)
from kiln.plugins._validation_pipeline_internals import (
    _PLA_DENSITY_G_PER_CM3 as _PLA_DENSITY_G_PER_CM3,
)
from kiln.plugins._validation_pipeline_internals import (
    _SCORE_PENALTY_ERROR as _SCORE_PENALTY_ERROR,
)
from kiln.plugins._validation_pipeline_internals import (
    _SCORE_PENALTY_REPAIR as _SCORE_PENALTY_REPAIR,
)
from kiln.plugins._validation_pipeline_internals import (
    _SCORE_PENALTY_SKIP as _SCORE_PENALTY_SKIP,
)
from kiln.plugins._validation_pipeline_internals import (
    _SCORE_PENALTY_WARNING as _SCORE_PENALTY_WARNING,
)
from kiln.plugins._validation_pipeline_internals import (
    _SIMPLIFY_THRESHOLD as _SIMPLIFY_THRESHOLD,
)
from kiln.plugins._validation_pipeline_internals import (
    _STL_HEADER_SIZE as _STL_HEADER_SIZE,
)
from kiln.plugins._validation_pipeline_internals import (
    _STL_TRIANGLE_SIZE as _STL_TRIANGLE_SIZE,
)
from kiln.plugins._validation_pipeline_internals import (
    _SUPPORTED_FORMATS as _SUPPORTED_FORMATS,
)
from kiln.plugins._validation_pipeline_internals import (
    _auto_scale_if_needed as _auto_scale_if_needed,
)
from kiln.plugins._validation_pipeline_internals import (
    _CheckResult as _CheckResult,
)
from kiln.plugins._validation_pipeline_internals import (
    _compute_printability_score as _compute_printability_score,
)
from kiln.plugins._validation_pipeline_internals import (
    _get_build_volume_for_printer as _get_build_volume_for_printer,
)
from kiln.plugins._validation_pipeline_internals import (
    _inline_stl_analysis as _inline_stl_analysis,
)
from kiln.plugins._validation_pipeline_internals import (
    _inline_stl_binary_fallback as _inline_stl_binary_fallback,
)
from kiln.plugins._validation_pipeline_internals import (
    _inline_stl_scale as _inline_stl_scale,
)
from kiln.plugins._validation_pipeline_internals import (
    _max_dim_mm as _max_dim_mm,
)
from kiln.plugins._validation_pipeline_internals import (
    _PipelineReport as _PipelineReport,
)
from kiln.plugins._validation_pipeline_internals import (
    _run_material_check as _run_material_check,
)
from kiln.plugins._validation_pipeline_internals import (
    _sanitize_summary_detail as _sanitize_summary_detail,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_auto_scale as _step_auto_scale,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_bed_fit as _step_bed_fit,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_estimate as _step_estimate,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_format_check as _step_format_check,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_material_check as _step_material_check,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_mesh_analysis as _step_mesh_analysis,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_printability as _step_printability,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_repair as _step_repair,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_structural as _step_structural,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_support_assessment as _step_support_assessment,
)
from kiln.plugins._validation_pipeline_internals import (
    _step_watertight_check as _step_watertight_check,
)
from kiln.plugins._validation_pipeline_internals import (
    _unit_verdict as _unit_verdict,
)
from kiln.plugins._validation_pipeline_internals import (
    _UnitVerdict as _UnitVerdict,
)

_logger = logging.getLogger(__name__)


def run_full_validation_pipeline(
    input_path: str,
    printer_id: str = "",
    material: str = "",
) -> dict[str, Any]:
    """Module-level entry point for the full pre-print validation pipeline.

    Same orchestration as the ``validate_and_prepare`` MCP tool, lifted out
    of the closure so other call sites (``slice_and_print``,
    ``run_quick_print``, recovery retries) can run the gate programmatically
    without going through MCP.

    Runs format check, mesh analysis, optional auto-scale, watertight check,
    auto-repair, printability analysis, support assessment, structural check,
    bed-fit, material check, and estimation.  Returns the same dict shape as
    ``validate_and_prepare`` — including ``ready_to_print``, ``validated_path``,
    ``printability_score``, ``next_action``, and ``summary``.

    :param input_path: Path to a 3D model file (.stl, .3mf, .obj, .step, .glb).
    :param printer_id: Optional printer model ID for bed-fit checking.
    :param material: Optional material name for material-specific checks.
    :returns: Dict matching ``validate_and_prepare``'s return shape.
    """
    report = _PipelineReport(input_path=input_path)

    # Step 1: Format check
    ext = _step_format_check(report, input_path)
    if ext is None:
        return report.to_dict()

    # Step 1b: A STEP file is B-rep, not a mesh.  The format check accepts
    # .step, so without this the pipeline carried it four steps deeper and
    # then raised an uncaught "Unsupported format: .step" from the estimator
    # — the front door saying yes and the back room saying no.  Convert here,
    # once, and every step after this sees an ordinary mesh.
    from kiln.step_import import NoBackendError, ensure_mesh_path

    try:
        input_path, _step_note, _step_conversion = ensure_mesh_path(
            input_path, with_record=True
        )
    except NoBackendError as exc:
        report.checks.append(_CheckResult(
            name="format",
            passed=False,
            details=str(exc),
            severity="error",
        ))
        report.status = "fail"
        report.ready_to_print = False
        report.validated_path = input_path
        report.summary = (
            "Not ready (0/100). This is a STEP file and no converter is "
            "installed."
        )
        report.printability_score = 0
        report.next_action = None
        result = report.to_dict()
        # The structured remedy travels with the report so the agent can tell
        # the user the one command, or that it's a server-side gap.
        result["remedy"] = exc.remedy
        return result
    except Exception as exc:  # noqa: BLE001 — a bad STEP is a user error
        report.checks.append(_CheckResult(
            name="format",
            passed=False,
            details=f"STEP conversion failed: {exc}",
            severity="error",
        ))
        report.status = "fail"
        report.ready_to_print = False
        report.validated_path = input_path
        report.summary = f"Not ready (0/100). STEP conversion failed: {exc}"
        report.printability_score = 0
        report.next_action = None
        return report.to_dict()

    if _step_note:
        report.checks.append(_CheckResult(
            name="step_conversion",
            passed=True,
            details=_step_note,
        ))
        ext = Path(input_path).suffix.lower()

    # Step 2: Mesh analysis
    _step_mesh_analysis(report, input_path, ext)

    # Step 2b: Auto-scale
    input_path, _auto_scaled = _step_auto_scale(report, input_path, ext)

    # Step 3: Watertight check
    is_manifold = _step_watertight_check(report, input_path)

    # Step 4: Auto-repair
    working_path = _step_repair(
        report, input_path, Path(input_path), is_manifold,
    )

    # Step 5: Printability
    _step_printability(report, working_path)

    # Step 5b: Support assessment
    _step_support_assessment(
        report, working_path, material,
    )

    # Step 6: Structural
    _step_structural(report)

    # Step 7: Bed fit + scale check
    _step_bed_fit(report, printer_id, _auto_scaled)

    # Step 8: Material check
    _step_material_check(report, material)

    # Step 9: Estimate
    _step_estimate(report, working_path)

    # ----------------------------------------------------------
    # Step 10: Aggregate verdict
    # ----------------------------------------------------------
    has_failure = any(
        not c.passed and c.severity == "error" for c in report.checks
    )
    has_warning = any(
        (not c.passed and c.severity == "warning")
        or (c.passed and c.severity == "warning")
        for c in report.checks
    )

    if has_failure:
        report.status = "fail"
        report.ready_to_print = False
    elif has_warning:
        report.status = "pass_with_warnings"
        report.ready_to_print = True
    else:
        report.status = "pass"
        report.ready_to_print = True

    # ----------------------------------------------------------
    # Step 11: Printability score
    # ----------------------------------------------------------
    report.printability_score, report.score_breakdown = _compute_printability_score(
        report.checks,
        repaired=report.repaired,
    )

    # ----------------------------------------------------------
    # Step 12: validated_path
    # ----------------------------------------------------------
    report.validated_path = report.repaired_path or input_path

    # ----------------------------------------------------------
    # Step 13: summary
    # ----------------------------------------------------------
    issues: list[str] = []
    warnings: list[str] = []
    for c in report.checks:
        if not c.passed and c.severity == "error":
            issues.append(_sanitize_summary_detail(c.details))
        elif not c.passed and c.severity == "warning":
            warnings.append(_sanitize_summary_detail(c.details))

    score_str = f"{report.printability_score}/100"

    # Build cost/time snippet for summary from model_info
    _est_snippet = ""
    _t = report.model_info.get("estimated_print_time_min")
    _c = report.model_info.get("estimated_cost_usd")
    if _t:
        _est_snippet = f" ~{_t} min, ~${_c:.2f}." if _c else f" ~{_t} min."

    if report.ready_to_print and not issues and not warnings:
        report.summary = f"Print-ready ({score_str}).{_est_snippet}"
    elif report.ready_to_print:
        count = len(warnings)
        label = "warning" if count == 1 else "warnings"
        first = warnings[0] if warnings else ""
        base = f"Print-ready ({score_str}).{_est_snippet} {count} {label}: {first}"
        report.summary = base[:200]
    else:
        count = len(issues)
        label = "issue" if count == 1 else "issues"
        brief = "; ".join(issues[:2])
        base = f"Not ready ({score_str}). {count} {label}: {brief}"
        report.summary = base[:200]

    # ----------------------------------------------------------
    # Step 14: next_action
    # ----------------------------------------------------------
    if report.ready_to_print:
        slice_args: dict[str, Any] = {"input_path": report.validated_path}
        if printer_id:
            slice_args["printer_id"] = printer_id
        report.next_action = {
            "tool": "slice_model",
            "args": slice_args,
        }
    else:
        # Determine most actionable next step
        bed_fail = any(
            not c.passed and c.name == "bed_fit" for c in report.checks
        )
        mesh_fail = any(
            not c.passed and c.name in ("watertight", "repair")
            for c in report.checks
        )
        printability_fail = any(
            not c.passed and c.name == "printability"
            for c in report.checks
        )
        scale_fail = any(
            not c.passed and c.name == "scale_check"
            for c in report.checks
        )
        if bed_fail:
            action: dict[str, Any] = {
                "tool": "scale_mesh_to_fit",
                "reason": "Model exceeds printer build volume",
            }
            if printer_id:
                action["printer_id"] = printer_id
            report.next_action = action
        elif scale_fail:
            report.next_action = {
                "tool": "rescale_model",
                "reason": "Model is suspiciously small — likely exported in wrong units",
            }
        elif mesh_fail:
            report.next_action = {
                "tool": "repair_mesh_advanced",
                "reason": "Mesh is non-manifold and auto-repair failed",
            }
        elif printability_fail:
            report.next_action = {
                "tool": "auto_orient_model",
                "reason": "Low printability score — reorienting may improve it",
            }
        else:
            report.next_action = None

    out = report.to_dict()
    if _step_conversion is not None:
        # Every score below was measured on triangles, and this says how
        # closely those triangles follow the CAD they came from.  A
        # printability verdict taken on a 0.162 mm approximation is a
        # different claim from the same verdict on a 0.0068 mm one, and
        # without this the report reads identically either way.
        from dataclasses import asdict as _asdict

        out["conversion"] = _asdict(_step_conversion)
    return out


class _ValidationPipelinePlugin:
    """Post-generation validation pipeline — comprehensive pass/fail report.

    Tools:
        - validate_and_prepare
        - prepare_ai_model_for_print
    """

    @property
    def name(self) -> str:
        return "validation_pipeline_tools"

    @property
    def description(self) -> str:
        return "Post-generation validation pipeline for any 3D model"

    def register(self, mcp: Any) -> None:
        """Register validation pipeline tools with the MCP server."""

        @mcp.tool()
        def validate_and_prepare(
            input_path: str,
            printer_id: str = "",
            material: str = "",
        ) -> dict[str, Any]:
            """Comprehensive validation pipeline for any 3D model before printing.

            Runs format check, mesh analysis, watertight check, auto-repair,
            printability analysis, structural assessment, bed-fit check, and
            (optionally) material-specific checks.
            Returns a detailed pass/fail report with actionable recommendations
            plus a numeric printability score (0-100).

            Works with any model — AI-generated, downloaded from marketplaces,
            or created in CAD.  Every model passes Kiln's engineering review
            before it touches your printer.

            Each step is resilient — if a step's underlying module is unavailable
            the step is skipped and the pipeline continues.

            :param input_path: Path to a 3D model file (.stl, .3mf, .obj, .step, .glb).
            :param printer_id: Optional printer model ID (e.g. "bambu_a1") for
                bed-fit checking.  If empty, bed-fit check is skipped.
            :param material: Optional material name (e.g. "pla", "petg", "abs",
                "asa", "tpu").  When provided, adds a material-specific check
                for known print-quality risks.  If empty, material check is skipped.
            :returns: Dict with pass/fail status, per-check details, recommendations,
                ``printability_score`` (0-100), and ``score_breakdown``.
            """
            return run_full_validation_pipeline(
                input_path, printer_id=printer_id, material=material,
            )

        @mcp.tool()
        def prepare_ai_model_for_print(
            input_path: str,
            target_height_mm: float = 0,
            printer_id: str = "",
            material: str = "PLA",
        ) -> dict:
            """Prepare any AI-generated model for printing — fixes the unit mix-up.

            AI model generators (Meshy, Tripo, Stability, Gemini) routinely
            export models in meters instead of millimeters, so a 60mm figurine
            reads as 0.06mm.  This tool corrects that by the real unit
            conversion when exactly one explains the size — never by scaling
            to an invented "reasonable" target — then runs the full validation
            pipeline and provides smart recommendations for simplification
            and hollowing.

            Pipeline:
                1. Run validate_and_prepare for baseline analysis
                2. Size — scale to target_height_mm when given; otherwise
                   apply a unit correction only when exactly one real
                   conversion (meters, centimeters, inches, microns) lands
                   the model at a printable size.  A size several units
                   could explain, or none can, is reported, never guessed at.
                3. Mesh simplification recommendation (if > 100K triangles)
                4. Smart hollow recommendation (only when appropriate)
                5. Re-validate the scaled model
                6. Return combined before/after comparison

            Works with STL, OBJ, and 3MF files.

            :param input_path: Path to the AI-generated model file.
            :param target_height_mm: Desired height in mm — an instruction,
                honored at any starting size.  If 0, only a unit mistake is
                ever fixed; the model's designed size is otherwise kept.
            :param printer_id: Optional printer model ID for bed-fit checking.
            :param material: Material name (default "PLA") for material checks.
            :returns: Dict with original/prepared comparison, actions taken,
                recommendations, and next_action for slicing.
            """
            actions_taken: list[str] = []
            recommendations: list[str] = []

            # -------------------------------------------------------
            # Step 1: Baseline validation
            # -------------------------------------------------------
            baseline = validate_and_prepare(
                input_path, printer_id=printer_id, material=material,
            )

            original_info: dict[str, Any] = {
                "dimensions_mm": baseline.get("model_info", {}).get("dimensions_mm", {}),
                "triangles": baseline.get("model_info", {}).get("triangles", 0),
                "printability_score": baseline.get("printability_score", 0),
            }

            # Extract dimensions for scaling logic.  ``or``-chaining, not
            # ``dict.get`` defaults, so a present-but-zero ``z`` still falls
            # through to ``height_mm`` — same idiom as ``_max_dim_mm``.
            dims = baseline.get("model_info", {}).get("dimensions_mm", {})
            z = float(dims.get("z") or dims.get("height_mm") or 0)
            max_dim = _max_dim_mm(baseline.get("model_info", {}))
            tri_count = int(baseline.get("model_info", {}).get("triangles", 0))

            # Track the working path — may change after scaling
            working_path = baseline.get("validated_path", input_path)
            scale_factor = 0.0

            # -------------------------------------------------------
            # Step 2: size — the caller's instruction, else a unit repair
            # -------------------------------------------------------
            # Two different questions that used to share one branch.  A
            # ``target_height_mm`` is an INSTRUCTION and always wins: the
            # user said how big they want it, so no inference is involved
            # and it applies at any size.  Without one, the only thing we
            # may change is a unit mistake, and only when exactly one real
            # conversion explains the size.
            #
            # What this replaces: three invented sizes chosen by aspect
            # ratio — 80mm for "figurine", 100mm for "flat", 60mm for
            # "cubic".  None is a unit conversion; each simply asserts how
            # big the user's object ought to be, and is wrong for every
            # object whose true size is not that number.
            verdict = _unit_verdict(max_dim)
            reason = ""

            if target_height_mm > 0 and z > 0:
                scale_factor = target_height_mm / z
                reason = f"scaled to the requested height of {target_height_mm}mm"
            elif verdict.corrected:
                scale_factor = verdict.factor
                reason = verdict.describe()
            elif verdict.status in ("ambiguous", "oversize", "unexplained"):
                recommendations.append(verdict.describe())

            # One writer for both cases.  The duplicate that used to sit
            # below — same rescale, same fallback, same logging, reached
            # only when a target height met a normal-size model — is where
            # the two branches were free to drift apart.
            if scale_factor > 0 and abs(scale_factor - 1.0) > 0.01:
                scaled_path: str | None = None

                # Try rescale_model from server
                try:
                    from kiln.server import rescale_model as _rescale

                    result = _rescale(input_path, scale_factor=scale_factor)
                    sp = result.get("path", "")
                    if sp and Path(sp).exists():
                        scaled_path = sp
                except Exception:
                    _logger.debug(
                        "rescale_model unavailable for prepare_ai_model",
                        exc_info=True,
                    )

                # Inline fallback for STL
                if scaled_path is None and Path(input_path).suffix.lower() == ".stl":
                    try:
                        scaled_path = _inline_stl_scale(input_path, scale_factor)
                    except Exception:
                        _logger.debug(
                            "Inline STL scaling failed in prepare_ai_model",
                            exc_info=True,
                        )

                if scaled_path is not None:
                    working_path = scaled_path
                    actions_taken.append(f"Scaled {scale_factor:g}x — {reason}")
                else:
                    # Could not scale — report factor for manual use
                    recommendations.append(
                        f"Could not auto-scale. Apply scale factor "
                        f"{scale_factor:g}x manually with rescale_model."
                    )

            # -------------------------------------------------------
            # Step 3: Mesh simplification recommendation
            # -------------------------------------------------------
            if tri_count > _SIMPLIFY_THRESHOLD:
                recommendations.append(
                    f"Model has {tri_count:,} triangles — consider "
                    f"simplify_mesh_model for faster slicing "
                    f"(FDM can't resolve detail finer than 0.1mm)"
                )

            # -------------------------------------------------------
            # Step 4: Smart hollow recommendation
            # -------------------------------------------------------
            # Use scaled dimensions if scaling occurred, otherwise baseline.
            _h_dims = baseline.get("model_info", {}).get("dimensions_mm", {})
            if scale_factor > 0 and _h_dims:
                # Apply scale factor to baseline dims for hollow analysis
                _h_dims = {
                    k: float(v or 0) * scale_factor
                    for k, v in _h_dims.items()
                    if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit())
                }
            _h_x = float(_h_dims.get("x", _h_dims.get("width_mm", 0)) or 0)
            _h_y = float(_h_dims.get("y", _h_dims.get("depth_mm", 0)) or 0)
            _h_z = float(_h_dims.get("z", _h_dims.get("height_mm", 0)) or 0)
            _h_max_dim = max(_h_x, _h_y, _h_z)

            # Compute surface-area and volume from bounding box
            _h_sa = 2.0 * (_h_x * _h_y + _h_x * _h_z + _h_y * _h_z) if (_h_x > 0 and _h_y > 0 and _h_z > 0) else 0.0
            _h_vol = _h_x * _h_y * _h_z  # mm3
            _h_bbox_vol_cm3 = _h_vol / 1000.0  # cm3
            _h_sa_vol_ratio = _h_sa / _h_vol if _h_vol > 0 else 0.0

            if _h_max_dim < 30:
                pass  # Too small — walls would be paper-thin, don't suggest hollow
            elif _h_sa_vol_ratio > 1.0:
                # Already thin-shelled — don't suggest hollow
                recommendations.append(
                    "Model is thin-shelled (surface-to-volume ratio "
                    f"{_h_sa_vol_ratio:.2f}/mm) — consider "
                    "thicken_mesh_walls for structural integrity"
                )
            elif _h_bbox_vol_cm3 > 50 and _h_sa_vol_ratio < 0.3:
                # Large, solid model — hollowing saves material
                est_save = min(60, max(20, int(_h_bbox_vol_cm3 / 5)))
                recommendations.append(
                    f"Large solid model ({_h_bbox_vol_cm3:.0f} cm\u00b3, "
                    f"surface-to-volume ratio {_h_sa_vol_ratio:.2f}/mm) "
                    f"— hollow_mesh_model would save ~{est_save}% material "
                    f"and reduce print time"
                )
            # Otherwise: no hollow recommendation (case by case)

            # -------------------------------------------------------
            # Step 5: Re-validate on the prepared model
            # -------------------------------------------------------
            if working_path != input_path:
                prepared_report = validate_and_prepare(
                    working_path, printer_id=printer_id, material=material,
                )
            else:
                prepared_report = baseline

            prepared_dims = prepared_report.get("model_info", {}).get("dimensions_mm", {})
            prepared_info: dict[str, Any] = {
                "dimensions_mm": prepared_dims,
                "triangles": prepared_report.get("model_info", {}).get("triangles", 0),
                "printability_score": prepared_report.get("printability_score", 0),
                "stl_path": working_path,
            }

            # -------------------------------------------------------
            # Step 6: Build return envelope
            # -------------------------------------------------------
            ready = prepared_report.get("ready_to_print", False)

            next_action: dict[str, Any] | None = None
            if ready:
                next_action = {
                    "tool": "slice_model",
                    "args": {"input_path": working_path},
                }
            else:
                next_action = prepared_report.get("next_action")

            response = {
                "status": "success" if ready else "needs_attention",
                "original": original_info,
                "prepared": prepared_info,
                "actions_taken": actions_taken,
                "recommendations": recommendations,
                "scale_factor": round(scale_factor, 2) if scale_factor else 0,
                "ready_to_print": ready,
                "next_action": next_action,
            }
            try:
                from kiln_pro.plugins.git_render_tools import (
                    attach_inspect_bundle,
                )

                return attach_inspect_bundle(
                    response, level="quick", source_path=working_path,
                )
            except ImportError:
                return response

        _logger.debug("Registered validation pipeline tools")


plugin = _ValidationPipelinePlugin()
