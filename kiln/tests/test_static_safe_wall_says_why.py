"""Where a static-safe grade's own minimum wall is judged, the served reason for it is said.

Kiln's servers send, with a static-safe grade's minimum wall, why that wall
is a floor where they have the sentence (``note`` on the figure).  On an
install without kiln-pro (stood in here, with a sentence and a figure no
catalogue plastic has) the design validator says it with a wall under the
floor, and the OpenSCAD parameter check says it in its reply.  A note too
long to be a sentence is not kept.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

WHY = "A stand-in reason this wall is a floor for the grade's electrical property."


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _answer(note: str) -> dict[str, Any]:
    return {
        "success": True, "status": "ok", "known": True, "material_id": "petg_esd",
        "scenario": {"nozzle_mm": 0.4},
        "limits": {
            "overhang_unsupported_deg": {"value": 45.0, "unit": "deg", "basis": "general"},
            "bridge_max_mm": {"value": 10.0, "unit": "mm", "basis": "general"},
            "wall_recommended_mm": {"value": 1.2, "unit": "mm", "basis": "general"},
            "pin_min_mm": {"value": 3.0, "unit": "mm", "basis": "general"},
            "esd_wall_min_mm": {"value": 1.7, "unit": "mm", "basis": "plastic", "note": note},
        },
        "note": "",
    }


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


@pytest.fixture
def serve(monkeypatch):
    import kiln.server as srv

    def with_note(note: str) -> None:
        def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
            if tool_name != "material_limits" or "quantities" in kwargs:
                return {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"}
            return _answer(note)

        monkeypatch.setattr(srv, "_pro_api_call", fake)

    return with_note


def _tools() -> dict[str, Any]:
    from kiln.plugins.design_tools import plugin

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools


def _cup(folder: Path, wall_mm: float) -> str:
    from trimesh.creation import box

    body = box(extents=(30.0, 30.0, 25.0))
    body.apply_translation((0, 0, 12.5))
    inner = box(extents=(30.0 - 2 * wall_mm, 30.0 - 2 * wall_mm, 25.0))
    inner.apply_translation((0, 0, 2.0 + 12.5))
    path = folder / f"cup_{wall_mm:g}.stl"
    body.difference(inner).export(str(path))
    return str(path)


def test_the_validator_says_why_with_a_wall_under_the_floor(serve, tmp_path):
    serve(WHY)
    reply = _tools()["validate_design_for_requirements"](_cup(tmp_path, 1.3), "a desk cup", material="petg_esd")
    wall = next(c for c in reply["checks"] if c["check_name"] == "wall_thickness")
    assert wall["severity"] == "critical" and wall["required_value"] == 1.7
    assert wall["fix_suggestion"].endswith(WHY)


def test_the_parameter_check_says_why_in_its_reply(serve):
    from kiln.reading_notes import attach
    from kiln.tool_context import enter_tool, leave_tool

    serve(WHY)
    token = enter_tool("validate_scad_parameters", {})
    try:
        reply = attach(_tools()["validate_scad_parameters"]("wall_thickness = 1.3;\ncube(10);", material="petg_esd"))
    finally:
        leave_tool(token)
    assert reply["limits_note"] == WHY


def test_a_note_too_long_to_be_a_sentence_is_not_kept(serve):
    from kiln import material_limits

    serve("x" * 401)
    assert material_limits.limits_for("petg_esd").why == {}
