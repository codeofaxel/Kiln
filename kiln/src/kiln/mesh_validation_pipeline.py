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

    # Summary
    summary: str = ""  # human-readable one-paragraph summary

    # Inspection bundle (when provided) — source-of-truth manifest whose
    # printability findings drove the score.  Consumers can read all
    # channel evidence (PNG paths, raw measurements, view-selection
    # reasons) from here without re-running anything.  None on the legacy
    # path; populated when the caller passed a bundle in.
    inspection_bundle: dict[str, Any] | None = None

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
    inspection_bundle: dict[str, Any] | None = None,
    printer_id: str | None = None,
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
    :param inspection_bundle: Optional pre-built inspection-bundle dict
        (the ``result["inspection_bundle"]`` field produced by
        ``attach_inspect_bundle`` in kiln-pro).  When the bundle carries
        printability findings, the pipeline reads them instead of
        running a redundant :func:`analyze_printability` pass — same
        answer, half the cost.  Legacy callers (no bundle) get the
        unchanged path.
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

        from kiln.printability import (
            _apply_placement_check,
            _placement_faults,
            _resolve_placement_volume,
        )

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

        # Prefer pre-computed bundle findings over a fresh re-run.  The
        # bundle is the bundle-as-lingua-franca contract: producers emit
        # it once, consumers read it instead of re-deriving.  Same answer,
        # half the cost when the caller already ran inspection upstream.
        bundle_printability: dict[str, Any] | None = None
        if inspection_bundle is not None:
            bundle_printability = (
                inspection_bundle.get("channels", {})
                .get("printability", {})
                .get("findings")
            )

        if (
            bundle_printability
            and bundle_printability.get("score") is not None
        ):
            printability_score = bundle_printability["score"]
            printability_grade = bundle_printability.get("grade", "F")
            printability_details = dict(bundle_printability)
            recommendations.extend(
                bundle_printability.get("recommendations", [])
            )
            _logger.info(
                "Step 3/4: Printability read from inspection bundle — "
                "score %d/100 (grade %s); analyze_printability re-run skipped",
                printability_score,
                printability_grade,
            )

            # The bundle's findings describe the mesh's SHAPE; they carry
            # no verdict on where the part sits.  Skipping the
            # analyze_printability re-run therefore also skipped the
            # placement check that runs at the end of it, and a part
            # hanging below the bed or wider than the machine came back
            # graded on shape alone.  Run the one shared helper here so
            # this path lands on the same verdict as the direct one.
            (
                printability_score,
                printability_grade,
                placement_printable,
                placement_recs,
            ) = _apply_placement_check(
                printability_score,
                recommendations,
                bounding_box,
                build_volume=placement_volume,
            )

            if placement_recs:
                # Keep the ride-along findings coherent with the verdict
                # the pipeline is actually reporting.  This is the local
                # copy, so the caller's bundle is left untouched.
                printability_details["score"] = printability_score
                printability_details["grade"] = printability_grade
                printability_details["printable"] = placement_printable
                printability_details["recommendations"] = [
                    *placement_recs,
                    *printability_details.get("recommendations", []),
                ]
                _logger.warning(
                    "Placement check overrode bundle findings — "
                    "score %d/100 (grade %s): %s",
                    printability_score,
                    printability_grade,
                    "; ".join(placement_recs),
                )
        else:
            try:
                from kiln.printability import analyze_printability

                report = analyze_printability(
                    working_path,
                    nozzle_diameter=nozzle_diameter,
                    layer_height=layer_height,
                    material=material.lower(),
                    # Same bed the bundle branch checks against, so the
                    # two branches cannot reach different verdicts.
                    build_volume=placement_volume,
                )
                printability_score = report.score
                printability_grade = report.grade
                printability_details = report.to_dict()
                recommendations.extend(report.recommendations)
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

        # Whichever branch produced the score above, the geometry is the
        # same — so read the faults once, from the mesh, rather than from
        # whichever path happened to run.  Pure detection: it re-reads the
        # verdict without deducting a second time.
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

        if printability_score < min_printability_score:
            passed = False
            if "Printability analysis failed" not in " ".join(errors):
                warnings.append(
                    f"Printability score {printability_score} is below minimum "
                    f"threshold of {min_printability_score}.",
                )

        if fits_build_volume is False:
            passed = False

        # Placement is feasibility, not quality, so it is deliberately not
        # filtered through min_printability_score — that setting says how
        # rough a mesh the caller will accept, not whether they will take
        # one that cannot print at all.  This is the same reasoning that
        # already lets fits_build_volume fail a run on its own; without it
        # the two placement faults were graded alike but gated differently.
        if placement_faults:
            passed = False
            warnings.extend(placement_faults)

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
            inspection_bundle=inspection_bundle,
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
