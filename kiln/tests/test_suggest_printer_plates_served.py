"""``suggest_printer_for_job`` reads plate room through Kiln's servers where the survey is not on this computer.

Each suggestion says ``has_room``.  The plate survey that judges it runs on
Kiln's servers, so an install without it used to answer ``null`` at every
plan, a Business account included.  Now the door reads each plate HERE, the
same reading ``route_print_job`` sends, and asks the same served door for the
plates alone.  With the server stood in (public tests cannot import
kiln-pro, and here they cannot even find it), these pin the door's side:

* a Business answer fills ``has_room`` and the plate block per printer, and
  says nothing in ``plate_note``;
* what goes out: each printer by name alone, one plate reading each with no
  part, the plates-only ask, and never an address or an access code;
* below the plan, ``has_room`` is ``null`` and ``plate_note`` is the servers'
  own sentence, said once and never pitched twice;
* a call that got no answer (offline, signed out, no answer) is said in the
  shared voice; Kiln's servers older than the plates-only ask say so.
"""

from __future__ import annotations

import errno
import sys
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_fleet_plate_doors import _block, doors  # noqa: F401 -- fixture

HOST, ACCESS_CODE = "192.168.7.42", "s3cret-access-code"

#: The servers' refusal below Business, as their gate writes it.
BELOW_BUSINESS = {
    "status": "error",
    "error": (
        "Plate room across your printers (which of them can take a print now) needs Kiln Business. "
        "Business adds team review, approval gates, and 3 printers (grow to 50). Upgrade at "
        "https://kiln3d.com/pricing — or run `kiln subscribe --tier business`."
    ),
    "code": "TIER_REQUIRED",
    "required_tier": "business",
    "retryable": False,
    "upgrade_url": "https://kiln3d.com/pricing",
}


@pytest.fixture
def plain_install(monkeypatch):
    """No kiln-pro anywhere on this computer."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def suggest(doors, monkeypatch, plain_install):  # noqa: F811 -- the fixture, passed in
    """``suggest_printer_for_job`` over printers a and b, the survey absent,
    the served call answering *answer* (or raising it)."""
    import kiln.persistence as persistence
    import kiln.server as srv

    ranked = [{"printer_name": "a", "success_rate": 0.9, "total_prints": 4},
              {"printer_name": "b", "success_rate": 0.8, "total_prints": 2}]
    monkeypatch.setattr(persistence, "get_db", lambda: SimpleNamespace(suggest_printer_for_outcome=lambda **kw: list(ranked)))

    def make(answer: Any):
        d = doors(["a", "b"], survey_missing=True)
        for name, adapter in d.registry.adapters.items():
            adapter.host, adapter.access_code, adapter.serial = HOST, ACCESS_CODE, f"01P00A00000{name}"
        sent: list[dict] = []

        def served(tool, _timeout=None, _asked_by_user=True, **kw):
            sent.append({"tool": tool, **kw})
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(srv, "_pro_api_call", served)
        return d.tools["suggest_printer_for_job"], sent

    return make


def _rooms(result: dict) -> dict[str, Any]:
    return {s["printer_name"]: s["has_room"] for s in result["suggestions"]}


class TestABusinessAnswer:
    def test_has_room_and_the_plate_come_from_the_servers_answer(self, suggest):
        occupied = _block(status="occupied", has_room=False, refusals=["the jar"], occupied_count=1)
        door, sent = suggest({"success": True, "plates": {"a": _block(), "b": occupied}})
        result = door(material_type="PLA")
        assert result["success"] is True
        assert _rooms(result) == {"a": True, "b": False}
        assert [s["plate"] for s in result["suggestions"]] == [_block(), occupied]
        assert result["plate_note"] is None
        assert len(sent) == 1 and sent[0]["tool"] == "plan_fleet_route" and sent[0]["plates_only"] is True

    def test_what_goes_out_is_each_printer_by_name_with_no_part_and_no_address(self, suggest):
        door, sent = suggest({"success": True, "plates": {"a": _block(), "b": _block()}})
        door(material_type="PLA", file_name="cube.stl")
        body = sent[0]
        assert body["candidates"] == [{"printer_id": "a"}, {"printer_id": "b"}]
        assert body["material"] == "PLA" and set(body["plates"]) == {"a", "b"}
        for reading in body["plates"].values():
            assert reading["schema"] == "plate_reading/1" and reading["file_name"] == "cube.stl"
            assert reading["request"] is None or reading["request"]["part"] is None
        wire = repr(body)
        assert HOST not in wire and ACCESS_CODE not in wire

    def test_with_no_material_named_the_plates_are_still_asked_for(self, suggest):
        door, sent = suggest({"success": True, "plates": {"a": _block(), "b": _block()}})
        result = door()
        assert sent[0]["material"] == "" and _rooms(result) == {"a": True, "b": True}


class TestBelowThePlan:
    def test_has_room_is_null_and_the_servers_sentence_is_said_once(self, suggest):
        door, _ = suggest(dict(BELOW_BUSINESS))
        result = door(material_type="PLA")
        assert result["success"] is True
        assert _rooms(result) == {"a": None, "b": None}
        assert [s["plate"] for s in result["suggestions"]] == [None, None]
        note = result["plate_note"]
        assert note == BELOW_BUSINESS["error"]
        assert note.count("needs Kiln Business") == 1 and note.count("kiln3d.com/pricing") == 1


class TestNoAnswer:
    @pytest.mark.parametrize(("answer", "said"), [
        (urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")), "this computer is offline"),
        ({"status": "error", "error": "Sign in to use this.", "code": "KILN_ACCOUNT_NOT_PAIRED",
          "required_tier": "free", "why": "signed_out"}, "Kiln is signed out"),
        ({"status": "error", "success": False, "error": "no answer", "code": "SERVER_UNREACHABLE",
          "why": "unanswered"}, "didn't answer"),
    ], ids=["offline", "signed_out", "unanswered"])
    def test_a_miss_is_said_in_the_shared_voice_and_room_stays_unknown(self, suggest, answer, said):
        door, _ = suggest(answer)
        result = door(material_type="PLA")
        assert result["success"] is True and _rooms(result) == {"a": None, "b": None}
        note = result["plate_note"]
        assert said in note and "Kiln can't ask Kiln's servers which of these plates have room" in note, note
        for word in ("kiln-pro", "kiln_pro", "Business", "pricing"):
            assert word not in note, f"{word!r} in {note!r}"

    def test_servers_older_than_the_plates_only_ask_say_so(self, suggest):
        door, _ = suggest({"success": False, "error": "Unknown parameters: plates_only"})
        result = door(material_type="PLA")
        assert _rooms(result) == {"a": None, "b": None}
        note = result["plate_note"]
        assert "do not read plate room for these suggestions yet" in note and "route_print_job" in note
        assert "Unknown parameters" not in note
