"""Parametric design style system — aesthetic customization for functional parts.

Applies named aesthetic styles to parametric OpenSCAD designs while
preserving structural function.  Each style defines edge treatments,
surface patterns, proportions, and material preferences that transform
a purely functional design into a styled object.

The style system works at the prompt level (guiding generation) and
the code level (providing SCAD snippets and module references).

Styles:
    minimalist   — clean lines, uniform radii, no ornamentation
    industrial   — exposed edges, chamfers, hex/grid patterns
    organic      — flowing curves, variable radii, voronoi/nature patterns
    art_deco     — geometric symmetry, stepped profiles, fan motifs
    brutalist    — raw surfaces, heavy proportions, no fillets
    scandinavian — gentle curves, light proportions, rounded everything
    retro        — bold radii, stepped edges, playful proportions

Public API:
    list_styles()          — all available style definitions
    get_style(name)        — single style by name
    style_to_constraints() — convert style to generation prompt constraints
    apply_style_to_scad()  — inject style modules into OpenSCAD code
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EdgeTreatment:
    """How edges should be treated in this style."""

    type: str  # "fillet", "chamfer", "sharp", "variable_fillet"
    radius_mm: float = 2.0
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SurfacePattern:
    """Surface pattern preference for this style."""

    pattern: str  # "none", "honeycomb", "voronoi", "lattice", "grid", "wave"
    scad_module: str = ""  # Which SCAD library module implements this
    parameters: dict[str, float] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesignStyle:
    """Complete aesthetic style definition for parametric designs."""

    name: str
    display_name: str
    description: str
    edge_treatment: EdgeTreatment
    surface_patterns: list[SurfacePattern]
    proportion_rules: list[str]  # Natural-language proportion guidance
    generation_constraints: list[str]  # Constraints to inject into prompts
    scad_includes: list[str]  # SCAD library files to include
    material_preference: str | None = None  # Suggested material
    wall_thickness_multiplier: float = 1.0  # Style-specific thickness adjustment
    infill_hint: str | None = None  # e.g. "gyroid" for organic

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["edge_treatment"] = self.edge_treatment.to_dict()
        d["surface_patterns"] = [p.to_dict() for p in self.surface_patterns]
        return d


@dataclass
class StyledPrompt:
    """Generation prompt enhanced with style constraints."""

    original_prompt: str
    style_name: str
    styled_prompt: str
    constraints_added: list[str]
    scad_includes: list[str]
    scad_preamble: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Style definitions
# ---------------------------------------------------------------------------

_STYLES: dict[str, DesignStyle] = {}


def _register(style: DesignStyle) -> None:
    _STYLES[style.name] = style


_register(DesignStyle(
    name="minimalist",
    display_name="Minimalist",
    description="Clean lines, uniform radii, no ornamentation. Form follows function.",
    edge_treatment=EdgeTreatment(
        type="fillet",
        radius_mm=2.0,
        description="Uniform small fillets on all edges",
    ),
    surface_patterns=[
        SurfacePattern(pattern="none", description="No surface patterns — pure geometry"),
    ],
    proportion_rules=[
        "Uniform wall thickness throughout",
        "Symmetrical where possible",
        "No decorative elements — every feature serves a function",
    ],
    generation_constraints=[
        "clean geometric shapes with uniform fillet radius",
        "no decorative ornamentation or surface textures",
        "consistent wall thickness, symmetrical design",
        "minimal material use while maintaining strength",
    ],
    scad_includes=["decorative.scad"],
    material_preference="pla",
    wall_thickness_multiplier=1.0,
))

_register(DesignStyle(
    name="industrial",
    display_name="Industrial",
    description="Exposed structure, chamfers, hex/grid patterns. Engineered aesthetic.",
    edge_treatment=EdgeTreatment(
        type="chamfer",
        radius_mm=1.5,
        description="Sharp chamfers on exposed edges",
    ),
    surface_patterns=[
        SurfacePattern(
            pattern="honeycomb",
            scad_module="honeycomb_wall",
            parameters={"cell_size": 8.0, "wall_thickness": 1.2},
            description="Hexagonal cut-out pattern on flat panels",
        ),
        SurfacePattern(
            pattern="grid",
            scad_module="lattice_grid",
            parameters={"spacing": 5.0, "bar_width": 1.0},
            description="Rectangular grid pattern for ventilation panels",
        ),
    ],
    proportion_rules=[
        "Exposed fastener features (hex recesses, bolt bosses)",
        "Heavier proportions — thicker walls, wider bases",
        "Visible structure over smooth surfaces",
    ],
    generation_constraints=[
        "chamfered edges, no rounded fillets",
        "hexagonal or grid patterns on flat surfaces for ventilation",
        "exposed structural elements, heavier proportions",
        "thick walls (minimum 2.4mm) for industrial durability",
    ],
    scad_includes=["decorative.scad", "honeycomb.scad", "lattice.scad"],
    material_preference="petg",
    wall_thickness_multiplier=1.5,
))

_register(DesignStyle(
    name="organic",
    display_name="Organic",
    description="Flowing curves, variable radii, nature-inspired patterns.",
    edge_treatment=EdgeTreatment(
        type="variable_fillet",
        radius_mm=4.0,
        description="Large variable-radius fillets, smooth transitions",
    ),
    surface_patterns=[
        SurfacePattern(
            pattern="voronoi",
            scad_module="voronoi_panel",
            parameters={"cell_count": 20, "wall_width": 1.5},
            description="Voronoi cell pattern for organic look",
        ),
    ],
    proportion_rules=[
        "No parallel edges — flowing, asymmetric curves",
        "Variable wall thickness (thicker at stress points, thinner decoratively)",
        "Tapered profiles rather than uniform extrusions",
    ],
    generation_constraints=[
        "flowing organic curves, no sharp angles or straight edges",
        "variable-radius fillets, smooth transitions between surfaces",
        "voronoi or nature-inspired patterns where structurally safe",
        "tapered profiles, thicker at base transitioning to thinner at top",
    ],
    scad_includes=["decorative.scad", "voronoi.scad"],
    material_preference="pla",
    wall_thickness_multiplier=1.2,
    infill_hint="gyroid",
))

_register(DesignStyle(
    name="art_deco",
    display_name="Art Deco",
    description="Geometric symmetry, stepped profiles, radiating fan motifs.",
    edge_treatment=EdgeTreatment(
        type="chamfer",
        radius_mm=1.0,
        description="Precise chamfers with stepped profile transitions",
    ),
    surface_patterns=[
        SurfacePattern(
            pattern="none",
            description="Stepped geometric profiles rather than surface patterns",
        ),
    ],
    proportion_rules=[
        "Strong symmetry — bilateral or radial",
        "Stepped profiles (ziggurats, tiered bases)",
        "Fan or sunburst motifs radiating from center",
        "Bold geometric shapes: chevrons, triangles, arcs",
    ],
    generation_constraints=[
        "strong bilateral or radial symmetry throughout",
        "stepped tiered profiles with geometric precision",
        "bold geometric motifs: chevrons, arcs, radiating lines",
        "chamfered edges with precise geometric transitions",
    ],
    scad_includes=["decorative.scad"],
    material_preference="pla",
    wall_thickness_multiplier=1.1,
))

_register(DesignStyle(
    name="brutalist",
    display_name="Brutalist",
    description="Raw surfaces, heavy proportions, no decorative softening.",
    edge_treatment=EdgeTreatment(
        type="sharp",
        radius_mm=0.0,
        description="No edge treatment — raw sharp edges",
    ),
    surface_patterns=[
        SurfacePattern(pattern="none", description="Raw undecorated surfaces"),
    ],
    proportion_rules=[
        "Massive proportions — oversized walls, chunky geometry",
        "No edge softening — sharp raw edges throughout",
        "Monolithic forms, minimal articulation",
    ],
    generation_constraints=[
        "raw sharp edges, no fillets or chamfers",
        "heavy massive proportions, minimum 3mm wall thickness",
        "monolithic solid forms, minimal surface articulation",
        "bold simple geometry — cubes, cylinders, slabs",
    ],
    scad_includes=["decorative.scad"],
    material_preference="petg",
    wall_thickness_multiplier=2.0,
))

_register(DesignStyle(
    name="scandinavian",
    display_name="Scandinavian",
    description="Gentle curves, light proportions, rounded everything.",
    edge_treatment=EdgeTreatment(
        type="fillet",
        radius_mm=4.0,
        description="Large generous fillets on all edges",
    ),
    surface_patterns=[
        SurfacePattern(pattern="none", description="Clean surfaces, no patterns"),
    ],
    proportion_rules=[
        "Light airy proportions — thin walls, open spaces",
        "Generous radii on every edge and corner",
        "Balanced asymmetry — not perfectly symmetric, but harmonious",
    ],
    generation_constraints=[
        "large generous fillets (4mm+) on all edges and corners",
        "light proportions, elegant thin walls (1.2-1.6mm)",
        "smooth flowing transitions, no sharp corners anywhere",
        "clean undecorated surfaces with gentle curves",
    ],
    scad_includes=["decorative.scad"],
    material_preference="pla",
    wall_thickness_multiplier=0.8,
))

_register(DesignStyle(
    name="retro",
    display_name="Retro",
    description="Bold radii, stepped edges, playful proportions.",
    edge_treatment=EdgeTreatment(
        type="fillet",
        radius_mm=6.0,
        description="Oversized bold fillets for retro look",
    ),
    surface_patterns=[
        SurfacePattern(pattern="none", description="Smooth surfaces with bold geometry"),
    ],
    proportion_rules=[
        "Oversized radii — chunky rounded corners",
        "Playful asymmetry and unexpected proportions",
        "Stepped or layered profiles for visual interest",
    ],
    generation_constraints=[
        "oversized bold fillets (6mm+) on all corners",
        "chunky playful proportions, slightly oversized features",
        "stepped or layered profiles for visual depth",
        "rounded everything — no sharp edges or points",
    ],
    scad_includes=["decorative.scad"],
    material_preference="pla",
    wall_thickness_multiplier=1.3,
))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_styles() -> list[DesignStyle]:
    """Return all available design styles."""
    return list(_STYLES.values())


def get_style(name: str) -> DesignStyle | None:
    """Look up a style by name (case-insensitive).

    :param name: Style name (e.g. ``"organic"``).
    :returns: The :class:`DesignStyle` or ``None`` if not found.
    """
    return _STYLES.get(name.lower().strip())


def style_to_constraints(
    style_name: str,
    *,
    base_wall_mm: float = 1.6,
) -> list[str]:
    """Convert a style to generation prompt constraints.

    :param style_name: Style name.
    :param base_wall_mm: Base wall thickness to apply the style's
        multiplier to.
    :returns: List of constraint strings for prompt injection.
    """
    style = get_style(style_name)
    if style is None:
        return []

    constraints = list(style.generation_constraints)

    # Style-adjusted wall thickness.
    adjusted_wall = round(base_wall_mm * style.wall_thickness_multiplier, 1)
    if adjusted_wall != base_wall_mm:
        constraints.append(f"wall thickness {adjusted_wall}mm for {style.display_name} style")

    # Material suggestion.
    if style.material_preference:
        constraints.append(f"designed for {style.material_preference.upper()} material")

    # Infill hint.
    if style.infill_hint:
        constraints.append(f"use {style.infill_hint} infill pattern for internal structure")

    return constraints


def apply_style_to_prompt(
    prompt: str,
    style_name: str,
    *,
    max_length: int = 10_000,
) -> StyledPrompt:
    """Enhance a generation prompt with style-specific constraints.

    Appends style constraints and SCAD module references to produce
    a generation prompt that creates a styled design.

    :param prompt: The base generation prompt.
    :param style_name: Style to apply.
    :param max_length: Maximum prompt length.
    :returns: A :class:`StyledPrompt` with the enhanced prompt.
    """
    style = get_style(style_name)
    if style is None:
        return StyledPrompt(
            original_prompt=prompt,
            style_name=style_name,
            styled_prompt=prompt,
            constraints_added=[],
            scad_includes=[],
            scad_preamble="",
        )

    constraints = style_to_constraints(style_name)
    style_desc = f"Style: {style.display_name} — {style.description}"

    # Build SCAD preamble with includes and edge treatment guidance.
    scad_lines: list[str] = []
    for inc in style.scad_includes:
        scad_lines.append(f"// Available: include <{inc}>")
    edge = style.edge_treatment
    if edge.type in ("fillet", "variable_fillet") and edge.radius_mm > 0:
        scad_lines.append(f"// Edge treatment: fillet all edges with radius {edge.radius_mm}mm")
    elif edge.type == "chamfer" and edge.radius_mm > 0:
        scad_lines.append(f"// Edge treatment: chamfer all edges {edge.radius_mm}mm")
    elif edge.type == "sharp":
        scad_lines.append("// Edge treatment: sharp raw edges, no softening")
    scad_preamble = "\n".join(scad_lines)

    # Build the styled prompt.
    req_text = ". ".join(constraints)
    suffix = f" {style_desc}. Requirements: {req_text}."

    max_original = max_length - len(suffix)
    if max_original < 20:
        suffix = f" Style: {style.display_name}. {'. '.join(constraints[:4])}."
        max_original = max_length - len(suffix)

    trimmed = prompt[:max_original].rstrip()
    styled = trimmed + suffix

    if len(styled) > max_length:
        styled = styled[: max_length - 3] + "..."

    return StyledPrompt(
        original_prompt=prompt,
        style_name=style.name,
        styled_prompt=styled,
        constraints_added=constraints,
        scad_includes=style.scad_includes,
        scad_preamble=scad_preamble,
    )
