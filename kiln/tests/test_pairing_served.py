"""Whether two materials share one dual-extrusion print, asked of Kiln's servers.

``check_multi_material_pairing`` answers from Kiln's pairing verdict, asked
through :mod:`kiln._pro_pairing_bridge` (kiln-pro, https://kiln3d.com).  These
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

#: The three shapes a basic answer's notes take, and nothing else.
_BASIC_NOTES = (
    re.compile(r"^These two can't share one nozzle temperature: the nozzle ranges of .+ do not overlap\. "
               r"How well they stick together was not judged\.$"),
    re.compile(r"^The nozzle ranges of .+ overlap\. How well they stick together and how much to purge "
               r"between them was not judged\.$"),
    re.compile(r"^Kiln has no nozzle temperature range for .+ on this computer, so neither their "
               r"temperatures nor how well they stick together was judged\.$"),
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

    def call(tool_name, _timeout=None, _asked_by_user=None, **kwargs):
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
        _served(monkeypatch, _answer(found="none", compatible=False, interface_adhesion="unknown",
                                     notes="No compatibility data for pla + hips.", material_b="hips"))
        out = _door()("pla", "hips")
        assert out["compatible"] is False and out["interface_adhesion"] == "unknown"
        assert out["notes"] == "No compatibility data for pla + hips."
        assert out["caution"].startswith("Nothing here covers this pair")


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
        assert out["notes"].startswith("Kiln has no nozzle temperature range for PLA+ or PVA")
        assert "has judged nothing about this pair" in out["caution"]

    @pytest.mark.parametrize("pair", [("pla", "tpu"), ("pla", "abs"), ("petg", "pva"), ("nylon", "pa"),
                                      ("cf_pla", "pla"), ("tpu", "tpu"), ("asa", "abs"), ("x", "y")])
    def test_a_basic_reply_carries_nothing_from_a_pairing_table(self, monkeypatch, pair):
        _served(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"})
        out = _door()(*pair)
        assert set(out) == {"material_a", "material_b", "compatible", "interface_adhesion", "notes", "support_pair",
                            "general_rules", "caution", "basic", "why", "why_code", "why_detail", "success"}
        assert out["compatible"] is None and out["interface_adhesion"] == "unknown"
        assert out["support_pair"] is None and out["general_rules"] == []
        assert any(shape.match(out["notes"]) for shape in _BASIC_NOTES), out["notes"]
        for grade in ("excellent", "good", "moderate", "poor"):
            assert grade not in json.dumps(out), grade


def test_the_pairing_table_is_not_in_this_package():
    assert not (_SRC / "data" / "design_knowledge" / "multi_material_pairing.json").exists()
    assert not hasattr(di._DesignKnowledgeBase, "multi_material")
    hits = [p for p in _SRC.rglob("*") if p.is_file() and p.suffix in (".py", ".json")
            and "co_print_compatibility" in p.read_text(encoding="utf-8", errors="replace")]
    assert not hits, [str(p.relative_to(_SRC)) for p in hits]
