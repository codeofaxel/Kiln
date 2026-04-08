"""Stress tests for original_design.py new code paths on feature/provenance-qr-validation."""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test: OriginalDesignAudit dataclass has structural_analysis field
# ---------------------------------------------------------------------------
class TestOriginalDesignAuditDataclass:
    def _make_audit(self, **overrides):
        from kiln.original_design import OriginalDesignAudit
        defaults = dict(
            file_path="/tmp/test.stl",
            requirements_text="test",
            material="PLA",
            printer_model=None,
            build_volume_mm=None,
            readiness_score=85,
            readiness_grade="B",
            ready_for_print=True,
            blockers=[],
            next_actions=[],
            design_brief={},
            enhanced_prompt={},
            mesh_validation={},
            printability={},
            design_validation={},
            mesh_diagnostics=None,
            orientation=None,
            gates=[],
            feedback=[],
        )
        defaults.update(overrides)
        return OriginalDesignAudit(**defaults)

    def test_structural_analysis_defaults_to_none(self):
        audit = self._make_audit()
        assert audit.structural_analysis is None

    def test_structural_analysis_can_be_set(self):
        audit = self._make_audit(
            structural_analysis={"critical_count": 1, "overall_structural_score": 40},
        )
        assert audit.structural_analysis is not None
        assert audit.structural_analysis["critical_count"] == 1


# ---------------------------------------------------------------------------
# Test: structural gate logic
# ---------------------------------------------------------------------------
class TestStructuralIntegrityGate:
    """Test the gate computation logic extracted from audit_original_design."""

    def _build_gate(self, structural_plan: dict[str, Any] | None):
        """Replicate the gate-building logic from the diff."""
        from kiln.original_design import AuditGate
        gates = []
        if structural_plan is not None:
            struct_critical = structural_plan.get("critical_count", 0)
            struct_score = structural_plan.get("overall_structural_score", 100)
            struct_grade = structural_plan.get("structural_grade", "A")
            struct_passed = struct_critical == 0 and struct_score >= 60
            gates.append(
                AuditGate(
                    name="structural_integrity",
                    passed=struct_passed,
                    severity=(
                        "critical" if struct_critical > 0
                        else "warning" if struct_score < 70
                        else "info"
                    ),
                    message=(
                        f"Structural score {struct_score}/100 ({struct_grade}). "
                        + (
                            f"{struct_critical} critical risk(s) found."
                            if struct_critical > 0
                            else "No critical structural risks."
                        )
                    ),
                    details=structural_plan,
                )
            )
        return gates

    def test_no_structural_plan_no_gate(self):
        gates = self._build_gate(None)
        assert len(gates) == 0

    def test_clean_structural_plan_passes(self):
        gates = self._build_gate({
            "critical_count": 0,
            "warning_count": 0,
            "overall_structural_score": 85,
            "structural_grade": "B",
        })
        assert len(gates) == 1
        assert gates[0].passed is True
        assert gates[0].severity == "info"
        assert "No critical structural risks" in gates[0].message

    def test_critical_risks_fail_gate(self):
        gates = self._build_gate({
            "critical_count": 2,
            "warning_count": 1,
            "overall_structural_score": 30,
            "structural_grade": "F",
        })
        assert len(gates) == 1
        assert gates[0].passed is False
        assert gates[0].severity == "critical"
        assert "2 critical risk(s)" in gates[0].message

    def test_low_score_without_critical_is_warning(self):
        gates = self._build_gate({
            "critical_count": 0,
            "warning_count": 3,
            "overall_structural_score": 55,
            "structural_grade": "D",
        })
        assert len(gates) == 1
        # score < 60 means failed even without critical
        assert gates[0].passed is False
        assert gates[0].severity == "warning"

    def test_score_exactly_60_passes(self):
        gates = self._build_gate({
            "critical_count": 0,
            "overall_structural_score": 60,
            "structural_grade": "D",
        })
        assert gates[0].passed is True

    def test_score_59_fails(self):
        gates = self._build_gate({
            "critical_count": 0,
            "overall_structural_score": 59,
            "structural_grade": "D",
        })
        assert gates[0].passed is False


# ---------------------------------------------------------------------------
# Test: scoring penalty from structural risks
# ---------------------------------------------------------------------------
class TestStructuralScorePenalty:
    """Test the score deduction logic from the diff."""

    def _apply_penalty(self, score: int, structural_plan: dict[str, Any] | None) -> int:
        if structural_plan is not None:
            score -= structural_plan.get("critical_count", 0) * 15
            score -= structural_plan.get("warning_count", 0) * 5
        return max(0, min(100, score))

    def test_no_plan_no_penalty(self):
        assert self._apply_penalty(80, None) == 80

    def test_critical_heavy_penalty(self):
        assert self._apply_penalty(80, {"critical_count": 3, "warning_count": 0}) == 35

    def test_warning_light_penalty(self):
        assert self._apply_penalty(80, {"critical_count": 0, "warning_count": 2}) == 70

    def test_mixed_penalty(self):
        assert self._apply_penalty(80, {"critical_count": 1, "warning_count": 2}) == 55

    def test_penalty_floors_at_zero(self):
        assert self._apply_penalty(10, {"critical_count": 5, "warning_count": 5}) == 0

    def test_missing_keys_default_zero(self):
        assert self._apply_penalty(80, {}) == 80


# ---------------------------------------------------------------------------
# Test: next_actions from structural reinforcements
# ---------------------------------------------------------------------------
class TestStructuralNextActions:
    def _extract_next_actions(self, structural_plan: dict[str, Any] | None) -> list[str]:
        next_actions = []
        if structural_plan is not None:
            for rec in structural_plan.get("reinforcements", [])[:3]:
                desc = rec.get("description", "")
                if desc:
                    next_actions.append(desc)
        return next_actions

    def test_no_plan_no_actions(self):
        assert self._extract_next_actions(None) == []

    def test_extracts_descriptions(self):
        plan = {"reinforcements": [
            {"description": "Add ribs"},
            {"description": "Thicken walls"},
        ]}
        actions = self._extract_next_actions(plan)
        assert actions == ["Add ribs", "Thicken walls"]

    def test_max_three_actions(self):
        plan = {"reinforcements": [
            {"description": f"Fix {i}"} for i in range(10)
        ]}
        assert len(self._extract_next_actions(plan)) == 3

    def test_skips_empty_descriptions(self):
        plan = {"reinforcements": [
            {"description": ""},
            {"description": "Real fix"},
        ]}
        actions = self._extract_next_actions(plan)
        assert actions == ["Real fix"]

    def test_no_reinforcements_key(self):
        assert self._extract_next_actions({}) == []


# ---------------------------------------------------------------------------
# Test: resolve_printer_generation_context fallback logic
# ---------------------------------------------------------------------------
class TestPrinterContextFallback:
    """Verify that material/printer_model from printer context is used when user doesn't provide them."""

    def test_fallback_uses_context_when_material_none(self):
        """The `material or ctx.material` pattern should pick ctx when material is None."""
        material = None
        ctx_material = "PETG"
        effective = material or ctx_material
        assert effective == "PETG"

    def test_no_fallback_when_material_provided(self):
        material = "ABS"
        ctx_material = "PETG"
        effective = material or ctx_material
        assert effective == "ABS"

    def test_fallback_uses_context_when_printer_model_none(self):
        printer_model = None
        ctx_printer = "Bambu A1"
        effective = printer_model or ctx_printer
        assert effective == "Bambu A1"

    def test_resolve_function_importable(self):
        """resolve_printer_generation_context should be importable."""
        from kiln.generation_feedback import resolve_printer_generation_context
        assert callable(resolve_printer_generation_context)

    def test_structural_risks_to_feedback_importable(self):
        """structural_risks_to_feedback should be importable."""
        from kiln.generation_feedback import structural_risks_to_feedback
        assert callable(structural_risks_to_feedback)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
