"""Generation feedback loop -- failed print to improved prompt.

When a print fails or a generated model has issues, this module
constructs an improved generation prompt that addresses the specific
problems. Closes the loop between physical reality and AI generation.

The feedback types:
- PRINTABILITY: Model has overhangs/thin walls -> add constraints
- DIMENSIONAL: Model too large/small -> specify dimensions
- STRUCTURAL: Model failed during printing -> add strength requirements
- AESTHETIC: Poor surface quality -> adjust style/detail
- MATERIAL: Material-specific issues -> add material constraints
"""

from __future__ import annotations

import enum
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default maximum prompt length (Meshy API limit).  Use
# ``get_provider_prompt_limit()`` for provider-aware limits.
_MAX_PROMPT_LENGTH = 600

# Per-provider prompt length limits.  Providers with larger budgets
# allow richer design-intelligence constraints to be injected.
_PROVIDER_PROMPT_LIMITS: dict[str, int] = {
    "meshy": 600,
    "gemini": 10_000,
    "tripo3d": 5_000,
    "stability": 5_000,
    "openscad": 100_000,
}


def get_provider_prompt_limit(provider: str | None = None) -> int:
    """Return the maximum prompt length for a provider.

    :param provider: Provider name (e.g. ``"meshy"``).  Returns the
        default (600) when ``None``.
    :returns: Maximum prompt length in characters.
    """
    if provider is None:
        return _MAX_PROMPT_LENGTH
    return _PROVIDER_PROMPT_LIMITS.get(provider, _MAX_PROMPT_LENGTH)


def _dimensions_from_bbox(bbox: dict[str, Any]) -> dict[str, float] | None:
    """Derive width/depth/height dimensions from a bounding-box-like dict."""
    if not isinstance(bbox, dict):
        return None

    if {"width", "depth", "height"} <= set(bbox):
        return {
            "width": float(bbox["width"]),
            "depth": float(bbox["depth"]),
            "height": float(bbox["height"]),
        }

    if {"x", "y", "z"} <= set(bbox):
        return {
            "width": float(bbox["x"]),
            "depth": float(bbox["y"]),
            "height": float(bbox["z"]),
        }

    if {"x_min", "x_max", "y_min", "y_max", "z_min", "z_max"} <= set(bbox):
        return {
            "width": float(bbox["x_max"]) - float(bbox["x_min"]),
            "depth": float(bbox["y_max"]) - float(bbox["y_min"]),
            "height": float(bbox["z_max"]) - float(bbox["z_min"]),
        }

    return None


def _coerce_build_volume(build_volume: Any) -> dict[str, float] | None:
    """Normalize build-volume inputs to an ``x/y/z`` dict."""
    if isinstance(build_volume, dict):
        if {"x", "y", "z"} <= set(build_volume):
            return {
                "x": float(build_volume["x"]),
                "y": float(build_volume["y"]),
                "z": float(build_volume["z"]),
            }
        if {"width", "depth", "height"} <= set(build_volume):
            return {
                "x": float(build_volume["width"]),
                "y": float(build_volume["depth"]),
                "z": float(build_volume["height"]),
            }
        return None

    if isinstance(build_volume, (list, tuple)) and len(build_volume) == 3:
        return {
            "x": float(build_volume[0]),
            "y": float(build_volume[1]),
            "z": float(build_volume[2]),
        }

    return None


def _normalize_feedback_report(report: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten real Kiln analysis reports into feedback-friendly keys."""
    if not report:
        return {}

    normalized: dict[str, Any] = {}

    def merge(data: Any) -> None:
        if not isinstance(data, dict):
            return

        for nested_key in (
            "report",
            "validation",
            "mesh_validation",
            "diagnostics",
            "mesh_diagnostics",
        ):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                merge(nested)

        overhangs = data.get("overhangs")
        if isinstance(overhangs, dict):
            angle = overhangs.get("max_overhang_angle")
            if angle is not None:
                normalized["max_overhang_angle"] = float(angle)

        thin_walls = data.get("thin_walls")
        if isinstance(thin_walls, dict):
            min_wall = thin_walls.get("min_wall_thickness_mm")
            if min_wall is not None:
                normalized["min_wall_thickness"] = float(min_wall)

        bridging = data.get("bridging")
        if isinstance(bridging, dict):
            bridge_count = int(bridging.get("bridge_count", 0) or 0)
            if bridge_count > 0 or bridging.get("needs_supports_for_bridges"):
                normalized["has_bridges"] = True

        adhesion = data.get("bed_adhesion")
        if isinstance(adhesion, dict):
            contact_pct = adhesion.get("contact_percentage")
            if contact_pct is not None:
                normalized["bed_contact_percentage"] = float(contact_pct)

        dims = data.get("dimensions")
        if isinstance(dims, dict) and {"width", "depth", "height"} <= set(dims):
            normalized["dimensions"] = {
                "width": float(dims["width"]),
                "depth": float(dims["depth"]),
                "height": float(dims["height"]),
            }

        dims_mm = data.get("dimensions_mm")
        if isinstance(dims_mm, dict):
            if {"x", "y", "z"} <= set(dims_mm):
                normalized["dimensions"] = {
                    "width": float(dims_mm["x"]),
                    "depth": float(dims_mm["y"]),
                    "height": float(dims_mm["z"]),
                }
            elif {"width_mm", "depth_mm", "height_mm"} <= set(dims_mm):
                normalized["dimensions"] = {
                    "width": float(dims_mm["width_mm"]),
                    "depth": float(dims_mm["depth_mm"]),
                    "height": float(dims_mm["height_mm"]),
                }

        bbox = data.get("bounding_box")
        bbox_dims = _dimensions_from_bbox(bbox) if isinstance(bbox, dict) else None
        if bbox_dims:
            normalized.setdefault("dimensions", bbox_dims)

        build_volume = _coerce_build_volume(data.get("build_volume"))
        if build_volume:
            normalized["build_volume"] = build_volume

        if data.get("has_floating_fragments") or int(data.get("component_count", 1) or 1) > 1:
            normalized["has_floating_parts"] = True

        if "is_manifold" in data and not bool(data.get("is_manifold")):
            normalized["non_manifold"] = True
        if "is_watertight" in data and not bool(data.get("is_watertight")):
            normalized["non_manifold"] = True
        if int(data.get("hole_count", 0) or 0) > 0:
            normalized["non_manifold"] = True

        if "max_overhang_angle" in data:
            normalized["max_overhang_angle"] = float(data["max_overhang_angle"])
        if "min_wall_thickness" in data:
            normalized["min_wall_thickness"] = float(data["min_wall_thickness"])
        if "min_wall_thickness_mm" in data:
            normalized["min_wall_thickness"] = float(data["min_wall_thickness_mm"])
        if data.get("has_bridges"):
            normalized["has_bridges"] = True
        if data.get("has_floating_parts"):
            normalized["has_floating_parts"] = True
        if data.get("non_manifold"):
            normalized["non_manifold"] = True

    merge(report)
    return normalized


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FeedbackType(enum.Enum):
    """Types of generation feedback."""

    PRINTABILITY = "printability"
    DIMENSIONAL = "dimensional"
    STRUCTURAL = "structural"
    AESTHETIC = "aesthetic"
    MATERIAL = "material"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PrintFeedback:
    """A single piece of feedback about a generated model."""

    original_prompt: str
    feedback_type: FeedbackType
    issues: list[str]
    constraints: list[str]  # specific constraints to add to the prompt
    severity: str  # "minor", "moderate", "critical"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["feedback_type"] = self.feedback_type.value
        return data


@dataclass
class ImprovedPrompt:
    """An improved prompt with feedback constraints applied."""

    original_prompt: str
    improved_prompt: str
    feedback_applied: list[PrintFeedback]
    constraints_added: list[str]
    iteration: int  # which retry attempt this is
    expected_improvements: list[str]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["feedback_applied"] = [f.to_dict() for f in self.feedback_applied]
        return data


@dataclass
class FeedbackLoop:
    """Tracks iterative improvement of a generated model."""

    model_id: str
    original_prompt: str
    iterations: list[dict[str, Any]]  # [{prompt, issues, outcome}]
    current_iteration: int
    resolved: bool
    best_iteration: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Constraint generators
# ---------------------------------------------------------------------------

_PRINTABILITY_CONSTRAINTS: dict[str, str] = {
    "overhang": "flat bottom, no overhangs greater than 45 degrees",
    "thin_wall": "minimum wall thickness of 2mm",
    "bridge": "simple geometry, minimize bridges",
    "island": "single continuous body, no floating parts",
    "non_manifold": "solid watertight mesh, no holes or gaps",
}

_STRUCTURAL_CONSTRAINTS: dict[str, str] = {
    "weak_base": "wide flat base for bed adhesion",
    "fragile": "minimum 3mm thickness on structural elements",
    "top_heavy": "low center of gravity, stable base",
    "thin_neck": "no thin connection points, minimum 4mm diameter",
}

_DIMENSIONAL_CONSTRAINTS: dict[str, str] = {
    "too_large": "maximum dimensions {max_x} x {max_y} x {max_z} mm",
    "too_small": "minimum dimensions 20 x 20 x 10 mm",
    "wrong_scale": "real-world scale, approximately {target_size}",
}


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def analyze_for_feedback(
    file_path: str,
    *,
    original_prompt: str,
    failure_mode: str | None = None,
    printability_report: dict[str, Any] | None = None,
) -> list[PrintFeedback]:
    """Analyze a model and print outcome to identify improvement areas.

    :param file_path: Path to the model file.
    :param original_prompt: The original generation prompt.
    :param failure_mode: Optional failure mode string (e.g. ``"adhesion"``,
        ``"spaghetti"``).
    :param printability_report: Optional dict with printability analysis
        results (e.g. overhang angles, thin wall counts, dimensions).
    :returns: List of :class:`PrintFeedback` items.
    """
    feedback_items: list[PrintFeedback] = []
    report = _normalize_feedback_report(printability_report)

    # --- Printability checks ---
    printability_issues: list[str] = []
    printability_constraints: list[str] = []

    max_overhang = report.get("max_overhang_angle", 0)
    if max_overhang > 45:
        printability_issues.append(f"Overhangs detected ({max_overhang} degrees)")
        printability_constraints.append(_PRINTABILITY_CONSTRAINTS["overhang"])

    min_wall = report.get("min_wall_thickness", float("inf"))
    if min_wall < 2.0:
        printability_issues.append(f"Thin walls detected ({min_wall:.1f}mm)")
        printability_constraints.append(_PRINTABILITY_CONSTRAINTS["thin_wall"])

    if report.get("has_bridges"):
        printability_issues.append("Bridges detected")
        printability_constraints.append(_PRINTABILITY_CONSTRAINTS["bridge"])

    if report.get("has_floating_parts"):
        printability_issues.append("Floating/disconnected parts detected")
        printability_constraints.append(_PRINTABILITY_CONSTRAINTS["island"])

    if report.get("non_manifold"):
        printability_issues.append("Non-manifold geometry detected")
        printability_constraints.append(_PRINTABILITY_CONSTRAINTS["non_manifold"])

    if printability_issues:
        severity = "critical" if max_overhang > 70 or min_wall < 1.0 else "moderate"
        feedback_items.append(
            PrintFeedback(
                original_prompt=original_prompt,
                feedback_type=FeedbackType.PRINTABILITY,
                issues=printability_issues,
                constraints=printability_constraints,
                severity=severity,
            )
        )

    contact_pct = report.get("bed_contact_percentage")
    if contact_pct is not None and contact_pct < 15.0:
        feedback_items.append(
            PrintFeedback(
                original_prompt=original_prompt,
                feedback_type=FeedbackType.STRUCTURAL,
                issues=[f"Low bed contact area ({contact_pct:.1f}%)"],
                constraints=[_STRUCTURAL_CONSTRAINTS["weak_base"]],
                severity="moderate" if contact_pct >= 5.0 else "critical",
            )
        )

    # --- Dimensional checks ---
    dimensions = report.get("dimensions", {})
    if dimensions:
        dim_issues: list[str] = []
        dim_constraints: list[str] = []
        max_dim = max(
            dimensions.get("width", 0),
            dimensions.get("depth", 0),
            dimensions.get("height", 0),
        )
        build_volume = report.get("build_volume", {})

        if build_volume:
            bv_x = build_volume.get("x", 250)
            bv_y = build_volume.get("y", 210)
            bv_z = build_volume.get("z", 210)
            if (
                dimensions.get("width", 0) > bv_x
                or dimensions.get("depth", 0) > bv_y
                or dimensions.get("height", 0) > bv_z
            ):
                dim_issues.append("Model exceeds build volume")
                dim_constraints.append(f"maximum dimensions {bv_x} x {bv_y} x {bv_z} mm")
        elif max_dim > 250:
            dim_issues.append(f"Model may be too large ({max_dim:.0f}mm)")
            dim_constraints.append("maximum dimensions 200 x 200 x 200 mm")

        if max_dim < 5:
            dim_issues.append(f"Model may be too small ({max_dim:.1f}mm)")
            dim_constraints.append("minimum dimensions 20 x 20 x 10 mm")

        if dim_issues:
            feedback_items.append(
                PrintFeedback(
                    original_prompt=original_prompt,
                    feedback_type=FeedbackType.DIMENSIONAL,
                    issues=dim_issues,
                    constraints=dim_constraints,
                    severity="moderate",
                )
            )

    # --- Failure-mode based structural feedback ---
    if failure_mode:
        struct_issues: list[str] = []
        struct_constraints: list[str] = []

        fm_lower = failure_mode.lower()
        if fm_lower in ("adhesion", "adhesion_loss"):
            struct_issues.append("Part detached from bed during printing")
            struct_constraints.append(_STRUCTURAL_CONSTRAINTS["weak_base"])
        if fm_lower in ("spaghetti", "layer_shift"):
            struct_issues.append(f"Print failure mode: {failure_mode}")
            struct_constraints.append(_STRUCTURAL_CONSTRAINTS["fragile"])
            struct_constraints.append(_PRINTABILITY_CONSTRAINTS["overhang"])
        if fm_lower == "stringing":
            struct_issues.append("Excessive stringing between parts")
            struct_constraints.append(_PRINTABILITY_CONSTRAINTS["bridge"])
        if fm_lower in ("warping",):
            struct_issues.append("Part warped during printing")
            struct_constraints.append(_STRUCTURAL_CONSTRAINTS["weak_base"])

        if struct_issues:
            feedback_items.append(
                PrintFeedback(
                    original_prompt=original_prompt,
                    feedback_type=FeedbackType.STRUCTURAL,
                    issues=struct_issues,
                    constraints=struct_constraints,
                    severity="moderate" if fm_lower == "stringing" else "critical",
                )
            )

    # If no issues found, return empty list
    return feedback_items


def structural_risks_to_feedback(
    risks: list[Any],
    *,
    original_prompt: str,
    load_analysis: Any | None = None,
) -> list[PrintFeedback]:
    """Convert structural risk analysis into generation feedback.

    Maps :class:`~kiln.design_reasoning.StructuralRisk` items and an
    optional :class:`~kiln.design_reasoning.LoadAnalysis` into
    :class:`PrintFeedback` constraints that can refine a generation
    prompt on the next iteration.

    :param risks: List of ``StructuralRisk`` objects (or dicts with
        ``risk_type``, ``severity``, ``description``).
    :param original_prompt: The generation prompt being refined.
    :param load_analysis: Optional ``LoadAnalysis`` for orientation /
        layer-strength guidance.
    :returns: List of :class:`PrintFeedback` items.
    """
    if not risks and load_analysis is None:
        return []

    _RISK_CONSTRAINTS: dict[str, str] = {
        "thin_neck": "no thin connection points, minimum 4mm cross-section",
        "stress_concentration": "smooth transitions between sections, add fillets at joints",
        "cantilever": "minimize unsupported cantilevers, add gussets at overhanging joints",
        "sharp_corner": "rounded edges at concave corners to prevent crack initiation",
        "insufficient_base": "wide flat base for stability, low center of gravity",
        "weak_layer_adhesion": "orient load paths along print layers, not across them",
    }

    structural_issues: list[str] = []
    structural_constraints: list[str] = []
    has_critical = False

    for risk in risks:
        risk_type = risk.risk_type if hasattr(risk, "risk_type") else risk.get("risk_type", "")
        severity = risk.severity if hasattr(risk, "severity") else risk.get("severity", "warning")
        description = risk.description if hasattr(risk, "description") else risk.get("description", "")

        if severity == "critical":
            has_critical = True

        structural_issues.append(description or f"Structural risk: {risk_type}")

        constraint = _RISK_CONSTRAINTS.get(risk_type)
        if constraint and constraint not in structural_constraints:
            structural_constraints.append(constraint)

    # Load analysis adds orientation and layer-strength guidance.
    if load_analysis is not None:
        concern = (
            load_analysis.layer_direction_concern
            if hasattr(load_analysis, "layer_direction_concern")
            else (load_analysis.get("layer_direction_concern") if isinstance(load_analysis, dict) else "")
        )
        if concern:
            structural_issues.append(f"Layer direction concern: {concern}")

        rec_orient = (
            load_analysis.recommended_print_orientation
            if hasattr(load_analysis, "recommended_print_orientation")
            else (load_analysis.get("recommended_print_orientation") if isinstance(load_analysis, dict) else "")
        )
        if rec_orient:
            structural_constraints.append(
                f"design for {rec_orient} print orientation for maximum strength"
            )

    if not structural_issues:
        return []

    return [
        PrintFeedback(
            original_prompt=original_prompt,
            feedback_type=FeedbackType.STRUCTURAL,
            issues=structural_issues,
            constraints=structural_constraints,
            severity="critical" if has_critical else "moderate",
        )
    ]


def design_validation_to_feedback(
    report: Any,
    original_prompt: str,
) -> list[PrintFeedback]:
    """Convert a DesignValidationReport into feedback items.

    Delegates to :func:`~kiln.design_validator.validation_to_feedback`
    but accepts either a :class:`~kiln.design_validator.DesignValidationReport`
    or a raw dict, and always returns :class:`PrintFeedback` objects.
    """
    if hasattr(report, "checks"):
        from kiln.design_validator import validation_to_feedback

        return validation_to_feedback(report, original_prompt)

    # Dict form — rebuild minimal check objects.
    checks = report.get("checks", []) if isinstance(report, dict) else []
    failed = [c for c in checks if not c.get("passed", True)]
    if not failed:
        return []

    issues: list[str] = []
    constraints: list[str] = []
    for check in failed:
        fix = check.get("fix_suggestion", "")
        name = check.get("check_name", "unknown")
        actual = check.get("actual_value", "")
        required = check.get("required_value", "")
        issues.append(f"{name}: actual={actual}, required={required}")
        if fix:
            constraints.append(fix)

    return [
        PrintFeedback(
            original_prompt=original_prompt,
            feedback_type=FeedbackType.PRINTABILITY,
            issues=issues,
            constraints=constraints,
            severity="critical" if any(
                c.get("severity") == "critical" for c in failed
            ) else "moderate",
        )
    ]


def generate_improved_prompt(
    original_prompt: str,
    feedback: list[PrintFeedback],
    *,
    iteration: int = 1,
    provider: str | None = None,
    max_length: int | None = None,
) -> ImprovedPrompt:
    """Construct an improved prompt incorporating feedback constraints.

    Adds physical constraints to the end of the original prompt without
    modifying the creative intent. Keeps the total prompt under the
    provider-specific limit (or ``max_length`` if given explicitly).

    :param original_prompt: The original generation prompt.
    :param feedback: List of :class:`PrintFeedback` items to apply.
    :param iteration: Which retry iteration this is (default 1).
    :param provider: Provider name for prompt-length limit lookup.
    :param max_length: Explicit maximum prompt length (overrides provider lookup).
    :returns: An :class:`ImprovedPrompt` with the improved text.
    """
    limit = max_length if max_length is not None else get_provider_prompt_limit(provider)

    # Collect unique constraints from all feedback
    all_constraints: list[str] = []
    expected_improvements: list[str] = []

    for fb in feedback:
        for constraint in fb.constraints:
            if constraint not in all_constraints:
                all_constraints.append(constraint)
        for issue in fb.issues:
            expected_improvements.append(f"Fix: {issue}")

    # Build improved prompt
    if all_constraints:
        requirements = ". ".join(all_constraints)
        suffix = f" Requirements: {requirements}."
        # Trim original prompt if needed to fit within limit
        max_original_len = limit - len(suffix)
        if max_original_len < 20:
            suffix = f" Requirements: {'. '.join(all_constraints[:3])}."
            max_original_len = limit - len(suffix)

        trimmed_prompt = original_prompt[:max_original_len].rstrip()
        improved = trimmed_prompt + suffix
    else:
        improved = original_prompt

    # Final length enforcement
    if len(improved) > limit:
        improved = improved[: limit - 3] + "..."

    return ImprovedPrompt(
        original_prompt=original_prompt,
        improved_prompt=improved,
        feedback_applied=feedback,
        constraints_added=all_constraints,
        iteration=iteration,
        expected_improvements=expected_improvements,
    )


# ---------------------------------------------------------------------------
# Feedback loop persistence
# ---------------------------------------------------------------------------


def start_feedback_loop(model_id: str, original_prompt: str) -> FeedbackLoop:
    """Start a new feedback loop for a generated model.

    :param model_id: Unique identifier for the generated model.
    :param original_prompt: The original generation prompt.
    :returns: A new :class:`FeedbackLoop`.
    """
    from kiln.persistence import get_db

    loop = FeedbackLoop(
        model_id=model_id,
        original_prompt=original_prompt,
        iterations=[],
        current_iteration=0,
        resolved=False,
        best_iteration=None,
    )

    db = get_db()
    now = time.time()
    try:
        db.execute(
            """INSERT INTO feedback_loops
               (model_id, original_prompt, iterations, current_iteration,
                resolved, best_iteration, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                model_id,
                original_prompt,
                json.dumps([]),
                0,
                0,
                None,
                now,
                now,
            ),
        )
        db.commit()
    except Exception:
        logger.exception("Failed to save feedback loop (non-fatal)")

    return loop


def add_iteration(
    model_id: str,
    prompt: str,
    issues: list[str],
    outcome: str,
) -> FeedbackLoop:
    """Add an iteration to an existing feedback loop.

    :param model_id: The model ID of the feedback loop.
    :param prompt: The prompt used in this iteration.
    :param issues: List of issues found in this iteration.
    :param outcome: Outcome string (e.g. ``"success"``, ``"failed"``).
    :returns: Updated :class:`FeedbackLoop`.
    """
    from kiln.persistence import get_db

    db = get_db()
    loop = get_feedback_loop(model_id)

    if loop is None:
        loop = FeedbackLoop(
            model_id=model_id,
            original_prompt=prompt,
            iterations=[],
            current_iteration=0,
            resolved=False,
            best_iteration=None,
        )

    iteration_data = {
        "prompt": prompt,
        "issues": issues,
        "outcome": outcome,
        "timestamp": time.time(),
    }
    loop.iterations.append(iteration_data)
    loop.current_iteration = len(loop.iterations)

    if outcome == "success":
        loop.resolved = True
        loop.best_iteration = loop.current_iteration

    now = time.time()
    try:
        db.execute(
            """UPDATE feedback_loops
               SET iterations = ?, current_iteration = ?, resolved = ?,
                   best_iteration = ?, updated_at = ?
               WHERE model_id = ?""",
            (
                json.dumps(loop.iterations),
                loop.current_iteration,
                1 if loop.resolved else 0,
                loop.best_iteration,
                now,
                model_id,
            ),
        )
        db.commit()
    except Exception:
        logger.exception("Failed to update feedback loop (non-fatal)")

    return loop


@dataclass
class PrinterGenerationContext:
    """Live printer context for generation-aware prompt enrichment.

    Resolved from the actual printer state at generation time — loaded
    material, nozzle diameter, build volume, and common failure modes.
    """

    material: str | None = None
    material_source: str = ""  # "user", "ams", "spool", "default"
    nozzle_diameter_mm: float = 0.4
    build_volume_mm: dict[str, float] | None = None
    printer_model: str | None = None
    common_failures: list[str] | None = None  # e.g. ["adhesion", "stringing"]
    printer_notes: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_printer_generation_context(
    *,
    material: str | None = None,
    printer_name: str | None = None,
) -> PrinterGenerationContext:
    """Resolve live printer state for generation-aware prompt enrichment.

    When the agent doesn't specify a material, queries the printer's
    AMS or filament sensor to detect what's loaded.  Also resolves
    build volume, nozzle diameter, and common failure patterns from
    printer intelligence.

    :param material: Explicit material override.  When provided, skips
        auto-detection.
    :param printer_name: Printer to query.  ``None`` for the default.
    :returns: A :class:`PrinterGenerationContext` with resolved values.
    """
    ctx = PrinterGenerationContext()

    # Explicit material wins.
    if material:
        ctx.material = material
        ctx.material_source = "user"

    try:
        # Use the server's adapter resolution — handles both fleet registry
        # and single-printer setups.
        import kiln.server as _srv

        if printer_name:
            adapter = _srv._registry.get(printer_name)
        else:
            adapter = _srv._get_adapter()
    except Exception:
        logger.debug("No printer available for context resolution", exc_info=True)
        return ctx

    # Build volume from printer info.
    try:
        info = adapter.get_printer_info()
        bv = getattr(info, "build_volume", None)
        if isinstance(bv, dict) and bv:
            ctx.build_volume_mm = {
                "x": float(bv.get("x", 256)),
                "y": float(bv.get("y", 256)),
                "z": float(bv.get("z", 256)),
            }
        nozzle = getattr(info, "nozzle_diameter", None)
        if nozzle:
            ctx.nozzle_diameter_mm = float(nozzle)
        model = getattr(info, "model", None) or getattr(info, "printer_model", None)
        if model:
            ctx.printer_model = str(model)
    except Exception:
        logger.debug("Could not resolve printer info", exc_info=True)

    # Auto-detect material from AMS (Bambu) or spool manager.
    if not ctx.material:
        try:
            # Bambu AMS: get_ams_status() → units → trays → tray_type
            if hasattr(adapter, "get_ams_status"):
                ams = adapter.get_ams_status()
                if isinstance(ams, dict):
                    # Find the currently active tray's material type.
                    tray_now = ams.get("tray_now")
                    for unit in ams.get("units", []):
                        for tray in unit.get("trays", []):
                            slot = tray.get("slot")
                            tray_type = tray.get("tray_type", "")
                            if tray_type and (tray_now is None or str(slot) == str(tray_now)):
                                ctx.material = tray_type.lower()
                                ctx.material_source = "ams"
                                break
                        if ctx.material:
                            break
        except Exception:
            logger.debug("Could not auto-detect material from AMS", exc_info=True)

    # Printer intelligence — common failure modes and notes.
    if ctx.printer_model:
        try:
            from kiln.printer_intelligence import get_printer_intelligence

            intel = get_printer_intelligence(ctx.printer_model)
            if intel:
                failures = intel.get("common_failures", [])
                if failures:
                    ctx.common_failures = [f.get("symptom", "") for f in failures[:5] if f.get("symptom")]
                notes = intel.get("agent_notes", [])
                if notes:
                    ctx.printer_notes = notes[:3]
        except Exception:
            logger.debug("Printer intelligence unavailable", exc_info=True)

    return ctx


def enhance_prompt_with_design_intelligence(
    prompt: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
    provider: str | None = None,
    max_length: int | None = None,
    printer_context: PrinterGenerationContext | None = None,
) -> ImprovedPrompt:
    """Enhance a generation prompt with design intelligence constraints.

    Analyzes the prompt for functional requirements and appends relevant
    manufacturing constraints, material guidance, and design rules to
    produce a smarter generation request.

    This is called **before** generation (proactive), unlike
    :func:`analyze_for_feedback` which is called **after** (reactive).

    :param prompt: The original generation prompt.
    :param material: Optional material to constrain to.
    :param printer_model: Optional printer model for build-volume/capability
        constraints.
    :param provider: Provider name (e.g. ``"meshy"``, ``"openscad"``) for
        prompt-length limit lookup.  Ignored when *max_length* is given.
    :param max_length: Explicit maximum prompt length.  When ``None`` the
        limit is derived from *provider* via
        :func:`get_provider_prompt_limit`.
    :param printer_context: Optional :class:`PrinterGenerationContext`
        from :func:`resolve_printer_generation_context`.  When provided,
        auto-resolved material and printer-specific failure mitigations
        are included in the prompt.
    :returns: An :class:`ImprovedPrompt` with design constraints applied.
    """
    # Merge printer context into explicit parameters when available.
    if printer_context is not None:
        if not material and printer_context.material:
            material = printer_context.material
        if not printer_model and printer_context.printer_model:
            printer_model = printer_context.printer_model
    limit = max_length if max_length is not None else get_provider_prompt_limit(provider)

    try:
        from kiln.design_intelligence import (
            get_design_constraints,
            get_printer_design_profile,
        )

        brief = get_design_constraints(
            prompt,
            material=material,
            printer_model=printer_model,
        )
        printer_profile = (
            get_printer_design_profile(printer_model)
            if printer_model
            else None
        )
    except Exception:
        logger.debug("Design intelligence unavailable, returning original prompt", exc_info=True)
        return ImprovedPrompt(
            original_prompt=prompt,
            improved_prompt=prompt,
            feedback_applied=[],
            constraints_added=[],
            iteration=0,
            expected_improvements=[],
        )

    # Material design limits — use actual per-material values, not hardcoded
    rules = brief.combined_rules
    mat = brief.recommended_material
    mat_limits: dict[str, Any] = {}
    if mat and mat.material and hasattr(mat.material, "design_limits"):
        mat_limits = mat.material.design_limits or {}

    # -----------------------------------------------------------------
    # Core constraints (always included regardless of budget)
    # -----------------------------------------------------------------
    core_constraints: list[str] = []

    # Wall thickness — prefer material-specific recommended value
    rec_wall = mat_limits.get("recommended_wall_thickness_mm") or rules.get("min_wall_thickness_mm")
    if rec_wall:
        core_constraints.append(f"minimum wall thickness {rec_wall}mm")

    if rules.get("infill_min_pct"):
        core_constraints.append(f"solid infill, minimum {rules['infill_min_pct']}% density")
    if rules.get("gussets_required"):
        core_constraints.append("triangular gussets at load-bearing joints")
    if rules.get("fillets_required"):
        radius = rules.get("fillet_min_radius_mm", 1)
        core_constraints.append(f"rounded fillets (min {radius}mm radius) at corners and joints")

    # Material suitability
    if mat and mat.material:
        core_constraints.append(f"designed for {mat.material.display_name} material")

    # Printer build volume
    if printer_profile:
        build = printer_profile.build_volume_mm
        core_constraints.append(
            f"fit within {build['x']} x {build['y']} x {build['z']} mm build volume"
        )

    # Material-specific overhang/bridge limits
    overhang_limit = mat_limits.get("max_unsupported_overhang_deg", 50)
    core_constraints.append(f"no overhangs greater than {overhang_limit} degrees")

    bridge_limit = mat_limits.get("max_bridge_length_mm")
    if bridge_limit:
        core_constraints.append(f"minimize bridges, max {bridge_limit}mm unsupported spans")

    cantilever_limit = mat_limits.get("max_cantilever_length_mm")
    if cantilever_limit:
        core_constraints.append(f"max cantilever length {cantilever_limit}mm")

    # Pattern-specific constraints — include design rules, not just orientation
    for pattern in brief.applicable_patterns[:2]:
        if pattern.print_orientation:
            core_constraints.append(pattern.print_orientation_reason)
        # Include the most important pattern-specific rule
        if hasattr(pattern, "design_rules") and pattern.design_rules:
            for key in ("min_arm_thickness_mm", "min_wall_thickness_mm"):
                val = pattern.design_rules.get(key)
                if val:
                    core_constraints.append(
                        f"{pattern.display_name}: min {key.replace('_', ' ')} {val}"
                    )
                    break

    # Printer-specific failure mitigations — learned from this printer's
    # common failure patterns so the generated design avoids them.
    _FAILURE_MITIGATIONS: dict[str, str] = {
        "adhesion": "extra-wide base for bed adhesion, consider brim",
        "stringing": "minimize travel moves, avoid thin isolated features",
        "warping": "chamfered corners, avoid large flat surfaces",
        "layer_shift": "low center of gravity, avoid tall narrow geometry",
        "spaghetti": "no unsupported overhangs, solid geometry",
        "elephant_foot": "slight chamfer on bottom edges",
        "under_extrusion": "minimum wall thickness 1.2mm",
        "clogging": "avoid rapid retraction zones",
    }
    if printer_context is not None and printer_context.common_failures:
        for failure in printer_context.common_failures[:3]:
            mitigation = _FAILURE_MITIGATIONS.get(failure.lower())
            if mitigation and mitigation not in core_constraints:
                core_constraints.append(mitigation)

    # Printability fundamentals
    core_constraints.append("flat bottom for bed adhesion")
    core_constraints.append("single solid body, no floating parts")

    # Inject top combined_guidance strings (expert rules) when budget allows.
    if limit > 800 and brief.combined_guidance:
        for guidance in brief.combined_guidance[:3]:
            short = guidance[:120].rstrip(". ") if len(guidance) > 120 else guidance.rstrip(". ")
            if short not in core_constraints:
                core_constraints.append(short)

    # -----------------------------------------------------------------
    # Detailed constraints (included when prompt budget allows)
    # -----------------------------------------------------------------
    detailed_constraints: list[str] = []

    # From mat_limits — precision design limits
    min_hole = mat_limits.get("min_hole_diameter_mm")
    if min_hole:
        detailed_constraints.append(f"minimum hole diameter {min_hole}mm")

    min_pin = mat_limits.get("min_pin_diameter_mm")
    if min_pin:
        detailed_constraints.append(f"minimum pin diameter {min_pin}mm")

    snap_tol = mat_limits.get("snap_fit_tolerance_mm")
    if snap_tol:
        detailed_constraints.append(f"snap-fit clearance tolerance \u00b1{snap_tol}mm")

    press_fit = mat_limits.get("press_fit_interference_mm")
    if press_fit:
        detailed_constraints.append(f"press-fit interference {press_fit}mm")

    thread_pitch = mat_limits.get("thread_min_pitch_mm")
    if thread_pitch:
        detailed_constraints.append(f"minimum thread pitch {thread_pitch}mm for printable threads")

    # From MaterialProfile thermal/chemical properties
    if mat and mat.material:
        max_service = mat.material.thermal.get("max_service_temp_c")
        if max_service:
            detailed_constraints.append(f"designed for environments up to {max_service}\u00b0C")

        warping = mat.material.thermal.get("warping_tendency")
        if warping in ("high", "very_high") and (printer_profile is None or not printer_profile.has_enclosure):
            detailed_constraints.append(
                "design with warping mitigation: chamfered corners, "
                "avoid large flat surfaces, gradual geometry transitions"
            )

        uv_res = mat.material.chemical.get("uv_resistance")
        if uv_res in ("poor", "very_poor"):
            detailed_constraints.append("not suitable for prolonged outdoor/UV exposure")

        moisture = mat.material.chemical.get("moisture_absorption")
        if moisture == "high":
            detailed_constraints.append("avoid designs requiring long-term water contact")

    # From PrinterDesignProfile
    if printer_profile:
        typ_tol = printer_profile.typical_tolerance_mm
        if typ_tol:
            detailed_constraints.append(
                f"design tolerances for \u00b1{typ_tol}mm printer accuracy"
            )

        if not printer_profile.has_direct_drive:
            mat_lower = (material or "").lower()
            if mat_lower in ("tpu", "flexible", "tpe"):
                detailed_constraints.append(
                    "design for bowden extruder: avoid thin walls under 1.5mm, "
                    "minimize retractions"
                )

        if printer_profile.max_print_speed_mm_s > 300:
            detailed_constraints.append(
                "optimized for high-speed printing: reinforce thin features "
                "to resist vibration"
            )

        layer_heights = printer_profile.default_layer_heights_mm
        if layer_heights:
            mid = layer_heights[len(layer_heights) // 2]
            detailed_constraints.append(f"optimized for {mid}mm layer height")

    # -----------------------------------------------------------------
    # Combine based on budget
    # -----------------------------------------------------------------
    if limit > 2000:
        constraints = core_constraints + detailed_constraints
    elif limit > 800:
        constraints = core_constraints + detailed_constraints[:5]
    else:
        constraints = core_constraints

    if not constraints:
        return ImprovedPrompt(
            original_prompt=prompt,
            improved_prompt=prompt,
            feedback_applied=[],
            constraints_added=[],
            iteration=0,
            expected_improvements=[],
        )

    # Build the enhanced prompt — use more constraints when budget allows
    max_constraints = 8 if limit <= 600 else 15
    requirements = ". ".join(constraints[:max_constraints])
    suffix = f" Requirements: {requirements}."

    max_original = limit - len(suffix)
    if max_original < 20:
        suffix = f" Requirements: {'. '.join(constraints[:4])}."
        max_original = limit - len(suffix)

    trimmed = prompt[:max_original].rstrip()
    improved = trimmed + suffix

    if len(improved) > limit:
        improved = improved[: limit - 3] + "..."

    return ImprovedPrompt(
        original_prompt=prompt,
        improved_prompt=improved,
        feedback_applied=[],
        constraints_added=constraints,
        iteration=0,
        expected_improvements=[
            f"Design-aware generation with {len(constraints)} constraints applied",
        ],
    )


def build_parametric_generation_prompt(
    requirements: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
) -> ImprovedPrompt:
    """Build a prompt optimized for parametric OpenSCAD code generation.

    Uses the full design intelligence pipeline with ``provider="openscad"``
    (100K char limit) and wraps the result in OpenSCAD-specific
    instructions that guide the LLM to produce well-structured
    parametric code.

    :param requirements: Natural-language description of the desired part.
    :param material: Optional material to constrain to.
    :param printer_model: Optional printer model for build-volume constraints.
    :returns: An :class:`ImprovedPrompt` with OpenSCAD instructions prepended.
    """
    # Get design-intelligence-enhanced prompt
    inner = enhance_prompt_with_design_intelligence(
        requirements,
        material=material,
        printer_model=printer_model,
        provider="openscad",
    )

    # Check for matching library components
    matched_components: list = []
    try:
        from kiln.components import match_components

        matched_components = match_components(requirements)
    except Exception:
        logger.debug("Component matching unavailable", exc_info=True)

    # Build library rule and component section conditionally
    if matched_components:
        # Collect unique library imports
        libraries_used: set[str] = set()
        for m in matched_components:
            libraries_used.add(m.component.library)

        lib_rule = (
            "- You may use these bundled OpenSCAD libraries: "
            + ", ".join(sorted(libraries_used))
            + "\n"
        )

        # Build component reference section
        comp_lines = [
            "\nAVAILABLE COMPONENTS (use these instead of writing from scratch):"
        ]
        for m in matched_components[:5]:  # limit to top 5
            c = m.component
            comp_lines.append(f"\n## {c.display_name}")
            comp_lines.append(f"Import: {c.import_line}")
            comp_lines.append(f"Usage: {c.example_call}")
            if c.key_params:
                param_strs = []
                for pname, pinfo in c.key_params.items():
                    desc = pinfo.get("description", pname)
                    default = pinfo.get("default", "")
                    param_strs.append(f"  - {pname}: {desc} (default: {default})")
                comp_lines.append("Parameters:\n" + "\n".join(param_strs))
            if c.agent_guidance:
                comp_lines.append(f"Note: {c.agent_guidance}")
            if c.printability_notes:
                comp_lines.append(f"Printing: {c.printability_notes}")

        component_section = "\n".join(comp_lines)
    else:
        lib_rule = "- No external library dependencies (pure OpenSCAD)\n"
        component_section = ""

    # OpenSCAD instruction header
    header = (
        "Generate valid OpenSCAD code for the following design.\n"
        "\n"
        "RULES:\n"
        "- Put ALL adjustable dimensions as named variables at the top of the file\n"
        "- Add a comment after each variable with units and valid range: "
        "// mm (min: 2, max: 50)\n"
        "- Use descriptive variable names (wall_thickness, not wt)\n"
        "- Organize code with modules for logical groupings\n"
        "- Use $fn=60 or higher for smooth curves\n"
        "- Design for FDM 3D printing: flat bottom, printable geometry\n"
        "- Single solid body unless multi-part is explicitly requested\n"
        + lib_rule
    )

    # Material limits comment block
    mat_comment = ""
    try:
        from kiln.design_intelligence import get_material_profile

        if material:
            mat_profile = get_material_profile(material)
            if mat_profile:
                dl = mat_profile.design_limits or {}
                lines = [f"// Material: {mat_profile.display_name} \u2014 Design limits:"]
                rec_wall = dl.get("recommended_wall_thickness_mm")
                if rec_wall:
                    lines.append(f"// - Recommended wall thickness: {rec_wall}mm")
                overhang = dl.get("max_unsupported_overhang_deg")
                if overhang:
                    lines.append(f"// - Max unsupported overhang: {overhang}\u00b0")
                bridge = dl.get("max_bridge_length_mm")
                if bridge:
                    lines.append(f"// - Max bridge length: {bridge}mm")
                min_hole = dl.get("min_hole_diameter_mm")
                if min_hole:
                    lines.append(f"// - Min hole diameter: {min_hole}mm")
                min_pin = dl.get("min_pin_diameter_mm")
                if min_pin:
                    lines.append(f"// - Min pin diameter: {min_pin}mm")
                snap_tol = dl.get("snap_fit_tolerance_mm")
                if snap_tol:
                    lines.append(f"// - Snap-fit tolerance: \u00b1{snap_tol}mm")
                if len(lines) > 1:
                    mat_comment = "\n".join(lines) + "\n"
    except Exception:
        logger.debug("Could not load material profile for OpenSCAD comment", exc_info=True)

    # Combine: header + material comment + components + enhanced prompt
    parts = [header]
    if mat_comment:
        parts.append(mat_comment)
    if component_section:
        parts.append(component_section)
    parts.append(inner.improved_prompt)

    combined = "\n".join(parts)

    return ImprovedPrompt(
        original_prompt=requirements,
        improved_prompt=combined,
        feedback_applied=[],
        constraints_added=inner.constraints_added,
        iteration=0,
        expected_improvements=[
            f"Parametric OpenSCAD prompt with {len(inner.constraints_added)} "
            "design constraints applied",
        ],
    )


def get_feedback_loop(model_id: str) -> FeedbackLoop | None:
    """Retrieve a feedback loop by model ID.

    :param model_id: The model ID to look up.
    :returns: The :class:`FeedbackLoop` or ``None`` if not found.
    """
    from kiln.persistence import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM feedback_loops WHERE model_id = ?",
            (model_id,),
        ).fetchone()

        if not row:
            return None

        record = dict(row)
        iterations = json.loads(record.get("iterations", "[]"))

        return FeedbackLoop(
            model_id=record["model_id"],
            original_prompt=record["original_prompt"],
            iterations=iterations,
            current_iteration=record.get("current_iteration", 0),
            resolved=bool(record.get("resolved", 0)),
            best_iteration=record.get("best_iteration"),
        )
    except Exception:
        logger.debug("Failed to fetch feedback loop for %s", model_id, exc_info=True)
        return None
