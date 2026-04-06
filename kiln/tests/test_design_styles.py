"""Tests for kiln.design_styles.

Coverage areas:
- Style registry (list, get, lookup)
- Style dataclass serialization
- style_to_constraints generation
- apply_style_to_prompt prompt enrichment
- Edge cases (unknown style, empty prompt, long prompt trimming)
"""

from __future__ import annotations

from kiln.design_styles import (
    EdgeTreatment,
    StyledPrompt,
    SurfacePattern,
    apply_style_to_prompt,
    get_style,
    list_styles,
    style_to_constraints,
)


class TestStyleRegistry:
    """Style registry lookup and listing."""

    def test_list_styles_not_empty(self):
        styles = list_styles()
        assert len(styles) >= 7

    def test_all_styles_have_required_fields(self):
        for style in list_styles():
            assert style.name
            assert style.display_name
            assert style.description
            assert style.edge_treatment
            assert isinstance(style.generation_constraints, list)
            assert len(style.generation_constraints) >= 1

    def test_get_style_by_name(self):
        style = get_style("minimalist")
        assert style is not None
        assert style.name == "minimalist"

    def test_get_style_case_insensitive(self):
        style = get_style("ART_DECO")
        assert style is not None
        assert style.name == "art_deco"

    def test_get_style_strips_whitespace(self):
        style = get_style("  organic  ")
        assert style is not None
        assert style.name == "organic"

    def test_get_unknown_style_returns_none(self):
        assert get_style("cyberpunk") is None

    def test_known_styles_exist(self):
        expected = {"minimalist", "industrial", "organic", "art_deco",
                    "brutalist", "scandinavian", "retro"}
        actual = {s.name for s in list_styles()}
        assert expected <= actual


class TestStyleSerialization:
    """Style dataclass to_dict round-trip."""

    def test_style_to_dict(self):
        style = get_style("industrial")
        assert style is not None
        d = style.to_dict()
        assert d["name"] == "industrial"
        assert "edge_treatment" in d
        assert d["edge_treatment"]["type"] == "chamfer"

    def test_edge_treatment_to_dict(self):
        et = EdgeTreatment(type="fillet", radius_mm=3.0, description="test")
        d = et.to_dict()
        assert d["type"] == "fillet"
        assert d["radius_mm"] == 3.0

    def test_surface_pattern_to_dict(self):
        sp = SurfacePattern(
            pattern="honeycomb",
            scad_module="honeycomb_wall",
            parameters={"cell_size": 8.0},
        )
        d = sp.to_dict()
        assert d["pattern"] == "honeycomb"
        assert d["scad_module"] == "honeycomb_wall"

    def test_styled_prompt_to_dict(self):
        sp = StyledPrompt(
            original_prompt="test",
            style_name="organic",
            styled_prompt="test styled",
            constraints_added=["a", "b"],
            scad_includes=["voronoi.scad"],
            scad_preamble="// voronoi",
        )
        d = sp.to_dict()
        assert d["style_name"] == "organic"
        assert len(d["constraints_added"]) == 2


class TestStyleToConstraints:
    """style_to_constraints generates prompt constraints."""

    def test_known_style_returns_constraints(self):
        constraints = style_to_constraints("organic")
        assert len(constraints) >= 4
        assert any("curve" in c.lower() or "fillet" in c.lower() for c in constraints)

    def test_unknown_style_returns_empty(self):
        constraints = style_to_constraints("cyberpunk")
        assert constraints == []

    def test_wall_thickness_adjusted(self):
        # Industrial has multiplier 1.5 => 1.6 * 1.5 = 2.4
        constraints = style_to_constraints("industrial", base_wall_mm=1.6)
        assert any("2.4" in c for c in constraints)

    def test_brutalist_high_wall_multiplier(self):
        constraints = style_to_constraints("brutalist", base_wall_mm=1.6)
        assert any("3.2" in c for c in constraints)

    def test_material_preference_included(self):
        constraints = style_to_constraints("industrial")
        assert any("PETG" in c for c in constraints)

    def test_infill_hint_for_organic(self):
        constraints = style_to_constraints("organic")
        assert any("gyroid" in c.lower() for c in constraints)


class TestApplyStyleToPrompt:
    """apply_style_to_prompt enriches generation prompts."""

    def test_basic_styling(self):
        result = apply_style_to_prompt("a phone stand", "minimalist")
        assert result.style_name == "minimalist"
        assert "Minimalist" in result.styled_prompt
        assert len(result.constraints_added) >= 4

    def test_scad_includes_set(self):
        result = apply_style_to_prompt("a pencil holder", "industrial")
        assert "honeycomb.scad" in result.scad_includes

    def test_scad_preamble_has_edge_treatment(self):
        result = apply_style_to_prompt("a bracket", "organic")
        assert "fillet" in result.scad_preamble.lower()

    def test_sharp_edge_in_brutalist(self):
        result = apply_style_to_prompt("a shelf", "brutalist")
        assert "sharp" in result.scad_preamble.lower()

    def test_unknown_style_returns_original_prompt(self):
        result = apply_style_to_prompt("a widget", "unknown_style")
        assert result.styled_prompt == "a widget"
        assert result.constraints_added == []

    def test_prompt_length_respected(self):
        result = apply_style_to_prompt("a phone stand", "organic", max_length=200)
        assert len(result.styled_prompt) <= 200

    def test_original_prompt_preserved(self):
        result = apply_style_to_prompt("custom bracket for desk", "art_deco")
        assert result.original_prompt == "custom bracket for desk"
        assert "custom bracket" in result.styled_prompt

    def test_all_styles_produce_valid_output(self):
        for style in list_styles():
            result = apply_style_to_prompt("test object", style.name)
            assert result.style_name == style.name
            assert len(result.styled_prompt) > len("test object")
            assert len(result.constraints_added) >= 4


class TestStyleEdgeTreatmentCoverage:
    """Every edge treatment type is handled in apply_style_to_prompt."""

    def test_fillet_in_preamble(self):
        result = apply_style_to_prompt("test", "minimalist")
        assert "fillet" in result.scad_preamble.lower()

    def test_chamfer_in_preamble(self):
        result = apply_style_to_prompt("test", "industrial")
        assert "chamfer" in result.scad_preamble.lower()

    def test_sharp_in_preamble(self):
        result = apply_style_to_prompt("test", "brutalist")
        assert "sharp" in result.scad_preamble.lower()

    def test_variable_fillet_in_preamble(self):
        result = apply_style_to_prompt("test", "organic")
        assert "fillet" in result.scad_preamble.lower()


class TestStyleConstraintQuality:
    """Style constraints are specific and actionable, not generic."""

    def test_industrial_mentions_hex(self):
        constraints = style_to_constraints("industrial")
        assert any("hex" in c.lower() or "grid" in c.lower() for c in constraints)

    def test_organic_mentions_voronoi(self):
        constraints = style_to_constraints("organic")
        assert any("voronoi" in c.lower() or "nature" in c.lower() for c in constraints)

    def test_brutalist_mentions_sharp(self):
        constraints = style_to_constraints("brutalist")
        assert any("sharp" in c.lower() or "raw" in c.lower() for c in constraints)

    def test_art_deco_mentions_symmetry(self):
        constraints = style_to_constraints("art_deco")
        assert any("symmetry" in c.lower() or "geometric" in c.lower() for c in constraints)

    def test_scandinavian_mentions_generous_fillets(self):
        constraints = style_to_constraints("scandinavian")
        assert any("4mm" in c or "generous" in c.lower() for c in constraints)

    def test_retro_mentions_oversized(self):
        constraints = style_to_constraints("retro")
        assert any("6mm" in c or "oversized" in c.lower() for c in constraints)
