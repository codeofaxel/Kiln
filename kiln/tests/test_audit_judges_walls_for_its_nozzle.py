"""The design audit judges a part's walls for the nozzle it prints with.

The audit runs the printability check and the design check on one part,
and both judge its walls for the nozzle the audit resolves: a wall too thin
for a wide nozzle does not pass the design check.  Stood in here: an install
without kiln-pro whose servers do not answer, so every check reads the
general figures, which follow the nozzle.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from kiln import material_limits


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture(autouse=True)
def no_answer(monkeypatch):
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    monkeypatch.setattr(
        srv, "_pro_api_call",
        lambda tool_name, *_a, **_k: {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"},
    )
    yield
    _forget()


def _wall_check(tmp_path, nozzle_mm: float) -> dict[str, Any]:
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("manifold3d")
    import importlib

    from kiln.tool_context import enter_tool, leave_tool

    part = tmp_path / "cup.stl"
    if not part.exists():
        # A cup with 1.3 mm walls: three lines of a 0.4 mm nozzle, under
        # three lines of a 0.6 mm one.
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
        reply = tools["audit_original_design"](
            str(part), "a desk cup that holds pens", material="pla", nozzle_diameter=nozzle_mm,
        )
    finally:
        leave_tool(token)
    assert reply.get("success"), reply
    gate = next(g for g in reply["gates"] if g["name"] == "design_requirements")
    return next(c for c in gate["details"]["checks"] if c["check_name"] == "wall_thickness")


def test_a_wall_three_lines_of_the_reference_nozzle_passes_for_it(tmp_path):
    check = _wall_check(tmp_path, 0.4)
    assert check["passed"] is True, check
    assert check["required_value"] == pytest.approx(1.2)


def test_the_same_wall_is_held_to_three_lines_of_a_wider_nozzle(tmp_path):
    check = _wall_check(tmp_path, 0.6)
    assert check["required_value"] == pytest.approx(1.8), check
    assert check["passed"] is False, check
