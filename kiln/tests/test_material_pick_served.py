"""Kiln's material pick for one part, asked of Kiln's servers.

``recommend_design_material`` and the design analysis
(``get_design_constraints``) ask for the pick through
``kiln._pro_material_pick_bridge``: on a plain install (no kiln-pro) a
signed-in plan below Kiln Pro asks ``material_floor_pick``; a Kiln Pro plan
asks ``material_pick`` and, when that has no pick for the part, asks as
every other plan does.  These pin, with Kiln's servers stood in:

* the reply is rebuilt from what the servers sent and this computer's own
  catalogue record of the material, and nothing else;
* which wire each plan asks, and what is sent;
* a miss (signed out, offline, no answer, refused) picks nothing: the reply
  is basic -- the plastics this printer can melt, the cautions for the part,
  and one sentence saying why there is no pick and what fixes it -- and the
  basic reply reads nothing about a material but the nozzle table;
* the cautions are said on every path: a part worn against skin, food
  contact, a load-bearing part;
* the pick's engine is not in this package.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from kiln import _pro_material_pick_bridge as bridge
from kiln import design_intelligence as di
from kiln.material_basics import basic_plastics, plastics_printer_can_melt
from tests._plan_callers import free_caller  # noqa: F401 -- a fixture

#: A pick as ``material_floor_pick`` sends it.
_FLOOR_PICK = {
    "material_id": "petg",
    "score": 62.0,
    "reasons": ["Recommended for: sustained load. PETG chosen — picked on Kiln's servers."],
    "warnings": ["Load not checked for this material."],
    "alternatives": [
        {"material_id": "asa", "display_name": "ASA", "score": 50.0, "reasons": [], "warnings": ["w"]},
    ],
    "recommended_brands": [],
    "fallback": False,
}
#: A pick as ``material_pick`` sends it (Kiln Pro).
_KILN_PRO_PICK = {
    "material_id": "asa",
    "score": 81.0,
    "reasons": ["picked by Kiln Pro"],
    "warnings": [],
    "alternatives": [],
    "recommended_brands": [{"profile_id": "x_asa", "brand": "X"}],
}


def _ok(pick: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "status": "ok", "known": True, "pick": dict(pick)}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "_last_miss", None, raising=False)
    di._reset_knowledge_base()
    yield
    di._reset_knowledge_base()


def _plan(monkeypatch, plan: str) -> None:
    import kiln.licensing as licensing

    paid = plan != "free"
    monkeypatch.setattr(
        licensing, "check_tier",
        lambda *_a, **_k: (True, None) if paid else (False, "needs Kiln Pro, see kiln3d.com/pricing"),
    )


def _servers(monkeypatch, replies: dict[str, Any]) -> list[tuple[str, dict]]:
    """Kiln's servers stood in: *replies* maps a wire to its answer (or a
    callable raising what the network would).  Returns what was asked."""
    import kiln.server as srv

    asked: list[tuple[str, dict]] = []

    def served(tool_name, /, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        asked.append((tool_name, dict(kwargs)))
        reply = replies[tool_name]
        return reply(**kwargs) if callable(reply) else reply

    monkeypatch.setattr(srv, "_pro_api_call", served)
    return asked


def _door():
    from kiln.plugins.design_tools import plugin

    tools: dict = {}

    class _Mcp:
        def tool(self, **_kw):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools["recommend_design_material"]


# ---------------------------------------------------------------------------
# The served pick
# ---------------------------------------------------------------------------


class TestTheServedPick:
    def test_a_free_plan_asks_the_floor_wire_and_gets_its_pick(self, monkeypatch):
        _plan(monkeypatch, "free")
        asked = _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        rec = di.recommend_material_for_design(
            "shelf bracket", printer_has_enclosure=True, printer_has_direct_drive=False,
            max_hotend_temp_c=260, supported_materials=["petg", "asa"],
        )
        assert asked == [(bridge.FLOOR_WIRE_TOOL, {
            "requirements_text": "shelf bracket", "printer_has_enclosure": True,
            "printer_has_direct_drive": False, "max_hotend_temp_c": 260,
            "supported_materials": ["petg", "asa"],
        })]
        assert rec.material.material_id == "petg" and rec.score == 62.0
        assert rec.reasons == _FLOOR_PICK["reasons"] and rec.warnings == _FLOOR_PICK["warnings"]
        assert rec.alternatives == _FLOOR_PICK["alternatives"] and rec.recommended_brands == []

    def test_the_material_is_this_computers_own_record(self, monkeypatch):
        """The served pick names the material; its name, family, heat and
        chemistry are this computer's catalogue record, and a scored pick
        carries no design-limits summary."""
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        rec = di.recommend_material_for_design("shelf bracket")
        record = di.get_public_material_profile("petg")
        assert rec.material.display_name == record.display_name
        assert rec.material.thermal == record.thermal and rec.material.chemical == record.chemical
        assert rec.material.design_limits == {} and rec.design_limits_summary == {}

    def test_the_everyday_default_summarises_its_own_design_limits(self, monkeypatch):
        _plan(monkeypatch, "free")
        fallback = {**_FLOOR_PICK, "material_id": "pla", "reasons": [], "alternatives": [], "fallback": True}
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(fallback)})
        rec = di.recommend_material_for_design("anything")
        assert rec.design_limits_summary == di.get_public_material_profile("pla").design_limits != {}

    def test_a_kiln_pro_plan_gets_kiln_pros_pick_and_no_miss(self, monkeypatch):
        _plan(monkeypatch, "pro")
        asked = _servers(monkeypatch, {bridge.WIRE_TOOL: _ok(_KILN_PRO_PICK)})
        rec, miss = di.material_pick_for_design("strong bracket")
        assert miss is None and [tool for tool, _ in asked] == [bridge.WIRE_TOOL]
        assert rec.material.material_id == "asa" and rec.recommended_brands[0]["brand"] == "X"

    @pytest.mark.parametrize(
        "pro_reply",
        [
            {"success": True, "status": "ok", "known": False, "pick": None},
            {"success": False, "status": "error", "code": "TIER_REQUIRED", "error": "needs Kiln Pro", "required_tier": "pro"},
            {"status": "error", "success": False, "code": "INVALID_ARGUMENT", "error": "requirements_text must describe the part."},
        ],
    )
    def test_a_kiln_pro_plan_with_no_pro_pick_asks_as_every_plan_does(self, monkeypatch, pro_reply):
        _plan(monkeypatch, "pro")
        asked = _servers(monkeypatch, {bridge.WIRE_TOOL: pro_reply, bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        rec = di.recommend_material_for_design("shelf bracket")
        assert [tool for tool, _ in asked] == [bridge.WIRE_TOOL, bridge.FLOOR_WIRE_TOOL]
        assert rec.material.material_id == "petg"

    @pytest.mark.parametrize(
        ("pro_reply", "said"),
        [
            ({"success": True, "status": "ok", "known": False, "pick": None},
             "This is the pick every plan gets, not Kiln Pro's pick. Kiln Pro's pick had nothing for this part."),
            ({"success": False, "status": "error", "code": "TIER_REQUIRED", "required_tier": "pro",
              "error": "Kiln Pro's material pick needs Kiln Pro. Upgrade at https://kiln3d.com/pricing."},
             "because Kiln's Pro pick said no, so it gives the pick every plan gets instead. Kiln's servers do not "
             "count this computer's sign-in as Kiln Pro; signing in again brings this computer's record of the "
             "plan up to date"),
            ({"status": "error", "success": False, "code": "INVALID_ARGUMENT",
              "error": "Describe the part in a sentence or two."},
             "because Kiln's Pro pick said no, so it gives the pick every plan gets instead. Describe the part in "
             "a sentence or two."),
        ],
    )
    def test_a_kiln_pro_plan_given_the_every_plan_pick_is_told_once_and_never_sold(self, monkeypatch, pro_reply, said):
        """A caller whose plan includes Kiln Pro, handed the pick every plan
        gets, reads one line saying so (why and what fixes it), and nothing
        that offers the plan they hold.  The line names Kiln Pro's pick as
        what did not come, never the pick it is shown beside, and a Kiln Pro
        pick that answered with nothing for the part is not a miss."""
        _plan(monkeypatch, "pro")
        _servers(monkeypatch, {bridge.WIRE_TOOL: pro_reply, bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        rec = di.recommend_material_for_design("shelf bracket")
        line = rec.warnings[0]
        assert line.startswith("This is the pick every plan gets, not Kiln Pro's pick.") and said in line
        assert "Kiln's material pick" not in line and "didn't answer" not in line
        assert sum("not Kiln Pro's pick" in w for w in rec.warnings) == 1
        assert "kiln3d.com/pricing" not in line and "Upgrade" not in line and "needs Kiln" not in line
        assert rec.warnings[1:] == _FLOOR_PICK["warnings"]
        # A free plan handed the same pick is told nothing of the kind.
        _plan(monkeypatch, "free")
        assert di.recommend_material_for_design("shelf bracket").warnings == _FLOOR_PICK["warnings"]

    @pytest.mark.parametrize(
        "wall_words",
        [
            # A plan wall worded from the tool's own summary, offering nothing by name.
            "Serve Kiln Pro's material pick for one part to the install that asked. "
            "That's what Kiln Pro would do for this request.",
            "Kiln Pro would run material pick for this request and return the full result.",
        ],
    )
    def test_a_plan_wall_in_any_words_is_never_repeated_to_someone_who_holds_the_plan(self, monkeypatch, wall_words):
        """The servers' tier wall is the plan wall whatever its words: a caller
        this computer counts as holding Kiln Pro is told how to bring the
        record up to date, never the wall's own sentence."""
        _plan(monkeypatch, "pro")
        refusal = {"success": False, "status": "error", "code": "TIER_REQUIRED", "required_tier": "pro",
                   "error": wall_words}
        _servers(monkeypatch, {bridge.WIRE_TOOL: refusal, bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        line = di.recommend_material_for_design("shelf bracket").warnings[0]
        assert "signing in again" in line
        assert wall_words.split(".")[0] not in line

    def test_a_stale_kiln_pro_record_meeting_a_free_server_is_told_to_sign_in_and_never_pitched(self, monkeypatch):
        """The servers answer the pick every plan gets, as for a free caller,
        with the load caution and its plan sentence; this computer counts the
        caller as holding Kiln Pro, so the reply says how to bring the record
        up to date and keeps the caution, never the plan sentence."""
        _plan(monkeypatch, "pro")
        pitched = dict(_FLOOR_PICK, warnings=[
            "Load not checked for this material.",
            f"{di._PICK_LOAD_CAUTION} Kiln Pro sizes the part for the weight and how long it's held; see "
            "https://kiln3d.com/pricing.",
        ])
        refusal = {"success": False, "status": "error", "code": "TIER_REQUIRED", "required_tier": "pro",
                   "error": "Kiln Pro's material pick needs Kiln Pro. Upgrade at https://kiln3d.com/pricing."}
        _servers(monkeypatch, {bridge.WIRE_TOOL: refusal, bridge.FLOOR_WIRE_TOOL: _ok(pitched)})
        rec = di.recommend_material_for_design("shelf bracket that holds books")
        assert "signing in again" in rec.warnings[0]
        assert di._PICK_LOAD_CAUTION in rec.warnings
        assert not any("kiln3d.com/pricing" in w or "sizes the part" in w for w in rec.warnings), rec.warnings

    @pytest.mark.parametrize("ceiling", [None, float("nan"), float("inf"), "hot", True])
    def test_a_hotend_ceiling_that_is_not_a_number_is_refused_here_and_never_waits(self, monkeypatch, ceiling):
        """Refused before anything is asked, in plain words, and never read as
        Kiln's servers not answering: the next good ask is asked at once."""
        _plan(monkeypatch, "free")
        asked = _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        rec, miss = di.material_pick_for_design("shelf bracket", max_hotend_temp_c=ceiling)
        assert rec is None and asked == []
        assert (miss.cause, miss.code, miss.detail) == ("refused", "INVALID_ARGUMENT", bridge.BAD_CEILING)
        assert miss.retry_after_s == 0
        rec, miss = di.material_pick_for_design("shelf bracket", max_hotend_temp_c=260)
        assert miss is None and rec.material.material_id == "petg"
        assert [tool for tool, _ in asked] == [bridge.FLOOR_WIRE_TOOL]

    @pytest.mark.usefixtures("free_caller")
    def test_the_door_returns_the_pick_with_its_cautions(self, monkeypatch):
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        out = _door()("a ring worn daily", max_hotend_temp_c=260)
        assert out["success"] is True and "basic" not in out
        assert out["material"]["material_id"] == "petg" and out["score"] == 62.0
        assert any(w.startswith("SKIN CONTACT: no 3D-printed part is skin-safe") for w in out["warnings"])
        assert out["skin_contact"]["never_skin_safe"] is True


# ---------------------------------------------------------------------------
# A miss picks nothing
# ---------------------------------------------------------------------------


def _offline(**_kw):
    raise socket.gaierror(8, "nodename nor servname provided")


def _silent(**_kw):
    raise ConnectionResetError("reset by peer")


class TestAMissPicksNothing:
    @pytest.mark.usefixtures("free_caller")
    def test_signed_out_says_sign_in_and_kiln_picks(self):
        """No sign-in on this install: the real served door refuses before any
        network call, and the reply is the basic list."""
        out = _door()("kids toy", max_hotend_temp_c=230)
        assert out["basic"] is True and out["success"] is True and out["why"] == "signed_out"
        assert out["message"] == (
            "Kiln did not pick a plastic for this part. Kiln can't pick one right now because "
            "Kiln is signed out, so it lists the plastics this printer can melt instead. Sign in "
            "and Kiln picks the plastic for this part (a free account is enough)."
        )
        assert out["plastics"] == plastics_printer_can_melt(230)
        for key in ("material", "score", "reasons", "alternatives", "recommended_brands"):
            assert key not in out

    @pytest.mark.parametrize(
        ("reply", "cause", "words"),
        [
            (_offline, "offline", "this computer is offline"),
            (_silent, "unanswered", "Kiln's material pick didn't answer"),
            ({"status": "error", "success": False, "code": "SEAT_DAILY_CAP", "error": "Too many asks today."},
             "refused", "Kiln's material pick said no"),
        ],
    )
    def test_each_miss_says_its_cause_and_lists_the_plastics(self, monkeypatch, reply, cause, words):
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: reply})
        out = _door()("cable clip", max_hotend_temp_c=250)
        assert out["basic"] is True and out["why"] == cause
        assert out["message"].startswith("Kiln did not pick a plastic for this part.")
        assert words in out["message"]
        assert out["plastics"] == plastics_printer_can_melt(250)
        assert "score" not in out and "material" not in out

    @pytest.mark.usefixtures("free_caller")
    def test_a_ceiling_too_large_to_be_a_number_lists_every_plastic(self):
        """Signed out, a hotend ceiling no float can hold still gets the basic
        list (every plastic, as for no ceiling), never a failed reply."""
        out = _door()("cable clip", max_hotend_temp_c=10**400)
        assert out["success"] is True and out["basic"] is True
        assert out["plastics"] == plastics_printer_can_melt(None)
        assert plastics_printer_can_melt(float("nan")) == plastics_printer_can_melt(None)

    def test_a_ceiling_the_servers_refuse_still_lists_every_plastic(self, monkeypatch):
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: {
            "status": "error", "success": False, "code": "INVALID_ARGUMENT",
            "error": "Give the hotend's ceiling in whole degrees C, above 0 and at most 600.",
        }})
        out = _door()("cable clip", max_hotend_temp_c=10**400)
        assert out["success"] is True and out["basic"] is True and out["why"] == "refused"
        assert out["plastics"] == plastics_printer_can_melt(None)

    def test_the_plastics_are_the_ones_the_hotend_melts(self, monkeypatch):
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _offline})
        low = _door()("cable clip", max_hotend_temp_c=200)["plastics"]
        assert low == plastics_printer_can_melt(200) and "PC" not in low and "PLA" in low

    def test_servers_that_do_not_answer_are_left_alone_for_a_while(self, monkeypatch):
        _plan(monkeypatch, "free")
        asked = _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _silent})
        for _ in range(3):
            out = _door()("cable clip")
            assert out["basic"] is True and out["why"] == "unanswered"
        assert len(asked) == 1

    def test_a_miss_said_again_says_when_kiln_asks_again(self, monkeypatch):
        """While the servers are left alone, the miss is said in the past
        ("when last asked") with the real wait, never "wait a minute"; once
        the wait is over Kiln asks again."""
        _plan(monkeypatch, "free")
        asked = _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _silent})
        first = _door()("cable clip")
        assert "Kiln's material pick didn't answer, so" in first["message"]
        assert "Wait a minute and ask again" in first["message"] and "retry_after_s" not in first
        again = _door()("cable clip")
        assert "Kiln's material pick didn't answer when last asked, so" in again["message"]
        assert again["message"].endswith("Wait about 5 minutes and ask again for Kiln's pick for this part.")
        assert 290 <= again["retry_after_s"] <= 300 and again["retryable"] is True
        assert len(asked) == 1
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        assert _door()("cable clip")["material"]["material_id"] == "petg"

    def test_an_offline_computer_is_asked_again_at_once(self, monkeypatch):
        """Only a server that did not answer is left alone: back online, the
        next ask gets the pick, never a stale "this computer is offline"."""
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _offline})
        assert _door()("cable clip")["why"] == "offline"
        asked = _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        out = _door()("cable clip")
        assert out["material"]["material_id"] == "petg" and "basic" not in out
        assert len(asked) == 1

    def test_a_kiln_pro_plan_offline_is_not_asked_twice(self, monkeypatch):
        _plan(monkeypatch, "pro")
        asked = _servers(monkeypatch, {bridge.WIRE_TOOL: _offline, bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        out = _door()("cable clip")
        assert out["basic"] is True and [tool for tool, _ in asked] == [bridge.WIRE_TOOL]

    def test_a_call_the_servers_already_missed_is_not_asked_again(self, monkeypatch):
        """The tool ran here because Kiln's servers missed the call already
        (served_public): the pick says that miss and asks nothing more."""
        from kiln import served_public
        from kiln.served_answer import Miss

        _plan(monkeypatch, "free")
        asked = _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        with served_public.here_after(Miss("offline", code="SERVER_UNREACHABLE")):
            out = _door()("cable clip")
        assert asked == [] and out["basic"] is True and out["why"] == "offline"

    def test_a_pick_this_computer_cannot_read_is_a_miss(self, monkeypatch):
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok({**_FLOOR_PICK, "score": None})})
        out = _door()("cable clip")
        assert out["basic"] is True and "material" not in out

    @pytest.mark.parametrize(("plan", "wire"), [("free", bridge.FLOOR_WIRE_TOOL), ("pro", bridge.WIRE_TOOL)])
    def test_a_material_this_computer_does_not_know_is_shown_as_the_servers_named_it(self, monkeypatch, plan, wire):
        """A pick of a material newer than this install's catalogue is the
        pick, named as the servers named it, never "didn't answer, wait a
        minute"; nothing this computer cannot vouch for is said about it, and
        the food floor still refuses a material it does not know."""
        _plan(monkeypatch, plan)
        newer = {**_FLOOR_PICK, "material_id": "petg_new_blend", "display_name": "PETG New Blend",
                 "category": "thermoplastic"}
        asked = _servers(monkeypatch, {wire: _ok(newer)})
        out = _door()("cable clip")
        assert "basic" not in out and [tool for tool, _ in asked] == [wire]
        assert out["material"]["material_id"] == "petg_new_blend"
        assert out["material"]["display_name"] == "PETG New Blend" and out["material"]["category"] == "thermoplastic"
        assert out["material"]["thermal"] == {} and out["material"]["chemical"] == {}
        assert out["score"] == 62.0 and out["reasons"] == newer["reasons"]
        food = _door()("cookie cutter, food safe")
        assert food["food_safety"]["primary_material_verdict"] == "refuse"

    def test_the_basic_reply_reads_nothing_about_a_material(self, monkeypatch):
        """With every reader of the material catalogue made to fail, the basic
        reply still comes back whole: it reads the nozzle table, and for a
        part that touches food only the public food-contact floor."""
        def _no(*_a, **_k):
            raise AssertionError("the basic reply read the material catalogue")

        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _offline})
        monkeypatch.setattr(di._DesignKnowledgeBase, "materials", property(_no))
        for name in ("get_material_profile", "get_public_material_profile", "list_public_material_profiles",
                     "list_material_profiles"):
            monkeypatch.setattr(di, name, _no)
        for text in ("shelf bracket that holds 10 lbs, worn on the wrist",
                     "shelf bracket that holds 10 lbs, food safe, worn on the wrist"):
            out = _door()(text, max_hotend_temp_c=240)
            assert out["basic"] is True and out["plastics"] == basic_plastics(text, 240)
        assert _door()("shelf bracket", max_hotend_temp_c=240)["plastics"] == plastics_printer_can_melt(240)


# ---------------------------------------------------------------------------
# The cautions are said on every path
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("free_caller")
class TestTheCautionsOnEveryPath:
    def test_skin_contact(self):
        out = _door()("a ring worn daily")
        assert out["basic"] is True
        assert (
            "SKIN CONTACT: no 3D-printed part is skin-safe, hypoallergenic, or biocompatible. "
            "For any mouth, eye, broken-skin, piercing, or implant use, see a medical professional."
        ) in out["warnings"]

    def test_food_contact(self):
        from kiln.material_safety import food_contact_nozzle_advisory

        out = _door()("coffee mug")
        assert out["basic"] is True and out["food_contact_use_case_detected"] is True
        assert food_contact_nozzle_advisory() in out["warnings"]

    def test_a_load_bearing_part(self):
        out = _door()("shelf bracket that holds 10 lbs")
        load = [w for w in out["warnings"] if "load-bearing" in w]
        assert out["basic"] is True and len(load) == 1
        assert load[0].startswith("This appears to be a load-bearing application; none of the plastics listed")
        assert "kiln3d.com" in load[0]

    def test_the_load_nudge_names_the_plan_once_after_what_it_does_for_the_part(self):
        """Value first, the plan as a fact, the link once."""
        out = _door()("shelf bracket that holds 10 lbs")
        text = " ".join(out["warnings"])
        assert text.count("Kiln Pro") == 1 and text.count("kiln3d.com/pricing") == 1
        assert "Kiln Pro sizes the part for the weight and how long it's held" in text

    def test_a_part_with_nothing_to_warn_about_carries_no_caution(self):
        out = _door()("cable clip")
        assert out["basic"] is True and out["warnings"] == []
        assert "food_contact_use_case_detected" not in out


# ---------------------------------------------------------------------------
# The design analysis
# ---------------------------------------------------------------------------


class TestTheDesignAnalysis:
    def test_a_served_pick_is_its_recommended_material(self, monkeypatch):
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        brief = di.get_design_constraints("shelf bracket").to_dict()
        assert brief["recommended_material"]["material"]["material_id"] == "petg"
        assert "material_basic" not in brief

    @pytest.mark.usefixtures("free_caller")
    def test_a_miss_names_the_plastics_and_why(self):
        brief = di.get_design_constraints("a cereal bowl to eat from").to_dict()
        assert brief["recommended_material"] is None
        basic = brief["material_basic"]
        assert basic["basic"] is True and basic["why"] == "signed_out"
        # A part that touches food lists only what the food-contact floor allows.
        assert basic["plastics"] == basic_plastics("a cereal bowl to eat from", None)
        assert "ABS" not in basic["plastics"] and basic["plastics"]
        assert basic["food_contact_use_case_detected"] is True


# ---------------------------------------------------------------------------
# Replies built from the analysis
# ---------------------------------------------------------------------------


def _cube(path) -> str:
    import struct

    lo, hi = 0.0, 20.0
    v = {(a, b, c): (hi if a else lo, hi if b else lo, hi if c else lo) for a in (0, 1) for b in (0, 1) for c in (0, 1)}
    quads = [
        ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)), ((0, 0, 1), (0, 1, 1), (1, 1, 1), (1, 0, 1)),
        ((0, 0, 0), (0, 1, 0), (0, 1, 1), (0, 0, 1)), ((1, 0, 0), (1, 0, 1), (1, 1, 1), (1, 1, 0)),
        ((0, 0, 0), (0, 0, 1), (1, 0, 1), (1, 0, 0)), ((0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)),
    ]
    tris = [t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))]
    with open(path, "wb") as fh:
        fh.write(b"cube".ljust(80, b"\x00") + struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for corner in tri:
                fh.write(struct.pack("<3f", *v[corner]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _tools(module: str) -> dict[str, Any]:
    import importlib

    plugin = importlib.import_module(module).plugin
    tools: dict = {}

    class _Mcp:
        def tool(self, **_kw):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools


def _says_basic(block: Any) -> None:
    """A reply's basic material block: Kiln did not pick, why, and the fix."""
    assert isinstance(block, dict) and block["basic"] is True and block["why"] == "signed_out"
    assert block["message"].startswith("Kiln did not pick a plastic for this part.")
    assert "Sign in and Kiln picks the plastic for this part (a free account is enough)." in block["message"]
    assert block["plastics"]


@pytest.mark.usefixtures("free_caller")
class TestRepliesBuiltFromTheAnalysisSayBasic:
    """With no pick, a reply built from the design analysis says so: the
    validator's report and every prompt builder carry the analysis's basic
    block, never a material that silently went missing."""

    def test_the_validators_report_and_prompt_say_basic_only_with_no_pick(self, monkeypatch, tmp_path):
        tools = _tools("kiln.plugins.design_tools")
        out = tools["validate_design_for_requirements"](_cube(tmp_path / "cube.stl"), "shelf bracket that holds 10 lbs")
        assert out["success"] is True and out["material"] is None
        _says_basic(out["material_basic"])
        # With a pick, neither reply carries the block and the prompt names the material.
        _plan(monkeypatch, "free")
        _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        report = tools["validate_design_for_requirements"](_cube(tmp_path / "cube.stl"), "shelf bracket")
        prompt = tools["build_generation_prompt"]("shelf bracket")["prompt"]
        assert report["material"] == "petg" and "material_basic" not in report
        assert "material_basic" not in prompt
        assert any(c.startswith("designed for ") for c in prompt["constraints_added"])

    def test_the_generation_prompt(self):
        out = _tools("kiln.plugins.design_tools")["build_generation_prompt"]("shelf bracket that holds 10 lbs")
        prompt = out["prompt"]
        assert not any(c.startswith("designed for ") for c in prompt["constraints_added"])
        _says_basic(prompt["material_basic"])

    def test_the_parametric_prompt(self):
        from kiln.generation_feedback import build_parametric_generation_prompt

        _says_basic(build_parametric_generation_prompt("shelf bracket that holds 10 lbs").to_dict()["material_basic"])

    def test_generate_model(self, monkeypatch):
        from unittest.mock import MagicMock

        import kiln.server as srv
        from kiln.generation import GenerationJob, GenerationStatus

        gen = MagicMock()
        gen.display_name = "Stand-in"
        gen.generate.return_value = GenerationJob(
            id="j1", provider="meshy", prompt="p", status=GenerationStatus.PENDING, progress=0,
            created_at=0.0, format="stl",
        )
        monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr(srv, "_get_generation_provider", lambda *_a, **_k: gen)
        out = srv.generate_model("shelf bracket that holds 10 lbs", provider="meshy")
        _says_basic(out["design_intelligence"]["material_basic"])

    def test_iterate_design(self, monkeypatch):
        from unittest.mock import MagicMock

        import kiln.server as srv
        from kiln.generation import GenerationJob, GenerationStatus

        gen = MagicMock()
        gen.generate.return_value = GenerationJob(
            id="j1", provider="meshy", prompt="p", status=GenerationStatus.PENDING, progress=0,
            created_at=0.0, format="stl",
        )
        monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr(srv, "_get_generation_provider", lambda *_a, **_k: gen)
        out = _tools("kiln.plugins.design_reasoning_tools")["iterate_design"](
            "shelf bracket that holds 10 lbs", provider="meshy", max_iterations=1,
        )
        _says_basic(out["material_basic"])


class TestOneAskPerPart:
    """The pick reads at most one part's description, and one tool call asks
    for one part once."""

    @pytest.mark.parametrize("plan", ["free", "pro"])
    def test_a_description_too_long_is_refused_once_in_plain_words(self, monkeypatch, plan):
        """A part described in more than 2,000 characters is refused here,
        with nothing asked of the servers, in words that name no field."""
        _plan(monkeypatch, plan)
        asked = _servers(monkeypatch, {bridge.WIRE_TOOL: _ok(_KILN_PRO_PICK), bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        out = _door()("shelf " * 400, max_hotend_temp_c=250)
        assert asked == [] and out["basic"] is True and out["why"] == "refused"
        assert out["message"].endswith("Describe the part in 2,000 characters or fewer.")
        assert "requirements" not in out["message"] and "_" not in out["message"]
        assert len("x" * bridge.MAX_REQUIREMENTS_CHARS) == 2000
        assert _door()("x" * 2000)["material"]["material_id"] in {"petg", "asa"}

    def test_one_tool_call_asks_for_the_pick_once(self, monkeypatch, tmp_path):
        """An audit reads the analysis three times (its brief, its prompt, its
        validation); within one tool call the servers are asked once, and
        each reader still gets its own copy."""
        from kiln import tool_context

        _plan(monkeypatch, "free")
        asked = _servers(monkeypatch, {bridge.FLOOR_WIRE_TOOL: _ok(_FLOOR_PICK)})
        audit = _tools("kiln.plugins.design_tools")["audit_original_design"]
        token = tool_context.enter_tool("audit_original_design", {})
        try:
            out = audit(_cube(tmp_path / "cube.stl"), "shelf bracket that holds 10 lbs")
            again = di.recommend_material_for_design("shelf bracket that holds 10 lbs")
            again.warnings.append("changed by one reader")
            assert "changed by one reader" not in di.recommend_material_for_design(
                "shelf bracket that holds 10 lbs").warnings
        finally:
            tool_context.leave_tool(token)
        # Only the pick is counted: the same call also asks the design-limits
        # wire for the picked plastic's limits, which is a different question.
        picks = lambda: [a for a in asked if a[0] == bridge.FLOOR_WIRE_TOOL]  # noqa: E731
        assert out["success"] is True and len(picks()) == 1
        # A new call asks again: the memo lives for one call only.
        token = tool_context.enter_tool("analyze_design_requirements", {})
        try:
            di.get_design_constraints("shelf bracket that holds 10 lbs")
        finally:
            tool_context.leave_tool(token)
        assert len(picks()) == 2


# ---------------------------------------------------------------------------
# The engine is not here
# ---------------------------------------------------------------------------


#: The pick engine's names, by digest (tests/_moved_away.py).
_PICK_ENGINE_NAMES = frozenset({
    "a7a4ae31bee4f5fc18190c16774a501baf05cd30ac77bbf26cc8bdf6fe8a775c",
    "0479388b0262a8b0b2d639bee734f86bff7629fce0015b2d339d1a5f33b898cc",
    "8e011dfca10f46a4dfa379b3ba3df224e5540d3118479f199133d0b04911e479",
    "72b2968e1cdbba10353f0896fa2514b94c7f36f300cf1002ddf65ee413682a5f",
    "613bbb787bcd63b3bc5abdedc0ad944836c923f6b8dde0ac6f97351e1ae7cf37",
    "429bce7047064ba1af478e623b4831402fdac2fd8932724f931779833a47a3ed",
    "ea9d5f16f4b0655e6a143ccd85ad891f89bc875224a3b277ee1f9105213a2b9d",
    "28fcc92cdb5b273fce45a95c6304a889fb89aba6e9f0c782e1c8373ee404549f",
    "95b349296000a48c2982fa431e6fc7eff40535dff6fea31dec44b310225c819e",
    "e6748a39ca6a305a377f2f1690752ffff89908bee34195c579292033b1e5cc10",
    "685d1eb42dd5855db7b169207165b08e37a8aebb5ac45adbba7544afb86d2cf2",
    "bb737d4d7700a67b17c654bac4e1e9ac1945de7ef2cc2595bd331557a9ec86b8",
    "ad3b12644c3204c95e460b2b881dc260810fc1c1f69bbfb9002a266b66e31d22",
    "99a65d5b45f27e8d97116017ae038f1566215d8fdd06d17b66151801cf556592",
})
#: Sentences the pick engine says, by digest and number of words.
_PICK_ENGINE_SAID = {
    "7c24f4e8fcf2358cf045b1a48a054ab7ea7da32a5d0ac4b976a503f361302eb8": 3,
    "c0338e796b7c0763a0952fc65aecfedddaffab09e89751f7ba69fcff71bc94ad": 7,
    "ad518f74e63b05481faa16aec4ca3f1e8d82724e2e082c4b2d0103369276b890": 6,
    "c8f722673989a77b4a73926dad6cac4e4892a47b36cb8054b973f5c5726c0801": 7,
}


def test_the_pick_engine_is_not_in_this_package():
    from tests._moved_away import attributes_back, strings_back

    assert attributes_back(di, _PICK_ENGINE_NAMES) == []
    assert strings_back(_PICK_ENGINE_SAID) == [], "a module still words the pick"
