"""Every slice door refuses to slice onto a part left on the plate, and places beside it when told where.

THE DEFECT
----------
A slice on a plate that still held the last print moved the new part to
the centre of the bed -- onto the old one -- and said nothing.  The plate
record already knew the part was there.

WHAT THIS PINS
--------------
One shared gate in front of all four slice doors (``slice_model``,
``reslice_with_overrides``, ``slice_and_print``, ``slice_and_estimate``),
walked by one parametrised class so a door cannot quietly opt out:

* an occupied plate with no ``placement`` refuses before anything is
  sliced, naming what is there and the spots that would work;
* a named spot with an ok verdict moves the part there in a copy, slices
  the copy, sends the sliced file back for a second verdict, and the
  success result carries the verdict and says a design-mesh approval does
  not carry;
* a refusing verdict refuses with the engine's own sentence;
* no verdict at all -- offline, signed out, no answer -- refuses, fail
  closed, with one fixed sentence;
* a verdict that fails on the sliced file refuses the result: no file is
  recommended or handed on;
* a clear plate passes through exactly as before.

The bridge and the slicer are stubbed the way the sibling tests stub them;
nothing here talks to a printer, a slicer or the network.  Named regions,
the fail-closed wording and the stage's occupancy block are pinned below.
"""

from __future__ import annotations

import inspect
import os
import re
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln import _pro_placement_bridge as bridge
from kiln import served_answer as sa
from kiln.plate_state import PlateJob, PlateState, mark_clear, mark_occupied
from kiln.printers.bed_fit import compute_mesh_bbox
from kiln.slicer import SliceResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cube(path: Path, size: float = 20.0, off: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> str:
    ox, oy, oz = off
    v = [
        (ox, oy, oz), (ox + size, oy, oz), (ox + size, oy + size, oz), (ox, oy + size, oz),
        (ox, oy, oz + size), (ox + size, oy, oz + size), (ox + size, oy + size, oz + size), (ox, oy + size, oz + size),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _machine() -> SimpleNamespace:
    """A machine with a serial (the plate record's key) and just enough of
    an adapter for a door to reach its start: it uploads and reports idle."""
    return SimpleNamespace(
        name="bambu",
        serial="01P00A000000001",
        _printer_model="bambu_a1",
        upload_file=lambda path: SimpleNamespace(
            success=True, file_name=os.path.basename(path), message="uploaded",
            to_dict=lambda: {"success": True, "file_name": os.path.basename(path)},
        ),
        get_state=lambda: SimpleNamespace(connected=True, state=SimpleNamespace(value="idle")),
        start_print=lambda *a, **k: pytest.fail("a start must never reach the printer while the plate is occupied"),
    )


def _occupy(machine, *, max_z: float | None = 42.0, file: str = "jar_v2.gcode.3mf") -> None:
    mark_occupied(machine, PlateJob(file=file, footprint_mm=[90, 90, 160, 160], max_z_mm=max_z, printer_id="bambu_a1"))


def _verdict(ok: bool = True, at=(40.0, 40.0), spots=None, refusals=None, placed_by: str = "agent") -> dict:
    at = list(at)
    return {
        "schema": bridge.SCHEMA, "ok": ok, "placed_by": placed_by, "at_mm": at if ok else None,
        "tower_at_mm": None, "footprint_mm": [at[0], at[1], at[0] + 20.0, at[1] + 20.0] if ok else None,
        "clearance_mm": 12.0 if ok else None,
        "refusals": refusals if refusals is not None else ([] if ok else [{"code": "TOO_CLOSE", "sentence": "the head would clip the jar on its way down"}]),
        "conflicts": [], "switched_off": {},
        "spots": spots if spots is not None else [{"at_mm": [40.0, 40.0], "clearance_mm": 12.0}, {"at_mm": [40.0, 200.0], "clearance_mm": 30.0}],
        "occupancy": {"kind": bridge.OCCUPANCY_KIND, "bed_mm": [240.0, 240.0],
                      "occupied": [{"name": "jar_v2.gcode.3mf", "rect_mm": [90, 90, 160, 160], "top_mm": 42.0}],
                      "proposed": {"rect_mm": [at[0], at[1], at[0] + 20.0, at[1] + 20.0], "ok": ok} if ok else None,
                      "source": "gcode"},
        "record": {"printer_id": "bambu_a1", "measured": True, "source": "overlay"},
        "tier": {"verdict": "free", "plan": "pro"},
    }


class _Bridge:
    """A scripted bridge: one answer per ask, in order; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[dict] = []

    def __call__(self, request):
        self.asked.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    import kiln.server as srv
    from kiln import monitor_twin

    d = tmp_path / "twin"
    d.mkdir()
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", d)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", d / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", d / "active.json")
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "error": "no network in tests"})
    # A 240 mm bed keeps the thirds round (80 mm) for the region tests.
    monkeypatch.setattr("kiln.printers.bed_fit.get_build_volume", lambda pid: (240.0, 240.0, 250.0))


@pytest.fixture
def machine(monkeypatch):
    import kiln.server as srv

    m = _machine()
    monkeypatch.setattr(srv, "_resolve_adapter", lambda *_a, **_k: m)
    return m


def _fake_slice(tmp_path: Path) -> tuple[MagicMock, str]:
    gcode = tmp_path / "out.gcode"
    gcode.write_text(";LAYER_CHANGE\n;Z:0.2\nG1 X1 Y1 E1\n; filament used [g] = 4.26\n")
    result = SliceResult(success=True, output_path=str(gcode), slicer="prusa-slicer", message="Sliced")
    return MagicMock(return_value=result), str(gcode)


def _slicer_tools() -> dict[str, Any]:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    return tools


def _estimate_tools() -> dict[str, Any]:
    from kiln.plugins.estimate_tools import _EstimateToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _EstimateToolsPlugin().register(_FakeMcp())
    return tools


#: (door name, registry, extra kwargs).  slice_and_print is stopped at the
#: upload step -- past the slice and the second verdict, before any printer.
DOORS = [
    ("slice_model", _slicer_tools, {}),
    ("reslice_with_overrides", _slicer_tools, {"overrides": {"brim_width": "5"}}),
    ("slice_and_print", _slicer_tools, {"skip_validation": True}),
    ("slice_and_estimate", _estimate_tools, {}),
]

#: The doors whose output is only a number.  They walk every scenario below
#: with the rest -- none may quietly opt out -- but where a door that makes a
#: printable file refuses because of the plate, an estimate answers: nothing
#: it produces is ever started onto the plate (Adam, 2026-09-22).
ESTIMATE_DOORS = frozenset({"slice_and_estimate"})


def _assert_estimates_the_part_on_an_empty_plate(resp: dict, spy: Any) -> None:
    """What an estimate door does where a printing door refuses: it answers,
    for the part as sliced on an empty plate, and says that is what it is."""
    from kiln.plugins.estimate_tools import EMPTY_PLATE_ESTIMATE_NOTE

    assert resp["success"] is True, resp
    assert resp["plate_note"] == EMPTY_PLATE_ESTIMATE_NOTE
    assert spy.called, "an estimate that answers has sliced something"
    assert not str(spy.call_args.args[0]).endswith("_placed.stl"), "the fallback estimates the part unplaced"
    assert "placement" not in resp, "an estimate on an empty plate claims no spot beside the part"
    if "message" in resp:
        # The summary is what gets read: the caveat rides it, not only a side key.
        assert resp["message"].endswith(EMPTY_PLATE_ESTIMATE_NOTE), resp["message"]


def _call(door: str, registry, extra: dict, **kwargs) -> dict:
    tool = registry()[door]
    with patch("kiln.printers.upload_prep.prepare_upload_for_adapter", side_effect=RuntimeError("stop here")):
        return tool(printer_id="ender3", **extra, **kwargs)


# ---------------------------------------------------------------------------
# Every door
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("door", "registry", "extra"), DOORS, ids=[d[0] for d in DOORS])
class TestEveryDoor:
    def test_the_door_takes_placement(self, door, registry, extra):
        param = inspect.signature(registry()[door]).parameters.get("placement")
        assert param is not None and param.default is None

    def test_an_occupied_plate_with_no_placement_refuses_before_slicing(self, door, registry, extra, tmp_path, machine, monkeypatch):
        _occupy(machine)
        asked = _Bridge((_verdict(ok=False, spots=[{"at_mm": [40.0, 200.0], "clearance_mm": 30.0}]), None))
        monkeypatch.setattr(bridge, "ask", asked)
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl)
        if door in ESTIMATE_DOORS:
            _assert_estimates_the_part_on_an_empty_plate(resp, spy)
            return
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_PLATE_OCCUPIED"
        msg = resp["error"]["message"]
        assert msg.startswith("The last print, jar v2, is still on the plate (since ")
        assert "about 42 mm tall" in msg and "on top of it" in msg
        assert "Spots with room: [40, 200] (30 mm clear)." in msg
        assert resp["spots"] == [{"at_mm": [40.0, 200.0], "clearance_mm": 30.0}]
        assert resp["occupancy"]["kind"] == bridge.OCCUPANCY_KIND
        assert resp["plate"]["status"] == "occupied"
        assert asked.asked[0]["placement"] == "auto" and asked.asked[0]["placed_by"] == "auto"
        assert not spy.called, "nothing is sliced onto an occupied plate without a spot"

    def test_a_named_spot_with_an_ok_verdict_translates_slices_and_checks_the_sliced_file(self, door, registry, extra, tmp_path, machine, monkeypatch):
        _occupy(machine)
        asked = _Bridge((_verdict(ok=True, at=(40.0, 40.0)), None))
        monkeypatch.setattr(bridge, "ask", asked)
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        spy, gcode = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl, placement=[40, 40])

        # The slicer was handed a placed COPY whose footprint origin is the spot.
        sliced = spy.call_args.args[0]
        assert sliced != stl and sliced.endswith("_placed.stl")
        bbox = compute_mesh_bbox(sliced)
        assert (bbox["x_min"], bbox["y_min"]) == pytest.approx((40.0, 40.0))
        assert (bbox["x_max"], bbox["y_max"]) == pytest.approx((60.0, 60.0))
        # Two verdicts: the envelope, then the sliced file.
        assert len(asked.asked) == 2
        assert asked.asked[0]["placement"] == [40.0, 40.0] and asked.asked[0]["placed_by"] == "agent"
        assert asked.asked[0]["sliced_gcode"] is None
        assert asked.asked[0]["part"]["size_mm"] == [20.0, 20.0, 20.0]
        assert asked.asked[1]["sliced_gcode"] == {"path": gcode}
        assert asked.asked[1]["placement"] == [40.0, 40.0]
        assert asked.asked[0]["plate"]["job"]["file"] == "jar_v2.gcode.3mf"

        if door == "slice_and_print":
            # Sliced and checked, then refused at the start: the file's own
            # start sequence crosses the plate.  The slice and its verdict
            # ride the refusal, attached before any start could happen.
            assert resp["success"] is False and resp["error"]["code"] == "PLATE_OCCUPIED_START_NOT_YET"
            assert resp["error"]["message"].startswith("The last print, jar v2, is still on the plate (since ")
            assert "won't start this one" in resp["error"]["message"]
            assert resp["slice"]["output_path"] == gcode
            assert resp["placement"]["ok"] is True and resp["approval_carries"] is False
            assert resp["start"] == {"allowed": False, "why": resp["error"]["message"]}
            return
        assert resp["success"] is True
        assert resp["placement"]["ok"] is True and resp["placement"]["schema"] == bridge.SCHEMA
        assert resp["approval_carries"] is False
        assert "jar v2" in resp["approval_note"] and "approve from here" in resp["approval_note"]
        # A slice beside the occupant is not a print beside it: the result
        # says so, so an agent does not go on to start it by hand.
        assert resp["start"]["allowed"] is False
        assert resp["start"]["why"].startswith("The last print, jar v2, is still on the plate (since ")
        assert "won't start this one" in resp["start"]["why"]
        stage = resp.get("stage_mesh_path") or resp.get("slice", {}).get("stage_mesh_path")
        assert stage in (None, sliced), "the stage shows the placed copy when the result names a mesh"

    def test_a_refusing_verdict_refuses_with_its_sentence(self, door, registry, extra, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False), None)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl, placement=[100, 100])
        if door in ESTIMATE_DOORS:
            _assert_estimates_the_part_on_an_empty_plate(resp, spy)
            return
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_REFUSED"
        assert "the head would clip the jar on its way down." in resp["error"]["message"]
        assert "Spots with room: [40, 40] (12 mm clear), [40, 200] (30 mm clear)." in resp["error"]["message"]
        assert resp["placement"]["ok"] is False and len(resp["spots"]) == 2
        assert not spy.called

    @pytest.mark.parametrize("reason", [
        sa.Miss("offline"), sa.Miss("signed_out"), sa.Miss("unanswered"),
        sa.Miss("refused", "MACHINE_NOT_PAIRED", "This device has not reported that printer; register it and ask again."),
    ])
    def test_no_verdict_fails_closed(self, door, registry, extra, tmp_path, machine, monkeypatch, reason):
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((None, reason)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl, placement=[40, 40])
        if door in ESTIMATE_DOORS:
            # An estimate needs no clearance verdict, so being offline or
            # signed out never stops one.
            _assert_estimates_the_part_on_an_empty_plate(resp, spy)
            return
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_NO_VERDICT"
        from kiln import plate_state

        assert resp["error"]["message"] == _no_verdict_sentence(plate_state.read(machine), reason)
        # The shared voice's why fields ride beside the sentence, never inside it.
        assert resp["why"] == reason.cause and resp["why_code"] == reason.code
        assert not reason.code or reason.code not in resp["error"]["message"]
        assert resp["occupancy"]["source"] == "record_box"
        assert not spy.called

    def test_a_verdict_that_fails_on_the_sliced_file_refuses_the_result(self, door, registry, extra, tmp_path, machine, monkeypatch):
        _occupy(machine)
        second = _verdict(ok=False, refusals=[{"code": "TOWER", "sentence": "the prime tower lands on the jar"}], spots=[])
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None), (second, None)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl, placement=[40, 40])
        if door in ESTIMATE_DOORS:
            # The refusal came AFTER the bed-fit gate ran: it is still the
            # plate's, and the estimate still answers.
            _assert_estimates_the_part_on_an_empty_plate(resp, spy)
            assert spy.call_count == 2, "the placed copy was sliced, refused, and the part sliced again unplaced"
            return
        assert spy.called, "the slice happened; what is refused is handing the file on"
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_REFUSED"
        assert "checked the sliced file" in resp["error"]["message"]
        assert "the prime tower lands on the jar." in resp["error"]["message"]
        assert "Clear the plate and say so." in resp["error"]["message"]
        for key in ("recommended_upload_path", "output_path", "output_3mf_path", "estimate", "upload"):
            assert key not in resp

    def test_a_clear_plate_passes_through_unchanged(self, door, registry, extra, tmp_path, machine, monkeypatch):
        mark_clear(machine, "human")
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("a clear plate asks nobody"))
        stl = _cube(tmp_path / "part.stl", off=(10.0, 10.0, 0.0))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl)
        assert spy.call_args.args[0] == stl
        assert "placement" not in resp and "approval_note" not in resp

    def test_no_printer_at_all_passes_through(self, door, registry, extra, tmp_path, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_resolve_adapter", MagicMock(side_effect=RuntimeError("no adapters in tests")))
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("no printer means no plate to ask about"))
        stl = _cube(tmp_path / "part.stl", off=(10.0, 10.0, 0.0))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            _call(door, registry, extra, input_path=stl, placement=[40, 40])
        assert spy.call_args.args[0] == stl


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def _gate(input_path: str, placement, **over):
    from kiln.plugins.slicer_tools import _apply_plate_placement

    kwargs = {"effective_printer_id": "ender3", "printer_name": None, "placement": placement}
    kwargs.update(over)
    return _apply_plate_placement(input_path, **kwargs)


class TestNamedRegions:
    """Resolved in the door, never in the engine: the engine is asked for
    spots, the door keeps the ones in that third of the bed."""

    def _probe(self):
        return _verdict(ok=True, spots=[
            {"at_mm": [10.0, 160.0], "clearance_mm": 5.0},    # back-left (centre 20, 170)
            {"at_mm": [30.0, 170.0], "clearance_mm": 9.0},    # back-left (centre 40, 180) -- best
            {"at_mm": [100.0, 160.0], "clearance_mm": 20.0},  # back-centre
            {"at_mm": [200.0, 10.0], "clearance_mm": 40.0},   # front-right
        ])

    def test_a_region_picks_its_best_spot_and_asks_again_as_a_human(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        asked = _Bridge((self._probe(), None), (_verdict(ok=True, at=(30.0, 170.0), placed_by="human"), None))
        monkeypatch.setattr(bridge, "ask", asked)
        placed, err, info = _gate(_cube(tmp_path / "part.stl"), "back-left")
        assert err is None
        assert asked.asked[0]["placement"] == "auto"
        assert asked.asked[1]["placement"] == [30.0, 170.0] and asked.asked[1]["placed_by"] == "human"
        bbox = compute_mesh_bbox(placed)
        assert (bbox["x_min"], bbox["y_min"]) == pytest.approx((30.0, 170.0))
        assert info["placement"]["placed_by"] == "human"

    def test_no_spot_in_the_region_names_the_regions_that_have_room(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((self._probe(), None)))
        _placed, err, _info = _gate(_cube(tmp_path / "part.stl"), "front-left")
        assert err["error"]["code"] == "PLACEMENT_NO_ROOM_IN_REGION"
        assert err["error"]["message"] == (
            "There is no safe spot front-left with jar v2 on the plate; there is room front-right, back-left and back-centre."
        )
        assert len(err["spots"]) == 4

    def test_one_region_with_room_reads_singular(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True, spots=[{"at_mm": [100.0, 100.0], "clearance_mm": 3.0}]), None)))
        _placed, err, _info = _gate(_cube(tmp_path / "part.stl"), "back-right")
        assert err["error"]["message"] == "There is no safe spot back-right with jar v2 on the plate; there is room centre."

    def test_no_spots_anywhere_says_so(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        _placed, err, _info = _gate(_cube(tmp_path / "part.stl"), "back-left")
        assert err["error"]["message"] == "There is no safe spot back-left with jar v2 on the plate; there is no safe spot anywhere beside it."

    def test_no_verdict_for_the_probe_fails_closed(self, tmp_path, machine, monkeypatch):
        from kiln import plate_state
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((None, sa.Miss("signed_out"))))
        _placed, err, _info = _gate(_cube(tmp_path / "part.stl"), "centre")
        assert err["error"]["code"] == "PLACEMENT_NO_VERDICT"
        assert err["error"]["message"] == _no_verdict_sentence(plate_state.read(machine), sa.Miss("signed_out"))

    @pytest.mark.parametrize("spelling, cell", [
        ("front-left", (0, 0)), ("Front Left", (0, 0)), ("front_left", (0, 0)), ("front-center", (0, 1)),
        ("front-centre", (0, 1)), ("middle-left", (1, 0)), ("centre", (1, 1)), ("center", (1, 1)),
        ("middle-centre", (1, 1)), ("back-right", (2, 2)), ("BACK-CENTER", (2, 1)),
    ])
    def test_every_spelling_of_the_nine_regions_resolves(self, spelling, cell):
        from kiln.plugins.slicer_tools import PLACEMENT_REGIONS, _parse_placement, _region_cell

        assert _region_cell(spelling) == cell
        assert _parse_placement(spelling) == ("region", cell, None)
        assert len(PLACEMENT_REGIONS) == 9 and PLACEMENT_REGIONS[4] == "centre"

    def test_a_spot_or_keep_or_nothing_parses_and_garbage_is_named(self):
        from kiln.plugins.slicer_tools import _parse_placement

        assert _parse_placement(None) == ("auto", "auto", None)
        assert _parse_placement("auto") == ("auto", "auto", None)
        assert _parse_placement("keep") == ("keep", "keep", None)
        assert _parse_placement([40, 50]) == ("spot", [40.0, 50.0], None)
        assert _parse_placement("[40, 50]") == ("spot", [40.0, 50.0], None)
        kind, value, problem = _parse_placement("somewhere nice")
        assert value is None and "front-left" in problem and "keep" in problem

    def test_an_unparseable_placement_refuses_even_on_a_clear_plate(self, tmp_path, machine):
        mark_clear(machine, "human")
        _placed, err, _info = _gate(_cube(tmp_path / "part.stl"), "somewhere nice")
        assert err["error"]["code"] == "PLACEMENT_INVALID"


class TestTheFailClosedSentence:
    """The fail-closed refusal speaks in the one voice every served door
    speaks in (:mod:`kiln.served_answer`): the plate record's own opening,
    then the shared cause and fix, and a refusal in the server's own words
    appended whole.  Pinned word for word, and linted the way the roster
    lints every served door's sentences."""

    _CODE_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
    _SYSTEM_WORD = re.compile(
        r"\b(API|endpoint|bridge|kiln-pro|kiln_pro|hosted service|HTTP|urlopen|Errno|traceback|exception|"
        r"backoff|envelope|payload)\b", re.IGNORECASE,
    )

    def _state(self, max_z: float | None, file: str = "jar_v2.gcode.3mf") -> PlateState:
        return PlateState(machine="m", status="occupied", source="kiln_started_print", since="2026-09-21T18:12:00",
                          job=PlateJob(file=file, footprint_mm=None, max_z_mm=max_z))

    def test_the_wording_for_each_cause(self, monkeypatch):
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        state = self._state(42.0)
        monkeypatch.setattr(PlateState, "since_clock", lambda self: "18:12")
        head = "The last print, jar v2, is still on the plate (since 18:12, about 42 mm tall). "
        assert _no_verdict_sentence(state, sa.Miss("offline")) == (
            head + "Kiln can't check whether a second part fits safely beside it right now because this computer is "
            "offline, so it won't slice onto this plate. Clear the plate and say so, or reconnect to the internet and try again."
        )
        assert _no_verdict_sentence(state, sa.Miss("signed_out")) == (
            head + "Kiln can't check whether a second part fits safely beside it right now because Kiln is signed out, "
            "so it won't slice onto this plate. Clear the plate and say so, or sign in and try again."
        )
        for reason in (sa.Miss("unanswered"), "unanswered", None, "something else"):
            assert _no_verdict_sentence(state, reason) == (
                head + "Kiln can't check whether a second part fits safely beside it right now because Kiln's clearance "
                "check didn't answer, so it won't slice onto this plate. Clear the plate and say so, or wait a minute and try again."
            )
        refused = sa.Miss("refused", "MACHINE_NOT_PAIRED", "This device has not reported that printer; register it and ask again.")
        assert _no_verdict_sentence(state, refused) == (
            head + "Kiln can't check whether a second part fits safely beside it right now because Kiln's clearance "
            "check said no, so it won't slice onto this plate. This device has not reported that printer; register it "
            "and ask again. Clear the plate and say so."
        )

    def test_an_unknown_height_drops_the_clause_rather_than_printing_none(self, monkeypatch):
        from kiln.plugins.slicer_tools import _no_verdict_sentence, _plate_holds_sentence

        monkeypatch.setattr(PlateState, "since_clock", lambda self: "Sep 20 09:03")
        assert _plate_holds_sentence(self._state(None)) == "The last print, jar v2, is still on the plate (since Sep 20 09:03)."
        assert "None" not in _no_verdict_sentence(self._state(None), sa.Miss("offline"))
        assert _plate_holds_sentence(self._state(18.5)) == "The last print, jar v2, is still on the plate (since Sep 20 09:03, about 18.5 mm tall)."

    def test_every_cause_reads_in_the_one_shape_with_no_code_or_system_word(self, monkeypatch):
        """The roster's own lint, applied to this door: every cause, one
        distinct sentence each, the shape's anchors present, no wire code
        and no system word inside."""
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        monkeypatch.setattr(PlateState, "since_clock", lambda self: "18:12")
        misses = [
            sa.Miss("offline", "SERVER_UNREACHABLE", "no route"),
            sa.Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED", "wall"),
            sa.Miss("unanswered", "SERVER_UNREACHABLE", "timed out"),
            sa.Miss("refused", "MACHINE_NOT_PAIRED", "This device has not reported that printer; register it and ask again."),
            sa.Miss("refused", "WEIRD_CODE", ""),
        ]
        seen = set()
        for miss in misses:
            text = _no_verdict_sentence(self._state(42.0), miss)
            assert text == " ".join(text.split()) and text.endswith(".")
            assert not self._CODE_TOKEN.search(text), text
            assert not self._SYSTEM_WORD.search(text), text
            for anchor in ("Kiln can't", "right now because", ", so it"):
                assert anchor in text, text
            seen.add(text)
        assert len(seen) == len(misses)

    def test_the_job_name_is_prettified_the_same_way_everywhere(self):
        """One prettifier in public Kiln, on the record; its list matches
        kiln-pro's own (``.gcode.3mf``, ``.3mf``, ``.gcode``, ``.stl``,
        ``.obj``, ``.step``; underscores and dashes to spaces)."""
        from kiln.plate_state import pretty_job_name
        from kiln.plugins.slicer_tools import _pretty_job_name

        assert _pretty_job_name is pretty_job_name
        assert pretty_job_name("jar_v2.gcode.3mf") == "jar v2"
        assert pretty_job_name("/tmp/x/Phone-Stand_final.3mf") == "Phone Stand final"
        assert pretty_job_name("coaster.gcode") == "coaster"
        for ext in (".stl", ".obj", ".step"):
            assert pretty_job_name(f"bracket_v3{ext}") == "bracket v3"
        assert pretty_job_name("bracket.stp") == "bracket.stp", "not on the shared list, so not stripped"
        assert pretty_job_name("") == "the last part"


class TestKeepAndFormats:
    def test_keep_asks_with_the_parts_own_origin_and_slices_the_file_itself(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        asked = _Bridge((_verdict(ok=True, at=(100.0, 100.0), placed_by="keep"), None))
        monkeypatch.setattr(bridge, "ask", asked)
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        placed, err, info = _gate(stl, "keep")
        assert err is None and placed == stl
        assert asked.asked[0]["placement"] == "keep" and asked.asked[0]["placed_by"] == "keep"
        assert asked.asked[0]["keep_at_mm"] == [100.0, 100.0]
        assert info["approval_carries"] is False

    def test_a_3mf_is_placed_by_its_build_items(self, tmp_path, machine, monkeypatch):
        import zipfile

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True, at=(40.0, 40.0)), None)))
        core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
        xml = (
            f'<?xml version="1.0"?><model unit="millimeter" xmlns="{core}"><resources>'
            '<object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
            '<vertex x="5" y="10" z="8"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
            '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 100 100 0"/></build></model>'
        )
        src = tmp_path / "part.3mf"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("3D/3dmodel.model", xml)
        placed, err, _info = _gate(str(src), [40, 40])
        assert err is None and placed.endswith("_placed.3mf")
        bbox = compute_mesh_bbox(placed)
        assert (bbox["x_min"], bbox["y_min"]) == pytest.approx((40.0, 40.0))

    def test_a_format_kiln_cannot_move_is_refused_in_a_sentence(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        obj = tmp_path / "part.obj"
        obj.write_text("v 0 0 0\nv 10 0 0\nv 5 10 8\nf 1 2 3\n")
        _placed, err, _info = _gate(str(obj), [40, 40])
        assert err["error"]["code"] == "PLACEMENT_UNPLACEABLE"
        if compute_mesh_bbox(str(obj)):
            assert "STL or a 3MF" in err["error"]["message"] and ".obj" in err["error"]["message"]

    def test_an_unrecorded_plate_passes_through_open_by_design(self, tmp_path, machine, monkeypatch):
        """``plate: unknown`` fails OPEN at the door.  Defensible, and said so
        in the gate's docstring: the record is written only by Kiln's own
        starts and print-ended hook, so no record means Kiln never put a part
        there -- not that one might be -- and a person's own manual print is
        theirs to clear.  (An OCCUPIED plate with no verdict fails closed;
        that is the other test.)"""
        from kiln import plate_state
        from kiln.plugins.slicer_tools import _apply_plate_placement

        assert plate_state.read(machine).status == "unknown", "no record on file for this machine"
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("an unrecorded plate asks nobody"))
        stl = _cube(tmp_path / "part.stl")
        placed, err, info = _gate(stl, None)
        assert err is None and placed == stl and info == {"plate": "unknown"}
        placed, err, info = _gate(stl, [40, 40])
        assert err is None and placed == stl and info == {"plate": "unknown"}, "even a named spot is not applied to a plate Kiln has no record of"
        assert "fails OPEN" in _apply_plate_placement.__doc__ and "written only by Kiln's own starts" in _apply_plate_placement.__doc__

    def test_the_hosted_process_never_reads_its_own_plate_as_yours(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr("kiln.runtime_env.is_hosted_multitenant", lambda: True)
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("hosted asks nobody"))
        stl = _cube(tmp_path / "part.stl")
        placed, err, info = _gate(stl, None)
        assert err is None and placed == stl and info == {"plate": "unknown", "gate": "skipped_hosted"}

    def test_the_part_envelope_reads_the_profile_when_it_is_trivially_readable(self, tmp_path):
        from kiln.plugins.slicer_tools import _part_envelope

        stl = _cube(tmp_path / "part.stl")
        ini = tmp_path / "p.ini"
        ini.write_text("layer_height = 0.28\nskirts = 2\nskirt_distance = 4\nbrim_width = 8\n")
        part, bbox = _part_envelope(stl, str(ini))
        assert part == {"size_mm": [20.0, 20.0, 20.0], "layer_height_mm": 0.28, "tower_mm": None, "colour_changes_at_mm": [], "skirt_mm": 8.0}
        assert bbox["x_min"] == 0.0
        part, _ = _part_envelope(stl, None)
        assert part["layer_height_mm"] == 0.2 and part["skirt_mm"] == 6.0, "the slicer's own defaults when the profile is silent"
        assert _part_envelope(str(tmp_path / "missing.stl"), None) == (None, None)


# ---------------------------------------------------------------------------
# The stage shows the plate as it is
# ---------------------------------------------------------------------------


class TestTheStage:
    def _real_cube(self, path):
        trimesh = pytest.importorskip("trimesh")
        trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(str(path))
        return str(path)

    def test_a_mesh_on_an_occupied_plate_carries_the_record_box(self, tmp_path, monkeypatch):
        from kiln import local_stage

        m = _machine()
        _occupy(m)
        monkeypatch.setattr("kiln.server._get_adapter", lambda: m)
        monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model", lambda: "bambu_a1")
        payload = local_stage._payload_for_mesh(self._real_cube(tmp_path / "cube.stl"))
        assert payload["occupancy"] == {
            "kind": "kiln.plate_occupancy.v1",
            "bed_mm": [256.0, 256.0],
            "occupied": [{"name": "jar v2", "rect_mm": [90.0, 90.0, 160.0, 160.0], "top_mm": 42.0}],
            "proposed": None,
            "source": "record_box",
        }
        assert payload["plate"]["printer_id"] == "bambu_a1"

    def test_a_clear_plate_carries_none(self, tmp_path, monkeypatch):
        from kiln import local_stage

        m = _machine()
        mark_clear(m, "human")
        monkeypatch.setattr("kiln.server._get_adapter", lambda: m)
        payload = local_stage._payload_for_mesh(self._real_cube(tmp_path / "cube.stl"))
        assert "occupancy" not in payload

    def test_a_door_with_a_verdict_passes_its_block_through(self, monkeypatch):
        from kiln import stage_plate

        m = _machine()
        _occupy(m)
        monkeypatch.setattr("kiln.server._get_adapter", lambda: m)
        block = _verdict()["occupancy"]
        payload = stage_plate.attach_stage_plate({"kind": "kiln.mesh.v1"}, "bambu_a1", occupancy=block)
        assert payload["occupancy"] is block

    def test_the_hosted_process_shows_nobodys_plate(self, monkeypatch):
        from kiln import stage_plate

        m = _machine()
        _occupy(m)
        monkeypatch.setattr("kiln.server._get_adapter", lambda: m)
        monkeypatch.setattr("kiln.runtime_env.is_hosted_multitenant", lambda: True)
        assert "occupancy" not in stage_plate.attach_stage_plate({"kind": "kiln.mesh.v1"}, "bambu_a1")

    def test_the_record_box_block_from_the_record(self):
        state = PlateState(machine="m", status="occupied", job=PlateJob(file="/x/jar.3mf", footprint_mm=None, max_z_mm=None))
        assert state.occupancy(None) == {
            "kind": "kiln.plate_occupancy.v1", "bed_mm": None,
            "occupied": [{"name": "jar", "rect_mm": None, "top_mm": None}],
            "proposed": None, "source": "record_box",
        }
        assert PlateState(machine="m", status="clear").occupancy([256, 256]) is None


# ---------------------------------------------------------------------------
# Every caller of the slicer
# ---------------------------------------------------------------------------

#: Source files (relative to the kiln package) that call ``slice_file`` and
#: are walked behaviourally below.  The structural test DERIVES the caller
#: set from the source, so a new caller fails until it is wired through the
#: gate AND named here -- a door nobody walks is the one the bug survives in.
_WALKED = {
    "plugins/slicer_tools.py",
    "plugins/estimate_tools.py",
    "pipelines.py",
    "cli/main.py",
    "plugins/smart_print_tools.py",
    "plugins/generation_ai_tools.py",
    "design_reasoning.py",
    "design_rebuild.py",
}


#: ``kiln.slicer``'s entry points: everything that ends in a slice.  A call
#: to any of these outside the slicer module is a door reaching the slicer.
_SLICER_ENTRIES = frozenset({"slice_file", "estimate_print", "slice_multicolor_copies"})
#: The shared step and its wrappers -- a function that calls one of these
#: reaches the slicer through the gate by construction.
_SHARED_STEPS = frozenset({"_placed_slice", "_slice_step", "_cli_placed_slice"})
_PLATE_GATES = ("_apply_plate_placement", "_verify_plate_placement")


def _called_names(fn: Any) -> set[str]:
    """Every name a function's body calls, plain or as an attribute --
    except an attribute on something that is not the slicer module, so an
    estimator's own ``estimate_print`` method is not mistaken for ours."""
    import ast

    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            base = target.value
            base_name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
            if target.attr in _SLICER_ENTRIES and base_name not in ("slicer", "kiln"):
                continue
            names.add(target.attr)
    return names


def _raw_entry_calls(fn: Any) -> set[str]:
    """The slicer entries a function calls ITSELF.  A lambda handed to a
    shared step as its ``slicer=`` is that step's slicer, and slices through
    the gate by construction; an entry called anywhere else is raw."""
    import ast

    inside_gated_lambda: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SHARED_STEPS:
            for kw in node.keywords:
                if kw.arg == "slicer" and isinstance(kw.value, ast.Lambda):
                    inside_gated_lambda.update(id(n) for n in ast.walk(kw.value))
    names: set[str] = set()
    for node in ast.walk(fn):
        if id(node) in inside_gated_lambda or not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            base = target.value
            base_name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
            name = target.attr if base_name in ("slicer", "kiln") else ""
        else:
            continue
        if name in _SLICER_ENTRIES:
            names.add(name)
    return names


def _functions(root: Path):
    """``(relative path, function name, ast node)`` for every function in the package."""
    import ast

    for py in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield py.relative_to(root).as_posix(), node.name, node


def _slicer_reachers() -> tuple[dict[tuple[str, str], bool], set[str]]:
    """``({(file, function): gated} for every function that calls a slicer
    entry directly, {file for every module that reaches the slicer at all})``
    -- derived from the source, so a new caller cannot hide."""
    import kiln

    root = Path(kiln.__file__).parent
    raw: dict[tuple[str, str], bool] = {}
    modules: set[str] = set()
    for rel, name, node in _functions(root):
        if rel == "slicer.py":
            continue
        called = _called_names(node)
        if _raw_entry_calls(node):
            raw[(rel, name)] = all(gate in called for gate in _PLATE_GATES)
            modules.add(rel)
        if called & _SHARED_STEPS:
            modules.add(rel)
    return raw, modules


class TestEveryCallerOfTheSlicer:
    """The one-door fallacy, closed: every function that reaches the slicer
    -- ``slice_file``, ``estimate_print`` or ``slice_multicolor_copies`` --
    does so through the plate gate and the second verdict, and every module
    that does is walked behaviourally here."""

    def test_every_slicer_entry_is_reached_through_the_gate_and_walked(self):
        raw, modules = _slicer_reachers()
        assert ("plugins/slicer_tools.py", "_placed_slice") in raw, "the walk itself is broken"
        # A function that slices directly must gate and verify in that same
        # function; a function that slices through the shared step needs
        # nothing more.  Nested functions count for their enclosing one.
        ungated = sorted(f"{rel}:{name}" for (rel, name), gated in raw.items() if not gated)
        assert not ungated, (
            f"these functions reach the slicer without the plate gate and the post-slice verdict: "
            f"{ungated}.  Slice through _placed_slice (one helper, no per-door branch), or call "
            f"_apply_plate_placement and _verify_plate_placement in the same function, and walk the door here."
        )
        assert modules == _WALKED, (
            f"the modules that reach the slicer changed: {sorted(modules ^ _WALKED)}.  A new one must be "
            f"gated AND walked behaviourally here; a removed one comes off _WALKED."
        )

    def test_only_the_estimate_helper_may_switch_the_plate_gate_off(self):
        """The plate gate's one exemption is a door whose output is never
        printed.  Written down in prose it would be borrowed by the next door
        that finds the gate inconvenient; this fails the moment anything but
        the estimate helper passes ``plate_gate`` to the shared step."""
        import ast

        import kiln

        root = Path(kiln.__file__).parent
        callers: set[str] = set()
        for rel, name, node in _functions(root):
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and any(kw.arg == "plate_gate" for kw in call.keywords):
                    callers.add(f"{rel}:{name}")
        assert callers == {"plugins/estimate_tools.py:_estimate_slice"}, (
            f"the plate gate is switched off from {sorted(callers)}.  Only an estimate -- whose output is "
            f"never started onto a plate -- may do that, through _estimate_slice."
        )

    def test_an_estimate_still_refuses_a_part_that_does_not_fit_the_bed(self, tmp_path, machine, monkeypatch):
        """The plate stops refusing an estimate; the bed does not.  A part
        too big for the machine has no honest estimate on any plate."""
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        big = _cube(tmp_path / "big.stl", size=400.0)
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _estimate_tools()["slice_and_estimate"](input_path=big, printer_id="ender3")
        assert resp["success"] is False
        assert resp["error"]["code"] == "EXCEEDS_BED"
        assert "plate_note" not in resp
        assert not spy.called

    def test_the_walk_sees_the_slicers_public_wrappers_as_entries(self):
        """``estimate_print`` and ``slice_multicolor_copies`` slice too; a
        door that reaches them raw would go red here today."""
        import ast

        for src, expect in (
            ("def f():\n    from kiln.slicer import estimate_print\n    return estimate_print('a')\n", True),
            ("def f():\n    from kiln import slicer\n    return slicer.slice_multicolor_copies('a', 2)\n", True),
            ("def f():\n    return get_estimator().estimate_print(x)\n", False),
            ("def f():\n    return estimator.estimate_print(x)\n", False),
            # A lambda handed to the shared step as its slicer IS the gated path...
            ("def f():\n    return _placed_slice(p, slicer=lambda path, **kw: slice_multicolor_copies(path, 2, **kw))\n", False),
            # ...a lambda anywhere else is still a raw entry.
            ("def f():\n    run(lambda path: slice_file(path))\n", True),
        ):
            fn = ast.parse(src).body[0]
            assert bool(_raw_entry_calls(fn)) is expect, src

    def test_the_pipelines_refuse_at_the_slice_step_and_stop(self, tmp_path, machine, monkeypatch):
        from kiln import pipelines

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        monkeypatch.setattr(pipelines, "_resolve_pipeline_adapter", lambda name: machine)
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        runs = (
            (pipelines.quick_print, {}),
            (pipelines.reslice_and_print, {"overrides": {"brim_width": "5"}}),
            (pipelines.benchmark, {}),
        )
        with patch("kiln.slicer.slice_file", spy):
            for run, kwargs in runs:
                result = run(model_path=stl, printer_id="ender3", skip_validation=True, **kwargs)
                step = next(s for s in result.steps if s.name == "slice")
                assert result.success is False and step.success is False, run.__name__
                assert step.message.startswith("The last print, jar v2, is still on the plate (since "), run.__name__
                assert step.data["error"]["code"] == "PLACEMENT_PLATE_OCCUPIED"
                assert step.data["occupancy"]["kind"] == bridge.OCCUPANCY_KIND
                assert [s.name for s in result.steps][-1] == "slice", "a refused slice is fatal: nothing runs after it"
        assert not spy.called

    def test_the_pipelines_place_and_carry_the_verdict(self, tmp_path, machine, monkeypatch):
        from kiln import pipelines

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        monkeypatch.setattr(pipelines, "_resolve_pipeline_adapter", lambda name: machine)
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            result = pipelines.quick_print(model_path=stl, printer_id="ender3", skip_validation=True, placement=[40, 40])
        step = next(s for s in result.steps if s.name == "slice")
        assert step.success is True
        assert step.data["placement"]["ok"] is True and step.data["approval_carries"] is False
        bbox = compute_mesh_bbox(spy.call_args.args[0])
        assert (bbox["x_min"], bbox["y_min"]) == pytest.approx((40.0, 40.0))

    def test_the_run_quick_print_and_run_reslice_tools_take_placement(self):
        import kiln.server as srv

        for tool in (srv.run_quick_print, srv.run_reslice_and_print, srv.design_to_gcode_pipeline):
            assert inspect.signature(tool).parameters["placement"].default is None, tool.__name__

    def test_kiln_slice_refuses_in_json_and_rich_and_exits_non_zero(self, tmp_path, machine, monkeypatch):
        import json

        from click.testing import CliRunner

        from kiln.cli.main import cli

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: machine)
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        spy, _ = _fake_slice(tmp_path)
        runner = CliRunner()
        with patch("kiln.slicer.slice_file", spy):
            res = runner.invoke(cli, ["slice", stl, "--printer-id", "ender3", "--json"])
            assert res.exit_code == 1, res.output
            data = json.loads(res.output)
            assert data["status"] == "error" and data["error"]["code"] == "PLACEMENT_PLATE_OCCUPIED"
            assert data["error"]["message"].startswith("The last print, jar v2, is still on the plate (since ")
            res = runner.invoke(cli, ["slice", stl, "--printer-id", "ender3"])
            assert res.exit_code == 1
            assert "The last print, jar v2, is still on the plate" in res.output
        assert not spy.called

        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        with patch("kiln.slicer.slice_file", spy):
            res = runner.invoke(cli, ["slice", stl, "--printer-id", "ender3", "--placement", "[40, 40]", "--json"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["data"]["placement"]["ok"] is True and data["data"]["approval_carries"] is False
        assert spy.call_args.args[0].endswith("_placed.stl")
        with patch("kiln.slicer.slice_file", spy):
            res = runner.invoke(cli, ["slice", stl, "--printer-id", "ender3", "--placement", "front-left"])
        assert res.exit_code == 0, res.output
        assert "Placement: placed beside jar v2" in res.output

    def test_kiln_generate_and_print_refuses_before_slicing(self, tmp_path, machine, monkeypatch):
        """The generated model lands on disk, then meets the same gate as
        kiln slice: an occupied plate refuses before the slicer runs."""
        import json

        from click.testing import CliRunner

        from kiln.cli.main import cli
        from kiln.generation import GenerationStatus
        from tests.test_generation_cli import _make_job, _make_result, _make_validation

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: machine)
        stl = _cube(tmp_path / "model.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.generation.OpenSCADProvider") as provider_cls, \
                patch("kiln.generation.validate_mesh", return_value=_make_validation(valid=True)), \
                patch("kiln.slicer.slice_file", spy):
            provider = provider_cls.return_value
            provider.display_name = "OpenSCAD"
            provider.generate.return_value = _make_job(provider="openscad", prompt="cube([10,10,10]);", status=GenerationStatus.SUCCEEDED, progress=100)
            provider.download_result.return_value = _make_result(provider="openscad", prompt="cube([10,10,10]);", local_path=stl)
            res = CliRunner().invoke(cli, [
                "generate-and-print", "cube([10,10,10]);", "--provider", "openscad",
                "--printer-id", "ender3", "--no-preview", "--json",
            ])
        assert res.exit_code == 1, res.output
        data = json.loads(res.output)
        assert data["status"] == "error" and data["error"]["code"] == "PLACEMENT_PLATE_OCCUPIED"
        assert data["error"]["message"].startswith("The last print, jar v2, is still on the plate (since ")
        assert not spy.called

    def test_kiln_slice_multicolor_copies_take_the_same_gate(self, tmp_path, machine, monkeypatch):
        """The multi-colour path slices through its own wrapper; it is a
        slicer entry like any other and meets the gate before it runs."""
        import json

        from click.testing import CliRunner

        from kiln.cli.main import cli

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: machine)
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_multicolor_copies", spy), patch("kiln.slicer.slice_file", spy):
            res = CliRunner().invoke(cli, ["slice", stl, "--printer-id", "ender3", "--copies", "2", "--ams-mapping", "0,1", "--json"])
        assert res.exit_code == 1, res.output
        assert json.loads(res.output)["error"]["code"] == "PLACEMENT_PLATE_OCCUPIED"
        assert not spy.called

    def test_estimate_print_time_takes_the_gate_and_says_no_start(self, tmp_path, machine, monkeypatch):
        """The estimate slices too, through the shared step: an occupied plate
        with no safe spot named still gets an answer -- the part on an empty
        plate, said so -- and a placed estimate says the part must not be
        started there by hand."""
        tools = _estimate_tools()
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = tools["estimate_print_time"](file_path=stl, printer_id="ender3")
        _assert_estimates_the_part_on_an_empty_plate(resp, spy)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        with patch("kiln.slicer.slice_file", spy):
            resp = tools["estimate_print_time"](file_path=stl, printer_id="ender3", placement=[40, 40])
        assert resp["success"] is True and resp["placement"]["ok"] is True
        assert resp["start"]["allowed"] is False
        assert spy.call_args.args[0].endswith("_placed.stl")

    def test_retry_print_with_fix_refuses_before_slicing(self, tmp_path, machine, monkeypatch):
        from kiln.plugins.smart_print_tools import _SmartPrintToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self, name: str | None = None, **_kwargs):
                def decorator(fn):
                    tools[name or fn.__name__] = fn
                    return fn

                return decorator

        _SmartPrintToolsPlugin().register(_FakeMcp())
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = tools["retry_print_with_fix"](model_path=stl, printer_id="ender3", skip_diagnosis=True, skip_validation=True)
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_PLATE_OCCUPIED"
        assert resp["error"]["message"].startswith("The last print, jar v2, is still on the plate (since ")
        assert not spy.called
        assert inspect.signature(tools["retry_print_with_fix"]).parameters["placement"].default is None

    def test_generate_and_print_refuses_before_slicing(self, tmp_path, machine, monkeypatch):
        from kiln.generation import GenerationStatus
        from kiln.server import generate_and_print
        from tests.test_generation_server import _make_job, _make_result, _make_validation

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        monkeypatch.setattr("kiln.server._get_adapter", lambda: machine)
        stl = _cube(tmp_path / "model.stl")
        provider = MagicMock()
        provider.generate.return_value = _make_job(status=GenerationStatus.SUCCEEDED)
        provider.download_result.return_value = _make_result(local_path=stl)
        spy, _ = _fake_slice(tmp_path)
        pipeline = {"ready_to_print": True, "printability_score": 92, "validated_path": stl, "summary": "ok",
                    "next_action": None, "repaired": False, "model_info": {"dimensions_mm": {"x": 20.0, "y": 20.0, "z": 20.0}},
                    "checks": [], "status": "pass"}
        with patch("kiln.server._get_generation_provider", return_value=provider), \
                patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline", return_value=pipeline), \
                patch("kiln.generation.validate_mesh", return_value=_make_validation(valid=True)), \
                patch("kiln.slicer.slice_file", spy):
            resp = generate_and_print("a cube", provider="meshy", printer_id="ender3")
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_PLATE_OCCUPIED", resp
        assert not spy.called
        assert inspect.signature(generate_and_print).parameters["placement"].default is None

    def test_design_to_gcode_refuses_the_slice_step_in_its_errors(self, tmp_path, machine, monkeypatch):
        from kiln import design_reasoning

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        spy, _ = _fake_slice(tmp_path)

        def fake_compile(_code, output_path):
            _cube(Path(output_path))

        with patch("kiln.slicer.slice_file", spy), patch("kiln.parametric.compile_scad_code", side_effect=fake_compile):
            result = design_reasoning.design_to_gcode("a simple coaster", output_dir=str(tmp_path / "d"), material="ASA")
        assert result.gcode_file == "" and "slicing" not in result.steps_completed
        assert any(e.startswith("The last print, jar v2, is still on the plate (since ") for e in result.errors), result.errors
        assert result.to_dict()["placement"]["ok"] is False
        assert not spy.called

    def test_design_rebuild_refuses_the_slice_with_the_sentence(self, tmp_path, machine, monkeypatch):
        from kiln.design_rebuild import slice_stl

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False, spots=[]), None)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy), pytest.raises(RuntimeError, match=r"^The last print, jar v2, is still on the plate"):
            slice_stl(stl, None)
        assert not spy.called
        mark_clear(machine, "human")
        with patch("kiln.slicer.slice_file", spy):
            assert slice_stl(stl, None).endswith("out.gcode")


# ---------------------------------------------------------------------------
# Every door that starts a print
# ---------------------------------------------------------------------------

#: The names a function may call to take the start gate: the gate itself,
#: the pipelines' step wrapper, the CLI's exit-non-zero wrapper.
_START_GATES = frozenset({"start_refusal", "_start_refused_step", "_cli_start_refusal"})
#: Doors that start prints from a QUEUE, named here with the reason they
#: are not gated: the ingest watcher and the scheduler are batch doors,
#: out of this change's scope, and an unnamed exemption would be a hole.
_QUEUE_DOORS = {
    ("cli/main.py", "_dispatch_pending"): "the ingest watcher's queue — a batch door, out of scope",
    ("cli/main.py", "ingest_watch_cmd"): "encloses _dispatch_pending — the same batch door",
    ("scheduler.py", "tick"): "the print queue — a batch door, out of scope",
}


def _start_callers() -> dict[tuple[str, str], bool]:
    """``{(file, function): takes the start gate}`` for every function whose
    body calls ``<something>.start_print(`` -- the adapter's method, the one
    call that reaches a printer.  A plain ``start_print(...)`` is the gated
    TOOL delegating, and is not counted.  Derived from the source."""
    import ast

    import kiln

    root = Path(kiln.__file__).parent
    out: dict[tuple[str, str], bool] = {}
    for rel, name, node in _functions(root):
        if rel.startswith("printers/"):
            continue
        starts = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "start_print"
            for n in ast.walk(node)
        )
        if starts:
            out[(rel, name)] = bool(_called_names(node) & _START_GATES)
    return out


class TestEveryDoorThatStartsAPrint:
    """A plate that still holds the last print is never started onto: the
    file carries the maker's own start sequence, which drives the head
    across the plate at a few millimetres.  Every door that reaches
    ``adapter.start_print`` takes the one gate first, and says so in one
    sentence with the code ``PLATE_OCCUPIED_START_NOT_YET``."""

    def test_every_function_that_starts_a_print_takes_the_gate(self):
        callers = _start_callers()
        assert ("server.py", "start_print") in callers, "the walk itself is broken"
        ungated = sorted(f"{rel}:{name}" for (rel, name), gated in callers.items() if not gated and (rel, name) not in _QUEUE_DOORS)
        assert not ungated, (
            f"these functions call adapter.start_print without kiln.plate_state.start_refusal first: "
            f"{ungated}.  Gate them, or name them in _QUEUE_DOORS with the reason."
        )
        stale = sorted(f"{rel}:{name}" for (rel, name) in _QUEUE_DOORS if (rel, name) not in callers)
        assert not stale, f"exemptions for doors that no longer start a print: {stale}"

    def test_the_gate_itself(self, machine, monkeypatch):
        from kiln.plate_state import START_NOT_YET_CODE, start_refusal

        assert start_refusal(machine) is None, "an unrecorded plate passes"
        mark_clear(machine, "human")
        assert start_refusal(machine) is None
        _occupy(machine)
        block = start_refusal(machine)
        assert block["success"] is False and block["error"]["code"] == START_NOT_YET_CODE == "PLATE_OCCUPIED_START_NOT_YET"
        assert block["error"]["retryable"] is False
        monkeypatch.setattr(PlateState, "since_clock", lambda self: "18:12")
        assert start_refusal(machine)["error"]["message"] == (
            "The last print, jar v2, is still on the plate (since 18:12, about 42 mm tall). "
            "Kiln can't start a print onto an occupied plate yet — the printer's own start sequence "
            "drives the head across it — so it won't start this one. Clear the plate and say so."
        )
        assert block["plate"]["status"] == "occupied" and block["occupancy"]["kind"] == bridge.OCCUPANCY_KIND
        assert start_refusal(machine, resume=True) is None, "a resume is the same job, still on the plate"

    def test_start_print_refuses_and_a_resume_passes_the_gate(self, machine, monkeypatch):
        import kiln.server as srv
        from kiln import plate_state

        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (machine, "default"))
        monkeypatch.setattr(srv, "_TOOL_RATE_LIMITS", {})  # two starts in one second, on purpose
        _occupy(machine)
        resp = srv.start_print(file_name="part.gcode")
        assert resp["success"] is False and resp["error"]["code"] == "PLATE_OCCUPIED_START_NOT_YET"
        seen: list = []
        monkeypatch.setattr(
            plate_state, "start_refusal",
            lambda adapter, resume=False, file_name=None, local_path=None: seen.append((resume, file_name)) or None,
        )
        srv.start_print(file_name="part.gcode", resume_from_paused=True)
        assert seen == [(True, "part.gcode")], "the tool tells the gate a resume is a resume, and names the file"

    def test_start_monitored_print_refuses(self, machine, monkeypatch):
        from kiln.plugins import monitoring_tools

        tools: dict[str, Any] = {}

        class _Registrar:
            def tool(self, *args: Any, **kwargs: Any):
                def decorator(fn):
                    tools[kwargs.get("name") or fn.__name__] = fn
                    return fn

                return decorator

        monitoring_tools._MonitoringToolsPlugin().register(_Registrar())
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        _occupy(machine)
        resp = tools["start_monitored_print"](file_name="part.gcode")
        assert resp["success"] is False and resp["error"]["code"] == "PLATE_OCCUPIED_START_NOT_YET"

    def test_the_pipelines_refuse_at_the_start_step_after_a_placed_slice(self, tmp_path, machine, monkeypatch):
        from kiln import pipelines

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        monkeypatch.setattr(pipelines, "_resolve_pipeline_adapter", lambda name: machine)
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            for run, kwargs in ((pipelines.quick_print, {}), (pipelines.reslice_and_print, {"overrides": {"brim_width": "5"}})):
                result = run(model_path=stl, printer_id="ender3", skip_validation=True, placement=[40, 40], **kwargs)
                names = [s.name for s in result.steps]
                assert names[-1] == "start_print", (run.__name__, names, [s.message for s in result.steps])
                slice_step = next(s for s in result.steps if s.name == "slice")
                assert slice_step.success is True and slice_step.data["approval_carries"] is False
                start = result.steps[-1]
                assert result.success is False and start.success is False
                assert start.data["error"]["code"] == "PLATE_OCCUPIED_START_NOT_YET"
                assert start.message.startswith("The last print, jar v2, is still on the plate (since ")

    def test_retry_print_with_fix_refuses_the_start_after_a_placed_slice(self, tmp_path, machine, monkeypatch):
        from kiln.plugins.smart_print_tools import _SmartPrintToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self, name: str | None = None, **_kwargs):
                def decorator(fn):
                    tools[name or fn.__name__] = fn
                    return fn

                return decorator

        _SmartPrintToolsPlugin().register(_FakeMcp())
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        spy, gcode = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = tools["retry_print_with_fix"](model_path=stl, printer_id="ender3", skip_diagnosis=True, skip_validation=True, placement=[40, 40])
        assert resp["success"] is False and resp["error"]["code"] == "PLATE_OCCUPIED_START_NOT_YET"
        assert resp["slice"]["output_path"] == gcode and resp["placement"]["ok"] is True
        assert resp["approval_carries"] is False

    def test_generate_and_print_refuses_the_auto_start_and_keeps_the_upload(self, tmp_path, machine, monkeypatch):
        import kiln.server as srv
        from kiln.generation import GenerationStatus
        from kiln.server import generate_and_print
        from tests.test_generation_server import _make_job, _make_result, _make_validation

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        monkeypatch.setattr(srv, "_get_adapter", lambda: machine)
        monkeypatch.setattr(srv, "_AUTO_PRINT_GENERATED", True)
        stl = _cube(tmp_path / "model.stl", off=(100.0, 100.0, 0.0))
        provider = MagicMock()
        provider.generate.return_value = _make_job(status=GenerationStatus.SUCCEEDED)
        provider.download_result.return_value = _make_result(local_path=stl)
        spy, gcode = _fake_slice(tmp_path)
        pipeline = {"ready_to_print": True, "printability_score": 92, "validated_path": stl, "summary": "ok",
                    "next_action": None, "repaired": False, "model_info": {"dimensions_mm": {"x": 20.0, "y": 20.0, "z": 20.0}},
                    "checks": [], "status": "pass"}
        with patch("kiln.server._get_generation_provider", return_value=provider), \
                patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline", return_value=pipeline), \
                patch("kiln.generation.validate_mesh", return_value=_make_validation(valid=True)), \
                patch("kiln.slicer.slice_file", spy):
            resp = generate_and_print("a cube", provider="meshy", printer_id="ender3", placement=[40, 40])
        assert resp["success"] is False and resp["error"]["code"] == "PLATE_OCCUPIED_START_NOT_YET", resp
        assert resp["upload"]["file_name"] == os.path.basename(gcode) and resp["slice"]["output_path"] == gcode
        assert resp["placement"]["ok"] is True

    def test_kiln_print_and_kiln_slice_print_after_refuse_and_exit_non_zero(self, tmp_path, machine, monkeypatch):
        import json

        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: machine)
        _occupy(machine)
        runner = CliRunner()
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\nG1 X10 Y10 Z0.2 E1\n")
        res = runner.invoke(cli, ["print", str(gcode), "--skip-preflight", "--json"])
        assert res.exit_code == 1, res.output
        assert "PLATE_OCCUPIED_START_NOT_YET" in res.output and "won't start this one" in res.output

        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=True), None)))
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            res = runner.invoke(cli, ["slice", stl, "--printer-id", "ender3", "--placement", "[40, 40]", "--print-after", "--json"])
        assert res.exit_code == 1, res.output
        data = json.loads(res.output)
        assert data["error"]["code"] == "PLATE_OCCUPIED_START_NOT_YET"
        assert spy.called, "sliced, then refused at the start"

    def test_download_and_upload_and_the_cli_auto_print_are_pinned_structurally(self):
        """Both reach adapter.start_print only behind a standing opt-in and a
        marketplace download; the AST pin above holds them to the gate, and
        this names them so the omission from the walk is not silent."""
        callers = _start_callers()
        assert callers[("server.py", "download_and_upload")] is True
        assert callers[("cli/main.py", "generate_and_print_cmd")] is True
