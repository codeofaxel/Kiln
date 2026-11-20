"""The design advisor reads no plastic's design limits, because it shows none.

``design_advisor`` shows the requirement brief's own rules and never a
plastic's own figures, so a plastic it names is not asked of Kiln's servers
(where each distinct plastic a day counts against the caller's allowance).
The brief it reads still carries every other rule.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


def _forget() -> None:
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    _forget()
    yield
    _forget()


def test_the_advisor_asks_for_no_plastics_limits(monkeypatch):
    import kiln.server as srv
    from kiln.plugins.design_reasoning_tools import plugin

    asked: list[str] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        asked.append(tool_name)
        return {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"}

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    tools: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    answer = tools["design_advisor"]("a wall bracket that holds 5 kg", material="abs")
    assert answer["recommended_approach"]
    assert "material_limits" not in asked


def test_a_brief_left_without_limits_keeps_its_own_rules(monkeypatch):
    import kiln.server as srv
    from kiln.design_intelligence import get_design_constraints

    monkeypatch.setattr(srv, "_pro_api_call", lambda *a, **k: {"success": False, "code": "X", "why": "signed_out"})
    with_limits = get_design_constraints("a wall bracket that holds 5 kg", material="abs").combined_rules
    without = get_design_constraints(
        "a wall bracket that holds 5 kg", material="abs", material_limits=False,
    ).combined_rules
    assert {k: v for k, v in with_limits.items() if not k.startswith("material_")} == {
        k: v for k, v in without.items() if not k.startswith("material_")
    }
    assert "material_max_unsupported_overhang_deg" in with_limits
    assert "material_max_unsupported_overhang_deg" not in without
