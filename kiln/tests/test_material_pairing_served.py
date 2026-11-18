"""Whether two materials share one dual-extrusion print, asked of Kiln's servers.

``check_multi_material_pairing`` answers from Kiln's pairing verdict, asked
through :mod:`kiln._pro_material_pairing_bridge` (kiln-pro, https://kiln3d.com).  These
pin, on an install without kiln-pro:

* the door rebuilds its reply from the served answer, for the pair the
  person named, and says the person asked;
* a miss (offline, signed out, no answer, refused) is a basic answer made
  from the nozzle temperature table alone: ``basic: true``, ``compatible``
  never a yes, no adhesion grade, support record or rule, and one sentence
  saying why the verdict is missing and how to get it;
* the pairing table is not in this package.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kiln import design_intelligence as di

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"

#: The four shapes a basic answer's notes take, and nothing else.
_BASIC_NOTES = (
    re.compile(r"^These two can't share one nozzle temperature: the nozzle ranges of .+ do not overlap\. "
               r"How well they stick together was not judged\.$"),
    re.compile(r"^The nozzle ranges of .+ overlap\. How well they stick together and how much to purge "
               r"between them was not judged\.$"),
    re.compile(r"^Kiln's nozzle temperature table has no range for .+, so neither their "
               r"temperatures nor how well they stick together was judged\.$"),
    re.compile(r"^Both are .+: one material shares its own nozzle temperature and bonds to itself\.$"),
)


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _door():
    from kiln.plugins.design_tools import plugin

    mcp = _FakeMCP()
    plugin.register(mcp)
    return mcp.tools["check_multi_material_pairing"]


def _served(monkeypatch, reply: Any) -> list[dict[str, Any]]:
    """Answer the served door with *reply* (a dict, or an exception to
    raise), and record each ask."""
    import kiln.server as srv

    asks: list[dict[str, Any]] = []

    def call(tool_name, _timeout=None, _asked_by_user=None, _feature=None, **kwargs):
        asks.append({"tool": tool_name, "asked_by_user": _asked_by_user, **kwargs})
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(srv, "_pro_api_call", call)
    return asks


def _answer(**overrides: Any) -> dict[str, Any]:
    out = {
        "success": True, "material_a": "pla", "material_b": "tpu", "found": "pair", "compatible": True,
        "interface_adhesion": "moderate", "notes": "", "support_pair": None, "general_rules": [],
    }
    out.update(overrides)
    return out


class TestTheServedVerdict:
    def test_the_door_rebuilds_its_reply_from_the_served_answer(self, monkeypatch):
        asks = _served(monkeypatch, _answer())
        out = _door()("PLA", "TPU")
        assert asks == [{"tool": "material_pairing", "asked_by_user": True, "material_a": "PLA", "material_b": "TPU"}]
        assert out["success"] is True
        assert (out["material_a"], out["material_b"]) == ("pla", "tpu")
        assert out["compatible"] is True and out["interface_adhesion"] == "moderate"
        assert out["notes"] == "" and out["support_pair"] is None and out["general_rules"] == []
        assert out["caution"].startswith("The verdict is here, but not the reasoning behind it")
        assert "basic" not in out and "why" not in out

    def test_a_support_record_rides_through(self, monkeypatch):
        record = {"model_material": "pla", "support_material": "pva", "dissolution_method": "x", "interface_adhesion": "excellent"}
        _served(monkeypatch, _answer(material_b="pva", interface_adhesion="excellent", support_pair=record))
        out = _door()("pla", "pva")
        assert out["support_pair"] == record and out["compatible"] is True

    def test_a_pair_nothing_covers_says_so(self, monkeypatch):
        asks = _served(monkeypatch, _answer(found="none", compatible=False, interface_adhesion="unknown",
                                            notes="No compatibility data for pla + hips.", material_b="hips"))
        out = _door()("pla", "hips")
        assert [ask["tool"] for ask in asks] == ["material_pairing"]
        assert out["compatible"] is False and out["interface_adhesion"] == "unknown"
        assert out["notes"] == "No compatibility data for pla + hips."
        assert out["caution"].startswith("Nothing here covers this pair")
        assert "not available either" in out["caution"]

    def test_a_pair_nothing_covers_points_at_the_general_rules_it_came_with(self, monkeypatch):
        """Kiln's servers send the general rules with a pair the table lacks;
        the caution points at them instead of saying they are missing."""
        rules = ["Materials with similar print temperatures usually bond better.", "Purge more after dark colours."]
        _served(monkeypatch, _answer(found="none", compatible=False, interface_adhesion="unknown",
                                     notes="No compatibility data for pla + hips.", material_b="hips",
                                     general_rules=rules))
        out = _door()("pla", "hips")
        assert out["general_rules"] == rules
        assert out["caution"].startswith("Nothing here covers this pair; reason it out from the general rules below")
        assert "not available" not in out["caution"]

    def test_one_material_in_both_places_is_said_to_bond_to_itself(self, monkeypatch):
        _served(monkeypatch, _answer(found="none", compatible=False, interface_adhesion="unknown",
                                     notes="No compatibility data for abs + abs.", material_a="abs",
                                     material_b="abs", general_rules=["A stand-in rule."]))
        out = _door()("ABS", "abs")
        assert "bonds to itself" in out["caution"]
        assert "Nothing here covers" not in out["caution"] and "not available" not in out["caution"]


class TestABasicAnswer:
    @pytest.mark.parametrize(
        "reply,cause,fix",
        [
            ({"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"},
             "signed_out", "sign in and ask again"),
            (urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")), "offline",
             "reconnect to the internet"),
            ({"status": "error", "code": "SERVER_UNREACHABLE", "error": "timed out"}, "unanswered", "wait a minute"),
            ({"status": "error", "code": "TOOL_DISABLED", "error": "This check is switched off."}, "refused",
             "This check is switched off."),
            ({"success": True, "found": "maybe"}, "unanswered", "wait a minute"),
        ],
    )
    def test_a_miss_is_a_basic_answer_with_one_line_saying_why(self, monkeypatch, reply, cause, fix):
        _served(monkeypatch, reply)
        out = _door()("pla", "abs")
        assert out["success"] is True and out["basic"] is True and out["why"] == cause
        assert out["compatible"] is None, "a basic answer is never a yes"
        assert out["interface_adhesion"] == "unknown"
        assert out["support_pair"] is None and out["general_rules"] == []
        assert "Kiln can't give its pairing verdict for PLA and ABS right now because" in out["caution"]
        assert fix in out["caution"]

    def test_ranges_that_do_not_overlap_cannot_share_a_nozzle_temperature(self, monkeypatch):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        out = _door()("pla", "pc")
        assert out["notes"].startswith("These two can't share one nozzle temperature")
        assert "PLA (180-220 °C)" in out["notes"] and "PC (260-310 °C)" in out["notes"]

    def test_ranges_that_overlap_judge_nothing_about_the_bond(self, monkeypatch):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        out = _door()("polycarbonate", "nylon")
        assert out["notes"].startswith("The nozzle ranges of PC (260-310 °C) and Nylon (240-280 °C) overlap.")
        assert out["compatible"] is None

    def test_a_material_the_nozzle_table_lacks_is_said(self, monkeypatch):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        out = _door()("pla_plus", "pva")
        assert out["notes"].startswith("Kiln's nozzle temperature table has no range for PLA+ or PVA,")
        assert "on this computer" not in out["notes"]
        assert "has judged nothing about this pair" in out["caution"]

    def test_the_line_promises_only_what_the_verdict_carries(self, monkeypatch):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        caution = _door()("pla", "petg")["caution"]
        assert caution.startswith(
            "Kiln's pairing verdict says whether two materials can share one print and how well they bond."
        )
        assert "purge" not in caution

    @pytest.mark.parametrize("pair", [("pla", "pla"), ("PETG-CF", "petg_cf"), ("tpu", "TPU")])
    def test_one_material_twice_is_not_a_precaution(self, monkeypatch, pair):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        out = _door()(*pair)
        assert re.match(r"^Both are .+: one material shares its own nozzle temperature and bonds to itself\.$",
                        out["notes"]), out["notes"]
        assert "answered only that one material pairs with itself" in out["caution"]
        assert "test piece" not in out["caution"]
        assert out["compatible"] is None, "a basic answer is still never a yes"

    @pytest.mark.parametrize("pair", [("pla", "tpu"), ("pla", "abs"), ("petg", "pva"), ("nylon", "pa"),
                                      ("cf_pla", "pla"), ("tpu", "tpu"), ("asa", "abs"), ("x", "y")])
    def test_a_basic_reply_carries_nothing_from_a_pairing_table(self, monkeypatch, pair):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        out = _door()(*pair)
        filled = {"hardware_warnings"} if pair == ("cf_pla", "pla") else set()
        assert set(out) == {"material_a", "material_b", "compatible", "interface_adhesion", "notes", "support_pair",
                            "general_rules", "caution", "basic", "why", "why_code", "why_detail", "success"} | filled
        assert out["compatible"] is None and out["interface_adhesion"] == "unknown"
        assert out["support_pair"] is None and out["general_rules"] == []
        assert any(shape.match(out["notes"]) for shape in _BASIC_NOTES), out["notes"]
        for grade in ("excellent", "good", "moderate", "poor"):
            assert grade not in json.dumps(out), grade


class TestAFilledFilament:
    """A filled filament needs a hardened nozzle whatever the verdict says,
    and the reply says so the same way served or basic."""

    _LINE = "PETG-CF requires a hardened/wear-resistant nozzle (not standard brass)."

    def test_a_served_answer_carries_the_hardened_nozzle_line(self, monkeypatch):
        _served(monkeypatch, _answer(material_a="petg-cf", material_b="pla"))
        out = _door()("PETG-CF", "pla")
        assert out["hardware_warnings"] == [self._LINE]
        assert out["compatible"] is True, "the verdict itself is unchanged"

    def test_a_basic_answer_carries_the_same_line(self, monkeypatch):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        out = _door()("PETG-CF", "pla")
        assert out["basic"] is True and out["hardware_warnings"] == [self._LINE]

    def test_one_line_per_material_and_none_for_an_unfilled_pair(self, monkeypatch):
        _served(monkeypatch, _answer(material_a="petg_cf", material_b="pa6_gf"))
        assert _door()("petg_cf", "pa6_gf")["hardware_warnings"] == [
            self._LINE, "PA6-GF requires a hardened/wear-resistant nozzle (not standard brass).",
        ]
        _served(monkeypatch, _answer(material_a="petg-cf", material_b="petg_cf"))
        assert _door()("petg-cf", "petg_cf")["hardware_warnings"] == [self._LINE]
        _served(monkeypatch, _answer())
        assert "hardware_warnings" not in _door()("pla", "tpu")


class TestANameTooLong:
    """A material name longer than the check reads is refused before anything
    is asked, and no reply repeats any of it."""

    _LONG = "q" * 60 + "zzmrk"

    @pytest.mark.parametrize("names", [(_LONG, "pla"), ("pla", _LONG)])
    def test_the_door_refuses_in_plain_words_and_asks_nothing(self, monkeypatch, names):
        from kiln import _pro_material_pairing_bridge as bridge

        asks = _served(monkeypatch, _answer())
        monkeypatch.setattr(bridge, "_local_co_print", lambda a, b: pytest.fail("asked kiln-pro"))
        out = _door()(*names)
        assert asks == []
        assert out == {"success": False, "code": "INVALID_INPUT",
                       "error": f"A material name is at most {bridge.MAX_MATERIAL_CHARS} characters."}

    def test_the_bridge_refuses_before_asking(self, monkeypatch):
        from kiln import _pro_material_pairing_bridge as bridge

        asks = _served(monkeypatch, _answer())
        monkeypatch.setattr(bridge, "_local_co_print", lambda a, b: pytest.fail("asked kiln-pro"))
        answer, miss = bridge.co_print("pla", self._LONG)
        assert answer is None and asks == []
        assert (miss.cause, miss.code) == ("refused", "INVALID_INPUT")
        assert "zzmrk" not in miss.detail and "qqq" not in miss.detail

    def test_the_pairing_function_refuses_without_repeating_the_name(self, monkeypatch):
        asks = _served(monkeypatch, _answer())
        with pytest.raises(ValueError) as caught:
            di.check_multi_material_compatibility(self._LONG, "pla")
        assert asks == [] and "zzmrk" not in str(caught.value)

    def test_a_name_at_the_limit_is_still_asked(self, monkeypatch):
        from kiln import _pro_material_pairing_bridge as bridge

        name = "p" * bridge.MAX_MATERIAL_CHARS
        asks = _served(monkeypatch, _answer(found="none", compatible=False, interface_adhesion="unknown",
                                            material_a=name, notes="No compatibility data."))
        out = _door()(name, "pla")
        assert [ask["material_a"] for ask in asks] == [name]
        assert out["success"] is True


def test_the_pairing_table_is_not_in_this_package():
    assert not (_SRC / "data" / "design_knowledge" / "multi_material_pairing.json").exists()
    assert not hasattr(di._DesignKnowledgeBase, "multi_material")
    hits = [p for p in _SRC.rglob("*") if p.is_file() and p.suffix in (".py", ".json")
            and "co_print_compatibility" in p.read_text(encoding="utf-8", errors="replace")]
    assert not hits, [str(p.relative_to(_SRC)) for p in hits]
