"""An exception raised on this computer is logged here and never told to the caller.

Every bridge to kiln-pro answers in process when kiln-pro is installed beside
public Kiln, and on Kiln's servers that process serves a remote caller.  A
bridge used to put the exception's own text into the miss it worded, and the
door passed it out (``why_detail``, a warning, a clause), so a path, a query
or an internal name could reach whoever asked.  The one helper
(``kiln.served_answer.local_fault``) keeps the text in the log and gives a
fixed detail; this walks each bridge with an exception carrying a made-up
path and query, and checks the answer a door returns.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from typing import Any

import pytest

def _forget() -> None:
    """Drop every kept answer, said miss and service backoff."""
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper

    helper._answers.clear()
    helper._said.clear()
    bridge._service_down_until, bridge._service_down_miss = 0.0, None


#: What a fault on the server could carry: a path, a query, a tenant.
SECRET = "/srv/kiln/tenant-7f3a/private.sqlite: SELECT api_key FROM kilntest_tenants"
MARKERS = ("tenant-7f3a", "kilntest_tenants", "api_key", "private.sqlite")


class _Boom(RuntimeError):
    pass


def _raise(*_a: Any, **_k: Any) -> Any:
    raise _Boom(SECRET)


def _stub(monkeypatch, dotted: str, **attrs: Any) -> None:
    """Put a kiln-pro module stand-in at *dotted* (and its parents)."""
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        name = ".".join(parts[:i])
        if not isinstance(sys.modules.get(name), types.ModuleType) or name == "kiln_pro":
            pkg = types.ModuleType(name)
            pkg.__path__ = []  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, name, pkg)
    module = types.ModuleType(dotted)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, dotted, module)


def _assert_kept_local(out: Any, caplog) -> None:
    text = json.dumps(out, default=str)
    for marker in MARKERS:
        assert marker not in text, f"the exception's text reached the answer: {text[:600]}"
    assert SECRET in caplog.text, "the exception was not logged on this computer"


@pytest.fixture(autouse=True)
def _log(caplog):
    caplog.set_level(logging.DEBUG)


def test_the_load_figure(monkeypatch, caplog):
    import kiln.licensing as licensing
    from kiln import _pro_load_bridge
    from kiln.plugins.design_tools import plugin

    _stub(monkeypatch, "kiln_pro.load_capacity", answer=_raise)
    monkeypatch.setattr(_pro_load_bridge, "_local_available", lambda: True)
    monkeypatch.setattr(licensing, "check_tier", lambda *a, **k: (True, None))
    tools: dict = {}

    class _Mcp:
        def tool(self, **_kw):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    out = tools["estimate_structural_load"]("petg", 24.0, 100.0)
    assert out["success"] is False and out["code"] == "LOAD_FIGURES_UNAVAILABLE"
    _assert_kept_local(out, caplog)


def test_the_material_record(monkeypatch, caplog):
    from kiln import _pro_material_bridge as bridge
    from kiln import design_intelligence as di

    _stub(monkeypatch, "kiln_pro.material_catalogue", material_record=_raise, catalogue_present=lambda: True)
    monkeypatch.setattr(bridge, "available", lambda: True)
    bridge.forget()
    di._reset_knowledge_base()
    try:
        brand = sorted(di._get_kb().materials["petg"].get("brand_profiles") or {})[0]
        out = {
            "resolved": di.resolve_filament(brand).to_dict(),
            "why": di.brand_figures_unanswered("petg"),
            "miss": bridge.last_miss("petg").__dict__,
        }
        assert any("not in hand" in w for w in out["resolved"]["warnings"])
        _assert_kept_local(out, caplog)
    finally:
        bridge.forget()
        di._reset_knowledge_base()


def test_the_printer_record(monkeypatch, caplog):
    from kiln import _pro_catalogue_bridge as bridge

    _stub(monkeypatch, "kiln_pro.device_intelligence.printer_catalogue", printer_record=_raise, catalogue_present=lambda: True)
    monkeypatch.setattr(bridge, "available", lambda: True)
    bridge.forget()
    try:
        assert bridge.printer_record("bambu_a1") is None
        out = {"why": bridge.unanswered("bambu_a1"), "miss": bridge.last_miss("bambu_a1").__dict__}
        assert out["why"]
        _assert_kept_local(out, caplog)
    finally:
        bridge.forget()


def test_the_cost_intelligence(monkeypatch, caplog, tmp_path):
    from kiln import _pro_cost_bridge as bridge

    _stub(monkeypatch, "kiln_pro.cost_intelligence", consult=_raise)
    monkeypatch.setattr(bridge, "available", lambda: True)
    gcode = tmp_path / "part.gcode"
    gcode.write_text("G1 X1\n")
    estimate: dict[str, Any] = {"warnings": [], "filaments": [{"material": "PLA"}, {"material": "PETG"}]}
    bridge.attach_cost_intelligence(estimate, str(gcode), printer_id="bambu_a1")
    out = {"estimate": estimate, "unanswered": bridge.unanswered()}
    assert estimate.get("cost_intelligence") is None and estimate["warnings"]
    _assert_kept_local(out, caplog)


def test_the_melt_rates(monkeypatch, caplog):
    from kiln import _pro_melt_bridge as bridge

    _stub(monkeypatch, "kiln_pro.device_intelligence.melt_rate_intelligence", melt_rate=_raise)
    monkeypatch.setattr(bridge, "available", lambda: True)
    bridge.forget()
    try:
        assert bridge.printer_melt_rate("bambu_a1", 0.4, "pla") is None
        out = {"why": bridge.unanswered("bambu_a1", 0.4)}
        assert out["why"]
        _assert_kept_local(out, caplog)
    finally:
        bridge.forget()


def test_the_material_rule_check(monkeypatch, caplog):
    from kiln import _pro_material_rules_bridge as bridge

    _stub(monkeypatch, bridge.LOCAL_MODULE, rule_check=_raise)
    out = bridge.rule_checks("pla", ["food_safe"])
    assert out["checked"] is False
    _assert_kept_local(out, caplog)


def test_the_substitution_ranking(monkeypatch, caplog):
    import kiln.server as srv
    from kiln import _pro_substitution_bridge as bridge

    _stub(monkeypatch, bridge.LOCAL_MODULE, substitutes_for_the_wire=_raise)
    out = {"list": srv.find_material_substitute("pla"), "best": srv.get_best_material_substitute("pla")}
    assert out["list"]["basic"] is True and out["best"]["basic"] is True
    assert out["list"]["why"] == "unanswered"
    _assert_kept_local(out, caplog)


def test_the_material_recommendation(monkeypatch, caplog):
    from kiln import _pro_routing_bridge as bridge
    from kiln.material_routing import material_list, recommend_material

    _stub(monkeypatch, bridge.LOCAL_MODULE, for_the_wire=_raise)
    out = {"pick": recommend_material("strong").to_dict(), "list": material_list()}
    assert out["pick"]["basic"] is True and out["list"]["basic"] is True
    _assert_kept_local(out, caplog)


def test_the_adaptive_slicing_settings(monkeypatch, caplog):
    from kiln import _pro_adaptive_bridge as bridge
    from kiln.plugins.adaptive_slicing_tools import quick_adaptive_plan

    _stub(monkeypatch, bridge.LOCAL_MODULE, answer=_raise)
    out = quick_adaptive_plan("petg", 12.0, nozzle_diameter_mm=0.4)
    assert out["success"] is True and out["basic"] is True
    _assert_kept_local(out, caplog)


def test_the_pairing_check(monkeypatch, caplog):
    from kiln import _pro_material_pairing_bridge as bridge
    from kiln.design_intelligence import check_multi_material_compatibility

    _stub(monkeypatch, bridge.LOCAL_MODULE, co_print_with_notes=_raise)
    out = check_multi_material_compatibility("pla", "petg").to_dict()
    assert out["basic"] is True and out["compatible"] is None
    _assert_kept_local(out, caplog)


def test_the_design_limits(monkeypatch, caplog):
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits

    _stub(monkeypatch, bridge.LOCAL_MODULE, for_the_wire=_raise)
    _forget()
    try:
        limits = material_limits.limits_for("pla")
        assert limits.basic and limits.miss is not None
        out = {"limits": limits.to_dict(), "notes": limits.notes()}
        assert out["notes"]
        _assert_kept_local(out, caplog)
    finally:
        _forget()


def test_the_multi_material_check(monkeypatch, caplog):
    from kiln import _pro_material_pairing_bridge as bridge
    from kiln.material_safety import check_material_compatibility

    _stub(monkeypatch, bridge.MIX_LOCAL_MODULE, pair_entries=_raise)
    out = check_material_compatibility(["PLA", "ABS"])
    assert out["basic"] is True and out["safe"] is False
    _assert_kept_local(out, caplog)


def test_the_placement_check(monkeypatch, caplog):
    from kiln import _pro_placement_bridge as bridge
    from kiln import served_answer

    monkeypatch.setattr(bridge, "_local_pro", lambda: None)
    monkeypatch.setattr(bridge, "_served", _raise)
    doc, miss = bridge.ask({"printer_id": "bambu_a1"})
    assert doc is None and miss is not None
    out = {
        "miss": miss.__dict__,
        "sentence": served_answer.sentence(
            miss, feature="placement check", on_the_line="x", cannot="check the plate", wont="won't slice",
        ),
        "fields": served_answer.fields(miss),
    }
    _assert_kept_local(out, caplog)


def test_the_helper_says_where_and_nothing_else(caplog):
    from kiln.served_answer import local_fault

    miss = local_fault(_Boom(SECRET))
    assert (miss.cause, miss.code, miss.detail) == ("unanswered", "NOT_ASKED", "kiln-pro on this computer stopped")
    assert SECRET in caplog.text
    assert local_fault(_Boom(SECRET), where="cost intelligence on this computer").detail == (
        "cost intelligence on this computer stopped"
    )
