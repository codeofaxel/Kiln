"""Every material-inventory door goes through one router, split by number of machines.

One machine: the answer is worked out here from this install's records,
offline, nothing asked.  Several machines below the plan that covers them:
the fleet refusal, nothing asked.  On that plan (Kiln Business): Kiln's
servers answer, sent only the rows the question reads.  The doors are walked
by name, so a door added later that answers on its own fails here.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from kiln.persistence import KilnDB

#: Every door that answers a material question about the machines, and a call.
DOORS: list[tuple[str, dict[str, Any], str]] = [
    ("get_fleet_material_summary", {}, "summary"),
    ("get_material_consumption_history", {"days": 14}, "history"),
    ("forecast_material_consumption", {"material_type": "PLA"}, "forecast"),
    ("get_restock_suggestions", {}, "restock"),
    ("find_printers_with_material", {"material_type": "PLA"}, "find"),
    ("optimize_fleet_assignment", {"jobs": [{"file_name": "a.gcode", "material_type": "PLA", "required_grams": 5.0}]}, "assign"),
    ("suggest_spool_swaps", {"jobs": [{"material_type": "ABS", "required_grams": 5.0}]}, "swaps"),
    ("check_material_sufficiency", {"printer_name": "a1", "required_grams": 900.0}, "alternatives"),
    ("recommend_material", {"intent": "strong", "on_hand_only": True}, "on_hand"),
]

#: What a row of each kind may carry when it leaves this computer.
SENT_FIELDS = {
    "spools": {"id", "material_type", "color", "brand", "remaining_grams"},
    "loaded": {"printer_name", "tool_index", "material_type", "color", "spool_id", "remaining_grams"},
    "history": {"material_type", "completed_at", "filament_used_mm"},
    "jobs": {"file_name", "material_type", "required_grams", "color"},
}


class _Registry:
    def __init__(self, names: list[str]):
        self.names = names

    @property
    def count(self) -> int:
        return len(self.names)

    def list_names(self) -> list[str]:
        return list(self.names)


@pytest.fixture()
def db(tmp_path):
    db = KilnDB(str(tmp_path / "inv.db"))
    db.save_spool({"id": "s1", "material_type": "PLA", "color": "white", "brand": "Acme", "weight_grams": 1000.0,
                   "remaining_grams": 300.0, "notes": "kilntest private note", "vendor": "kilntest vendor"})
    db.save_spool({"id": "s2", "material_type": "ABS", "color": None, "brand": "Corx", "weight_grams": 1000.0,
                   "remaining_grams": 700.0})
    db.save_material("a1", 0, "PLA", color="white", spool_id="s1", remaining_grams=300.0)
    db.save_material("b2", 0, "PETG", color="black", spool_id=None, remaining_grams=900.0)
    db.save_print_record({"job_id": "j1", "printer_name": "a1", "file_name": "/home/kilntest/secret-part.gcode",
                          "status": "completed", "material_type": "PLA", "completed_at": 1.0,
                          "metadata": {"filament_used_mm": 1000.0, "kilntest": "private"}})
    yield db
    db.close()


@pytest.fixture()
def tools(db, monkeypatch):
    monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
    captured: dict[str, Any] = {}

    class _Mcp:
        def tool(self, **_kw):
            def keep(fn):
                captured[fn.__name__] = fn
                return fn

            return keep

    from kiln.plugins.intelligence_tools import plugin as intel
    from kiln.plugins.material_inventory_tools import plugin as inv

    inv.register(_Mcp())
    intel.register(_Mcp())
    return captured


@pytest.fixture()
def machines(monkeypatch):
    """Sets how many machines are registered and the plan's printer cap."""
    import kiln.registry as registry_mod

    def _set(names: list[str], cap: int | None) -> None:
        monkeypatch.setattr(registry_mod, "get_registry", lambda: _Registry(names))
        lic = sys.modules.get("kiln.licensing")
        if lic is None:
            lic = types.ModuleType("kiln.licensing")
            monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
        monkeypatch.setattr(lic, "get_tier", lambda: "plan", raising=False)
        monkeypatch.setattr(lic, "max_printers_for_tier", lambda _t: cap, raising=False)

    return _set


@pytest.fixture()
def servers(monkeypatch):
    """Kiln's servers, stood in for; every ask recorded."""
    import kiln.server as srv

    asks: list[dict[str, Any]] = []

    def answer(name, /, _timeout=None, _asked_by_user=True, question=None, params=None, **rows):
        asks.append({"tool": name, "question": question, "params": params, "rows": rows})
        value: Any = {"stood_in": True} if question == "forecast" else []
        return {"success": True, "status": "ok", "question": question, "answer": value}

    monkeypatch.setattr(srv, "_pro_api_call", answer)
    monkeypatch.setitem(sys.modules, "kiln_pro.fleet_materials", None)
    return asks


@pytest.mark.parametrize(("door", "kwargs", "question"), DOORS, ids=[d[0] for d in DOORS])
def test_one_machine_answers_here_and_asks_nothing(tools, machines, servers, door, kwargs, question):
    machines(["a1"], cap=1)
    out = tools[door](**kwargs)
    assert out.get("success") is True, out
    assert servers == []


@pytest.mark.parametrize(("door", "kwargs", "question"), DOORS, ids=[d[0] for d in DOORS])
def test_several_machines_below_the_plan_ask_nothing(tools, machines, servers, door, kwargs, question):
    machines(["a1", "b2", "c3"], cap=1)
    out = tools[door](**kwargs)
    assert servers == [], f"{door} asked Kiln's servers below the plan"
    if door == "check_material_sufficiency":
        # The one door whose own verdict is never gated: the other machines
        # are withheld and counted, as before.
        assert out["check"]["alternative_printers"] == [] and out["check"]["sufficient"] is False
    else:
        assert out["success"] is False and out["code"] == "TIER_FLEET_SCOPE"


@pytest.mark.parametrize(("door", "kwargs", "question"), DOORS, ids=[d[0] for d in DOORS])
def test_on_the_plan_the_servers_answer_from_only_the_rows_needed(tools, machines, servers, door, kwargs, question):
    from kiln.material_inventory import FLEET_QUESTIONS

    machines(["a1", "b2", "c3"], cap=50)
    out = tools[door](**kwargs)
    assert out.get("success") is True, out
    assert [(a["tool"], a["question"]) for a in servers] == [("plan_fleet_materials", question)]
    rows = servers[0]["rows"]
    assert set(rows) == set(FLEET_QUESTIONS[question]), (question, sorted(rows))
    for kind, records in rows.items():
        for record in records:
            assert set(record) <= SENT_FIELDS[kind], (kind, sorted(record))
    text = json.dumps(servers)
    for private in ("kilntest private note", "kilntest vendor", "secret-part", "kilntest\": \"private"):
        assert private not in text


def test_every_door_asks_the_one_router(tools, machines, monkeypatch):
    """A/B: a door that answered on its own -- its own gate, its own engine --
    is a door this split does not cover."""
    import kiln.material_inventory as mi

    asked: list[str] = []
    real = mi.inventory_answer

    def spy(question, db, **kw):
        asked.append(question)
        return real(question, db, **kw)

    monkeypatch.setattr(mi, "inventory_answer", spy)
    machines(["a1", "b2", "c3"], cap=1)
    for door, kwargs, question in DOORS:
        asked.clear()
        tools[door](**kwargs)
        if door == "check_material_sufficiency":
            assert asked == [], "below the plan the sufficiency door counts the other machines itself"
        else:
            assert asked == [question], door


def test_a_miss_on_the_plan_is_worded_and_says_unanswered(tools, machines, monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_pro_api_call", lambda *a, **k: {
        "status": "error", "success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "x"})
    monkeypatch.setitem(sys.modules, "kiln_pro.fleet_materials", None)
    machines(["a1", "b2", "c3"], cap=50)
    out = tools["get_fleet_material_summary"]()
    assert out["success"] is False and out["code"] == "FLEET_ANSWER_UNAVAILABLE" and out["why"] == "signed_out"
    assert "one printer" in out["error"]
    check = tools["check_material_sufficiency"](printer_name="a1", required_grams=900.0)["check"]
    assert check["sufficient"] is False and check["alternative_printers"] == []
    assert check["fleet_alternatives"]["code"] == "FLEET_ANSWER_UNAVAILABLE"


def test_an_answer_in_the_wrong_shape_is_a_miss(tools, machines, monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_pro_api_call", lambda *a, **k: {
        "success": True, "status": "ok", "question": "summary", "answer": {"not": "a list"}})
    monkeypatch.setitem(sys.modules, "kiln_pro.fleet_materials", None)
    machines(["a1", "b2"], cap=50)
    out = tools["get_fleet_material_summary"]()
    assert out["success"] is False and out["why"] == "unanswered"


def test_the_bridge_is_on_the_hosted_doors_roster():
    from kiln.served_answer import HOSTED_DOORS

    assert HOSTED_DOORS["kiln._pro_fleet_materials_bridge"][0] == "served_answer"
