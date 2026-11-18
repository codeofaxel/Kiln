"""``recommend_material`` and ``list_available_materials`` ask Kiln's servers.

The recommender's material table, intent weights and scoring are Kiln's; public
Kiln asks for the pick through ``kiln._pro_routing_bridge`` and builds the
reply from it, adding what only this computer knows: where each material is,
the nozzle on record, and the cautions every install says.  With the servers
stood in (public tests cannot import kiln-pro), these pin:

* what is sent: the intent, the printer's enclosure and heated bed, the budget
  and the names of the materials on hand -- never a printer name or an amount;
* the served answer is relayed: the pick, its score and settings, the sentence;
  where each material is, built here from the person's own records;
* a miss (offline, signed out, no answer, refused) makes no pick: the reply is
  basic, lists the plastics the printer can melt, applies the food floor and
  the skin caution, and says why in one sentence in the shared voice;
* the basic replies carry no figure of the material catalogue beyond names and
  categories;
* the recommender's table and scoring are not in the public package.
"""

from __future__ import annotations

import errno
import json
import re
import sys
import types
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from tests._routing_served import SIGNED_OUT, WIRE, plain_install_served, record, served_pick

CEREAL = "food storage container for the kitchen, holds cereal"
_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    """No kiln-pro on this machine, whatever the test runner has on its path."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def doors():
    from kiln.plugins.intelligence_tools import plugin

    mcp = _FakeMCP()
    plugin.register(mcp)
    return mcp.tools


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    """Two machines: PETG loaded on a1-left, PLA on a1-right."""
    from kiln.persistence import KilnDB

    db = KilnDB(db_path=str(tmp_path / "shelf.db"))
    db.save_material("a1-left", 0, "PETG", color="black", remaining_grams=800.0)
    db.save_material("a1-right", 0, "PLA", color="white", remaining_grams=900.0)
    monkeypatch.setattr("kiln.persistence.get_db", lambda *a, **k: db)
    yield db
    db.close()


def _picks(sent):
    return [request for tool, request in sent if tool == WIRE]


# ---------------------------------------------------------------------------
# What is sent
# ---------------------------------------------------------------------------


class TestWhatIsSent:
    def test_the_intent_the_printer_and_the_budget(self, doors):
        with plain_install_served(served_pick()) as sent:
            doors["recommend_material"](intent="make it strong", has_enclosure=True, has_heated_bed=False, budget_usd=30)
        assert _picks(sent) == [{
            "want": "pick", "intent": "make it strong",
            "printer_capabilities": {"has_enclosure": True, "has_heated_bed": False},
            "budget_usd": 30, "on_hand_materials": None, "inventory_recorded": False,
        }]

    def test_the_materials_on_hand_go_by_name_only(self, doors, shelf):
        with plain_install_served(served_pick()) as sent:
            doors["recommend_material"](intent="strong", on_hand_only=True)
        (request,) = _picks(sent)
        assert request["on_hand_materials"] == ["PETG", "PLA"] and request["inventory_recorded"] is True
        text = json.dumps(request)
        assert "a1-left" not in text and "a1-right" not in text and "800" not in text and "900" not in text

    def test_a_scoped_answer_never_sends_the_other_machines_material(self, doors, shelf):
        with plain_install_served(served_pick("pla")) as sent:
            doors["recommend_material"](intent="strong", on_hand_only=True, printer_id="a1-right")
        assert _picks(sent)[0]["on_hand_materials"] == ["PLA"]

    def test_the_list_asks_for_the_catalogue(self, doors):
        with plain_install_served({"success": True, "status": "ok", "materials": []}) as sent:
            doors["list_available_materials"]()
        assert _picks(sent) == [{"want": "catalogue"}]


# ---------------------------------------------------------------------------
# The served answer, relayed
# ---------------------------------------------------------------------------


class TestTheServedAnswerIsRelayed:
    def test_the_pick_its_score_settings_and_sentence(self, doors):
        answer = served_pick("petg", "PETG", alternatives=["pla", "abs"])
        with plain_install_served(answer):
            reply = doors["recommend_material"](intent="strong")
        assert reply["success"] is True and "basic" not in reply
        rec = reply["recommendation"]
        assert rec["material"] == answer["pick"]
        assert (rec["score"], rec["settings"], rec["estimated_cost_usd"]) == (77.7, {"stand_in": True}, 1.5)
        assert rec["reasoning"] == answer["sentence"]
        assert rec["alternatives"] == answer["alternatives"]
        assert rec["availability"] is None and rec["success_rate"] is None

    def test_where_the_material_is_comes_from_this_computer(self, doors, shelf):
        answer = served_pick(
            "petg", "PETG", alternatives=["pla"], on_hand_status="on_hand",
            on_hand_catalog={"PETG": "petg", "PLA": "pla"},
        )
        with plain_install_served(answer):
            reply = doors["recommend_material"](intent="strong", on_hand_only=True)
        rec = reply["recommendation"]
        assert reply["on_hand_scope"] == "fleet"
        assert [r["printer_name"] for r in rec["availability"]["loaded_on"]] == ["a1-left"]
        assert rec["availability"]["as_recorded"] == ["PETG"]
        assert rec["reasoning"] == f"{answer['sentence']} ON HAND: PETG loaded on a1-left (~800g)."
        assert rec["alternatives"][0]["availability"] == {"status": "loaded", "loaded_on": ["a1-right"], "shelf_spools": 0}

    def test_a_poor_fit_says_what_the_servers_named_as_better(self, doors, shelf):
        better = {"name": "tpu", "display_name": "TPU", "score": 90.0, "score_gap": 12.3, "sentence": "TPU suits it better."}
        answer = served_pick("petg", "PETG", on_hand_status="on_hand", on_hand_catalog={"PETG": "petg"}, better=better)
        with plain_install_served(answer):
            rec = doors["recommend_material"](intent="flexible gasket", on_hand_only=True)["recommendation"]
        assert rec["reasoning"].startswith("POOR FIT ON HAND: TPU suits it better. PETG is the best of what you do have.\n\n")
        assert rec["availability"]["better_catalog_option"] == {"name": "tpu", "display_name": "TPU", "score": 90.0, "score_gap": 12.3}

    def test_nothing_on_hand_is_labelled_a_purchase(self, doors, tmp_path, monkeypatch):
        from kiln.persistence import KilnDB

        db = KilnDB(db_path=str(tmp_path / "pva.db"))
        db.save_material("ender3", 0, "PVA", color="natural", remaining_grams=400.0)
        monkeypatch.setattr("kiln.persistence.get_db", lambda *a, **k: db)
        try:
            with plain_install_served(served_pick("petg", "PETG", on_hand_status="needs_purchase")):
                rec = doors["recommend_material"](intent="strong", on_hand_only=True)["recommendation"]
        finally:
            db.close()
        assert rec["reasoning"].startswith(
            "NOT ON HAND: nothing you have recorded (PVA) matches this request — PETG is a catalog pick and needs to be purchased.\n\n"
        )
        assert rec["availability"]["status"] == "needs_purchase" and rec["availability"]["on_hand_recorded"] == ["PVA"]

    def test_an_empty_shelf_says_how_to_record_one(self, doors, tmp_path, monkeypatch):
        from kiln.persistence import KilnDB

        db = KilnDB(db_path=str(tmp_path / "empty.db"))
        monkeypatch.setattr("kiln.persistence.get_db", lambda *a, **k: db)
        try:
            with plain_install_served(served_pick(on_hand_status="no_inventory_recorded")) as sent:
                rec = doors["recommend_material"](intent="strong", on_hand_only=True)["recommendation"]
        finally:
            db.close()
        assert _picks(sent)[0]["inventory_recorded"] is False
        assert rec["availability"]["status"] == "no_inventory_recorded" and "add_spool" in rec["reasoning"]

    @pytest.mark.parametrize("intent", ["a ring worn daily", "watch band", "ring"])
    def test_the_skin_caution_rides_on_a_served_pick(self, doors, intent):
        with plain_install_served(served_pick("pla", "PLA")):
            rec = doors["recommend_material"](intent=intent)["recommendation"]
        assert rec["reasoning"].startswith("SKIN CONTACT: no printed part is proven safe on skin. ")
        assert "ask a doctor." in rec["reasoning"] and rec["reasoning"].endswith("PLA is the stand-in pick.")

    def test_a_napkin_ring_is_not_worn(self, doors):
        with plain_install_served(served_pick("pla", "PLA")):
            rec = doors["recommend_material"](intent="napkin ring")["recommendation"]
        assert "SKIN CONTACT" not in rec["reasoning"]

    def test_the_abrasive_floor_rides_on_a_filled_spool(self, doors, tmp_path, monkeypatch):
        from kiln.persistence import KilnDB

        db = KilnDB(db_path=str(tmp_path / "cf.db"))
        db.save_material("x1c", 0, "PETG-CF", color="black", remaining_grams=750.0)
        monkeypatch.setattr("kiln.persistence.get_db", lambda *a, **k: db)
        try:
            answer = served_pick("petg", "PETG", on_hand_status="on_hand", on_hand_catalog={"PETG-CF": "petg"})
            with plain_install_served(answer):
                rec = doors["recommend_material"](intent="strong", on_hand_only=True, printer_id="x1c")["recommendation"]
        finally:
            db.close()
        assert "ABRASIVE FILAMENT" in rec["reasoning"] and "PETG-CF" in rec["reasoning"]

    def test_an_answer_of_the_wrong_shape_is_a_miss(self, doors):
        with plain_install_served({"success": True, "status": "ok"}):
            reply = doors["recommend_material"](intent="strong")
        assert reply["basic"] is True and "recommendation" not in reply

    def test_the_list_door_relays_the_served_list(self, doors):
        materials = [record("abs"), record("pla")]
        with plain_install_served({"success": True, "status": "ok", "materials": materials}):
            assert doors["list_available_materials"]() == {"success": True, "materials": materials, "count": 2}


# ---------------------------------------------------------------------------
# A miss: no pick, the basic answer
# ---------------------------------------------------------------------------

MISSES = {
    "offline": urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")),
    "signed_out": SIGNED_OUT,
    "unanswered": ConnectionRefusedError("refused"),
    "refused": {"status": "error", "success": False, "code": "NOT_FOR_THIS_ACCOUNT", "error": "Not for this account."},
}
_CODE_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_SYSTEM_WORD = re.compile(
    r"\b(API|endpoint|bridge|kiln-pro|kiln_pro|hosted service|HTTP|urlopen|Errno|traceback|exception|"
    r"backoff|envelope|payload)\b",
    re.IGNORECASE,
)


def _assert_one_voice(text: str) -> None:
    assert text == " ".join(text.split()) and text.endswith("."), text
    assert not _CODE_TOKEN.search(text) and not _SYSTEM_WORD.search(text), text
    for anchor in ("Kiln can't", "right now because", ", so it"):
        assert anchor in text, text


class TestAMissIsBasic:
    @pytest.mark.parametrize("cause", sorted(MISSES))
    def test_no_pick_the_plastics_this_printer_can_melt_and_why(self, doors, cause):
        from kiln.material_basics import plastics_printer_can_melt

        with plain_install_served(MISSES[cause]):
            reply = doors["recommend_material"](intent="strong")
        assert reply["success"] is True and reply["basic"] is True
        assert "recommendation" not in reply and "score" not in json.dumps(reply)
        assert reply["plastics"] == plastics_printer_can_melt(None)
        assert reply["why"] == cause
        assert reply["message"].startswith("Kiln did not pick a plastic for this part. Kiln can't pick one right now because ")
        _assert_one_voice(reply["message"])
        assert reply["retryable"] is (cause in ("offline", "unanswered"))

    def test_signed_out_says_signing_in_gets_the_pick(self, doors):
        with plain_install_served(SIGNED_OUT):
            reply = doors["recommend_material"](intent="strong")
        assert reply["message"].endswith("Sign in and Kiln picks the plastic for this part.")

    def test_both_material_picks_say_a_miss_the_same_way(self, doors):
        """The recommender and the design pick share one signed-out sentence
        and one plastics list, narrowed by the food floor for a food part."""
        from kiln.design_intelligence import basic_material_answer
        from kiln.served_answer import Miss

        with plain_install_served(SIGNED_OUT):
            pick = doors["recommend_material"](intent="a cup for drinking coffee")
        design = basic_material_answer("a cup for drinking coffee", None, Miss("signed_out", code="ACCOUNT_REQUIRED"))
        assert pick["plastics"] == design["plastics"]
        assert pick["message"].split(". ")[-1] == design["message"].split(". ")[-1]
        assert "ABS" not in design["plastics"]

    def test_a_food_brief_lists_only_what_the_food_floor_keeps(self, doors):
        from kiln.material_basics import plastics_printer_can_melt
        from kiln.material_safety import filter_food_safe_materials

        with plain_install_served(SIGNED_OUT):
            reply = doors["recommend_material"](intent=CEREAL)
        kept = filter_food_safe_materials(plastics_printer_can_melt(None), require="yes_or_conditional")
        assert reply["plastics"] == kept and kept
        assert not {"ABS", "ASA", "TPU"} & set(reply["plastics"])

    def test_a_brief_with_no_food_lists_every_plastic(self, doors):
        with plain_install_served(SIGNED_OUT):
            reply = doors["recommend_material"](intent="outdoor bracket for a garden fence, sun all day")
        assert {"ABS", "ASA", "PETG"} <= set(reply["plastics"])

    @pytest.mark.parametrize("intent", ["a ring worn daily", "watch band", "ring"])
    def test_a_worn_part_still_hears_the_skin_caution(self, doors, intent):
        with plain_install_served(SIGNED_OUT):
            reply = doors["recommend_material"](intent=intent)
        caution, note = reply["message"].split("\n\n")
        assert caution == (
            "SKIN CONTACT: no printed part is proven safe on skin. Keep it out of the mouth, away from the "
            "eyes, and off broken skin, piercings and implants. If your skin reacts, ask a doctor."
        )
        _assert_one_voice(note)

    def test_a_napkin_ring_hears_no_skin_caution(self, doors):
        with plain_install_served(SIGNED_OUT):
            reply = doors["recommend_material"](intent="napkin ring")
        assert "SKIN CONTACT" not in reply["message"] and "\n\n" not in reply["message"]

    def test_a_printer_with_its_own_ceiling_lists_what_it_can_melt(self, doors, monkeypatch):
        import kiln.printer_model_resolver as resolver
        import kiln.safety_profiles as profiles
        from kiln.material_basics import plastics_printer_can_melt

        monkeypatch.setattr(resolver, "resolve_printer_model_for", lambda name: "stand_in_model")
        monkeypatch.setattr(profiles, "get_profile", lambda model, **k: types.SimpleNamespace(id=model, max_hotend_temp=230.0))
        with plain_install_served(SIGNED_OUT):
            reply = doors["recommend_material"](intent="strong", printer_id="my-printer")
        assert reply["plastics"] == plastics_printer_can_melt(230.0) and "PC" not in reply["plastics"]
        assert reply["hotend_limit_c"] == 230.0 and reply["answered_for_printer"] == "my-printer"
        assert "lists the plastics this printer can melt instead" in reply["message"]

    def test_a_printer_with_only_the_generic_limits_lists_every_plastic(self, doors, monkeypatch):
        import kiln.printer_model_resolver as resolver
        import kiln.safety_profiles as profiles
        from kiln.material_basics import plastics_printer_can_melt

        monkeypatch.setattr(resolver, "resolve_printer_model_for", lambda name: "unknown_model")
        monkeypatch.setattr(profiles, "get_profile", lambda model, **k: types.SimpleNamespace(id="default", max_hotend_temp=240.0))
        with plain_install_served(SIGNED_OUT):
            reply = doors["recommend_material"](intent="strong", printer_id="my-printer")
        assert reply["plastics"] == plastics_printer_can_melt(None) and "hotend_limit_c" not in reply
        assert "lists common plastics instead" in reply["message"]

    def test_the_servers_are_not_asked_twice_for_a_call_they_already_missed(self, doors):
        from kiln import served_answer, served_public

        with plain_install_served(served_pick()) as sent, served_public.here_after(served_answer.Miss("offline")):
            reply = doors["recommend_material"](intent="strong")
        assert _picks(sent) == [] and reply["basic"] is True and reply["why"] == "offline"

    @pytest.mark.parametrize("cause", sorted(MISSES))
    def test_the_list_door_gives_names_and_categories_only(self, doors, cause):
        from kiln.design_intelligence import list_public_material_profiles

        with plain_install_served(MISSES[cause]):
            reply = doors["list_available_materials"]()
        assert reply["success"] is True and reply["basic"] is True and reply["why"] == cause
        assert reply["materials"] == [
            {"name": p.material_id, "display_name": p.display_name, "category": p.category}
            for p in list_public_material_profiles()
        ]
        assert reply["count"] == len(reply["materials"]) > 0
        assert reply["message"].startswith("Kiln did not list each material's properties. Kiln can't list them right now because ")
        _assert_one_voice(reply["message"])


# ---------------------------------------------------------------------------
# The basic replies carry nothing of the catalogue but names and categories
# ---------------------------------------------------------------------------


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for k, v in value.items() for leaf in [k, *_leaves(v)]]
    if isinstance(value, list):
        return [leaf for v in value for leaf in _leaves(v)]
    return [value]


def _catalogue_figures() -> set[Any]:
    """Every value the shipped material catalogue holds, other than each
    material's names and category."""
    data = json.loads((_SRC / "data" / "design_knowledge" / "materials.json").read_text())
    figures: set[Any] = set()
    allowed: set[Any] = set()
    for material_id, entry in data.items():
        if material_id.startswith("_") or not isinstance(entry, dict):
            continue
        allowed |= {material_id, entry.get("display_name"), entry.get("category")}
        for key, value in entry.items():
            if key not in ("display_name", "category"):
                figures.update(leaf for leaf in _leaves({key: value}) if not isinstance(leaf, bool))
    return {f for f in figures - allowed if f not in ("", None)}


@pytest.mark.parametrize("intent", ["strong", "a ring worn daily", CEREAL, "something magical"])
def test_a_basic_reply_carries_no_catalogue_figure(doors, intent):
    from kiln.gcode import _MATERIAL_TEMPS

    figures = _catalogue_figures()
    names = set(_MATERIAL_TEMPS)
    with plain_install_served(SIGNED_OUT):
        replies = [doors["recommend_material"](intent=intent), doors["list_available_materials"]()]
    for reply in replies:
        structural = {k: v for k, v in reply.items() if k not in ("count", "retryable")}
        said = [leaf for leaf in _leaves(structural) if leaf not in names and not isinstance(leaf, bool)]
        assert not [leaf for leaf in said if leaf in figures], [leaf for leaf in said if leaf in figures]


# ---------------------------------------------------------------------------
# The recommender's table and scoring are not in the public package
# ---------------------------------------------------------------------------


def test_the_table_and_its_scoring_are_not_in_the_public_package():
    import kiln.material_routing as routing

    gone = ("_MATERIALS", "_INTENT_MAP", "_DEFAULT_WEIGHTS", "_score_material", "parse_intent",
            "describe_material_pick", "describe_better_option", "_CATALOG_ALIASES", "_catalog_match",
            "_POOR_FIT_SCORE_GAP", "MaterialProperties", "IntentMapping", "list_materials", "get_material")
    assert not [name for name in gone if hasattr(routing, name)]
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "_INTENT_MAP" not in text and "_POOR_FIT_SCORE_GAP" not in text, path
        assert not re.search(r"ease_of_print\s*=", text), path
