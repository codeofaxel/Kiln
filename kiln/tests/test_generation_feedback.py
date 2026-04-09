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
    PrinterGenerationContext,
    PrintFeedback,
    add_iteration,
    analyze_for_feedback,
    build_parametric_generation_prompt,
    enhance_prompt_with_design_intelligence,
    generate_improved_prompt,
    get_feedback_loop,
    resolve_printer_generation_context,
    start_feedback_loop,
    structural_risks_to_feedback,
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
        conn.row_factory = __import__("sqlite3").Row
        db = MagicMock()
        db._conn = conn
        db.execute = conn.execute
        db.commit = conn.commit
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


# ---------------------------------------------------------------------------
# structural_risks_to_feedback
# ---------------------------------------------------------------------------


def _mock_risk(
    risk_type: str = "thin_neck",
    severity: str = "warning",
    description: str = "Narrow cross-section at z=15mm",
):
    return SimpleNamespace(
        risk_type=risk_type,
        severity=severity,
        description=description,
    )


def _mock_load_analysis(
    layer_concern: str = "Load crosses layer boundaries",
    recommended: str = "on_side",
):
    return SimpleNamespace(
        layer_direction_concern=layer_concern,
        recommended_print_orientation=recommended,
    )


class TestStructuralRisksToFeedback:
    """structural_risks_to_feedback converts geometric risks to prompt constraints."""

    def test_empty_risks_returns_empty(self):
        result = structural_risks_to_feedback([], original_prompt="a bracket")
        assert result == []

    def test_no_risks_with_none_load_returns_empty(self):
        result = structural_risks_to_feedback(
            [], original_prompt="a bracket", load_analysis=None
        )
        assert result == []

    def test_single_risk_produces_feedback(self):
        risks = [_mock_risk()]
        result = structural_risks_to_feedback(risks, original_prompt="a bracket")
        assert len(result) == 1
        assert result[0].feedback_type == FeedbackType.STRUCTURAL
        assert result[0].severity == "moderate"
        assert len(result[0].issues) == 1
        assert "cross-section" in result[0].issues[0].lower()

    def test_critical_risk_sets_critical_severity(self):
        risks = [_mock_risk(severity="critical")]
        result = structural_risks_to_feedback(risks, original_prompt="test")
        assert result[0].severity == "critical"

    def test_multiple_risks_dedupe_constraints(self):
        risks = [
            _mock_risk(risk_type="thin_neck", description="Thin at z=15"),
            _mock_risk(risk_type="thin_neck", description="Thin at z=30"),
        ]
        result = structural_risks_to_feedback(risks, original_prompt="test")
        assert len(result) == 1
        assert len(result[0].issues) == 2
        # Same risk_type => same constraint, should not duplicate.
        assert len(result[0].constraints) == 1

    def test_different_risk_types_produce_multiple_constraints(self):
        risks = [
            _mock_risk(risk_type="thin_neck"),
            _mock_risk(risk_type="cantilever"),
            _mock_risk(risk_type="sharp_corner"),
        ]
        result = structural_risks_to_feedback(risks, original_prompt="test")
        assert len(result[0].constraints) == 3

    def test_load_analysis_adds_orientation_constraint(self):
        result = structural_risks_to_feedback(
            [],
            original_prompt="a bracket",
            load_analysis=_mock_load_analysis(),
        )
        assert len(result) == 1
        assert any("on_side" in c for c in result[0].constraints)
        assert any("layer" in issue.lower() for issue in result[0].issues)

    def test_risks_plus_load_analysis_combined(self):
        risks = [_mock_risk(risk_type="cantilever")]
        load = _mock_load_analysis()
        result = structural_risks_to_feedback(
            risks, original_prompt="test", load_analysis=load
        )
        assert len(result) == 1
        # Issues from both risk and load.
        assert len(result[0].issues) >= 2
        # Constraints from both risk and load.
        assert len(result[0].constraints) >= 2

    def test_dict_risks_accepted(self):
        """Accepts dict-form risks (e.g. from to_dict())."""
        risks = [
            {
                "risk_type": "stress_concentration",
                "severity": "warning",
                "description": "Abrupt section change",
            }
        ]
        result = structural_risks_to_feedback(risks, original_prompt="test")
        assert len(result) == 1
        assert "smooth" in result[0].constraints[0].lower()

    def test_dict_load_analysis_accepted(self):
        """Accepts dict-form load analysis."""
        load = {
            "layer_direction_concern": "Load crosses layers",
            "recommended_print_orientation": "upright",
        }
        result = structural_risks_to_feedback(
            [], original_prompt="test", load_analysis=load
        )
        assert len(result) == 1
        assert any("upright" in c for c in result[0].constraints)

    def test_unknown_risk_type_still_produces_issue(self):
        risks = [_mock_risk(risk_type="unknown_type", description="Something weird")]
        result = structural_risks_to_feedback(risks, original_prompt="test")
        assert len(result) == 1
        assert "Something weird" in result[0].issues[0]
        # No matching constraint for unknown type, but issues are still present.

    def test_feedback_integrates_with_improved_prompt(self):
        """Structural feedback can be fed into generate_improved_prompt."""
        risks = [_mock_risk(risk_type="cantilever", severity="critical")]
        fb = structural_risks_to_feedback(risks, original_prompt="a shelf bracket")
        improved = generate_improved_prompt("a shelf bracket", fb, iteration=1)
        assert "gusset" in improved.improved_prompt.lower() or "cantilever" in improved.improved_prompt.lower()
        assert improved.iteration == 1


# ---------------------------------------------------------------------------
# PrinterGenerationContext + resolve_printer_generation_context
# ---------------------------------------------------------------------------


class TestPrinterGenerationContext:
    """PrinterGenerationContext dataclass and serialization."""

    def test_defaults(self):
        ctx = PrinterGenerationContext()
        assert ctx.material is None
        assert ctx.nozzle_diameter_mm == 0.4
        assert ctx.material_source == ""

    def test_to_dict(self):
        ctx = PrinterGenerationContext(
            material="pla",
            material_source="ams",
            printer_model="bambu_a1",
        )
        d = ctx.to_dict()
        assert d["material"] == "pla"
        assert d["material_source"] == "ams"
        assert d["printer_model"] == "bambu_a1"

    def test_explicit_material_sets_source(self):
        ctx = PrinterGenerationContext(
            material="petg",
            material_source="user",
        )
        assert ctx.material_source == "user"


class TestResolvePrinterGenerationContext:
    """resolve_printer_generation_context with mocked printer state."""

    def test_explicit_material_wins(self):
        ctx = resolve_printer_generation_context(material="abs")
        assert ctx.material == "abs"
        assert ctx.material_source == "user"

    def test_no_printer_returns_defaults(self):
        ctx = resolve_printer_generation_context()
        # With no printer connected, should return defaults gracefully.
        assert ctx.nozzle_diameter_mm == 0.4
        # Material may or may not be resolved depending on printer availability.

    def test_common_failures_default_none(self):
        ctx = resolve_printer_generation_context()
        # Without a printer model, failures stay None.
        assert ctx.common_failures is None or isinstance(ctx.common_failures, list)


class TestEnhanceWithPrinterContext:
    """enhance_prompt_with_design_intelligence uses printer_context."""

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_failure_mitigations_injected(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        ctx = PrinterGenerationContext(
            common_failures=["adhesion", "warping"],
        )
        result = enhance_prompt_with_design_intelligence(
            "a phone stand",
            printer_context=ctx,
        )
        # Should contain adhesion and warping mitigations.
        lowered = result.improved_prompt.lower()
        assert "adhesion" in lowered or "brim" in lowered
        assert "warp" in lowered or "chamfer" in lowered

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_auto_material_from_context(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        ctx = PrinterGenerationContext(
            material="petg",
            material_source="ams",
        )
        enhance_prompt_with_design_intelligence(
            "a bracket",
            printer_context=ctx,
        )
        # material=None but printer_context provides "petg" —
        # design_constraints should be called with petg.
        call_kwargs = mock_constraints.call_args
        assert call_kwargs[1].get("material") == "petg" or call_kwargs.kwargs.get("material") == "petg"

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_explicit_material_overrides_context(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        ctx = PrinterGenerationContext(
            material="petg",
            material_source="ams",
        )
        enhance_prompt_with_design_intelligence(
            "a bracket",
            material="abs",
            printer_context=ctx,
        )
        # Explicit "abs" should override context's "petg".
        call_kwargs = mock_constraints.call_args
        assert call_kwargs[1].get("material") == "abs" or call_kwargs.kwargs.get("material") == "abs"

    @patch("kiln.design_intelligence.get_material_profile", return_value=None)
    @patch("kiln.design_intelligence.get_printer_design_profile", return_value=None)
    @patch("kiln.design_intelligence.get_design_constraints")
    def test_no_context_works_normally(self, mock_constraints, _p, _m):
        mock_constraints.return_value = _mock_brief()
        result = enhance_prompt_with_design_intelligence(
            "a simple cube",
        )
        assert "Requirements:" in result.improved_prompt


# ---------------------------------------------------------------------------
# AMS material auto-detection
# ---------------------------------------------------------------------------


class TestResolvePrinterContextAMS:
    """resolve_printer_generation_context auto-detects material from AMS."""

    @patch("kiln.server._get_adapter")
    def test_bambu_ams_material_detected(self, mock_adapter):
        """Auto-detect PLA from Bambu AMS tray_type."""
        adapter = MagicMock()
        adapter.get_printer_info.return_value = SimpleNamespace(
            build_volume={"x": 256, "y": 256, "z": 256},
            nozzle_diameter=0.4,
            model="bambu_a1",
        )
        adapter.get_ams_status.return_value = {
            "tray_now": "1",
            "units": [
                {
                    "trays": [
                        {"slot": 0, "tray_type": "PLA", "remain": 80},
                        {"slot": 1, "tray_type": "PETG", "remain": 60},
                    ]
                }
            ],
        }
        mock_adapter.return_value = adapter

        ctx = resolve_printer_generation_context()
        assert ctx.material == "petg"  # tray_now=1, slot 1 is PETG
        assert ctx.material_source == "ams"

    @patch("kiln.server._get_adapter")
    def test_ams_no_tray_now_uses_first(self, mock_adapter):
        """When tray_now is None, use first tray with material."""
        adapter = MagicMock()
        adapter.get_printer_info.return_value = SimpleNamespace(
            build_volume=None, nozzle_diameter=None, model=None,
        )
        adapter.get_ams_status.return_value = {
            "tray_now": None,
            "units": [{"trays": [{"slot": 0, "tray_type": "ABS"}]}],
        }
        mock_adapter.return_value = adapter

        ctx = resolve_printer_generation_context()
        assert ctx.material == "abs"
        assert ctx.material_source == "ams"

    @patch("kiln.server._get_adapter")
    def test_explicit_material_skips_ams(self, mock_adapter):
        """Explicit material= prevents AMS query entirely."""
        ctx = resolve_printer_generation_context(material="tpu")
        assert ctx.material == "tpu"
        assert ctx.material_source == "user"
        # Adapter should not be called when material is explicit.

    @patch("kiln.server._get_adapter")
    def test_no_ams_falls_back_gracefully(self, mock_adapter):
        """Adapters without get_ams_status don't crash."""
        adapter = MagicMock()
        adapter.get_printer_info.side_effect = Exception("offline")
        del adapter.get_ams_status  # Simulate adapter without AMS support
        mock_adapter.return_value = adapter

        ctx = resolve_printer_generation_context()
        assert ctx.material is None  # No detection, no crash

    @patch("kiln.server._get_adapter")
    def test_build_volume_resolved(self, mock_adapter):
        adapter = MagicMock()
        adapter.get_printer_info.return_value = SimpleNamespace(
            build_volume={"x": 180, "y": 180, "z": 180},
            nozzle_diameter=0.6,
            model="prusa_mini",
        )
        adapter.get_ams_status.side_effect = AttributeError
        mock_adapter.return_value = adapter

        ctx = resolve_printer_generation_context()
        assert ctx.build_volume_mm == {"x": 180.0, "y": 180.0, "z": 180.0}
        assert ctx.nozzle_diameter_mm == 0.6
        assert ctx.printer_model == "prusa_mini"


# ---------------------------------------------------------------------------
# Structural feedback → improved prompt integration test
# ---------------------------------------------------------------------------


class TestStructuralFeedbackIntegration:
    """Full cycle: risks → feedback → improved prompt with constraints."""

    def test_thin_neck_constraint_appears_in_prompt(self):
        risks = [
            _mock_risk(risk_type="thin_neck", severity="critical",
                       description="Cross-section at z=15mm is only 2mm²"),
        ]
        fb = structural_risks_to_feedback(risks, original_prompt="a shelf bracket")
        improved = generate_improved_prompt("a shelf bracket", fb, iteration=1)
        assert "cross-section" in improved.improved_prompt.lower() or "4mm" in improved.improved_prompt
        assert improved.iteration == 1
        assert len(improved.constraints_added) >= 1

    def test_multiple_risk_types_all_constrained(self):
        risks = [
            _mock_risk(risk_type="cantilever", description="Unsupported arm"),
            _mock_risk(risk_type="sharp_corner", description="Crack-prone edge"),
            _mock_risk(risk_type="insufficient_base", description="Topple risk"),
        ]
        load = _mock_load_analysis(recommended="on_side")
        fb = structural_risks_to_feedback(
            risks, original_prompt="a wall hook", load_analysis=load,
        )
        improved = generate_improved_prompt("a wall hook", fb, iteration=2)
        lowered = improved.improved_prompt.lower()
        assert "gusset" in lowered or "cantilever" in lowered
        assert "rounded" in lowered or "corner" in lowered or "crack" in lowered
        assert "base" in lowered or "stability" in lowered
        assert "on_side" in lowered
        assert improved.iteration == 2

    def test_no_risks_produces_no_constraints(self):
        fb = structural_risks_to_feedback([], original_prompt="a cube")
        improved = generate_improved_prompt("a cube", fb, iteration=1)
        # No feedback → prompt unchanged.
        assert improved.improved_prompt == "a cube"


# ---------------------------------------------------------------------------
# ASCII STL bounding box parser
# ---------------------------------------------------------------------------


class TestASCIISTLBoundingBox:
    """_bbox_from_ascii_stl parses vertex lines correctly."""

    def test_simple_triangle(self):
        from kiln.model_visualizer import _bbox_from_ascii_stl

        stl = (
            b"solid test\n"
            b"  facet normal 0 0 1\n"
            b"    outer loop\n"
            b"      vertex 0.0 0.0 0.0\n"
            b"      vertex 10.0 0.0 0.0\n"
            b"      vertex 5.0 10.0 5.0\n"
            b"    endloop\n"
            b"  endfacet\n"
            b"endsolid test\n"
        )
        info = _bbox_from_ascii_stl(stl)
        assert info.center_x == 5.0
        assert info.center_y == 5.0
        assert info.center_z == 2.5
        assert info.distance > 20  # diagonal of 10x10x5 ≈ 15, * 2.0 = 30

    def test_offset_geometry(self):
        from kiln.model_visualizer import _bbox_from_ascii_stl

        stl = (
            b"solid offset\n"
            b"  facet normal 0 0 1\n"
            b"    outer loop\n"
            b"      vertex 100.0 200.0 50.0\n"
            b"      vertex 150.0 200.0 50.0\n"
            b"      vertex 125.0 250.0 75.0\n"
            b"    endloop\n"
            b"  endfacet\n"
            b"endsolid offset\n"
        )
        info = _bbox_from_ascii_stl(stl)
        assert info.center_x == 125.0
        assert info.center_y == 225.0
        assert info.center_z == 62.5

    def test_empty_stl_returns_default(self):
        from kiln.model_visualizer import _bbox_from_ascii_stl

        info = _bbox_from_ascii_stl(b"solid empty\nendsolid empty\n")
        assert info.distance == 250  # default

    def test_scientific_notation_vertices(self):
        from kiln.model_visualizer import _bbox_from_ascii_stl

        stl = (
            b"solid sci\n"
            b"  facet normal 0 0 1\n"
            b"    outer loop\n"
            b"      vertex 1.5e1 2.0e1 0.0\n"
            b"      vertex 3.0e1 2.0e1 0.0\n"
            b"      vertex 2.25e1 4.0e1 1.0e1\n"
            b"    endloop\n"
            b"  endfacet\n"
            b"endsolid sci\n"
        )
        info = _bbox_from_ascii_stl(stl)
        # 15-30 in X, 20-40 in Y, 0-10 in Z
        assert abs(info.center_x - 22.5) < 0.01
        assert abs(info.center_y - 30.0) < 0.01
        assert abs(info.center_z - 5.0) < 0.01
