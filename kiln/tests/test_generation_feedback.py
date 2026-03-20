"""Tests for kiln.generation_feedback.

Coverage areas:
- FeedbackType enum values
- PrintFeedback, ImprovedPrompt, FeedbackLoop dataclasses
- analyze_for_feedback with various printability issues
- generate_improved_prompt constraint application
- Feedback loop lifecycle (start, add iteration, get)
- Prompt length limits
- Edge cases: no issues, empty feedback, long prompts
- enhance_prompt_with_design_intelligence provider-aware limits
- build_parametric_generation_prompt OpenSCAD output
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiln.generation_feedback import (
    _MAX_PROMPT_LENGTH,
    FeedbackLoop,
    FeedbackType,
    ImprovedPrompt,
    PrintFeedback,
    add_iteration,
    analyze_for_feedback,
    build_parametric_generation_prompt,
    enhance_prompt_with_design_intelligence,
    generate_improved_prompt,
    get_feedback_loop,
    start_feedback_loop,
)


class TestFeedbackTypeEnum:
    """FeedbackType enum uses string values."""

    def test_all_types_have_string_values(self):
        for ft in FeedbackType:
            assert isinstance(ft.value, str)

    def test_expected_types_exist(self):
        expected = {"printability", "dimensional", "structural", "aesthetic", "material"}
        actual = {ft.value for ft in FeedbackType}
        assert expected == actual


class TestPrintFeedbackDataclass:
    """PrintFeedback to_dict() serialises enum."""

    def test_to_dict_serialises_feedback_type(self):
        fb = PrintFeedback(
            original_prompt="a phone stand",
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["overhangs"],
            constraints=["no overhangs > 45 degrees"],
            severity="moderate",
        )
        d = fb.to_dict()
        assert d["feedback_type"] == "printability"
        assert d["severity"] == "moderate"

    def test_to_dict_returns_dict(self):
        fb = PrintFeedback(
            original_prompt="test",
            feedback_type=FeedbackType.AESTHETIC,
            issues=[],
            constraints=[],
            severity="minor",
        )
        assert isinstance(fb.to_dict(), dict)


class TestImprovedPromptDataclass:
    """ImprovedPrompt to_dict() serialises nested feedback."""

    def test_to_dict_serialises_feedback_list(self):
        fb = PrintFeedback(
            original_prompt="a vase",
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["thin walls"],
            constraints=["min 2mm walls"],
            severity="moderate",
        )
        ip = ImprovedPrompt(
            original_prompt="a vase",
            improved_prompt="a vase. Requirements: min 2mm walls.",
            feedback_applied=[fb],
            constraints_added=["min 2mm walls"],
            iteration=1,
            expected_improvements=["Fix: thin walls"],
        )
        d = ip.to_dict()
        assert d["feedback_applied"][0]["feedback_type"] == "printability"
        assert d["iteration"] == 1


class TestFeedbackLoopDataclass:
    """FeedbackLoop to_dict() returns plain dict."""

    def test_to_dict(self):
        fl = FeedbackLoop(
            model_id="model-1",
            original_prompt="a phone stand",
            iterations=[{"prompt": "test", "issues": [], "outcome": "success"}],
            current_iteration=1,
            resolved=True,
            best_iteration=1,
        )
        d = fl.to_dict()
        assert d["model_id"] == "model-1"
        assert d["resolved"] is True


class TestAnalyzeForFeedback:
    """analyze_for_feedback identifies printability issues."""

    def test_no_issues_returns_empty(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="a simple cube",
        )
        assert result == []

    def test_overhang_detected(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="a fancy sculpture",
            printability_report={"max_overhang_angle": 60},
        )
        assert len(result) >= 1
        assert any(fb.feedback_type == FeedbackType.PRINTABILITY for fb in result)
        assert any("overhang" in c.lower() for fb in result for c in fb.constraints)

    def test_thin_wall_detected(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="a thin vase",
            printability_report={"min_wall_thickness": 0.8},
        )
        assert len(result) >= 1
        assert any("wall thickness" in c.lower() for fb in result for c in fb.constraints)

    def test_bridges_detected(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="an arch",
            printability_report={"has_bridges": True},
        )
        assert len(result) >= 1
        assert any("bridge" in c.lower() for fb in result for c in fb.constraints)

    def test_floating_parts_detected(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="test",
            printability_report={"has_floating_parts": True},
        )
        assert len(result) >= 1
        assert any("floating" in c.lower() or "continuous" in c.lower() for fb in result for c in fb.constraints)

    def test_non_manifold_detected(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="test",
            printability_report={"non_manifold": True},
        )
        assert len(result) >= 1
        assert any("manifold" in c.lower() or "watertight" in c.lower() for fb in result for c in fb.constraints)

    def test_adhesion_failure_mode(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="a tall tower",
            failure_mode="adhesion",
        )
        assert len(result) >= 1
        assert any(fb.feedback_type == FeedbackType.STRUCTURAL for fb in result)
        assert any("base" in c.lower() for fb in result for c in fb.constraints)

    def test_spaghetti_failure_mode(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="a complex model",
            failure_mode="spaghetti",
        )
        assert len(result) >= 1
        assert any(fb.feedback_type == FeedbackType.STRUCTURAL for fb in result)

    def test_stringing_failure_mode(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="test",
            failure_mode="stringing",
        )
        assert len(result) >= 1

    def test_warping_failure_mode(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="a flat plate",
            failure_mode="warping",
        )
        assert len(result) >= 1
        assert any(fb.feedback_type == FeedbackType.STRUCTURAL for fb in result)

    def test_model_too_large(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="test",
            printability_report={
                "dimensions": {"width": 300, "depth": 300, "height": 300},
                "build_volume": {"x": 250, "y": 210, "z": 210},
            },
        )
        assert len(result) >= 1
        assert any(fb.feedback_type == FeedbackType.DIMENSIONAL for fb in result)

    def test_model_too_small(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="test",
            printability_report={
                "dimensions": {"width": 2, "depth": 2, "height": 2},
            },
        )
        assert len(result) >= 1
        assert any(fb.feedback_type == FeedbackType.DIMENSIONAL for fb in result)

    def test_severity_critical_for_extreme_overhang(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="test",
            printability_report={"max_overhang_angle": 80},
        )
        assert any(fb.severity == "critical" for fb in result)

    def test_multiple_issues_combined(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="test",
            failure_mode="adhesion",
            printability_report={
                "max_overhang_angle": 60,
                "min_wall_thickness": 0.5,
            },
        )
        # Should have both printability and structural feedback
        types = {fb.feedback_type for fb in result}
        assert FeedbackType.PRINTABILITY in types
        assert FeedbackType.STRUCTURAL in types

    def test_nested_kiln_reports_are_normalized(self):
        result = analyze_for_feedback(
            "/tmp/test.stl",
            original_prompt="a printable bracket",
            printability_report={
                "report": {
                    "overhangs": {"max_overhang_angle": 62},
                    "thin_walls": {"min_wall_thickness_mm": 0.8},
                    "bridging": {"bridge_count": 2},
                    "bed_adhesion": {"contact_percentage": 4.0},
                },
                "validation": {"is_manifold": False},
                "mesh_diagnostics": {
                    "has_floating_fragments": True,
                    "hole_count": 2,
                },
            },
        )
        types = {fb.feedback_type for fb in result}
        assert FeedbackType.PRINTABILITY in types
        assert FeedbackType.STRUCTURAL in types
        constraints = [c.lower() for fb in result for c in fb.constraints]
        assert any("overhang" in c for c in constraints)
        assert any("wall thickness" in c for c in constraints)
        assert any("bridge" in c for c in constraints)
        assert any("watertight" in c or "manifold" in c for c in constraints)
        assert any("continuous" in c or "floating" in c for c in constraints)
        assert any("base" in c for c in constraints)


class TestGenerateImprovedPrompt:
    """generate_improved_prompt adds constraints to prompts."""

    def test_no_feedback_returns_original(self):
        result = generate_improved_prompt("a simple cube", [])
        assert result.improved_prompt == "a simple cube"
        assert result.constraints_added == []

    def test_adds_constraints_suffix(self):
        fb = PrintFeedback(
            original_prompt="a vase",
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["overhangs"],
            constraints=["no overhangs greater than 45 degrees"],
            severity="moderate",
        )
        result = generate_improved_prompt("a vase", [fb])
        assert "Requirements:" in result.improved_prompt
        assert "overhangs" in result.improved_prompt.lower()

    def test_prompt_under_max_length(self):
        fb = PrintFeedback(
            original_prompt="a" * 500,
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["overhang"],
            constraints=["no overhangs greater than 45 degrees"],
            severity="moderate",
        )
        result = generate_improved_prompt("a" * 500, [fb])
        assert len(result.improved_prompt) <= _MAX_PROMPT_LENGTH

    def test_multiple_constraints_combined(self):
        fb1 = PrintFeedback(
            original_prompt="test",
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["overhangs"],
            constraints=["flat bottom"],
            severity="moderate",
        )
        fb2 = PrintFeedback(
            original_prompt="test",
            feedback_type=FeedbackType.STRUCTURAL,
            issues=["weak base"],
            constraints=["wide base for adhesion"],
            severity="critical",
        )
        result = generate_improved_prompt("test", [fb1, fb2])
        assert "flat bottom" in result.improved_prompt.lower()
        assert "wide base" in result.improved_prompt.lower()
        assert len(result.constraints_added) == 2

    def test_duplicate_constraints_deduplicated(self):
        fb1 = PrintFeedback("t", FeedbackType.PRINTABILITY, ["a"], ["flat base"], "moderate")
        fb2 = PrintFeedback("t", FeedbackType.STRUCTURAL, ["b"], ["flat base"], "critical")
        result = generate_improved_prompt("test", [fb1, fb2])
        assert result.constraints_added.count("flat base") == 1

    def test_iteration_tracked(self):
        result = generate_improved_prompt("test", [], iteration=3)
        assert result.iteration == 3

    def test_expected_improvements_populated(self):
        fb = PrintFeedback(
            original_prompt="test",
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["thin walls detected"],
            constraints=["min 2mm walls"],
            severity="moderate",
        )
        result = generate_improved_prompt("test", [fb])
        assert len(result.expected_improvements) > 0

    def test_very_long_prompt_trimmed(self):
        long_prompt = "a" * 1000
        fb = PrintFeedback(
            original_prompt=long_prompt,
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["overhang"],
            constraints=["flat bottom"],
            severity="moderate",
        )
        result = generate_improved_prompt(long_prompt, [fb])
        assert len(result.improved_prompt) <= _MAX_PROMPT_LENGTH


class TestFeedbackLoopPersistence:
    """Tests for feedback loop lifecycle with mock DB."""

    def _make_mock_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE feedback_loops (
                model_id TEXT PRIMARY KEY,
                original_prompt TEXT NOT NULL,
                iterations TEXT NOT NULL,
                current_iteration INTEGER DEFAULT 0,
                resolved BOOLEAN DEFAULT 0,
                best_iteration INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        db = MagicMock()
        db._conn = conn
        return db

    @patch("kiln.persistence.get_db")
    def test_start_feedback_loop(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        loop = start_feedback_loop("model-1", "a phone stand")
        assert loop.model_id == "model-1"
        assert loop.original_prompt == "a phone stand"
        assert loop.current_iteration == 0
        assert loop.resolved is False

    @patch("kiln.persistence.get_db")
    def test_add_iteration_success(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        start_feedback_loop("model-2", "a vase")
        loop = add_iteration("model-2", "a vase v2", ["thin walls"], "failed")
        assert loop.current_iteration == 1
        assert loop.resolved is False
        assert len(loop.iterations) == 1

    @patch("kiln.persistence.get_db")
    def test_add_iteration_resolves_on_success(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        start_feedback_loop("model-3", "a cube")
        add_iteration("model-3", "a cube v2", ["overhangs"], "failed")
        loop = add_iteration("model-3", "a cube v3", [], "success")
        assert loop.resolved is True
        assert loop.best_iteration == 2
        assert loop.current_iteration == 2

    @patch("kiln.persistence.get_db")
    def test_get_feedback_loop(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        start_feedback_loop("model-4", "test prompt")
        loop = get_feedback_loop("model-4")
        assert loop is not None
        assert loop.model_id == "model-4"

    @patch("kiln.persistence.get_db")
    def test_get_feedback_loop_not_found(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        loop = get_feedback_loop("nonexistent")
        assert loop is None

    @patch("kiln.persistence.get_db")
    def test_multiple_iterations_tracked(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        start_feedback_loop("model-5", "original")
        add_iteration("model-5", "v2", ["issue1"], "failed")
        add_iteration("model-5", "v3", ["issue2"], "failed")
        loop = add_iteration("model-5", "v4", [], "success")
        assert loop.current_iteration == 3
        assert len(loop.iterations) == 3
        assert loop.resolved is True


# ---------------------------------------------------------------------------
# Helpers for design-intelligence mocking
# ---------------------------------------------------------------------------


def _mock_material(design_limits=None, thermal=None, chemical=None, display_name="PLA"):
    """Create a mock material profile."""
    return SimpleNamespace(
        material_id="pla",
        display_name=display_name,
        design_limits=design_limits or {},
        thermal=thermal or {},
        chemical=chemical or {},
    )


def _mock_brief(material=None, constraints=None, patterns=None, guidance=None):
    """Create a mock DesignBrief."""
    mat_rec = None
    if material:
        mat_rec = SimpleNamespace(
            material=material,
            score=100.0,
            design_limits_summary=material.design_limits,
        )
    return SimpleNamespace(
        recommended_material=mat_rec,
        combined_rules=constraints or {},
        applicable_patterns=patterns or [],
        combined_guidance=guidance or [],
    )


def _mock_printer_profile(
    has_enclosure=False,
    has_direct_drive=True,
    build_volume=None,
    typical_tolerance_mm=0.15,
    max_print_speed_mm_s=200,
    default_layer_heights_mm=None,
):
    return SimpleNamespace(
        build_volume_mm=build_volume or {"x": 256, "y": 256, "z": 256},
        has_enclosure=has_enclosure,
        has_direct_drive=has_direct_drive,
        typical_tolerance_mm=typical_tolerance_mm,
        max_print_speed_mm_s=max_print_speed_mm_s,
        default_layer_heights_mm=default_layer_heights_mm or [0.12, 0.2, 0.28],
    )


# ---------------------------------------------------------------------------
# Provider-aware prompt limits
# ---------------------------------------------------------------------------


class TestEnhancePromptProvider:
    """enhance_prompt_with_design_intelligence honours provider limits."""

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_openscad_uses_100k_limit(self, mock_constraints, _mock_printer):
        mat = _mock_material(design_limits={"recommended_wall_thickness_mm": 1.6})
        mock_constraints.return_value = _mock_brief(material=mat, constraints={})
        result = enhance_prompt_with_design_intelligence(
            "a box", provider="openscad",
        )
        # With 100K budget, the prompt should NOT be truncated to 600 chars
        assert len(result.improved_prompt) <= 100_000

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_meshy_keeps_under_600(self, mock_constraints, _mock_printer):
        mat = _mock_material(design_limits={"recommended_wall_thickness_mm": 1.6})
        mock_constraints.return_value = _mock_brief(material=mat, constraints={})
        result = enhance_prompt_with_design_intelligence(
            "a box", provider="meshy",
        )
        assert len(result.improved_prompt) <= 600

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_none_provider_uses_default(self, mock_constraints, _mock_printer):
        mat = _mock_material()
        mock_constraints.return_value = _mock_brief(material=mat)
        result = enhance_prompt_with_design_intelligence("a box")
        assert len(result.improved_prompt) <= _MAX_PROMPT_LENGTH

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_explicit_max_length_overrides_provider(self, mock_constraints, _mock_printer):
        mat = _mock_material()
        mock_constraints.return_value = _mock_brief(material=mat)
        result = enhance_prompt_with_design_intelligence(
            "a box", provider="openscad", max_length=200,
        )
        assert len(result.improved_prompt) <= 200


class TestEnhancePromptDetailedConstraints:
    """Design-intelligence detailed constraints are injected correctly."""

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_min_hole_diameter_in_constraints(self, mock_constraints, _mock_printer):
        mat = _mock_material(design_limits={"min_hole_diameter_mm": 2.0})
        mock_constraints.return_value = _mock_brief(material=mat)
        result = enhance_prompt_with_design_intelligence(
            "a box", max_length=5000,
        )
        assert any("hole diameter" in c for c in result.constraints_added)

    @patch("kiln.design_intelligence.get_design_constraints")
    def test_warping_mitigation_no_enclosure(self, mock_constraints):
        mat = _mock_material(
            thermal={"warping_tendency": "high"},
            display_name="ABS",
        )
        brief = _mock_brief(material=mat)
        mock_constraints.return_value = brief
        printer = _mock_printer_profile(has_enclosure=False)
        with patch(
            "kiln.design_intelligence.get_printer_design_profile",
            return_value=printer,
        ):
            result = enhance_prompt_with_design_intelligence(
                "a case", printer_model="test_printer", max_length=5000,
            )
        assert any("warping" in c.lower() for c in result.constraints_added)

    @patch("kiln.design_intelligence.get_design_constraints")
    def test_bowden_constraint_for_tpu(self, mock_constraints):
        mat = _mock_material(display_name="TPU")
        brief = _mock_brief(material=mat)
        mock_constraints.return_value = brief
        printer = _mock_printer_profile(has_direct_drive=False)
        with patch(
            "kiln.design_intelligence.get_printer_design_profile",
            return_value=printer,
        ):
            result = enhance_prompt_with_design_intelligence(
                "a flexible grip",
                material="tpu",
                printer_model="test_printer",
                max_length=5000,
            )
        assert any("bowden" in c.lower() for c in result.constraints_added)

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_short_limit_only_core_constraints(self, mock_constraints, _mock_printer):
        mat = _mock_material(
            design_limits={
                "recommended_wall_thickness_mm": 1.6,
                "min_hole_diameter_mm": 2.0,
            },
        )
        mock_constraints.return_value = _mock_brief(material=mat)
        # 600-char budget → only core constraints, not detailed
        result = enhance_prompt_with_design_intelligence(
            "a box", max_length=600,
        )
        # min_hole_diameter is a "detailed" constraint, should be absent at 600
        assert not any("hole diameter" in c for c in result.constraints_added)


# ---------------------------------------------------------------------------
# build_parametric_generation_prompt
# ---------------------------------------------------------------------------


class TestBuildParametricPrompt:
    """build_parametric_generation_prompt produces OpenSCAD-ready prompts."""

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_contains_openscad_instructions(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        result = build_parametric_generation_prompt("a box")
        assert "Generate valid OpenSCAD code" in result.improved_prompt

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_material_limits_as_comments(self, mock_constraints, _p):
        mat = _mock_material(
            design_limits={
                "recommended_wall_thickness_mm": 1.6,
                "max_unsupported_overhang_deg": 50,
            },
        )
        mock_constraints.return_value = _mock_brief(material=mat)
        with patch(
            "kiln.design_intelligence.get_material_profile",
            return_value=SimpleNamespace(
                display_name="PLA",
                design_limits={
                    "recommended_wall_thickness_mm": 1.6,
                    "max_unsupported_overhang_deg": 50,
                },
            ),
        ):
            result = build_parametric_generation_prompt("a bracket", material="pla")
        assert "// Material: PLA" in result.improved_prompt
        assert "1.6mm" in result.improved_prompt

    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_includes_design_constraints(self, mock_constraints, _p):
        mat = _mock_material(design_limits={"recommended_wall_thickness_mm": 1.6})
        mock_constraints.return_value = _mock_brief(material=mat)
        with patch(
            "kiln.design_intelligence.get_material_profile",
            return_value=None,
        ):
            result = build_parametric_generation_prompt("a bracket", material="pla")
        assert len(result.constraints_added) > 0

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_basic_call_no_material_no_printer(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        result = build_parametric_generation_prompt("a simple box")
        assert result.original_prompt == "a simple box"
        assert "OpenSCAD" in result.improved_prompt


# ---------------------------------------------------------------------------
# build_parametric_generation_prompt with component matching
# ---------------------------------------------------------------------------


class TestBuildParametricPromptWithComponents:
    """build_parametric_generation_prompt integrates component catalog."""

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_gear_description_includes_bosl2(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        result = build_parametric_generation_prompt(
            "phone stand with gear mechanism"
        )
        assert "BOSL2" in result.improved_prompt
        assert "spur_gear" in result.improved_prompt.lower() or "Spur Gear" in result.improved_prompt

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_no_components_uses_pure_openscad(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        result = build_parametric_generation_prompt("simple rectangular box")
        assert "No external library dependencies" in result.improved_prompt

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_component_section_has_import_line(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        result = build_parametric_generation_prompt(
            "phone stand with gear mechanism"
        )
        assert "include <BOSL2" in result.improved_prompt

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_component_section_has_example(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        result = build_parametric_generation_prompt(
            "phone stand with gear mechanism"
        )
        assert "spur_gear(" in result.improved_prompt
