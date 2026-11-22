"""A static-safe grade's own minimum wall is a floor of its own in the printability check.

On an install without kiln-pro the printability check measures thin walls
against the plastic's own wall floor from :func:`kiln.material_limits.limits_for`:
its own recommended wall, and a static-safe grade's own minimum wall for its
electrical property, whichever is thicker.  The server is stood in here with a
figure no catalogue plastic has, so a check that flags against it read the
served answer.

* a wall under the served static-safe minimum is counted thin, through the
  registered door;
* a wall at or above it is not;
* a plastic with no static-safe minimum is judged as before.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the cups

#: A stand-in static-safe minimum no catalogue plastic has.
STAND_IN_MM = 1.7


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _answer(material_id: str, static_safe: float | None) -> dict[str, Any]:
    esd = (
        {"value": static_safe, "unit": "mm", "basis": "plastic"}
        if static_safe else {"value": None, "unit": "mm", "basis": "not_applicable"}
    )
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
            "esd_wall_min_mm": esd,
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
def served(monkeypatch):
    import kiln.server as srv

    answers: dict[str, dict[str, Any]] = {}

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        if tool_name != "material_limits":
            return {"success": False, "error": "not stood in", "code": "UNKNOWN_TOOL"}
        if "quantities" in kwargs:  # what the slice keeps: not this test's question
            return {"success": False, "error": "not stood in", "code": "UNKNOWN_TOOL"}
        return answers[str(kwargs.get("material", "")).lower()]

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return answers


def _cup(folder: Path, wall_mm: float) -> str:
    from trimesh.creation import box

    body = box(extents=(30.0, 30.0, 25.0))
    body.apply_translation((0, 0, 12.5))
    inner = box(extents=(30.0 - 2 * wall_mm, 30.0 - 2 * wall_mm, 25.0))
    inner.apply_translation((0, 0, 2.0 + 12.5))
    shell = body.difference(inner)
    assert shell.is_watertight
    path = folder / f"cup_{wall_mm:g}.stl"
    shell.export(str(path))
    return str(path)


def _door():
    from kiln.plugins.printability_tools import plugin

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools["analyze_printability"]


def test_a_wall_under_the_static_safe_minimum_is_thin(served, tmp_path):
    served["petg_esd"] = _answer("petg_esd", STAND_IN_MM)
    walls = _door()(_cup(tmp_path, 1.5), material="petg_esd")["report"]["thin_walls"]
    assert walls["thin_wall_count"] > 0
    assert walls["threshold_basis"] == "material"


def test_a_wall_at_the_static_safe_minimum_is_not(served, tmp_path):
    served["petg_esd"] = _answer("petg_esd", STAND_IN_MM)
    walls = _door()(_cup(tmp_path, 1.8), material="petg_esd")["report"]["thin_walls"]
    assert walls["thin_wall_count"] == 0


def test_a_plastic_with_no_static_safe_minimum_is_judged_as_before(served, tmp_path):
    served["petg"] = _answer("petg", None)
    walls = _door()(_cup(tmp_path, 1.5), material="petg")["report"]["thin_walls"]
    assert walls["thin_wall_count"] == 0
    assert walls["threshold_basis"] == "nozzle"
