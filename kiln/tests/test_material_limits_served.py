"""A plastic's design limits reach every design check from one helper.

``kiln.material_limits.limits_for`` is the one reader: on an install without
kiln-pro it asks Kiln's servers (``material_limits``) through the served door,
and every check -- the design validator, the brief's rules, both generation
prompts, the OpenSCAD parameter check, the printability overhang threshold, a
slice's support threshold and edge finishing -- reads the figure it gets back.
The server is stood in here (public tests cannot import kiln-pro).

* the request carries one plastic and the scenario, nothing else;
* a served figure is the figure every door states, the same number at each;
* a parameter is judged by the limit its name is for: a pin by the pin
  figure, a hole by the hole figure, a bare diameter or any other angle by
  none.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from kiln import material_limits

#: A served answer with figures no general figure equals, so a door that
#: states one of them read the served answer.
_SERVED = {
    "overhang_unsupported_deg": {"value": 57.0, "unit": "deg", "basis": "family"},
    "bridge_max_mm": {"value": 33.0, "unit": "mm", "basis": "family"},
    "wall_recommended_mm": {"value": 1.7, "unit": "mm", "basis": "family"},
    "pin_min_mm": {"value": 2.2, "unit": "mm", "basis": "family"},
    "esd_wall_min_mm": {"value": None, "unit": "mm", "basis": "not_applicable"},
}


def _answer(**overrides: Any) -> dict[str, Any]:
    limits = {q: dict(f) for q, f in _SERVED.items()}
    limits.update(overrides)
    return {
        "success": True,
        "status": "ok",
        "known": True,
        "material_id": "pla",
        "scenario": {"nozzle_mm": 0.4},
        "limits": limits,
        "note": "",
    }


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    """No kiln-pro on this machine, whatever the test runner has on its path."""
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    material_limits.forget()
    yield
    material_limits.forget()


@pytest.fixture
def served(monkeypatch):
    import kiln.server as srv

    calls: list[tuple[str, dict]] = []
    answers: list[Any] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        calls.append((tool_name, {"_asked_by_user": _asked_by_user, **kwargs}))
        answer = answers[0] if answers else _answer()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return calls, answers


def test_the_request_is_one_plastic_and_its_scenario(served):
    calls, _ = served
    limits = material_limits.limits_for("PLA", nozzle_mm=0.6, enclosed=True)
    assert calls, "nothing was asked"
    tool, sent = calls[0]
    assert tool == "material_limits"
    assert sent == {"_asked_by_user": False, "material": "PLA", "nozzle_mm": 0.6, "enclosed": True}
    assert limits.value("overhang_unsupported_deg") == 57.0
    assert limits.basis["overhang_unsupported_deg"] == "family"
    assert limits.miss is None and not limits.basic


def test_a_session_asks_once_per_plastic_and_scenario(served):
    calls, _ = served
    for _ in range(3):
        material_limits.limits_for("PLA")
    assert len(calls) == 1


def test_the_figures_that_are_one_for_every_plastic_are_never_asked(served):
    calls, _ = served
    limits = material_limits.limits_for("")
    assert calls == []
    assert limits.value("hole_usable_mm") == 2.0
    assert limits.value("wall_min_mm") == 0.8


def test_one_served_overhang_is_the_figure_every_door_states(served, tmp_path):
    from kiln.design_intelligence import get_design_constraints
    from kiln.edge_finish import print_frame
    from kiln.generation_feedback import build_parametric_generation_prompt, enhance_prompt_with_design_intelligence
    from kiln.printability import overhang_threshold_deg
    from kiln.slicer_supports import support_settings

    assert overhang_threshold_deg("pla") == 57.0
    assert support_settings(overhang_threshold_deg("pla"))  # writes from the same figure
    brief = get_design_constraints("a desk organizer", material="pla")
    assert brief.combined_rules["material_max_unsupported_overhang_deg"] == 57.0
    assert brief.combined_rules["material_max_bridge_length_mm"] == 33.0
    assert brief.combined_rules["material_min_pin_diameter_mm"] == 2.2
    prompt = enhance_prompt_with_design_intelligence("a desk organizer", material="pla")
    assert "no overhangs greater than 57.0 degrees" in prompt.improved_prompt
    assert "max 33.0mm unsupported spans" in prompt.improved_prompt
    scad = build_parametric_generation_prompt("a desk organizer", material="pla")
    assert "Max unsupported overhang: 57.0" in scad.improved_prompt
    frame, _said = print_frame(material="pla", nozzle_mm=0.4)
    assert frame.overhang_deg == 57.0


def test_the_plastics_own_recommended_wall_reaches_the_prompt_and_the_parameter_check(served):
    from kiln.generation_feedback import enhance_prompt_with_design_intelligence
    from kiln.parametric import validate_openscad_parameters

    prompt = enhance_prompt_with_design_intelligence("a desk organizer", material="pla")
    assert "minimum wall thickness 1.7mm" in prompt.improved_prompt
    warnings = validate_openscad_parameters("wall_thickness = 1.0; // mm\n", material="pla")
    assert any(w.limit_value == 1.7 for w in warnings), warnings


def test_a_general_recommended_wall_is_not_called_the_plastics_own(served):
    _, answers = served
    answers.append(_answer(wall_recommended_mm={"value": 1.2, "unit": "mm", "basis": "general"}))
    limits = material_limits.limits_for("PLA")
    assert "recommended_wall_thickness_mm" not in limits.design_limits()


def test_a_static_safe_grade_keeps_its_own_wall_floor(served):
    _, answers = served
    answers.append(_answer(esd_wall_min_mm={"value": 1.5, "unit": "mm", "basis": "plastic"}))
    limits = material_limits.limits_for("petg_esd")
    assert limits.design_limits()["min_wall_thickness_mm"] == 1.5


class TestParameterNames:
    """A parameter is judged by the limit its name is for."""

    def _warnings(self, scad: str):
        from kiln.parametric import validate_openscad_parameters

        return validate_openscad_parameters(scad, material="pla")

    def test_a_pin_is_judged_by_the_pin_figure_only(self, served):
        warnings = self._warnings("pin_diameter = 1.0; // mm\n")
        assert [w.limit_value for w in warnings] == [2.2]

    def test_a_bare_diameter_gets_no_limit(self, served):
        assert self._warnings("diameter = 1.0; // mm\n") == []

    def test_a_hole_is_judged_by_the_hole_figure(self, served):
        warnings = self._warnings("hole_diameter = 1.0; // mm\n")
        assert [w.limit_value for w in warnings] == [2.0]

    def test_an_angle_that_is_not_an_overhang_is_not_judged_as_one(self, served):
        assert self._warnings("chamfer_angle = 80; // deg\n") == []

    def test_an_overhang_is_judged_by_the_overhang_figure(self, served):
        warnings = self._warnings("overhang_angle = 80; // deg\n")
        assert [w.limit_value for w in warnings] == [57.0]
