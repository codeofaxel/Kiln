"""Tests for kiln.generation_feedback.

Coverage areas:
- FeedbackType enum values
- PrintFeedback, ImprovedPrompt, FeedbackLoop dataclasses
- analyze_for_feedback with various printability issues
- generate_improved_prompt constraint application
- Feedback loop lifecycle (start, add iteration, get)
- Prompt length limits
- Edge cases: no issues, empty feedback, long prompts
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from kiln.generation_feedback import (
    _MAX_PROMPT_LENGTH,
    FeedbackLoop,
    FeedbackType,
    ImprovedPrompt,
    PrintFeedback,
    add_iteration,
    analyze_for_feedback,
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
