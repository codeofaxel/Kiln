"""The design validator judges the thinnest wall it measured, on every install.

``validate_design_for_requirements`` takes the thinnest wall the printability
analysis measured and judges it against the wall that closes (critical below
it) and the recommended wall (a warning below it), whatever the analysis
counted as thin at its own threshold.  The reported thickness is the measured
one, never the required one.  On an install without kiln-pro, signed out,
the general figures judge the wall; signed in, the plastic's served figures
do (stood in here with figures no catalogue plastic has).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the cups

DOOR = "validate_design_for_requirements"
WANTS = "a desk cup that holds pens"
#: A stand-in recommended wall no catalogue plastic has.
STAND_IN_MM = 1.9


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


def _signed_out(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
    return {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"}


def _served_with_own_wall(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
    if tool_name != "material_limits" or "quantities" in kwargs:
        return _signed_out(tool_name)
    return {
        "success": True,
        "status": "ok",
        "known": True,
        "material_id": "abs",
        "scenario": {"nozzle_mm": 0.4},
        "limits": {
            "overhang_unsupported_deg": {"value": 45.0, "unit": "deg", "basis": "general"},
            "bridge_max_mm": {"value": 10.0, "unit": "mm", "basis": "general"},
            "wall_recommended_mm": {"value": STAND_IN_MM, "unit": "mm", "basis": "family"},
            "pin_min_mm": {"value": 3.0, "unit": "mm", "basis": "general"},
            "esd_wall_min_mm": {"value": None, "unit": "mm", "basis": "not_applicable"},
        },
        "note": "",
    }


def _cup(folder: Path, wall_mm: float) -> str:
    from trimesh.creation import box

    body = box(extents=(30.0, 30.0, 25.0))
    body.apply_translation((0, 0, 12.5))
    inner = box(extents=(30.0 - 2 * wall_mm, 30.0 - 2 * wall_mm, 25.0))
    inner.apply_translation((0, 0, max(2.0, wall_mm + 0.5) + 12.5))
    shell = body.difference(inner)
    assert shell.is_watertight
    path = folder / f"cup_{wall_mm:g}.stl"
    shell.export(str(path))
    return str(path)


def _validate(monkeypatch, path: str, served) -> dict[str, Any]:
    import kiln.server as srv
    from kiln.plugins.design_tools import plugin

    monkeypatch.setattr(srv, "_pro_api_call", served)
    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    reply = tools[DOOR](path, WANTS, material="abs")
    assert reply.get("success") is True, reply
    walls = [c for c in reply["checks"] if c["check_name"] == "wall_thickness"]
    assert len(walls) == 1, reply["checks"]
    return {"reply": reply, "wall": walls[0]}


@pytest.mark.parametrize("served", [_signed_out, _served_with_own_wall], ids=["signed_out", "signed_in"])
def test_a_wall_that_does_not_close_is_critical(monkeypatch, tmp_path, served):
    wall = _validate(monkeypatch, _cup(tmp_path, 0.6), served)["wall"]
    assert wall["passed"] is False and wall["severity"] == "critical"
    assert wall["actual_value"] == 0.6


def test_signed_out_the_general_recommended_wall_warns(monkeypatch, tmp_path):
    got = _validate(monkeypatch, _cup(tmp_path, 1.0), _signed_out)
    wall = got["wall"]
    assert wall["passed"] is False and wall["severity"] == "warning"
    assert wall["actual_value"] == 1.0 and wall["required_value"] == 1.2
    assert "general figures" in got["reply"]["summary"]


def test_signed_in_the_plastics_own_recommended_wall_warns(monkeypatch, tmp_path):
    wall = _validate(monkeypatch, _cup(tmp_path, 1.5), _served_with_own_wall)["wall"]
    assert wall["passed"] is False and wall["severity"] == "warning"
    assert wall["actual_value"] == 1.5 and wall["required_value"] == STAND_IN_MM


@pytest.mark.parametrize("served", [_signed_out, _served_with_own_wall], ids=["signed_out", "signed_in"])
def test_a_passing_wall_reports_its_own_thickness(monkeypatch, tmp_path, served):
    wall = _validate(monkeypatch, _cup(tmp_path, 2.5), served)["wall"]
    assert wall["passed"] is True and wall["actual_value"] == 2.5


def test_a_wall_kiln_could_not_measure_states_no_thickness():
    from kiln.design_validator import _unmeasured_wall_check

    check = _unmeasured_wall_check(1.2)
    assert check.passed is False and check.severity == "warning"
    assert check.actual_value == "not measured"
