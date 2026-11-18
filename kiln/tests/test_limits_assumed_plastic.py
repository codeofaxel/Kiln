"""A plastic a check reads only because the person named none is asked as assumed.

Some checks name no plastic (a support estimate, a wall thickening, a slice
with no filament stated) and read the plastic Kiln assumes.  Kiln's servers
count a plastic against the person's daily allowance only when the person
chose it, so the ask says when the plastic was assumed.  On an install
without kiln-pro (stood in here) the assumed plastic is asked with
``assumed`` and a named plastic never is.
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


def _answer(material_id: str) -> dict[str, Any]:
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
    }


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


@pytest.fixture
def asks(monkeypatch) -> list[dict[str, Any]]:
    import kiln.server as srv

    seen: list[dict[str, Any]] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        if tool_name != "material_limits":
            return {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"}
        seen.append(kwargs)
        if "quantities" in kwargs:
            return {
                "success": True, "known": False, "material_id": None, "limits": {
                    "sliced_wall_min_mm": {"value": 0.19, "unit": "mm", "basis": "general"},
                    "sliced_pin_min_mm": {"value": 0.8, "unit": "mm", "basis": "general"},
                },
            }
        return _answer(str(kwargs.get("material")).lower())

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return seen


def _plastic_asks(seen: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    return [(str(a.get("material")), a.get("assumed") is True) for a in seen if "quantities" not in a]


def test_an_unnamed_plastic_is_asked_as_assumed(asks):
    with material_limits.assuming(None) as plastic:
        assert plastic == material_limits.ASSUMED_PLASTIC
        material_limits.limits_for(plastic)
    assert _plastic_asks(asks) == [(material_limits.ASSUMED_PLASTIC, True)]


def test_a_named_plastic_is_never_asked_as_assumed(asks):
    with material_limits.assuming("ABS") as plastic:
        assert plastic == "ABS"
        material_limits.limits_for(plastic)
    material_limits.limits_for("pla")
    assert _plastic_asks(asks) == [("ABS", False), ("pla", False)]


def test_the_assumption_ends_with_its_block(asks):
    with material_limits.assuming(""):
        pass
    material_limits.limits_for("pla")
    assert _plastic_asks(asks) == [("pla", False)]


def test_a_printability_check_with_no_plastic_asks_it_as_assumed(asks, tmp_path):
    trimesh = pytest.importorskip("trimesh")
    from kiln.printability import analyze_printability

    part = tmp_path / "box.stl"
    trimesh.creation.box(extents=(20, 20, 10)).export(part)
    analyze_printability(str(part))
    asked = _plastic_asks(asks)
    assert asked and all(flag for _name, flag in asked), asked
    _forget()
    asks.clear()
    analyze_printability(str(part), material="abs")
    asked = _plastic_asks(asks)
    assert asked and not any(flag for _name, flag in asked), asked


def test_a_slice_with_no_plastic_asks_it_as_assumed(asks, tmp_path):
    trimesh = pytest.importorskip("trimesh")
    from kiln.slicer_supports import decide_supports

    part = tmp_path / "box.stl"
    trimesh.creation.box(extents=(20, 20, 10)).export(part)
    decide_supports(str(part), material=None)
    asked = _plastic_asks(asks)
    assert asked and all(flag for _name, flag in asked), asked
