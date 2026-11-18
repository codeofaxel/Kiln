"""A design prompt states the wall the next check holds the part to.

``build_generation_prompt`` and ``build_parametric_prompt`` tell a generator
what wall to give a part; ``validate_scad_parameters`` then judges it.  For
every plastic the prompt names a wall, and it is the wall the check warns
below: the plastic's own when Kiln's servers answered (a static-safe grade's
minimum named as such), the general figure when they did not or the plastic
has none of its own, said to be general.  The servers are stood in (public
tests cannot import kiln-pro), with made-up figures.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import pytest

from kiln import material_limits

_WALL = re.compile(r"minimum wall thickness ([0-9.]+)mm( \(([^)]*)\))?")


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    from kiln import design_intelligence as di

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    di._reset_knowledge_base()
    yield
    _forget()
    di._reset_knowledge_base()


def _limits(*, wall: tuple[float, str], esd: tuple[float | None, str]) -> dict[str, Any]:
    return {
        "success": True,
        "status": "ok",
        "known": True,
        "material_id": "stand-in",
        "scenario": {"nozzle_mm": 0.4},
        "limits": {
            "overhang_unsupported_deg": {"value": 50.0, "unit": "deg", "basis": "plastic"},
            "bridge_max_mm": {"value": 10.0, "unit": "mm", "basis": "plastic"},
            "wall_recommended_mm": {"value": wall[0], "unit": "mm", "basis": wall[1]},
            "pin_min_mm": {"value": 2.0, "unit": "mm", "basis": "general"},
            "esd_wall_min_mm": {"value": esd[0], "unit": "mm", "basis": esd[1]},
        },
        "note": "",
    }


def _serve(monkeypatch, answer: dict[str, Any]) -> list[str]:
    """Kiln's servers stood in: the design limits answer *answer*; every
    other served tool gets an answer that is not one."""
    import kiln.server as srv

    asked: list[str] = []

    def served(tool_name, /, _timeout=None, _asked_by_user=True, _feature=None, **_kwargs):
        asked.append(tool_name)
        if tool_name == "material_limits":
            return answer
        return {"status": "error", "success": False, "code": "NOT_SERVED_HERE", "error": "stand-in"}

    monkeypatch.setattr(srv, "_pro_api_call", served)
    return asked


def _doors() -> dict:
    from kiln.plugins.design_tools import plugin

    tools: dict = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools


def _stated_wall(reply: dict[str, Any]) -> tuple[float, str]:
    found = _WALL.findall(reply["prompt"]["improved_prompt"])
    assert len(found) == 1, reply["prompt"]["improved_prompt"]
    value, _bracket, words = found[0]
    return float(value), words


def _checked_wall(material: str) -> float:
    """The wall the parameter check warns below for *material*."""
    out = _doors()["validate_scad_parameters"]("wall_thickness = 0.9; // mm\n", material=material)
    walls = sorted(w["limit_value"] for w in out["warnings"] if w["parameter_name"] == "wall_thickness")
    assert walls, out
    return walls[-1]


PART = "a storage box"


@pytest.mark.parametrize("door", ["build_generation_prompt", "build_parametric_prompt"])
def test_a_static_safe_grade_is_told_its_own_minimum_wall(monkeypatch, door):
    asked = _serve(monkeypatch, _limits(wall=(1.2, "general"), esd=(1.5, "plastic")))
    wall, words = _stated_wall(_doors()[door](PART, material="petg_esd"))
    assert wall == 1.5 == _checked_wall("petg_esd")
    assert words == "this plastic's static-safe minimum"
    # Read like every other limit: one ask for the plastic, the prompt and
    # the check together.
    assert asked.count("material_limits") == 1, asked


@pytest.mark.parametrize("material", ["tpu", "petg"])
@pytest.mark.parametrize("door", ["build_generation_prompt", "build_parametric_prompt"])
def test_a_plastic_with_no_wall_of_its_own_is_told_the_general_wall(monkeypatch, door, material):
    _serve(monkeypatch, _limits(wall=(1.2, "general"), esd=(None, "not_applicable")))
    wall, words = _stated_wall(_doors()[door](PART, material=material))
    assert wall == 1.2 == _checked_wall(material)
    assert words == "the general figure for any plastic"


@pytest.mark.parametrize("door", ["build_generation_prompt", "build_parametric_prompt"])
def test_signed_out_the_prompt_states_the_general_wall_and_says_so(door):
    """No sign-in on this install: the real served door, the general figures,
    the same wall the check holds the part to, said to be general."""
    wall, words = _stated_wall(_doors()[door](PART, material="petg_esd"))
    assert wall == _checked_wall("petg_esd")
    assert words == "the general figure for any plastic"

