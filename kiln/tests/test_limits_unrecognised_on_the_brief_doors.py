"""The brief and prompt doors say once when Kiln cannot place the plastic named.

``analyze_design_requirements``, ``build_generation_prompt`` and
``build_parametric_prompt`` read a named plastic's limits through the one
helper every design check reads.  A name neither the catalogue nor Kiln's
design limits can place is said once
(:data:`kiln.material_limits.UNRECOGNISED`), never on a miss; a name the
design limits place where the catalogue cannot is read as that plastic.
Through the real tool dispatcher on an install without kiln-pro, Kiln's
servers stood in.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest

from kiln import material_limits

DOORS = ("analyze_design_requirements", "build_generation_prompt", "build_parametric_prompt")
#: A name nothing places.
UNPLACED = "ABS+"
#: A name Kiln's design limits place (as ``pp``) and the catalogue does not.
PLACED_BY_THE_LIMITS = "Polypropylene"
#: That plastic's own overhang, as the stand-in answers it.
OWN_OVERHANG = 40.0


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
def servers(monkeypatch) -> dict[str, Any]:
    """Kiln's servers stood in: answering (``signed_in``) or signed out."""
    import kiln.server as srv

    state: dict[str, Any] = {"mode": "signed_in", "asked": []}

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        if tool_name != "material_limits":
            return {"success": False, "code": "UNKNOWN_TOOL", "error": "not stood in"}
        if state["mode"] == "signed_out":
            return {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in to Kiln.", "why": "signed_out"}
        name = str(kwargs.get("material")).lower()
        state["asked"].append(name)
        own = name == PLACED_BY_THE_LIMITS.lower()
        known = name != UNPLACED.lower()
        return {
            "success": True, "status": "ok", "known": known,
            "material_id": ("pp" if own else name) if known else None,
            "limits": {
                "overhang_unsupported_deg": _figure(OWN_OVERHANG, "deg", "plastic") if own else _figure(45.0, "deg"),
                "bridge_max_mm": _figure(10.0),
                "wall_recommended_mm": _figure(1.2),
                "pin_min_mm": _figure(3.0),
                "esd_wall_min_mm": _figure(None, basis="not_applicable"),
            },
            "note": "",
        }

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return state


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from kiln import server

    out = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(out, tuple):
        out = out[0]
    blocks = out if isinstance(out, list) else out.content
    return json.loads(blocks[0].text)


def _said(reply: dict[str, Any], sentence: str) -> int:
    return json.dumps(reply).count(json.dumps(sentence)[1:-1])


@pytest.mark.parametrize("door", DOORS)
def test_a_name_nothing_places_is_said_once(servers, door):
    reply = _call(door, {"requirements": "a wall hook", "material": UNPLACED})
    assert reply.get("success") is not False, reply
    assert _said(reply, material_limits.UNRECOGNISED) == 1


@pytest.mark.parametrize("door", DOORS)
def test_a_plastic_kiln_lists_says_nothing_of_the_kind(servers, door):
    reply = _call(door, {"requirements": "a wall hook", "material": "abs"})
    assert _said(reply, material_limits.UNRECOGNISED) == 0


@pytest.mark.parametrize("door", DOORS)
def test_a_miss_never_says_it(servers, door):
    servers["mode"] = "signed_out"
    reply = _call(door, {"requirements": "a wall hook", "material": UNPLACED})
    assert _said(reply, material_limits.UNRECOGNISED) == 0


def test_a_name_the_limits_place_is_read_as_that_plastic(servers):
    reply = _call("analyze_design_requirements", {"requirements": "a wall hook", "material": PLACED_BY_THE_LIMITS})
    assert PLACED_BY_THE_LIMITS.lower() in servers["asked"]
    assert reply["combined_rules"]["material_max_unsupported_overhang_deg"] == OWN_OVERHANG
    assert _said(reply, material_limits.UNRECOGNISED) == 0


@pytest.mark.parametrize("door", ("build_generation_prompt", "build_parametric_prompt"))
def test_a_prompt_holds_the_limits_of_a_name_the_limits_place(servers, door):
    reply = _call(door, {"requirements": "a wall hook", "material": PLACED_BY_THE_LIMITS})
    assert f"no overhangs greater than {OWN_OVERHANG} degrees" in json.dumps(reply)
