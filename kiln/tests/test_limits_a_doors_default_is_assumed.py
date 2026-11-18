"""A door's own default plastic is read as assumed, not as the person's choice.

Several doors default their ``material`` argument to PLA in their own
signature.  Called with no plastic named, the plastic they read is the one
Kiln assumes, so the ask to Kiln's servers says it was assumed and spends
nothing of the person's daily allowance of plastics; called with PLA named,
it is the person's plastic and asked as any named one.  These go through the
real tool dispatcher, the way an agent calls each door, on an install without
kiln-pro (Kiln's servers stood in).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln import material_limits  # noqa: E402


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


def _figure(value: float | None, unit: str = "mm", basis: str = "general") -> dict[str, Any]:
    return {"value": value, "unit": unit, "basis": basis}


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


@pytest.fixture
def asks(monkeypatch) -> list[dict[str, Any]]:
    """Every design-limits ask a door makes of Kiln's servers, answered."""
    import kiln.server as srv

    seen: list[dict[str, Any]] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        if tool_name != "material_limits":
            return {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"}
        seen.append(kwargs)
        if "quantities" in kwargs:
            return {
                "success": True, "known": False, "material_id": None, "limits": {
                    "sliced_wall_min_mm": _figure(0.19), "sliced_pin_min_mm": _figure(0.8),
                },
            }
        return {
            "success": True, "status": "ok", "known": True,
            "material_id": str(kwargs.get("material")).lower(),
            "limits": {
                "overhang_unsupported_deg": _figure(45.0, "deg"),
                "bridge_max_mm": _figure(10.0),
                "wall_recommended_mm": _figure(1.2),
                "pin_min_mm": _figure(3.0),
                "esd_wall_min_mm": _figure(None, basis="not_applicable"),
            },
            "note": "",
        }

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return seen


def _plastic_asks(seen: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    return [(str(a.get("material")).lower(), a.get("assumed") is True) for a in seen if "quantities" not in a]


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from kiln import server

    out = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(out, tuple):
        out = out[0]
    blocks = out if isinstance(out, list) else out.content
    return json.loads(blocks[0].text)


@pytest.fixture
def cup(tmp_path: Path) -> str:
    from trimesh.creation import box

    body = box(extents=(30.0, 30.0, 25.0))
    body.apply_translation((0, 0, 12.5))
    inner = box(extents=(27.4, 27.4, 25.0))
    inner.apply_translation((0, 0, 14.5))
    path = tmp_path / "cup.stl"
    body.difference(inner).export(path)
    return str(path)


@pytest.fixture
def t_part(tmp_path: Path) -> str:
    """A T on its stem: an arm that needs supports."""
    from trimesh.creation import box

    stem = box(extents=(10, 10, 40))
    stem.apply_translation((0, 0, 20))
    arm = box(extents=(60, 10, 8))
    arm.apply_translation((0, 0, 44))
    path = tmp_path / "t.stl"
    trimesh.util.concatenate([stem, arm]).export(path)
    return str(path)


#: Doors whose own signature defaults ``material`` to PLA.
DOORS = (
    "analyze_printability",
    "optimize_print_orientation",
    "check_print_readiness",
    "mesh_quality_scorecard",
    "validate_and_prepare_mesh",
    "analyze_warping_risk",
)


@pytest.mark.parametrize("door", DOORS)
def test_a_door_called_with_no_plastic_asks_its_default_as_assumed(asks, cup, door):
    _call(door, {"file_path": cup})
    asked = _plastic_asks(asks)
    assert asked, f"{door} read no plastic's limits"
    assert all(name == material_limits.ASSUMED_PLASTIC and assumed for name, assumed in asked), asked


@pytest.mark.parametrize("door", DOORS)
def test_the_same_door_with_pla_named_asks_it_as_the_persons(asks, cup, door):
    _call(door, {"file_path": cup, "material": "PLA"})
    asked = _plastic_asks(asks)
    assert asked and not any(assumed for _name, assumed in asked), asked


@pytest.fixture
def stand_in_slicer(monkeypatch):
    """The slicer binary stood in: the slice's own decisions run, the slicer
    writes a file."""
    import kiln.slicer as slicer_mod

    def fake_run(cmd, **_kwargs):
        Path(cmd[cmd.index("--output") + 1]).write_text("; generated by fake\nG1 X0 Y0 Z0.2 E1\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(slicer_mod, "_run_slicer_with_startup_retry", fake_run)
    monkeypatch.setattr(slicer_mod, "find_slicer", lambda *_a, **_k: SimpleNamespace(
        path="/fake/prusa-slicer", name="prusaslicer", version="2.9.4",
    ))
    monkeypatch.setattr(slicer_mod, "_gcode_output_is_complete", lambda *_a: True)


def test_a_slice_with_no_plastic_named_asks_its_support_plastic_as_assumed(asks, t_part, stand_in_slicer):
    reply = _call("slice_model", {"input_path": t_part})
    assert reply.get("success") is True, reply
    asked = _plastic_asks(asks)
    assert asked and all(name == material_limits.ASSUMED_PLASTIC and assumed for name, assumed in asked), asked


def test_outside_a_tool_call_a_named_pla_is_the_persons(asks):
    material_limits.limits_for("PLA")
    assert _plastic_asks(asks) == [("pla", False)]
