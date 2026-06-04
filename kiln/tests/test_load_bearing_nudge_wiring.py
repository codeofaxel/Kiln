"""Wiring tests for the free-tier load-bearing upgrade nudge.

The detector emits ``engineering_grade: "heuristic"`` + ``pro_upgrade``; the
three free-tier estimator tools must attach it to their response when the
part is load-bearing — and stay silent on cosmetic cases.

Covers:
- is_engineering_material / load_bearing_signal — fires + suppressions
- attach_load_bearing_nudge — force, signal, no-op, error response
- estimate_structural_load — always nudges (definitionally structural)
- get_joint_recommendation — nudges on engineering material / press-fit;
  silent on PLA clearance / snap fits
- validate_assembly — nudges on engineering material / screw-into-printed;
  silent on a PLA snap-fit lid
"""

from __future__ import annotations

import json

import pytest

from kiln.load_bearing_detector import (
    _LOW_LOAD_THRESHOLD_N,
    attach_load_bearing_nudge,
    is_engineering_material,
    load_bearing_signal,
)


# ---------------------------------------------------------------------------
# Helpers — capture the registered tool functions off a fake MCP
# ---------------------------------------------------------------------------


def _capture_tools(plugin_module: str) -> dict:
    import importlib

    plugin = importlib.import_module(plugin_module).plugin
    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(FakeMCP())
    return tools


# ---------------------------------------------------------------------------
# Predicate — is_engineering_material
# ---------------------------------------------------------------------------


class TestIsEngineeringMaterial:
    def test_nylon_is_engineering(self):
        assert is_engineering_material("nylon")

    def test_pa6_cf_is_engineering(self):
        assert is_engineering_material("PA6-CF")

    def test_polycarbonate_is_engineering(self):
        assert is_engineering_material("Polycarbonate")

    def test_pla_is_not(self):
        assert not is_engineering_material("PLA")

    def test_petg_is_not(self):
        assert not is_engineering_material("PETG")

    def test_none_is_not(self):
        assert not is_engineering_material(None)

    def test_empty_is_not(self):
        assert not is_engineering_material("")


# ---------------------------------------------------------------------------
# Predicate — load_bearing_signal
# ---------------------------------------------------------------------------


class TestLoadBearingSignal:
    def test_engineering_material_fires(self):
        assert load_bearing_signal(material="nylon")

    def test_press_fit_fires(self):
        assert load_bearing_signal(joint_type="press_fit")

    def test_threaded_fires(self):
        assert load_bearing_signal(joint_type="threaded")

    def test_load_at_threshold_fires(self):
        assert load_bearing_signal(applied_load_n=_LOW_LOAD_THRESHOLD_N)

    def test_pla_clearance_silent(self):
        assert not load_bearing_signal(material="PLA", joint_type="clearance_fit")

    def test_pla_snap_silent(self):
        assert not load_bearing_signal(material="PLA", joint_type="snap_fit")

    def test_load_below_threshold_silent(self):
        assert not load_bearing_signal(applied_load_n=_LOW_LOAD_THRESHOLD_N - 1)

    def test_all_none_silent(self):
        assert not load_bearing_signal()


# ---------------------------------------------------------------------------
# attach_load_bearing_nudge
# ---------------------------------------------------------------------------


class TestAttachLoadBearingNudge:
    def test_force_attaches_structured_and_string(self):
        out = attach_load_bearing_nudge({"success": True}, force=True)
        assert out["upgrade_recommendation"]["engineering_grade"] == "heuristic"
        assert out["upgrade_recommendation"]["pro_upgrade"]["upgrade_url"]
        assert isinstance(out["load_bearing_note"], str) and out["load_bearing_note"]

    def test_signal_attaches(self):
        out = attach_load_bearing_nudge({"success": True}, material="nylon")
        assert "upgrade_recommendation" in out

    def test_no_signal_is_noop(self):
        out = attach_load_bearing_nudge(
            {"success": True}, material="PLA", joint_type="snap_fit"
        )
        assert "upgrade_recommendation" not in out
        assert "load_bearing_note" not in out

    def test_error_response_is_noop(self):
        out = attach_load_bearing_nudge({"success": False, "error": "x"}, force=True)
        assert "upgrade_recommendation" not in out


# ---------------------------------------------------------------------------
# Tool wiring — estimate_structural_load (always)
# ---------------------------------------------------------------------------


class TestEstimateStructuralLoadWiring:
    def test_always_nudges_even_for_pla(self):
        tools = _capture_tools("kiln.plugins.design_tools")
        res = tools["estimate_structural_load"](
            material="PLA", cross_section_mm2=20.0, cantilever_length_mm=30.0
        )
        assert res["success"] is True
        assert "upgrade_recommendation" in res
        assert res["upgrade_recommendation"]["engineering_grade"] == "heuristic"


# ---------------------------------------------------------------------------
# Tool wiring — get_joint_recommendation (conditional)
# ---------------------------------------------------------------------------


class TestGetJointRecommendationWiring:
    def test_pla_clearance_fit_is_silent(self):
        tools = _capture_tools("kiln.plugins.assembly_tools")
        res = tools["get_joint_recommendation"](
            joint_type="clearance_fit", material_a="PLA", material_b="PLA"
        )
        assert res["success"] is True
        assert "upgrade_recommendation" not in res

    def test_engineering_material_nudges(self):
        tools = _capture_tools("kiln.plugins.assembly_tools")
        res = tools["get_joint_recommendation"](
            joint_type="clearance_fit", material_a="nylon", material_b="PLA"
        )
        assert "upgrade_recommendation" in res

    def test_press_fit_nudges(self):
        tools = _capture_tools("kiln.plugins.assembly_tools")
        res = tools["get_joint_recommendation"](
            joint_type="press_fit", material_a="PLA", material_b="PLA"
        )
        assert "upgrade_recommendation" in res


# ---------------------------------------------------------------------------
# Tool wiring — validate_assembly (conditional)
# ---------------------------------------------------------------------------


def _no_clearances(asm, **_kw):
    asm.clearance_checks = []
    return []


class TestValidateAssemblyWiring:
    def test_engineering_material_part_nudges(self, monkeypatch):
        from kiln import assembly as assembly_mod
        from kiln.assembly import Assembly, AssemblyPart

        monkeypatch.setattr(assembly_mod, "check_all_clearances", _no_clearances)
        tools = _capture_tools("kiln.plugins.assembly_tools")
        asm = Assembly(assembly_id="a1", name="A")
        asm.parts.extend([
            AssemblyPart(part_id="base", file_path="/tmp/b.stl", material="nylon"),
            AssemblyPart(part_id="lid", file_path="/tmp/l.stl", material="PLA"),
        ])
        res = tools["validate_assembly"](assembly_json=json.dumps(asm.to_dict()))
        assert res["success"] is True
        assert "upgrade_recommendation" in res

    def test_pla_snap_fit_lid_is_silent(self, monkeypatch):
        from kiln import assembly as assembly_mod
        from kiln.assembly import Assembly, AssemblyPart, MatingInterface

        monkeypatch.setattr(assembly_mod, "check_all_clearances", _no_clearances)
        tools = _capture_tools("kiln.plugins.assembly_tools")
        asm = Assembly(assembly_id="a2", name="B")
        asm.parts.extend([
            AssemblyPart(part_id="base", file_path="/tmp/b.stl", material="PLA"),
            AssemblyPart(part_id="lid", file_path="/tmp/l.stl", material="PLA"),
        ])
        asm.interfaces.append(MatingInterface(
            part_a_id="base", part_b_id="lid",
            joint_type="snap_fit", clearance_mm=0.15,
        ))
        res = tools["validate_assembly"](assembly_json=json.dumps(asm.to_dict()))
        assert res["success"] is True
        assert "upgrade_recommendation" not in res

    def test_screw_into_printed_nudges(self, monkeypatch):
        from kiln import assembly as assembly_mod
        from kiln.assembly import (
            Assembly,
            AssemblyPart,
            FastenerSpec,
            MatingInterface,
        )

        monkeypatch.setattr(assembly_mod, "check_all_clearances", _no_clearances)
        tools = _capture_tools("kiln.plugins.assembly_tools")
        asm = Assembly(assembly_id="a3", name="C")
        asm.parts.extend([
            AssemblyPart(part_id="base", file_path="/tmp/b.stl", material="PLA"),
            AssemblyPart(part_id="lid", file_path="/tmp/l.stl", material="PLA"),
        ])
        asm.interfaces.append(MatingInterface(
            part_a_id="base", part_b_id="lid",
            joint_type="clearance_fit", clearance_mm=2.0,
            fastener_spec=FastenerSpec(
                size="M3", family="metric_machine_screw", surface_type="printed",
            ),
        ))
        res = tools["validate_assembly"](assembly_json=json.dumps(asm.to_dict()))
        assert res["success"] is True
        assert "upgrade_recommendation" in res
