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
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln import _pro_placement_bridge as bridge
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
    return SimpleNamespace(name="bambu", serial="01P00A000000001", _printer_model="bambu_a1")


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
                      "reserved": [{"name": "purge line", "rect_mm": [0, 0, 240, 5], "why": "the start sequence draws here"}],
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
            assert "stop here" in resp["error"]["message"], "stopped at the upload stub, past the slice"
            return
        assert resp["success"] is True
        assert resp["placement"]["ok"] is True and resp["placement"]["schema"] == bridge.SCHEMA
        assert resp["approval_carries"] is False
        assert "jar v2" in resp["approval_note"] and "approve from here" in resp["approval_note"]
        stage = resp.get("stage_mesh_path") or resp.get("slice", {}).get("stage_mesh_path")
        assert stage in (None, sliced), "the stage shows the placed copy when the result names a mesh"

    def test_a_refusing_verdict_refuses_with_its_sentence(self, door, registry, extra, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_verdict(ok=False), None)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl, placement=[100, 100])
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_REFUSED"
        assert "the head would clip the jar on its way down." in resp["error"]["message"]
        assert "Spots with room: [40, 40] (12 mm clear), [40, 200] (30 mm clear)." in resp["error"]["message"]
        assert resp["placement"]["ok"] is False and len(resp["spots"]) == 2
        assert not spy.called

    @pytest.mark.parametrize("reason", [bridge.OFFLINE, bridge.SIGNED_OUT, bridge.NOT_ANSWERED])
    def test_no_verdict_fails_closed(self, door, registry, extra, tmp_path, machine, monkeypatch, reason):
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((None, reason)))
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            resp = _call(door, registry, extra, input_path=stl, placement=[40, 40])
        assert resp["success"] is False and resp["error"]["code"] == "PLACEMENT_NO_VERDICT"
        from kiln import plate_state

        assert resp["error"]["message"] == _no_verdict_sentence(plate_state.read(machine), reason)
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
        monkeypatch.setattr(bridge, "ask", _Bridge((None, bridge.SIGNED_OUT)))
        _placed, err, _info = _gate(_cube(tmp_path / "part.stl"), "centre")
        assert err["error"]["code"] == "PLACEMENT_NO_VERDICT"
        assert err["error"]["message"] == _no_verdict_sentence(plate_state.read(machine), bridge.SIGNED_OUT)

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
    def _state(self, max_z: float | None, file: str = "jar_v2.gcode.3mf") -> PlateState:
        return PlateState(machine="m", status="occupied", source="kiln_started_print", since="2026-09-21T18:12:00",
                          job=PlateJob(file=file, footprint_mm=None, max_z_mm=max_z))

    def test_the_wording_for_each_cause(self, monkeypatch):
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        state = self._state(42.0)
        monkeypatch.setattr(PlateState, "since_clock", lambda self: "18:12")
        head = "The last print, jar v2, is still on the plate (since 18:12, about 42 mm tall). "
        assert _no_verdict_sentence(state, bridge.OFFLINE) == (
            head + "Kiln can't check whether a second part fits safely beside it because this computer is offline, "
            "so it won't slice onto this plate. Clear the plate and say so, or reconnect to the internet and try again."
        )
        assert _no_verdict_sentence(state, bridge.SIGNED_OUT) == (
            head + "Kiln can't check whether a second part fits safely beside it because Kiln is signed out, "
            "so it won't slice onto this plate. Clear the plate and say so, or sign in and try again."
        )
        for reason in (bridge.NOT_ANSWERED, None, "something else"):
            assert _no_verdict_sentence(state, reason) == (
                head + "Kiln can't check whether a second part fits safely beside it because Kiln's clearance check "
                "didn't answer, so it won't slice onto this plate. Clear the plate and say so, or wait a minute and try again."
            )

    def test_an_unknown_height_drops_the_clause_rather_than_printing_none(self, monkeypatch):
        from kiln.plugins.slicer_tools import _no_verdict_sentence, _plate_holds_sentence

        monkeypatch.setattr(PlateState, "since_clock", lambda self: "Sep 20 09:03")
        assert _plate_holds_sentence(self._state(None)) == "The last print, jar v2, is still on the plate (since Sep 20 09:03)."
        assert "None" not in _no_verdict_sentence(self._state(None), bridge.OFFLINE)
        assert _plate_holds_sentence(self._state(18.5)) == "The last print, jar v2, is still on the plate (since Sep 20 09:03, about 18.5 mm tall)."

    def test_no_codes_inside_any_sentence(self, monkeypatch):
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        for reason in (bridge.OFFLINE, bridge.SIGNED_OUT, bridge.NOT_ANSWERED):
            text = _no_verdict_sentence(self._state(42.0), reason)
            assert "_" not in text and "SERVER" not in text and "KILN_" not in text

    def test_the_job_name_is_prettified_the_same_way_everywhere(self):
        from kiln.plugins.slicer_tools import _pretty_job_name

        assert _pretty_job_name("jar_v2.gcode.3mf") == "jar v2"
        assert _pretty_job_name("/tmp/x/Phone-Stand_final.3mf") == "Phone Stand final"
        assert _pretty_job_name("coaster.gcode") == "coaster"
        assert _pretty_job_name("") == "the last part"


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
            "occupied": [{"name": "jar_v2.gcode.3mf", "rect_mm": [90.0, 90.0, 160.0, 160.0], "top_mm": 42.0}],
            "reserved": [],
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
            "occupied": [{"name": "jar.3mf", "rect_mm": None, "top_mm": None}],
            "reserved": [], "proposed": None, "source": "record_box",
        }
        assert PlateState(machine="m", status="clear").occupancy([256, 256]) is None
