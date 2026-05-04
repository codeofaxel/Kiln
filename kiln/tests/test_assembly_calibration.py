"""Tests for the calibration wire-up in :func:`kiln.assembly.get_clearance_recommendation`.

Covers the additive ``printer_id`` parameter that lets a Pro+ caller
narrow the joint clearance range based on their printer's calibration
tier.  Free-tier callers (no ``printer_id``, or kiln-pro not installed)
get the historic flat-range behaviour exactly — every test below must
respect that contract or it fails the regression-guard rule.

The tests stub out ``kiln_pro.engineering.calibration_coach`` via
``monkeypatch`` so they run in either environment (kiln-pro installed
or not).  When kiln-pro IS installed the import succeeds and the stub
replaces the lookup with a deterministic verdict; when kiln-pro is
NOT installed the import fails and the function falls back to the
historic path — both are exercised here.

Skip semantics: these tests do NOT need the engineering overlay
(materials.json moat fields).  They only need
``kiln_pro.engineering.calibration_coach`` to be importable, and
several tests stub it explicitly.  The ``requires_engineering_overlay``
marker is therefore intentionally NOT applied here.
"""

from __future__ import annotations

import importlib

import pytest

from kiln import assembly as assembly_mod
from kiln.assembly import get_clearance_recommendation


# ---------------------------------------------------------------------------
# Stub helpers — single source for test calibration verdicts
# ---------------------------------------------------------------------------


def _make_verdict_block(tier: str, accuracy_mm: float) -> dict:
    """Mimic the shape of ``calibration_used_block`` for a stub verdict."""
    return {
        "printer_id": "bambu_a1",
        "tier": tier,
        "expected_accuracy_mm": accuracy_mm,
        "source": (
            "imported from OrcaSlicer config (test_pla)"
            if tier in ("high", "medium")
            else "no calibration data found; generic process defaults applied"
        ),
        "xy_compensation_mm": 0.05 if tier == "high" else None,
        "flow_rate": 0.95 if tier == "high" else None,
        "pressure_advance": 0.018 if tier == "high" else None,
    }


def _stub_calibration(monkeypatch, tier: str, accuracy_mm: float) -> dict:
    """Patch the lazy import target to return a fixed verdict block.

    The wire-up does ``from kiln_pro.engineering.calibration_coach
    import calibration_for, calibration_used_block`` inside the
    function, so we patch the module's attributes directly.  The stub
    is used by ``_calibration_view_for_clearance``.
    """
    cc = pytest.importorskip("kiln_pro.engineering.calibration_coach")

    block = _make_verdict_block(tier, accuracy_mm)

    class _StubVerdict:
        def __init__(self) -> None:
            self.tier = tier
            self.expected_accuracy_mm = accuracy_mm

    monkeypatch.setattr(
        cc, "calibration_for",
        lambda printer_id, material=None, **_: _StubVerdict(),
    )
    monkeypatch.setattr(
        cc, "calibration_used_block",
        lambda verdict, *, printer_id: block,
    )
    return block


# ---------------------------------------------------------------------------
# Regression: behaviour with no printer_id is identical to historic path
# ---------------------------------------------------------------------------


class TestNoPrinterIdRegression:
    """The function must behave exactly as before when ``printer_id`` is omitted.

    This is the contract that protects free users — even if kiln-pro is
    installed locally, a caller that didn't ask for calibration must
    not get calibration-narrowed ranges back.
    """

    def test_snap_fit_pla_no_printer_id_returns_historic_range(self):
        rec = get_clearance_recommendation("snap_fit", material_a="PLA", material_b="PLA")
        assert rec["clearance_range_mm"] == [0.1, 0.3]
        # Midpoint of historic range
        assert rec["recommended_clearance_mm"] == pytest.approx(0.2)
        # Half-width of historic range
        assert rec["tolerance_mm"] == pytest.approx(0.1)

    def test_press_fit_no_printer_id_returns_negative_range(self):
        rec = get_clearance_recommendation("press_fit")
        # Press fit clearance is negative (interference)
        assert rec["clearance_range_mm"][0] == pytest.approx(-0.2)
        assert rec["clearance_range_mm"][1] == pytest.approx(-0.05)

    def test_calibration_used_field_present_and_empty_without_printer_id(self):
        """Field shape contract — always present, empty dict when no calibration."""
        rec = get_clearance_recommendation("snap_fit")
        assert "calibration_used" in rec
        assert rec["calibration_used"] == {}


# ---------------------------------------------------------------------------
# Calibration narrowing behaviour
# ---------------------------------------------------------------------------


class TestCalibrationNarrowing:
    """When a calibration tier is HIGH or MEDIUM, the range narrows."""

    def test_snap_fit_high_calibration_narrows_range(self, monkeypatch):
        block = _stub_calibration(monkeypatch, tier="high", accuracy_mm=0.10)
        rec = get_clearance_recommendation(
            "snap_fit", material_a="PLA", material_b="PLA",
            printer_id="bambu_a1",
        )
        # Original snap-fit half-width is 0.10mm (range 0.1-0.3).
        # HIGH calibration with ±0.10mm accuracy halves the half-width
        # to 0.05mm centered on midpoint 0.20 → 0.15-0.25 range.
        assert rec["clearance_range_mm"] == [pytest.approx(0.15), pytest.approx(0.25)]
        assert rec["tolerance_mm"] == pytest.approx(0.05)
        # Midpoint stays the same — narrowing is symmetric.
        assert rec["recommended_clearance_mm"] == pytest.approx(0.2)
        # calibration_used carries the verdict block
        assert rec["calibration_used"] == block
        assert rec["calibration_used"]["tier"] == "high"

    def test_press_fit_high_calibration_keeps_negative_sign(self, monkeypatch):
        """Press-fit is interference (negative); narrowing must not flip the sign."""
        _stub_calibration(monkeypatch, tier="high", accuracy_mm=0.10)
        rec = get_clearance_recommendation(
            "press_fit", material_a="PLA", material_b="PLA",
            printer_id="bambu_a1",
        )
        lo, hi = rec["clearance_range_mm"]
        # Original press_fit range -0.2 to -0.05 → midpoint -0.125,
        # half-width 0.075.  HIGH narrows half-width to 0.0375
        # → range -0.1625 to -0.0875.  Result is rounded to 3dp for
        # the response payload (so -0.163 / -0.088 in the dict).
        assert lo < 0, f"low end should remain negative, got {lo}"
        assert hi < 0, f"high end should remain negative, got {hi}"
        assert lo == pytest.approx(-0.163, abs=1e-3)
        assert hi == pytest.approx(-0.087, abs=1e-3)

    def test_low_calibration_leaves_range_unchanged(self, monkeypatch):
        """LOW calibration narrow_factor is 1.0 (no shrinking)."""
        _stub_calibration(monkeypatch, tier="low", accuracy_mm=0.20)
        rec = get_clearance_recommendation(
            "snap_fit", material_a="PLA", material_b="PLA",
            printer_id="bambu_a1",
        )
        # Range identical to historic flat range
        assert rec["clearance_range_mm"] == [pytest.approx(0.1), pytest.approx(0.3)]
        assert rec["tolerance_mm"] == pytest.approx(0.1)
        # But calibration_used is still attached and tagged "low"
        assert rec["calibration_used"]["tier"] == "low"

    def test_unknown_calibration_leaves_range_unchanged(self, monkeypatch):
        """UNKNOWN tier never narrows — widening would mislead."""
        _stub_calibration(monkeypatch, tier="unknown", accuracy_mm=0.30)
        rec = get_clearance_recommendation(
            "snap_fit", material_a="PLA", material_b="PLA",
            printer_id="bambu_a1",
        )
        assert rec["clearance_range_mm"] == [pytest.approx(0.1), pytest.approx(0.3)]
        assert rec["calibration_used"]["tier"] == "unknown"


# ---------------------------------------------------------------------------
# Material warnings — orthogonal to calibration, must still fire
# ---------------------------------------------------------------------------


class TestMaterialWarningsStillFire:
    """Calibration tightening must NOT suppress brittle/flexible warnings."""

    def test_brittle_warning_with_high_calibration(self, monkeypatch):
        _stub_calibration(monkeypatch, tier="high", accuracy_mm=0.10)
        rec = get_clearance_recommendation(
            "snap_fit", material_a="PLA", material_b="PLA",
            printer_id="bambu_a1",
        )
        warning_text = " ".join(rec["warnings"])
        assert "Brittle material" in warning_text
        assert "PLA" in warning_text or "snap" in warning_text.lower()

    def test_flexible_widens_clearance_after_calibration_narrowing(self, monkeypatch):
        """Flexible materials still get the +50% bump on top of the calibration narrow."""
        _stub_calibration(monkeypatch, tier="high", accuracy_mm=0.10)
        rec = get_clearance_recommendation(
            "snap_fit", material_a="TPU", material_b="PLA",
            printer_id="bambu_a1",
        )
        # Flexible bumps base_clearance by 1.5x — verify mid still
        # rises above the calibration-narrowed midpoint.
        assert rec["recommended_clearance_mm"] > 0.20
        assert "flexible material" in rec["rationale"].lower()


# ---------------------------------------------------------------------------
# calibration_used field shape
# ---------------------------------------------------------------------------


class TestCalibrationUsedShape:
    """Field shape MUST match the engineering-tools wire-up exactly."""

    def test_calibration_used_carries_standard_keys(self, monkeypatch):
        block = _stub_calibration(monkeypatch, tier="high", accuracy_mm=0.10)
        rec = get_clearance_recommendation(
            "snap_fit", printer_id="bambu_a1",
        )
        cu = rec["calibration_used"]
        # Identity check — the function returns the exact block from
        # calibration_used_block, no re-shaping.
        assert cu is block
        # Spot-check the contract keys.  These must match
        # calibration_coach.calibration_used_block().
        assert "printer_id" in cu
        assert "tier" in cu
        assert "expected_accuracy_mm" in cu
        assert "source" in cu


# ---------------------------------------------------------------------------
# Graceful degradation when kiln-pro is not importable
# ---------------------------------------------------------------------------


class TestImportErrorDegradation:
    """When kiln-pro can't be imported, fall back to historic behaviour silently."""

    def test_import_error_returns_historic_range(self, monkeypatch):
        # Force the lazy import inside _calibration_view_for_clearance
        # to raise ImportError by hiding the calibration_coach module.
        import sys as _sys

        # Snapshot which kiln_pro modules are loaded so we can restore.
        hidden_modules = {
            name: _sys.modules.pop(name)
            for name in list(_sys.modules)
            if name.startswith("kiln_pro.engineering.calibration_coach")
            or name == "kiln_pro.engineering.calibration_coach"
        }

        original_import = builtins_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def _fail_calibration_import(name, *args, **kwargs):
            if name == "kiln_pro.engineering.calibration_coach":
                raise ImportError("kiln_pro not installed for this test")
            if name.startswith("kiln_pro.engineering.calibration_coach."):
                raise ImportError("kiln_pro not installed for this test")
            return original_import(name, *args, **kwargs)

        # Patch builtins.__import__ via monkeypatch.setitem so the
        # change is auto-reverted at test teardown.
        import builtins as _builtins
        monkeypatch.setattr(_builtins, "__import__", _fail_calibration_import)

        try:
            rec = get_clearance_recommendation(
                "snap_fit", printer_id="bambu_a1",
            )
            # Same shape as the no-printer-id path: historic range,
            # empty calibration_used.
            assert rec["clearance_range_mm"] == [0.1, 0.3]
            assert rec["calibration_used"] == {}
        finally:
            # Restore hidden modules (for any tests run after this one)
            for name, module in hidden_modules.items():
                _sys.modules[name] = module


# ---------------------------------------------------------------------------
# Smoke: assembly_tools MCP wrapper forwards printer_id
# ---------------------------------------------------------------------------


class TestAssemblyToolsForwarding:
    """The MCP tool wrapper must pass ``printer_id`` through to the engine."""

    def test_mcp_tool_passes_printer_id(self, monkeypatch):
        # We don't need a real MCP server — just verify the wrapper
        # function delegates correctly by patching the engine.
        called = {}

        def _fake_engine(joint_type, material_a, material_b, *, printer_id=None):
            called["joint_type"] = joint_type
            called["printer_id"] = printer_id
            return {"recommended_clearance_mm": 0.2, "calibration_used": {}}

        monkeypatch.setattr(assembly_mod, "get_clearance_recommendation", _fake_engine)

        # Re-import the tools module so the patched function is picked up
        from kiln.plugins import assembly_tools as at_mod
        importlib.reload(at_mod)

        # Build a tiny stub MCP that captures registered tools
        registered: dict = {}

        class _StubMCP:
            def tool(self, *args, **kwargs):
                def _decorator(fn):
                    registered[fn.__name__] = fn
                    return fn
                return _decorator

        plugin = at_mod.plugin
        plugin.register(_StubMCP())

        # Call the MCP wrapper with printer_id
        get_joint_recommendation = registered["get_joint_recommendation"]
        result = get_joint_recommendation(
            joint_type="snap_fit",
            material_a="PLA",
            material_b="PLA",
            printer_id="bambu_a1",
        )
        assert result["success"] is True
        assert called["printer_id"] == "bambu_a1"
        assert called["joint_type"] == "snap_fit"
