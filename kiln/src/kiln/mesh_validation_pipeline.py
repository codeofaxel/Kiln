"""Post-generation mesh validation pipeline.

Orchestrates Kiln's validation, repair, and printability analysis tools
into a single quality gate.  Every AI-generated mesh should pass through
this pipeline before reaching the slicer or printer.

The pipeline:
1. Validate mesh geometry (watertight, dimensions, triangle count)
2. Auto-repair if non-manifold (basic repair → advanced repair)
3. Analyze printability (overhangs, thin walls, bridging, bed adhesion)
4. Check build volume fit (optional auto-scale)
5. Aggregate into pass/fail verdict with detailed report
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ValidationPipelineResult:
    """Result of the full validation pipeline."""

    passed: bool
    file_path: str  # path to the (possibly repaired/scaled) mesh
    original_file_path: str

    # Mesh validation
    is_manifold: bool
    triangle_count: int
    bounding_box: dict[str, float] | None
    dimensions_mm: dict[str, float] | None

    # Repair info
    was_repaired: bool
    repair_stats: dict[str, Any] | None

    # Printability
    printability_score: int  # 0-100
    printability_grade: str  # A/B/C/D/F
    printability_details: dict[str, Any] | None

    # Build volume
    fits_build_volume: bool | None  # None if no build volume provided
    was_scaled: bool
    scale_factor: float  # 1.0 if not scaled

    # Issues and recommendations
    errors: list[str] = field(default_factory=list)  # blocking issues
    warnings: list[str] = field(default_factory=list)  # non-blocking concerns
    recommendations: list[str] = field(default_factory=list)  # from printability analysis
    # What is worth knowing and is not wrong: pieces that sit on each other,
    # each closed, for one.
    notes: list[str] = field(default_factory=list)

    # The printability verdict (kiln.printability.verdict): ``ready``,
    # ``prints_with_supports``, ``wont_print`` or ``check``, and its
    # sentence; ``""`` when the analysis could not run.  This is what
    # ``passed`` reads, never the score.
    verdict: str = ""
    verdict_text: str = ""

    # Summary
    summary: str = ""  # human-readable one-paragraph summary

    def to_dict(self) -> dict[str, Any]:
        # The rating stays in the process (kiln.printability.RATING_KEYS);
        # ``verdict`` and ``verdict_text`` carry the answer.
        from kiln.printability import without_rating

        out = without_rating(asdict(self))
        if isinstance(out.get("printability_details"), dict):
            out["printability_details"] = without_rating(out["printability_details"])
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dimensions_from_bbox(bbox: dict[str, float] | None) -> dict[str, float] | None:
    """Derive width/depth/height from a bounding box dict."""
    if bbox is None:
        return None
    return {
        "x": round(bbox["x_max"] - bbox["x_min"], 3),
        "y": round(bbox["y_max"] - bbox["y_min"], 3),
        "z": round(bbox["z_max"] - bbox["z_min"], 3),
    }


def _mesh_fits_volume(
    dims: dict[str, float] | None,
    build_volume: tuple[float, float, float],
) -> bool:
    """Return True if *dims* fit within *build_volume* (x, y, z)."""
    if dims is None:
        return True  # can't measure → assume it fits
    return (
        dims["x"] <= build_volume[0]
        and dims["y"] <= build_volume[1]
        and dims["z"] <= build_volume[2]
    )


def _build_summary(result: ValidationPipelineResult) -> str:
    """Generate a human-readable summary paragraph."""
    parts: list[str] = []
    fname = Path(result.original_file_path).name

    if result.passed:
        parts.append(f"Mesh '{fname}' PASSED validation.")
    else:
        parts.append(f"Mesh '{fname}' FAILED validation.")

    # The verdict leads; the score stays on the result for comparing versions.
    if result.verdict_text:
        parts.append(result.verdict_text)

    # Closed surface / repair
    if result.was_repaired and result.is_manifold:
        parts.append("The surface had holes, and repair closed them.")
    elif not result.is_manifold:
        parts.append("The surface is not closed, and repair could not close it.")
    else:
        parts.append("The surface is closed (watertight).")
    parts.extend(result.notes)

    # Build volume
    if result.fits_build_volume is not None:
        if result.was_scaled:
            parts.append(
                f"Mesh was scaled by {result.scale_factor:.2%} to fit the build volume."
            )
        elif result.fits_build_volume:
            parts.append("Mesh fits within the build volume.")
        else:
            parts.append("Mesh exceeds the build volume.")

    parts.append(f"Triangle count: {result.triangle_count:,}.")

    if result.errors:
        parts.append(f"Errors: {'; '.join(result.errors)}.")
    # The stop the verdict names is said once, there.
    warnings = [w for w in result.warnings if w not in result.verdict_text]
    if warnings:
        parts.append(f"Warnings: {'; '.join(warnings)}.")

    return " ".join(parts)


#: The lowest score that passes a part the check names no single cause for
#: (verdict ``check``): the bar this pass holds a rough part to.  No caller
#: sets it (:func:`kiln.printability.passes`).
_CHECK_BAR = 40


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_validation_pipeline(
    file_path: str,
    *,
    material: str = "PLA",
    nozzle_diameter: float | None = None,
    layer_height: float | None = None,
    build_volume: tuple[float, float, float] | None = None,
    auto_repair: bool = True,
    auto_scale: bool = False,
    printer_id: str | None = None,
) -> ValidationPipelineResult:
    """Run the full mesh validation pipeline.

    Chains validation, optional repair, printability analysis, and
    build-volume checking into a single pass/fail verdict.

    :param file_path: Path to an STL or OBJ mesh file.
    :param material: Filament material for printability analysis.
    :param nozzle_diameter: Printer nozzle diameter in mm.  Left unsaid, the
        size :func:`kiln.assumed_nozzle.assumed_nozzle` picks for *printer_id*.
    :param layer_height: Print layer height in mm; unsaid, the layer Kiln
        slices the nozzle at.
    :param build_volume: Optional ``(x, y, z)`` build volume in mm.
    :param auto_repair: Attempt automatic repair of non-manifold meshes.
    :param auto_scale: Scale mesh down to fit build volume if too large.
    The verdict decides whether a part passes
    (:func:`kiln.printability.passes`): a part that prints, as it is or with
    supports, passes, one that won't print fails, and one the check names no
    single cause for needs :data:`_CHECK_BAR`.

    :param printer_id: Optional printer whose bed the placement check
        measures against when *build_volume* is not given.  An unknown
        printer resolves to no bed, which skips the fit check rather
        than failing the run.
    :returns: :class:`ValidationPipelineResult` with full report.
    """
    from kiln.generation.validation import (
        repair_stl,
        repair_stl_advanced,
        scale_to_fit,
        validate_mesh,
    )

    # Fail fast on a missing input with a clear, cross-platform
    # message.  Without this the pipeline would crash deep inside the
    # auto-repair copy step, surfacing an OS-specific error string
    # (``No such file`` on POSIX, ``WinError 3`` on Windows).
    if not Path(file_path).is_file():
        raise FileNotFoundError(f"Mesh file not found: {file_path}")

    errors: list[str] = []
    warnings: list[str] = []
    recommendations: list[str] = []

    working_path = file_path
    was_repaired = False
    repair_stats: dict[str, Any] | None = None
    _temp_files: list[str] = []  # track temp files for cleanup on failure

    try:
        # ------------------------------------------------------------------
        # Step 1: Parse & validate
        # ------------------------------------------------------------------
        _logger.info("Step 1/4: Validating mesh geometry — %s", file_path)
        validation = validate_mesh(file_path)

        is_manifold = validation.is_manifold
        triangle_count = validation.triangle_count
        bounding_box = validation.bounding_box
        dimensions = _dimensions_from_bbox(bounding_box)

        errors.extend(validation.errors)
        warnings.extend(validation.warnings)

        if not validation.valid and not validation.is_manifold and not auto_repair:
            _logger.warning("Mesh is invalid and auto_repair is disabled.")

        # ------------------------------------------------------------------
        # Step 2: Auto-repair if needed
        # ------------------------------------------------------------------
        if not is_manifold and auto_repair:
            _logger.info("Step 2/4: Mesh is non-manifold — attempting basic repair")

            # Work on a copy to preserve the original.
            suffix = Path(file_path).suffix or ".stl"
            with tempfile.NamedTemporaryFile(
                suffix=suffix, prefix="kiln_repair_", delete=False,
            ) as tmp_fd:
                tmp_repaired = tmp_fd.name
            shutil.copy2(file_path, tmp_repaired)
            _temp_files.append(tmp_repaired)

            # Phase A: basic repair (degenerate removal + normal fix)
            try:
                repair_stats = repair_stl(tmp_repaired)
                _logger.info(
                    "Basic repair complete: %s",
                    {k: v for k, v in repair_stats.items() if k != "path"},
                )
            except Exception as exc:
                errors.append(f"Basic repair failed: {exc}")
                _logger.error("Basic repair failed: %s", exc, exc_info=True)

            # Re-validate after basic repair
            post_basic = validate_mesh(tmp_repaired)
            is_manifold = post_basic.is_manifold
            triangle_count = post_basic.triangle_count
            bounding_box = post_basic.bounding_box
            dimensions = _dimensions_from_bbox(bounding_box)

            # Phase B: advanced repair if still non-manifold
            if not is_manifold:
                _logger.info(
                    "Step 2/4: Still non-manifold — attempting advanced repair (hole closing)",
                )
                try:
                    adv_stats = repair_stl_advanced(tmp_repaired, close_holes=True)
                    if repair_stats is not None:
                        repair_stats["advanced"] = adv_stats
                    else:
                        repair_stats = {"advanced": adv_stats}
                    _logger.info(
                        "Advanced repair complete: %s",
                        {k: v for k, v in adv_stats.items() if k != "path"},
                    )
                except Exception as exc:
                    errors.append(f"Advanced repair failed: {exc}")
                    _logger.error("Advanced repair failed: %s", exc, exc_info=True)

                # Re-validate after advanced repair
                post_adv = validate_mesh(tmp_repaired)
                is_manifold = post_adv.is_manifold
                triangle_count = post_adv.triangle_count
                bounding_box = post_adv.bounding_box
                dimensions = _dimensions_from_bbox(bounding_box)

            if is_manifold:
                was_repaired = True
                working_path = tmp_repaired
                _logger.info("Repair succeeded — mesh is now manifold.")
            else:
                warnings.append("Mesh remains non-manifold after repair attempts.")
                # Still use the repaired copy — degenerate cleanup may help downstream.
                was_repaired = repair_stats is not None
                working_path = tmp_repaired
                _logger.warning("Repair could not achieve manifold status.")
        else:
            _logger.info(
                "Step 2/4: Skipped — mesh is already manifold (or repair disabled).",
            )

        # ------------------------------------------------------------------
        # Step 3: Printability analysis
        # ------------------------------------------------------------------
        _logger.info("Step 3/4: Analyzing printability")

        from kiln.printability import _placement_faults, _resolve_placement_volume

        # Resolve the bed once — an explicit build_volume wins, else the
        # printer catalogue answers (or answers None for a machine it
        # does not know, which skips the fit check rather than failing).
        resolved_build_volume = _resolve_placement_volume(
            build_volume, printer_id,
        )
        # With auto_scale on, step 4 shrinks an oversized mesh to fit and
        # the part becomes printable — so the fit half of the placement
        # check must not dock the score here on a size step 4 is about to
        # fix.  Off-bed geometry is docked either way: scaling does not
        # lift a part back above z = 0.
        placement_volume = None if auto_scale else resolved_build_volume

        printability_score = 0
        printability_grade = "F"
        printability_details: dict[str, Any] | None = None
        verdict_key, verdict_text = "", ""
        # What stops the part printing as it is, beyond placement, read by
        # the one helper the verdict reads (kiln.printability.stops).
        hard_stops: list[str] = []

        try:
            from kiln.assumed_nozzle import assumed_nozzle
            from kiln.printability import analyze_printability

            nozzle = assumed_nozzle(printer_id, stated=nozzle_diameter)
            report = analyze_printability(
                working_path,
                nozzle_diameter=nozzle.diameter_mm,
                layer_height=layer_height,
                material=material.lower(),
                # The bed resolved above; none while auto_scale leaves
                # the fit to step 4.
                build_volume=placement_volume,
            )
            # The engine was handed a size, not the printer: say where
            # the size really came from.
            report.nozzle = nozzle.to_dict()
            printability_score = report.score
            printability_grade = report.grade
            printability_details = report.to_dict()
            recommendations.extend(report.recommendations)
            from kiln.printability import stops as _stops
            from kiln.printability import verdict_of as _verdict_of

            hard_stops = _stops(report)
            verdict_key, verdict_text = _verdict_of(report)
            _logger.info(
                "Printability score: %d/100 (grade %s)", report.score, report.grade,
            )
        except ImportError:
            warnings.append(
                "Printability analysis unavailable (missing kiln.printability module).",
            )
            _logger.warning("kiln.printability not importable — skipping analysis")
        except Exception as exc:
            errors.append(f"Printability analysis failed: {exc}")
            _logger.error("Printability analysis failed: %s", exc, exc_info=True)

        # The placement faults, read once from the mesh for the warnings
        # below.  Pure detection: analyze_printability already took them
        # off the score, and this does not deduct a second time.
        placement_faults = _placement_faults(
            bounding_box, build_volume=placement_volume,
        )

        # ------------------------------------------------------------------
        # Step 4: Build volume check (+ optional auto-scale)
        # ------------------------------------------------------------------
        fits_build_volume: bool | None = None
        was_scaled = False
        scale_factor = 1.0

        if resolved_build_volume is not None:
            _logger.info(
                "Step 4/4: Checking build volume fit (%.0f x %.0f x %.0f mm)",
                *resolved_build_volume,
            )
            fits = _mesh_fits_volume(dimensions, resolved_build_volume)

            if fits:
                fits_build_volume = True
                _logger.info("Mesh fits within build volume.")
            elif auto_scale:
                _logger.info("Mesh exceeds build volume — auto-scaling to fit.")
                try:
                    scale_result = scale_to_fit(
                        working_path,
                        max_x_mm=resolved_build_volume[0],
                        max_y_mm=resolved_build_volume[1],
                        max_z_mm=resolved_build_volume[2],
                    )
                    scale_factor = scale_result["scale_factor"]
                    was_scaled = scale_factor < 1.0
                    working_path = scale_result["path"]
                    fits_build_volume = True

                    if was_scaled:
                        warnings.append(
                            f"Mesh was scaled to {scale_factor:.2%} of original size "
                            f"to fit build volume.",
                        )
                        dimensions = scale_result.get("new_dimensions", dimensions)
                        _logger.info(
                            "Scaled to %.2f%% — new dims: %s",
                            scale_factor * 100,
                            dimensions,
                        )
                    else:
                        _logger.info("Mesh already fits — no scaling needed.")
                except Exception as exc:
                    errors.append(f"Auto-scale failed: {exc}")
                    fits_build_volume = False
                    _logger.error("Auto-scale failed: %s", exc, exc_info=True)
            else:
                fits_build_volume = False
                errors.append(
                    f"Mesh exceeds build volume "
                    f"({dimensions!r} vs "
                    f"{resolved_build_volume[0]}x{resolved_build_volume[1]}"
                    f"x{resolved_build_volume[2]} mm).",
                )
                _logger.warning("Mesh does not fit build volume.")
        else:
            _logger.info("Step 4/4: Skipped — no build volume resolved.")

        # ------------------------------------------------------------------
        # Step 5: Composite verdict
        # ------------------------------------------------------------------
        passed = True

        if not is_manifold:
            passed = False

        # The verdict decides (kiln.printability.passes): a part that prints
        # with supports passes whatever it scores.
        from kiln.printability import passes as _passes

        if not _passes(verdict_key, printability_score, bar=_CHECK_BAR):
            passed = False
            if verdict_key == "check":
                warnings.append(
                    "No single cause is named, and the part is not clear to print "
                    "as it is: check it before printing.",
                )

        if fits_build_volume is False:
            passed = False

        # Placement is feasibility, not quality: a part that cannot sit on
        # the bed fails the run on its own, as fits_build_volume does.
        if placement_faults:
            passed = False
            warnings.extend(placement_faults)

        # A part the analysis says will not print as it is (smaller than any
        # printer makes, resting on a point, a wall the nozzle cannot lay)
        # fails here too, whatever its score.
        unsaid = [stop for stop in hard_stops if stop not in placement_faults]
        if unsaid:
            passed = False
            warnings[:0] = unsaid

        if validation.errors:
            passed = False

        result = ValidationPipelineResult(
            passed=passed,
            file_path=working_path,
            original_file_path=file_path,
            is_manifold=is_manifold,
            triangle_count=triangle_count,
            bounding_box=bounding_box,
            dimensions_mm=dimensions,
            was_repaired=was_repaired,
            repair_stats=repair_stats,
            printability_score=printability_score,
            printability_grade=printability_grade,
            printability_details=printability_details,
            fits_build_volume=fits_build_volume,
            was_scaled=was_scaled,
            scale_factor=scale_factor,
            errors=errors,
            warnings=warnings,
            recommendations=recommendations,
            notes=list(validation.notes),
            verdict=verdict_key,
            verdict_text=verdict_text,
            summary="",
        )
        result.summary = _build_summary(result)

        _logger.info("Pipeline complete — %s", "PASSED" if passed else "FAILED")
        return result

    except Exception:
        # Clean up temp files on unexpected failure
        for tmp in _temp_files:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
        raise
