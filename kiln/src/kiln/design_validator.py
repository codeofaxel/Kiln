"""Design validation pipeline — check generated STL against design constraints.

Validates a generated model *after* creation against the DesignBrief
constraints that were established *before* generation.  Catches geometry
that violates material limits, exceeds build volume, or creates
stability risks.

The pipeline:
    1. Get a DesignBrief from design intelligence (material, patterns, rules)
    2. Run printability analysis on the mesh (existing engine)
    3. Compare geometry against DesignBrief constraints (wall thickness,
       overhang angles, stability, bridges, adhesion)
    4. Return a structured report with pass/fail per check, severity,
       and actionable fix descriptions

Public API:
    validate_design          — full validation pipeline
    validation_to_feedback   — convert failures to generation feedback
    DesignValidationCheck    — single check result dataclass
    DesignValidationReport   — full report dataclass
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DesignValidationCheck:
    """Result of a single validation check."""

    check_name: str
    passed: bool
    severity: str  # "critical", "warning", "info"
    actual_value: float | str
    required_value: float | str
    fix_suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "passed": self.passed,
            "severity": self.severity,
            "actual_value": self.actual_value,
            "required_value": self.required_value,
            "fix_suggestion": self.fix_suggestion,
        }


@dataclass
class DesignValidationReport:
    """Full design validation report."""

    file_path: str
    requirements_text: str
    material: str | None
    overall_pass: bool
    checks: list[DesignValidationCheck] = field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "requirements_text": self.requirements_text,
            "material": self.material,
            "overall_pass": self.overall_pass,
            "checks": [c.to_dict() for c in self.checks],
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Internal check functions
# ---------------------------------------------------------------------------


def _check_wall_thickness(
    min_wall_mm: float,
    required_mm: float,
    *,
    material_name: str = "",
) -> DesignValidationCheck:
    """Check wall thickness against the design brief minimum."""
    passed = min_wall_mm >= required_mm
    material_clause = f" for {material_name}" if material_name else ""

    if passed:
        severity = "info"
        fix = ""
    elif min_wall_mm < required_mm * 0.5:
        severity = "critical"
        fix = (
            f"Increase wall thickness to at least {required_mm:.1f} mm"
            f"{material_clause}. Thinnest point is {min_wall_mm:.2f} mm "
            f"— thicken thin regions or increase the wall count in the slicer."
        )
    else:
        severity = "warning"
        fix = (
            f"Increase wall thickness to at least {required_mm:.1f} mm. "
            f"Current minimum is {min_wall_mm:.2f} mm{material_clause}."
        )

    return DesignValidationCheck(
        check_name="wall_thickness",
        passed=passed,
        severity=severity,
        actual_value=round(min_wall_mm, 2),
        required_value=required_mm,
        fix_suggestion=fix,
    )


def _check_overhang_angle(
    max_overhang_deg: float,
    limit_deg: float,
    *,
    material_name: str = "",
) -> DesignValidationCheck:
    """Check overhang angle against the material's unsupported limit.

    The printability engine reports ``max_overhang_angle`` in the usual
    FDM convention: degrees from vertical, where 0 is a vertical wall and
    90 is a horizontal ceiling. Higher values are more difficult to print.
    """
    # No overhangs detected = pass.
    if max_overhang_deg == 0:
        return DesignValidationCheck(
            check_name="overhang",
            passed=True,
            severity="info",
            actual_value=0,
            required_value=limit_deg,
            fix_suggestion="",
        )

    passed = max_overhang_deg <= limit_deg
    material_clause = f" for {material_name}" if material_name else ""

    if passed:
        severity = "info"
        fix = ""
    else:
        severity = "warning"
        fix = (
            f"Re-orient the model to reduce overhangs below "
            f"{limit_deg:.0f} degrees{material_clause}, or enable "
            f"supports for steep regions."
        )

    return DesignValidationCheck(
        check_name="overhang",
        passed=passed,
        severity=severity,
        actual_value=round(max_overhang_deg, 1),
        required_value=limit_deg,
        fix_suggestion=fix,
    )


def _check_stability(
    model_dims: dict[str, float],
    *,
    max_ratio: float = 4.0,
) -> DesignValidationCheck:
    """Check aspect ratio for stability (tall narrow parts tip over)."""
    width = model_dims.get("width", 0.0)
    depth = model_dims.get("depth", 0.0)
    height = model_dims.get("height", 0.0)

    # Degenerate geometry — can't compute ratio.
    if height <= 0 or (width <= 0 and depth <= 0):
        return DesignValidationCheck(
            check_name="stability",
            passed=True,
            severity="info",
            actual_value=0,
            required_value=max_ratio,
            fix_suggestion="",
        )

    base = min(width, depth) if min(width, depth) > 0 else max(width, depth)
    if base <= 0:
        return DesignValidationCheck(
            check_name="stability",
            passed=True,
            severity="info",
            actual_value=0,
            required_value=max_ratio,
            fix_suggestion="",
        )

    ratio = height / base
    passed = ratio <= max_ratio

    if passed:
        severity = "info"
        fix = ""
    elif ratio > max_ratio * 2:
        severity = "critical"
        fix = (
            f"Model is extremely tall and narrow ({ratio:.1f}:1). "
            f"Widen the base to at least {height / max_ratio:.1f} mm, "
            f"add a brim, or re-orient to reduce the ratio below "
            f"{max_ratio:.0f}:1."
        )
    else:
        severity = "warning"
        fix = (
            f"Height-to-base ratio is {ratio:.1f}:1 (max {max_ratio:.0f}:1). "
            f"Widen the base to at least {height / max_ratio:.1f} mm or "
            f"add a brim in the slicer."
        )

    return DesignValidationCheck(
        check_name="stability",
        passed=passed,
        severity=severity,
        actual_value=round(ratio, 1),
        required_value=max_ratio,
        fix_suggestion=fix,
    )


def _check_bridge_length(
    max_bridge_mm: float,
    limit_mm: float,
    *,
    material_name: str = "",
) -> DesignValidationCheck:
    """Check bridge length against the material's max bridge limit."""
    passed = max_bridge_mm <= limit_mm
    material_clause = f" for {material_name}" if material_name else ""

    if passed:
        severity = "info"
        fix = ""
    else:
        severity = "warning"
        fix = (
            f"Longest bridge is {max_bridge_mm:.1f} mm, "
            f"maximum{material_clause} is {limit_mm:.0f} mm. "
            f"Reduce bridge spans or enable supports in the slicer."
        )

    return DesignValidationCheck(
        check_name="bridge_length",
        passed=passed,
        severity=severity,
        actual_value=round(max_bridge_mm, 1),
        required_value=limit_mm,
        fix_suggestion=fix,
    )


def _check_bed_adhesion(
    contact_percentage: float,
    *,
    min_contact_pct: float = 15.0,
) -> DesignValidationCheck:
    """Check bed adhesion by contact area percentage."""
    passed = contact_percentage >= min_contact_pct

    if passed:
        severity = "info"
        fix = ""
    elif contact_percentage < 5.0:
        severity = "warning"
        fix = (
            f"Very low bed contact ({contact_percentage:.1f}%). "
            f"Add a flat base, use a brim or raft, or re-orient to "
            f"increase the contact surface above {min_contact_pct:.0f}%."
        )
    else:
        severity = "warning"
        fix = (
            f"Low bed contact ({contact_percentage:.1f}%). "
            f"Consider adding a brim or re-orienting the model."
        )

    return DesignValidationCheck(
        check_name="bed_adhesion",
        passed=passed,
        severity=severity,
        actual_value=round(contact_percentage, 1),
        required_value=min_contact_pct,
        fix_suggestion=fix,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_design(
    file_path: str,
    requirements_text: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
    build_volume: tuple[float, float, float] | None = None,
) -> DesignValidationReport:
    """Validate a generated model against design intelligence constraints.

    :param file_path: Path to the STL or OBJ file.
    :param requirements_text: Natural language requirements (same text
        used for ``get_design_brief``).
    :param material: Optional material override (e.g. ``"petg"``).
    :param printer_model: Optional printer model for capability lookup.
    :param build_volume: Optional (X, Y, Z) build volume in mm.
    :returns: A :class:`DesignValidationReport`.
    :raises ValueError: If the file cannot be parsed.
    """
    from kiln.design_intelligence import get_design_constraints
    from kiln.printability import _parse_mesh, analyze_printability

    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    # 1. Get DesignBrief from design intelligence.
    brief = get_design_constraints(
        requirements_text,
        material=material,
        printer_model=printer_model,
    )

    # 2. Run printability analysis.
    report = analyze_printability(file_path, build_volume=build_volume)
    report_dict = report.to_dict()

    overhangs = report_dict.get("overhangs", {})
    thin_walls = report_dict.get("thin_walls", {})
    bridging_data = report_dict.get("bridging", {})
    adhesion_data = report_dict.get("bed_adhesion", {})

    # 3. Extract model dimensions.
    _, vertices = _parse_mesh(file_path)
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    model_dims = {
        "width": max(xs) - min(xs),
        "depth": max(ys) - min(ys),
        "height": max(zs) - min(zs),
    }

    rules = brief.combined_rules
    rec_material = brief.recommended_material
    material_name = ""
    material_limits: dict[str, Any] = {}

    if rec_material and rec_material.material:
        material_name = rec_material.material.display_name
        material_limits = rec_material.material.design_limits

    resolved_material_id: str | None = None
    if rec_material and rec_material.material:
        resolved_material_id = rec_material.material.material_id

    # 4. Run checks.
    checks: list[DesignValidationCheck] = []

    # Wall thickness — only check if thin walls were actually detected.
    # The printability engine returns nozzle_diameter as min_wall when
    # no thin walls exist, which would be a false positive.
    min_wall_required = _resolve_wall_thickness(rules, material_limits)
    thin_wall_count = thin_walls.get("thin_wall_count", 0)
    if min_wall_required is not None and thin_wall_count > 0:
        actual_min_wall = thin_walls.get("min_wall_thickness_mm", float("inf"))
        checks.append(
            _check_wall_thickness(actual_min_wall, min_wall_required, material_name=material_name)
        )
    elif min_wall_required is not None:
        # No thin walls detected — pass the check with a reasonable value.
        checks.append(
            _check_wall_thickness(min_wall_required, min_wall_required, material_name=material_name)
        )

    # Overhang angle
    overhang_limit = material_limits.get("max_unsupported_overhang_deg")
    if overhang_limit is not None:
        actual_overhang = overhangs.get("max_overhang_angle", 0)
        checks.append(
            _check_overhang_angle(actual_overhang, overhang_limit, material_name=material_name)
        )

    # Stability (aspect ratio)
    checks.append(_check_stability(model_dims))

    # Bridge length
    bridge_limit = material_limits.get("max_bridge_length_mm")
    if bridge_limit is not None:
        actual_bridge = bridging_data.get("max_bridge_length_mm", 0)
        checks.append(
            _check_bridge_length(actual_bridge, bridge_limit, material_name=material_name)
        )

    # Bed adhesion
    contact_pct = adhesion_data.get("contact_percentage", 100.0)
    checks.append(_check_bed_adhesion(contact_pct))

    # 5. Aggregate results.
    failed_checks = [c for c in checks if not c.passed]
    critical_count = sum(1 for c in failed_checks if c.severity == "critical")
    warning_count = sum(1 for c in failed_checks if c.severity == "warning")
    overall_pass = critical_count == 0

    summary = _build_summary(checks, failed_checks, material_name)

    return DesignValidationReport(
        file_path=file_path,
        requirements_text=requirements_text,
        material=resolved_material_id,
        overall_pass=overall_pass,
        checks=checks,
        critical_count=critical_count,
        warning_count=warning_count,
        summary=summary,
    )


def validation_to_feedback(
    report: DesignValidationReport,
    original_prompt: str,
) -> list[Any]:
    """Convert validation failures into generation feedback for iterative improvement.

    Maps each failed check to a
    :class:`~kiln.generation_feedback.PrintFeedback` item with specific
    constraints that can be appended to the generation prompt.

    :param report: A :class:`DesignValidationReport` from :func:`validate_design`.
    :param original_prompt: The original generation prompt.
    :returns: List of :class:`~kiln.generation_feedback.PrintFeedback` items.
    """
    from kiln.generation_feedback import FeedbackType, PrintFeedback

    feedback_items: list[PrintFeedback] = []
    failed = [c for c in report.checks if not c.passed]

    if not failed:
        return feedback_items

    # Group failed checks by feedback type.
    printability_issues: list[str] = []
    printability_constraints: list[str] = []
    structural_issues: list[str] = []
    structural_constraints: list[str] = []
    dimensional_issues: list[str] = []
    dimensional_constraints: list[str] = []

    for check in failed:
        name = check.check_name

        if name == "wall_thickness":
            printability_issues.append(
                f"Thin walls ({check.actual_value} mm, need {check.required_value} mm)"
            )
            printability_constraints.append(
                f"minimum wall thickness {check.required_value} mm"
            )

        elif name == "overhang":
            printability_issues.append(
                f"Steep overhangs ({check.actual_value} deg, max {check.required_value} deg)"
            )
            printability_constraints.append(
                f"no overhangs greater than {check.required_value} degrees"
            )

        elif name == "bridge_length":
            printability_issues.append(
                f"Long bridges ({check.actual_value} mm, max {check.required_value} mm)"
            )
            printability_constraints.append(
                f"minimize bridges, no spans greater than {check.required_value} mm"
            )

        elif name == "stability":
            structural_issues.append(
                f"Unstable aspect ratio ({check.actual_value}:1, max {check.required_value}:1)"
            )
            structural_constraints.append(
                "wide flat base for stability, low center of gravity"
            )

        elif name == "bed_adhesion":
            structural_issues.append(
                f"Low bed contact ({check.actual_value}% area)"
            )
            structural_constraints.append(
                "flat bottom surface for bed adhesion"
            )

    if printability_issues:
        severity = "critical" if any(
            c.severity == "critical" for c in failed if c.check_name in ("wall_thickness", "overhang", "bridge_length")
        ) else "moderate"
        feedback_items.append(
            PrintFeedback(
                original_prompt=original_prompt,
                feedback_type=FeedbackType.PRINTABILITY,
                issues=printability_issues,
                constraints=printability_constraints,
                severity=severity,
            )
        )

    if structural_issues:
        severity = "critical" if any(
            c.severity == "critical" for c in failed if c.check_name in ("stability", "bed_adhesion")
        ) else "moderate"
        feedback_items.append(
            PrintFeedback(
                original_prompt=original_prompt,
                feedback_type=FeedbackType.STRUCTURAL,
                issues=structural_issues,
                constraints=structural_constraints,
                severity=severity,
            )
        )

    if dimensional_issues:
        feedback_items.append(
            PrintFeedback(
                original_prompt=original_prompt,
                feedback_type=FeedbackType.DIMENSIONAL,
                issues=dimensional_issues,
                constraints=dimensional_constraints,
                severity="critical",
            )
        )

    return feedback_items


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_wall_thickness(
    rules: dict[str, Any],
    material_limits: dict[str, Any],
) -> float | None:
    """Resolve the effective minimum wall thickness.

    Takes the stricter (larger) of the brief's combined rule and the
    material's own design limit.
    """
    brief_wall = rules.get("min_wall_thickness_mm")
    material_wall = material_limits.get("min_wall_thickness_mm")
    material_wall_from_rules = rules.get("material_min_wall_thickness_mm")

    candidates = [
        v for v in (brief_wall, material_wall, material_wall_from_rules) if v is not None
    ]
    if not candidates:
        return None

    return max(candidates)


def _build_summary(
    all_checks: list[DesignValidationCheck],
    failed_checks: list[DesignValidationCheck],
    material_name: str,
) -> str:
    """Build a human-readable summary string."""
    total = len(all_checks)
    passed = total - len(failed_checks)

    if not failed_checks:
        mat_clause = f" for {material_name}" if material_name else ""
        return f"All {total} checks passed{mat_clause}. Model is ready for printing."

    critical = [c for c in failed_checks if c.severity == "critical"]
    warnings = [c for c in failed_checks if c.severity == "warning"]

    parts: list[str] = [f"{passed}/{total} checks passed."]

    if critical:
        names = ", ".join(c.check_name for c in critical)
        parts.append(f"{len(critical)} critical issue(s): {names}.")
    if warnings:
        names = ", ".join(c.check_name for c in warnings)
        parts.append(f"{len(warnings)} warning(s): {names}.")

    return " ".join(parts)
