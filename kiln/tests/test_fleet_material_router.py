"""Every material-inventory door goes through one router, split by number of machines.

One machine: the answer is worked out here from this install's records,
offline, nothing asked.  Several machines below the plan that covers them:
the fleet refusal, nothing asked; a door that can answer for one named
printer asks which one instead, and answers for it.  On that plan (Kiln Business): Kiln's
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
    ("material_on_hand", {}, "summary"),
    ("get_fleet_material_summary", {}, "summary"),
    ("get_material_consumption_history", {"days": 14}, "history"),
    ("forecast_material_consumption", {"material_type": "PLA"}, "forecast"),
    ("get_restock_suggestions", {}, "restock"),
    ("find_printers_with_material", {"material_type": "PLA"}, "find"),
    ("assign_jobs", {"jobs": [{"file_name": "a.gcode", "material_type": "PLA", "required_grams": 5.0}]}, "assign"),
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

    def list_machines(self) -> list[str]:
        return sorted(self.names)

    def list_all(self) -> dict[str, Any]:
        return dict.fromkeys(self.names)


#: The doors that can answer for one named printer: below the plan, with
#: several printers and none named, they ask which one.
ASKS_WHICH_PRINTER = {"material_on_hand", "get_fleet_material_summary", "assign_jobs", "optimize_fleet_assignment"}


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
    elif door in ASKS_WHICH_PRINTER:
        assert out["success"] is True and out["ask"] == "Which printer?"
        assert [row["name"] for row in out["known_printers"]] == ["a1", "b2", "c3"] and out["count"] == 3
        assert out["note"].count("Kiln Business") == 1 and "printer_name" in out["note"]
        assert out["upgrade_url"].endswith(f"tool={door}")
        # Nothing about any printer's material is in the answer.
        assert "summary" not in out and "assignments" not in out and "PLA" not in json.dumps(out)
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


# ---------------------------------------------------------------------------
# One named printer answers for itself on every plan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [1, 50], ids=["below the plan", "on the plan"])
def test_a_named_printers_material_is_answered_here_whatever_the_plan(tools, machines, servers, cap):
    machines(["a1", "b2", "c3"], cap=cap)
    out = tools["material_on_hand"](printer_name="a1")
    assert out["success"] is True and out["printer_name"] == "a1" and servers == []
    by_type = {row["material_type"]: row for row in out["summary"]}
    # What a1 has loaded, and the shelf: the spool it holds and the spare one.
    assert by_type["PLA"]["total_grams"] == 300.0 and by_type["PLA"]["printers_loaded"] == ["a1"]
    assert by_type["ABS"]["total_grams"] == 700.0 and by_type["ABS"]["printers_loaded"] == []
    # Nothing another printer has loaded.
    assert "PETG" not in by_type


def test_a_spool_loaded_on_another_printer_is_not_this_printers(tools, machines, servers, db):
    db.save_material("c3", 0, "ABS", color=None, spool_id="s2", remaining_grams=700.0)
    machines(["a1", "b2", "c3"], cap=1)
    types_a1 = {row["material_type"] for row in tools["material_on_hand"](printer_name="a1")["summary"]}
    types_c3 = {row["material_type"] for row in tools["material_on_hand"](printer_name="c3")["summary"]}
    assert types_a1 == {"PLA"} and types_c3 == {"ABS"}


def test_a_named_printers_jobs_are_planned_here_whatever_the_plan(tools, machines, servers):
    machines(["a1", "b2", "c3"], cap=1)
    jobs = [{"file_name": "a.gcode", "material_type": "PLA", "required_grams": 100.0},
            {"file_name": "b.gcode", "material_type": "PETG", "required_grams": 100.0}]
    on_a1 = tools["assign_jobs"](jobs=jobs, printer_name="a1")
    on_b2 = tools["assign_jobs"](jobs=jobs, printer_name="b2")
    assert servers == [] and on_a1["printer_name"] == "a1" and on_a1["count"] == 2
    assert [a["material_match"] for a in on_a1["assignments"]] == [True, False]
    assert [a["material_match"] for a in on_b2["assignments"]] == [False, True]
    assert on_a1["assignments"][0]["recommended_printer"] == "a1"
    assert on_b2["assignments"][1]["recommended_printer"] == "b2"


@pytest.mark.parametrize("door, kwargs", [
    ("material_on_hand", {}),
    ("assign_jobs", {"jobs": [{"file_name": "a.gcode", "material_type": "PLA", "required_grams": 5.0}]}),
])
def test_a_printer_that_is_not_set_up_is_said_with_the_ones_that_are(tools, machines, servers, door, kwargs):
    machines(["a1", "b2"], cap=1)
    out = tools[door](printer_name="attic", **kwargs)
    assert out["success"] is False and out["code"] == "PRINTER_NOT_FOUND"
    assert "'attic'" in out["error"] and "a1, b2" in out["error"] and servers == []


def test_asking_which_printer_is_counted_and_answering_for_one_is_not(tools, machines, monkeypatch):
    counted: list[str] = []
    monkeypatch.setattr("kiln.account_plan.count_plan_wall", lambda tool: counted.append(tool))
    machines(["a1", "b2"], cap=1)
    tools["material_on_hand"]()
    tools["get_fleet_material_summary"]()
    tools["assign_jobs"](jobs=[])
    assert counted == ["material_on_hand", "get_fleet_material_summary", "assign_jobs"]
    counted.clear()
    tools["material_on_hand"](printer_name="a1")
    machines(["a1"], cap=1)
    tools["material_on_hand"]()
    assert counted == []


@pytest.mark.parametrize("names, cap", [(["a1"], 1), (["a1", "b2"], 1), (["a1", "b2"], 50)],
                         ids=["one printer", "several below the plan", "several on the plan"])
def test_the_older_names_give_the_same_answers(tools, machines, servers, names, cap):
    machines(names, cap=cap)
    jobs = [{"file_name": "a.gcode", "material_type": "PLA", "required_grams": 5.0}]

    def same(new: dict, old: dict) -> bool:
        return {k: v for k, v in new.items() if k != "upgrade_url"} == {k: v for k, v in old.items() if k != "upgrade_url"}

    assert same(tools["material_on_hand"](), tools["get_fleet_material_summary"]())
    assert same(tools["assign_jobs"](jobs=jobs), tools["optimize_fleet_assignment"](jobs=jobs))


def test_only_the_two_questions_a_printer_answers_for_itself_take_a_name(db):
    from kiln.material_inventory import ONE_PRINTER_QUESTIONS, inventory_answer

    assert {"summary", "assign"} == ONE_PRINTER_QUESTIONS
    with pytest.raises(ValueError, match="one named printer"):
        inventory_answer("restock", db, feature="x", printer_name="a1")
