"""The design audit says what its checks read about a plastic once.

The audit carries the design validator's report whole, and beside it a gate
and a reason that repeat what the validator found.  The validator's summary
also says why its figures were the general ones (the day's allowance used)
and what they leave out for a flexible plastic.  The report says it; the
gate and the reason repeat only what the checks found, so the reply says
each once.  Stood in here: an install without kiln-pro whose server answers
past the allowance.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from kiln import material_limits

LINE = (
    "Checked against the general figures: your plan checks 6 plastics a day against their own design "
    "limits, today's are used, and they renew at midnight UTC. Kiln Business has no daily limit: "
    "https://kiln3d.com/pricing"
)
FLEXIBLE = (
    "The general figures are for ordinary rigid plastics; a flexible "
    "plastic needs more margin than they give."
)


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _past_the_allowance(material_id: str) -> dict[str, Any]:
    return {
        "success": True,
        "status": "ok",
        "known": True,
        "material_id": material_id,
        "scenario": {"nozzle_mm": 0.4},
        "limits": {
            "overhang_unsupported_deg": {"value": 45.0, "unit": "deg", "basis": "general"},
            "bridge_max_mm": {"value": 10.0, "unit": "mm", "basis": "general"},
            "wall_recommended_mm": {"value": 1.2, "unit": "mm", "basis": "general"},
            "pin_min_mm": {"value": 3.0, "unit": "mm", "basis": "general"},
            "esd_wall_min_mm": {"value": None, "unit": "mm", "basis": "not_applicable"},
        },
        "note": "",
        "allowance": LINE,
    }


@pytest.fixture(autouse=True)
def served(monkeypatch):
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        if tool_name != "material_limits" or "quantities" in kwargs:
            return {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"}
        name = str(kwargs.get("material")).strip().lower().replace(" ", "_")
        return _past_the_allowance(name)

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    yield
    _forget()


def _audit(tmp_path, material: str) -> dict[str, Any]:
    trimesh = pytest.importorskip("trimesh")
    import importlib

    from kiln.reading_notes import attach
    from kiln.tool_context import enter_tool, leave_tool

    part = tmp_path / "cup.stl"
    body = trimesh.creation.box(extents=(30, 30, 25))
    inner = trimesh.creation.box(extents=(27.4, 27.4, 25))
    inner.apply_translation((0, 0, 2))
    body.difference(inner).export(part)
    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    importlib.import_module("kiln.plugins.design_tools").plugin.register(_Mcp())
    token = enter_tool("audit_original_design", {})
    try:
        return attach(tools["audit_original_design"](str(part), "a desk cup that holds pens", material=material))
    finally:
        leave_tool(token)


def test_the_plans_line_is_said_once(tmp_path):
    pytest.importorskip("manifold3d")
    reply = _audit(tmp_path, "abs")
    assert json.dumps(reply).count(json.dumps(LINE)[1:-1]) == 1, json.dumps(reply)[:3000]


def test_a_flexible_plastics_caution_is_said_once(tmp_path):
    pytest.importorskip("manifold3d")
    reply = _audit(tmp_path, "TPU 85A")
    assert json.dumps(reply).count(json.dumps(FLEXIBLE)[1:-1]) == 1, json.dumps(reply)[:3000]
