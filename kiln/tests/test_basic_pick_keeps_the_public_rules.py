"""A material answer with no pick still keeps Kiln's public rules for the part.

Signed out or offline, the material doors list the plastics a printer can
melt instead of picking one.  The public requirement rules for what a part
must do (stand the heat of a car, hold a load) leave some plastics out; the
list leaves them out too, says so in one line naming the rules, and the load
caution is still said.  Nothing is ranked.  Pinned through the doors:
``recommend_design_material``, ``analyze_design_requirements`` and
``recommend_material``.
"""

from __future__ import annotations

import socket
import sys

import pytest

from tests._plan_callers import free_caller  # noqa: F401 -- a fixture

DASHBOARD = "phone mount for a hot car dashboard"
BRACKET = "shelf bracket that holds 5 kg"
#: What the public rules for these two parts leave out.
LEFT_OUT = {"PLA", "TPU"}


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    """No kiln-pro on this machine, and no answer remembered from another test."""
    from kiln import _pro_material_pick_bridge as pick_bridge
    from kiln import design_intelligence as di

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(pick_bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(pick_bridge, "_last_miss", None, raising=False)
    di._reset_knowledge_base()
    yield
    di._reset_knowledge_base()


@pytest.fixture(params=["signed_out", "offline"])
def posture(request, monkeypatch, free_caller):  # noqa: F811 -- the fixture above
    """Signed out: the real served door, no sign-in on this install.  Offline:
    every served ask fails the way a computer with no network fails."""
    if request.param == "offline":
        import kiln.server as srv

        def _no_network(*_a, **_k):
            raise socket.gaierror(8, "nodename nor servname provided")

        monkeypatch.setattr(srv, "_pro_api_call", _no_network)
    return request.param


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *_a, **_k):
        def keep(fn):
            self.tools[fn.__name__] = fn
            return fn

        return keep


def _tools(plugin) -> dict:
    mcp = _FakeMCP()
    plugin.register(mcp)
    return mcp.tools


def _design_doors() -> dict:
    from kiln.plugins.design_tools import plugin

    return _tools(plugin)


def _rules_line(lines: list[str]) -> str:
    found = [line for line in lines if line.startswith("Kiln's rules for ")]
    assert len(found) == 1, lines
    return found[0]


@pytest.mark.parametrize("part", [DASHBOARD, BRACKET])
def test_the_design_pick_leaves_out_what_the_rules_leave_out(posture, part):
    out = _design_doors()["recommend_design_material"](part)
    assert out["basic"] is True and out["why"] == posture
    assert not LEFT_OUT & set(out["plastics"]), out["plastics"]
    assert "PETG" in out["plastics"]
    line = _rules_line(out["warnings"])
    assert "leave out PLA and TPU" in line
    # The load caution is still said beside the list.
    assert any(w.startswith("This appears to be a load-bearing application") for w in out["warnings"])
    for word in ("best", "score", "rank"):
        assert word not in line.lower()


def test_a_hot_part_is_told_why_in_the_rules_own_words(posture):
    out = _design_doors()["recommend_design_material"](DASHBOARD)
    line = _rules_line(out["warnings"])
    assert "Heat Resistant / High Temperature" in line
    # PETG stays listed, said as allowed only with conditions for heat.
    assert "allow PETG only with conditions" in line


@pytest.mark.parametrize("part", [DASHBOARD, BRACKET])
def test_the_analysis_never_lists_what_its_own_rules_exclude(posture, part):
    from kiln.catalog_keys import resolve_material_key

    out = _design_doors()["analyze_design_requirements"](part)
    assert out["success"] is True
    basic = out["material_basic"]
    assert basic["basic"] is True
    excluded = out["combined_rules"]["material_exclude"]
    contradicted = [p for p in basic["plastics"] if resolve_material_key(p, excluded) is not None]
    assert not contradicted, (basic["plastics"], excluded)


@pytest.mark.parametrize("part", [DASHBOARD, BRACKET])
def test_the_intent_pick_leaves_out_what_the_rules_leave_out(posture, part):
    from kiln.plugins.intelligence_tools import plugin

    out = _tools(plugin)["recommend_material"](intent=part)
    assert out["success"] is True and out["basic"] is True and out["why"] == posture
    assert not LEFT_OUT & set(out["plastics"]), out["plastics"]
    assert "Kiln's rules for " in out["message"] and "leave out PLA and TPU" in out["message"]


def test_rules_that_leave_out_every_plastic_name_what_they_ask_for(posture):
    """A static-sensitive part: none of the common plastics suits it.  The
    list is empty, and the line names what the rules ask for instead, so the
    answer is never a dead end."""
    out = _design_doors()["recommend_design_material"]("a pcb holder for my bench")
    assert out["basic"] is True and out["plastics"] == []
    line = _rules_line(out["warnings"])
    assert "leave out every plastic listed here" in line and "PETG-ESD" in line
