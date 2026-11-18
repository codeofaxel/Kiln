"""A door's one line about a miss stays one line when the servers' answer
arrives already worded.

The served door wraps an HTTP error in an envelope that carries its own
sentence for the tool in ``error`` and the servers' words in ``why_detail``.
A door that rebuilds its own line from that envelope must take the servers'
words, never the envelope's sentence, or its line grows a second sentence
that names a tool the person never called.
"""

from __future__ import annotations

from kiln import served_answer
from kiln.material_basics import basic_pick_line


def _envelope(status: int, detail: str) -> dict:
    return served_answer.envelope_for_http("material_substitutes", status, {"detail": detail})


def test_a_worded_envelope_gives_the_servers_words_as_the_detail():
    miss = served_answer.classify_answer(_envelope(404, "Not Found"))
    assert miss is not None
    assert miss.detail == "Not Found"


def test_the_door_line_never_names_the_tool_the_envelope_named():
    for status, detail in ((404, "Not Found"), (409, "That request could not be read.")):
        line = basic_pick_line(served_answer.classify_answer(_envelope(status, detail)), feature="material pick")
        assert "material_substitutes" not in line
        assert line.count("Kiln can't") == 1


def test_an_answer_without_a_worded_detail_keeps_its_own_text():
    miss = served_answer.classify_answer({"success": False, "why": "refused", "error": "Not on this plan."})
    assert miss is not None and miss.detail == "Not on this plan."


def test_a_server_without_the_tool_did_not_answer_and_its_name_is_not_said():
    envelope = _envelope(404, "Unknown tool: material_substitutes")
    assert envelope["why"] == "unanswered"
    assert envelope["why_code"] == "NOT_SERVED_HERE"
    assert "said no" not in envelope["error"]
    assert "material_substitutes" not in envelope["why_detail"]
    miss = served_answer.classify_answer(envelope)
    assert miss is not None and miss.cause == "unanswered" and miss.no_answer
    line = basic_pick_line(miss, feature="material pick")
    assert "didn't answer" in line and "said no" not in line
    assert "material_substitutes" not in line


def test_a_retired_tool_keeps_the_servers_words():
    words = "That tool was replaced: ask for the material record instead."
    miss = served_answer.classify_answer(_envelope(404, words))
    assert miss is not None and miss.cause == "refused" and miss.detail == words


# ---------------------------------------------------------------------------
# Signed out: the sign-in sentence names what the person asked for, never the
# served tool behind it.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from kiln import (  # noqa: E402
    _pro_adaptive_bridge,
    _pro_material_limits_bridge,
    _pro_material_pairing_bridge,
    _pro_material_pick_bridge,
    _pro_routing_bridge,
    _pro_substitution_bridge,
)


def _signed_out_misses():
    pick_printer = {"printer_has_enclosure": False, "printer_has_direct_drive": True, "max_hotend_temp_c": 260}
    return {
        _pro_routing_bridge.WIRE_TOOL: lambda: _pro_routing_bridge.pick("a shelf bracket")[1],
        "intent_materials/catalogue": lambda: _pro_routing_bridge.catalogue()[1],
        _pro_substitution_bridge.WIRE_TOOL: lambda: _pro_substitution_bridge.ask("PLA").miss,
        _pro_material_pairing_bridge.WIRE_TOOL: lambda: _pro_material_pairing_bridge.co_print("pla", "petg")[1],
        _pro_material_pairing_bridge.MIX_TOOL: lambda: _pro_material_pairing_bridge.mix_check(["PLA", "PETG"])[1],
        _pro_material_pick_bridge.FLOOR_WIRE_TOOL: lambda: _pro_material_pick_bridge.pick("a shelf bracket", pick_printer)[1],
        _pro_adaptive_bridge.WIRE_TOOL: lambda: _pro_adaptive_bridge.ask("materials")[1],
        _pro_material_limits_bridge.WIRE_TOOL: lambda: _pro_material_limits_bridge.ask("pla", nozzle_mm=0.4)[1],
    }


@pytest.fixture
def _signed_out_plain_install(monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_served_bearer", lambda: ("", None))
    for bridge in (
        _pro_routing_bridge, _pro_adaptive_bridge, _pro_substitution_bridge, _pro_material_pick_bridge,
    ):
        monkeypatch.setattr(bridge, "_local", lambda *a, _not_here=bridge._NOT_HERE, **k: _not_here)
    monkeypatch.setattr(_pro_material_pick_bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(_pro_material_pick_bridge, "_plan_includes_the_pick", lambda: False)
    monkeypatch.setattr(
        _pro_material_pairing_bridge, "_local_co_print", lambda *a, **k: _pro_material_pairing_bridge._NOT_HERE
    )
    monkeypatch.setattr(
        _pro_material_pairing_bridge, "_local_mix_check", lambda *a, **k: _pro_material_pairing_bridge._NOT_HERE
    )
    monkeypatch.setattr(
        _pro_material_limits_bridge, "_local", lambda *a, **k: _pro_material_limits_bridge._NOT_HERE
    )
    monkeypatch.setattr(_pro_material_limits_bridge, "_service_down_until", 0.0)


@pytest.mark.parametrize("wire", sorted(_signed_out_misses()))
def test_a_signed_out_miss_never_names_the_served_tool(wire, _signed_out_plain_install):
    miss = _signed_out_misses()[wire]()
    assert miss is not None and miss.cause == "signed_out", miss
    for name in (wire.split("/")[0], "intent_materials", "material_floor_pick"):
        assert name not in miss.detail, miss.detail
    assert miss.detail.startswith("Kiln"), miss.detail


# ---------------------------------------------------------------------------
# A refusal whose ``error`` is a bare code word is said in the servers' own
# sentence, found elsewhere in their answer, at every door.
# ---------------------------------------------------------------------------

_HEADLINE = "Kiln 9.9.9 or newer is required to continue."
#: The servers' version floor, as it answers (HTTP 426): the person's sentence
#: is in the upgrade block, the ``error`` field is a code word.
_VERSION_FLOOR = {
    "success": False,
    "error": "upgrade_required",
    "code": "UPGRADE_REQUIRED",
    "upgrade": {"headline": _HEADLINE, "target": "9.9.9"},
}


def test_a_code_word_refusal_is_said_in_the_servers_sentence():
    miss = served_answer.classify_answer(_VERSION_FLOOR)
    assert miss is not None and miss.cause == "refused" and miss.code == "UPGRADE_REQUIRED"
    assert miss.detail == _HEADLINE


def test_a_code_word_with_no_sentence_beside_it_is_no_reason_rather_than_a_word():
    miss = served_answer.classify_answer({"success": False, "error": "upgrade_required", "code": "UPGRADE_REQUIRED"})
    assert miss is not None and miss.detail == ""
    line = basic_pick_line(miss, feature="material pick")
    assert "upgrade_required" not in line and "gave no reason" in line


def test_a_code_word_takes_the_message_when_there_is_no_upgrade_block():
    miss = served_answer.classify_answer(
        {"status": "error", "code": "SUSPENDED", "error": "suspended", "message": "This account is paused."}
    )
    assert miss is not None and miss.detail == "This account is paused."


@pytest.mark.parametrize("wire", sorted(_signed_out_misses()))
def test_every_material_bridge_says_the_version_floor_in_words(wire, _signed_out_plain_install, monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_pro_api_call", lambda *_a, **_k: dict(_VERSION_FLOOR))
    miss = _signed_out_misses()[wire]()
    assert miss is not None and miss.cause == "refused", miss
    assert miss.detail == _HEADLINE, miss.detail


def test_the_material_doors_never_end_on_the_code_word(_signed_out_plain_install, monkeypatch):
    import json

    import kiln.server as srv
    from kiln.material_routing import recommend_material

    monkeypatch.setattr(srv, "_pro_api_call", lambda *_a, **_k: dict(_VERSION_FLOOR))
    replies = {
        "find_material_substitute": srv.find_material_substitute("PLA"),
        "recommend_material": recommend_material("a shelf bracket").to_dict(),
    }
    for door, reply in replies.items():
        said = json.dumps(reply)
        assert "upgrade_required" not in said, (door, reply)
        assert _HEADLINE in said, (door, reply)
