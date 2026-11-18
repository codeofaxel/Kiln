"""What the general figures leave out is said on every door that read them.

When a check reads a plastic's design limits and gets the general figures
instead of its own (a miss, or the day's allowance used), a flexible plastic
needs more margin than they give and a static-safe grade's own minimum wall
is not known.  The doors that build their own words said this; the others
(the printability check, the prompt builders, a slice) said nothing.  The
one helper every check reads now notes the reason and the caution on the
running call, so every door's reply says them once.  Stood in here: an
install without kiln-pro, never signed in.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from kiln import material_limits

FLEXIBLE = (
    "The general figures are for ordinary rigid plastics; a flexible "
    "plastic needs more margin than they give."
)
STATIC_SAFE = (
    "This static-safe grade's own minimum wall for its electrical "
    "property is not known here; the wall figure is the general one."
)
SIGNED_OUT = {"status": "error", "success": False, "code": "KILN_AUTH_REQUIRED", "why": "signed_out", "error": "Sign in."}


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture(autouse=True)
def signed_out(monkeypatch):
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    monkeypatch.setattr(srv, "_pro_api_call", lambda tool_name, **kwargs: dict(SIGNED_OUT))
    yield
    _forget()


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

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    importlib.import_module(module_path).plugin.register(_Mcp())
    return tools


def _cup(folder) -> str:
    trimesh = pytest.importorskip("trimesh")
    part = folder / "box.stl"
    trimesh.creation.box(extents=(20, 20, 10)).export(part)
    return str(part)


def _said_once(reply: Any, line: str) -> None:
    text = json.dumps(reply, default=str)
    assert text.count(json.dumps(line)[1:-1]) == 1, (line, text[:2000])


def _doors(tmp_path, material: str) -> dict[str, Any]:
    design = _registered("kiln.plugins.design_tools")
    printability = _registered("kiln.plugins.printability_tools")
    return {
        "analyze_printability": _call(
            "analyze_printability", printability["analyze_printability"], _cup(tmp_path), material=material,
        ),
        "build_generation_prompt": _call(
            "build_generation_prompt", design["build_generation_prompt"], "a phone case", material=material,
        ),
        "analyze_design_requirements": _call(
            "analyze_design_requirements", design["analyze_design_requirements"], "a phone case",
            material=material,
        ),
    }


def test_a_flexible_plastic_answered_generally_is_cautioned_on_every_door(tmp_path):
    for door, reply in _doors(tmp_path, "TPU 85A").items():
        _said_once(reply, FLEXIBLE)
        assert "Sign in" in json.dumps(reply) or "sign in" in json.dumps(reply), door


def test_a_static_safe_grade_answered_generally_says_its_floor_is_not_known(tmp_path):
    for _door, reply in _doors(tmp_path, "PETG-ESD").items():
        _said_once(reply, STATIC_SAFE)


def test_a_rigid_plastic_carries_no_caution(tmp_path):
    for _door, reply in _doors(tmp_path, "ABS").items():
        text = json.dumps(reply)
        assert FLEXIBLE not in text
        assert STATIC_SAFE not in text


def test_the_cautions_are_the_helpers_own():
    assert material_limits.FLEXIBLE_CAUTION == FLEXIBLE
    assert material_limits.STATIC_SAFE_UNKNOWN == STATIC_SAFE
