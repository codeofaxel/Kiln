"""recommend_material's NOZZLE ADVISORY on a free plan, on a plain install.

The abrasive-filament verdict is one banded door: every plan is warned
that the filament is abrasive on the brass nozzle on record and told to fit
a hardened one; Kiln Pro's answer goes on to the figure (the grams budget,
the tip's wear), and a free answer says once, in ``pro_adds``, what Kiln
Pro adds.  A free plan's served answer is never a bare refusal that hides
the advisory, and a bridge with kiln-pro present never hands the full
verdict to every caller.

These tests run with kiln-pro made unimportable, as on every customer's
install, with Kiln's servers stood in for at the one served door.

A/B: the free-band and Pro-band served tests fail on the pre-fix tree on the
``pro_adds`` line the advisory did not carry; the in-process test fails on
the ``depth`` the bridge did not pass.
"""

from __future__ import annotations

import re
import sys
import types
from typing import Any

import pytest

FREE_BAND = {
    "success": True,
    "status": "ok",
    "tool": "abrasive_escalation",
    "severity": "red",
    "escalated": True,
    "filament_class": "abrasive",
    "nozzle_material": "brass",
    "user_warning": (
        "Your printer has a brass nozzle installed.  PETG-CF is an abrasive "
        "filament (carbon, glass, wood or metal fill) and it wears brass fast.  "
        "Fit a hardened steel nozzle before this print."
    ),
    "swap_recommendation": "hardened_steel",
    "pro_adds": (
        "Kiln Pro adds the figure: how much abrasive filament a brass nozzle has "
        "in it, what this nozzle's own odometer says is left, and how far its "
        "tip has worn.  https://kiln3d.com/pricing"
    ),
}
PRO_BAND = {
    **{k: v for k, v in FREE_BAND.items() if k != "pro_adds"},
    "user_warning": FREE_BAND["user_warning"]
    + "  Brass bore-widens in ~500 g of abrasive filament; expect dimensional "
    "drift, under-extrusion, and a shorter nozzle life than a steel swap would "
    "give you.  Recommended swap: hardened steel (~$15).",
    "tip_wear_fraction": None,
}
_FIGURE = re.compile(r"\d+\s?(?:g\b|%)")


def _on_hand(material: str, printer: str) -> list[dict]:
    return [{
        "material_type": material,
        "loaded_on": [{"printer_name": printer, "tool_index": 0, "remaining_grams": 500.0, "color": "black", "spool_id": None}],
        "shelf_spools": [],
        "total_grams": 500.0,
    }]


@pytest.fixture
def plain_install(monkeypatch):
    """No kiln-pro: any ``import kiln_pro...`` raises ImportError."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def servers(monkeypatch, plain_install):
    """Kiln's servers at the one served door, answering a chosen band."""
    from kiln import _pro_nozzle_bridge as bridge
    import kiln.server as server

    state: dict[str, Any] = {"abrasive": FREE_BAND, "asked": []}

    def _pro_api_call(tool_name: str, **kwargs: Any) -> dict[str, Any]:
        state["asked"].append((tool_name, {k: v for k, v in kwargs.items() if not k.startswith("_")}))
        if tool_name == "abrasive_escalation":
            return dict(state["abrasive"])
        return {"success": False, "code": "SERVER_UNREACHABLE"}

    monkeypatch.setattr(server, "_pro_api_call", _pro_api_call)
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "_last_abrasive_miss", {})
    monkeypatch.setattr(bridge, "_record_memo", {})
    return state


def _recommend(printer: str = "default"):
    from kiln.material_routing import recommend_material

    return recommend_material(
        "strong functional bracket",
        printer_id=printer,
        on_hand=_on_hand("PETG-CF", printer),
    )


class TestTheServedAdvisoryOnAPlainInstall:
    def test_a_free_plan_sees_the_warning_and_what_pro_adds_once(self, servers):
        rec = _recommend()
        assert "NOZZLE ADVISORY" in rec.reasoning
        advisory = rec.reasoning.split("\n\n", 1)[0]
        assert "brass" in advisory and "hardened" in advisory and "PETG-CF" in advisory
        assert not _FIGURE.search(advisory), advisory
        assert "Kiln Pro adds" in advisory and advisory.count("kiln3d.com/pricing") == 1
        assert "ABRASIVE FILAMENT" not in rec.reasoning, "the material-only floor is not said twice"
        assert [name for name, _ in servers["asked"] if name == "abrasive_escalation"] == ["abrasive_escalation"]

    def test_a_pro_plan_sees_the_figure_and_no_pitch(self, servers):
        servers["abrasive"] = PRO_BAND
        rec = _recommend()
        advisory = rec.reasoning.split("\n\n", 1)[0]
        assert "NOZZLE ADVISORY" in advisory and "500 g" in advisory
        assert "kiln3d.com/pricing" not in rec.reasoning and "Kiln Pro adds" not in rec.reasoning

    def test_no_answer_leaves_the_material_only_floor(self, servers):
        servers["abrasive"] = {"success": False, "code": "SERVER_UNREACHABLE"}
        rec = _recommend()
        assert "NOZZLE ADVISORY" not in rec.reasoning
        assert "ABRASIVE FILAMENT" in rec.reasoning


class TestTheInProcessBridgeAsksThePlan:
    """With kiln-pro beside it (Kiln's servers), the bridge reads the caller's
    plan through ``kiln.licensing.check_tier`` and hands the engine ``depth``:
    withheld on a free plan, given on Pro.  kiln-pro is stood in for by a
    recording stub; the plan read and the hand-off are the real path."""

    @pytest.fixture
    def stub_kiln_pro(self, monkeypatch):
        calls: list[dict[str, Any]] = []
        state = object()

        pkg = types.ModuleType("kiln_pro")
        ni = types.ModuleType("kiln_pro.nozzle_intelligence")
        resolver = types.ModuleType("kiln_pro.nozzle_intelligence.store_resolver")
        verdicts = types.ModuleType("kiln_pro.nozzle_intelligence.verdicts")
        resolver.resolve_backend = lambda **_k: (object(), None)
        resolver.resolve_state_or_factory_default = lambda _backend, _printer: state

        def abrasive_escalation(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return dict(PRO_BAND if kwargs.get("depth") else FREE_BAND)

        verdicts.abrasive_escalation = abrasive_escalation
        for name, mod in (
            ("kiln_pro", pkg), ("kiln_pro.nozzle_intelligence", ni),
            ("kiln_pro.nozzle_intelligence.store_resolver", resolver),
            ("kiln_pro.nozzle_intelligence.verdicts", verdicts),
        ):
            monkeypatch.setitem(sys.modules, name, mod)
        return calls

    @pytest.mark.parametrize("covered", [False, True])
    def test_the_plan_decides_the_depth(self, monkeypatch, stub_kiln_pro, covered):
        import kiln.licensing  # noqa: F401 -- bound in sys.modules, whichever module it is

        licensing = sys.modules["kiln.licensing"]
        asked: list[Any] = []

        def check_tier(required: Any, *_a: Any, **_k: Any):
            asked.append(required)
            return (True, None) if covered else (False, "This feature requires Kiln Pro.")

        monkeypatch.setattr(licensing, "check_tier", check_tier)
        from kiln._pro_nozzle_bridge import consult_abrasive_escalation

        out = consult_abrasive_escalation("PETG-CF", "default")
        assert asked and getattr(asked[0], "value", asked[0]) in ("pro", licensing.LicenseTier.PRO)
        assert stub_kiln_pro[0]["depth"] is covered
        assert ("pro_adds" in out) is not covered
