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
    report = printability_report or {}

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
