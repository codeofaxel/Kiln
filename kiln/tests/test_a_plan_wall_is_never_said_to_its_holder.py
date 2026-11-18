"""A plan wall is never said to someone this computer counts as holding the plan.

When this computer's record of the sign-in holds a plan and Kiln's servers
refuse that plan's answer, the two disagree: the record is out of date.  The
person is told so and how to bring it up to date (sign in again), never the
wall's own words, which offer a plan they hold, whatever those words are.
One rule (:mod:`kiln.served_answer`), so every bridge to a paid answer says
it.  Each case below asks one paid bridge on an install without kiln-pro,
with Kiln's servers answering its wire with their plan wall (the material
pick's own case is in ``test_material_pick_served.py``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from typing import Any

import pytest

from kiln import served_answer

#: The wall's own sentence: worded from the wire's name, offering the plan.
_WALL_WORDS = "{plan} would run {phrase} for this request and return the full result."


def _wall(tool: str, plan: str) -> dict[str, Any]:
    """The plan wall Kiln's servers answer a paid wire with."""
    return {
        "success": False, "status": "error", "code": "TIER_REQUIRED", "retryable": False,
        "error": _WALL_WORDS.format(plan=f"Kiln {plan.title()}", phrase=tool.replace("_", " ")),
        "tool": tool, "current_tier": "free", "required_tier": plan,
        "upgrade_url": "https://kiln3d.com/pricing",
    }


@pytest.fixture
def held(monkeypatch):
    """This computer counts the caller as holding every plan; Kiln's servers
    wall every paid wire."""
    import kiln.licensing as licensing
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(licensing, "check_tier", lambda *_a, **_k: (True, None))
    asked: list[str] = []

    def walls(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **_kwargs):
        asked.append(tool_name)
        return _wall(tool_name, _PLAN_OF[tool_name])

    monkeypatch.setattr(srv, "_pro_api_call", walls)
    return asked


_PLAN_OF = {
    "get_material_record": "pro", "estimate_load_figure": "pro", "orientation_rank": "pro",
    "recovery_plan": "pro", "decoration_memory": "pro", "plan_fleet_route": "business",
    "plan_fleet_fill": "business", "plan_fleet_materials": "business",
}


def _said(out: Any) -> str:
    return json.dumps(out, default=str)


def _material_record(monkeypatch) -> str:
    from kiln import _pro_material_bridge as bridge

    # The suite serves records from a stand-in; this asks the real served door.
    spec = importlib.util.find_spec("kiln._pro_material_bridge")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    monkeypatch.setattr(bridge, "available", lambda: False)
    monkeypatch.setattr(bridge, "_served", fresh._served)
    bridge.forget()
    bridge.material_record("petg")
    return (bridge.unanswered("petg") or "") + _said(served_answer.fields(bridge.last_miss("petg")))


def _load_figure(monkeypatch) -> str:
    from kiln import server

    out = asyncio.run(server.mcp.call_tool(
        "estimate_structural_load", {"material": "petg", "cross_section_mm2": 20.0, "cantilever_length_mm": 30.0},
    ))
    if isinstance(out, tuple):
        out = out[0]
    blocks = out if isinstance(out, list) else out.content
    return blocks[0].text


def _orientation(monkeypatch) -> str:
    from kiln import _pro_orientation_bridge as bridge

    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    poses = [
        {"bed_contact_pct": 40.0, "support_pct": 5.0, "print_height_mm": 20.0, "overhang_pct": 3.0},
        {"bed_contact_pct": 10.0, "support_pct": 30.0, "print_height_mm": 40.0, "overhang_pct": 20.0},
    ]
    ranking, miss = bridge.rank_or_miss(poses)
    assert ranking is None and miss is not None
    return bridge.miss_note(miss) + _said(served_answer.fields(miss))


def _recovery(monkeypatch) -> str:
    from kiln import _pro_recovery_bridge as bridge

    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    asked = bridge.ask("fix", {"failure_type": "spaghetti", "layer": 12})
    return _said(bridge.floor_note(asked, "a plan for this failure"))


def _decoration_memory(monkeypatch) -> str:
    import kiln.server as srv
    from kiln.decoration_library import _replay_note
    from kiln.plugins.decoration_library_tools import _memory_refusal

    answer = srv._pro_api_call("decoration_memory", op="history")
    return _said(_memory_refusal("decoration_history", answer)) + _said(_replay_note(answer))


def _placement_stood_in(monkeypatch) -> None:
    from kiln import _pro_placement_bridge as placement

    monkeypatch.setattr(placement, "job_envelope", lambda _path: {"name": "part"})
    monkeypatch.setattr(placement, "fleet_readings", lambda *_a, **_k: {})


def _fleet_route(monkeypatch) -> str:
    from kiln.plugins.fleet_tools import _served_route

    _placement_stood_in(monkeypatch)
    return _said(_served_route(
        "part.stl", "pla", [{"printer_id": "one"}], {}, quality_priority=3, speed_priority=3,
    ))


def _fleet_fill(monkeypatch) -> str:
    from kiln import _pro_fleet_fill_bridge as bridge

    _placement_stood_in(monkeypatch)
    return _said(bridge.ask("part.stl", 2, material="pla", candidates=[{"printer_id": "one"}], adapters={}))


def _fleet_materials(monkeypatch) -> str:
    from kiln import _pro_fleet_materials_bridge as bridge

    answer, refusal = bridge.fleet_answer("forecast", rows={}, params={}, feature="A material forecast")
    assert answer is None
    return _said(refusal)


BRIDGES = {
    "get_material_record": _material_record,
    "estimate_load_figure": _load_figure,
    "orientation_rank": _orientation,
    "recovery_plan": _recovery,
    "decoration_memory": _decoration_memory,
    "plan_fleet_route": _fleet_route,
    "plan_fleet_fill": _fleet_fill,
    "plan_fleet_materials": _fleet_materials,
}


@pytest.mark.parametrize("wire", sorted(BRIDGES))
def test_a_paid_bridge_tells_a_holder_to_sign_in_again(held, monkeypatch, wire):
    said = BRIDGES[wire](monkeypatch)
    assert wire in held, (wire, held)
    assert "signing in again" in said, said
    wall = _wall(wire, _PLAN_OF[wire])
    assert wall["error"].rstrip(".") not in said, said
    assert "kiln3d.com/pricing" not in said, said
    assert served_answer.PLAN_NOT_COUNTED_CODE in said, said


def test_a_wall_for_a_plan_this_computer_does_not_hold_keeps_its_words(monkeypatch):
    import kiln.licensing as licensing

    monkeypatch.setattr(licensing, "check_tier", lambda *_a, **_k: (False, "needs Kiln Pro"))
    miss = served_answer.classify_answer(_wall("orientation_rank", "pro"))
    assert miss is not None and miss.code == "TIER_REQUIRED"
    assert miss.detail.startswith("Kiln Pro would run")


def test_the_plan_said_is_the_one_the_wall_names(monkeypatch):
    import kiln.licensing as licensing

    monkeypatch.setattr(licensing, "check_tier", lambda *_a, **_k: (True, None))
    miss = served_answer.classify_answer(_wall("plan_fleet_fill", "business"))
    assert miss is not None and "as Kiln Business;" in miss.detail
    assert served_answer.held_plan_wall({"success": True}) is None

