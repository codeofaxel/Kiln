"""Two doors that show a plastic's design limits read the served figures and say when they are general.

``compare_material_properties`` showed every plastic the same general block,
so PLA and ABS read as identical; it now shows each plastic's limits from the
one helper every check reads, each figure marked as the plastic's own or the
general one, with why when a figure is general.  The OpenSCAD prompt says
"general figures" for what it is: a miss (and why, in the reply), or a
plastic Kiln has no limits of its own for.  The server is stood in here with
figures no catalogue plastic has.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


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

    def with_answers(answers: dict[str, Any] | None) -> None:
        def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
            if answers is None:
                return SIGNED_OUT
            return answers.get(str(kwargs.get("material", "")).lower(), SIGNED_OUT)

        monkeypatch.setattr(srv, "_pro_api_call", fake)

    return with_answers


def _call(name: str, fn, *args: Any, **kwargs: Any) -> Any:
    from kiln.reading_notes import attach
    from kiln.tool_context import enter_tool, leave_tool

    token = enter_tool(name, kwargs)
    try:
        return attach(fn(*args, **kwargs))
    finally:
        leave_tool(token)


def test_compare_shows_each_plastics_own_figures_and_whose_they_are(serve):
    import kiln.server as srv

    serve({
        "pla": _answer("pla", overhang_unsupported_deg=(57.0, "family")),
        "abs": _answer("abs", overhang_unsupported_deg=(37.0, "family"), wall_recommended_mm=(1.9, "family")),
    })
    reply = srv.compare_material_properties("pla", "abs")
    assert reply["design_limits"]["max_overhang_deg"] == {"pla": 57.0, "abs": 37.0}
    assert reply["design_limits"]["recommended_wall_mm"] == {"pla": 1.2, "abs": 1.9}
    assert reply["design_limits_basis"]["max_overhang_deg"] == {"pla": "family", "abs": "family"}
    assert reply["design_limits_basis"]["recommended_wall_mm"] == {"pla": "general", "abs": "family"}
    assert reply["design_limits_basis"]["min_hole_diameter_mm"] == {"pla": "general", "abs": "general"}
    assert "design_limits_note" not in reply


def test_compare_signed_out_marks_every_figure_general_and_says_why(serve):
    import kiln.server as srv

    serve(None)
    reply = srv.compare_material_properties("pla", "abs")
    assert {b for per in reply["design_limits_basis"].values() for b in per.values()} == {"general"}
    assert reply["design_limits_note"] and "general figures" in reply["design_limits_note"][0]


def _prompt(material: str) -> dict[str, Any]:
    from kiln.plugins.design_tools import plugin

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return _call("build_parametric_prompt", tools["build_parametric_prompt"], "a wall hook", material=material)


def test_a_plastic_with_no_limits_of_its_own_is_not_called_unavailable(serve):
    serve({"petg": _answer("petg")})
    reply = _prompt("petg")
    text = str(reply["prompt"])
    assert "Kiln has no limits of this plastic's own" in text
    assert "not available" not in text and "not read" not in text
    assert "Recommended wall thickness: 1.2mm" in text
    assert "limits_note" not in reply


def test_a_miss_says_the_figures_are_general_and_why(serve):
    serve(None)
    reply = _prompt("petg")
    assert "this plastic's own limits were not read for this prompt" in str(reply["prompt"])
    assert "sign in" in reply["limits_note"].lower()


def test_a_plastic_read_with_its_own_figures_says_nothing_general(serve):
    serve({"abs": _answer("abs", overhang_unsupported_deg=(37.0, "family"), wall_recommended_mm=(1.9, "family"))})
    text = str(_prompt("abs")["prompt"])
    assert "general figures" not in text
    assert "Recommended wall thickness: 1.9mm" in text and "Max unsupported overhang: 37.0" in text
