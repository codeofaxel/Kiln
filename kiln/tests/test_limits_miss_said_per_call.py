"""Why a check read the general figures is said in every call that meets it.

:meth:`kiln.material_limits.MaterialLimits.notes` says a miss's sentence once
per cause.  Inside a tool call "once" is that call: one call is one caller,
so every caller who meets the miss hears why (Kiln's servers run one process
for every caller).  Outside a call it is once per process, as before.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln import material_limits  # noqa: E402

DOOR = "validate_design_for_requirements"


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture(autouse=True)
def _signed_out(monkeypatch):
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)

    def signed_out(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        return {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"}

    monkeypatch.setattr(srv, "_pro_api_call", signed_out)
    _forget()
    yield
    _forget()


def _call(name: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Run *fn* as one tool call, the way the dispatch chokepoints mark it."""
    from kiln.tool_context import enter_tool, leave_tool

    token = enter_tool(name, kwargs)
    try:
        return fn(*args, **kwargs)
    finally:
        leave_tool(token)


def test_every_call_that_meets_a_miss_says_why():
    first = _call("one", lambda: material_limits.limits_for("ABS").notes())
    second = _call("two", lambda: material_limits.limits_for("PETG").notes())
    sentence = material_limits.miss_sentence(material_limits.limits_for("ABS").miss)
    assert first and first[0] == sentence
    assert second and second[0] == sentence


def test_one_call_says_a_cause_once():
    def twice() -> list[str]:
        return material_limits.limits_for("ABS").notes() + material_limits.limits_for("PETG").notes()

    lines = _call("one", twice)
    assert sum("against the general figures" in line for line in lines) == 1


def test_outside_a_call_a_cause_is_said_once_per_process():
    assert material_limits.limits_for("ABS").notes()
    assert material_limits.limits_for("PETG").notes() == []


def test_two_callers_of_the_validator_both_hear_why(tmp_path):
    from kiln.plugins.design_tools import plugin

    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    part = tmp_path / "box.stl"
    trimesh.creation.box(extents=(20, 20, 10)).export(part)
    for material in ("abs", "petg"):
        reply = _call(DOOR, tools[DOOR], str(part), "a desk organizer", material=material)
        assert "general figures" in reply["summary"], (material, reply["summary"])
