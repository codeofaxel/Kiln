"""``slice_and_print`` with NO hardware named: a sealed pocket is asked about.

Before this, "print it" on a part with a nut pocket inside printed the pocket
over empty, because the planner was asked only when hardware was named.  Now
a part with pockets and nothing named is put to the planner as a question --
what does each pocket look cut for -- and a pocket that would be sealed
inside the print refuses the start with that question, until the person
answers it or says in so many words that nothing goes in.

It is a question, not a verdict: a planner that cannot answer (not on this
install, signed out, offline, the servers said no) lets the print go on and
says so.  A part with no pockets never asks.  The slicer, printer and planner
are faked the way ``test_slice_and_print_hardware`` fakes them; the tool is
the real one.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

# The harness of the named-hardware tests, loaded by path: the tests dir is
# not a package, and the fakes belong to one file.
_spec = importlib.util.spec_from_file_location(
    "_hardware_print_harness", Path(__file__).with_name("test_slice_and_print_hardware.py"),
)
_harness = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_harness)
SLICE, _Printer, _run, _sent_nothing = _harness.SLICE, _harness._Printer, _harness._run, _harness._sent_nothing
slicer_tools = _harness.slicer_tools  # the module-scoped fixture
_no_preview_gate = _harness._no_preview_gate  # the autouse one

PLANNER_ASK = "S1 (5.7 mm hex closed cavity, 2.6 mm deep): an M3 nut — right?"


class _Proposer:
    """The planner answering a question about pockets: records the call,
    reads no file (none is sent for a question)."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer


def _proposals(*, needs_pause: bool | None = True, enclosed: bool = True, upgrade: bool = True) -> dict[str, Any]:
    """A free caller's answer: names and the question, no sizes."""
    proposal: dict[str, Any] = {
        "seats": ["S1"], "item": "1x M3 nut in S1", "kind": "nut", "size": "M3", "count": 1,
        "confidence": "likely", "ask": PLANNER_ASK, "alternatives": ["M2.5 nut"],
    }
    if needs_pause is not None:
        proposal["needs_pause"] = needs_pause
    answer: dict[str, Any] = {
        "status": "success", "your_tier": "free", "placements": [],
        "seats": [{"id": "S1", "size_mm": 5.7, "enclosed": enclosed,
                   "narrowest_opening_mm": 0.0 if enclosed else "face"}],
        "proposed_hardware": [proposal], "hardware_to_confirm": ["1x M3 nut in S1"],
        "sealed_pockets": [{"seat": "S1", "where": "5.7 mm hex closed cavity, 2.6 mm deep",
                            "what_happens": "sealed inside the print"}] if enclosed else [],
    }
    if upgrade:
        answer["upgrade"] = {"headline": "Plan this hardware with a paid plan.", "upgrade_url": "https://kiln3d.com/pricing"}
    return answer


@pytest.fixture()
def pockets(monkeypatch):
    """The part has pockets, read on this computer, so the planner is asked."""
    import kiln.plugins.slicer_tools as _st

    monkeypatch.setattr(_st, "_part_has_pockets", lambda _path: True)


def test_a_sealed_pocket_nobody_named_is_asked_about_not_printed_over(slicer_tools, tmp_path, monkeypatch, pockets):
    printer, planner = _Printer(), _Proposer(_proposals())
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware=None)

    assert resp["success"] is False and resp["error"]["code"] == "HARDWARE_UNNAMED"
    message = resp["error"]["message"]
    assert PLANNER_ASK in message and 'hardware=["none"]' in message and "Nothing was sent to the printer." in message
    assert resp["hardware"] == {"proposed": [PLANNER_ASK], "to_confirm": ["1x M3 nut in S1"]}
    assert resp["upgrade"]["upgrade_url"] == "https://kiln3d.com/pricing"  # the planner's own rope, handed on
    _sent_nothing(printer)
    # Asked as a question about the model: no hardware, no sliced file sent.
    (call,) = planner.calls
    assert call["model_path"].endswith("input.stl") and "gcode_path" not in call and "hardware" not in call
    assert call.get("write_pauses") is None


def test_pockets_that_open_on_a_face_print_with_a_note(slicer_tools, tmp_path, monkeypatch, pockets):
    printer = _Printer()
    planner = _Proposer(_proposals(needs_pause=False, enclosed=False))
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware=None)

    assert resp["success"] is True
    assert resp["hardware"]["proposed"] == [PLANNER_ASK] and "after the print" in resp["hardware"]["note"]
    assert printer.uploads == [("out.gcode", SLICE)]


@pytest.mark.parametrize("word", [["none"], ["No hardware"], "nothing", ["empty"]])
def test_saying_none_prints_every_pocket_empty_when_the_file_agrees(slicer_tools, tmp_path, monkeypatch, pockets, word):
    """Their word is taken: the planner is asked only whether the file contradicts it."""
    printer, planner = _Printer(), _Proposer(_proposals())  # pockets, but no pause in the file
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware=word)

    assert resp["success"] is True
    assert resp["hardware"]["declared"] == "none"
    assert printer.uploads == [("out.gcode", SLICE)]
    (call,) = planner.calls
    assert "hardware" not in call and call["gcode_path"].endswith("out.gcode")


def test_none_with_a_pause_already_over_a_sealed_pocket_is_a_contradiction_not_a_print(
    slicer_tools, tmp_path, monkeypatch, pockets,
):
    answer = _proposals()
    answer["stops_in_file"] = [{"word": "M601", "before_layer": 29, "stops_this_printer": "stops"}]
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Proposer(answer), hardware=["none"])

    assert resp["success"] is False and resp["error"]["code"] == "HARDWARE_NONE_BUT_PAUSED"
    message = resp["error"]["message"]
    assert "You said nothing goes in" in message and "before layer 29" in message and "Nothing was sent to the printer." in message
    assert resp["hardware"]["pauses_in_file"][0]["before_layer"] == 29
    _sent_nothing(printer)


def test_none_with_a_pause_but_no_sealed_pocket_prints(slicer_tools, tmp_path, monkeypatch, pockets):
    """A pause over pockets that open on a face contradicts nothing: a colour change, say."""
    answer = _proposals(needs_pause=False, enclosed=False)
    answer["sealed_pockets"] = []
    answer["stops_in_file"] = [{"word": "M601", "before_layer": 12, "stops_this_printer": "stops"}]
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Proposer(answer), hardware=["none"])

    assert resp["success"] is True and resp["hardware"]["declared"] == "none"
    assert printer.uploads == [("out.gcode", SLICE)]


def test_none_when_the_planner_cannot_answer_takes_their_word(slicer_tools, tmp_path, monkeypatch, pockets):
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Proposer(RuntimeError("down")), hardware=["none"])
    assert resp["success"] is True and resp["hardware"]["declared"] == "none"
    assert printer.uploads == [("out.gcode", SLICE)]


def test_an_older_planner_is_read_from_its_seats(slicer_tools, tmp_path, monkeypatch, pockets):
    """A planner that does not say ``needs_pause`` is judged by the seat it
    names: closed on both sides refuses, open to a face prints."""
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Proposer(_proposals(needs_pause=None, enclosed=True)),
                hardware=None)
    assert resp["success"] is False and resp["error"]["code"] == "HARDWARE_UNNAMED"
    _sent_nothing(printer)

    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Proposer(_proposals(needs_pause=None, enclosed=False)),
                hardware=None)
    assert resp["success"] is True and printer.uploads == [("out.gcode", SLICE)]


@pytest.mark.parametrize("answer", [
    {"success": False, "error": {"code": "ACCOUNT_REQUIRED", "message": "Sign in to Kiln first."}},
    {"status": "error", "code": "HOSTED_UNREACHABLE", "error": "no answer", "retryable": True},
    RuntimeError("the planner fell over"),
])
def test_a_planner_that_cannot_answer_lets_the_print_go_and_says_so(slicer_tools, tmp_path, monkeypatch, pockets, answer):
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Proposer(answer), hardware=None)

    assert resp["success"] is True
    assert resp["hardware"]["unchecked"]
    assert printer.uploads == [("out.gcode", SLICE)]


def test_an_install_without_the_planner_prints_and_says_so(slicer_tools, tmp_path, monkeypatch, pockets):
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, "absent", hardware=None)

    assert resp["success"] is True and "not on this install" in resp["hardware"]["unchecked"]
    assert printer.uploads == [("out.gcode", SLICE)]


def test_a_part_with_no_pockets_never_asks(slicer_tools, tmp_path, monkeypatch):
    import kiln.plugins.slicer_tools as _st

    monkeypatch.setattr(_st, "_part_has_pockets", lambda _path: False)
    printer, planner = _Printer(), _Proposer(_proposals())
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware=None)

    assert resp["success"] is True and planner.calls == [] and "hardware" not in resp


def test_a_planner_with_nothing_to_propose_is_quiet(slicer_tools, tmp_path, monkeypatch, pockets):
    printer, planner = _Printer(), _Proposer({"status": "success", "your_tier": "free", "placements": [], "seats": []})
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, planner, hardware=None)

    assert resp["success"] is True and "hardware" not in resp and len(planner.calls) == 1


def test_the_free_answer_is_handed_on_without_a_size_in_it(slicer_tools, tmp_path, monkeypatch, pockets):
    """The door repeats the planner's names and questions; it adds no
    measurement of its own, so a free caller sees exactly the free band."""
    printer = _Printer()
    resp = _run(slicer_tools, tmp_path, monkeypatch, printer, _Proposer(_proposals()), hardware=None)
    text = json.dumps(resp["hardware"]) + resp["error"]["message"]
    assert "5.5" not in text and "2.4" not in text  # an M3 nut's flats and height never appear
    assert "M3 nut" in text
