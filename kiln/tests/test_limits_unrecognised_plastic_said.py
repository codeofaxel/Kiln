"""A plastic Kiln cannot place is said once, on every check that read it.

A name Kiln's design limits cannot place ("Acme ABS", "ABS+") is answered
with the general figures.  The OpenSCAD parameter check and the design
validator passed such a part silently, as if the plastic had been read.
Every check reads the name through the one helper, and its reply says once
that Kiln does not recognise it.  A miss (no answer at all) says nothing
of the kind: nothing was asked to place it.  Stood in here: an install
without kiln-pro whose server answers.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from kiln import material_limits

UNRECOGNISED = (
    "Kiln doesn't recognise the plastic named, so the checks use the general figures; "
    "name it the way Kiln lists its plastics (PLA, PETG, ABS, ...) to check it against its own."
)


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _answer(material_id: str | None) -> dict[str, Any]:
    own = material_id == "abs"
    return {
        "success": True,
        "status": "ok",
        "known": material_id is not None,
        "material_id": material_id,
        "scenario": {"nozzle_mm": 0.4},
        "limits": {
            "overhang_unsupported_deg": {"value": 40.0 if own else 45.0, "unit": "deg", "basis": "family" if own else "general"},
            "bridge_max_mm": {"value": 10.0, "unit": "mm", "basis": "general"},
            "wall_recommended_mm": {"value": 2.0 if own else 1.2, "unit": "mm", "basis": "family" if own else "general"},
            "pin_min_mm": {"value": 3.0, "unit": "mm", "basis": "general"},
            "esd_wall_min_mm": {"value": None, "unit": "mm", "basis": "not_applicable"},
        },
        "note": "" if material_id else "Kiln has no figures of its own for this material, so these are the general figures.",
    }


@pytest.fixture
def server(monkeypatch):
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    state = {"down": False}

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        if state["down"]:
            return {"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "why": "unanswered"}
        if tool_name != "material_limits" or "quantities" in kwargs:
            return {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"}
        name = str(kwargs.get("material")).strip().lower()
        return _answer("abs" if name == "abs" else None)

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    yield state
    _forget()


def _call(name: str, fn, *args: Any, **kwargs: Any) -> Any:
    from kiln.reading_notes import attach
    from kiln.tool_context import enter_tool, leave_tool

    token = enter_tool(name, kwargs)
    try:
        return attach(fn(*args, **kwargs))
    finally:
        leave_tool(token)


def _design_tools() -> dict[str, Any]:
    import importlib

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    importlib.import_module("kiln.plugins.design_tools").plugin.register(_Mcp())
    return tools


def _cup(folder) -> str:
    trimesh = pytest.importorskip("trimesh")
    part = folder / "box.stl"
    trimesh.creation.box(extents=(20, 20, 10)).export(part)
    return str(part)


@pytest.mark.parametrize("name", ["Acme ABS", "ABS+"])
def test_the_parameter_check_and_the_validator_say_it_once(server, tmp_path, name):
    tools = _design_tools()
    scad = _call(
        "validate_scad_parameters", tools["validate_scad_parameters"], "wall_thickness = 1.3;\ncube(10);", material=name,
    )
    assert json.dumps(scad).count(UNRECOGNISED) == 1, scad
    _forget()
    checked = _call(
        "validate_design_for_requirements", tools["validate_design_for_requirements"],
        _cup(tmp_path), "a desk cup", material=name,
    )
    assert json.dumps(checked).count(UNRECOGNISED) == 1, checked


def test_a_plastic_kiln_places_says_nothing_of_the_kind(server):
    tools = _design_tools()
    scad = _call(
        "validate_scad_parameters", tools["validate_scad_parameters"], "wall_thickness = 1.3;\ncube(10);", material="ABS",
    )
    assert UNRECOGNISED not in json.dumps(scad)


def test_a_miss_does_not_call_the_plastic_unrecognised(server):
    server["down"] = True
    assert UNRECOGNISED not in material_limits.limits_for("Acme ABS").notes()


def test_the_sentence_is_the_helpers_own():
    assert material_limits.UNRECOGNISED == UNRECOGNISED
