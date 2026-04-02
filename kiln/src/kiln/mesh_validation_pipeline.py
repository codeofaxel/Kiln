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

import logging
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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

    # Summary
    summary: str = ""  # human-readable one-paragraph summary

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    # Manifold / repair
    if result.was_repaired and result.is_manifold:
        parts.append("The mesh was non-manifold but was successfully repaired.")
    elif not result.is_manifold:
        parts.append("The mesh is non-manifold and could not be repaired.")
    else:
        parts.append("The mesh is manifold (watertight).")

    # Printability
    parts.append(
        f"Printability score: {result.printability_score}/100 "
        f"(grade {result.printability_grade})."
    )

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
    if result.warnings:
        parts.append(f"Warnings: {'; '.join(result.warnings)}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_validation_pipeline(
    file_path: str,
    *,
    material: str = "PLA",
    nozzle_diameter: float = 0.4,
    layer_height: float = 0.2,
    build_volume: tuple[float, float, float] | None = None,
    auto_repair: bool = True,
    auto_scale: bool = False,
    min_printability_score: int = 40,
) -> ValidationPipelineResult:
    """Run the full mesh validation pipeline.

    Chains validation, optional repair, printability analysis, and
    build-volume checking into a single pass/fail verdict.

    :param file_path: Path to an STL or OBJ mesh file.
    :param material: Filament material for printability analysis.
    :param nozzle_diameter: Printer nozzle diameter in mm.
    :param layer_height: Print layer height in mm.
    :param build_volume: Optional ``(x, y, z)`` build volume in mm.
    :param auto_repair: Attempt automatic repair of non-manifold meshes.
    :param auto_scale: Scale mesh down to fit build volume if too large.
    :param min_printability_score: Minimum printability score to pass (0-100).
    :returns: :class:`ValidationPipelineResult` with full report.
    """
    from kiln.generation.validation import (
        repair_stl,
        repair_stl_advanced,
        scale_to_fit,
        validate_mesh,
    )

    errors: list[str] = []
    warnings: list[str] = []
    recommendations: list[str] = []

    working_path = file_path
    was_repaired = False
    repair_stats: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Step 1: Parse & validate
    # ------------------------------------------------------------------
    logger.info("Step 1/4: Validating mesh geometry — %s", file_path)
    validation = validate_mesh(file_path)

    is_manifold = validation.is_manifold
    triangle_count = validation.triangle_count
    bounding_box = validation.bounding_box
    dimensions = _dimensions_from_bbox(bounding_box)

    errors.extend(validation.errors)
    warnings.extend(validation.warnings)

    if not validation.valid and not validation.is_manifold and not auto_repair:
        # Mesh is broken and we aren't allowed to repair — early out.
        logger.warning("Mesh is invalid and auto_repair is disabled.")

    # ------------------------------------------------------------------
    # Step 2: Auto-repair if needed
    # ------------------------------------------------------------------
    if not is_manifold and auto_repair:
        logger.info("Step 2/4: Mesh is non-manifold — attempting basic repair")

        # Work on a copy to preserve the original.
        suffix = Path(file_path).suffix
        tmp_repaired = tempfile.mktemp(suffix=suffix, prefix="kiln_repair_")
        shutil.copy2(file_path, tmp_repaired)

        # Phase A: basic repair (degenerate removal + normal fix)
        try:
            repair_stats = repair_stl(tmp_repaired)
            logger.info(
                "Basic repair complete: %s",
                {k: v for k, v in repair_stats.items() if k != "path"},
            )
        except (ValueError, OSError) as exc:
            errors.append(f"Basic repair failed: {exc}")
            logger.error("Basic repair failed: %s", exc)

        # Re-validate after basic repair
        post_basic = validate_mesh(tmp_repaired)
        is_manifold = post_basic.is_manifold
        triangle_count = post_basic.triangle_count
        bounding_box = post_basic.bounding_box
        dimensions = _dimensions_from_bbox(bounding_box)

        # Phase B: advanced repair if still non-manifold
        if not is_manifold:
            logger.info("Step 2/4: Still non-manifold — attempting advanced repair (hole closing)")
            try:
                adv_stats = repair_stl_advanced(tmp_repaired, close_holes=True)
                # Merge stats
                if repair_stats is not None:
                    repair_stats["advanced"] = adv_stats
                else:
                    repair_stats = {"advanced": adv_stats}
                logger.info(
                    "Advanced repair complete: %s",
                    {k: v for k, v in adv_stats.items() if k != "path"},
                )
            except (ValueError, OSError) as exc:
                errors.append(f"Advanced repair failed: {exc}")
                logger.error("Advanced repair failed: %s", exc)

            # Re-validate after advanced repair
            post_adv = validate_mesh(tmp_repaired)
            is_manifold = post_adv.is_manifold
            triangle_count = post_adv.triangle_count
            bounding_box = post_adv.bounding_box
            dimensions = _dimensions_from_bbox(bounding_box)

        if is_manifold:
            was_repaired = True
            working_path = tmp_repaired
            logger.info("Repair succeeded — mesh is now manifold.")
        else:
            warnings.append("Mesh remains non-manifold after repair attempts.")
            # Still use the repaired copy — degenerate cleanup may help downstream.
            was_repaired = True
            working_path = tmp_repaired
            logger.warning("Repair could not achieve manifold status.")
    else:
        logger.info("Step 2/4: Skipped — mesh is already manifold (or repair disabled).")

    # ------------------------------------------------------------------
    # Step 3: Printability analysis
    # ------------------------------------------------------------------
    logger.info("Step 3/4: Analyzing printability")

    printability_score = 0
    printability_grade = "F"
    printability_details: dict[str, Any] | None = None

    try:
        from kiln.printability import analyze_printability

        report = analyze_printability(
            working_path,
            nozzle_diameter=nozzle_diameter,
            layer_height=layer_height,
            material=material.lower(),
        )
        printability_score = report.score
        printability_grade = report.grade
        printability_details = report.to_dict()
        recommendations.extend(report.recommendations)
        logger.info("Printability score: %d/100 (grade %s)", report.score, report.grade)
    except (ValueError, OSError) as exc:
        errors.append(f"Printability analysis failed: {exc}")
        logger.error("Printability analysis failed: %s", exc)

    # ------------------------------------------------------------------
    # Step 4: Build volume check (+ optional auto-scale)
    # ------------------------------------------------------------------
    fits_build_volume: bool | None = None
    was_scaled = False
    scale_factor = 1.0

    if build_volume is not None:
        logger.info(
            "Step 4/4: Checking build volume fit (%.0f x %.0f x %.0f mm)",
            *build_volume,
        )
        fits = _mesh_fits_volume(dimensions, build_volume)

        if fits:
            fits_build_volume = True
            logger.info("Mesh fits within build volume.")
        elif auto_scale:
            logger.info("Mesh exceeds build volume — auto-scaling to fit.")
            try:
                scale_result = scale_to_fit(
                    working_path,
                    max_x_mm=build_volume[0],
                    max_y_mm=build_volume[1],
                    max_z_mm=build_volume[2],
                )
                scale_factor = scale_result["scale_factor"]
                was_scaled = scale_factor < 1.0
                working_path = scale_result["path"]
                fits_build_volume = True

                if was_scaled:
                    warnings.append(
                        f"Mesh was scaled to {scale_factor:.2%} of original size "
                        f"to fit build volume."
                    )
                    # Refresh dimensions after scaling
                    dimensions = scale_result.get("new_dimensions", dimensions)
                    logger.info("Scaled to %.2f%% — new dims: %s", scale_factor * 100, dimensions)
                else:
                    logger.info("Mesh already fits — no scaling needed.")
            except (ValueError, OSError) as exc:
                errors.append(f"Auto-scale failed: {exc}")
                fits_build_volume = False
                logger.error("Auto-scale failed: %s", exc)
        else:
            fits_build_volume = False
            errors.append(
                f"Mesh exceeds build volume "
                f"({dimensions!r} vs {build_volume[0]}x{build_volume[1]}x{build_volume[2]} mm)."
            )
            logger.warning("Mesh does not fit build volume.")
    else:
        logger.info("Step 4/4: Skipped — no build volume specified.")

    # ------------------------------------------------------------------
    # Step 5: Composite verdict
    # ------------------------------------------------------------------
    passed = True

    if not is_manifold:
        passed = False

    if printability_score < min_printability_score:
        passed = False
        if "Printability analysis failed" not in " ".join(errors):
            warnings.append(
                f"Printability score {printability_score} is below minimum "
                f"threshold of {min_printability_score}."
            )

    if fits_build_volume is False:
        passed = False

    # Any hard errors from validation also fail the pipeline
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
        summary="",
    )
    result.summary = _build_summary(result)

    logger.info("Pipeline complete — %s", "PASSED" if passed else "FAILED")
    return result
