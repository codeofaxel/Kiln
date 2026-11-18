"""Past the day's allowance of plastics, a check says so in one plain line.

Kiln's servers answer a plastic with the general figures, never a refusal,
when the caller's daily allowance of plastics with their own design limits
is used, and send the one line a reply says under ``allowance``.  On an
install without kiln-pro (stood in here) the line is said by the reply of the
call that read it, whichever door it was, once; the answer is not kept past
the call; and a slice reply states its support decision, not the plastic's
own overhang threshold.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from kiln import material_limits

LINE = (
    "Checked against the general figures: your plan checks 6 plastics a day against their own design "
    "limits, today's are used, and they renew at midnight UTC. Kiln Business has no daily limit: "
    "https://kiln3d.com/pricing"
)


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _general(material_id: str, *, allowance: str | None = None, overhang: tuple[float, str] = (45.0, "general")):
    answer: dict[str, Any] = {
        "success": True,
        "status": "ok",
        "known": True,
        "material_id": material_id,
        "scenario": {"nozzle_mm": 0.4},
        "limits": {
            "overhang_unsupported_deg": {"value": overhang[0], "unit": "deg", "basis": overhang[1]},
            "bridge_max_mm": {"value": 10.0, "unit": "mm", "basis": "general"},
            "wall_recommended_mm": {"value": 1.2, "unit": "mm", "basis": "general"},
            "pin_min_mm": {"value": 3.0, "unit": "mm", "basis": "general"},
            "esd_wall_min_mm": {"value": None, "unit": "mm", "basis": "not_applicable"},
        },
        "note": "",
    }
    if allowance:
        answer["allowance"] = allowance
    return answer


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


@pytest.fixture
def served(monkeypatch):
    import kiln.server as srv

    answers: dict[str, Any] = {}
    calls: list[str] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        if tool_name != "material_limits" or "quantities" in kwargs:
            return {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"}
        calls.append(str(kwargs.get("material")))
        return answers[str(kwargs.get("material")).lower()]

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return answers, calls


def _call(name: str, fn, *args: Any, **kwargs: Any) -> Any:
    """One tool call, marked and answered the way the dispatch chokepoint does."""
    from kiln.reading_notes import attach
    from kiln.tool_context import enter_tool, leave_tool

    token = enter_tool(name, kwargs)
    try:
        return attach(fn(*args, **kwargs))
    finally:
        leave_tool(token)


def _registered(module_path: str) -> dict[str, Any]:
    import importlib

    plugin = importlib.import_module(module_path).plugin
    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools


def test_the_line_is_kept_to_be_said_and_the_answer_is_not_kept(served):
    answers, calls = served
    answers["abs"] = _general("abs", allowance=LINE)
    limits = material_limits.limits_for("ABS")
    assert limits.allowance == LINE and limits.basic
    assert LINE in limits.notes()
    material_limits.limits_for("ABS")
    assert calls == ["ABS", "ABS"], "an answer cut to the general figures is asked again"


def test_every_door_that_read_it_says_the_line_once(served):
    answers, _calls = served
    answers["abs"] = _general("abs", allowance=LINE)
    tools = _registered("kiln.plugins.design_tools")
    prompt = _call("build_generation_prompt", tools["build_generation_prompt"], "a wall hook", material="abs")
    assert prompt["limits_note"] == LINE
    scad = _call(
        "validate_scad_parameters", tools["validate_scad_parameters"],
        "wall_thickness = 1.5;\ncube(10);", material="abs",
    )
    assert scad["limits_note"] == LINE


def test_a_reply_that_already_says_it_gets_no_second_copy(served, tmp_path):
    trimesh = pytest.importorskip("trimesh")
    answers, _calls = served
    answers["abs"] = _general("abs", allowance=LINE)
    part = tmp_path / "box.stl"
    trimesh.creation.box(extents=(20, 20, 10)).export(part)
    tools = _registered("kiln.plugins.design_tools")
    reply = _call(
        "validate_design_for_requirements", tools["validate_design_for_requirements"],
        str(part), "a wall hook", material="abs",
    )
    assert reply["summary"].count(LINE) == 1
    assert "limits_note" not in reply


def test_within_the_allowance_nothing_is_said(served):
    answers, _calls = served
    answers["abs"] = _general("abs", overhang=(37.0, "family"))
    tools = _registered("kiln.plugins.design_tools")
    prompt = _call("build_generation_prompt", tools["build_generation_prompt"], "a wall hook", material="abs")
    assert "limits_note" not in prompt


def test_an_overlong_line_is_never_said(served):
    answers, _calls = served
    answers["abs"] = _general("abs", allowance="x" * 401)
    assert material_limits.limits_for("ABS").allowance == ""


def test_a_slice_states_its_support_decision_not_the_plastics_own_threshold(served, tmp_path):
    trimesh = pytest.importorskip("trimesh")
    from trimesh.creation import box

    from kiln.slicer_supports import decide_supports

    stem = box(extents=(10, 10, 40))
    stem.apply_translation((0, 0, 20))
    arm = box(extents=(60, 10, 8))
    arm.apply_translation((0, 0, 44))
    part = tmp_path / "t.stl"
    trimesh.util.concatenate([stem, arm]).export(part)
    answers, _calls = served
    answers["abs"] = _general("abs", overhang=(37.0, "family"))
    own = decide_supports(str(part), material="abs")
    assert own.enabled and own.settings["support_material_threshold"] == "53"
    assert "support_material_threshold" not in own.to_dict()["settings"]
    answers["pla"] = _general("pla")
    general = decide_supports(str(part), material="pla")
    assert general.to_dict()["settings"]["support_material_threshold"] == "45"
