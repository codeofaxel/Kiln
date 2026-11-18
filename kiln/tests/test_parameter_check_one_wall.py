"""The OpenSCAD parameter check warns against the recommended wall, and names whose figure it is.

``validate_scad_parameters`` (and the tweak that recompiles) checks a wall
parameter against the wall that closes and the recommended wall, the same
two walls the design validator checks: the plastic's own recommended wall
where Kiln's servers gave one, the general figure otherwise.  A warning says
whether a figure is the plastic's own or the general one, never calling a
general figure the plastic's, and the reply says why a figure is general.
The server is stood in here with figures no catalogue plastic has.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

SCAD = "wall_thickness = {wall};\npin_diameter = 2.5;\ncube(10);"


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _answer(material_id: str, **figures: tuple[float | None, str]) -> dict[str, Any]:
    limits = {
        "overhang_unsupported_deg": {"value": 45.0, "unit": "deg", "basis": "general"},
        "bridge_max_mm": {"value": 10.0, "unit": "mm", "basis": "general"},
        "wall_recommended_mm": {"value": 1.2, "unit": "mm", "basis": "general"},
        "pin_min_mm": {"value": 3.0, "unit": "mm", "basis": "general"},
        "esd_wall_min_mm": {"value": None, "unit": "mm", "basis": "not_applicable"},
    }
    for quantity, (value, basis) in figures.items():
        limits[quantity] = {"value": value, "unit": limits[quantity]["unit"], "basis": basis}
    return {
        "success": True, "status": "ok", "known": True, "material_id": material_id,
        "scenario": {"nozzle_mm": 0.4}, "limits": limits, "note": "",
    }


SIGNED_OUT = {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"}


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


@pytest.fixture
def serve(monkeypatch):
    import kiln.server as srv

    def with_answer(answer: dict[str, Any]) -> None:
        def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
            return answer

        monkeypatch.setattr(srv, "_pro_api_call", fake)

    return with_answer


def _check(wall: float, material: str) -> dict[str, Any]:
    """One call of the registered door, marked and answered the way the
    dispatch chokepoint does."""
    from kiln.plugins.design_tools import plugin
    from kiln.reading_notes import attach
    from kiln.tool_context import enter_tool, leave_tool

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    token = enter_tool("validate_scad_parameters", {"material": material})
    try:
        return attach(tools["validate_scad_parameters"](SCAD.format(wall=wall), material=material))
    finally:
        leave_tool(token)


def _wall_messages(reply: dict[str, Any]) -> list[str]:
    return [w["message"] for w in reply["warnings"] if w["parameter_name"] == "wall_thickness"]


def test_a_general_recommended_wall_still_warns_signed_in(serve):
    serve(_answer("pla"))
    assert _wall_messages(_check(1.0, "pla")) == [
        "'wall_thickness' (1.0mm) is below the general recommended wall thickness (1.2mm)"
    ]


def test_the_plastics_own_recommended_wall_is_named_as_its_own(serve):
    serve(_answer("abs", wall_recommended_mm=(1.9, "family")))
    assert _wall_messages(_check(1.5, "abs")) == [
        "'wall_thickness' (1.5mm) is below this plastic's recommended wall thickness (1.9mm)"
    ]


def test_a_wall_that_does_not_close_is_the_general_minimum(serve):
    serve(_answer("pla"))
    assert _wall_messages(_check(0.6, "pla")) == [
        "'wall_thickness' (0.6mm) is below the general minimum wall thickness (0.8mm)"
    ]


def test_a_static_safe_minimum_is_named_as_the_plastics(serve):
    serve(_answer("petg_esd", esd_wall_min_mm=(1.7, "plastic")))
    assert _wall_messages(_check(1.3, "petg_esd")) == [
        "'wall_thickness' (1.3mm) is below this plastic's static-safe minimum wall (1.7mm)"
    ]


def test_signed_out_the_figures_are_general_and_the_reply_says_why(serve):
    serve(SIGNED_OUT)
    reply = _check(1.0, "abs")
    assert _wall_messages(reply) == [
        "'wall_thickness' (1.0mm) is below the general recommended wall thickness (1.2mm)"
    ]
    assert not any("material minimum" in w["message"] for w in reply["warnings"])
    assert "general figures" in reply["limits_note"] and "sign in" in reply["limits_note"].lower()


def test_signed_out_a_flexible_plastic_is_told_what_the_general_figures_leave_out(serve):
    serve(SIGNED_OUT)
    reply = _check(1.0, "tpu_85a")
    assert "flexible plastic needs more margin" in reply["limits_note"]
