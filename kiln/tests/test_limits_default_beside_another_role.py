"""A door's own default plastic is read as assumed unless the call names it.

A tool call whose own arguments name a plastic for another role (a support
material in a second slot), or carry a word in an argument that is not a
plastic at all (a filament colour), has not been told the plastic its door
defaults to.  The assumed plastic read in such a call is asked uncounted;
only an argument that names it (alone, in a list or in a table) makes it the
person's choice.  Inside a tool call as the dispatcher marks one, on an
install without kiln-pro, with Kiln's servers stood in.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from kiln import material_limits
from kiln.tool_context import enter_tool, leave_tool


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge

    material_limits._answers.clear()
    material_limits._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture
def asked(monkeypatch) -> list[dict[str, Any]]:
    """Every design-limits ask: the plastic and whether it was sent as assumed."""
    import kiln.server as srv

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    seen: list[dict[str, Any]] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        seen.append({"material": kwargs.get("material"), "assumed": kwargs.get("assumed")})
        return {"success": False, "code": "SERVER_UNREACHABLE", "error": "No answer.", "why": "unanswered"}

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    _forget()
    yield seen
    _forget()


def _read_pla_in_a_call(arguments: dict[str, Any]) -> None:
    token = enter_tool("a_door", arguments)
    try:
        material_limits.limits_for("PLA")
    finally:
        leave_tool(token)


@pytest.mark.parametrize(
    "arguments",
    [
        {"second_material": "PVA"},
        {"support_material_name": "BVOH"},
        {"target_filament_color": "red"},
        {"filament_colors": ["red", "blue"]},
        {},
    ],
)
def test_the_doors_default_is_assumed_when_no_argument_names_it(asked, arguments):
    _read_pla_in_a_call(arguments)
    assert asked and asked[-1]["assumed"] is True, (arguments, asked)


@pytest.mark.parametrize(
    "arguments",
    [
        {"material": "PLA"},
        {"material": " pla "},
        {"materials": ["PETG", "pla"]},
        {"filament_map": {"0": "PLA", "1": "PVA"}},
    ],
)
def test_an_argument_naming_it_makes_it_the_persons_choice(asked, arguments):
    _read_pla_in_a_call(arguments)
    assert asked and not asked[-1]["assumed"], (arguments, asked)


def test_outside_a_tool_call_nothing_is_assumed(asked):
    material_limits.limits_for("PLA")
    assert asked and not asked[-1]["assumed"]
