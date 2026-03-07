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

# Maximum prompt length for Meshy API.
_MAX_PROMPT_LENGTH = 600


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


def generate_improved_prompt(
    original_prompt: str,
    feedback: list[PrintFeedback],
    *,
    iteration: int = 1,
) -> ImprovedPrompt:
    """Construct an improved prompt incorporating feedback constraints.

    Adds physical constraints to the end of the original prompt without
    modifying the creative intent. Keeps the total prompt under
    :data:`_MAX_PROMPT_LENGTH` characters (Meshy limit).

    :param original_prompt: The original generation prompt.
    :param feedback: List of :class:`PrintFeedback` items to apply.
    :param iteration: Which retry iteration this is (default 1).
    :returns: An :class:`ImprovedPrompt` with the improved text.
    """
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
        max_original_len = _MAX_PROMPT_LENGTH - len(suffix)
        if max_original_len < 20:
            # Constraints too long — prioritize the most important ones
            suffix = f" Requirements: {'. '.join(all_constraints[:3])}."
            max_original_len = _MAX_PROMPT_LENGTH - len(suffix)

        trimmed_prompt = original_prompt[:max_original_len].rstrip()
        improved = trimmed_prompt + suffix
    else:
        improved = original_prompt

    # Final length enforcement
    if len(improved) > _MAX_PROMPT_LENGTH:
        improved = improved[: _MAX_PROMPT_LENGTH - 3] + "..."

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
        db._conn.execute(
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
        db._conn.commit()
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
        db._conn.execute(
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
        db._conn.commit()
    except Exception:
        logger.exception("Failed to update feedback loop (non-fatal)")

    return loop


def enhance_prompt_with_design_intelligence(
    prompt: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
    max_length: int = _MAX_PROMPT_LENGTH,
) -> ImprovedPrompt:
    """Enhance a generation prompt with design intelligence constraints.

    Analyzes the prompt for functional requirements and appends relevant
    manufacturing constraints, material guidance, and design rules to
    produce a smarter generation request.

    This is called **before** generation (proactive), unlike
    :func:`analyze_for_feedback` which is called **after** (reactive).

    :param prompt: The original generation prompt.
    :param material: Optional material to constrain to.
    :param max_length: Maximum allowed prompt length.
    :returns: An :class:`ImprovedPrompt` with design constraints applied.
    """
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

    # Extract the most impactful constraints from the brief
    constraints: list[str] = []

    # Material design limits
    rules = brief.combined_rules
    if rules.get("min_wall_thickness_mm"):
        constraints.append(f"minimum wall thickness {rules['min_wall_thickness_mm']}mm")
    if rules.get("infill_min_pct"):
        constraints.append("solid, thick structural elements")
    if rules.get("gussets_required"):
        constraints.append("triangular gussets at load-bearing joints")
    if rules.get("fillets_required"):
        constraints.append("rounded fillets at all corners and joints")

    # Material suitability
    mat = brief.recommended_material
    if mat and mat.material:
        constraints.append(f"designed for {mat.material.display_name} material")

    if printer_profile:
        build = printer_profile.build_volume_mm
        constraints.append(
            f"fit within {build['x']} x {build['y']} x {build['z']} mm build volume"
        )

    # Pattern-specific constraints
    for pattern in brief.applicable_patterns[:2]:  # limit to top 2
        if pattern.print_orientation:
            constraints.append(pattern.print_orientation_reason)

    # Printability basics
    constraints.append("flat bottom for bed adhesion")
    constraints.append("no overhangs greater than 50 degrees")
    constraints.append("single solid body, no floating parts")

    if not constraints:
        return ImprovedPrompt(
            original_prompt=prompt,
            improved_prompt=prompt,
            feedback_applied=[],
            constraints_added=[],
            iteration=0,
            expected_improvements=[],
        )

    # Build the enhanced prompt
    requirements = ". ".join(constraints[:8])  # cap at 8 constraints
    suffix = f" Requirements: {requirements}."

    max_original = max_length - len(suffix)
    if max_original < 20:
        suffix = f" Requirements: {'. '.join(constraints[:4])}."
        max_original = max_length - len(suffix)

    trimmed = prompt[:max_original].rstrip()
    improved = trimmed + suffix

    if len(improved) > max_length:
        improved = improved[: max_length - 3] + "..."

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


def design_validation_to_feedback(
    report: Any,
    original_prompt: str,
) -> list[PrintFeedback]:
    """Convert a :class:`~kiln.design_validator.DesignValidationReport` into feedback.

    Bridge function that converts design validation failures into
    :class:`PrintFeedback` items for the existing iterative improvement
    loop.  This closes the loop: generate -> validate -> feedback ->
    regenerate.

    :param report: A ``DesignValidationReport`` from
        :func:`~kiln.design_validator.validate_design`.
    :param original_prompt: The original generation prompt.
    :returns: List of :class:`PrintFeedback` items.
    """
    from kiln.design_validator import validation_to_feedback

    return validation_to_feedback(report, original_prompt)


def get_feedback_loop(model_id: str) -> FeedbackLoop | None:
    """Retrieve a feedback loop by model ID.

    :param model_id: The model ID to look up.
    :returns: The :class:`FeedbackLoop` or ``None`` if not found.
    """
    from kiln.persistence import get_db

    db = get_db()
    try:
        row = db._conn.execute(
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
