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
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt sanity gate (KILN-010 claim 51)
# ---------------------------------------------------------------------------

# Minimum fraction of original-prompt tokens that must survive into the
# improved prompt, per claim 51's "at least 70 percent token overlap".
_MIN_TOKEN_OVERLAP_PCT = 0.70

# Detects "minimum X 2.5mm" / "max X 30 degrees" style numeric clauses.
# Capture groups: (1) bound kind, (2) noun phrase, (3) number, (4) unit.
_NUMERIC_BOUND_RE = re.compile(
    r"\b(min(?:imum)?|max(?:imum)?|no\s+more\s+than|at\s+least|at\s+most)\b"
    r"\s+([a-z][a-z\s\-]*?)\s+"
    r"(\d+(?:\.\d+)?)\s*"
    r"(mm|cm|m|degrees?|deg|%|percent|pct)?",
    re.IGNORECASE,
)
_LOWER_BOUND_KINDS = {"min", "minimum", "at least"}
_UPPER_BOUND_KINDS = {"max", "maximum", "no more than", "at most"}
# Token splitter — alphanumeric runs, lowercased.  Numbers count as
# tokens so "1.6mm" stays distinguishable from "1mm".
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_NORMALIZE_NOUN_RE = re.compile(r"\s+")

# Material-family conflicts — clauses claiming both a rigid and a
# flexible material, or both a high-temp and a low-temp service envelope,
# can't coexist in a single generated design.  The lists below are short
# on purpose: only confidently-conflicting families.
_MATERIAL_CONFLICT_GROUPS: list[tuple[str, list[str]]] = [
    ("rigid",      ["rigid", "stiff", "load-bearing", "structural"]),
    ("flexible",   ["flexible", "tpu", "elastomer", "rubber-like"]),
    ("food-safe",  ["food safe", "food-safe", "food contact"]),
    ("high-temp",  ["high temperature", "high-temp", "ultra high-performance"]),
]
# Pairs that conflict if both groups have at least one clause hit.
_MATERIAL_CONFLICT_PAIRS: list[tuple[str, str]] = [
    ("rigid", "flexible"),
]

# Textual presence/absence contradictions — "no X" with "with X" on the
# same noun.  Limited to small set of design nouns we actually emit.
_NEG_PRESENCE_RE = re.compile(
    r"\b(no|without|avoid|minimize)\s+([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,3})",
    re.IGNORECASE,
)
_POS_PRESENCE_RE = re.compile(
    r"\b(with|include|add|require[sd]?)\s+([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,3})",
    re.IGNORECASE,
)

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

# When this-printer outcome history has fewer than this many total
# recorded prints, the prompt context resolver also consults community
# aggregates to seed the gap.  Three is the point at which a median /
# mode starts being statistically meaningful for a small bag of
# categorical outcomes.
_LOCAL_SPARSE_THRESHOLD = 3


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
    # Intent-gate failures emitted by generator-declared assertions.
    # Synthesised by an adapter in the kiln-pro package; the enum value
    # lives in public Kiln so the existing PrintFeedback +
    # generate_improved_prompt retry loop can consume it without a
    # pro install.  See https://kiln3d.com for tier details.
    INTENT = "intent"


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


class SanityFailureKind(str, enum.Enum):
    """Categories of sanity-gate violations (KILN-010 claim 51)."""

    CONTRADICTION = "contradiction"
    BUDGET = "budget"
    INTENT_DRIFT = "intent_drift"


@dataclass
class SanityFailure:
    """A single sanity-gate violation on an improved prompt."""

    kind: SanityFailureKind
    message: str
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "detail": self.detail or {},
        }


@dataclass
class SanityResult:
    """Sanity-gate verdict for an improved prompt (KILN-010 claim 51).

    Three checks: no contradictions between constraints, prompt fits the
    provider budget, and at least 70% token overlap between the improved
    and original prompts (intent preservation).
    """

    passed: bool
    failures: list[SanityFailure]
    token_overlap_pct: float
    length: int
    budget: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": [f.to_dict() for f in self.failures],
            "token_overlap_pct": round(self.token_overlap_pct, 4),
            "length": self.length,
            "budget": self.budget,
        }


@dataclass
class ImprovedPrompt:
    """An improved prompt with feedback constraints applied."""

    original_prompt: str
    improved_prompt: str
    feedback_applied: list[PrintFeedback]
    constraints_added: list[str]
    iteration: int  # which retry attempt this is
    expected_improvements: list[str]
    sanity: SanityResult | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["feedback_applied"] = [f.to_dict() for f in self.feedback_applied]
        if self.sanity is not None:
            data["sanity"] = self.sanity.to_dict()
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
# Sanity gate
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokenization."""
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def _strip_suffix(improved: str) -> str:
    """Drop the appended ``Requirements:`` / ``Avoid:`` clauses.

    Token-overlap is computed against the user-intent prefix only —
    otherwise the appended constraints would always inflate the
    intersection toward 100% and the check would never fire.
    """
    cut = len(improved)
    for marker in (" Requirements:", " Avoid:"):
        idx = improved.find(marker)
        if idx >= 0 and idx < cut:
            cut = idx
    return improved[:cut]


def _normalize_noun(noun: str) -> str:
    """Collapse whitespace and lowercase a constraint noun phrase."""
    return _NORMALIZE_NOUN_RE.sub(" ", noun.strip().lower())


def _detect_numeric_contradictions(text: str) -> list[tuple[str, str]]:
    """Return contradicting (lower-bound, upper-bound) clause pairs.

    Looks for ``minimum X N unit`` and ``maximum X M unit`` in the same
    text where ``min > max`` for matching noun phrases and units.
    """
    bounds: list[dict[str, Any]] = []
    for m in _NUMERIC_BOUND_RE.finditer(text):
        kind = m.group(1).lower().strip()
        kind = _NORMALIZE_NOUN_RE.sub(" ", kind)
        noun = _normalize_noun(m.group(2))
        if not noun:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        unit = (m.group(4) or "").lower()
        bounds.append({
            "kind": kind,
            "noun": noun,
            "value": value,
            "unit": unit,
            "raw": m.group(0).strip(),
        })

    contradictions: list[tuple[str, str]] = []
    for i, a in enumerate(bounds):
        for b in bounds[i + 1 :]:
            if a["noun"] != b["noun"] or a["unit"] != b["unit"]:
                continue
            a_lower = a["kind"] in _LOWER_BOUND_KINDS
            b_lower = b["kind"] in _LOWER_BOUND_KINDS
            a_upper = a["kind"] in _UPPER_BOUND_KINDS
            b_upper = b["kind"] in _UPPER_BOUND_KINDS
            if a_lower and b_upper and a["value"] > b["value"]:
                contradictions.append((a["raw"], b["raw"]))
            elif b_lower and a_upper and b["value"] > a["value"]:
                contradictions.append((b["raw"], a["raw"]))
    return contradictions


def _detect_material_conflicts(text: str) -> list[tuple[str, str]]:
    """Return material-family conflict pairs found in *text*.

    A flexible-material clause adjacent to a rigid-material clause in
    the same prompt is the canonical conflict.  Both clauses must have
    a textual hit for the conflict to register.
    """
    lower = text.lower()
    hits: dict[str, str] = {}
    for group, terms in _MATERIAL_CONFLICT_GROUPS:
        for term in terms:
            if term in lower:
                hits[group] = term
                break
    conflicts: list[tuple[str, str]] = []
    for a, b in _MATERIAL_CONFLICT_PAIRS:
        if a in hits and b in hits:
            conflicts.append((hits[a], hits[b]))
    return conflicts


def _detect_presence_contradictions(text: str) -> list[tuple[str, str]]:
    """Return presence/absence contradiction pairs.

    Catches "no X" + "with X" on the same noun phrase.  Conservative:
    only flags exact-noun matches to avoid noise from partial overlaps.
    """
    negatives: dict[str, str] = {}
    for m in _NEG_PRESENCE_RE.finditer(text):
        noun = _normalize_noun(m.group(2))
        # Tokenise the noun phrase head and use it as the lookup key —
        # "thin walls" vs "with thin walls" should match on the head.
        head = noun.split()[0] if noun else ""
        if head and head not in negatives:
            negatives[head] = m.group(0).strip()

    conflicts: list[tuple[str, str]] = []
    if not negatives:
        return conflicts
    for m in _POS_PRESENCE_RE.finditer(text):
        noun = _normalize_noun(m.group(2))
        head = noun.split()[0] if noun else ""
        if head in negatives:
            conflicts.append((negatives[head], m.group(0).strip()))
    return conflicts


def check_prompt_sanity(
    original_prompt: str,
    improved_prompt: str,
    *,
    budget: int,
    min_token_overlap: float = _MIN_TOKEN_OVERLAP_PCT,
) -> SanityResult:
    """Run the three-check sanity gate from KILN-010 claim 51.

    The gate is a deterministic stand-in for the patent's "second model"
    — it catches the contradiction shapes a model would catch, plus the
    cheaper budget and intent-overlap checks.  Callers re-generate when
    the result is not ``passed``.

    :param original_prompt: User-supplied prompt before enhancement.
    :param improved_prompt: Enhanced prompt with constraints appended.
    :param budget: Maximum prompt length the provider will accept.
    :param min_token_overlap: Minimum fraction of original tokens that
        must appear in the improved prompt's intent prefix.  Defaults to
        the patent's 0.70 threshold.
    :returns: A :class:`SanityResult` summarising the verdict.
    """
    failures: list[SanityFailure] = []

    # (b) Budget check first — cheap, and a bust here makes other checks
    # irrelevant.
    length = len(improved_prompt)
    if length > budget:
        over = length - budget
        failures.append(
            SanityFailure(
                kind=SanityFailureKind.BUDGET,
                message=(
                    f"Improved prompt is {length} chars — {over} over the "
                    f"provider budget of {budget}.  Trim {over} chars from the "
                    f"original prompt or drop the lowest-priority constraints."
                ),
                detail={"length": length, "budget": budget, "over_by": over},
            )
        )

    # (a) Contradictions — three independent shapes.  Numeric bound
    # inversions (min > max), material family conflicts (rigid vs
    # flexible), and presence/absence conflicts (no X + with X).
    for lower, upper in _detect_numeric_contradictions(improved_prompt):
        failures.append(
            SanityFailure(
                kind=SanityFailureKind.CONTRADICTION,
                message=(
                    f"Contradicting numeric bounds: {lower!r} and {upper!r}.  "
                    f"Drop one of the two bounds before sending to the provider."
                ),
                detail={"shape": "numeric_bound", "lower": lower, "upper": upper},
            )
        )
    for a, b in _detect_material_conflicts(improved_prompt):
        failures.append(
            SanityFailure(
                kind=SanityFailureKind.CONTRADICTION,
                message=(
                    f"Conflicting material families: {a!r} and {b!r}.  "
                    f"Pick one material family — the design can't be both."
                ),
                detail={"shape": "material_family", "a": a, "b": b},
            )
        )
    for negative, positive in _detect_presence_contradictions(improved_prompt):
        failures.append(
            SanityFailure(
                kind=SanityFailureKind.CONTRADICTION,
                message=(
                    f"Presence/absence conflict: {negative!r} and {positive!r}.  "
                    f"Resolve by removing whichever clause was added by mistake."
                ),
                detail={"shape": "presence", "negative": negative, "positive": positive},
            )
        )

    # (c) Intent-preservation: ≥70% of original tokens must appear in the
    # improved prompt's prefix (excluding the appended Requirements /
    # Avoid clauses, which would otherwise inflate the overlap to ~100%).
    original_tokens = _tokenize(original_prompt)
    intent_prefix_tokens = set(_tokenize(_strip_suffix(improved_prompt)))
    if original_tokens:
        kept = sum(1 for t in original_tokens if t in intent_prefix_tokens)
        overlap = kept / len(original_tokens)
    else:
        overlap = 1.0
    if overlap < min_token_overlap:
        failures.append(
            SanityFailure(
                kind=SanityFailureKind.INTENT_DRIFT,
                message=(
                    f"The improved prompt only retains {overlap:.0%} of the "
                    f"original tokens (required: {min_token_overlap:.0%}).  The "
                    f"appended constraints are crowding out the user's request "
                    f"— shorten the constraint suffix or raise the budget."
                ),
                detail={
                    "overlap_pct": round(overlap, 4),
                    "required": min_token_overlap,
                    "original_tokens": len(original_tokens),
                },
            )
        )

    return SanityResult(
        passed=not failures,
        failures=failures,
        token_overlap_pct=overlap,
        length=length,
        budget=budget,
    )


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
    # Each detected issue synthesizes a constraint string that bakes in
    # the ACTUAL measured geometry — "no overhangs greater than 65
    # degrees (current max 72°)" is more actionable than the template
    # "no overhangs greater than 45 degrees".  Constraints remain
    # target-specific (the AI is told what to aim for) but also carry
    # the delta from current state.
    printability_issues: list[str] = []
    printability_constraints: list[str] = []

    max_overhang = report.get("max_overhang_angle", 0)
    if max_overhang > 45:
        printability_issues.append(f"Overhangs detected ({max_overhang} degrees)")
        printability_constraints.append(
            f"flat bottom, no overhangs greater than 45 degrees "
            f"(current max {max_overhang:.0f}°)"
        )

    min_wall = report.get("min_wall_thickness", float("inf"))
    if min_wall < 2.0:
        printability_issues.append(f"Thin walls detected ({min_wall:.1f}mm)")
        printability_constraints.append(
            f"minimum wall thickness 2mm (current min {min_wall:.1f}mm)"
        )

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
                constraints=[
                    f"wide flat base for bed adhesion "
                    f"(current contact ~{contact_pct:.0f}% — target >20%)"
                ],
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
    # When a failure_mode is accompanied by geometric context (dimensions,
    # bed contact, overhang angles), we synthesize a richer constraint
    # than the template — e.g., for warping on a large flat base, append
    # the actual base dimensions so the next generation knows exactly
    # what to chamfer down.  This upgrades mode→template to
    # (mode, geometry) → specific-geometric constraint.
    if failure_mode:
        struct_issues: list[str] = []
        struct_constraints: list[str] = []

        fm_lower = failure_mode.lower()
        dims = report.get("dimensions") or {}
        base_w = dims.get("width", 0)
        base_d = dims.get("depth", 0)

        if fm_lower in ("adhesion", "adhesion_loss"):
            struct_issues.append("Part detached from bed during printing")
            if contact_pct is not None and contact_pct < 15.0:
                struct_constraints.append(
                    f"wide flat base for bed adhesion, consider brim "
                    f"(previous print had only {contact_pct:.0f}% bed contact)"
                )
            else:
                struct_constraints.append(_STRUCTURAL_CONSTRAINTS["weak_base"])
        if fm_lower in ("spaghetti", "layer_shift"):
            struct_issues.append(f"Print failure mode: {failure_mode}")
            struct_constraints.append(_STRUCTURAL_CONSTRAINTS["fragile"])
            if max_overhang > 45:
                struct_constraints.append(
                    f"flat bottom, no overhangs greater than 45 degrees "
                    f"(previous failure had {max_overhang:.0f}° overhang)"
                )
            else:
                struct_constraints.append(_PRINTABILITY_CONSTRAINTS["overhang"])
        if fm_lower == "stringing":
            struct_issues.append("Excessive stringing between parts")
            struct_constraints.append(_PRINTABILITY_CONSTRAINTS["bridge"])
        if fm_lower in ("warping",):
            struct_issues.append("Part warped during printing")
            if base_w > 50 and base_d > 50:
                struct_constraints.append(
                    f"chamfered corners, avoid large flat surfaces "
                    f"(previous failure had a {base_w:.0f}mm x {base_d:.0f}mm "
                    f"base — chamfer corners ≥3mm)"
                )
            else:
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

    sanity = check_prompt_sanity(original_prompt, improved, budget=limit)
    if not sanity.passed:
        logger.warning(
            "Prompt sanity gate failed (iteration %d): %s",
            iteration,
            [f.kind for f in sanity.failures],
        )

    return ImprovedPrompt(
        original_prompt=original_prompt,
        improved_prompt=improved,
        feedback_applied=feedback,
        constraints_added=all_constraints,
        iteration=iteration,
        expected_improvements=expected_improvements,
        sanity=sanity,
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

    # Adapter resolution is best-effort: when the server registry has no
    # matching adapter (e.g. offline tests, fresh install) we still want
    # the downstream outcome-history and community-intelligence blend to
    # run, so we carry on with ``adapter = None`` instead of bailing out.
    adapter: Any = None
    try:
        import kiln.server as _srv

        if printer_name:
            adapter = _srv._registry.get(printer_name)
        else:
            adapter = _srv._get_adapter()
    except Exception:
        logger.debug("No adapter available for context resolution", exc_info=True)

    # Build volume from printer info.
    if adapter is not None:
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
        except Exception:
            logger.debug("Could not resolve printer info", exc_info=True)

        # The model goes through the shared resolver, NOT the raw probe:
        # ctx.printer_model drives printer intelligence and the design
        # profile, so a live self-report must never outrank the model
        # the owner declared in config.yaml.
        try:
            from kiln.community_autofire import resolve_adapter_model

            resolved = resolve_adapter_model(adapter)
            if resolved:
                ctx.printer_model = resolved
        except Exception:
            logger.debug("Could not resolve printer model", exc_info=True)

    # Auto-detect material from AMS (Bambu) or spool manager.
    if not ctx.material and adapter is not None:
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
    # Three sources, in priority order:
    #   1. LIVE outcome history for this specific printer instance (SQLite
    #      ``print_outcomes`` table).  This is ground truth — what THIS
    #      printer has actually failed with, most recent first.
    #   2. STATIC knowledge base for the printer model (firmware quirks,
    #      known-issue symptoms).  Fallback when live history is sparse.
    #   3. COMMUNITY aggregate for the (printer_model, material) pair —
    #      anonymous cross-user intelligence, only consulted when local
    #      outcomes are too sparse to be statistically meaningful (< 3).
    #
    # Higher-priority failures are placed first so the downstream
    # ``_FAILURE_MITIGATIONS`` loop (which takes the first 3) picks this
    # printer's real problems before model-level hearsay or community wisdom.
    live_failures: list[str] = []
    live_outcome_count = 0
    if printer_name:
        try:
            from kiln.persistence import get_db

            insights = get_db().get_printer_learning_insights(printer_name)
            live_outcome_count = int(insights.get("total_outcomes", 0) or 0)
            breakdown = insights.get("failure_breakdown") or {}
            # failure_breakdown is already ordered by count DESC from the
            # SQL GROUP BY, but dict iteration order is insertion order
            # in 3.7+ so this is safe.
            live_failures = [
                fm for fm, count in breakdown.items()
                if fm and count > 0
            ]
        except Exception:
            logger.debug("Live learning insights unavailable", exc_info=True)

    static_failures: list[str] = []
    if ctx.printer_model:
        try:
            from kiln.printer_intelligence import (
                get_printer_intel,
                intel_to_dict,
            )

            intel_obj = get_printer_intel(ctx.printer_model)
            intel = intel_to_dict(intel_obj) if intel_obj else None
            if intel:
                failures = intel.get("common_failures", [])
                if failures:
                    static_failures = [
                        f.get("symptom", "") for f in failures[:5]
                        if f.get("symptom")
                    ]
                notes = intel.get("agent_notes", [])
                if notes:
                    ctx.printer_notes = notes[:3]
        except Exception:
            logger.debug("Printer intelligence unavailable", exc_info=True)

    community_failures: list[str] = []
    if (
        live_outcome_count < _LOCAL_SPARSE_THRESHOLD
        and ctx.printer_model
        and ctx.material
    ):
        try:
            from kiln.community_sync import fetch_community_insights

            community = fetch_community_insights(ctx.printer_model, ctx.material)
            if community and community.get("sample_size", 0) >= _LOCAL_SPARSE_THRESHOLD:
                breakdown = community.get("failure_breakdown") or {}
                community_failures = [
                    fm for fm, count in breakdown.items()
                    if fm and count > 0
                ]
        except Exception:
            logger.debug("Community insights unavailable", exc_info=True)

    # Merge: live → static → community; dedupe case-insensitively.
    seen_lower: set[str] = set()
    merged: list[str] = []
    for entry in (*live_failures, *static_failures, *community_failures):
        key = entry.lower()
        if key and key not in seen_lower:
            seen_lower.add(key)
            merged.append(entry)
    if merged:
        ctx.common_failures = merged

    return ctx


def enhance_prompt_with_design_intelligence(
    prompt: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
    provider: str | None = None,
    max_length: int | None = None,
    printer_context: PrinterGenerationContext | None = None,
    anti_patterns: list[str] | None = None,
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
    :param anti_patterns: Optional list of explicit anti-pattern clauses
        to inject as ``Avoid:`` guidance (KILN-010 claim 62).  When
        omitted, anti-patterns are derived from
        ``printer_context.common_failures`` via
        :func:`kiln.failure_vocabulary.anti_pattern_for`.
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
            sanity=check_prompt_sanity(prompt, prompt, budget=limit),
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
        # Guard the value actually appended, not its sibling.  The reason
        # comes from the kiln-pro overlay, so guarding on the public
        # orientation LABEL pushed an empty string into the constraint list
        # whenever the overlay was unavailable.  The label itself is public —
        # state that rather than nothing.
        if pattern.print_orientation_reason:
            core_constraints.append(pattern.print_orientation_reason)
        elif pattern.print_orientation:
            core_constraints.append(
                f"{pattern.display_name}: print orientation "
                f"{pattern.print_orientation.replace('_', ' ')}"
            )
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
    # Delegated to :mod:`kiln.failure_vocabulary` so the failure-mode
    # taxonomy and its design mitigations stay in one place.
    if printer_context is not None and printer_context.common_failures:
        from kiln.failure_vocabulary import mitigation_for

        for failure in printer_context.common_failures[:3]:
            mitigation = mitigation_for(failure)
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

    # Negative-constraint anti-patterns (KILN-010 claim 62) — explicit
    # exclusions of patterns known to cause failures for the current
    # (material, printer) context.  Caller-supplied ``anti_patterns``
    # take precedence; otherwise derive from the printer's recurring
    # failure history.
    anti_clauses: list[str] = []
    if anti_patterns:
        anti_clauses = [c.strip() for c in anti_patterns if c and c.strip()]
    elif printer_context is not None and printer_context.common_failures:
        from kiln.failure_vocabulary import anti_pattern_for

        for failure in printer_context.common_failures[:3]:
            ap = anti_pattern_for(failure)
            if ap and ap not in anti_clauses:
                anti_clauses.append(ap)

    if not constraints and not anti_clauses:
        return ImprovedPrompt(
            original_prompt=prompt,
            improved_prompt=prompt,
            feedback_applied=[],
            constraints_added=[],
            iteration=0,
            expected_improvements=[],
            sanity=check_prompt_sanity(prompt, prompt, budget=limit),
        )

    # Build the enhanced prompt — use more constraints when budget allows
    max_constraints = 8 if limit <= 600 else 15

    def _build_suffix(positive: list[str], negative: list[str]) -> str:
        parts: list[str] = []
        if positive:
            parts.append(f"Requirements: {'. '.join(positive)}.")
        if negative:
            parts.append(f"Avoid: {'; '.join(negative)}.")
        return (" " + " ".join(parts)) if parts else ""

    suffix = _build_suffix(constraints[:max_constraints], anti_clauses)
    max_original = limit - len(suffix)
    if max_original < 20:
        # Tight budget — reduce to top-4 positives and top-2 anti-patterns.
        suffix = _build_suffix(constraints[:4], anti_clauses[:2])
        max_original = limit - len(suffix)
    if max_original < 20 and anti_clauses:
        # Still tight — drop anti-patterns rather than positives.
        suffix = _build_suffix(constraints[:4], [])
        max_original = limit - len(suffix)
        anti_clauses = []

    trimmed = prompt[:max_original].rstrip()
    improved = trimmed + suffix

    if len(improved) > limit:
        improved = improved[: limit - 3] + "..."

    # Tag anti-patterns in constraints_added so callers can introspect.
    constraints_added = list(constraints) + [f"avoid: {c}" for c in anti_clauses]

    sanity = check_prompt_sanity(prompt, improved, budget=limit)
    if not sanity.passed:
        logger.warning(
            "Proactive prompt sanity gate failed: %s",
            [f.kind for f in sanity.failures],
        )

    return ImprovedPrompt(
        original_prompt=prompt,
        improved_prompt=improved,
        feedback_applied=[],
        constraints_added=constraints_added,
        iteration=0,
        expected_improvements=[
            f"Design-aware generation with {len(constraints)} constraints "
            f"and {len(anti_clauses)} anti-patterns applied",
        ],
        sanity=sanity,
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
